from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from metro_agent.api import create_app
from metro_agent.auth.models import AuthBase
from metro_agent.auth.service import AuthService
from metro_agent.knowledge_admin.models import Base as KnowledgeAdminBase
from metro_agent.knowledge_admin.repository import KnowledgeAdminRepository
from metro_agent.knowledge_admin.service import (
    KnowledgeAdminService,
    KnowledgePublisherService,
)

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "deploy" / "operations"
OLD_BUILD_ID = "build-old"
NEW_BUILD_ID = "build-new"


def _knowledge_package() -> bytes:
    metadata = (
        "---\n"
        "owner: Metro Operations\n"
        "source: Acceptance fixture\n"
        "updated: 2026-09-08\n"
        "effective_date: 2026-09-08\n"
        "expires_at: 2099-12-31\n"
        "risk_level: general\n"
        "---\n"
        "The governed acceptance source.\n"
    )
    package = BytesIO()
    with ZipFile(package, "w", ZIP_DEFLATED) as archive:
        archive.writestr("rules.md", metadata)
        archive.writestr(
            "release-smoke-queries.jsonl",
            '{"query":"acceptance","expected_source":"rules.md","minimum_matches":1}\n',
        )
    return package.getvalue()


class _FakePublishedQuery:
    """Deterministic stand-in for the published-only knowledge query path."""

    def __init__(self, build_id: str) -> None:
        self.build_id = build_id

    def query(self) -> dict[str, str]:
        return {"build_id": self.build_id, "source": "rules.md"}


class _FakeControlledIndexer:
    """A no-network indexer that moves the visible pointer only on success."""

    def __init__(self, query: _FakePublishedQuery) -> None:
        self.query = query
        self.calls: list[Path] = []
        self.fail_publish = False

    def build_and_publish_from_staged_source(self, source_root: Path) -> dict[str, str]:
        self.calls.append(source_root)
        if self.fail_publish:
            raise RuntimeError("deterministic publish failure")
        self.query.build_id = NEW_BUILD_ID
        return {"status": "published", "build_id": NEW_BUILD_ID}


class _InProcessPublisherGateway(KnowledgeAdminService):
    """Exercise the web boundary while keeping the controlled worker in-process."""

    def __init__(
        self,
        *,
        repository: KnowledgeAdminRepository,
        publisher: KnowledgePublisherService,
        upload_root: Path,
    ) -> None:
        super().__init__(repository=repository)
        self.publisher = publisher
        self.upload_root = upload_root
        self.forwarded_actors: list[tuple[str, str]] = []

    async def upload_draft(self, *, package, current_user=None) -> dict[str, object]:
        self.upload_root.mkdir(parents=True, exist_ok=True)
        package_path = self.upload_root / "incoming.zip"
        package_path.write_bytes(package.file.read())
        try:
            self.forwarded_actors.append(
                (str(current_user.id), current_user.username)
            )
            return self.publisher.ingest_draft(
                package_path,
                original_filename=package.filename or "knowledge.zip",
                actor_user_id=str(current_user.id),
                actor_username=current_user.username,
            )
        finally:
            package_path.unlink(missing_ok=True)


class _NoNetworkPublicationLock:
    def __init__(self, *_args: object) -> None:
        self.assertions = 0

    def __enter__(self) -> _NoNetworkPublicationLock:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def assert_held(self) -> None:
        self.assertions += 1


@pytest.fixture
def governed_web_knowledge(monkeypatch: pytest.MonkeyPatch):
    """A public-admin API plus real worker/repository, without Docker or live services."""
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "3600")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    scratch = ROOT / f".knowledge-e2e-contract-{uuid4().hex}"
    scratch.mkdir()
    try:
        AuthBase.metadata.create_all(engine)
        KnowledgeAdminBase.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        auth = AuthService(factory, session_pepper="governed-web-knowledge-test")
        admin = auth.create_user("knowledge.admin", "CorrectHorseBattery1", "admin", None)
        user = auth.create_user("knowledge.user", "CorrectHorseBattery2", "user", admin.id)
        repository = KnowledgeAdminRepository(factory)
        query = _FakePublishedQuery(OLD_BUILD_ID)
        indexer = _FakeControlledIndexer(query)

        def read_validated_release(_root: Path, build_id: str) -> SimpleNamespace:
            assert build_id == NEW_BUILD_ID
            return SimpleNamespace(
                build_id=build_id,
                collection_name=f"private-collection__build_{build_id}",
                sha256="a" * 64,
                source_tree_sha256="b" * 64,
            )

        def restore_validated_release(
            _client: object,
            _root: Path,
            build_id: str,
            *,
            before_publish,
        ) -> SimpleNamespace:
            before_publish()
            query.build_id = build_id
            return SimpleNamespace(current_build_id=build_id)

        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.read_validated_release",
            read_validated_release,
        )
        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.restore_validated_release",
            restore_validated_release,
        )
        monkeypatch.setattr(
            "metro_agent.knowledge_admin.service.PublicationLock",
            _NoNetworkPublicationLock,
        )
        publisher = KnowledgePublisherService(
            repository=repository,
            staging_root=scratch / "staging",
            artifact_root=scratch / "artifacts",
            indexer=indexer,
            chroma_client=object(),
            redis_client=object(),
        )
        service = _InProcessPublisherGateway(
            repository=repository,
            publisher=publisher,
            upload_root=scratch / "incoming",
        )
        app = create_app(
            graph_factory=lambda: object(),
            history_service_factory=lambda: SimpleNamespace(),
            monitoring_service_factory=lambda: SimpleNamespace(),
            auth_service_factory=lambda: auth,
            knowledge_admin_service_factory=lambda: service,
            chat_runner=lambda *_args, **_kwargs: "answer",
            knowledge_preflight=lambda: None,
        )
        with TestClient(app, base_url="http://testserver") as client:
            yield SimpleNamespace(
                client=client,
                factory=factory,
                repository=repository,
                publisher=publisher,
                service=service,
                indexer=indexer,
                query=query,
                admin=admin,
                user=user,
            )
    finally:
        engine.dispose()
        shutil.rmtree(scratch, ignore_errors=True)


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200


def _seed_old_release(governed_web_knowledge) -> None:
    draft = governed_web_knowledge.repository.create_draft(
        original_filename="old-release.zip",
        package_sha256="c" * 64,
        package_size_bytes=1,
        storage_key="old-release",
        actor_user_id=str(governed_web_knowledge.admin.id),
        actor_username=governed_web_knowledge.admin.username,
    )
    governed_web_knowledge.repository.record_release(
        build_id=OLD_BUILD_ID,
        collection_name="private-collection__build_old",
        artifact_sha256="d" * 64,
        source_manifest_sha256="e" * 64,
        draft_id=draft.id,
        document_count=1,
        validation_summary={"status": "valid"},
        published_at=datetime(2026, 9, 8, tzinfo=UTC),
    )


def _upload_valid_draft(governed_web_knowledge) -> tuple[str, str]:
    response = governed_web_knowledge.client.post(
        "/api/admin/knowledge/drafts",
        files={"package": ("knowledge.zip", _knowledge_package(), "application/zip")},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "queued"
    return response.json()["draft_id"], response.json()["job_id"]


def test_non_admin_is_denied_governed_web_knowledge_actions(
    governed_web_knowledge,
) -> None:
    _seed_old_release(governed_web_knowledge)
    _login(
        governed_web_knowledge.client,
        "knowledge.user",
        "CorrectHorseBattery2",
    )

    requests = (
        ("GET", "/api/admin/knowledge/drafts", {}),
        ("GET", "/api/admin/knowledge/releases", {}),
        (
            "POST",
            "/api/admin/knowledge/drafts",
            {"files": {"package": ("knowledge.zip", _knowledge_package(), "application/zip")}},
        ),
        (
            "POST",
            "/api/admin/knowledge/drafts/11111111-1111-4111-8111-111111111111/publish",
            {},
        ),
        (
            "POST",
            f"/api/admin/knowledge/releases/{OLD_BUILD_ID}/rollback",
            {"json": {"reason": "not an administrator"}},
        ),
    )
    for method, path, kwargs in requests:
        response = governed_web_knowledge.client.request(method, path, **kwargs)
        assert response.status_code == 403

    assert governed_web_knowledge.indexer.calls == []
    assert governed_web_knowledge.query.query()["build_id"] == OLD_BUILD_ID


def test_invalid_staged_upload_validation_preserves_old_published_release(
    governed_web_knowledge,
) -> None:
    _seed_old_release(governed_web_knowledge)
    _login(
        governed_web_knowledge.client,
        "knowledge.admin",
        "CorrectHorseBattery1",
    )
    draft_id, validation_job_id = _upload_valid_draft(governed_web_knowledge)
    draft = governed_web_knowledge.repository.get_draft(draft_id)
    staged_rules = governed_web_knowledge.publisher.staging_root / draft.storage_key / "rules.md"
    staged_rules.write_text("---\nowner: missing required metadata\n---\n", encoding="utf-8")

    with pytest.raises(ValueError):
        governed_web_knowledge.publisher.process_next_job()

    job = governed_web_knowledge.client.get(
        f"/api/admin/knowledge/jobs/{validation_job_id}"
    )
    refreshed_draft = governed_web_knowledge.client.get(
        f"/api/admin/knowledge/drafts/{draft_id}"
    )
    releases = governed_web_knowledge.client.get("/api/admin/knowledge/releases")

    assert job.status_code == 200
    assert job.json()["status"] == "failed"
    assert refreshed_draft.status_code == 200
    assert refreshed_draft.json()["status"] == "validation_failed"
    assert releases.status_code == 200
    assert releases.json()[0]["build_id"] == OLD_BUILD_ID
    assert releases.json()[0]["status"] == "current"
    assert governed_web_knowledge.indexer.calls == []
    assert governed_web_knowledge.query.query()["build_id"] == OLD_BUILD_ID


def test_admin_uploader_self_publishes_only_through_controlled_job(
    governed_web_knowledge,
) -> None:
    _seed_old_release(governed_web_knowledge)
    _login(
        governed_web_knowledge.client,
        "knowledge.admin",
        "CorrectHorseBattery1",
    )
    draft_id, _ = _upload_valid_draft(governed_web_knowledge)

    validation = governed_web_knowledge.publisher.process_next_job()
    publish = governed_web_knowledge.client.post(
        f"/api/admin/knowledge/drafts/{draft_id}/publish"
    )

    assert validation is not None
    assert validation["status"] == "valid"
    assert publish.status_code == 200
    assert publish.json()["kind"] == "publish_draft"
    assert governed_web_knowledge.indexer.calls == []

    completed_publish = governed_web_knowledge.publisher.process_next_job()
    releases = governed_web_knowledge.client.get("/api/admin/knowledge/releases")
    audits = governed_web_knowledge.client.get("/api/admin/knowledge/audits?limit=50")

    assert completed_publish == {"status": "published", "build_id": NEW_BUILD_ID}
    assert len(governed_web_knowledge.indexer.calls) == 1
    assert governed_web_knowledge.service.forwarded_actors == [
        (str(governed_web_knowledge.admin.id), "knowledge.admin")
    ]
    assert releases.status_code == 200
    by_build_id = {release["build_id"]: release for release in releases.json()}
    assert by_build_id[NEW_BUILD_ID]["status"] == "current"
    assert by_build_id[OLD_BUILD_ID]["status"] == "superseded"
    assert governed_web_knowledge.query.query()["build_id"] == NEW_BUILD_ID
    assert any(
        event["action"] == "publish_draft"
        and event["result"] == "succeeded"
        and event["actor_username"] == "knowledge.admin"
        for event in audits.json()
    )


def test_publish_failure_preserves_old_release_and_published_query_path(
    governed_web_knowledge,
) -> None:
    _seed_old_release(governed_web_knowledge)
    _login(
        governed_web_knowledge.client,
        "knowledge.admin",
        "CorrectHorseBattery1",
    )
    draft_id, _ = _upload_valid_draft(governed_web_knowledge)
    governed_web_knowledge.publisher.process_next_job()
    publish = governed_web_knowledge.client.post(
        f"/api/admin/knowledge/drafts/{draft_id}/publish"
    )
    governed_web_knowledge.indexer.fail_publish = True

    with pytest.raises(RuntimeError, match="deterministic publish failure"):
        governed_web_knowledge.publisher.process_next_job()

    job = governed_web_knowledge.client.get(
        f"/api/admin/knowledge/jobs/{publish.json()['id']}"
    )
    draft = governed_web_knowledge.client.get(f"/api/admin/knowledge/drafts/{draft_id}")
    releases = governed_web_knowledge.client.get("/api/admin/knowledge/releases")

    assert job.status_code == 200
    assert job.json()["status"] == "failed"
    assert draft.status_code == 200
    assert draft.json()["status"] == "publish_failed"
    assert [release["build_id"] for release in releases.json()] == [OLD_BUILD_ID]
    assert releases.json()[0]["status"] == "current"
    assert governed_web_knowledge.query.query()["build_id"] == OLD_BUILD_ID


def test_admin_rollback_restores_release_and_leaves_audit_evidence(
    governed_web_knowledge,
) -> None:
    _seed_old_release(governed_web_knowledge)
    _login(
        governed_web_knowledge.client,
        "knowledge.admin",
        "CorrectHorseBattery1",
    )
    draft_id, _ = _upload_valid_draft(governed_web_knowledge)
    governed_web_knowledge.publisher.process_next_job()
    governed_web_knowledge.client.post(f"/api/admin/knowledge/drafts/{draft_id}/publish")
    governed_web_knowledge.publisher.process_next_job()

    rollback = governed_web_knowledge.client.post(
        f"/api/admin/knowledge/releases/{OLD_BUILD_ID}/rollback",
        json={"reason": "restore the known good release"},
    )
    completed_rollback = governed_web_knowledge.publisher.process_next_job()
    releases = governed_web_knowledge.client.get("/api/admin/knowledge/releases")
    audits = governed_web_knowledge.client.get("/api/admin/knowledge/audits?limit=50")

    assert rollback.status_code == 200
    assert rollback.json()["kind"] == "rollback_release"
    assert completed_rollback == {"status": "rolled_back", "build_id": OLD_BUILD_ID}
    by_build_id = {release["build_id"]: release for release in releases.json()}
    assert by_build_id[OLD_BUILD_ID]["status"] == "current"
    assert by_build_id[NEW_BUILD_ID]["status"] == "rolled_back"
    assert governed_web_knowledge.query.query()["build_id"] == OLD_BUILD_ID
    rollback_events = [
        event
        for event in audits.json()
        if event["action"] == "rollback_release"
        and event["release_build_id"] == OLD_BUILD_ID
    ]
    assert {event["result"] for event in rollback_events} == {
        "queued",
        "running",
        "succeeded",
    }
    assert any(
        event["reason_summary"] == "restore the known good release"
        and event["actor_username"] == "knowledge.admin"
        for event in rollback_events
    )


def _compose() -> dict[str, object]:
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def test_backup_archives_complete_knowledge_release_unit_under_publication_lock() -> None:
    backup = (OPERATIONS / "backup-all.sh").read_text(encoding="utf-8")
    lock_helper = OPERATIONS / "with-knowledge-publication-lock.py"
    compose = _compose()

    assert lock_helper.exists()
    assert "knowledge-chroma.tar.gz" in backup
    assert "knowledge-artifacts.tar.gz" in backup
    assert "knowledge:publication" in lock_helper.read_text(encoding="utf-8")
    assert "PublicationLock" in lock_helper.read_text(encoding="utf-8")
    assert "docker compose stop app knowledge-read-proxy chroma" in backup
    assert "knowledge-indexer" in backup
    # The backup container sees the Chroma volume root at /chroma; the running
    # Chroma service mounts that same root at /chroma/chroma.  Archive the
    # former as the latter so restore extracts directly into the live mount.
    assert compose["services"]["knowledge-backup"]["volumes"] == [
        "knowledge_chroma_data:/chroma:ro",
        "knowledge_artifact_data:/var/lib/metro-agent/knowledge-artifacts:ro",
    ]
    assert "a.add('/chroma',arcname='chroma')" in backup
    assert "safe-restore-tar.py replace-volume-contents /backup/knowledge-chroma.tar.gz" in (
        OPERATIONS / "restore-all.sh"
    ).read_text(encoding="utf-8")


def test_backup_holds_one_publication_token_before_stopping_knowledge_services() -> None:
    backup = (OPERATIONS / "backup-all.sh").read_text(encoding="utf-8")
    lock_helper = (OPERATIONS / "with-knowledge-publication-lock.py").read_text(
        encoding="utf-8"
    )

    assert " hold " in backup
    assert "verify-token" in backup
    assert backup.index("hold") < backup.index("knowledge-indexer")
    assert backup.index("hold") < backup.index("docker compose stop app knowledge-read-proxy chroma")
    assert backup.index("knowledge-chroma.tar.gz") < backup.index("SHA256SUMS")
    assert "trap release_publication_lock EXIT HUP INT TERM" in backup
    assert ": > \"$LOCK_RELEASE_FILE\"" in backup
    assert "def hold_lock" in lock_helper
    assert "def verify_token" in lock_helper
    assert "PublicationLock" in lock_helper
    # A process boundary cannot trust an ordinary GET: it must atomically prove
    # token ownership while renewing the same bounded publication lease.
    assert "RENEW_SCRIPT" in lock_helper
    assert "_redis_client().eval(" in lock_helper
    assert "PUBLICATION_LOCK_KEY," in lock_helper
    assert "MAX_TTL_SECONDS * 1000" in lock_helper
    # The lock may expire while checksums are being written, so prove ownership
    # again after the checksum manifest is complete.
    checksum = backup.index("SHA256SUMS")
    assert "verify_publication_lock" in backup[checksum:]
    # docker wait prints the holder's exit status; suppressing that output loses
    # a non-zero holder result and is therefore not a valid release check.
    assert 'lock_holder_status=$(docker wait "$LOCK_CONTAINER")' in backup
    assert '"${lock_holder_status:-1}" != "0"' in backup


def test_restore_requires_complete_knowledge_release_and_verifies_it_before_app() -> None:
    restore = (OPERATIONS / "restore-all.sh").read_text(encoding="utf-8")

    assert "knowledge-chroma.tar.gz" in restore
    assert "knowledge-artifacts.tar.gz" in restore
    assert "verify-restored-release" in restore
    assert restore.index("verify-restored-release") < restore.index("docker compose up -d --no-build app")


def test_restore_validates_archives_and_swaps_the_knowledge_volume_before_starting_proxy() -> None:
    restore = (OPERATIONS / "restore-all.sh").read_text(encoding="utf-8")
    helper = (OPERATIONS / "safe-restore-tar.py").read_text(encoding="utf-8")

    assert "safe-restore-tar.py" in restore
    assert "--expected-root chroma" in restore
    assert "--expected-root knowledge-artifacts" in restore
    assert "replace-volume-contents" in restore
    assert "merge-artifacts" in restore
    # Archive members are fully inspected before any extraction; never rely on
    # tar's default path/link behavior for an untrusted backup medium.
    for marker in ("is_absolute", "..", "issym", "islnk", "isdev", "extractall"):
        assert marker in helper
    assert "os.replace" in helper
    assert "rollback.previous" in helper
    assert "rmtree(destination" not in helper
    # Successful release verification gates proxy startup; app starts only after
    # that proxy is up and its heartbeat has answered.
    verify = restore.index("verify-restored-release")
    proxy = restore.index("docker compose up -d --wait knowledge-read-proxy")
    app = restore.index("docker compose up -d --no-build app")
    assert verify < proxy < app
    assert "docker compose exec -T knowledge-read-proxy" in restore
    assert "http://127.0.0.1:8000/api/v2/heartbeat" in restore


def test_e2e_profile_uses_only_deterministic_embedding_and_fixture() -> None:
    compose = _compose()
    service = compose["services"]["knowledge-e2e"]

    assert service["profiles"] == ["knowledge-e2e"]
    assert service["environment"]["KNOWLEDGE_E2E"] == "1"
    assert "DEEPSEEK_API_KEY" not in service["environment"]
    assert "GLM_API_KEY" not in service["environment"]
    assert "DASHSCOPE_API_KEY" not in service["environment"]
    assert "deterministic" in service["environment"]["KNOWLEDGE_EMBEDDER"].lower()
    assert any("fixtures/knowledge-e2e" in value and value.endswith(":ro") for value in service["volumes"])


def test_e2e_fixture_is_a_governed_versioned_knowledge_source() -> None:
    rules = ROOT / "fixtures" / "knowledge-e2e" / "rules.md"
    smoke = ROOT / "fixtures" / "knowledge-e2e" / "release-smoke-queries.jsonl"

    assert rules.exists() and smoke.exists()
    text = rules.read_text(encoding="utf-8")
    for field in ("owner:", "source:", "updated:", "effective_date:", "expires_at:", "risk_level:"):
        assert field in text
    assert '"expected_source":"rules.md"' in smoke.read_text(encoding="utf-8")


def test_compose_keeps_knowledge_chroma_unpublished_and_artifacts_retained() -> None:
    compose = _compose()
    services = compose["services"]
    assert "ports" not in services["chroma"]
    assert "knowledge_artifact_data" in compose["volumes"]

    documentation = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "README.md",
            "docs/operations/coordinated-backup-restore.md",
            "docs/operations/internal-pilot-auth-acceptance.md",
        )
    )
    assert "不自动清理" in documentation
    assert "加密由备份目标负责" in documentation
