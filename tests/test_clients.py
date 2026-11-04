from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from app.core.errors import ServiceError
from app.schemas.common import ErrorDetail, RunStatus
from app.schemas.pipeline import PipelineResult
from app.services.backend_client import BackendClient
from app.services.llm.gemini import GeminiLLM


class Answer(BaseModel):
    summary: str


async def test_gemini_uses_structured_output_and_closes_client(settings, monkeypatch):
    settings.gemini_api_key = SecretStr("test-key")
    settings.gemini_model = "test-model"
    seen = {}

    class FakeAsyncClient:
        async def __aenter__(self):
            self.models = self
            return self

        async def __aexit__(self, *args):
            seen["closed"] = True

        async def generate_content(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(text='{"summary":"ok"}')

    monkeypatch.setattr(
        "app.services.llm.gemini.genai.Client",
        lambda **kwargs: SimpleNamespace(aio=FakeAsyncClient()),
    )
    result = await GeminiLLM(settings).generate(
        instruction="Follow system rules",
        data="untrusted inquiry",
        schema=Answer,
    )
    assert result.summary == "ok"
    assert seen["contents"] == "untrusted inquiry"
    assert seen["config"].system_instruction == "Follow system rules"
    assert seen["config"].response_json_schema == Answer.model_json_schema()
    assert seen["closed"]


async def test_gemini_missing_key_does_not_call_network(settings):
    with pytest.raises(ServiceError) as exc:
        await GeminiLLM(settings).generate(instruction="rules", data="query", schema=Answer)
    assert exc.value.code == "GEMINI_NOT_CONFIGURED"


async def test_gemini_validates_provider_output(settings, monkeypatch):
    settings.gemini_api_key = SecretStr("test-key")
    settings.gemini_model = "test-model"

    class FakeClient:
        async def __aenter__(self):
            self.models = self
            return self

        async def __aexit__(self, *args):
            pass

        async def generate_content(self, **kwargs):
            return SimpleNamespace(text='{"wrong":true}')

    monkeypatch.setattr(
        "app.services.llm.gemini.genai.Client",
        lambda **kwargs: SimpleNamespace(aio=FakeClient()),
    )
    with pytest.raises(ServiceError) as exc:
        await GeminiLLM(settings).generate(instruction="rules", data="query", schema=Answer)
    assert exc.value.code == "INVALID_LLM_RESPONSE"


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


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "GEMINI_REQUEST_REJECTED"),
        (402, "GEMINI_CREDITS_DEPLETED"),
        (429, "GEMINI_RATE_LIMITED"),
        (403, "GEMINI_CONFIGURATION_ERROR"),
        (404, "GEMINI_CONFIGURATION_ERROR"),
        (500, "GEMINI_UNAVAILABLE"),
    ],
)
async def test_gemini_errors_are_sanitized(settings, monkeypatch, status, expected):
    from google.genai import errors

    settings.gemini_api_key = SecretStr("sensitive-test-key")
    settings.gemini_model = "test-model"

    class FakeClient:
        async def __aenter__(self):
            self.models = self
            return self

        async def __aexit__(self, *args):
            pass

        async def generate_content(self, **kwargs):
            raise errors.APIError(status, {"error": {"message": "sensitive-test-key"}})

    monkeypatch.setattr(
        "app.services.llm.gemini.genai.Client", lambda **kwargs: SimpleNamespace(aio=FakeClient())
    )
    with pytest.raises(ServiceError) as exc:
        await GeminiLLM(settings).generate(instruction="rules", data="query", schema=Answer)
    assert exc.value.code == expected
    assert "sensitive-test-key" not in exc.value.message
