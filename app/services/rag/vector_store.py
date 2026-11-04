from typing import Protocol

from app.core.errors import FeatureNotReady
from app.schemas.rag import SourceInput


class VectorStore(Protocol):
    async def upsert(self, source: SourceInput) -> None: ...
    async def search(self, *, tenant_id: str, query: str, top_k: int) -> list[dict]: ...
    async def exclude(self, *, tenant_id: str, source_ids: list[str]) -> None: ...


class UnconfiguredVectorStore:
    async def upsert(self, source: SourceInput) -> None:
        raise FeatureNotReady("Vector store")

    async def search(self, *, tenant_id: str, query: str, top_k: int) -> list[dict]:
        raise FeatureNotReady("Vector store")

    async def exclude(self, *, tenant_id: str, source_ids: list[str]) -> None:
        raise FeatureNotReady("Vector store")
