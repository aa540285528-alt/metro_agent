from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

import pytest
from pydantic import ValidationError

from metro_agent.knowledge_admin.models import (
    DRAFT_STATUSES,
    JOB_KINDS,
    KnowledgeAdminAuditEvent,
    KnowledgeDraft,
    KnowledgeJob,
    new_uuid4,
    transition_draft,
)
from metro_agent.knowledge_admin.schemas import DraftDetail, ReleaseListItem


def test_knowledge_metadata_is_postgres_owned_and_has_governance_tables() -> None:
    tables = KnowledgeDraft.metadata.tables

    assert {
        "knowledge_drafts",
        "knowledge_jobs",
        "knowledge_releases",
        "knowledge_admin_audit_events",
    }.issubset(tables)
    assert "users" not in tables
    assert not any(
        foreign_key.target_fullname.startswith("users.")
        for table in tables.values()
        for column in table.columns
        for foreign_key in column.foreign_keys
    )
    assert KnowledgeDraft.__table__.c.actor_user_id.type.length == 128
    assert KnowledgeDraft.__table__.c.actor_username.type.length == 128
    assert KnowledgeDraft.__table__.c.storage_key.type.length == 512
    assert KnowledgeJob.__table__.c.kind.type.length == 32
    assert KnowledgeAdminAuditEvent.__table__.c.failure_summary.type.length == 512
    assert KnowledgeAdminAuditEvent.__table__.c.reason_summary.type.length == 512


def test_draft_transition_graph_blocks_republishing_a_published_draft() -> None:
    draft = KnowledgeDraft(
        original_filename="knowledge.zip",
        package_sha256="a" * 64,
        package_size_bytes=10,
        storage_key="drafts/private/knowledge.zip",
        actor_user_id="42",
        actor_username="admin",
        status="uploaded",
    )

    for status in ("validating", "ready_to_publish", "publishing", "published"):
        transition_draft(draft, status)

    assert draft.status == "published"
    with pytest.raises(ValueError, match="published.*publishing"):
        transition_draft(draft, "publishing")
    assert draft.status == "published"


def test_draft_revalidation_only_starts_from_retryable_states() -> None:
    draft = KnowledgeDraft(
        original_filename="knowledge.zip",
        package_sha256="b" * 64,
        package_size_bytes=10,
        storage_key="drafts/private/knowledge.zip",
        actor_user_id="42",
        actor_username="admin",
        status="uploaded",
    )

    transition_draft(draft, "validating")
    transition_draft(draft, "validation_failed")
    transition_draft(draft, "validating")
    transition_draft(draft, "ready_to_publish")
    transition_draft(draft, "publishing")
    transition_draft(draft, "publish_failed")
    transition_draft(draft, "validating")

    assert draft.status == "validating"
    failed_publish = KnowledgeDraft(
        original_filename="retry.zip",
        package_sha256="e" * 64,
        package_size_bytes=10,
        storage_key="drafts/private/retry.zip",
        actor_user_id="42",
        actor_username="admin",
        status="publish_failed",
    )
    with pytest.raises(ValueError, match="publish_failed.*publishing"):
        transition_draft(failed_publish, "publishing")
    with pytest.raises(ValueError, match="validating.*validating"):
        transition_draft(draft, "validating")
    assert set(DRAFT_STATUSES) == {
        "uploaded",
        "validating",
        "ready_to_publish",
        "publishing",
        "published",
        "validation_failed",
        "publish_failed",
    }


def test_models_generate_uuid4_identifiers_and_restrict_job_kinds() -> None:
    draft = KnowledgeDraft(
        original_filename="knowledge.zip",
        package_sha256="c" * 64,
        package_size_bytes=10,
        storage_key="drafts/private/knowledge.zip",
        actor_user_id="42",
        actor_username="admin",
    )
    job = KnowledgeJob(
        kind="validate_draft",
        actor_user_id="42",
        actor_username="admin",
        draft=draft,
    )

    assert UUID(new_uuid4(), version=4).version == 4
    assert callable(KnowledgeDraft.__table__.c.id.default.arg)
    assert callable(KnowledgeJob.__table__.c.id.default.arg)
    assert job.kind == "validate_draft"
    assert set(JOB_KINDS) == {
        "validate_draft",
        "publish_draft",
        "rollback_release",
        "get_status",
    }
    assert all(
        foreign_key.target_fullname != "users.id"
        for foreign_key in KnowledgeAdminAuditEvent.__table__.c.actor_user_id.foreign_keys
    )


def test_public_dtos_reject_unknown_fields_and_hide_private_names() -> None:
    detail = DraftDetail(
        id="draft-1",
        status="uploaded",
        original_filename="knowledge.zip",
        package_sha256="d" * 64,
        package_size_bytes=10,
        actor_username="admin",
        validation_report=None,
        created_at="2026-09-07T00:00:00Z",
        updated_at="2026-09-07T00:00:00Z",
    )
    release = ReleaseListItem(
        build_id="build-1",
        document_count=3,
        status="current",
        published_at="2026-09-07T00:00:00Z",
        artifact_sha256="a" * 64,
        package_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        draft_id="draft-1",
        validation_summary={"valid": True},
    )

    assert "storage_key" not in detail.model_dump()
    assert "collection_name" not in release.model_dump()
    with pytest.raises(ValidationError):
        DraftDetail(
            **detail.model_dump(), storage_key="drafts/private/knowledge.zip"
        )
    with pytest.raises(ValidationError):
        ReleaseListItem(**release.model_dump(), collection_name="unpublished")


def test_main_alembic_migration_creates_governed_knowledge_tables() -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql+psycopg://metro:secret@localhost:5432/metro"
    source_root = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH")) if part
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for table_name in {
        "knowledge_drafts",
        "knowledge_jobs",
        "knowledge_releases",
        "knowledge_admin_audit_events",
    }:
        assert f"CREATE TABLE {table_name}" in result.stdout
    assert "REFERENCES users" not in result.stdout
    assert "CREATE UNIQUE INDEX uq_knowledge_releases_one_current" in result.stdout
    assert "WHERE status = 'current'" in result.stdout
    assert "CREATE FUNCTION prevent_knowledge_admin_audit_event_mutation" in result.stdout
    assert "CREATE TRIGGER trg_knowledge_admin_audit_events_append_only" in result.stdout
