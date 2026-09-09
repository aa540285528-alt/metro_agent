from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from metro_agent.storage.history.models import Base

DRAFT_STATUSES: Final = (
    "uploaded",
    "validating",
    "ready_to_publish",
    "publishing",
    "published",
    "validation_failed",
    "publish_failed",
)
JOB_KINDS: Final = (
    "validate_draft",
    "publish_draft",
    "rollback_release",
    "get_status",
)
JOB_STATUSES: Final = ("queued", "running", "succeeded", "failed")
RELEASE_STATUSES: Final = ("current", "superseded", "rolled_back")

_DRAFT_TRANSITIONS: Final = {
    "uploaded": {"validating"},
    "validating": {"ready_to_publish", "validation_failed"},
    "ready_to_publish": {"publishing"},
    "publishing": {"published", "publish_failed"},
    "validation_failed": {"validating"},
    "publish_failed": {"validating"},
    "published": set(),
}


def new_uuid4() -> str:
    return str(uuid4())


def transition_draft(draft: KnowledgeDraft, target_status: str) -> None:
    """Apply one of the governed draft state transitions in memory."""
    if target_status not in _DRAFT_TRANSITIONS.get(draft.status, set()):
        raise ValueError(
            f"Illegal draft transition {draft.status} -> {target_status}"
        )
    draft.status = target_status


class KnowledgeDraft(Base):
    __tablename__ = "knowledge_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded', 'validating', 'ready_to_publish', 'publishing', "
            "'published', 'validation_failed', 'publish_failed')",
            name="ck_knowledge_drafts_status",
        ),
        Index("ix_knowledge_drafts_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded")
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    package_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_username: Mapped[str] = mapped_column(String(128), nullable=False)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    jobs: Mapped[list[KnowledgeJob]] = relationship(back_populates="draft")
    releases: Mapped[list[KnowledgeRelease]] = relationship(back_populates="draft")


class KnowledgeRelease(Base):
    __tablename__ = "knowledge_releases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('current', 'superseded', 'rolled_back')",
            name="ck_knowledge_releases_status",
        ),
        Index("ix_knowledge_releases_status_published_at", "status", "published_at"),
    )

    build_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    collection_name: Mapped[str] = mapped_column(String(256), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_drafts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    document_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="current")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    draft: Mapped[KnowledgeDraft] = relationship(back_populates="releases")
    jobs: Mapped[list[KnowledgeJob]] = relationship(back_populates="release")


class KnowledgeJob(Base):
    __tablename__ = "knowledge_jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('validate_draft', 'publish_draft', 'rollback_release', 'get_status')",
            name="ck_knowledge_jobs_kind",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_knowledge_jobs_status",
        ),
        Index("ix_knowledge_jobs_status_queued_at", "status", "queued_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_drafts.id", ondelete="RESTRICT"), index=True
    )
    release_build_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_releases.build_id", ondelete="RESTRICT"), index=True
    )
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_username: Mapped[str] = mapped_column(String(128), nullable=False)
    failure_summary: Mapped[str | None] = mapped_column(String(512))
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draft: Mapped[KnowledgeDraft | None] = relationship(back_populates="jobs")
    release: Mapped[KnowledgeRelease | None] = relationship(back_populates="jobs")


class KnowledgeAdminAuditEvent(Base):
    __tablename__ = "knowledge_admin_audit_events"
    __table_args__ = (Index("ix_knowledge_admin_audit_events_occurred_at", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid4)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_username: Mapped[str] = mapped_column(String(128), nullable=False)
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_drafts.id", ondelete="RESTRICT"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_jobs.id", ondelete="RESTRICT"), index=True
    )
    release_build_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_releases.build_id", ondelete="RESTRICT"), index=True
    )
    reason_summary: Mapped[str | None] = mapped_column(String(512))
    failure_summary: Mapped[str | None] = mapped_column(String(512))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
