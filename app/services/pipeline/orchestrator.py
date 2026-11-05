"""문의 분석부터 별도 검수까지 단계를 순서대로 실행하고 공통 결과로 반환한다."""

import logging
from collections.abc import Callable

from pydantic import ValidationError

from app.core.errors import ServiceError
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import CheckStatus, DraftResult, ErrorDetail, RunStatus
from app.schemas.pipeline import Analysis, PipelineRequest, PipelineResult, Verification
from app.schemas.rag import SearchResult
from app.schemas.routing import TeamRouting
from app.services.pipeline.analyzer import InquiryAnalyzer
from app.services.pipeline.drafter import DraftGenerator, EvidenceValidator
from app.services.pipeline.grounding import facts_are_current, invalidate_verification
from app.services.pipeline.reviewer import DraftReviewer
from app.services.pipeline.search import evidence_search_request
from app.services.pipeline.similar import SimilarInquiryFinder
from app.services.pipeline.stages import PipelineStage
from app.services.rag.service import RagService

logger = logging.getLogger(__name__)

RETRYABLE_ERROR_CODES = {
    "LLM_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_RATE_LIMITED",
    "EMBEDDING_API_ERROR",
    "EMBEDDING_TIMEOUT",
    "EMBEDDING_CONNECTION_FAILED",
}

UNUSABLE_OUTPUT_CODES = {
    "INVALID_LLM_RESPONSE",
    "EMPTY_LLM_RESPONSE",
    "INVALID_EVIDENCE_SELECTION",
    "INVALID_DRAFT_CITATIONS",
    "ANALYSIS_INPUT_TOO_LARGE",
}


def manual_result(result: PipelineResult, stage: PipelineStage | None) -> PipelineResult:
    """A rejected output is a review outcome, never an approved draft."""
    reason = "AI 결과의 구조나 근거를 검증하지 못했습니다. 담당자가 문의와 자료를 확인해 주세요."
    if stage == PipelineStage.ANALYZE:
        reason = "대화가 분석 가능한 길이를 초과해 담당자 확인이 필요합니다."
    return result.model_copy(
        update={
            "analysis": result.analysis
            or Analysis(
                category="기타",
                urgency="보통",
                summary="대화가 분석 가능한 범위를 초과해 자동 분류를 보류합니다.",
            ),
            "run_status": RunStatus.COMPLETED,
            "current_stage": None,
            "draft_result": DraftResult.MANUAL,
            "draft": None,
            "additional_question": None,
            "error": None,
            "routing": TeamRouting(reason=reason),
            "verification": Verification(
                status=CheckStatus.UNKNOWN,
                checks={
                    name: CheckStatus.UNKNOWN
                    for name in (
                        "evidence_current",
                        "source_conflict",
                        "claims_supported",
                        "internal_data_excluded",
                        "auto_response_scope",
                    )
                },
                reasons=[reason],
                unsupported_sentences=[],
            ),
        }
    )


class PipelineOrchestrator:
    def __init__(
        self,
        *,
        analyzer: InquiryAnalyzer,
        rag: RagService | None = None,
        similar_finder: SimilarInquiryFinder | None = None,
        evidence_validator: EvidenceValidator | None = None,
        draft_generator: DraftGenerator | None = None,
        reviewer: DraftReviewer | None = None,
        min_score: float = 0.5,
    ):
        self.analyzer = analyzer
        self.rag = rag
        self.similar_finder = similar_finder or SimilarInquiryFinder(rag, min_score=min_score)
        self.evidence_validator = evidence_validator or EvidenceValidator()
        self.draft_generator = draft_generator
        self.reviewer = reviewer
        self.min_score = min_score

    async def execute(
        self,
        request: PipelineRequest,
        *,
        run_id: str = "RUN-000001",
        on_stage: Callable[[PipelineStage], None] | None = None,
        precomputed_analysis: InquiryAnalysis | None = None,
        precomputed_retrieval: SearchResult | None = None,
    ) -> PipelineResult:
        current_stage: PipelineStage | None = None

        def report_stage(stage: PipelineStage) -> None:
            nonlocal current_stage
            current_stage = stage
            if on_stage is not None:
                on_stage(stage)

        # 모든 분기는 동일한 문의/실행 정보를 유지하고 결과 필드만 채운다.
        result = PipelineResult(
            run_id=run_id,
            tenant_id=request.tenant_id,
            inquiry_id=request.inquiry_id,
            inquiry_revision=request.inquiry_revision,
            based_on_message_id=request.based_on_message_id,
            run_status=RunStatus.COMPLETED,
        )

        try:
            # Stage 1: 문의 분석
            report_stage(PipelineStage.ANALYZE)
            inquiry_analysis = precomputed_analysis or await self.analyzer.analyze(request)
            projected_analysis = Analysis(
                category=inquiry_analysis.category,
                urgency=inquiry_analysis.urgency,
                summary=inquiry_analysis.summary,
                normalized_error_code=inquiry_analysis.normalized_error_code,
                missing_fields=inquiry_analysis.missing_fields,
            )
            result = result.model_copy(update={"analysis": projected_analysis})

            # Stage 2: 정보 부족 판단
            report_stage(PipelineStage.CHECK_INFORMATION)
            if inquiry_analysis.requires_manual_review:
                return manual_result(result, PipelineStage.CHECK_INFORMATION)
            if inquiry_analysis.needs_more_info:
                return result.model_copy(
                    update=dict(
                        analysis=projected_analysis,
                        draft_result=DraftResult.NEED_INFO,
                        additional_question=inquiry_analysis.additional_question,
                        routing=TeamRouting(
                            reason="문의 설명이 부족해 추가 질문 후 담당 팀을 판단합니다."
                        ),
                        verification=Verification(
                            status=CheckStatus.SKIPPED,
                            checks={},
                            reasons=["정보 수집 우선"],
                            unsupported_sentences=[],
                        ),
                    )
                )

            # Stage 3: 유사 문의 탐색
            report_stage(PipelineStage.FIND_SIMILAR)
            similar_inquiries = await self.similar_finder.find(request, inquiry_analysis)

            # Stage 4: RAG 검색
            report_stage(PipelineStage.RETRIEVE)
            if precomputed_retrieval is not None:
                retrieval = precomputed_retrieval
            else:
                if self.rag is None:
                    raise ServiceError(
                        503, "RAG_NOT_CONFIGURED", "Enable Chroma RAG in service settings"
                    )
                retrieval = await self.rag.search(
                    evidence_search_request(request, inquiry_analysis, min_score=self.min_score)
                )
            result = result.model_copy(
                update={
                    "knowledge_revision": retrieval.knowledge_revision,
                    "similar_inquiries": similar_inquiries,
                }
            )

            # Stage 5: 근거 검증
            report_stage(PipelineStage.VALIDATE_EVIDENCE)
            validated = await self.evidence_validator.validate(request, inquiry_analysis, retrieval)
            report_stage(PipelineStage.ROUTE_TEAM)
            routing = validated.routing or inquiry_analysis.routing or TeamRouting()
            if inquiry_analysis.routing is not None and inquiry_analysis.routing.team == "테크":
                routing = inquiry_analysis.routing
            routing = routing.model_copy(deep=True)
            if validated.status != "usable":
                routing.requires_human = True
            result = result.model_copy(update={"routing": routing})
            validated = validated.model_copy(update={"routing": routing})
            if validated.status == "usable" and not await facts_are_current(
                self.rag, request.tenant_id, validated.facts
            ):
                validated = validated.model_copy(
                    update={
                        "status": "insufficient",
                        "evidence_current": CheckStatus.FAIL,
                        "reasons": ["근거 검증 중 자료가 변경되었습니다."],
                    }
                )
            if validated.status != "usable":
                status = (
                    CheckStatus.FAIL
                    if validated.status == "conflict"
                    or validated.evidence_current == CheckStatus.FAIL
                    else CheckStatus.UNKNOWN
                )
                return result.model_copy(
                    update=dict(
                        knowledge_revision=retrieval.knowledge_revision,
                        analysis=projected_analysis,
                        similar_inquiries=similar_inquiries,
                        draft_result=DraftResult.MANUAL,
                        verification=Verification(
                            status=status,
                            checks={
                                "evidence_current": validated.evidence_current,
                                "source_conflict": validated.source_conflict,
                                "claims_supported": CheckStatus.SKIPPED,
                                "internal_data_excluded": CheckStatus.PASS,
                                "auto_response_scope": CheckStatus.FAIL
                                if status == CheckStatus.FAIL
                                else CheckStatus.UNKNOWN,
                            },
                            reasons=validated.reasons,
                            unsupported_sentences=[],
                        ),
                    )
                )

            # Stage 7: 초안 생성
            report_stage(PipelineStage.GENERATE_DRAFT)
            if self.draft_generator is None or self.reviewer is None:
                raise ServiceError(
                    503, "PIPELINE_NOT_CONFIGURED", "Draft generator and reviewer are required"
                )
            draft = await self.draft_generator.generate(request, inquiry_analysis, validated)

            # Stage 8: 별도 검수 (separate execution from generation)
            report_stage(PipelineStage.REVIEW)
            verification = await self.reviewer.review(request, inquiry_analysis, validated, draft)
            if not await facts_are_current(self.rag, request.tenant_id, validated.facts):
                verification = invalidate_verification(verification)
            elif (
                verification.checks.get("claims_supported") == CheckStatus.FAIL
                and verification.unsupported_sentences
                and verification.checks.get("internal_data_excluded") == CheckStatus.PASS
                and verification.checks.get("source_conflict") == CheckStatus.PASS
            ):
                draft = await self.draft_generator.generate(
                    request,
                    inquiry_analysis,
                    validated,
                    rejected_sentences=verification.unsupported_sentences,
                )
                verification = await self.reviewer.review(
                    request, inquiry_analysis, validated, draft
                )
                if not await facts_are_current(self.rag, request.tenant_id, validated.facts):
                    verification = invalidate_verification(verification)

            if routing.requires_human:
                checks = dict(verification.checks)
                if checks.get("auto_response_scope") == CheckStatus.PASS:
                    checks["auto_response_scope"] = CheckStatus.UNKNOWN
                verification = verification.model_copy(
                    update={
                        "checks": checks,
                        "status": CheckStatus.UNKNOWN
                        if verification.status == CheckStatus.PASS
                        else verification.status,
                        "reasons": [
                            *verification.reasons,
                            f"담당 팀: {routing.team}. {routing.reason}",
                        ],
                    }
                )

            return result.model_copy(
                update=dict(
                    knowledge_revision=retrieval.knowledge_revision,
                    analysis=projected_analysis,
                    similar_inquiries=similar_inquiries,
                    draft_result=DraftResult.READY,
                    draft=draft,
                    verification=verification,
                )
            )
        except ServiceError as exc:
            logger.warning(
                "Pipeline failed run=%s stage=%s code=%s", run_id, current_stage, exc.code
            )
            if exc.code in UNUSABLE_OUTPUT_CODES:
                return manual_result(result, current_stage)
            retryable = exc.code in RETRYABLE_ERROR_CODES
            return result.model_copy(
                update=dict(
                    run_status=RunStatus.FAILED,
                    current_stage=current_stage.value if current_stage else None,
                    analysis=None,
                    routing=None,
                    error=ErrorDetail(
                        code=exc.code,
                        message=exc.message,
                        retryable=retryable,
                    ),
                )
            )
        except ValidationError:
            logger.warning("Pipeline output invalid run=%s stage=%s", run_id, current_stage)
            return manual_result(result, current_stage)
        except Exception as exc:
            # Never log exception messages/tracebacks containing customer or source text.
            logger.error(
                "Pipeline failed run=%s stage=%s type=%s", run_id, current_stage, type(exc).__name__
            )
            return result.model_copy(
                update=dict(
                    run_status=RunStatus.FAILED,
                    current_stage=current_stage.value if current_stage else None,
                    analysis=None,
                    routing=None,
                    error=ErrorDetail(
                        code="PIPELINE_EXECUTION_ERROR",
                        message="파이프라인 실행 중 오류가 발생했습니다.",
                        retryable=False,
                    ),
                )
            )
