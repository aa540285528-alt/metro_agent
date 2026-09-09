"""Add governed knowledge administration tables.

Revision ID: 20260907_04
Revises: 20260720_03
Create Date: 2026-09-07 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260907_04"
down_revision: Union[str, Sequence[str], None] = "20260720_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("package_sha256", sa.String(length=64), nullable=False),
        sa.Column("package_size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("actor_user_id", sa.String(length=128), nullable=False),
        sa.Column("actor_username", sa.String(length=128), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('uploaded', 'validating', 'ready_to_publish', 'publishing', "
            "'published', 'validation_failed', 'publish_failed')",
            name="ck_knowledge_drafts_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_drafts_status_created_at",
        "knowledge_drafts",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "knowledge_releases",
        sa.Column("build_id", sa.String(length=128), nullable=False),
        sa.Column("collection_name", sa.String(length=256), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("validation_summary", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('current', 'superseded', 'rolled_back')",
            name="ck_knowledge_releases_status",
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["knowledge_drafts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("build_id"),
    )
    op.create_index("ix_knowledge_releases_draft_id", "knowledge_releases", ["draft_id"])
    op.create_index(
        "ix_knowledge_releases_status_published_at",
        "knowledge_releases",
        ["status", "published_at"],
        unique=False,
    )

    op.create_table(
        "knowledge_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("release_build_id", sa.String(length=128), nullable=True),
        sa.Column("actor_user_id", sa.String(length=128), nullable=False),
        sa.Column("actor_username", sa.String(length=128), nullable=False),
        sa.Column("failure_summary", sa.String(length=512), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('validate_draft', 'publish_draft', 'rollback_release', 'get_status')",
            name="ck_knowledge_jobs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_knowledge_jobs_status",
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["knowledge_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_build_id"], ["knowledge_releases.build_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_jobs_draft_id", "knowledge_jobs", ["draft_id"])
    op.create_index(
        "ix_knowledge_jobs_release_build_id", "knowledge_jobs", ["release_build_id"]
    )
    op.create_index(
        "ix_knowledge_jobs_status_queued_at",
        "knowledge_jobs",
        ["status", "queued_at"],
        unique=False,
    )

    op.create_table(
        "knowledge_admin_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=128), nullable=False),
        sa.Column("actor_username", sa.String(length=128), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("release_build_id", sa.String(length=128), nullable=True),
        sa.Column("reason_summary", sa.String(length=512), nullable=True),
        sa.Column("failure_summary", sa.String(length=512), nullable=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["knowledge_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["knowledge_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_build_id"], ["knowledge_releases.build_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_admin_audit_events_draft_id", "knowledge_admin_audit_events", ["draft_id"]
    )
    op.create_index(
        "ix_knowledge_admin_audit_events_job_id", "knowledge_admin_audit_events", ["job_id"]
    )
    op.create_index(
        "ix_knowledge_admin_audit_events_release_build_id",
        "knowledge_admin_audit_events",
        ["release_build_id"],
    )
    op.create_index(
        "ix_knowledge_admin_audit_events_occurred_at",
        "knowledge_admin_audit_events",
        ["occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_admin_audit_events_occurred_at", table_name="knowledge_admin_audit_events")
    op.drop_index("ix_knowledge_admin_audit_events_release_build_id", table_name="knowledge_admin_audit_events")
    op.drop_index("ix_knowledge_admin_audit_events_job_id", table_name="knowledge_admin_audit_events")
    op.drop_index("ix_knowledge_admin_audit_events_draft_id", table_name="knowledge_admin_audit_events")
    op.drop_table("knowledge_admin_audit_events")
    op.drop_index("ix_knowledge_jobs_status_queued_at", table_name="knowledge_jobs")
    op.drop_index("ix_knowledge_jobs_release_build_id", table_name="knowledge_jobs")
    op.drop_index("ix_knowledge_jobs_draft_id", table_name="knowledge_jobs")
    op.drop_table("knowledge_jobs")
    op.drop_index("ix_knowledge_releases_status_published_at", table_name="knowledge_releases")
    op.drop_index("ix_knowledge_releases_draft_id", table_name="knowledge_releases")
    op.drop_table("knowledge_releases")
    op.drop_index("ix_knowledge_drafts_status_created_at", table_name="knowledge_drafts")
    op.drop_table("knowledge_drafts")
