from dataclasses import dataclass
from typing import Protocol

from app.schemas.rag import (
    SearchRequest,
    SearchResult,
    SourceInput,
    SourceStatusBatch,
    SourceStatusResult,
    SourceView,
    TextChunk,
)


@dataclass(frozen=True)
class IndexTicket:
    key: str
    generation: str
    source: SourceInput
    chunks: list[TextChunk]
    space: str


class VectorStore(Protocol):
    """Synchronous storage operations are called in a thread by RagService."""

    def prepare(
        self, source: SourceInput, chunks: list[TextChunk], space: str
    ) -> IndexTicket | None: ...
    def complete(self, ticket: IndexTicket, vectors: list[list[float]]) -> bool: ...
    def fail(self, ticket: IndexTicket, error: str) -> None: ...
    def get(
        self,
        tenant: str,
        source_type: str,
        source_id: str,
        version: int | str,
        *,
        space: str | None = None,
    ) -> SourceView: ...
    def update_statuses(self, batch: SourceStatusBatch) -> SourceStatusResult: ...
    def search(
        self,
        request: SearchRequest,
        vector: list[float],
        space: str,
        *,
        source_types: set[str] | None = None,
    ) -> SearchResult: ...
    def list_sources(self, tenant: str, *, source_type: str | None = None) -> list[SourceInput]: ...
    def list_current_public_sources(self, tenant: str, space: str) -> list[SourceInput]: ...
