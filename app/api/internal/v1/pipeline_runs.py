from typing import Annotated

from fastapi import APIRouter, Header

from app.core.errors import FeatureNotReady
from app.deps.auth import ServiceTenant, check_body_tenant
from app.schemas.pipeline import PipelineAccepted, PipelineRequest, PipelineResult

router = APIRouter(prefix="/pipeline-runs", tags=["pipeline"])


@router.post("", response_model=PipelineAccepted, status_code=202)
async def create_run(
    body: PipelineRequest,
    tenant_id: ServiceTenant,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
) -> PipelineAccepted:
    check_body_tenant(body.tenant_id, tenant_id)
    raise FeatureNotReady("Pipeline submission and durable worker")


@router.get("/{run_id}", response_model=PipelineResult)
async def get_run(run_id: str, tenant_id: ServiceTenant) -> PipelineResult:
    raise FeatureNotReady("Pipeline result repository")
