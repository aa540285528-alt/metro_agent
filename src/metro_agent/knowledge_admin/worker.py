from __future__ import annotations

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
) -> FastAPI:
    publisher = service
    expected_secret = internal_bearer_secret or os.getenv(
        "KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.publisher = publisher or build_default_service()
        app.state.internal_bearer_secret = expected_secret
        yield

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


def _persist_upload(package: UploadFile, staging_root: Path) -> Path:
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", suffix=".zip", delete=False) as handle:
        shutil.copyfileobj(package.file, handle)
        return Path(handle.name)


app = create_app()
