"""Local Chroma RAG smoke test. No answer generation or customer delivery."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from app.cli.analyze import diagnostic_details
from app.core.config import get_settings
from app.core.errors import ServiceError
from app.schemas.rag import SearchRequest, SourceInput
from app.services.rag.chroma_store import ChromaVectorStore
from app.services.rag.embedding import create_embedder
from app.services.rag.service import RagService


async def retrieve(query: str, sources: Path | None, tenant: str) -> dict:
    settings = get_settings()
    if tenant not in settings.tenant_ids:
        raise ServiceError(403, "FORBIDDEN", "Tenant access denied")
    store = ChromaVectorStore(settings.chroma_path)
    service = RagService(store, create_embedder(settings))
    try:
        if sources is not None:
            inputs = TypeAdapter(list[SourceInput]).validate_json(sources.read_text())
            if any(source.tenant_id != tenant for source in inputs):
                raise ServiceError(403, "FORBIDDEN", "Source file contains another tenant")
            for source in inputs:
                await service.ingest(source)
        result = await service.search(
            SearchRequest(
                tenant_id=tenant,
                query=query,
                min_score=settings.rag_min_score,
            )
        )
        return result.model_dump(mode="json")
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index and search sample documents in local Chroma"
    )
    parser.add_argument("query")
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--tenant", default="syncnote")
    args = parser.parse_args()
    try:
        result = asyncio.run(retrieve(args.query, args.sources, args.tenant))
    except ServiceError as exc:
        print(
            json.dumps(
                {
                    "error": {"code": exc.code, "message": exc.message},
                    "diagnostics": diagnostic_details(exc, 0),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValidationError):
        print('{"error":{"code":"INVALID_INPUT"}}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
