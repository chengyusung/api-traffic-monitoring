from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin, analytics, auth, health, imports, records, services, ws
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.logging import RequestContextMiddleware, setup_logging
from app.db import SessionLocal, engine
from app.pipeline import Pipeline


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pipeline = pipeline = Pipeline.create(SessionLocal)
    try:
        await pipeline.start()
        yield
    finally:
        await pipeline.stop()
        await engine.dispose()


def create_app() -> FastAPI:
    setup_logging(settings.log_level)
    app = FastAPI(title="API Traffic Monitoring", version="0.1.0", lifespan=lifespan)
    # Added first, so it runs inside RequestContextMiddleware and has the request id.
    app.add_middleware(imports.UploadLimitMiddleware, path=f"/api/v1{imports.router.prefix}")
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    for router in (
        auth.router,
        services.router,
        records.router,
        imports.router,
        analytics.router,
        admin.router,
        ws.router,
    ):
        app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
