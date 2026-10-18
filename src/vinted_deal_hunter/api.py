"""Local import API. Request bodies are streamed with a bounded byte budget."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from app.admin.routes import admin_router
from app.health import health_router
from app.read_api import read_router
from app.scheduler.api import jobs_router
from app.scheduler.cycle import AnalysisContextProvider
from app.scheduler.runtime import JobRuntime
from app.scheduler.service import JobManager
from app.security import bearer_auth
from app.storage.migrations import ensure_database
from vinted_deal_hunter.config import Settings
from vinted_deal_hunter.db import SQLiteOfferRepository
from vinted_deal_hunter.ingestion import ImportReport, import_offers
from vinted_deal_hunter.sources import (
    CSVOfferSource,
    JSONOfferSource,
    OfferSource,
    SavedHTMLOfferSource,
    SourceError,
)

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    source: OfferSource | None = None,
    context: AnalysisContextProvider | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(ensure_database, config.ai.db_path, "ai")
        repository = SQLiteOfferRepository(config.db_path)
        app.state.repository = repository
        runtime = JobRuntime(config, repository, source=source, context=context)
        app.state.runtime = runtime
        app.state.jobs = runtime.manager
        runtime.manager.start()
        try:
            yield
        finally:
            try:
                await runtime.close()
            finally:
                repository.close()

    app = FastAPI(title="Vinted Deal Hunter", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router(config))
    app.include_router(read_router(config))
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(Path(__file__).parents[1] / "app/admin/static")),
        name="admin-static",
    )
    app.include_router(admin_router(config))

    @app.get("/", include_in_schema=False)
    async def home() -> Response:
        return RedirectResponse("/admin", status_code=303)

    @app.middleware("http")
    async def admin_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie, HX-Request"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'"
            )
        return response

    def current_manager(request: Request) -> JobManager:
        return cast(JobManager, request.app.state.jobs)

    app.include_router(jobs_router(current_manager, config.jobs_api_token))

    @app.post(
        "/imports",
        response_model=ImportReport,
        dependencies=[Depends(bearer_auth(config.jobs_api_token))],
        tags=["imports"],
    )
    async def post_import(
        request: Request,
        source_name: Annotated[str, Query(min_length=1, max_length=100)] = "demo",
        source_format: Annotated[Literal["csv", "html", "json"], Query(alias="format")] = "json",
        is_demo: bool = True,
    ) -> ImportReport:
        media_types = {
            "csv": "text/csv",
            "html": "text/html",
            "json": "application/json",
        }
        expected = media_types[source_format]
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != expected:
            raise HTTPException(415, f"Content-Type must be {expected}")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > config.max_import_bytes:
                raise HTTPException(413, "Import exceeds the byte limit")
            content.extend(chunk)
        imported_source: OfferSource
        if source_format == "csv":
            imported_source = CSVOfferSource(
                bytes(content), source_name=source_name, is_demo=is_demo
            )
        elif source_format == "html":
            imported_source = SavedHTMLOfferSource(
                bytes(content), source_name=source_name, is_demo=is_demo
            )
        else:
            imported_source = JSONOfferSource(
                bytes(content), source_name=source_name, is_demo=is_demo
            )
        repository = cast(SQLiteOfferRepository, request.app.state.repository)
        try:
            return await run_in_threadpool(import_offers, imported_source, repository, config)
        except SourceError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SQLAlchemyError as exc:
            logger.error("Import storage operation failed")
            raise HTTPException(503, "Import storage unavailable; transaction rolled back") from exc

    return app
