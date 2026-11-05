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


async def test_gemini_retries_503_and_falls_back_to_next_model(settings, monkeypatch):
    from google.genai import errors

    settings.gemini_api_key = SecretStr("test-key")
    settings.gemini_model = "primary-model"
    settings.gemini_fallback_models = "fallback-model"
    settings.gemini_max_retries = 2
    called_models = []

    class FakeClient:
        async def __aenter__(self):
            self.models = self
            return self

        async def __aexit__(self, *args):
            pass

        async def generate_content(self, **kwargs):
            model = kwargs["model"]
            called_models.append(model)
            if model == "primary-model":
                raise errors.APIError(503, {"error": {"message": "High demand"}})
            return SimpleNamespace(text='{"summary":"recovered-by-fallback"}')

    monkeypatch.setattr(
        "app.services.llm.gemini.genai.Client",
        lambda **kwargs: SimpleNamespace(aio=FakeClient()),
    )
    result = await GeminiLLM(settings).generate(instruction="rules", data="query", schema=Answer)
    assert result.summary == "recovered-by-fallback"
    assert called_models == ["primary-model", "primary-model", "fallback-model"]


async def test_gemini_429_switches_immediately_to_next_model_and_rotates_keys(
    settings, monkeypatch
):
    from google.genai import errors

    settings.gemini_api_key = SecretStr("exhausted-key,active-key")
    settings.gemini_model = "quota-exceeded-model"
    settings.gemini_fallback_models = "healthy-model"
    settings.gemini_max_retries = 2
    attempts = []

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def __aenter__(self):
            self.models = self
            return self

        async def __aexit__(self, *args):
            pass

        async def generate_content(self, **kwargs):
            model = kwargs["model"]
            attempts.append((self.api_key, model))
            if self.api_key == "exhausted-key" or model == "quota-exceeded-model":
                raise errors.APIError(429, {"error": {"message": "Quota exceeded"}})
            return SimpleNamespace(text='{"summary":"ok-from-second-key-and-model"}')

    monkeypatch.setattr(
        "app.services.llm.gemini.genai.Client",
        lambda **kwargs: SimpleNamespace(aio=FakeClient(kwargs["api_key"])),
    )
    result = await GeminiLLM(settings).generate(instruction="rules", data="query", schema=Answer)
    assert result.summary == "ok-from-second-key-and-model"
    assert attempts == [
        ("exhausted-key", "quota-exceeded-model"),
        ("exhausted-key", "healthy-model"),
        ("active-key", "quota-exceeded-model"),
        ("active-key", "healthy-model"),
    ]


async def test_bedrock_llm_parses_code_fenced_json_and_auto_fallback_works(settings, monkeypatch):
    from app.services.llm.bedrock import BedrockLLM, create_llm

    seen = {}

    class FakeBedrockClient:
        def converse(self, **kwargs):
            seen.update(kwargs)
            return {
                "output": {
                    "message": {
                        "content": [
                            {"text": '```json\n{"summary": "from-bedrock-haiku"}\n```'},
                        ]
                    }
                }
            }

    monkeypatch.setattr(
        "app.services.llm.bedrock.boto3.client", lambda *a, **kw: FakeBedrockClient()
    )
    settings.llm_provider = "bedrock"
    settings.bedrock_model_id = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

    llm = create_llm(settings)
    assert isinstance(llm, BedrockLLM)
    ans = await llm.generate(instruction="classify", data="input text", schema=Answer)
    assert ans.summary == "from-bedrock-haiku"
    assert seen["modelId"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert seen["inferenceConfig"]["temperature"] == 0
