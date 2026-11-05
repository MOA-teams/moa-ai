"""Use the same evidence search in previews and formal pipeline runs."""

from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import Sender
from app.schemas.pipeline import PipelineRequest
from app.schemas.rag import SearchRequest


def evidence_search_request(
    request: PipelineRequest, analysis: InquiryAnalysis, *, min_score: float
) -> SearchRequest:
    # Preserve the actual service/plan wording; never add prices or expected labels.
    latest = next(m.text for m in reversed(request.messages) if m.sender == Sender.CUSTOMER)
    query = analysis.search_query or analysis.summary
    return SearchRequest(
        tenant_id=request.tenant_id,
        query=query,
        lexical_query=f"{query}\n{latest}"[:3000],
        retrieval_mode="hybrid",
        purpose="고객 답변",
        min_score=min_score,
    )
