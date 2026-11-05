import asyncio
import json
import math
import os
import shlex
import subprocess
from collections import OrderedDict
from typing import Literal, Protocol

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from google import genai
from google.genai import errors, types

from app.core.config import Settings
from app.core.errors import ServiceError

_REMOTE_BEDROCK_EMBED_SCRIPT = """
import json, sys, boto3
from botocore.config import Config
req = json.loads(sys.stdin.read())
client = boto3.client(
    "bedrock-runtime",
    config=Config(region_name=req["region"], retries={"max_attempts": 3, "mode": "standard"}),
)
out = []
for text in req["texts"]:
    resp = client.invoke_model(
        modelId=req["model_id"],
        body=json.dumps({"inputText": text, "dimensions": req["dimensions"], "normalize": True}),
    )
    body = json.loads(resp["body"].read())
    out.append(body.get("embedding", []))
sys.stdout.write(json.dumps(out))
""".strip()

_TITAN_V2_VALID_DIMS = (256, 512, 1024)


class Embedder(Protocol):
    @property
    def space(self) -> str: ...
    async def documents(self, texts: list[str]) -> list[list[float]]: ...
    async def query(self, text: str) -> list[float]: ...


def validate_vectors(vectors: list[list[float]], count: int, dimensions: int) -> None:
    if len(vectors) != count or any(
        len(vector) != dimensions
        or not all(math.isfinite(x) for x in vector)
        or sum(x * x for x in vector) <= 0
        for vector in vectors
    ):
        raise ServiceError(502, "INVALID_EMBEDDING_RESPONSE", "Invalid embedding vectors")


_QUERY_EMBEDDING_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_MAX_QUERY_CACHE_SIZE = 256


class GeminiEmbedder:
    """Separate embedding model; the 3.5 text-generation model is not an embedder."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def space(self) -> str:
        return f"{self.settings.gemini_embedding_model}:{self.settings.embedding_dimensions}"

    async def _embed(
        self, texts: list[str], task: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
    ) -> list[list[float]]:
        if not texts:
            return []
        api_keys = self.settings.candidate_api_keys
        if not api_keys:
            raise ServiceError(503, "GEMINI_NOT_CONFIGURED", "Set Gemini API key")
        max_retries = max(1, self.settings.gemini_max_retries)
        last_error: ServiceError | None = None
        for key_idx, key in enumerate(api_keys):
            async with genai.Client(
                api_key=key,
                http_options=types.HttpOptions(
                    timeout=self.settings.gemini_timeout_ms,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ).aio as client:
                for attempt in range(max_retries):
                    try:
                        response = await client.models.embed_content(
                            model=self.settings.gemini_embedding_model,
                            contents=texts,
                            config=types.EmbedContentConfig(
                                task_type=task,
                                output_dimensionality=self.settings.embedding_dimensions,
                            ),
                        )
                        vectors = [item.values or [] for item in (response.embeddings or [])]
                        validate_vectors(vectors, len(texts), self.settings.embedding_dimensions)
                        return vectors
                    except errors.APIError as exc:
                        status = exc.code if exc.code in {402, 429, 503, 504} else 502
                        err = ServiceError(
                            status, "EMBEDDING_API_ERROR", "Gemini embedding request failed"
                        )
                        err.__cause__ = exc
                        last_error = err
                        if exc.code in {402, 429}:
                            break
                        if exc.code not in {500, 502, 503, 504}:
                            raise err from exc
                    except httpx.TimeoutException as exc:
                        err = ServiceError(504, "EMBEDDING_TIMEOUT", "Embedding request timed out")
                        err.__cause__ = exc
                        last_error = err
                    except (httpx.TransportError, OSError) as exc:
                        err = ServiceError(
                            502, "EMBEDDING_CONNECTION_FAILED", "Embedding connection failed"
                        )
                        err.__cause__ = exc
                        last_error = err
                    if (attempt + 1 < max_retries) or (key_idx + 1 < len(api_keys)):
                        await asyncio.sleep(0.4 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def query(self, text: str) -> list[float]:
        cache_key = (self.space, text.strip())
        cached = _QUERY_EMBEDDING_CACHE.get(cache_key)
        if cached is not None:
            _QUERY_EMBEDDING_CACHE.move_to_end(cache_key)
            return list(cached)
        vector = (await self._embed([text], "RETRIEVAL_QUERY"))[0]
        _QUERY_EMBEDDING_CACHE[cache_key] = list(vector)
        if len(_QUERY_EMBEDDING_CACHE) > _MAX_QUERY_CACHE_SIZE:
            _QUERY_EMBEDDING_CACHE.popitem(last=False)
        return vector


class BedrockEmbedder:
    """AWS Bedrock Titan Text Embeddings V2 adapter."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def dimensions(self) -> int:
        configured = self.settings.embedding_dimensions
        if configured in _TITAN_V2_VALID_DIMS:
            return configured
        return 1024

    @property
    def space(self) -> str:
        return f"{self.settings.bedrock_embedding_model}:{self.dimensions}"

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

    def _embed_via_ssh(self, texts: list[str], dims: int) -> list[list[float]]:
        host = self.settings.bedrock_ssh_host.strip()
        user = self.settings.bedrock_ssh_user.strip() or "ubuntu"
        key_path = os.path.expanduser(self.settings.bedrock_ssh_key_path.strip())
        timeout_sec = max(20, int(self.settings.gemini_timeout_ms / 1000))
        payload = json.dumps(
            {
                "region": self.settings.aws_region,
                "model_id": self.settings.bedrock_embedding_model,
                "dimensions": dims,
                "texts": texts,
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
                f"$HOME/venv/bin/python -c {shlex.quote(_REMOTE_BEDROCK_EMBED_SCRIPT)}",
            ],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0:
            stderr_msg = proc.stderr.decode("utf-8", errors="replace").strip()
            cause = RuntimeError(stderr_msg or f"ssh exit code {proc.returncode}")
            raise ServiceError(
                502, "EMBEDDING_CONNECTION_FAILED", "Bedrock embedding SSH relay failed"
            ) from cause
        return json.loads(proc.stdout.decode("utf-8", errors="replace"))

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        dims = self.dimensions
        try:
            client = self._build_client()
            vectors: list[list[float]] = []
            for text in texts:
                resp = client.invoke_model(
                    modelId=self.settings.bedrock_embedding_model,
                    body=json.dumps({"inputText": text, "dimensions": dims, "normalize": True}),
                )
                body = json.loads(resp["body"].read())
                vectors.append(body.get("embedding", []))
        except NoCredentialsError:
            if (
                self.settings.bedrock_ssh_host.strip()
                and self.settings.bedrock_ssh_key_path.strip()
            ):
                vectors = self._embed_via_ssh(texts, dims)
            else:
                raise
        validate_vectors(vectors, len(texts), dims)
        return vectors

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return await asyncio.to_thread(self._embed_sync, texts)
        except ServiceError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise ServiceError(504, "EMBEDDING_TIMEOUT", "Embedding request timed out") from exc
        except NoCredentialsError as exc:
            raise ServiceError(
                503,
                "GEMINI_NOT_CONFIGURED",
                "Set AWS Bedrock credentials or AWS_BEARER_TOKEN_BEDROCK",
            ) from exc
        except ClientError as exc:
            http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 502)
            status = http_status if http_status in {402, 429, 503, 504} else 502
            raise ServiceError(
                status, "EMBEDDING_API_ERROR", "Bedrock embedding request failed"
            ) from exc
        except (BotoCoreError, OSError, json.JSONDecodeError) as exc:
            raise ServiceError(
                502, "EMBEDDING_CONNECTION_FAILED", "Embedding connection failed"
            ) from exc

    async def documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def query(self, text: str) -> list[float]:
        cache_key = (self.space, text.strip())
        cached = _QUERY_EMBEDDING_CACHE.get(cache_key)
        if cached is not None:
            _QUERY_EMBEDDING_CACHE.move_to_end(cache_key)
            return list(cached)
        vector = (await self._embed([text]))[0]
        _QUERY_EMBEDDING_CACHE[cache_key] = list(vector)
        if len(_QUERY_EMBEDDING_CACHE) > _MAX_QUERY_CACHE_SIZE:
            _QUERY_EMBEDDING_CACHE.popitem(last=False)
        return vector


def create_embedder(settings: Settings) -> Embedder:
    provider = settings.llm_provider
    if provider == "bedrock":
        return BedrockEmbedder(settings)
    if provider == "gemini":
        return GeminiEmbedder(settings)
    if settings.has_bedrock_credentials:
        return BedrockEmbedder(settings)
    return GeminiEmbedder(settings)
