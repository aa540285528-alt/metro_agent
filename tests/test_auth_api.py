from dataclasses import FrozenInstanceError
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from metro_agent.api import create_app
from metro_agent.auth.dependencies import CurrentUser, get_current_user, require_admin
from metro_agent.auth.models import AuthBase
from metro_agent.auth.service import AuthService
from metro_agent.storage.history.service import ConversationNotFound


class StubAuthService:
    def __init__(self, resolved_user=None) -> None:
        self.resolved_user = resolved_user
        self.resolved_tokens: list[str] = []

    def resolve_session(self, raw_token: str):
        self.resolved_tokens.append(raw_token)
        return self.resolved_user


def auth_request(service: StubAuthService):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_service=service))
    )


def test_current_user_is_immutable() -> None:
    current_user = CurrentUser(id=1, username="operator", role="user")

    with pytest.raises(FrozenInstanceError):
        current_user.role = "admin"


@pytest.mark.parametrize("token", [None, "", "invalid-token"])
def test_get_current_user_rejects_missing_or_invalid_session(token: str | None) -> None:
    service = StubAuthService()

    with pytest.raises(HTTPException) as error:
        get_current_user(auth_request(service), token)

    assert error.value.status_code == 401
    assert error.value.detail == "Not authenticated"


def test_get_current_user_returns_safe_projection() -> None:
    service = StubAuthService(
        SimpleNamespace(id=7, username="metro.admin", role="admin")
    )

    current_user = get_current_user(auth_request(service), "raw-token")

    assert current_user == CurrentUser(id=7, username="metro.admin", role="admin")
    assert service.resolved_tokens == ["raw-token"]


def test_require_admin_rejects_regular_user() -> None:
    with pytest.raises(HTTPException) as error:
        require_admin(CurrentUser(id=9, username="operator", role="user"))

    assert error.value.status_code == 403
    assert error.value.detail == "Admin role required"


class FakeHistoryService:
    def __init__(self) -> None:
        self.conversations: dict[tuple[str, str], dict] = {}
        self.recorded_owners: list[str] = []

    def list_conversations(self, owner_id: str) -> list[dict]:
        return [
            value
            for (stored_owner, _thread_id), value in self.conversations.items()
            if stored_owner == owner_id
        ]

    def get_conversation(self, thread_id: str, owner_id: str) -> dict:
        try:
            return self.conversations[(owner_id, thread_id)]
        except KeyError as exc:
            raise ConversationNotFound(thread_id) from exc

    def update_conversation(
        self,
        thread_id: str,
        owner_id: str,
        *,
        title: str | None = None,
        is_pinned: bool | None = None,
    ) -> dict:
        conversation = self.get_conversation(thread_id, owner_id)
        if title is not None:
            conversation["title"] = title
        if is_pinned is not None:
            conversation["is_pinned"] = is_pinned
        return conversation

    def delete_conversation(self, thread_id: str, owner_id: str) -> None:
        self.get_conversation(thread_id, owner_id)
        del self.conversations[(owner_id, thread_id)]

    def record_turn(
        self,
        thread_id: str,
        owner_id: str,
        _message: str,
        _answer: str,
    ) -> None:
        self.recorded_owners.append(owner_id)
        self.conversations[(owner_id, thread_id)] = {
            "id": thread_id,
            "title": "new conversation",
            "is_pinned": False,
        }


@pytest.fixture
def auth_api(monkeypatch: pytest.MonkeyPatch):
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
    auth_service = AuthService(factory, session_pepper="api-test-pepper")
    admin = auth_service.create_user(
        "metro.admin", "CorrectHorseBattery1", "admin", None
    )
    user = auth_service.create_user(
        "operator", "CorrectHorseBattery2", "user", admin.id
    )
    other = auth_service.create_user(
        "second.user", "CorrectHorseBattery3", "user", admin.id
    )
    history = FakeHistoryService()
    app = create_app(
        graph_factory=lambda: object(),
        history_service_factory=lambda: history,
        monitoring_service_factory=lambda: SimpleNamespace(),
        auth_service_factory=lambda: auth_service,
        chat_runner=lambda *_args, **_kwargs: "answer",
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            service=auth_service,
            history=history,
            admin=admin,
            user=user,
            other=other,
        )
    engine.dispose()


def login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def independent_client(app) -> TestClient:
    return TestClient(app, base_url="http://testserver")


def test_unauthenticated_me_is_rejected(auth_api) -> None:
    response = auth_api.client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_login_sets_safe_cookie_and_me_returns_safe_user(auth_api) -> None:
    response = login(auth_api.client, "operator", "CorrectHorseBattery2")

    assert response.status_code == 200
    assert response.json() == {
        "id": auth_api.user.id,
        "username": "operator",
        "role": "user",
    }
    cookie = response.headers["set-cookie"]
    assert "metro_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=3600" in cookie
    assert "Secure" not in cookie
    assert auth_api.client.get("/api/auth/me").json() == response.json()


def test_login_cookie_uses_secure_attribute_when_configured(
    auth_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    secure_app = create_app(
        graph_factory=lambda: object(),
        history_service_factory=lambda: auth_api.history,
        monitoring_service_factory=lambda: SimpleNamespace(),
        auth_service_factory=lambda: auth_api.service,
        chat_runner=lambda *_args, **_kwargs: "answer",
    )

    with TestClient(secure_app, base_url="https://testserver") as secure_client:
        response = login(secure_client, "operator", "CorrectHorseBattery2")

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_login_failure_is_generic(auth_api) -> None:
    wrong_password = login(auth_api.client, "operator", "WrongPassword9")
    missing_user = login(auth_api.client, "missing", "WrongPassword9")

    assert wrong_password.status_code == 401
    assert missing_user.status_code == 401
    assert (
        wrong_password.json()
        == missing_user.json()
        == {"detail": "Invalid username or password"}
    )


def test_logout_revokes_cookie_and_future_requests(auth_api) -> None:
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200

    response = auth_api.client.post("/api/auth/logout")

    assert response.status_code == 204
    assert "metro_session=" in response.headers["set-cookie"]
    assert auth_api.client.get("/api/auth/me").status_code == 401


def test_regular_user_cannot_access_admin_or_monitoring(auth_api) -> None:
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200

    assert auth_api.client.get("/api/monitoring/summary").status_code == 403
    assert auth_api.client.get("/api/admin/users").status_code == 403


def test_business_api_requires_authentication(auth_api) -> None:
    assert auth_api.client.get("/api/conversations").status_code == 401
    assert (
        auth_api.client.post(
            "/api/chat/stream", json={"thread_id": "t1", "message": "hello"}
        ).status_code
        == 401
    )


def test_chat_rejects_user_id_and_uses_authenticated_owner(auth_api) -> None:
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200

    rejected = auth_api.client.post(
        "/api/chat/stream",
        json={"thread_id": "t1", "message": "hello", "user_id": "attacker"},
    )
    accepted = auth_api.client.post(
        "/api/chat/stream", json={"thread_id": "t1", "message": "hello"}
    )

    assert rejected.status_code == 422
    assert accepted.status_code == 200
    assert auth_api.history.recorded_owners == [str(auth_api.user.id)]
    schema = auth_api.app.openapi()["components"]["schemas"]["ChatRequest"]
    assert "user_id" not in schema["properties"]
    assert schema["additionalProperties"] is False


def test_conversations_are_isolated_by_authenticated_user(auth_api) -> None:
    owner_id = str(auth_api.user.id)
    auth_api.history.conversations[(owner_id, "private-thread")] = {
        "id": "private-thread",
        "title": "private",
        "is_pinned": False,
    }
    first = independent_client(auth_api.app)
    second = independent_client(auth_api.app)
    try:
        assert login(first, "operator", "CorrectHorseBattery2").status_code == 200
        assert login(second, "second.user", "CorrectHorseBattery3").status_code == 200

        assert (
            first.get(
                "/api/conversations/private-thread",
                params={"user_id": str(auth_api.other.id)},
            ).status_code
            == 200
        )
        assert second.get("/api/conversations/private-thread").status_code == 404
        assert second.get("/api/conversations").json() == []
    finally:
        first.close()
        second.close()


def test_admin_user_management_is_safe_and_disabling_invalidates_cookie(
    auth_api,
) -> None:
    admin_client = independent_client(auth_api.app)
    user_client = independent_client(auth_api.app)
    try:
        assert (
            login(admin_client, "metro.admin", "CorrectHorseBattery1").status_code
            == 200
        )
        assert login(user_client, "operator", "CorrectHorseBattery2").status_code == 200

        created = admin_client.post(
            "/api/admin/users",
            json={
                "username": "new.user",
                "password": "CorrectHorseBattery4",
                "role": "user",
            },
        )
        listed = admin_client.get("/api/admin/users")
        promoted = admin_client.patch(
            f"/api/admin/users/{created.json()['id']}", json={"role": "admin"}
        )
        changed = admin_client.patch(
            f"/api/admin/users/{auth_api.user.id}",
            json={"is_active": False, "role": "user", "password": "NewPassword9"},
        )

        assert created.status_code == 201
        assert listed.status_code == 200
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "admin"
        assert changed.status_code == 200
        rendered = f"{created.text}{listed.text}{promoted.text}{changed.text}"
        assert "password_hash" not in rendered
        assert "token_hash" not in rendered
        assert user_client.get("/api/auth/me").status_code == 401
        assert "user_role_changed" in [
            event.event_type for event in auth_api.service.list_audit_events()
        ]
    finally:
        admin_client.close()
        user_client.close()


def test_admin_multi_field_update_is_atomic(auth_api) -> None:
    assert (
        login(auth_api.client, "metro.admin", "CorrectHorseBattery1").status_code == 200
    )

    response = auth_api.client.patch(
        f"/api/admin/users/{auth_api.other.id}",
        json={"role": "admin", "password": "weak"},
    )

    stored = next(
        user for user in auth_api.service.list_users() if user.id == auth_api.other.id
    )
    assert response.status_code == 422
    assert stored.role == "user"
    assert not any(
        event.event_type == "user_role_changed"
        and event.subject_user_id == auth_api.other.id
        for event in auth_api.service.list_audit_events()
    )


def test_login_uses_atomic_auth_service_method(auth_api, monkeypatch) -> None:
    calls = []
    original_login = auth_api.service.login

    def recording_login(username: str, password: str, ttl: timedelta):
        calls.append((username, password, ttl))
        return original_login(username, password, ttl)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("API login must not call split authentication methods")

    monkeypatch.setattr(auth_api.service, "login", recording_login)
    monkeypatch.setattr(auth_api.service, "authenticate", forbidden)
    monkeypatch.setattr(auth_api.service, "create_session", forbidden)

    response = login(auth_api.client, "operator", "CorrectHorseBattery2")

    assert response.status_code == 200
    assert calls == [("operator", "CorrectHorseBattery2", timedelta(seconds=3600))]


def test_openapi_has_no_client_supplied_monitoring_user_id(auth_api) -> None:
    parameters = auth_api.app.openapi()["paths"]["/api/monitoring/summary"]["get"].get(
        "parameters", []
    )

    assert "user_id" not in {parameter["name"] for parameter in parameters}
