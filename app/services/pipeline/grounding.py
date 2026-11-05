"""초안 생성과 기존 답변 갱신에서 함께 사용하는 출처·최신성 검사."""

from app.core.errors import ServiceError
from app.schemas.common import CheckStatus, DocEvidence
from app.schemas.pipeline import Citation, Verification
from app.schemas.rag import SearchHit
from app.services.rag.service import RagService


def document_map(facts: list[SearchHit]) -> dict[str, DocEvidence]:
    """이미 검증한 검색 결과에서만 인용 가능한 문서 목록을 만든다."""
    documents: dict[str, DocEvidence] = {}
    for hit in facts:
        refs = list(hit.evidence.docs)
        if hit.source_type == "문서" and isinstance(hit.version, int):
            refs.append(DocEvidence(doc_id=hit.source_id, version=hit.version))
        for doc in refs:
            if doc.doc_id in documents and documents[doc.doc_id] != doc:
                raise ServiceError(502, "INVALID_DRAFT_CITATIONS", "Conflicting document versions")
            documents[doc.doc_id] = doc
    return documents


def verified_citations(
    documents: dict[str, DocEvidence],
    used_doc_ids: list[str],
    citations: list[tuple[str, list[str]]],
) -> tuple[list[DocEvidence], list[Citation]]:
    """잘못된 인용을 다른 문서로 바꾸지 않고 거절한다."""
    used_ids = set(used_doc_ids)
    if not used_ids or not used_ids <= documents.keys() or not citations:
        raise ServiceError(502, "INVALID_DRAFT_CITATIONS", "Missing or unknown document citations")

    result: list[Citation] = []
    cited_ids: set[str] = set()
    for text, doc_ids in citations:
        if not text.strip() or not doc_ids or not set(doc_ids) <= used_ids:
            raise ServiceError(
                502, "INVALID_DRAFT_CITATIONS", "Citation references an unused document"
            )
        unique_ids = list(dict.fromkeys(doc_ids))
        result.append(Citation(text=text, docs=[documents[doc_id] for doc_id in unique_ids]))
        cited_ids.update(unique_ids)
    if used_ids != cited_ids:
        raise ServiceError(502, "INVALID_DRAFT_CITATIONS", "Used documents require citations")

    return [documents[doc_id] for doc_id in dict.fromkeys(used_doc_ids)], result


async def facts_are_current(rag: RagService, tenant: str, facts: list[SearchHit]) -> bool:
    """LLM 호출 중 변경을 감지한다. 상태·버전·source_revision을 한 시점의 자료와 비교한다."""
    current = await rag.list_current_public_sources(tenant)
    keys = {
        (source.source_type, source.source_id, source.version, source.source_revision)
        for source in current
    }
    return bool(facts) and all(
        (hit.source_type, hit.source_id, hit.version, hit.source_revision) in keys
        and hit.visibility == "고객 공개"
        for hit in facts
    )


def invalidate_verification(verification: Verification) -> Verification:
    """작성 중 근거가 바뀐 초안은 보존하되 자동 발송을 금지한다."""
    return verification.model_copy(
        update={
            "status": CheckStatus.FAIL,
            "checks": {
                **verification.checks,
                "evidence_current": CheckStatus.FAIL,
                "auto_response_scope": CheckStatus.FAIL,
            },
            "reasons": [
                *verification.reasons,
                "답변 작성 중 근거 자료가 변경되어 재검토가 필요합니다.",
            ],
        }
    )
