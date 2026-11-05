import asyncio
import json
import os
import shlex
import subprocess
from typing import TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import ServiceError
from app.services.llm.base import StructuredLLM
from app.services.llm.gemini import GeminiLLM

T = TypeVar("T", bound=BaseModel)

_REMOTE_BEDROCK_SCRIPT = """
import json, sys, boto3
from botocore.config import Config
req = json.loads(sys.stdin.read())
client = boto3.client(
    "bedrock-runtime",
    config=Config(region_name=req["region"], retries={"max_attempts": 3, "mode": "standard"}),
)
resp = client.converse(
    modelId=req["model_id"],
    system=[{"text": req["system_prompt"]}],
    messages=[{"role": "user", "content": [{"text": req["data"]}]}],
    inferenceConfig={"maxTokens": req["max_tokens"], "temperature": 0},
)
blocks = resp.get("output", {}).get("message", {}).get("content", [])
text = next((b.get("text", "") for b in blocks if "text" in b), "").strip()
sys.stdout.write(text)
""".strip()


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if "\n" in cleaned:
            first, rest = cleaned.split("\n", 1)
            if first.strip().lower() in {"json", ""}:
                cleaned = rest
    return cleaned.strip()


class BedrockLLM:
    """AWS Bedrock Converse API adapter implementing StructuredLLM."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _build_client(self):
        bearer = self.settings.bedrock_bearer_token.get_secret_value().strip()
        if bearer:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer

        timeout_sec = max(10, int(self.settings.gemini_timeout_ms / 1000))
        cfg = Config(
            region_name=self.settings.aws_region,
            retries={"max_attempts": 3, "mode": "standard"},
            read_timeout=timeout_sec,
            connect_timeout=10,
        )
        return boto3.client("bedrock-runtime", config=cfg)

    def _build_system_prompt(self, instruction: str, json_schema: dict) -> str:
        schema_text = json.dumps(json_schema, ensure_ascii=False, indent=2)
        return (
            f"{instruction}\n\n"
            "You MUST respond with a single valid JSON object only. "
            "No markdown, no code fences, no explanation. "
            "The JSON MUST conform exactly to this JSON Schema:\n"
            f"{schema_text}"
        )

    def _converse_via_ssh(self, system_prompt: str, data: str) -> str:
        host = self.settings.bedrock_ssh_host.strip()
        user = self.settings.bedrock_ssh_user.strip() or "ubuntu"
        key_path = os.path.expanduser(self.settings.bedrock_ssh_key_path.strip())
        timeout_sec = max(15, int(self.settings.gemini_timeout_ms / 1000))
        payload = json.dumps(
            {
                "region": self.settings.aws_region,
                "model_id": self.settings.bedrock_model_id,
                "max_tokens": self.settings.bedrock_max_tokens,
                "system_prompt": system_prompt,
                "data": data,
            },
            ensure_ascii=False,
        )
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=10",
                "-i",
                key_path,
                f"{user}@{host}",
                f"$HOME/venv/bin/python -c {shlex.quote(_REMOTE_BEDROCK_SCRIPT)}",
            ],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0:
            stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()
            cause = RuntimeError(stderr_msg or f"ssh exit code {proc.returncode}")
            raise ServiceError(502, "GEMINI_UNAVAILABLE", "Bedrock SSH relay failed") from cause
        return _strip_code_fence(proc.stdout.decode("utf-8", errors="replace"))

    def _converse_sync(self, instruction: str, data: str, json_schema: dict) -> str:
        system_prompt = self._build_system_prompt(instruction, json_schema)
        try:
            client = self._build_client()
            resp = client.converse(
                modelId=self.settings.bedrock_model_id,
                system=[{"text": system_prompt}],
                messages=[{"role": "user", "content": [{"text": data}]}],
                inferenceConfig={
                    "maxTokens": self.settings.bedrock_max_tokens,
                    "temperature": 0,
                },
            )
            blocks = resp.get("output", {}).get("message", {}).get("content", [])
            text = next((b.get("text", "") for b in blocks if "text" in b), "").strip()
            return _strip_code_fence(text)
        except NoCredentialsError:
            if (
                self.settings.bedrock_ssh_host.strip()
                and self.settings.bedrock_ssh_key_path.strip()
            ):
                return self._converse_via_ssh(system_prompt, data)
            raise

    async def generate(self, *, instruction: str, data: str, schema: type[T]) -> T:
        try:
            raw_text = await asyncio.to_thread(
                self._converse_sync,
                instruction,
                data,
                schema.model_json_schema(),
            )
        except ServiceError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise ServiceError(504, "GEMINI_TIMEOUT", "Bedrock request timed out") from exc
        except NoCredentialsError as exc:
            raise ServiceError(
                503,
                "GEMINI_NOT_CONFIGURED",
                "Set AWS Bedrock credentials or AWS_BEARER_TOKEN_BEDROCK",
            ) from exc
        except ClientError as exc:
            err_info = exc.response.get("Error", {})
            err_code = str(err_info.get("Code", ""))
            http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 502)
            if (
                err_code in {"ThrottlingException", "TooManyRequestsException"}
                or http_status == 429
            ):
                raise ServiceError(
                    429, "GEMINI_RATE_LIMITED", "Bedrock quota or rate limit reached"
                ) from exc
            if err_code in {
                "AccessDeniedException",
                "UnrecognizedClientException",
                "InvalidSignatureException",
                "ExpiredTokenException",
                "ResourceNotFoundException",
            } or http_status in {401, 403, 404}:
                raise ServiceError(
                    503, "GEMINI_CONFIGURATION_ERROR", "Check Bedrock credentials and model ID"
                ) from exc
            if err_code == "ValidationException" or http_status == 400:
                raise ServiceError(
                    502, "GEMINI_REQUEST_REJECTED", "Bedrock rejected the request format"
                ) from exc
            if err_code == "ModelTimeoutException" or http_status == 504:
                raise ServiceError(504, "GEMINI_TIMEOUT", "Bedrock request timed out") from exc
            raise ServiceError(502, "GEMINI_UNAVAILABLE", "Bedrock request failed") from exc
        except BotoCoreError as exc:
            raise ServiceError(502, "GEMINI_UNAVAILABLE", "Bedrock connection failed") from exc

        if not raw_text:
            raise ServiceError(502, "EMPTY_LLM_RESPONSE", "Model returned no structured output")
        try:
            return schema.model_validate_json(raw_text)
        except ValidationError as exc:
            raise ServiceError(
                502, "INVALID_LLM_RESPONSE", "Model returned invalid structured output"
            ) from exc


class AutoFallbackLLM:
    """Tries primary LLM first and falls back to secondary LLM when unconfigured or rate-limited."""

    def __init__(self, primary: StructuredLLM, secondary: StructuredLLM):
        self.primary = primary
        self.secondary = secondary

    async def generate(self, *, instruction: str, data: str, schema: type[T]) -> T:
        try:
            return await self.primary.generate(instruction=instruction, data=data, schema=schema)
        except ServiceError as primary_exc:
            if primary_exc.code not in {
                "GEMINI_NOT_CONFIGURED",
                "GEMINI_CONFIGURATION_ERROR",
                "GEMINI_RATE_LIMITED",
                "GEMINI_UNAVAILABLE",
                "GEMINI_TIMEOUT",
                "GEMINI_CREDITS_DEPLETED",
            }:
                raise
            try:
                return await self.secondary.generate(
                    instruction=instruction, data=data, schema=schema
                )
            except ServiceError:
                raise primary_exc from None


def create_llm(settings: Settings) -> StructuredLLM:
    provider = settings.llm_provider
    if provider == "bedrock":
        return BedrockLLM(settings)
    if provider == "gemini":
        return GeminiLLM(settings)
    # "auto": if Bedrock bearer token or AWS credentials exist, try Bedrock first then Gemini;
    # otherwise try Gemini first then Bedrock.
    if settings.has_bedrock_credentials:
        return AutoFallbackLLM(BedrockLLM(settings), GeminiLLM(settings))
    return AutoFallbackLLM(GeminiLLM(settings), BedrockLLM(settings))
