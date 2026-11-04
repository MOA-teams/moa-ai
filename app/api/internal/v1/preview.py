from typing import Annotated

from fastapi import APIRouter, Body, Request

from app.deps.auth import ServiceTenant, check_body_tenant
from app.deps.rag import get_rag
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import ContractModel
from app.schemas.pipeline import PipelineRequest
from app.schemas.rag import SearchRequest, SearchResult
from app.services.llm.gemini import GeminiLLM
from app.services.pipeline.analyzer import InquiryAnalyzer

router = APIRouter(tags=["local preview"])

PREVIEW_EXAMPLES = {
    "error": {
        "summary": "저장 오류 → 문서 검색",
        "value": {
            "tenant_id": "syncnote",
            "inquiry_id": "Q-1001",
            "inquiry_revision": 1,
            "based_on_message_id": "MSG-000001",
            "customer_category": "기타",
            "messages": [
                {
                    "message_id": "MSG-000001",
                    "sender": "customer",
                    "text": "저장 버튼을 누르면 E502가 떠요.",
                    "sent_at": "2026-11-05T09:00:00+09:00",
                    "attachments": [],
                }
            ],
            "env": {"os": None, "app_version": None, "error_code": None},
            "review_only": False,
        },
    },
    "insufficient": {
        "summary": "정보 부족 → 추가 질문",
        "value": {
            "tenant_id": "syncnote",
            "inquiry_id": "Q-1001",
            "inquiry_revision": 1,
            "based_on_message_id": "MSG-000001",
            "customer_category": "기타",
            "messages": [
                {
                    "message_id": "MSG-000001",
                    "sender": "customer",
                    "text": "안 돼요.",
                    "sent_at": "2026-11-05T09:00:00+09:00",
                    "attachments": [],
                }
            ],
            "env": {"os": None, "app_version": None, "error_code": None},
            "review_only": False,
        },
    },
    "account": {
        "summary": "계정 이용 방법 문의",
        "value": {
            "tenant_id": "syncnote",
            "inquiry_id": "Q-1001",
            "inquiry_revision": 1,
            "based_on_message_id": "MSG-000001",
            "customer_category": "기타",
            "messages": [
                {
                    "message_id": "MSG-000001",
                    "sender": "customer",
                    "text": "비밀번호 변경 방법을 알려주세요.",
                    "sent_at": "2026-11-05T09:00:00+09:00",
                    "attachments": [],
                }
            ],
            "env": {"os": None, "app_version": None, "error_code": None},
            "review_only": False,
        },
    },
}


class PreviewResult(ContractModel):
    preview_only: bool = True
    stage: str
    analysis: InquiryAnalysis
    retrieval: SearchResult | None


@router.post(
    "/pipeline-preview",
    response_model=PreviewResult,
    summary="임시 테스트: 문의 분석 → 문서 검색 (발송·초안 생성 없음)",
)
async def preview(
    body: Annotated[PipelineRequest, Body(openapi_examples=PREVIEW_EXAMPLES)],
    tenant_id: ServiceTenant,
    request: Request,
) -> PreviewResult:
    check_body_tenant(body.tenant_id, tenant_id)
    settings = request.app.state.settings
    analyzer = request.app.state.inquiry_analyzer or InquiryAnalyzer(
        GeminiLLM(settings),
        max_input_chars=settings.analysis_max_input_chars,
    )
    analysis = await analyzer.analyze(body)
    if analysis.needs_more_info:
        return PreviewResult(stage="추가 질문 필요", analysis=analysis, retrieval=None)
    retrieval = await get_rag(request).search(
        SearchRequest(
            tenant_id=tenant_id,
            query=analysis.search_query,
            purpose="고객 답변",
            min_score=settings.rag_min_score,
        )
    )
    return PreviewResult(stage="RAG 검색 완료", analysis=analysis, retrieval=retrieval)
