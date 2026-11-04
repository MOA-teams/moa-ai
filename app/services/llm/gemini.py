from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import ServiceError

T = TypeVar("T", bound=BaseModel)


class GeminiLLM:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def generate(self, *, instruction: str, data: str, schema: type[T]) -> T:
        key = self.settings.gemini_api_key.get_secret_value()
        if not key or not self.settings.gemini_model:
            raise ServiceError(503, "GEMINI_NOT_CONFIGURED", "Set Gemini key and model")
        # Bound the free-tier request count. Higher-level pipeline retries are explicit.
        try:
            async with genai.Client(
                api_key=key,
                http_options=types.HttpOptions(
                    timeout=self.settings.gemini_timeout_ms,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ).aio as client:
                response = await client.models.generate_content(
                    model=self.settings.gemini_model,
                    contents=data,
                    config=types.GenerateContentConfig(
                        system_instruction=instruction,
                        response_mime_type="application/json",
                        response_json_schema=schema.model_json_schema(),
                        temperature=0,
                    ),
                )
        except errors.APIError as exc:
            code = exc.code
            if code == 402:
                raise ServiceError(
                    402, "GEMINI_CREDITS_DEPLETED", "Gemini project prepaid credits are depleted"
                ) from exc
            if code == 429:
                raise ServiceError(
                    429, "GEMINI_RATE_LIMITED", "Gemini quota or rate limit reached"
                ) from exc
            if code == 400:
                raise ServiceError(
                    502, "GEMINI_REQUEST_REJECTED", "Gemini rejected the request format"
                ) from exc
            if code in {401, 403, 404}:
                raise ServiceError(
                    503, "GEMINI_CONFIGURATION_ERROR", "Check Gemini key and model"
                ) from exc
            raise ServiceError(502, "GEMINI_UNAVAILABLE", "Gemini request failed") from exc
        except httpx.TimeoutException as exc:
            raise ServiceError(504, "GEMINI_TIMEOUT", "Gemini request timed out") from exc
        except httpx.TransportError as exc:
            raise ServiceError(502, "GEMINI_UNAVAILABLE", "Gemini connection failed") from exc
        if not response.text:
            raise ServiceError(502, "EMPTY_LLM_RESPONSE", "Model returned no structured output")
        try:
            return schema.model_validate_json(response.text)
        except ValidationError as exc:
            raise ServiceError(
                502, "INVALID_LLM_RESPONSE", "Model returned invalid structured output"
            ) from exc
