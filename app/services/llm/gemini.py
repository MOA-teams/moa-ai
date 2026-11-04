from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

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
        # Data is untrusted context, never part of the system instruction.
        async with genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=self.settings.gemini_timeout_ms),
        ).aio as client:
            response = await client.models.generate_content(
                model=self.settings.gemini_model,
                contents=data,
                config=types.GenerateContentConfig(
                    system_instruction=instruction,
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0,
                ),
            )
        if not response.text:
            raise ServiceError(502, "EMPTY_LLM_RESPONSE", "Model returned no structured output")
        return schema.model_validate_json(response.text)
