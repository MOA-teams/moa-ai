from fastapi import APIRouter, BackgroundTasks, Query, Request

from app.deps.auth import ServiceTenant, check_body_tenant
from app.deps.rag import get_rag
from app.schemas.rag import (
    SourceAccepted,
    SourceInput,
    SourceStatusBatch,
    SourceStatusResult,
    SourceType,
    SourceView,
)

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post(
    "/sources",
    response_model=SourceAccepted,
    status_code=202,
    summary="자료 등록 후 검색용 임베딩 생성",
)
async def ingest_source(
    body: SourceInput,
    tenant_id: ServiceTenant,
    request: Request,
    background_tasks: BackgroundTasks,
) -> SourceAccepted:
    check_body_tenant(body.tenant_id, tenant_id)
    service = get_rag(request)
    ticket = await service.prepare(body)
    view = await service.get(tenant_id, body.source_type, body.source_id, body.version)
    if ticket is not None:
        background_tasks.add_task(service.index_background, ticket)
    return SourceAccepted(
        source_id=view.source_id, version=view.version, indexing_status=view.indexing_status
    )


@router.post(
    "/source-statuses", response_model=SourceStatusResult, summary="자료 폐기·재검토 상태 반영"
)
async def update_statuses(
    body: SourceStatusBatch,
    tenant_id: ServiceTenant,
    request: Request,
) -> SourceStatusResult:
    check_body_tenant(body.tenant_id, tenant_id)
    return await get_rag(request).update_statuses(body)


@router.get(
    "/sources/{source_id:path}/versions/{version}",
    response_model=SourceView,
    summary="임베딩 완료·실패·검색 가능 여부 조회",
)
async def get_source(
    source_id: str,
    version: str,
    tenant_id: ServiceTenant,
    request: Request,
    source_type: SourceType = Query(...),
) -> SourceView:
    return await get_rag(request).get(tenant_id, source_type, source_id, version)
