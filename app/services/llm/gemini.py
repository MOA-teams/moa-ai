import asyncio
from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import ServiceError

T = TypeVar("T", bound=BaseModel)

TRANSIENT_HTTP_CODES = {500, 502, 503, 504}
_MODEL_ROTATION_COUNTER = 0


class GeminiLLM:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def generate(self, *, instruction: str, data: str, schema: type[T]) -> T:
        global _MODEL_ROTATION_COUNTER
        api_keys = self.settings.candidate_api_keys
        base_candidates = self.settings.candidate_models
        if not api_keys or not self.settings.gemini_model or not base_candidates:
            raise ServiceError(503, "GEMINI_NOT_CONFIGURED", "Set Gemini key and model")

        # When default multi-model fallback pool is active (>2 models), rotate starting model
        # so consecutive pipeline stages do not hammer a single model's per-minute RPM limit.
        if len(base_candidates) > 2:
            offset = _MODEL_ROTATION_COUNTER % len(base_candidates)
            _MODEL_ROTATION_COUNTER += 1
            candidates = base_candidates[offset:] + base_candidates[:offset]
        else:
            candidates = base_candidates

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
                for model_idx, model_name in enumerate(candidates):
                    for attempt in range(max_retries):
                        try:
                            response = await client.models.generate_content(
                                model=model_name,
                                contents=data,
                                config=types.GenerateContentConfig(
                                    system_instruction=instruction,
                                    response_mime_type="application/json",
                                    response_json_schema=schema.model_json_schema(),
                                    temperature=0,
                                ),
                            )
                            if not response.text:
                                raise ServiceError(
                                    502,
                                    "EMPTY_LLM_RESPONSE",
                                    "Model returned no structured output",
                                )
                            try:
                                return schema.model_validate_json(response.text)
                            except ValidationError as exc:
                                raise ServiceError(
                                    502,
                                    "INVALID_LLM_RESPONSE",
                                    "Model returned invalid structured output",
                                ) from exc
                        except errors.APIError as exc:
                            code = exc.code
                            if code == 400:
                                raise ServiceError(
                                    502,
                                    "GEMINI_REQUEST_REJECTED",
                                    "Gemini rejected the request format",
                                ) from exc
                            if code == 402:
                                err = ServiceError(
                                    402,
                                    "GEMINI_CREDITS_DEPLETED",
                                    "Gemini project prepaid credits are depleted",
                                )
                                err.__cause__ = exc
                                last_error = err
                                # Try next API key if multiple keys are configured
                                break
                            if code == 429:
                                err = ServiceError(
                                    429,
                                    "GEMINI_RATE_LIMITED",
                                    "Gemini quota or rate limit reached",
                                )
                                err.__cause__ = exc
                                last_error = err
                                # Quota is per-model in free tier; switch to next model
                                break
                            if code in {401, 403, 404}:
                                err = ServiceError(
                                    503,
                                    "GEMINI_CONFIGURATION_ERROR",
                                    "Check Gemini key and model",
                                )
                                err.__cause__ = exc
                                last_error = err
                                if code in {401, 403} and key_idx + 1 == len(api_keys):
                                    raise err from exc
                                break
                            err = ServiceError(502, "GEMINI_UNAVAILABLE", "Gemini request failed")
                            err.__cause__ = exc
                            last_error = err
                            if code not in TRANSIENT_HTTP_CODES:
                                raise err from exc
                        except httpx.TimeoutException as exc:
                            err = ServiceError(504, "GEMINI_TIMEOUT", "Gemini request timed out")
                            err.__cause__ = exc
                            last_error = err
                        except (httpx.TransportError, OSError) as exc:
                            err = ServiceError(
                                502, "GEMINI_UNAVAILABLE", "Gemini connection failed"
                            )
                            err.__cause__ = exc
                            last_error = err

                        has_more = (
                            (attempt + 1 < max_retries)
                            or (model_idx + 1 < len(candidates))
                            or (key_idx + 1 < len(api_keys))
                        )
                        if has_more:
                            await asyncio.sleep(0.4 * (attempt + 1))

        assert last_error is not None
        raise last_error
