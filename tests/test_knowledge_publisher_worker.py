from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import metro_agent.knowledge_admin.worker as worker_module
from metro_agent.knowledge_admin.worker import KnowledgePublisherService, create_app


ROOT = Path(__file__).resolve().parents[1]
SCRATCH_ROOT = ROOT / ".task3-test-artifacts" / "knowledge-publisher-tests"


def _write_valid_source(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "guide.md").write_text(
        """---
owner: operations
source: handbook
updated: 2026-09-01
effective_date: 2026-09-01
expires_at: 2026-12-31
risk_level: general
---
# Guide
""",
        encoding="utf-8",
    )
    (root / "release-smoke-queries.jsonl").write_text(
        '{"query":"How do I begin?","expected_source":"guide.md","minimum_matches":1}\n',
        encoding="utf-8",
    )


class FakeRepository:
    def __init__(self) -> None:
        self.created_drafts: list[dict[str, object]] = []
        self.queued_jobs: list[dict[str, object]] = []
        self.completed_jobs: list[tuple[str, dict[str, object] | None]] = []
        self.publish_completions: list[dict[str, object]] = []
        self.published_releases: list[dict[str, object]] = []
        self.failed_jobs: list[tuple[str, str]] = []
        self.rolled_back_jobs: list[tuple[str, str]] = []
        self.drafts: dict[str, SimpleNamespace] = {}
        self.jobs: dict[str, SimpleNamespace] = {}

    def create_draft(
        self,
        *,
        original_filename: str,
        package_sha256: str,
        package_size_bytes: int,
        storage_key: str,
        actor_user_id: str,
        actor_username: str,
    ) -> SimpleNamespace:
        record = {
            "original_filename": original_filename,
            "package_sha256": package_sha256,
            "package_size_bytes": package_size_bytes,
            "storage_key": storage_key,
            "actor_user_id": actor_user_id,
            "actor_username": actor_username,
        }
        self.created_drafts.append(record)
        draft = SimpleNamespace(
            id=storage_key,
            status="uploaded",
            original_filename=original_filename,
            package_sha256=package_sha256,
            package_size_bytes=package_size_bytes,
            storage_key=storage_key,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            validation_report=None,
        )
        self.drafts[draft.id] = draft
        return draft

    def queue_validation(
        self, draft_id: str, *, actor_user_id: str, actor_username: str
    ) -> SimpleNamespace:
        self.queued_jobs.append(
            {
                "draft_id": draft_id,
                "actor_user_id": actor_user_id,
                "actor_username": actor_username,
            }
        )
        job = SimpleNamespace(
            id="job-validate",
            kind="validate_draft",
            draft_id=draft_id,
            release_build_id=None,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        self.jobs[job.id] = job
        self.drafts[draft_id].status = "validating"
        return job

    def create_draft_and_queue_validation(
        self,
        *,
        original_filename: str,
        package_sha256: str,
        package_size_bytes: int,
        storage_key: str,
        actor_user_id: str,
        actor_username: str,
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        draft = self.create_draft(
            original_filename=original_filename,
            package_sha256=package_sha256,
            package_size_bytes=package_size_bytes,
            storage_key=storage_key,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        job = self.queue_validation(
            draft.id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return draft, job

    def get_draft(self, draft_id: str) -> SimpleNamespace:
        return self.drafts[draft_id]

    def complete_job(
        self, job_id: str, *, validation_report: dict[str, object] | None = None
    ) -> None:
        self.completed_jobs.append((job_id, validation_report))
        job = self.jobs[job_id]
        if job.kind == "validate_draft":
            draft = self.drafts[job.draft_id]
            draft.validation_report = validation_report
            draft.status = "ready_to_publish"
        elif job.kind == "publish_draft":
            self.drafts[job.draft_id].status = "published"

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
        validation_summary: dict[str, object],
        published_at: object | None = None,
    ) -> SimpleNamespace:
        self.publish_completions.append(
            {
                "job_id": job_id,
                "build_id": build_id,
                "collection_name": collection_name,
                "artifact_sha256": artifact_sha256,
                "source_manifest_sha256": source_manifest_sha256,
                "draft_id": draft_id,
                "document_count": document_count,
                "validation_summary": validation_summary,
                "published_at": published_at,
            }
        )
        self.drafts[draft_id].status = "published"
        self.drafts[draft_id].validation_report = validation_summary
        self.jobs[job_id].status = "succeeded"
        release = SimpleNamespace(
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
        self.published_releases.append(release.__dict__)
        return release

    def complete_rollback_job(
        self, job_id: str, restored_release_build_id: str
    ) -> None:
        self.rolled_back_jobs.append((job_id, restored_release_build_id))

    def fail_job(self, job_id: str, failure_summary: str) -> None:
        self.failed_jobs.append((job_id, failure_summary))


class FakeIndexer:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def build_and_publish_from_staged_source(self, staged_source_root: Path) -> dict[str, str]:
        self.calls.append(staged_source_root)
        return {"status": "published", "build_id": "build-1"}


def test_internal_draft_upload_requires_the_bearer_secret_and_queues_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = SCRATCH_ROOT / f"publisher-{uuid4().hex}"
    temp_root.mkdir(parents=True, exist_ok=False)
    try:
        repo = FakeRepository()
        service = KnowledgePublisherService(
            repository=repo,
            staging_root=temp_root / "staging",
            artifact_root=temp_root / "artifacts",
            indexer=FakeIndexer(),
            chroma_client=object(),
            redis_client=object(),
        )
        app = create_app(
            service=service,
            internal_bearer_secret="publisher-secret",
        )
        stage_calls: list[tuple[Path, str, Path]] = []

        def fake_stage(package_path: Path, *, draft_id: str, staging_root: Path) -> SimpleNamespace:
            stage_calls.append((package_path, draft_id, staging_root))
            staged_root = staging_root / draft_id
            return SimpleNamespace(
                package_sha256="a" * 64,
                package_size_bytes=17,
                source_root=staged_root,
                validated_source=SimpleNamespace(
                    root=staged_root,
                    documents=(),
                    smoke_queries=(),
                    source_tree_sha256="b" * 64,
                    git_commit=None,
                ),
            )

        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.stage_zip_knowledge_package",
            fake_stage,
        )
        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.uuid4",
            lambda: SimpleNamespace(hex="draft-123"),
        )

        client = TestClient(app)
        denied = client.post(
            "/internal/drafts",
            files={"package": ("knowledge.zip", b"zip-bytes", "application/zip")},
        )
        accepted = client.post(
            "/internal/drafts",
            headers={"Authorization": "Bearer publisher-secret"},
            files={"package": ("knowledge.zip", b"zip-bytes", "application/zip")},
        )
        actor_accepted = client.post(
            "/internal/drafts",
            headers={
                "Authorization": "Bearer publisher-secret",
                "X-Knowledge-Actor-User-Id": "17",
                "X-Knowledge-Actor-Username": "metro.admin",
            },
            files={"package": ("knowledge.zip", b"zip-bytes", "application/zip")},
        )
        health = client.get("/internal/healthz")

        assert denied.status_code == 401
        assert accepted.status_code == 201
        assert accepted.json() == {
            "draft_id": "draft-123",
            "job_id": "job-validate",
            "status": "queued",
        }
        assert actor_accepted.status_code == 201
        assert actor_accepted.json() == {
            "draft_id": "draft-123",
            "job_id": "job-validate",
            "status": "queued",
        }
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert len(stage_calls) == 2
        assert stage_calls[0][0].suffix == ".zip"
        assert stage_calls[0][1] == "draft-123"
        assert stage_calls[0][2] == temp_root / "staging"
        assert stage_calls[1][0].suffix == ".zip"
        assert stage_calls[1][1] == "draft-123"
        assert stage_calls[1][2] == temp_root / "staging"
        assert repo.created_drafts == [
            {
                "original_filename": "knowledge.zip",
                "package_sha256": "a" * 64,
                "package_size_bytes": 17,
                "storage_key": "draft-123",
                "actor_user_id": "knowledge-publisher",
                "actor_username": "knowledge-publisher",
            },
            {
                "original_filename": "knowledge.zip",
                "package_sha256": "a" * 64,
                "package_size_bytes": 17,
                "storage_key": "draft-123",
                "actor_user_id": "17",
                "actor_username": "metro.admin",
            },
        ]
        assert repo.queued_jobs == [
            {
                "draft_id": "draft-123",
                "actor_user_id": "knowledge-publisher",
                "actor_username": "knowledge-publisher",
            },
            {
                "draft_id": "draft-123",
                "actor_user_id": "17",
                "actor_username": "metro.admin",
            },
        ]
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_failed_job_summary_is_sanitized_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = SCRATCH_ROOT / f"publisher-{uuid4().hex}"
    temp_root.mkdir(parents=True, exist_ok=False)
    try:
        repo = FakeRepository()
        repo.drafts["draft-123"] = SimpleNamespace(
            id="draft-123",
            status="validating",
            original_filename="knowledge.zip",
            package_sha256="a" * 64,
            package_size_bytes=17,
            storage_key="draft-123",
            actor_user_id="knowledge-publisher",
            actor_username="knowledge-publisher",
            validation_report=None,
        )
        repo.jobs["job-validate"] = SimpleNamespace(
            id="job-validate",
            kind="validate_draft",
            draft_id="draft-123",
            release_build_id=None,
            actor_user_id="knowledge-publisher",
            actor_username="knowledge-publisher",
        )

        service = KnowledgePublisherService(
            repository=repo,
            staging_root=temp_root / "staging",
            artifact_root=temp_root / "artifacts",
            indexer=FakeIndexer(),
            chroma_client=object(),
            redis_client=object(),
        )
        abs_path = temp_root / "staging" / "draft-123"

        def boom(_: Path) -> object:
            raise ValueError(f"validation failed for {abs_path}")

        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.validate_source_root",
            boom,
        )

        with pytest.raises(ValueError, match="validation failed"):
            service.process_job(repo.jobs["job-validate"])

        assert repo.failed_jobs
        _, failure_summary = repo.failed_jobs[0]
        assert str(temp_root) not in failure_summary
        assert "validation failed" in failure_summary
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_internal_draft_upload_removes_staged_source_when_repository_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = SCRATCH_ROOT / f"publisher-{uuid4().hex}"
    temp_root.mkdir(parents=True, exist_ok=False)
    try:
        repo = FakeRepository()
        service = KnowledgePublisherService(
            repository=repo,
            staging_root=temp_root / "staging",
            artifact_root=temp_root / "artifacts",
            indexer=FakeIndexer(),
            chroma_client=object(),
            redis_client=object(),
        )
        package_path = temp_root / "knowledge.zip"
        package_path.write_bytes(b"zip-bytes")
        staged_root = temp_root / "staging" / "draft-123"

        def fake_stage(package: Path, *, draft_id: str, staging_root: Path) -> SimpleNamespace:
            assert package == package_path
            assert draft_id == "draft-123"
            assert staging_root == temp_root / "staging"
            staged_root.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(
                package_sha256="a" * 64,
                package_size_bytes=17,
                source_root=staged_root,
                validated_source=SimpleNamespace(
                    root=staged_root,
                    documents=(),
                    smoke_queries=(),
                    source_tree_sha256="b" * 64,
                    git_commit=None,
                ),
            )

        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.stage_zip_knowledge_package",
            fake_stage,
        )
        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.uuid4",
            lambda: SimpleNamespace(hex="draft-123"),
        )
        monkeypatch.setattr(
            repo,
            "create_draft_and_queue_validation",
            lambda **_: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        )

        with pytest.raises(RuntimeError, match="database unavailable"):
            service.ingest_draft(package_path, original_filename="knowledge.zip")

        assert not staged_root.exists()
        assert repo.created_drafts == []
        assert repo.queued_jobs == []
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_internal_draft_upload_streams_and_rejects_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = SCRATCH_ROOT / f"publisher-{uuid4().hex}"
    temp_root.mkdir(parents=True, exist_ok=False)
    try:
        reads: list[int] = []
        created_paths: list[Path] = []
        original_named_temporary_file = tempfile.NamedTemporaryFile
        (temp_root / "tmp").mkdir(parents=True, exist_ok=True)

        class FakeUploadStream:
            def __init__(self) -> None:
                self._chunks = [b"abcd", b"ef"]

            def read(self, size: int) -> bytes:
                reads.append(size)
                if self._chunks:
                    return self._chunks.pop(0)
                return b""

        class FakeUpload:
            filename = "knowledge.zip"
            file = FakeUploadStream()

        def fake_named_temporary_file(*args: object, **kwargs: object):
            kwargs = dict(kwargs)
            kwargs["dir"] = temp_root / "tmp"
            handle = original_named_temporary_file(*args, **kwargs)
            created_paths.append(Path(handle.name))
            return handle

        monkeypatch.setattr(worker_module, "MAX_INTERNAL_UPLOAD_BYTES", 5)
        monkeypatch.setattr(
            worker_module.tempfile,
            "NamedTemporaryFile",
            fake_named_temporary_file,
        )

        with pytest.raises(HTTPException, match="Uploaded package exceeds"):
            worker_module._persist_upload(FakeUpload(), temp_root / "staging")

        assert reads
        assert all(size == worker_module.INTERNAL_UPLOAD_CHUNK_BYTES for size in reads)
        assert created_paths and not created_paths[0].exists()
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_worker_lifespan_processes_jobs_and_stops_gracefully() -> None:
    class ConsumerService:
        def __init__(self) -> None:
            self.staging_root = Path("ignored")
            self.calls = ["validate_draft", "publish_draft", "rollback_release", "get_status"]
            self.processed: list[str] = []
            self.started = threading.Event()

        def process_next_job(self) -> dict[str, str] | None:
            self.started.set()
            if not self.calls:
                return None
            job_kind = self.calls.pop(0)
            self.processed.append(job_kind)
            return {"kind": job_kind}

    service = ConsumerService()
    app = create_app(
        service=service,
        internal_bearer_secret="publisher-secret",
        job_consumer_poll_interval_seconds=0.01,
    )

    with TestClient(app):
        assert service.started.wait(timeout=2)
        deadline = time.monotonic() + 2
        while len(service.processed) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.processed == [
            "validate_draft",
            "publish_draft",
            "rollback_release",
            "get_status",
        ]

    assert app.state.job_consumer_stop.is_set()
    assert app.state.job_consumer_task.done()


def test_fixed_jobs_validate_publish_rollback_and_get_status_run_in_python_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = SCRATCH_ROOT / f"publisher-{uuid4().hex}"
    temp_root.mkdir(parents=True, exist_ok=False)
    try:
        staging_root = temp_root / "staging"
        source_root = staging_root / "draft-123"
        _write_valid_source(source_root)

        repo = FakeRepository()
        repo.drafts["draft-123"] = SimpleNamespace(
            id="draft-123",
            status="validating",
            original_filename="knowledge.zip",
            package_sha256="a" * 64,
            package_size_bytes=17,
            storage_key="draft-123",
            actor_user_id="knowledge-publisher",
            actor_username="knowledge-publisher",
            validation_report=None,
        )
        repo.jobs["job-validate"] = SimpleNamespace(
            id="job-validate",
            kind="validate_draft",
            draft_id="draft-123",
            release_build_id=None,
            actor_user_id="knowledge-publisher",
            actor_username="knowledge-publisher",
        )
        repo.jobs["job-publish"] = SimpleNamespace(
            id="job-publish",
            kind="publish_draft",
            draft_id="draft-123",
            release_build_id=None,
            actor_user_id="knowledge-publisher",
            actor_username="knowledge-publisher",
        )
        repo.jobs["job-rollback"] = SimpleNamespace(
            id="job-rollback",
            kind="rollback_release",
            draft_id=None,
            release_build_id="build-old",
            actor_user_id="knowledge-publisher",
            actor_username="knowledge-publisher",
        )
        repo.jobs["job-status"] = SimpleNamespace(
            id="job-status",
            kind="get_status",
            draft_id="draft-123",
            release_build_id=None,
            actor_user_id="knowledge-publisher",
            actor_username="knowledge-publisher",
        )

        indexer = FakeIndexer()
        lock_events: list[str] = []
        restore_calls: list[tuple[str, object]] = []
        validate_calls: list[Path] = []

        class DummyPublicationLock:
            def __init__(self, *_: object) -> None:
                pass

            def __enter__(self) -> DummyPublicationLock:
                lock_events.append("enter")
                return self

            def __exit__(self, *_: object) -> None:
                lock_events.append("exit")

            def assert_held(self) -> None:
                lock_events.append("assert")

        from metro_agent.knowledge.source_validation import (
            validate_source_root as real_validate_source_root,
        )

        def wrapped_validate(root: Path):
            validate_calls.append(root)
            return real_validate_source_root(root)

        def fake_restore(
            client: object,
            artifact_root: Path,
            build_id: str,
            *,
            before_publish,
        ) -> SimpleNamespace:
            restore_calls.append((build_id, artifact_root))
            before_publish()
            return SimpleNamespace(current_build_id="build-restored")

        def fake_read_validated_release(artifact_root: Path, build_id: str) -> SimpleNamespace:
            assert artifact_root == temp_root / "artifacts"
            assert build_id == "build-1"
            return SimpleNamespace(
                build_id="build-1",
                collection_name="private-collection__build_build-1",
                sha256="d" * 64,
                source_tree_sha256="e" * 64,
            )

        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.PublicationLock",
            DummyPublicationLock,
        )
        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.restore_validated_release",
            fake_restore,
        )
        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.validate_source_root",
            wrapped_validate,
        )
        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.read_validated_release",
            fake_read_validated_release,
        )

        service = KnowledgePublisherService(
            repository=repo,
            staging_root=staging_root,
            artifact_root=temp_root / "artifacts",
            indexer=indexer,
            chroma_client=object(),
            redis_client=object(),
        )

        validate_result = service.process_job(repo.jobs["job-validate"])


        repo.drafts["draft-123"].status = "publishing"
        publish_result = service.process_job(repo.jobs["job-publish"])
        rollback_result = service.process_job(repo.jobs["job-rollback"])
        status_result = service.process_job(repo.jobs["job-status"])

        assert validate_result["status"] == "valid"
        assert validate_result["document_count"] == 1
        assert validate_result["source_tree_sha256"]
        assert len(validate_result["source_tree_sha256"]) == 64
        assert str(source_root) not in str(validate_result)
        assert repo.completed_jobs[0][0] == "job-validate"
        assert repo.drafts["draft-123"].status == "published"
        assert indexer.calls == [source_root]
        assert publish_result == {"status": "published", "build_id": "build-1"}
        assert len(repo.publish_completions) == 1
        publish_completion = repo.publish_completions[0]
        assert publish_completion["job_id"] == "job-publish"
        assert publish_completion["build_id"] == "build-1"
        assert publish_completion["collection_name"] == "private-collection__build_build-1"
        assert publish_completion["artifact_sha256"] == "d" * 64
        assert publish_completion["source_manifest_sha256"] == "e" * 64
        assert publish_completion["draft_id"] == "draft-123"
        assert publish_completion["document_count"] == 1
        assert publish_completion["validation_summary"] == {
            "status": "valid",
            "draft_id": "draft-123",
            "original_filename": "knowledge.zip",
            "package_sha256": "a" * 64,
            "package_size_bytes": 17,
            "document_count": 1,
            "smoke_query_count": 1,
            "source_tree_sha256": validate_result["source_tree_sha256"],
            "git_commit": None,
        }
        assert publish_completion["published_at"] is not None
        assert repo.published_releases[0]["build_id"] == "build-1"
        assert validate_calls == [source_root, source_root]
        assert restore_calls == [("build-old", temp_root / "artifacts")]
        assert lock_events == ["enter", "assert", "exit"]
        assert rollback_result == {
            "status": "rolled_back",
            "build_id": "build-restored",
        }
        assert repo.rolled_back_jobs == [("job-rollback", "build-restored")]
        assert repo.completed_jobs[-1][0] == "job-status"
        assert status_result["status"] == "observed"
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
