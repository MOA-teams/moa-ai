from fastapi import Request

from app.core.errors import ServiceError
from app.services.rag.chroma_store import ChromaVectorStore
from app.services.rag.embedding import create_embedder
from app.services.rag.service import RagService


def get_rag(request: Request) -> RagService:
    service = request.app.state.rag_service
    if service is not None:
        return service
    settings = request.app.state.settings
    if not settings.rag_enabled:
        raise ServiceError(503, "RAG_NOT_CONFIGURED", "Enable Chroma RAG in service settings")
    service = RagService(ChromaVectorStore(settings.chroma_path), create_embedder(settings))
    request.app.state.rag_service = service
    return service
