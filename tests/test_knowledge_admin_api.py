from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from metro_agent.api import create_app
from metro_agent.auth.dependencies import CurrentUser
from metro_agent.auth.models import AuthBase
from metro_agent.auth.service import AuthService
from metro_agent.knowledge_admin.models import (
    Base as KnowledgeAdminBase,
    KnowledgeAdminAuditEvent,
)
from metro_agent.knowledge_admin.repository import KnowledgeAdminRepository
from metro_agent.knowledge_admin.service import (
    KnowledgeAdminInvalidStateError,
    KnowledgeAdminMalformedRequestError,
    KnowledgeAdminNotFoundError,
    KnowledgeAdminService,
    KnowledgeAdminWorkerUnavailableError,
)


NOW = datetime(2026, 9, 8, tzinfo=UTC)
DRAFT_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
RELEASE_BUILD_ID = "build-20260908"


class FakeKnowledgeAdminService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.raise_for: dict[str, Exception] = {}
        self.upload_response = {
            "draft_id": DRAFT_ID,
            "job_id": JOB_ID,
            "status": "queued",
        }
        self.drafts = [
            SimpleNamespace(
                id=DRAFT_ID,
                status="uploaded",
                original_filename="knowledge.zip",
                package_sha256="a" * 64,
                package_size_bytes=42,
                actor_username="metro.admin",
                created_at=NOW,
                updated_at=NOW,
                storage_key="drafts/private/hidden/knowledge.zip",
            )
        ]
        self.draft_detail = SimpleNamespace(
            **self.drafts[0].__dict__,
            validation_report={"valid": True},
        )
        self.jobs = [
            SimpleNamespace(
                id=JOB_ID,
                kind="validate_draft",
                status="queued",
                draft_id=DRAFT_ID,
                release_build_id=None,
                failure_summary=None,
                queued_at=NOW,
                started_at=None,
                finished_at=None,
                lease_expires_at=None,
            )
        ]
        self.releases = [
            SimpleNamespace(
                build_id=RELEASE_BUILD_ID,
                document_count=3,
                status="current",
                published_at=NOW,
                collection_name="private-collection-hidden",
                artifact_sha256="b" * 64,
                source_manifest_sha256="c" * 64,
                draft_id=DRAFT_ID,
                validation_summary={"valid": True},
            )
        ]
        self.audit_events = [
            SimpleNamespace(
                id="33333333-3333-4333-8333-333333333333",
                action="rollback_release",
                result="queued",
                actor_user_id="7",
                actor_username="metro.admin",
                draft_id=None,
                job_id=JOB_ID,
                release_build_id=RELEASE_BUILD_ID,
                reason_summary="restore validated release",
                failure_summary=None,
                occurred_at=NOW,
            )
        ]

    async def upload_draft(self, *, package, current_user=None) -> dict[str, object]:
        self.calls.append(
            (
                "upload_draft",
                {
                    "filename": package.filename,
                    "cookie": getattr(package, "cookie", None),
                    "actor_user_id": getattr(current_user, "id", None),
                    "actor_username": getattr(current_user, "username", None),
                },
            )
        )
        if exc := self.raise_for.get("upload_draft"):
            raise exc
        return dict(self.upload_response)

    def list_drafts(self) -> list[SimpleNamespace]:
        self.calls.append(("list_drafts", None))
        return list(self.drafts)

    def get_draft(self, draft_id) -> SimpleNamespace:
        self.calls.append(("get_draft", str(draft_id)))
        if exc := self.raise_for.get("get_draft"):
            raise exc
        return SimpleNamespace(**self.draft_detail.__dict__)

    def queue_validation(self, draft_id, *, current_user) -> SimpleNamespace:
        self.calls.append(
            (
                "queue_validation",
                {
                    "draft_id": str(draft_id),
                    "actor_user_id": current_user.id,
                    "actor_username": current_user.username,
                },
            )
        )
        if exc := self.raise_for.get("queue_validation"):
            raise exc
        return SimpleNamespace(**self.jobs[0].__dict__)

    def queue_publish(self, draft_id, *, current_user) -> SimpleNamespace:
        self.calls.append(
            (
                "queue_publish",
                {
                    "draft_id": str(draft_id),
                    "actor_user_id": current_user.id,
                    "actor_username": current_user.username,
                },
            )
        )
        if exc := self.raise_for.get("queue_publish"):
            raise exc
        return SimpleNamespace(**self.jobs[0].__dict__)

    def list_releases(self) -> list[SimpleNamespace]:
        self.calls.append(("list_releases", None))
        return list(self.releases)

    def list_audit_events(self, *, limit: int = 50) -> list[SimpleNamespace]:
        self.calls.append(("list_audit_events", limit))
        return list(self.audit_events[:limit])

    def queue_rollback(
        self, release_build_id: str, *, reason: str, current_user
    ) -> SimpleNamespace:
        self.calls.append(
            (
                "queue_rollback",
                {
                    "release_build_id": release_build_id,
                    "reason": reason,
                    "actor_user_id": current_user.id,
                    "actor_username": current_user.username,
                },
            )
        )
        if exc := self.raise_for.get("queue_rollback"):
            raise exc
        return SimpleNamespace(**self.jobs[0].__dict__)

    def get_job(self, job_id) -> SimpleNamespace:
        self.calls.append(("get_job", str(job_id)))
        if exc := self.raise_for.get("get_job"):
            raise exc
        return SimpleNamespace(**self.jobs[0].__dict__)


@pytest.fixture
def admin_api(monkeypatch: pytest.MonkeyPatch):
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

    AuthBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    auth_service = AuthService(factory, session_pepper="knowledge-admin-test-pepper")
    admin = auth_service.create_user(
        "metro.admin", "CorrectHorseBattery1", "admin", None
    )
    user = auth_service.create_user(
        "operator", "CorrectHorseBattery2", "user", admin.id
    )
    knowledge_service = FakeKnowledgeAdminService()
    factory_calls = 0

    def knowledge_service_factory() -> FakeKnowledgeAdminService:
        nonlocal factory_calls
        factory_calls += 1
        return knowledge_service

    app = create_app(
        graph_factory=lambda: object(),
        history_service_factory=lambda: SimpleNamespace(),
        monitoring_service_factory=lambda: SimpleNamespace(),
        auth_service_factory=lambda: auth_service,
        knowledge_admin_service_factory=knowledge_service_factory,
        chat_runner=lambda *_args, **_kwargs: "answer",
        knowledge_preflight=lambda: None,
    )
    with TestClient(app, base_url="http://testserver") as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            service=auth_service,
            knowledge_service=knowledge_service,
            admin=admin,
            user=user,
            factory_calls=factory_calls,
        )
    engine.dispose()


def login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def test_create_app_injects_knowledge_admin_service_once(admin_api) -> None:
    assert admin_api.factory_calls == 1
    assert admin_api.app.state.knowledge_admin_service is admin_api.knowledge_service


@pytest.mark.parametrize(
    "method,path,body,files",
    [
        ("GET", "/api/admin/knowledge/drafts", None, None),
        ("GET", f"/api/admin/knowledge/drafts/{DRAFT_ID}", None, None),
        ("GET", "/api/admin/knowledge/releases", None, None),
        ("GET", "/api/admin/knowledge/audits", None, None),
        ("GET", f"/api/admin/knowledge/jobs/{JOB_ID}", None, None),
        ("POST", f"/api/admin/knowledge/drafts/{DRAFT_ID}/validate", {}, None),
        ("POST", f"/api/admin/knowledge/drafts/{DRAFT_ID}/publish", {}, None),
        ("POST", f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback", {"reason": "because"}, None),
    ],
)
def test_knowledge_admin_routes_require_authentication(
    admin_api, method: str, path: str, body, files
) -> None:
    response = admin_api.client.request(method, path, json=body, files=files)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "method,path,body,files",
    [
        ("GET", "/api/admin/knowledge/drafts", None, None),
        ("GET", f"/api/admin/knowledge/drafts/{DRAFT_ID}", None, None),
        ("GET", "/api/admin/knowledge/releases", None, None),
        ("GET", "/api/admin/knowledge/audits", None, None),
        ("GET", f"/api/admin/knowledge/jobs/{JOB_ID}", None, None),
        ("POST", f"/api/admin/knowledge/drafts/{DRAFT_ID}/validate", {}, None),
        ("POST", f"/api/admin/knowledge/drafts/{DRAFT_ID}/publish", {}, None),
        ("POST", f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback", {"reason": "because"}, None),
        ("POST", "/api/admin/knowledge/drafts", None, {"package": ("knowledge.zip", b"zip", "application/zip")}),
    ],
)
def test_regular_user_cannot_access_knowledge_admin_routes(
    admin_api, method: str, path: str, body, files
) -> None:
    assert (
        login(admin_api.client, "operator", "CorrectHorseBattery2").status_code == 200
    )

    response = admin_api.client.request(
        method,
        path,
        json=body,
        files=files,
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "method,path,body,files",
    [
        ("POST", "/api/admin/knowledge/drafts", None, {"package": ("knowledge.zip", b"zip", "application/zip")}),
        ("POST", f"/api/admin/knowledge/drafts/{DRAFT_ID}/validate", {}, None),
        ("POST", f"/api/admin/knowledge/drafts/{DRAFT_ID}/publish", {}, None),
        ("POST", f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback", {"reason": "because"}, None),
    ],
)
def test_knowledge_admin_mutations_reject_cross_origin_requests(
    admin_api, method: str, path: str, body, files
) -> None:
    assert (
        login(admin_api.client, "metro.admin", "CorrectHorseBattery1").status_code
        == 200
    )

    response = admin_api.client.request(
        method,
        path,
        headers={"Origin": "https://attacker.example"},
        json=body,
        files=files,
    )

    assert response.status_code == 403
    assert admin_api.knowledge_service.calls == []


@pytest.mark.parametrize(
    "path,body,field_name",
    [
        ("/api/admin/knowledge/drafts", {"operator": "mallory"}, "operator"),
        (f"/api/admin/knowledge/drafts/{DRAFT_ID}/validate", {"command": "run"}, "command"),
        (f"/api/admin/knowledge/drafts/{DRAFT_ID}/publish", {"collection": "private"}, "collection"),
        (
            f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback",
            {"reason": "ok", "force": True},
            "force",
        ),
    ],
)
def test_knowledge_admin_routes_reject_forbidden_fields(
    admin_api, path: str, body: dict[str, object], field_name: str
) -> None:
    assert (
        login(admin_api.client, "metro.admin", "CorrectHorseBattery1").status_code
        == 200
    )

    if path.endswith("/drafts"):
        response = admin_api.client.post(
            path,
            data=body,
            files={"package": ("knowledge.zip", b"zip", "application/zip")},
        )
    else:
        response = admin_api.client.post(path, json=body)

    assert response.status_code == 422
    assert field_name in response.text


def test_knowledge_admin_routes_use_current_user_and_hide_private_fields(admin_api) -> None:
    assert (
        login(admin_api.client, "metro.admin", "CorrectHorseBattery1").status_code
        == 200
    )

    upload = admin_api.client.post(
        "/api/admin/knowledge/drafts",
        files={"package": ("knowledge.zip", b"zip", "application/zip")},
    )
    drafts = admin_api.client.get("/api/admin/knowledge/drafts")
    draft_detail = admin_api.client.get(f"/api/admin/knowledge/drafts/{DRAFT_ID}")
    validate = admin_api.client.post(f"/api/admin/knowledge/drafts/{DRAFT_ID}/validate")
    publish = admin_api.client.post(f"/api/admin/knowledge/drafts/{DRAFT_ID}/publish")
    releases = admin_api.client.get("/api/admin/knowledge/releases")
    audits = admin_api.client.get("/api/admin/knowledge/audits?limit=1")
    rollback = admin_api.client.post(
        f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback",
        json={"reason": "published the wrong draft"},
    )
    jobs = admin_api.client.get(f"/api/admin/knowledge/jobs/{JOB_ID}")

    assert upload.status_code == 201
    assert upload.json() == {
        "draft_id": DRAFT_ID,
        "job_id": JOB_ID,
        "status": "queued",
    }
    assert drafts.status_code == 200
    assert "storage_key" not in drafts.text
    assert draft_detail.status_code == 200
    assert "storage_key" not in draft_detail.text
    assert validate.status_code == 200
    assert validate.json()["kind"] == "validate_draft"
    assert validate.json()["status"] == "queued"
    assert publish.status_code == 200
    assert publish.json()["kind"] == "validate_draft"
    assert releases.status_code == 200
    assert "collection_name" not in releases.text
    assert releases.json()[0]["artifact_sha256"] == "b" * 64
    assert releases.json()[0]["source_manifest_sha256"] == "c" * 64
    assert releases.json()[0]["validation_summary"] == {"valid": True}
    assert audits.status_code == 200
    audit_row = audits.json()[0]
    assert audit_row["action"] == "rollback_release"
    assert audit_row["result"] == "queued"
    assert audit_row["actor_user_id"] == "7"
    assert audit_row["actor_username"] == "metro.admin"
    assert audit_row["draft_id"] is None
    assert audit_row["job_id"] == JOB_ID
    assert audit_row["release_build_id"] == RELEASE_BUILD_ID
    assert audit_row["reason_summary"] == "restore validated release"
    assert audit_row["failure_summary"] is None
    assert rollback.status_code == 200
    assert rollback.json()["kind"] == "validate_draft"
    assert jobs.status_code == 200
    assert "lease_expires_at" in jobs.text

    assert admin_api.knowledge_service.calls == [
        (
            "upload_draft",
            {
                "filename": "knowledge.zip",
                "cookie": None,
                "actor_user_id": admin_api.admin.id,
                "actor_username": admin_api.admin.username,
            },
        ),
        ("list_drafts", None),
        ("get_draft", DRAFT_ID),
        (
            "queue_validation",
            {
                "draft_id": DRAFT_ID,
                "actor_user_id": admin_api.admin.id,
                "actor_username": admin_api.admin.username,
            },
        ),
        (
            "queue_publish",
            {
                "draft_id": DRAFT_ID,
                "actor_user_id": admin_api.admin.id,
                "actor_username": admin_api.admin.username,
            },
        ),
        ("list_releases", None),
        ("list_audit_events", 1),
        (
            "queue_rollback",
            {
                "release_build_id": RELEASE_BUILD_ID,
                "reason": "published the wrong draft",
                "actor_user_id": admin_api.admin.id,
                "actor_username": admin_api.admin.username,
            },
        ),
        ("get_job", JOB_ID),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/knowledge/drafts/not-a-uuid",
        "/api/admin/knowledge/drafts/not-a-uuid/validate",
        "/api/admin/knowledge/drafts/not-a-uuid/publish",
        "/api/admin/knowledge/jobs/not-a-uuid",
    ],
)
def test_knowledge_admin_routes_reject_invalid_uuid_path_params(
    admin_api, path: str
) -> None:
    assert (
        login(admin_api.client, "metro.admin", "CorrectHorseBattery1").status_code
        == 200
    )

    response = (
        admin_api.client.get(path)
        if path.endswith("/jobs/not-a-uuid") or path.endswith("/drafts/not-a-uuid")
        else admin_api.client.post(path)
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "method,path,exception,status",
    [
        ("GET", f"/api/admin/knowledge/drafts/{DRAFT_ID}", KnowledgeAdminNotFoundError("missing draft"), 404),
        ("GET", f"/api/admin/knowledge/jobs/{JOB_ID}", KnowledgeAdminNotFoundError("missing job"), 404),
        ("POST", f"/api/admin/knowledge/drafts/{DRAFT_ID}/publish", KnowledgeAdminInvalidStateError("published draft cannot be published"), 409),
        ("POST", "/api/admin/knowledge/drafts", KnowledgeAdminWorkerUnavailableError("publisher unavailable"), 503),
        ("POST", f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback", KnowledgeAdminMalformedRequestError("bad reason"), 422),
    ],
)
def test_knowledge_admin_status_mappings(
    admin_api, method: str, path: str, exception: Exception, status: int
) -> None:
    assert (
        login(admin_api.client, "metro.admin", "CorrectHorseBattery1").status_code
        == 200
    )
    if path.endswith("/publish"):
        admin_api.knowledge_service.raise_for["queue_publish"] = exception
    elif path.endswith("/rollback"):
        admin_api.knowledge_service.raise_for["queue_rollback"] = exception
    elif path.endswith("/drafts"):
        admin_api.knowledge_service.raise_for["upload_draft"] = exception
    elif "/jobs/" in path:
        admin_api.knowledge_service.raise_for["get_job"] = exception
    else:
        admin_api.knowledge_service.raise_for["get_draft"] = exception

    if path.endswith("/drafts"):
        response = admin_api.client.post(
            path,
            files={"package": ("knowledge.zip", b"zip", "application/zip")},
        )
    elif path.endswith("/rollback"):
        response = admin_api.client.post(path, json={"reason": "because"})
    else:
        response = admin_api.client.request(method, path)

    assert response.status_code == status


def test_rollback_reason_is_persisted_in_audit_event_via_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    AuthBase.metadata.create_all(engine)
    KnowledgeAdminBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    auth_service = AuthService(factory, session_pepper="knowledge-admin-test-pepper")
    admin = auth_service.create_user(
        "metro.admin", "CorrectHorseBattery1", "admin", None
    )
    repo = KnowledgeAdminRepository(factory)
    draft = repo.create_draft(
        original_filename="knowledge.zip",
        package_sha256="a" * 64,
        package_size_bytes=42,
        storage_key="drafts/private/a/knowledge.zip",
        actor_user_id=admin.id,
        actor_username=admin.username,
    )
    repo.record_release(
        build_id=RELEASE_BUILD_ID,
        collection_name="private-rollback",
        artifact_sha256="b" * 64,
        source_manifest_sha256="c" * 64,
        draft_id=draft.id,
        document_count=1,
        validation_summary={"valid": True},
        published_at=NOW,
    )

    app = create_app(
        graph_factory=lambda: object(),
        history_service_factory=lambda: SimpleNamespace(),
        monitoring_service_factory=lambda: SimpleNamespace(),
        auth_service_factory=lambda: auth_service,
        knowledge_admin_service_factory=lambda: KnowledgeAdminService(repository=repo),
        chat_runner=lambda *_args, **_kwargs: "answer",
        knowledge_preflight=lambda: None,
    )

    with TestClient(app, base_url="http://testserver") as client:
        assert (
            login(client, "metro.admin", "CorrectHorseBattery1").status_code == 200
        )
        response = client.post(
            f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback",
            json={"reason": "restore validated release"},
        )
        duplicate = client.post(
            f"/api/admin/knowledge/releases/{RELEASE_BUILD_ID}/rollback",
            json={"reason": "duplicate rollback"},
        )
        with factory.begin() as session:
            events = {
                (event.action, event.result): event
                for event in session.scalars(select(KnowledgeAdminAuditEvent)).all()
            }
            events[("upload_draft", "succeeded")].occurred_at = datetime(
                2026, 9, 8, 11, 59, tzinfo=UTC
            )
            events[("rollback_release", "queued")].occurred_at = datetime(
                2026, 9, 8, 12, 0, tzinfo=UTC
            )
        audits = client.get("/api/admin/knowledge/audits?limit=1")

    assert response.status_code == 200
    assert response.json()["kind"] == "rollback_release"
    assert response.json()["status"] == "queued"
    assert duplicate.status_code == 409
    assert audits.status_code == 200
    audit_row = audits.json()[0]
    assert audit_row["action"] == "rollback_release"
    assert audit_row["result"] == "queued"
    assert audit_row["actor_user_id"] == str(admin.id)
    assert audit_row["actor_username"] == admin.username
    assert audit_row["draft_id"] is None
    assert audit_row["job_id"] is not None
    assert audit_row["release_build_id"] == RELEASE_BUILD_ID
    assert audit_row["reason_summary"] == "restore validated release"
    assert audit_row["failure_summary"] is None

    with factory() as session:
        audit = session.scalar(
            select(KnowledgeAdminAuditEvent).where(
                KnowledgeAdminAuditEvent.action == "rollback_release",
                KnowledgeAdminAuditEvent.result == "queued",
                KnowledgeAdminAuditEvent.release_build_id == RELEASE_BUILD_ID,
            )
        )
    assert audit is not None
    assert audit.reason_summary == "restore validated release"


def test_upload_forwards_multipart_without_browser_cookie_and_uses_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 201

        def json(self) -> dict[str, object]:
            return {
                "draft_id": DRAFT_ID,
                "job_id": JOB_ID,
                "status": "queued",
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url: str, *, files, headers):
            captured["url"] = url
            captured["files"] = files
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "metro_agent.knowledge_admin.service.httpx.AsyncClient",
        FakeAsyncClient,
    )
    service = KnowledgeAdminService(
        repository=SimpleNamespace(),
        worker_base_url="http://knowledge-publisher:8000",
        internal_bearer_secret="publisher-secret",
    )
    package = SimpleNamespace(filename="knowledge.zip", file=BytesIO(b"zip-bytes"))
    current_user = CurrentUser(id=17, username="metro.admin", role="admin")

    result = asyncio.run(service.upload_draft(package=package, current_user=current_user))

    assert result == {
        "draft_id": DRAFT_ID,
        "job_id": JOB_ID,
        "status": "queued",
    }
    assert captured["url"] == "http://knowledge-publisher:8000/internal/drafts"
    assert "Cookie" not in captured["headers"]
    assert captured["headers"]["Authorization"] == "Bearer publisher-secret"
    assert captured["headers"]["X-Knowledge-Actor-User-Id"] == "17"
    assert captured["headers"]["X-Knowledge-Actor-Username"] == "metro.admin"
    assert captured["files"]["package"][0] == "knowledge.zip"
    timeout = captured["timeout"]
    assert timeout.connect is not None
    assert timeout.read is not None
    assert timeout.write is not None
    assert timeout.pool is not None


@pytest.mark.parametrize(
    "status_code,expected_exception,expected_message",
    [
        (500, KnowledgeAdminWorkerUnavailableError, "publisher worker unavailable"),
        (502, KnowledgeAdminWorkerUnavailableError, "publisher worker unavailable"),
        (504, KnowledgeAdminWorkerUnavailableError, "publisher worker unavailable"),
        (400, KnowledgeAdminMalformedRequestError, "publisher worker rejected upload"),
        (422, KnowledgeAdminMalformedRequestError, "publisher worker rejected upload"),
    ],
)
def test_upload_response_status_codes_map_to_expected_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_exception: type[Exception],
    expected_message: str,
) -> None:
    class FakeResponse:
        def __init__(self, code: int) -> None:
            self.status_code = code

        def json(self) -> dict[str, object]:
            return {
                "draft_id": DRAFT_ID,
                "job_id": JOB_ID,
                "status": "queued",
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse(status_code)

    monkeypatch.setattr(
        "metro_agent.knowledge_admin.service.httpx.AsyncClient",
        FakeAsyncClient,
    )
    service = KnowledgeAdminService(
        repository=SimpleNamespace(),
        worker_base_url="http://knowledge-publisher:8000",
        internal_bearer_secret="publisher-secret",
    )
    package = SimpleNamespace(filename="knowledge.zip", file=BytesIO(b"zip-bytes"))
    current_user = CurrentUser(id=17, username="metro.admin", role="admin")

    with pytest.raises(expected_exception, match=expected_message):
        asyncio.run(service.upload_draft(package=package, current_user=current_user))
