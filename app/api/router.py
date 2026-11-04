from fastapi import APIRouter

from app.api import health
from app.api.internal.v1.router import router as internal_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(internal_router, prefix="/internal/v1")
