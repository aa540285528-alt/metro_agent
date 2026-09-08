from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from metro_agent.knowledge_admin.service import (
    KnowledgePublisherService,
    build_default_service,
)


def create_app(
    *,
    service: KnowledgePublisherService | None = None,
    internal_bearer_secret: str | None = None,
    job_consumer_poll_interval_seconds: float = 1.0,
) -> FastAPI:
    publisher = service
    expected_secret = internal_bearer_secret or os.getenv(
        "KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.publisher = publisher or build_default_service()
        app.state.internal_bearer_secret = expected_secret
        stop_event = asyncio.Event()
        consumer_task = asyncio.create_task(
            _consume_jobs_forever(
                app.state.publisher,
                stop_event,
                job_consumer_poll_interval_seconds=job_consumer_poll_interval_seconds,
            )
        )
        app.state.job_consumer_stop = stop_event
        app.state.job_consumer_task = consumer_task
        app.state.job_consumer_poll_interval_seconds = job_consumer_poll_interval_seconds
        yield
        stop_event.set()
        await consumer_task

    app = FastAPI(lifespan=lifespan)

    def require_internal_bearer(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> None:
        if not expected_secret:
            raise HTTPException(status_code=503, detail="Internal bearer secret is not configured")
        if authorization != f"Bearer {expected_secret}":
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post("/internal/drafts", status_code=201)
    async def upload_draft(
        request: Request,
        _auth: None = Depends(require_internal_bearer),
        package: UploadFile = File(...),
    ) -> JSONResponse:
        publisher_service = publisher or request.app.state.publisher
        package_path = _persist_upload(package, publisher_service.staging_root)
        try:
            result = publisher_service.ingest_draft(
                package_path, original_filename=package.filename or "knowledge.zip"
            )
        finally:
            package_path.unlink(missing_ok=True)
        return JSONResponse(result, status_code=201)

    @app.get("/internal/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def _consume_jobs_forever(
    publisher_service: KnowledgePublisherService,
    stop_event: asyncio.Event,
    *,
    job_consumer_poll_interval_seconds: float,
) -> None:
    while not stop_event.is_set():
        try:
            result = await asyncio.to_thread(publisher_service.process_next_job)
        except Exception:
            if await _wait_for_stop_or_timeout(
                stop_event, job_consumer_poll_interval_seconds
            ):
                return
            continue
        if result is None and await _wait_for_stop_or_timeout(
            stop_event, job_consumer_poll_interval_seconds
        ):
            return


async def _wait_for_stop_or_timeout(
    stop_event: asyncio.Event, timeout_seconds: float
) -> bool:
    if timeout_seconds <= 0:
        await asyncio.sleep(0)
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
        return True
    except asyncio.TimeoutError:
        return stop_event.is_set()


def _persist_upload(package: UploadFile, staging_root: Path) -> Path:
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", suffix=".zip", delete=False) as handle:
        shutil.copyfileobj(package.file, handle)
        return Path(handle.name)


app = create_app()
