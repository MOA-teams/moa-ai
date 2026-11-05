import asyncio
import math
from collections import OrderedDict
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
