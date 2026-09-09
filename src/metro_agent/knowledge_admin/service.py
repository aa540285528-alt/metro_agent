from __future__ import annotations

import os
import shutil
from pathlib import Path
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi import UploadFile

from metro_agent.auth.dependencies import CurrentUser
from metro_agent.knowledge.chroma_client import get_chroma_client
from metro_agent.knowledge.config import (
    KnowledgeSettings,
    require_knowledge_upload_staging_root,
)
from metro_agent.knowledge.publication_lock import PublicationLock
from metro_agent.knowledge.releases import restore_validated_release
from metro_agent.knowledge.releases import read_validated_release
from metro_agent.knowledge.source_validation import validate_source_root
from metro_agent.knowledge_admin.repository import KnowledgeAdminRepository
from metro_agent.knowledge_admin.zip_staging import stage_zip_knowledge_package
from metro_agent.knowledge_admin.models import KnowledgeDraft
from metro_agent.knowledge_admin.models import KnowledgeJob
from metro_agent.tools.knowledge_indexer import KnowledgeIndexer


class KnowledgeAdminError(RuntimeError):
    """Base class for app-facing governed knowledge administration failures."""


class KnowledgeAdminNotFoundError(KnowledgeAdminError):
    """A requested draft, job, or release does not exist."""


class KnowledgeAdminInvalidStateError(KnowledgeAdminError):
    """A request targets a draft or job in the wrong state."""


class KnowledgeAdminMalformedRequestError(KnowledgeAdminError):
    """The worker or repository returned malformed data."""


class KnowledgeAdminWorkerUnavailableError(KnowledgeAdminError):
    """The internal publisher worker could not complete the upload."""


class KnowledgePublisherError(RuntimeError):
    """The controlled publisher cannot complete a job safely."""


class KnowledgeAdminService:
    def __init__(
        self,
        *,
        repository: KnowledgeAdminRepository,
        worker_base_url: str = "http://knowledge-publisher:8000",
        internal_bearer_secret: str | None = None,
        upload_timeout_seconds: float = 30.0,
    ) -> None:
        self.repository = repository
        self.worker_base_url = worker_base_url.rstrip("/")
        self.internal_bearer_secret = internal_bearer_secret or os.getenv(
            "KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET"
        )
        self.upload_timeout_seconds = upload_timeout_seconds

    async def upload_draft(
        self, *, package: UploadFile, current_user: CurrentUser | None = None
    ) -> dict[str, Any]:
        if not self.internal_bearer_secret:
            raise KnowledgeAdminWorkerUnavailableError(
                "internal publisher secret is not configured"
            )
        try:
            if hasattr(package.file, "seek"):
                package.file.seek(0)
            timeout = httpx.Timeout(
                connect=self.upload_timeout_seconds,
                read=self.upload_timeout_seconds,
                write=self.upload_timeout_seconds,
                pool=self.upload_timeout_seconds,
            )
            headers = {
                "Authorization": f"Bearer {self.internal_bearer_secret}",
            }
            if current_user is not None:
                headers.update(
                    {
                        "X-Knowledge-Actor-User-Id": str(current_user.id),
                        "X-Knowledge-Actor-Username": current_user.username,
                    }
                )
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.worker_base_url}/internal/drafts",
                    headers=headers,
                    files={
                        "package": (
                            package.filename or "knowledge.zip",
                            package.file,
                            "application/zip",
                        )
                    },
                )
        except httpx.TimeoutException as exc:
            raise KnowledgeAdminWorkerUnavailableError("publisher worker timed out") from exc
        except httpx.HTTPError as exc:
            raise KnowledgeAdminWorkerUnavailableError("publisher worker unavailable") from exc
        if 500 <= response.status_code < 600:
            raise KnowledgeAdminWorkerUnavailableError("publisher worker unavailable")
        if response.status_code >= 400:
            raise KnowledgeAdminMalformedRequestError("publisher worker rejected upload")
        try:
            payload = response.json()
        except Exception as exc:  # pragma: no cover - defensive
            raise KnowledgeAdminMalformedRequestError("publisher worker response is invalid") from exc
        if not isinstance(payload, dict):
            raise KnowledgeAdminMalformedRequestError("publisher worker response is invalid")
        for field in ("draft_id", "job_id", "status"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise KnowledgeAdminMalformedRequestError(
                    "publisher worker response is invalid"
                )
        return payload

    def list_drafts(self) -> list[Any]:
        return self.repository.list_drafts()

    def get_draft(self, draft_id: str) -> Any:
        try:
            return self.repository.get_draft(str(draft_id))
        except ValueError as exc:
            message = str(exc)
            if message.startswith("Unknown knowledge"):
                raise KnowledgeAdminNotFoundError(message) from exc
            raise KnowledgeAdminInvalidStateError(message) from exc

    def queue_validation(self, draft_id: str, *, current_user: CurrentUser) -> Any:
        return self._queue_job(
            self.repository.queue_validation,
            draft_id,
            current_user=current_user,
        )

    def queue_publish(self, draft_id: str, *, current_user: CurrentUser) -> Any:
        return self._queue_job(
            self.repository.queue_publish,
            draft_id,
            current_user=current_user,
        )

    def list_releases(self) -> list[Any]:
        return self.repository.list_releases()

    def list_audit_events(self, *, limit: int = 50) -> list[Any]:
        return self.repository.list_audit_events(limit=limit)

    def queue_rollback(
        self, release_build_id: str, *, reason: str, current_user: CurrentUser
    ) -> Any:
        try:
            return self.repository.queue_rollback(
                release_build_id,
                reason_summary=reason,
                actor_user_id=str(current_user.id),
                actor_username=current_user.username,
            )
        except ValueError as exc:
            message = str(exc)
            if message.startswith("Unknown knowledge"):
                raise KnowledgeAdminNotFoundError(message) from exc
            raise KnowledgeAdminInvalidStateError(message) from exc

    def get_job(self, job_id: str) -> Any:
        try:
            return self.repository.get_job(str(job_id))
        except ValueError as exc:
            raise KnowledgeAdminNotFoundError(str(exc)) from exc

    def _queue_job(
        self,
        operation: Any,
        draft_id: str,
        *,
        current_user: CurrentUser,
    ) -> Any:
        try:
            return operation(
                str(draft_id),
                actor_user_id=str(current_user.id),
                actor_username=current_user.username,
            )
        except ValueError as exc:
            message = str(exc)
            if message.startswith("Unknown knowledge"):
                raise KnowledgeAdminNotFoundError(message) from exc
            raise KnowledgeAdminInvalidStateError(message) from exc


class _LazyKnowledgeAdminService:
    """Delay DB-backed service construction until the first admin request."""

    def __init__(self) -> None:
        self._service: KnowledgeAdminService | None = None

    def _resolve(self) -> KnowledgeAdminService:
        if self._service is None:
            self._service = KnowledgeAdminService(repository=build_default_repository())
        return self._service

    async def upload_draft(
        self, *, package: UploadFile, current_user: CurrentUser | None = None
    ) -> dict[str, Any]:
        return await self._resolve().upload_draft(
            package=package, current_user=current_user
        )

    def list_drafts(self) -> list[Any]:
        return self._resolve().list_drafts()

    def get_draft(self, draft_id: str) -> Any:
        return self._resolve().get_draft(draft_id)

    def queue_validation(self, draft_id: str, *, current_user: CurrentUser) -> Any:
        return self._resolve().queue_validation(draft_id, current_user=current_user)

    def queue_publish(self, draft_id: str, *, current_user: CurrentUser) -> Any:
        return self._resolve().queue_publish(draft_id, current_user=current_user)

    def list_releases(self) -> list[Any]:
        return self._resolve().list_releases()

    def list_audit_events(self, *, limit: int = 50) -> list[Any]:
        return self._resolve().list_audit_events(limit=limit)

    def queue_rollback(
        self, release_build_id: str, *, reason: str, current_user: CurrentUser
    ) -> Any:
        return self._resolve().queue_rollback(
            release_build_id, reason=reason, current_user=current_user
        )

    def get_job(self, job_id: str) -> Any:
        return self._resolve().get_job(job_id)


class KnowledgePublisherService:
    def __init__(
        self,
        *,
        repository: KnowledgeAdminRepository,
        staging_root: Path | str,
        artifact_root: Path | str,
        indexer: Any,
        chroma_client: Any,
        redis_client: Any,
        internal_actor_user_id: str = "knowledge-publisher",
        internal_actor_username: str = "knowledge-publisher",
        publication_lock_key: str = "knowledge:publication",
    ) -> None:
        self.repository = repository
        self.staging_root = Path(staging_root)
        self.artifact_root = Path(artifact_root)
        self.indexer = indexer
        self.chroma_client = chroma_client
        self.redis_client = redis_client
        self.internal_actor_user_id = internal_actor_user_id
        self.internal_actor_username = internal_actor_username
        self.publication_lock_key = publication_lock_key

    def ingest_draft(
        self,
        package_path: Path,
        *,
        original_filename: str,
        actor_user_id: str | None = None,
        actor_username: str | None = None,
    ) -> dict[str, str]:
        draft_id = uuid4().hex
        staged = stage_zip_knowledge_package(
            package_path, draft_id=draft_id, staging_root=self.staging_root
        )
        resolved_actor_user_id = actor_user_id or self.internal_actor_user_id
        resolved_actor_username = actor_username or self.internal_actor_username
        try:
            create_and_queue = getattr(
                self.repository, "create_draft_and_queue_validation"
            )
        except AttributeError:
            create_and_queue = None

        try:
            if callable(create_and_queue):
                draft, job = create_and_queue(
                    original_filename=original_filename,
                    package_sha256=staged.package_sha256,
                    package_size_bytes=staged.package_size_bytes,
                    storage_key=draft_id,
                    actor_user_id=resolved_actor_user_id,
                    actor_username=resolved_actor_username,
                )
            else:
                draft = self.repository.create_draft(
                    original_filename=original_filename,
                    package_sha256=staged.package_sha256,
                    package_size_bytes=staged.package_size_bytes,
                    storage_key=draft_id,
                    actor_user_id=resolved_actor_user_id,
                    actor_username=resolved_actor_username,
                )
                job = self.repository.queue_validation(
                    draft.id,
                    actor_user_id=resolved_actor_user_id,
                    actor_username=resolved_actor_username,
                )
        except Exception:
            shutil.rmtree(staged.source_root, ignore_errors=True)
            raise
        return {"draft_id": draft.id, "job_id": job.id, "status": "queued"}

    def process_job(self, job: KnowledgeJob | Any) -> dict[str, Any]:
        try:
            if job.kind == "validate_draft":
                return self._validate_draft(job)
            if job.kind == "publish_draft":
                return self._publish_draft(job)
            if job.kind == "rollback_release":
                return self._rollback_release(job)
            if job.kind == "get_status":
                return self._get_status(job)
            raise ValueError(f"Unsupported knowledge job kind {job.kind}")
        except Exception as exc:
            if hasattr(self.repository, "fail_job"):
                self.repository.fail_job(job.id, self._safe_failure_summary(exc))
            raise

    def process_next_job(self) -> dict[str, Any] | None:
        job = self.repository.claim_next_job()
        if job is None:
            return None
        return self.process_job(job)

    def _validate_draft(self, job: KnowledgeJob | Any) -> dict[str, Any]:
        draft = self.repository.get_draft(self._required_draft_id(job))
        self._require_draft_status(draft, "validating")
        source_root = self._staged_source_root(draft)
        validated_source = validate_source_root(source_root)
        report = self._validation_report(draft, validated_source)
        self.repository.complete_job(job.id, validation_report=report)
        return report

    def _publish_draft(self, job: KnowledgeJob | Any) -> dict[str, Any]:
        draft = self.repository.get_draft(self._required_draft_id(job))
        self._require_draft_status(draft, "publishing")
        source_root = self._staged_source_root(draft)
        validated_source = validate_source_root(source_root)
        result = self.indexer.build_and_publish_from_staged_source(source_root)
        build_id = result.get("build_id")
        if not isinstance(build_id, str) or not build_id:
            raise KnowledgePublisherError("indexer must return a build_id")
        release = read_validated_release(self.artifact_root, build_id)
        validation_summary = self._validation_report(draft, validated_source)
        self.repository.complete_publish_job(
            job.id,
            build_id=release.build_id,
            collection_name=release.collection_name,
            artifact_sha256=release.sha256,
            source_manifest_sha256=release.source_tree_sha256,
            draft_id=draft.id,
            document_count=validation_summary["document_count"],
            validation_summary=validation_summary,
            published_at=datetime.now(UTC),
        )
        return dict(result)

    def _rollback_release(self, job: KnowledgeJob | Any) -> dict[str, Any]:
        if not job.release_build_id:
            raise ValueError("rollback_release requires a release build id")
        with PublicationLock(self.redis_client, self.publication_lock_key) as publication_lock:
            pointer = restore_validated_release(
                self.chroma_client,
                self.artifact_root,
                job.release_build_id,
                before_publish=publication_lock.assert_held,
            )
        self.repository.complete_rollback_job(job.id, pointer.current_build_id)
        return {"status": "rolled_back", "build_id": pointer.current_build_id}

    def _get_status(self, job: KnowledgeJob | Any) -> dict[str, Any]:
        status: dict[str, Any] = {"status": "observed", "job_id": job.id}
        draft_id = getattr(job, "draft_id", None)
        if draft_id:
            draft = self.repository.get_draft(draft_id)
            status["draft_status"] = draft.status
        if getattr(job, "release_build_id", None):
            status["release_build_id"] = job.release_build_id
        self.repository.complete_job(job.id)
        return status

    def _staged_source_root(self, draft: KnowledgeDraft | Any) -> Path:
        return self.staging_root / str(draft.storage_key)

    @staticmethod
    def _required_draft_id(job: KnowledgeJob | Any) -> str:
        draft_id = getattr(job, "draft_id", None)
        if not draft_id:
            raise ValueError("job requires a draft id")
        return draft_id

    @staticmethod
    def _require_draft_status(draft: KnowledgeDraft | Any, expected: str) -> None:
        if getattr(draft, "status", None) != expected:
            raise ValueError(f"draft must be {expected}")

    @staticmethod
    def _validation_report(draft: KnowledgeDraft | Any, validated_source: Any) -> dict[str, Any]:
        return {
            "status": "valid",
            "draft_id": draft.id,
            "original_filename": draft.original_filename,
            "package_sha256": draft.package_sha256,
            "package_size_bytes": draft.package_size_bytes,
            "document_count": len(validated_source.documents),
            "smoke_query_count": len(validated_source.smoke_queries),
            "source_tree_sha256": validated_source.source_tree_sha256,
            "git_commit": validated_source.git_commit,
        }

    def _safe_failure_summary(self, exc: Exception) -> str:
        summary = f"{type(exc).__name__}: {exc}"
        for root in (self.staging_root, self.artifact_root):
            root_text = str(root)
            if root_text:
                summary = summary.replace(root_text, "<redacted-path>")
        return summary[:512]


def build_default_repository() -> KnowledgeAdminRepository:
    from metro_agent.storage.history.database import SessionLocal

    return KnowledgeAdminRepository(SessionLocal)


def build_default_knowledge_admin_service() -> KnowledgeAdminService:
    return _LazyKnowledgeAdminService()


def build_default_service() -> KnowledgePublisherService:
    settings = KnowledgeSettings.from_environment()
    if settings.artifact_root is None:
        raise ValueError("KNOWLEDGE_ARTIFACT_ROOT must be configured")
    staging_root = require_knowledge_upload_staging_root(settings.upload_staging_root)
    import redis

    chroma_client = get_chroma_client(settings)
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )
    indexer = KnowledgeIndexer(
        client=chroma_client,
        artifact_root=settings.artifact_root,
        redis_client=redis_client,
    )
    return KnowledgePublisherService(
        repository=build_default_repository(),
        staging_root=staging_root,
        artifact_root=settings.artifact_root,
        indexer=indexer,
        chroma_client=chroma_client,
        redis_client=redis_client,
    )
