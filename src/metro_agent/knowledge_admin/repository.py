from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from metro_agent.knowledge_admin.models import (
    JOB_KINDS,
    KnowledgeAdminAuditEvent,
    KnowledgeDraft,
    KnowledgeJob,
    KnowledgeRelease,
    transition_draft,
)
from metro_agent.knowledge_admin.schemas import AuditListItem, DraftListItem, ReleaseListItem


class KnowledgeAdminRepository:
    """PostgreSQL-backed state changes for the knowledge-admin workflow."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def queued_job_claim_statement() -> Select[tuple[KnowledgeJob]]:
        return (
            select(KnowledgeJob)
            .where(
                KnowledgeJob.status == "queued",
                KnowledgeJob.kind.in_(JOB_KINDS),
            )
            .order_by(KnowledgeJob.queued_at, KnowledgeJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    def create_draft(
        self,
        *,
        original_filename: str,
        package_sha256: str,
        package_size_bytes: int,
        storage_key: str,
        actor_user_id: str,
        actor_username: str,
    ) -> KnowledgeDraft:
        draft = KnowledgeDraft(
            original_filename=original_filename,
            package_sha256=package_sha256,
            package_size_bytes=package_size_bytes,
            storage_key=storage_key,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            status="uploaded",
        )
        with self._session_factory() as session, session.begin():
            session.add(draft)
            session.flush()
            self._audit(
                session,
                action="upload_draft",
                result="succeeded",
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                draft_id=draft.id,
            )
        return draft

    def create_draft_and_queue_validation(
        self,
        *,
        original_filename: str,
        package_sha256: str,
        package_size_bytes: int,
        storage_key: str,
        actor_user_id: str,
        actor_username: str,
    ) -> tuple[KnowledgeDraft, KnowledgeJob]:
        with self._session_factory() as session, session.begin():
            draft = KnowledgeDraft(
                original_filename=original_filename,
                package_sha256=package_sha256,
                package_size_bytes=package_size_bytes,
                storage_key=storage_key,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                status="uploaded",
            )
            session.add(draft)
            session.flush()
            self._audit(
                session,
                action="upload_draft",
                result="succeeded",
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                draft_id=draft.id,
            )
            transition_draft(draft, "validating")
            job = self._enqueue(
                session,
                kind="validate_draft",
                draft_id=draft.id,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
            )
        return draft, job

    def queue_validation(
        self, draft_id: str, *, actor_user_id: str, actor_username: str
    ) -> KnowledgeJob:
        with self._session_factory() as session, session.begin():
            draft = self._locked_draft(session, draft_id)
            transition_draft(draft, "validating")
            job = self._enqueue(
                session,
                kind="validate_draft",
                draft_id=draft.id,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
            )
        return job

    def queue_publish(
        self, draft_id: str, *, actor_user_id: str, actor_username: str
    ) -> KnowledgeJob:
        with self._session_factory() as session, session.begin():
            draft = self._locked_draft(session, draft_id)
            transition_draft(draft, "publishing")
            job = self._enqueue(
                session,
                kind="publish_draft",
                draft_id=draft.id,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
            )
        return job

    def queue_rollback(
        self,
        build_id: str,
        *,
        actor_user_id: str,
        actor_username: str,
        reason_summary: str | None = None,
    ) -> KnowledgeJob:
        with self._session_factory() as session, session.begin():
            release = session.scalar(
                select(KnowledgeRelease)
                .where(KnowledgeRelease.build_id == build_id)
                .with_for_update()
            )
            if release is None:
                raise ValueError(f"Unknown knowledge release {build_id}")
            active_job = session.scalar(
                select(KnowledgeJob.id)
                .where(
                    KnowledgeJob.kind == "rollback_release",
                    KnowledgeJob.release_build_id == release.build_id,
                    KnowledgeJob.status.in_(("queued", "running")),
                )
                .limit(1)
            )
            if active_job is not None:
                raise ValueError(
                    f"Knowledge release {build_id} already has an active rollback job"
                )
            job = self._enqueue(
                session,
                kind="rollback_release",
                release_build_id=release.build_id,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                reason_summary=reason_summary,
            )
        return job

    def claim_next_job(self, *, lease_seconds: int = 900) -> KnowledgeJob | None:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            job = session.scalar(self.queued_job_claim_statement())
            if job is None:
                return None
            job.status = "running"
            job.started_at = now
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            self._audit(
                session,
                action=job.kind,
                result="running",
                actor_user_id=job.actor_user_id,
                actor_username=job.actor_username,
                draft_id=job.draft_id,
                job_id=job.id,
                release_build_id=job.release_build_id,
            )
            session.flush()
        return job

    def complete_job(
        self, job_id: str, *, validation_report: dict[str, Any] | None = None
    ) -> None:
        with self._session_factory() as session, session.begin():
            job = self._locked_running_job(session, job_id)
            if job.kind == "rollback_release":
                raise ValueError(
                    "rollback_release requires a dedicated rollback restore operation"
                )
            if job.kind not in {"validate_draft", "publish_draft", "get_status"}:
                raise ValueError(f"Unsupported knowledge job completion kind {job.kind}")
            if job.kind == "validate_draft":
                draft = self._locked_draft(session, self._required_target(job.draft_id))
                draft.validation_report = validation_report
                transition_draft(draft, "ready_to_publish")
            elif job.kind == "publish_draft":
                draft = self._locked_draft(session, self._required_target(job.draft_id))
                transition_draft(draft, "published")
            job.status = "succeeded"
            job.finished_at = datetime.now(UTC)
            job.lease_expires_at = None
            self._audit_for_job(session, job, "succeeded")

    def complete_publish_job(
        self,
        job_id: str,
        *,
        build_id: str,
        collection_name: str,
        artifact_sha256: str,
        source_manifest_sha256: str,
        draft_id: str,
        document_count: int,
        validation_summary: dict[str, Any],
        published_at: datetime | None = None,
    ) -> KnowledgeRelease:
        with self._session_factory() as session, session.begin():
            job = self._locked_running_job(session, job_id)
            if job.kind != "publish_draft":
                raise ValueError(f"Knowledge job {job_id} is not a publish_draft")
            if job.draft_id != draft_id:
                raise ValueError(
                    f"Knowledge job {job_id} does not target knowledge draft {draft_id}"
                )
            draft = self._locked_draft(session, draft_id)
            release = self._record_release(
                session,
                build_id=build_id,
                collection_name=collection_name,
                artifact_sha256=artifact_sha256,
                source_manifest_sha256=source_manifest_sha256,
                draft_id=draft.id,
                document_count=document_count,
                validation_summary=validation_summary,
                published_at=published_at or datetime.now(UTC),
            )
            if draft.status == "ready_to_publish":
                transition_draft(draft, "publishing")
            transition_draft(draft, "published")
            job.status = "succeeded"
            job.finished_at = datetime.now(UTC)
            job.lease_expires_at = None
            self._audit_for_job(session, job, "succeeded")
            return release

    def complete_rollback_job(self, job_id: str, restored_release_build_id: str) -> None:
        """Record a worker-completed rollback after it restored the validated target."""
        with self._session_factory() as session, session.begin():
            job = self._locked_running_job(session, job_id)
            if job.kind != "rollback_release":
                raise ValueError(f"Knowledge job {job_id} is not a rollback_release")
            if job.release_build_id != restored_release_build_id:
                raise ValueError(
                    "Restored release does not match rollback target "
                    f"for knowledge job {job_id}"
                )
            restored_release = session.scalar(
                select(KnowledgeRelease)
                .where(KnowledgeRelease.build_id == restored_release_build_id)
                .with_for_update()
            )
            if restored_release is None:  # pragma: no cover - protected by FK
                raise ValueError(f"Unknown knowledge release {restored_release_build_id}")
            current_releases = session.scalars(
                select(KnowledgeRelease)
                .where(KnowledgeRelease.status == "current")
                .with_for_update()
            ).all()
            for current_release in current_releases:
                if current_release.build_id != restored_release.build_id:
                    current_release.status = "rolled_back"
            restored_release.status = "current"
            job.status = "succeeded"
            job.finished_at = datetime.now(UTC)
            job.lease_expires_at = None
            self._audit_for_job(session, job, "succeeded")

    def fail_job(self, job_id: str, failure_summary: str) -> None:
        with self._session_factory() as session, session.begin():
            job = self._locked_running_job(session, job_id)
            if job.kind == "validate_draft":
                transition_draft(
                    self._locked_draft(session, self._required_target(job.draft_id)),
                    "validation_failed",
                )
            elif job.kind == "publish_draft":
                transition_draft(
                    self._locked_draft(session, self._required_target(job.draft_id)),
                    "publish_failed",
                )
            job.status = "failed"
            job.failure_summary = failure_summary[:512]
            job.finished_at = datetime.now(UTC)
            job.lease_expires_at = None
            self._audit_for_job(session, job, "failed", failure_summary=job.failure_summary)

    def record_release(
        self,
        *,
        build_id: str,
        collection_name: str,
        artifact_sha256: str,
        source_manifest_sha256: str,
        draft_id: str,
        document_count: int,
        validation_summary: dict[str, Any],
        published_at: datetime,
    ) -> KnowledgeRelease:
        with self._session_factory() as session, session.begin():
            return self._record_release(
                session,
                build_id=build_id,
                collection_name=collection_name,
                artifact_sha256=artifact_sha256,
                source_manifest_sha256=source_manifest_sha256,
                draft_id=draft_id,
                document_count=document_count,
                validation_summary=validation_summary,
                published_at=published_at,
            )

    def _record_release(
        self,
        session: Session,
        *,
        build_id: str,
        collection_name: str,
        artifact_sha256: str,
        source_manifest_sha256: str,
        draft_id: str,
        document_count: int,
        validation_summary: dict[str, Any],
        published_at: datetime,
    ) -> KnowledgeRelease:
        release = KnowledgeRelease(
            build_id=build_id,
            collection_name=collection_name,
            artifact_sha256=artifact_sha256,
            source_manifest_sha256=source_manifest_sha256,
            draft_id=draft_id,
            document_count=document_count,
            validation_summary=validation_summary,
            published_at=published_at,
            status="current",
        )
        if session.get(KnowledgeDraft, draft_id) is None:
            raise ValueError(f"Unknown knowledge draft {draft_id}")
        current_releases = session.scalars(
            select(KnowledgeRelease)
            .where(KnowledgeRelease.status == "current")
            .with_for_update()
        ).all()
        for current_release in current_releases:
            current_release.status = "superseded"
        session.add(release)
        return release

    def list_drafts(self) -> list[DraftListItem]:
        with self._session_factory() as session:
            drafts = session.scalars(
                select(KnowledgeDraft).order_by(KnowledgeDraft.created_at.desc())
            ).all()
            return [
                DraftListItem(
                    id=draft.id,
                    status=draft.status,
                    original_filename=draft.original_filename,
                    package_sha256=draft.package_sha256,
                    package_size_bytes=draft.package_size_bytes,
                    actor_username=draft.actor_username,
                    created_at=draft.created_at,
                    updated_at=draft.updated_at,
                )
                for draft in drafts
            ]

    def get_draft(self, draft_id: str) -> KnowledgeDraft:
        with self._session_factory() as session:
            draft = session.get(KnowledgeDraft, draft_id)
            if draft is None:
                raise ValueError(f"Unknown knowledge draft {draft_id}")
            return draft

    def get_job(self, job_id: str) -> KnowledgeJob:
        with self._session_factory() as session:
            job = session.get(KnowledgeJob, job_id)
            if job is None:
                raise ValueError(f"Unknown knowledge job {job_id}")
            return job

    def list_releases(self) -> list[ReleaseListItem]:
        with self._session_factory() as session:
            releases = session.scalars(
                select(KnowledgeRelease).order_by(KnowledgeRelease.published_at.desc())
            ).all()
            return [
                ReleaseListItem(
                    build_id=release.build_id,
                    document_count=release.document_count,
                    status=release.status,
                    published_at=release.published_at,
                    artifact_sha256=release.artifact_sha256,
                    source_manifest_sha256=release.source_manifest_sha256,
                    draft_id=release.draft_id,
                    validation_summary=release.validation_summary,
                )
                for release in releases
            ]

    def list_audit_events(self, *, limit: int = 50) -> list[AuditListItem]:
        with self._session_factory() as session:
            events = session.scalars(
                select(KnowledgeAdminAuditEvent)
                .order_by(
                    KnowledgeAdminAuditEvent.occurred_at.desc(),
                    KnowledgeAdminAuditEvent.id.desc(),
                )
                .limit(limit)
            ).all()
            return [
                AuditListItem(
                    id=event.id,
                    action=event.action,
                    result=event.result,
                    actor_user_id=event.actor_user_id,
                    actor_username=event.actor_username,
                    draft_id=event.draft_id,
                    job_id=event.job_id,
                    release_build_id=event.release_build_id,
                    reason_summary=event.reason_summary,
                    failure_summary=event.failure_summary,
                    occurred_at=event.occurred_at,
                )
                for event in events
            ]

    @staticmethod
    def _required_target(target_id: str | None) -> str:
        if target_id is None:
            raise ValueError("Job has no required draft target")
        return target_id

    @staticmethod
    def _locked_draft(session: Session, draft_id: str) -> KnowledgeDraft:
        draft = session.scalar(
            select(KnowledgeDraft)
            .where(KnowledgeDraft.id == draft_id)
            .with_for_update()
        )
        if draft is None:
            raise ValueError(f"Unknown knowledge draft {draft_id}")
        return draft

    @staticmethod
    def _locked_running_job(session: Session, job_id: str) -> KnowledgeJob:
        job = session.scalar(
            select(KnowledgeJob).where(KnowledgeJob.id == job_id).with_for_update()
        )
        if job is None:
            raise ValueError(f"Unknown knowledge job {job_id}")
        if job.status != "running":
            raise ValueError(f"Knowledge job {job_id} is not running")
        return job

    def _enqueue(
        self,
        session: Session,
        *,
        kind: str,
        actor_user_id: str,
        actor_username: str,
        draft_id: str | None = None,
        release_build_id: str | None = None,
        reason_summary: str | None = None,
    ) -> KnowledgeJob:
        if kind not in JOB_KINDS:
            raise ValueError(f"Unsupported knowledge job kind {kind}")
        job = KnowledgeJob(
            kind=kind,
            status="queued",
            draft_id=draft_id,
            release_build_id=release_build_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        session.add(job)
        session.flush()
        self._audit_for_job(session, job, "queued", reason_summary=reason_summary)
        return job

    @staticmethod
    def _audit(
        session: Session,
        *,
        action: str,
        result: str,
        actor_user_id: str,
        actor_username: str,
        draft_id: str | None = None,
        job_id: str | None = None,
        release_build_id: str | None = None,
        reason_summary: str | None = None,
        failure_summary: str | None = None,
    ) -> None:
        session.add(
            KnowledgeAdminAuditEvent(
                action=action,
                result=result,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                draft_id=draft_id,
                job_id=job_id,
                release_build_id=release_build_id,
                reason_summary=reason_summary[:512] if reason_summary else None,
                failure_summary=failure_summary[:512] if failure_summary else None,
            )
        )

    def _audit_for_job(
        self,
        session: Session,
        job: KnowledgeJob,
        result: str,
        *,
        reason_summary: str | None = None,
        failure_summary: str | None = None,
    ) -> None:
        self._audit(
            session,
            action=job.kind,
            result=result,
            actor_user_id=job.actor_user_id,
            actor_username=job.actor_username,
            draft_id=job.draft_id,
            job_id=job.id,
            release_build_id=job.release_build_id,
            reason_summary=reason_summary,
            failure_summary=failure_summary,
        )
