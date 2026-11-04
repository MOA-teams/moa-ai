from fastapi import APIRouter

from app.api.internal.v1 import pipeline_runs, rag
from app.schemas.common import ErrorResponse

router = APIRouter(
    responses={code: {"model": ErrorResponse} for code in (401, 403, 409, 422, 501, 503)}
)
router.include_router(pipeline_runs.router)
router.include_router(rag.router)
