from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile

from metro_agent.auth.dependencies import CurrentUser, require_admin, require_same_origin
from metro_agent.knowledge_admin.schemas import (
    DraftDetail,
    DraftListItem,
    DraftSubmissionResult,
    AuditListItem,
    EmptyAdminMutation,
    JobStatus,
    ReleaseListItem,
    RollbackRequest,
)
from metro_agent.knowledge_admin.service import (
    KnowledgeAdminError,
    KnowledgeAdminInvalidStateError,
    KnowledgeAdminMalformedRequestError,
    KnowledgeAdminNotFoundError,
    KnowledgeAdminWorkerUnavailableError,
)


def create_knowledge_admin_router() -> APIRouter:
    router = APIRouter(prefix="/api/admin/knowledge")

    @router.post(
        "/drafts",
        response_model=DraftSubmissionResult,
        status_code=201,
        dependencies=[Depends(require_same_origin)],
    )
    async def upload_draft(
        request: Request,
        current_user: CurrentUser = Depends(require_admin),
        package: UploadFile = File(...),
        _form_guard: None = Depends(_reject_extra_upload_fields),
    ) -> dict[str, Any]:
        service = _service(request)
        try:
            return await service.upload_draft(package=package, current_user=current_user)
        except KnowledgeAdminError as exc:
            _raise_mapped_error(exc)

    @router.get("/drafts", response_model=list[DraftListItem])
    def list_drafts(
        request: Request,
        _admin: CurrentUser = Depends(require_admin),
    ) -> list[Any]:
        return _service(request).list_drafts()

    @router.get("/drafts/{draft_id}", response_model=DraftDetail)
    def get_draft(
        request: Request,
        draft_id: UUID,
        _admin: CurrentUser = Depends(require_admin),
    ) -> Any:
        try:
            return _service(request).get_draft(str(draft_id))
        except KnowledgeAdminError as exc:
            _raise_mapped_error(exc)

    @router.post(
        "/drafts/{draft_id}/validate",
        response_model=JobStatus,
        dependencies=[Depends(require_same_origin)],
    )
    def validate_draft(
        request: Request,
        draft_id: UUID,
        payload: EmptyAdminMutation | None = Body(default=None),
        current_user: CurrentUser = Depends(require_admin),
    ) -> Any:
        _ = payload
        try:
            return _service(request).queue_validation(str(draft_id), current_user=current_user)
        except KnowledgeAdminError as exc:
            _raise_mapped_error(exc)

    @router.post(
        "/drafts/{draft_id}/publish",
        response_model=JobStatus,
        dependencies=[Depends(require_same_origin)],
    )
    def publish_draft(
        request: Request,
        draft_id: UUID,
        payload: EmptyAdminMutation | None = Body(default=None),
        current_user: CurrentUser = Depends(require_admin),
    ) -> Any:
        _ = payload
        try:
            return _service(request).queue_publish(str(draft_id), current_user=current_user)
        except KnowledgeAdminError as exc:
            _raise_mapped_error(exc)

    @router.get("/releases", response_model=list[ReleaseListItem])
    def list_releases(
        request: Request,
        _admin: CurrentUser = Depends(require_admin),
    ) -> list[Any]:
        return _service(request).list_releases()

    @router.get("/audits", response_model=list[AuditListItem])
    def list_audits(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        _admin: CurrentUser = Depends(require_admin),
    ) -> list[Any]:
        return _service(request).list_audit_events(limit=limit)

    @router.post(
        "/releases/{release_build_id}/rollback",
        response_model=JobStatus,
        dependencies=[Depends(require_same_origin)],
    )
    def rollback_release(
        request: Request,
        release_build_id: str,
        payload: RollbackRequest,
        current_user: CurrentUser = Depends(require_admin),
    ) -> Any:
        try:
            return _service(request).queue_rollback(
                release_build_id,
                reason=payload.reason,
                current_user=current_user,
            )
        except KnowledgeAdminError as exc:
            _raise_mapped_error(exc)

    @router.get("/jobs/{job_id}", response_model=JobStatus)
    def get_job(
        request: Request,
        job_id: UUID,
        _admin: CurrentUser = Depends(require_admin),
    ) -> Any:
        try:
            return _service(request).get_job(str(job_id))
        except KnowledgeAdminError as exc:
            _raise_mapped_error(exc)

    return router


def _service(request: Request) -> Any:
    service = request.app.state.knowledge_admin_service
    if service is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Knowledge admin service unavailable")
    return service


async def _reject_extra_upload_fields(request: Request) -> None:
    form = await request.form()
    unexpected = sorted(key for key in form.keys() if key != "package")
    if unexpected:
        raise HTTPException(
            status_code=422,
            detail=f"Unexpected upload fields: {', '.join(unexpected)}",
        )


def _raise_mapped_error(exc: KnowledgeAdminError) -> None:
    if isinstance(exc, KnowledgeAdminNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, KnowledgeAdminInvalidStateError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, KnowledgeAdminMalformedRequestError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, KnowledgeAdminWorkerUnavailableError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail="Knowledge admin request failed") from exc
