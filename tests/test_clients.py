import json
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel, SecretStr

from app.core.errors import ServiceError
from app.schemas.common import ErrorDetail, RunStatus
from app.schemas.pipeline import PipelineResult
from app.services.backend_client import BackendClient
from app.services.llm.bedrock import BedrockLLM, create_llm


class Answer(BaseModel):
    summary: str


async def test_bedrock_uses_converse_with_schema_and_system_instruction(settings, monkeypatch):
    seen = {}

    class Client:
        def converse(self, **kwargs):
            seen.update(kwargs)
            return {
                "output": {"message": {"content": [{"text": '```json\n{"summary":"ok"}\n```'}]}}
            }

    monkeypatch.setattr(BedrockLLM, "_build_client", lambda self: Client())
    llm = create_llm(settings)
    assert isinstance(llm, BedrockLLM)
    result = await llm.generate(instruction="Follow rules", data="untrusted text", schema=Answer)
    assert result.summary == "ok"
    assert seen["modelId"] == settings.bedrock_model_id
    assert "Follow rules" in seen["system"][0]["text"]
    assert (
        json.dumps(Answer.model_json_schema(), ensure_ascii=False, indent=2)
        in seen["system"][0]["text"]
    )
    assert seen["messages"][0]["content"][0]["text"] == "untrusted text"
    assert seen["inferenceConfig"]["temperature"] == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "EMPTY_LLM_RESPONSE"),
        ('{"wrong":"value"}', "INVALID_LLM_RESPONSE"),
        ("invalid-json", "INVALID_LLM_RESPONSE"),
    ],
)
async def test_bedrock_validates_response(settings, monkeypatch, raw, expected):
    class Client:
        def converse(self, **kwargs):
            return {"output": {"message": {"content": [{"text": raw}]}}}

    monkeypatch.setattr(BedrockLLM, "_build_client", lambda self: Client())
    with pytest.raises(ServiceError) as error:
        await BedrockLLM(settings).generate(instruction="rules", data="input", schema=Answer)
    assert error.value.code == expected


@pytest.mark.parametrize(
    "provider_code,status,expected",
    [
        ("ThrottlingException", 429, "LLM_RATE_LIMITED"),
        ("AccessDeniedException", 403, "LLM_CONFIGURATION_ERROR"),
        ("ResourceNotFoundException", 404, "LLM_CONFIGURATION_ERROR"),
        ("ValidationException", 400, "LLM_REQUEST_REJECTED"),
        ("ModelTimeoutException", 504, "LLM_TIMEOUT"),
        ("ServiceUnavailableException", 503, "LLM_UNAVAILABLE"),
    ],
)
async def test_bedrock_errors_are_sanitized(settings, monkeypatch, provider_code, status, expected):
    class Client:
        def converse(self, **kwargs):
            raise ClientError(
                {
                    "Error": {"Code": provider_code, "Message": "private-token"},
                    "ResponseMetadata": {"HTTPStatusCode": status},
                },
                "Converse",
            )

    monkeypatch.setattr(BedrockLLM, "_build_client", lambda self: Client())
    with pytest.raises(ServiceError) as error:
        await BedrockLLM(settings).generate(instruction="rules", data="input", schema=Answer)
    assert error.value.code == expected
    assert "private-token" not in error.value.message


async def test_bedrock_missing_credentials_is_actionable(settings, monkeypatch):
    def unavailable(self):
        raise NoCredentialsError()

    monkeypatch.setattr(BedrockLLM, "_build_client", unavailable)
    with pytest.raises(ServiceError) as error:
        await BedrockLLM(settings).generate(instruction="rules", data="input", schema=Answer)
    assert error.value.code == "LLM_NOT_CONFIGURED"


async def test_bedrock_transport_failure_is_sanitized(settings, monkeypatch):
    def unavailable(self):
        raise BotoCoreError()

    monkeypatch.setattr(BedrockLLM, "_build_client", unavailable)
    with pytest.raises(ServiceError) as error:
        await BedrockLLM(settings).generate(instruction="rules", data="input", schema=Answer)
    assert error.value.code == "LLM_UNAVAILABLE"


async def test_bedrock_ssh_relay_passes_customer_data_via_stdin(settings, monkeypatch):
    settings.bedrock_ssh_host = "test-host"
    settings.bedrock_ssh_key_path = "test-key.pem"
    seen = {}

    def unavailable(self):
        raise NoCredentialsError()

    def run(args, **kwargs):
        seen.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout=b'{"summary":"relay-ok"}', stderr=b"")

    monkeypatch.setattr(BedrockLLM, "_build_client", unavailable)
    monkeypatch.setattr("app.services.llm.bedrock.subprocess.run", run)
    result = await BedrockLLM(settings).generate(
        instruction="rules", data="private customer text", schema=Answer
    )
    assert result.summary == "relay-ok"
    assert "private customer text" not in " ".join(seen["args"])
    assert json.loads(seen["input"])["data"] == "private customer text"


def failed_result():
    return PipelineResult(
        run_id="RUN-000001",
        tenant_id="syncnote",
        inquiry_id="Q-1001",
        inquiry_revision=1,
        based_on_message_id="MSG-000001",
        run_status=RunStatus.FAILED,
        error=ErrorDetail(code="NOT_IMPLEMENTED", message="Not implemented"),
    )


async def test_callback_uses_fixed_path_and_tenant(settings):
    settings.backend_service_token = SecretStr("callback-test-token")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"apply_status": "applied"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await BackendClient(settings).deliver(failed_result(), client)
    assert seen[0].url.path == "/internal/v1/pipeline-results"
    assert seen[0].headers["X-Tenant-ID"] == "syncnote"
    assert seen[0].headers["Idempotency-Key"] == "pipeline-result:RUN-000001"


async def test_callback_does_not_follow_redirect(settings):
    settings.backend_service_token = SecretStr("callback-test-token")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ServiceError):
            await BackendClient(settings).deliver(failed_result(), client)
    assert len(seen) == 1
