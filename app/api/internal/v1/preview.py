from typing import Annotated

from fastapi import APIRouter, Body, Query, Request

from app.deps.auth import ServiceTenant, check_body_tenant
from app.deps.rag import get_rag
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import ContractModel, RunStatus, TenantID
from app.schemas.pipeline import PipelineRequest, PipelineResult
from app.schemas.rag import SearchResult
from app.services.llm.bedrock import create_llm
from app.services.pipeline.analyzer import InquiryAnalyzer
from app.services.pipeline.revision import (
    AnswerCandidate,
    AnswerRevisionResult,
    AnswerRevisionService,
)
from app.services.pipeline.search import evidence_search_request
from app.workers.pipeline import build_orchestrator

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
    pipeline_result: PipelineResult | None = None


class AnswerRevisionPreviewRequest(ContractModel):
    tenant_id: TenantID
    candidate: AnswerCandidate


@router.post(
    "/pipeline-preview",
    response_model=PreviewResult,
    summary="임시 테스트: 문의 분석 → 문서 검색 (선택 시 전체 초안·검수 미리보기)",
)
async def preview(
    body: Annotated[PipelineRequest, Body(openapi_examples=PREVIEW_EXAMPLES)],
    tenant_id: ServiceTenant,
    request: Request,
    full_pipeline: bool = Query(default=False),
) -> PreviewResult:
    check_body_tenant(body.tenant_id, tenant_id)
    settings = request.app.state.settings
    analyzer = request.app.state.inquiry_analyzer or InquiryAnalyzer(
        create_llm(settings),
        max_input_chars=settings.analysis_max_input_chars,
    )
    analysis = await analyzer.analyze(body)
    if analysis.needs_more_info:
        return PreviewResult(stage="추가 질문 필요", analysis=analysis, retrieval=None)
    rag = get_rag(request)
    retrieval = await rag.search(
        evidence_search_request(body, analysis, min_score=settings.rag_min_score)
    )
    if not full_pipeline:
        return PreviewResult(stage="RAG 검색 완료", analysis=analysis, retrieval=retrieval)
    orchestrator = getattr(request.app.state, "pipeline_orchestrator", None) or build_orchestrator(
        settings, rag=rag, analyzer=analyzer
    )
    full_res = await orchestrator.execute(
        body,
        run_id="RUN-PREVIEW",
        precomputed_analysis=analysis,
        precomputed_retrieval=retrieval,
    )
    stage = (
        "파이프라인 실행 실패"
        if full_res.run_status == RunStatus.FAILED
        else "전체 파이프라인 완료"
    )
    return PreviewResult(
        stage=stage,
        analysis=analysis,
        retrieval=retrieval,
        pipeline_result=full_res,
    )


@router.post(
    "/answer-revision-preview",
    response_model=AnswerRevisionResult,
    summary="임시 테스트: 문서 변경에 따른 기존 답변 수정안 생성 및 별도 검수",
)
async def preview_answer_revision(
    body: AnswerRevisionPreviewRequest,
    tenant_id: ServiceTenant,
    request: Request,
) -> AnswerRevisionResult:
    check_body_tenant(body.tenant_id, tenant_id)
    settings = request.app.state.settings
    rag = get_rag(request)
    service = getattr(request.app.state, "answer_revision_service", None) or AnswerRevisionService(
        llm=create_llm(settings),
        rag=rag,
    )
    return await service.revise_answer(tenant_id, body.candidate)
