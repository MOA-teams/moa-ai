import asyncio

from app.core.errors import ServiceError
from app.schemas.rag import (
    SearchRequest,
    SearchResult,
    SourceInput,
    SourceStatusBatch,
    SourceStatusResult,
    SourceView,
)
from app.services.rag.chunking import split_source
from app.services.rag.embedding import Embedder
from app.services.rag.vector_store import IndexTicket, VectorStore


class RagService:
    def __init__(self, store: VectorStore, embedder: Embedder):
        self.store = store
        self.embedder = embedder

    async def prepare(self, source: SourceInput) -> IndexTicket | None:
        chunks = split_source(source)
        if not chunks or len(chunks) > 64:
            raise ServiceError(413, "SOURCE_CHUNK_LIMIT", "Source must contain 1 to 64 text chunks")
        return await asyncio.to_thread(self.store.prepare, source, chunks, self.embedder.space)

    async def index(self, ticket: IndexTicket) -> None:
        try:
            vectors = await self.embedder.documents(
                [f"{ticket.source.title}\n{chunk.text}" for chunk in ticket.chunks]
            )
            if len(vectors) != len(ticket.chunks):
                raise ServiceError(502, "INVALID_EMBEDDING_RESPONSE", "Embedding count mismatch")
            await asyncio.to_thread(self.store.complete, ticket, vectors)
        except ServiceError as exc:
            await asyncio.to_thread(self.store.fail, ticket, exc.code)
            raise
        except Exception as exc:
            await asyncio.to_thread(self.store.fail, ticket, "INDEXING_FAILED")
            raise ServiceError(502, "INDEXING_FAILED", "Source indexing failed") from exc

    async def index_background(self, ticket: IndexTicket) -> None:
        try:
            await self.index(ticket)
        except ServiceError:
            # The 202 response is already sent; failure is visible through the source-status API.
            return

    async def ingest(self, source: SourceInput) -> SourceView:
        ticket = await self.prepare(source)
        if ticket is not None:
            await self.index(ticket)
        return await self.get(
            source.tenant_id, source.source_type, source.source_id, source.version
        )

    async def get(
        self, tenant: str, source_type: str, source_id: str, version: int | str
    ) -> SourceView:
        return await asyncio.to_thread(self.store.get, tenant, source_type, source_id, version)

    async def update_statuses(self, batch: SourceStatusBatch) -> SourceStatusResult:
        return await asyncio.to_thread(self.store.update_statuses, batch)

    async def search(self, request: SearchRequest) -> SearchResult:
        if not request.query.strip():
            raise ServiceError(400, "EMPTY_QUERY", "Query is blank")
        vector = await self.embedder.query(request.query)
        return await asyncio.to_thread(self.store.search, request, vector, self.embedder.space)
