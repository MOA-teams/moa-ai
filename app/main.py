from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        service = app.state.rag_service
        if service is not None and hasattr(service.store, "close"):
            service.store.close()

    app = FastAPI(title="MOA AI", version="0.1.0", lifespan=lifespan)
    app.state.settings = config
    app.state.rag_service = None
    app.state.inquiry_analyzer = None
    app.dependency_overrides[get_settings] = lambda: config
    register_error_handlers(app)
    app.include_router(api_router)
    if config.enable_preview:
        from app.api.internal.v1.preview import router as preview_router

        app.include_router(preview_router, prefix="/internal/v1")
    return app


app = create_app()
