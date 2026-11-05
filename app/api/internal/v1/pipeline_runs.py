from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Request

from app.deps.auth import ServiceTenant, check_body_tenant
from app.deps.rag import get_rag
from app.schemas.pipeline import PipelineAccepted, PipelineRequest, PipelineResult
from app.services.backend_client import BackendClient
from app.services.pipeline.orchestrator import PipelineOrchestrator
from app.workers.pipeline import PipelineRunStore, build_orchestrator, execute_pipeline

router = APIRouter(prefix="/pipeline-runs", tags=["pipeline"])


def get_run_store(request: Request) -> PipelineRunStore:
    store = getattr(request.app.state, "pipeline_store", None)
    if store is None:
        store = PipelineRunStore()
        request.app.state.pipeline_store = store
    return store


def get_orchestrator(request: Request) -> PipelineOrchestrator:
    orchestrator = getattr(request.app.state, "pipeline_orchestrator", None)
    if orchestrator is not None:
        return orchestrator
    settings = request.app.state.settings
    rag = request.app.state.rag_service
    if rag is None and settings.rag_enabled:
        rag = get_rag(request)
    return build_orchestrator(
        settings,
        rag=rag,
        analyzer=getattr(request.app.state, "inquiry_analyzer", None),
    )


@router.post("", response_model=PipelineAccepted, status_code=202)
async def create_run(
    body: PipelineRequest,
    tenant_id: ServiceTenant,
    request: Request,
    background_tasks: BackgroundTasks,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> PipelineAccepted:
    check_body_tenant(body.tenant_id, tenant_id)
    store = get_run_store(request)
    submission = store.submit(tenant_id, idempotency_key, body)
    if submission.is_new:
        try:
            orchestrator = get_orchestrator(request)
            backend_client = getattr(request.app.state, "backend_client", None) or BackendClient(
                request.app.state.settings
            )
            http_client = getattr(request.app.state, "callback_http_client", None)
        except Exception:
            store.discard(submission.accepted.run_id)
            raise
        background_tasks.add_task(
            execute_pipeline,
            submission.request,
            orchestrator,
            run_id=submission.accepted.run_id,
            store=store,
            backend_client=backend_client,
            http_client=http_client,
        )
    return submission.accepted


@router.get("/{run_id}", response_model=PipelineResult)
async def get_run(
    run_id: str,
    tenant_id: ServiceTenant,
    request: Request,
) -> PipelineResult:
    return get_run_store(request).get(tenant_id, run_id)
