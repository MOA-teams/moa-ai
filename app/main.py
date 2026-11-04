from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.db.session import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database = Database(config.database_url)
        yield
        await app.state.database.close()

    app = FastAPI(title="MOA AI", version="0.1.0", lifespan=lifespan)
    app.dependency_overrides[get_settings] = lambda: config
    register_error_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
