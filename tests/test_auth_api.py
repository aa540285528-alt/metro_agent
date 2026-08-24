import base64
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import timedelta
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from metro_agent.api import create_app
from metro_agent.agent_service import AgentRunError
from metro_agent.auth.dependencies import (
    CurrentUser,
    auth_checkpoint_thread_id,
    auth_owner_subject,
    get_current_user,
    require_admin,
)
from metro_agent.auth.models import AuthBase
from metro_agent.auth.router import get_auth_cookie_secure
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
        self.claim_calls: list[tuple[str, str, str]] = []

    def claim_thread(self, thread_id: str, owner_id: str, initial_content: str) -> None:
        self.claim_calls.append((thread_id, owner_id, initial_content))
        for (stored_owner, stored_thread), _conversation in self.conversations.items():
            if stored_thread == thread_id:
                if stored_owner != owner_id:
                    raise ConversationNotFound(thread_id)
                return
        self.conversations[(owner_id, thread_id)] = {
            "id": thread_id,
            "title": " ".join(initial_content.split())[:40],
            "is_pinned": False,
        }

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


class RecordingChatRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.on_call = None

    def __call__(self, _graph, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call()
        return "answer"


class FakeMonitoringService:
    def summary(self, _filters):
        return {"trace_count": 0}


class FailingLogoutAuthService:
    def __init__(self, *, resolve_error=None, revoke_error=None) -> None:
        self.resolve_error = resolve_error
        self.revoke_error = revoke_error
        self.user = SimpleNamespace(id=7, username="operator", role="user")
        self.revoke_calls = 0

    def resolve_session(self, _raw_token: str):
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.user

    def revoke_session(self, *_args) -> None:
        self.revoke_calls += 1
        if self.revoke_error is not None:
            raise self.revoke_error


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
    runner = RecordingChatRunner()
    app = create_app(
        graph_factory=lambda: object(),
        history_service_factory=lambda: history,
        monitoring_service_factory=lambda: FakeMonitoringService(),
        auth_service_factory=lambda: auth_service,
        chat_runner=runner,
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            service=auth_service,
            history=history,
            runner=runner,
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


def test_cookie_secure_defaults_true_and_allows_explicit_local_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_COOKIE_SECURE", raising=False)
    assert get_auth_cookie_secure() is True

    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    assert get_auth_cookie_secure() is False


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


@pytest.mark.parametrize("cookie_value", [None, "invalid-or-expired-token"])
def test_logout_is_idempotent_for_missing_or_invalid_cookie(
    auth_api, cookie_value: str | None
) -> None:
    if cookie_value is not None:
        auth_api.client.cookies.set(
            "metro_session",
            cookie_value,
            domain="testserver.local",
            path="/",
        )

    response = auth_api.client.post("/api/auth/logout")

    assert response.status_code == 204
    assert "metro_session=" in response.headers["set-cookie"]
    assert auth_api.client.cookies.get("metro_session") is None


def test_logout_is_idempotent_for_revoked_cookie(auth_api) -> None:
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200
    token = auth_api.client.cookies.get("metro_session")
    assert token is not None
    auth_api.service.revoke_session(token, auth_api.user.id, auth_api.user.id)

    response = auth_api.client.post("/api/auth/logout")

    assert response.status_code == 204
    assert auth_api.client.cookies.get("metro_session") is None


def test_logout_is_idempotent_for_disabled_user_cookie(auth_api) -> None:
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200
    auth_api.service.update_user(
        auth_api.user.id,
        auth_api.admin.id,
        is_active=False,
    )

    response = auth_api.client.post("/api/auth/logout")

    assert response.status_code == 204
    assert auth_api.client.cookies.get("metro_session") is None


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("resolve", RuntimeError("auth database unavailable")),
        ("resolve", SQLAlchemyError("auth database unavailable")),
        ("revoke", RuntimeError("auth database unavailable")),
        ("revoke", SQLAlchemyError("auth database unavailable")),
    ],
)
def test_logout_propagates_database_and_transaction_failures(
    auth_api, stage: str, error: Exception
) -> None:
    fake = FailingLogoutAuthService(
        resolve_error=error if stage == "resolve" else None,
        revoke_error=error if stage == "revoke" else None,
    )
    with TestClient(auth_api.app, raise_server_exceptions=False) as client:
        auth_api.app.state.auth_service = fake
        client.cookies.set(
            "metro_session",
            "still-active",
            domain="testserver.local",
            path="/",
        )

        response = client.post("/api/auth/logout")

        assert response.status_code == 500
        assert "set-cookie" not in response.headers
        assert client.cookies.get("metro_session") == "still-active"


def test_logout_tolerates_concurrent_missing_session_during_revoke(auth_api) -> None:
    fake = FailingLogoutAuthService(revoke_error=ValueError("session not found"))
    with TestClient(auth_api.app) as client:
        auth_api.app.state.auth_service = fake
        client.cookies.set(
            "metro_session",
            "concurrently-revoked",
            domain="testserver.local",
            path="/",
        )

        response = client.post("/api/auth/logout")

        assert response.status_code == 204
        assert fake.revoke_calls == 1
        assert client.cookies.get("metro_session") is None


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
    owner_subject = auth_owner_subject(auth_api.user.id)
    assert auth_api.history.recorded_owners == [owner_subject]
    assert auth_api.history.claim_calls == [("t1", owner_subject, "hello")]
    assert auth_api.runner.calls == [
        {
            "thread_id": auth_checkpoint_thread_id(owner_subject, "t1"),
            "user_id": owner_subject,
            "message": "hello",
        }
    ]
    schema = auth_api.app.openapi()["components"]["schemas"]["ChatRequest"]
    assert "user_id" not in schema["properties"]
    assert schema["additionalProperties"] is False


def test_conversations_are_isolated_by_authenticated_user(auth_api) -> None:
    owner_id = auth_owner_subject(auth_api.user.id)
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


def test_attacker_reusing_owned_thread_is_rejected_before_runner(auth_api) -> None:
    owner = auth_owner_subject(auth_api.user.id)
    auth_api.history.conversations[(owner, "claimed-thread")] = {
        "id": "claimed-thread",
        "title": "private",
        "is_pinned": False,
    }
    attacker = independent_client(auth_api.app)
    try:
        assert login(attacker, "second.user", "CorrectHorseBattery3").status_code == 200

        response = attacker.post(
            "/api/chat/stream",
            json={"thread_id": "claimed-thread", "message": "steal context"},
        )

        assert response.status_code == 404
        assert auth_api.runner.calls == []
    finally:
        attacker.close()


def test_only_claim_winner_enters_runner_when_new_thread_competes(auth_api) -> None:
    first = independent_client(auth_api.app)
    second = independent_client(auth_api.app)

    def fail_agent() -> None:
        raise AgentRunError("simulated agent failure")

    auth_api.runner.on_call = fail_agent
    try:
        assert login(first, "operator", "CorrectHorseBattery2").status_code == 200
        assert login(second, "second.user", "CorrectHorseBattery3").status_code == 200

        winner = first.post(
            "/api/chat/stream",
            json={"thread_id": "new-race", "message": "first claim"},
        )
        loser = second.post(
            "/api/chat/stream",
            json={"thread_id": "new-race", "message": "second claim"},
        )

        assert winner.status_code == 200
        assert "event: error" in winner.text
        assert loser.status_code == 404
        assert len(auth_api.runner.calls) == 1
        winner_owner = auth_owner_subject(auth_api.user.id)
        assert (winner_owner, "new-race") in auth_api.history.conversations
        assert auth_api.history.recorded_owners == []
    finally:
        first.close()
        second.close()


def test_stream_rechecks_session_before_calling_runner(
    auth_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200
    original_resolve = auth_api.service.resolve_session
    resolve_count = 0

    def expire_after_route_authentication(raw_token: str):
        nonlocal resolve_count
        resolve_count += 1
        if resolve_count == 1:
            return original_resolve(raw_token)
        return None

    monkeypatch.setattr(
        auth_api.service, "resolve_session", expire_after_route_authentication
    )

    response = auth_api.client.post(
        "/api/chat/stream", json={"thread_id": "delayed", "message": "hello"}
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert auth_api.runner.calls == []
    assert auth_api.history.recorded_owners == []


def test_stream_does_not_write_history_when_user_disabled_during_runner(
    auth_api,
) -> None:
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200
    auth_api.runner.on_call = lambda: auth_api.service.update_user(
        auth_api.user.id,
        auth_api.admin.id,
        is_active=False,
    )

    response = auth_api.client.post(
        "/api/chat/stream", json={"thread_id": "disable-mid-run", "message": "hello"}
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "event: final" not in response.text
    assert len(auth_api.runner.calls) == 1
    assert auth_api.history.recorded_owners == []


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


@pytest.mark.parametrize("changes", [{"is_active": False}, {"role": "user"}])
def test_admin_cannot_lock_out_own_management_access(auth_api, changes: dict) -> None:
    assert (
        login(auth_api.client, "metro.admin", "CorrectHorseBattery1").status_code == 200
    )

    response = auth_api.client.patch(
        f"/api/admin/users/{auth_api.admin.id}", json=changes
    )

    stored = next(
        user for user in auth_api.service.list_users() if user.id == auth_api.admin.id
    )
    assert response.status_code == 422
    assert stored.is_active is True
    assert stored.role == "admin"
    assert auth_api.client.get("/api/admin/users").status_code == 200


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


def test_login_rate_limit_blocks_sixth_request_before_service_call(
    auth_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original_login = auth_api.service.login

    def recording_login(username: str, password: str, ttl: timedelta):
        nonlocal calls
        calls += 1
        return original_login(username, password, ttl)

    monkeypatch.setattr(auth_api.service, "login", recording_login)

    responses = [login(auth_api.client, "operator", "WrongPassword9") for _ in range(6)]

    assert [response.status_code for response in responses] == [
        401,
        401,
        401,
        401,
        401,
        429,
    ]
    assert calls == 5
    assert responses[-1].headers["Retry-After"] == "60"
    assert responses[-1].json() == {"detail": "Too many login attempts"}


def test_concurrent_sixth_login_is_blocked_before_database_call(
    auth_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_barrier = Barrier(6)
    calls_lock = Lock()
    database_calls = 0
    original_is_blocked = auth_api.app.state.login_rate_limiter.is_blocked

    def synchronized_non_atomic_check(*args):
        result = original_is_blocked(*args)
        check_barrier.wait(timeout=5)
        return result

    def slow_failed_login(*_args, **_kwargs):
        nonlocal database_calls
        with calls_lock:
            database_calls += 1
        return None

    monkeypatch.setattr(auth_api.service, "login", slow_failed_login)
    monkeypatch.setattr(
        auth_api.app.state.login_rate_limiter,
        "is_blocked",
        synchronized_non_atomic_check,
    )

    def request(index: int):
        client = TestClient(
            auth_api.app,
            client=("10.0.0.1", 50000 + index),
        )
        try:
            return login(client, "operator", "WrongPassword9")
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(request, range(6)))

    assert sorted(response.status_code for response in responses) == [
        401,
        401,
        401,
        401,
        401,
        429,
    ]
    assert database_calls == 5


def test_successful_login_clears_failure_bucket(auth_api) -> None:
    for _ in range(4):
        assert login(auth_api.client, "operator", "WrongPassword9").status_code == 401
    assert login(auth_api.client, "operator", "CorrectHorseBattery2").status_code == 200
    auth_api.client.cookies.clear()

    responses = [login(auth_api.client, "operator", "WrongPassword9") for _ in range(6)]

    assert [response.status_code for response in responses] == [
        401,
        401,
        401,
        401,
        401,
        429,
    ]


def test_login_rate_limit_does_not_trust_x_forwarded_for(auth_api) -> None:
    responses = []
    for index in range(6):
        responses.append(
            auth_api.client.post(
                "/api/auth/login",
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
                json={"username": "operator", "password": "WrongPassword9"},
            )
        )

    assert [response.status_code for response in responses] == [
        401,
        401,
        401,
        401,
        401,
        429,
    ]


def test_login_rate_limit_uses_request_client_host_not_forwarded_header(
    auth_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_api.service, "login", lambda *_args, **_kwargs: None)
    forwarded = {"X-Forwarded-For": "203.0.113.10"}
    first = TestClient(auth_api.app, client=("10.0.0.1", 50000))
    second = TestClient(auth_api.app, client=("10.0.0.2", 50000))
    try:
        first_responses = [
            first.post(
                "/api/auth/login",
                headers=forwarded,
                json={"username": f"user{index:03d}", "password": "WrongPassword9"},
            )
            for index in range(25)
        ]
        first_blocked = first.post(
            "/api/auth/login",
            headers=forwarded,
            json={"username": "blocked.user", "password": "WrongPassword9"},
        )
        second_response = second.post(
            "/api/auth/login",
            headers=forwarded,
            json={"username": "allowed.user", "password": "WrongPassword9"},
        )
    finally:
        first.close()
        second.close()

    assert all(response.status_code == 401 for response in first_responses)
    assert first_blocked.status_code == 429
    assert second_response.status_code == 401


def test_login_database_error_is_not_recorded_as_password_failure(
    auth_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_login = auth_api.service.login

    def fail_login(*_args, **_kwargs):
        raise RuntimeError("auth database unavailable")

    monkeypatch.setattr(auth_api.service, "login", fail_login)
    with pytest.raises(RuntimeError, match="auth database unavailable"):
        login(auth_api.client, "operator", "WrongPassword9")
    monkeypatch.setattr(auth_api.service, "login", original_login)

    assert (
        auth_api.app.state.login_rate_limiter.failure_count("operator", "testclient")
        == 0
    )


def test_create_app_builds_one_injected_rate_limiter(auth_api) -> None:
    from metro_agent.auth.rate_limit import LoginRateLimiter

    limiter = LoginRateLimiter()
    factory_calls = 0

    def limiter_factory():
        nonlocal factory_calls
        factory_calls += 1
        return limiter

    app = create_app(
        graph_factory=lambda: object(),
        history_service_factory=lambda: auth_api.history,
        monitoring_service_factory=lambda: FakeMonitoringService(),
        auth_service_factory=lambda: auth_api.service,
        rate_limiter_factory=limiter_factory,
        chat_runner=auth_api.runner,
    )

    with TestClient(app) as client:
        assert login(client, "operator", "WrongPassword9").status_code == 401
        assert login(client, "operator", "WrongPassword9").status_code == 401

    assert factory_calls == 1
    assert app.state.login_rate_limiter is limiter
    assert limiter.failure_count("operator", "testclient") == 2


def test_openapi_has_no_client_supplied_monitoring_user_id(auth_api) -> None:
    parameters = auth_api.app.openapi()["paths"]["/api/monitoring/summary"]["get"].get(
        "parameters", []
    )

    assert "user_id" not in {parameter["name"] for parameter in parameters}


def test_preview_serves_self_hosted_css_and_strict_script_csp(auth_api) -> None:
    page = auth_api.client.get("/")
    css = auth_api.client.get("/static/tailwind.css")

    assert page.status_code == 200
    assert 'href="/static/tailwind.css"' in page.text
    csp = page.headers["Content-Security-Policy"]
    assert "script-src 'self' 'sha256-" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", page.text, re.DOTALL)
    assert len(scripts) == 1
    script_hash = base64.b64encode(
        hashlib.sha256(scripts[0].encode("utf-8")).digest()
    ).decode("ascii")
    assert f"'sha256-{script_hash}'" in csp
    assert "cdn.tailwindcss.com" not in page.text
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert len(css.content) > 1000


def test_old_monitoring_user_id_is_explicitly_rejected(auth_api) -> None:
    assert (
        login(auth_api.client, "metro.admin", "CorrectHorseBattery1").status_code == 200
    )

    response = auth_api.client.get(
        "/api/monitoring/summary", params={"user_id": "legacy-owner"}
    )

    assert response.status_code == 422


def test_cross_origin_state_changes_are_rejected(auth_api) -> None:
    cross_origin = {"Origin": "https://attacker.example"}
    assert (
        auth_api.client.post(
            "/api/auth/login",
            headers=cross_origin,
            json={"username": "operator", "password": "CorrectHorseBattery2"},
        ).status_code
        == 403
    )

    assert (
        login(auth_api.client, "metro.admin", "CorrectHorseBattery1").status_code == 200
    )
    requests = [
        ("POST", "/api/auth/logout", None),
        (
            "POST",
            "/api/admin/users",
            {
                "username": "blocked.user",
                "password": "CorrectHorseBattery9",
                "role": "user",
            },
        ),
        ("PATCH", f"/api/admin/users/{auth_api.user.id}", {"role": "user"}),
        ("PATCH", "/api/conversations/missing", {"title": "blocked"}),
        ("DELETE", "/api/conversations/missing", None),
        ("POST", "/api/chat/stream", {"thread_id": "t1", "message": "blocked"}),
    ]
    for method, path, body in requests:
        response = auth_api.client.request(
            method,
            path,
            headers=cross_origin,
            json=body,
        )
        assert response.status_code == 403, (method, path, response.text)


def test_same_origin_and_missing_origin_are_allowed(auth_api) -> None:
    same_origin = login(
        auth_api.client,
        "operator",
        "CorrectHorseBattery2",
    )
    assert same_origin.status_code == 200

    auth_api.client.cookies.clear()
    response = auth_api.client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": "operator", "password": "CorrectHorseBattery2"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    ["HTTP://TESTSERVER:80", "http://testserver:80"],
)
def test_same_origin_normalizes_case_and_default_http_port(
    auth_api, origin: str
) -> None:
    response = auth_api.client.post(
        "/api/auth/login",
        headers={"Origin": origin},
        json={"username": "operator", "password": "CorrectHorseBattery2"},
    )

    assert response.status_code == 200


def test_same_origin_normalizes_default_https_port(auth_api) -> None:
    secure_app = create_app(
        graph_factory=lambda: object(),
        history_service_factory=lambda: auth_api.history,
        monitoring_service_factory=lambda: FakeMonitoringService(),
        auth_service_factory=lambda: auth_api.service,
        chat_runner=auth_api.runner,
    )
    with TestClient(secure_app, base_url="https://testserver") as client:
        response = client.post(
            "/api/auth/login",
            headers={"Origin": "HTTPS://TESTSERVER:443"},
            json={"username": "operator", "password": "CorrectHorseBattery2"},
        )

    assert response.status_code == 200


def test_same_origin_rejects_different_port(auth_api) -> None:
    response = auth_api.client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver:81"},
        json={"username": "operator", "password": "CorrectHorseBattery2"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "not-a-url",
        "http:///missing-host",
        "file://testserver",
        "http://testserver/path",
        "http://user@testserver",
        "http://testserver:",
    ],
)
def test_same_origin_rejects_invalid_or_hostless_origin(auth_api, origin: str) -> None:
    response = auth_api.client.post(
        "/api/auth/login",
        headers={"Origin": origin},
        json={"username": "operator", "password": "CorrectHorseBattery2"},
    )

    assert response.status_code == 403


def test_openapi_declares_cookie_security_and_sse_contract(auth_api) -> None:
    schema = auth_api.app.openapi()
    cookie_scheme = schema["components"]["securitySchemes"]["MetroSessionCookie"]
    assert cookie_scheme == {
        "type": "apiKey",
        "in": "cookie",
        "name": "metro_session",
    }

    chat_operation = schema["paths"]["/api/chat/stream"]["post"]
    assert {"MetroSessionCookie": []} in chat_operation["security"]
    assert "text/event-stream" in chat_operation["responses"]["200"]["content"]
    assert {"401", "403"}.issubset(chat_operation["responses"])
