import math
from typing import Literal, Protocol

import httpx
from google import genai
from google.genai import errors, types

from app.core.config import Settings
from app.core.errors import ServiceError


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
        key = self.settings.gemini_api_key.get_secret_value()
        if not key:
            raise ServiceError(503, "GEMINI_NOT_CONFIGURED", "Set Gemini API key")
        try:
            async with genai.Client(
                api_key=key,
                http_options=types.HttpOptions(
                    timeout=self.settings.gemini_timeout_ms,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ).aio as client:
                response = await client.models.embed_content(
                    model=self.settings.gemini_embedding_model,
                    contents=texts,
                    config=types.EmbedContentConfig(
                        task_type=task,
                        output_dimensionality=self.settings.embedding_dimensions,
                    ),
                )
        except errors.APIError as exc:
            status = exc.code if exc.code in {402, 429, 503, 504} else 502
            raise ServiceError(
                status, "EMBEDDING_API_ERROR", "Gemini embedding request failed"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ServiceError(504, "EMBEDDING_TIMEOUT", "Embedding request timed out") from exc
        except httpx.TransportError as exc:
            raise ServiceError(
                502, "EMBEDDING_CONNECTION_FAILED", "Embedding connection failed"
            ) from exc
        vectors = [item.values or [] for item in (response.embeddings or [])]
        validate_vectors(vectors, len(texts), self.settings.embedding_dimensions)
        return vectors

    async def documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def query(self, text: str) -> list[float]:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]
