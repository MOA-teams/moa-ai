from fastapi import APIRouter, Query

from app.core.errors import FeatureNotReady
from app.deps.auth import ServiceTenant, check_body_tenant
from app.schemas.rag import (
    SourceAccepted,
    SourceInput,
    SourceStatusBatch,
    SourceStatusResult,
    SourceType,
    SourceView,
)

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/sources", response_model=SourceAccepted, status_code=202)
async def ingest_source(body: SourceInput, tenant_id: ServiceTenant) -> SourceAccepted:
    check_body_tenant(body.tenant_id, tenant_id)
    raise FeatureNotReady("Source ingestion and vector indexing")


@router.post("/source-statuses", response_model=SourceStatusResult)
async def update_statuses(body: SourceStatusBatch, tenant_id: ServiceTenant) -> SourceStatusResult:
    check_body_tenant(body.tenant_id, tenant_id)
    raise FeatureNotReady("Atomic source invalidation")


@router.get("/sources/{source_id}/versions/{version}", response_model=SourceView)
async def get_source(
    source_id: str, version: str, tenant_id: ServiceTenant, source_type: SourceType = Query(...)
) -> SourceView:
    raise FeatureNotReady("Source metadata repository")
