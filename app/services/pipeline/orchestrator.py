"""문의 분석부터 별도 검수까지 단계를 순서대로 실행하고 공통 결과로 반환한다."""

from collections.abc import Callable

from app.core.errors import ServiceError
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import CheckStatus, DraftResult, ErrorDetail, RunStatus
from app.schemas.pipeline import Analysis, PipelineRequest, PipelineResult, Verification
from app.schemas.rag import SearchRequest, SearchResult
from app.services.pipeline.analyzer import InquiryAnalyzer
from app.services.pipeline.drafter import DraftGenerator, EvidenceValidator
from app.services.pipeline.grounding import facts_are_current, invalidate_verification
from app.services.pipeline.reviewer import DraftReviewer
from app.services.pipeline.similar import SimilarInquiryFinder
from app.services.pipeline.stages import PipelineStage
from app.services.rag.service import RagService

RETRYABLE_ERROR_CODES = {
    "GEMINI_UNAVAILABLE",
    "GEMINI_TIMEOUT",
    "GEMINI_RATE_LIMITED",
    "EMBEDDING_API_ERROR",
    "EMBEDDING_TIMEOUT",
    "EMBEDDING_CONNECTION_FAILED",
}


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
        def report_stage(stage: PipelineStage) -> None:
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

            # Stage 2: 정보 부족 판단
            report_stage(PipelineStage.CHECK_INFORMATION)
            if inquiry_analysis.needs_more_info:
                return result.model_copy(
                    update=dict(
                        analysis=projected_analysis,
                        draft_result=DraftResult.NEED_INFO,
                        additional_question=inquiry_analysis.additional_question,
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
                    SearchRequest(
                        tenant_id=request.tenant_id,
                        query=inquiry_analysis.search_query or inquiry_analysis.summary,
                        purpose="고객 답변",
                        min_score=self.min_score,
                    )
                )

            # Stage 5: 근거 검증
            report_stage(PipelineStage.VALIDATE_EVIDENCE)
            validated = await self.evidence_validator.validate(request, inquiry_analysis, retrieval)
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

            # Stage 6: 초안 생성
            report_stage(PipelineStage.GENERATE_DRAFT)
            if self.draft_generator is None or self.reviewer is None:
                raise ServiceError(
                    503, "PIPELINE_NOT_CONFIGURED", "Draft generator and reviewer are required"
                )
            draft = await self.draft_generator.generate(request, inquiry_analysis, validated)

            # Stage 7: 별도 검수 (separate execution from generation)
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
            retryable = exc.code in RETRYABLE_ERROR_CODES
            return result.model_copy(
                update=dict(
                    run_status=RunStatus.FAILED,
                    error=ErrorDetail(
                        code=exc.code,
                        message=exc.message,
                        retryable=retryable,
                    ),
                )
            )
        except Exception:
            return result.model_copy(
                update=dict(
                    run_status=RunStatus.FAILED,
                    error=ErrorDetail(
                        code="PIPELINE_EXECUTION_ERROR",
                        message="파이프라인 실행 중 오류가 발생했습니다.",
                        retryable=False,
                    ),
                )
            )
