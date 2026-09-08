from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from metro_agent.knowledge.chroma_client import get_chroma_client
from metro_agent.knowledge.config import (
    KnowledgeSettings,
    require_knowledge_upload_staging_root,
)
from metro_agent.knowledge.publication_lock import PublicationLock
from metro_agent.knowledge.releases import restore_validated_release
from metro_agent.knowledge.source_validation import validate_source_root
from metro_agent.knowledge_admin.repository import KnowledgeAdminRepository
from metro_agent.knowledge_admin.zip_staging import stage_zip_knowledge_package
from metro_agent.knowledge_admin.models import KnowledgeDraft
from metro_agent.knowledge_admin.models import KnowledgeJob
from metro_agent.tools.knowledge_indexer import KnowledgeIndexer


class KnowledgePublisherError(RuntimeError):
    """The controlled publisher cannot complete a job safely."""


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
        self, package_path: Path, *, original_filename: str
    ) -> dict[str, str]:
        draft_id = uuid4().hex
        staged = stage_zip_knowledge_package(
            package_path, draft_id=draft_id, staging_root=self.staging_root
        )
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
                    actor_user_id=self.internal_actor_user_id,
                    actor_username=self.internal_actor_username,
                )
            else:
                draft = self.repository.create_draft(
                    original_filename=original_filename,
                    package_sha256=staged.package_sha256,
                    package_size_bytes=staged.package_size_bytes,
                    storage_key=draft_id,
                    actor_user_id=self.internal_actor_user_id,
                    actor_username=self.internal_actor_username,
                )
                job = self.repository.queue_validation(
                    draft.id,
                    actor_user_id=self.internal_actor_user_id,
                    actor_username=self.internal_actor_username,
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
        self._require_draft_status(draft, "ready_to_publish")
        source_root = self._staged_source_root(draft)
        validate_source_root(source_root)
        result = self.indexer.build_and_publish_from_staged_source(source_root)
        self.repository.complete_job(job.id)
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
