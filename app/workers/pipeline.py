from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.core.errors import ServiceError
from app.schemas.common import RunStatus
from app.schemas.pipeline import PipelineAccepted, PipelineRequest, PipelineResult
from app.services.backend_client import BackendClient
from app.services.llm.bedrock import create_llm
from app.services.pipeline.analyzer import InquiryAnalyzer
from app.services.pipeline.drafter import DraftGenerator, EvidenceValidator
from app.services.pipeline.orchestrator import PipelineOrchestrator
from app.services.pipeline.reviewer import DraftReviewer
from app.services.pipeline.similar import SimilarInquiryFinder
from app.services.pipeline.stages import PipelineStage
from app.services.rag.chroma_store import digest
from app.services.rag.service import RagService


@dataclass
class RunSubmission:
    accepted: PipelineAccepted
    request: PipelineRequest
    is_new: bool


@dataclass
class _RunEntry:
    run_id: str
    tenant_id: str
    idempotency_key: str
    fingerprint: str
    request: PipelineRequest
    result: PipelineResult
    callback_status: str = "미설정"  # 대기 / 완료 / 실패 / 미설정


class PipelineRunStore:
    """In-memory run state and idempotency store.

    Note: Because persistent PostgreSQL is owned by the backend service and no external
    run-storage API is provided yet, run state in this store is kept in process memory
    and does not survive process restarts.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[str, _RunEntry] = {}
        self._by_idempotency: dict[tuple[str, str], str] = {}
        self._latest_revision: dict[tuple[str, str], int] = {}
        self._latest_run: dict[tuple[str, str], str] = {}

    def submit(
        self,
        tenant_id: str,
        idempotency_key: str,
        request: PipelineRequest,
    ) -> RunSubmission:
        fingerprint = digest(request.model_dump(mode="json"))
        idem_key = (tenant_id, idempotency_key)
        inquiry_key = (tenant_id, request.inquiry_id)

        with self._lock:
            existing_run_id = self._by_idempotency.get(idem_key)
            if existing_run_id is not None:
                entry = self._runs[existing_run_id]
                if entry.fingerprint != fingerprint:
                    raise ServiceError(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "Idempotency-Key was already used with a different request payload",
                    )
                return RunSubmission(
                    accepted=PipelineAccepted(
                        run_id=entry.run_id,
                        inquiry_id=entry.request.inquiry_id,
                        inquiry_revision=entry.request.inquiry_revision,
                        run_status=entry.result.run_status,
                    ),
                    request=entry.request,
                    is_new=False,
                )

            latest_rev = self._latest_revision.get(inquiry_key, 0)
            if request.inquiry_revision < latest_rev:
                raise ServiceError(
                    409,
                    "STALE_INQUIRY_REVISION",
                    "A newer inquiry_revision has already been submitted",
                )
            self._latest_revision[inquiry_key] = max(latest_rev, request.inquiry_revision)

            run_id = f"RUN-{uuid4().hex}"
            initial = PipelineResult(
                run_id=run_id,
                tenant_id=tenant_id,
                inquiry_id=request.inquiry_id,
                inquiry_revision=request.inquiry_revision,
                based_on_message_id=request.based_on_message_id,
                run_status=RunStatus.QUEUED,
                current_stage=None,
            )
            entry = _RunEntry(
                run_id=run_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                request=request,
                result=initial,
            )
            self._runs[run_id] = entry
            self._latest_run[inquiry_key] = run_id
            self._by_idempotency[idem_key] = run_id
            return RunSubmission(
                accepted=PipelineAccepted(
                    run_id=run_id,
                    inquiry_id=request.inquiry_id,
                    inquiry_revision=request.inquiry_revision,
                    run_status=RunStatus.QUEUED,
                ),
                request=request,
                is_new=True,
            )

    def mark_stage(self, run_id: str, stage: PipelineStage) -> None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None and entry.result.run_status in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
            }:
                entry.result = entry.result.model_copy(
                    update={
                        "run_status": RunStatus.RUNNING,
                        "current_stage": stage.value,
                    }
                )

    def complete(self, run_id: str, result: PipelineResult) -> None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None:
                entry.result = result

    def mark_callback(self, run_id: str, status: str) -> None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None:
                entry.callback_status = status

    def is_latest_run(self, run_id: str) -> bool:
        with self._lock:
            entry = self._runs[run_id]
            return self._latest_run.get((entry.tenant_id, entry.request.inquiry_id)) == run_id

    def get(self, tenant_id: str, run_id: str) -> PipelineResult:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or entry.tenant_id != tenant_id:
                raise ServiceError(404, "RUN_NOT_FOUND", "Pipeline run not found")
            return entry.result

    def callback_status(self, run_id: str) -> str | None:
        with self._lock:
            entry = self._runs.get(run_id)
            return entry.callback_status if entry else None

    def discard(self, run_id: str) -> None:
        """Roll back a newly created run if worker initialization fails before scheduling."""
        with self._lock:
            entry = self._runs.pop(run_id, None)
            if entry is None:
                return
            idem_key = (entry.tenant_id, entry.idempotency_key)
            if self._by_idempotency.get(idem_key) == run_id:
                self._by_idempotency.pop(idem_key, None)
            inquiry_key = (entry.tenant_id, entry.request.inquiry_id)
            remaining = [
                r for r in self._runs.values() if (r.tenant_id, r.request.inquiry_id) == inquiry_key
            ]
            if remaining:
                latest_entry = max(remaining, key=lambda r: r.request.inquiry_revision)
                self._latest_revision[inquiry_key] = latest_entry.request.inquiry_revision
                self._latest_run[inquiry_key] = latest_entry.run_id
            else:
                self._latest_revision.pop(inquiry_key, None)
                self._latest_run.pop(inquiry_key, None)


def build_orchestrator(
    settings: Settings,
    *,
    rag: RagService | None = None,
    analyzer: InquiryAnalyzer | None = None,
) -> PipelineOrchestrator:
    llm = create_llm(settings)
    active_analyzer = analyzer or InquiryAnalyzer(
        llm, max_input_chars=settings.analysis_max_input_chars
    )
    return PipelineOrchestrator(
        analyzer=active_analyzer,
        rag=rag,
        similar_finder=SimilarInquiryFinder(rag, min_score=settings.rag_min_score),
        evidence_validator=EvidenceValidator(llm),
        draft_generator=DraftGenerator(llm),
        reviewer=DraftReviewer(llm),
        min_score=settings.rag_min_score,
    )


async def execute_pipeline(
    request: PipelineRequest,
    orchestrator: PipelineOrchestrator,
    *,
    run_id: str = "RUN-000001",
    store: PipelineRunStore | None = None,
    backend_client: BackendClient | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> PipelineResult:
    """Execute the pipeline, update run status, and optionally deliver callback to backend."""

    def on_stage(stage: PipelineStage) -> None:
        if store is not None:
            store.mark_stage(run_id, stage)

    result = await orchestrator.execute(request, run_id=run_id, on_stage=on_stage)
    if store is not None:
        store.complete(run_id, result)

    if backend_client is not None:
        await deliver_result(result, store, backend_client, http_client)
    return result


async def deliver_result(
    result: PipelineResult,
    store: PipelineRunStore | None,
    backend_client: BackendClient,
    http_client: httpx.AsyncClient | None,
) -> None:
    """AI 실행 결과와 콜백 전달 상태는 별개로 관리한다."""
    run_id = result.run_id
    # 같은 문의의 새 실행이 있으면 이전 실행의 콜백을 보내지 않는다.
    if store is not None and not store.is_latest_run(run_id):
        store.mark_callback(run_id, "보류(구버전)")
        return

    token = backend_client.settings.backend_service_token.get_secret_value()
    if not token:
        if store is not None:
            store.mark_callback(run_id, "미설정")
        return

    if store is not None:
        store.mark_callback(run_id, "대기")
    try:
        if http_client is not None:
            await backend_client.deliver(result, http_client)
        else:
            async with httpx.AsyncClient() as client:
                await backend_client.deliver(result, client)
        if store is not None:
            store.mark_callback(run_id, "완료")
    except ServiceError:
        # Callback delivery state is tracked separately from AI run_status (APISpec B01)
        if store is not None:
            store.mark_callback(run_id, "실패")
