from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from metro_agent.knowledge_admin.models import (
    Base,
    KnowledgeAdminAuditEvent,
    KnowledgeDraft,
    KnowledgeJob,
    KnowledgeRelease,
)
from metro_agent.knowledge_admin.repository import KnowledgeAdminRepository


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def repository(session_factory: sessionmaker[Session]) -> KnowledgeAdminRepository:
    return KnowledgeAdminRepository(session_factory)


def create_draft(repository: KnowledgeAdminRepository) -> KnowledgeDraft:
    return repository.create_draft(
        original_filename="knowledge.zip",
        package_sha256="a" * 64,
        package_size_bytes=42,
        storage_key="drafts/private/a/knowledge.zip",
        actor_user_id="42",
        actor_username="admin",
    )


def test_queue_validation_commits_transition_job_and_audit_event(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session]
) -> None:
    draft = create_draft(repository)

    job = repository.queue_validation(
        draft.id, actor_user_id="42", actor_username="admin"
    )

    assert job.kind == "validate_draft"
    assert job.status == "queued"
    with session_factory() as session:
        stored_draft = session.get(KnowledgeDraft, draft.id)
        stored_job = session.get(KnowledgeJob, job.id)
        assert stored_draft is not None and stored_draft.status == "validating"
        assert stored_job is not None and stored_job.draft_id == draft.id
        assert session.scalar(select(KnowledgeJob).where(KnowledgeJob.id == job.id))
        assert len(stored_draft.jobs) == 1
        audit = session.scalar(
            select(KnowledgeAdminAuditEvent).where(
                KnowledgeAdminAuditEvent.job_id == job.id
            )
        )
        assert audit is not None
        assert audit.action == "validate_draft"
        assert audit.result == "queued"


def test_create_draft_and_queue_validation_rolls_back_partial_work_when_enqueue_fails(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> object:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(repository, "_enqueue", boom)

    with pytest.raises(RuntimeError, match="enqueue failed"):
        repository.create_draft_and_queue_validation(
            original_filename="knowledge.zip",
            package_sha256="a" * 64,
            package_size_bytes=42,
            storage_key="drafts/private/a/knowledge.zip",
            actor_user_id="42",
            actor_username="admin",
        )

    with session_factory() as session:
        assert session.scalar(select(KnowledgeDraft)) is None
        assert session.scalar(select(KnowledgeJob)) is None
        assert session.scalar(select(KnowledgeAdminAuditEvent)) is None


def test_claiming_a_job_uses_postgres_skip_locked_and_only_claims_known_kinds(
    repository: KnowledgeAdminRepository,
) -> None:
    statement = repository.queued_job_claim_statement()
    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert "knowledge_jobs.kind IN" in compiled


def test_claim_and_failure_are_atomic_and_allow_revalidation(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session]
) -> None:
    draft = create_draft(repository)
    queued = repository.queue_validation(
        draft.id, actor_user_id="42", actor_username="admin"
    )

    claimed = repository.claim_next_job(lease_seconds=60)
    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == "running"
    assert claimed.started_at is not None
    assert claimed.lease_expires_at is not None

    repository.fail_job(queued.id, "bad package" * 100)
    retried = repository.queue_validation(
        draft.id, actor_user_id="42", actor_username="admin"
    )

    with session_factory() as session:
        failed = session.get(KnowledgeJob, queued.id)
        stored_draft = session.get(KnowledgeDraft, draft.id)
        assert failed is not None and failed.status == "failed"
        assert failed.finished_at is not None
        assert failed.failure_summary is not None and len(failed.failure_summary) == 512
        assert stored_draft is not None and stored_draft.status == "validating"
        assert retried.id != queued.id


def test_published_draft_cannot_be_queued_for_publish_again(
    repository: KnowledgeAdminRepository,
) -> None:
    draft = create_draft(repository)
    validation = repository.queue_validation(
        draft.id, actor_user_id="42", actor_username="admin"
    )
    repository.claim_next_job()
    repository.complete_job(validation.id, validation_report={"valid": True})
    publish = repository.queue_publish(
        draft.id, actor_user_id="42", actor_username="admin"
    )
    repository.claim_next_job()
    repository.complete_job(publish.id)

    with pytest.raises(ValueError, match="published.*publishing"):
        repository.queue_publish(draft.id, actor_user_id="42", actor_username="admin")


def test_public_list_dtos_do_not_disclose_storage_or_collection_names(
    repository: KnowledgeAdminRepository,
) -> None:
    draft = create_draft(repository)
    repository.record_release(
        build_id="build-20260907",
        collection_name="private-staging-collection",
        artifact_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        draft_id=draft.id,
        document_count=2,
        validation_summary={"valid": True},
        published_at=datetime(2026, 9, 7, tzinfo=UTC),
    )

    draft_data = repository.list_drafts()[0].model_dump()
    release_data = repository.list_releases()[0].model_dump()

    assert "storage_key" not in draft_data
    assert "collection_name" not in release_data


def test_record_release_supersedes_the_prior_current_release(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session]
) -> None:
    draft = create_draft(repository)
    repository.record_release(
        build_id="build-1",
        collection_name="private-1",
        artifact_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        draft_id=draft.id,
        document_count=1,
        validation_summary={"valid": True},
        published_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    repository.record_release(
        build_id="build-2",
        collection_name="private-2",
        artifact_sha256="d" * 64,
        source_manifest_sha256="e" * 64,
        draft_id=draft.id,
        document_count=2,
        validation_summary={"valid": True},
        published_at=datetime(2026, 9, 8, tzinfo=UTC),
    )

    with session_factory() as session:
        releases = session.scalars(
            select(KnowledgeRelease).order_by(KnowledgeRelease.build_id)
        ).all()
        assert [(release.build_id, release.status) for release in releases] == [
            ("build-1", "superseded"),
            ("build-2", "current"),
        ]


def test_generic_completion_rejects_rollback_until_a_dedicated_restore_exists(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session]
) -> None:
    draft = create_draft(repository)
    repository.record_release(
        build_id="build-rollback",
        collection_name="private-rollback",
        artifact_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        draft_id=draft.id,
        document_count=1,
        validation_summary={"valid": True},
        published_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    queued = repository.queue_rollback(
        "build-rollback", actor_user_id="42", actor_username="admin"
    )
    repository.claim_next_job()

    with pytest.raises(ValueError, match="rollback_release.*dedicated rollback"):
        repository.complete_job(queued.id)

    with session_factory() as session:
        job = session.get(KnowledgeJob, queued.id)
        assert job is not None and job.status == "running"


def test_dedicated_rollback_completion_records_success_after_target_restore(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session]
) -> None:
    draft = create_draft(repository)
    repository.record_release(
        build_id="build-restored",
        collection_name="private-restored",
        artifact_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        draft_id=draft.id,
        document_count=1,
        validation_summary={"valid": True},
        published_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    queued = repository.queue_rollback(
        "build-restored", actor_user_id="42", actor_username="admin"
    )
    repository.claim_next_job()

    repository.complete_rollback_job(queued.id, "build-restored")

    with session_factory() as session:
        job = session.get(KnowledgeJob, queued.id)
        audit = session.scalar(
            select(KnowledgeAdminAuditEvent).where(
                KnowledgeAdminAuditEvent.job_id == queued.id,
                KnowledgeAdminAuditEvent.result == "succeeded",
            )
        )
        assert job is not None and job.status == "succeeded"
        assert job.finished_at is not None and job.lease_expires_at is None
        assert audit is not None and audit.action == "rollback_release"


def test_dedicated_rollback_completion_rejects_wrong_target_without_state_change(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session]
) -> None:
    draft = create_draft(repository)
    repository.record_release(
        build_id="build-expected",
        collection_name="private-expected",
        artifact_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        draft_id=draft.id,
        document_count=1,
        validation_summary={"valid": True},
        published_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    queued = repository.queue_rollback(
        "build-expected", actor_user_id="42", actor_username="admin"
    )
    repository.claim_next_job()

    with pytest.raises(ValueError, match="does not match rollback target"):
        repository.complete_rollback_job(queued.id, "build-other")

    with session_factory() as session:
        job = session.get(KnowledgeJob, queued.id)
        assert job is not None and job.status == "running"


def test_dedicated_rollback_completion_rejects_other_job_kinds(
    repository: KnowledgeAdminRepository, session_factory: sessionmaker[Session]
) -> None:
    draft = create_draft(repository)
    queued = repository.queue_validation(
        draft.id, actor_user_id="42", actor_username="admin"
    )
    repository.claim_next_job()

    with pytest.raises(ValueError, match="not a rollback_release"):
        repository.complete_rollback_job(queued.id, "build-any")

    with session_factory() as session:
        job = session.get(KnowledgeJob, queued.id)
        assert job is not None and job.status == "running"
