from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from metro_agent.auth.models import AuthBase, AuthSession
from metro_agent.auth.passwords import (
    normalize_username,
    password_hash,
    verify_password,
)
from metro_agent.auth.service import AuthService


@pytest.fixture
def auth_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    AuthBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def auth_service(auth_session_factory) -> AuthService:
    return AuthService(auth_session_factory, session_pepper="test-only-pepper")


def test_password_hash_and_normalized_username() -> None:
    assert normalize_username("  Metro.Admin ") == "metro.admin"
    encoded = password_hash("CorrectHorseBattery1")
    assert encoded != "CorrectHorseBattery1"
    assert encoded.startswith("$argon2")
    assert verify_password("CorrectHorseBattery1", encoded) is True
    assert verify_password("wrong", encoded) is False


@pytest.mark.parametrize(
    "username",
    ["ab", "a" * 65, "operator name", "operator@metro", "\u8fd0\u8425\u5458"],
)
def test_invalid_username_is_rejected(username: str) -> None:
    with pytest.raises(ValueError, match="username"):
        normalize_username(username)


@pytest.mark.parametrize(
    "password", ["short1a", "letterswithoutdigits", "123456789012"]
)
def test_weak_password_is_rejected(password: str) -> None:
    with pytest.raises(ValueError, match="password"):
        password_hash(password)


def test_create_and_authenticate_user_records_audit(auth_service: AuthService) -> None:
    user = auth_service.create_user(
        " Metro.Admin ", "CorrectHorseBattery1", "admin", None
    )

    authenticated = auth_service.authenticate("METRO.ADMIN", "CorrectHorseBattery1")

    assert user.username == "metro.admin"
    assert authenticated is not None
    assert authenticated.id == user.id
    assert authenticated.last_login_at is not None
    assert [event.event_type for event in auth_service.list_audit_events()] == [
        "user_created",
        "authentication_succeeded",
    ]


def test_bad_credentials_do_not_authenticate(auth_service: AuthService) -> None:
    auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)

    assert auth_service.authenticate("operator", "wrong-password1") is None
    assert auth_service.authenticate("missing", "wrong-password1") is None

    assert [event.event_type for event in auth_service.list_audit_events()][-2:] == [
        "authentication_failed",
        "authentication_failed",
    ]


def test_duplicate_username_and_invalid_role_are_rejected(
    auth_service: AuthService,
) -> None:
    auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)

    with pytest.raises(ValueError, match="username"):
        auth_service.create_user(" OPERATOR ", "AnotherPassword2", "user", None)
    with pytest.raises(ValueError, match="role"):
        auth_service.create_user("dispatcher", "CorrectHorseBattery1", "owner", None)


def test_disabling_user_invalidates_session(auth_service: AuthService) -> None:
    user = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)
    raw_token = auth_service.create_session(user.id, timedelta(hours=8), None)
    auth_service.set_user_active(user.id, False, user.id)
    assert auth_service.resolve_session(raw_token) is None


def test_expired_or_revoked_tokens_cannot_resolve(auth_service: AuthService) -> None:
    user = auth_service.create_user("dispatcher", "CorrectHorseBattery1", "user", None)
    expired = auth_service.create_session(user.id, timedelta(seconds=-1), None)
    active = auth_service.create_session(user.id, timedelta(hours=1), None)
    auth_service.revoke_session(active, user.id, user.id)
    assert auth_service.resolve_session(expired) is None
    assert auth_service.resolve_session(active) is None
    assert auth_service.list_audit_events()[-1].event_type == "session_revoked"


def test_active_session_resolves_without_storing_raw_token(
    auth_service: AuthService, auth_session_factory
) -> None:
    user = auth_service.create_user("dispatcher", "CorrectHorseBattery1", "user", None)
    raw_token = auth_service.create_session(user.id, timedelta(hours=1), user.id)

    resolved = auth_service.resolve_session(raw_token)
    with auth_session_factory() as session:
        stored = session.scalar(select(AuthSession))

    assert resolved is not None
    assert resolved.id == user.id
    assert stored is not None
    assert stored.token_hash != raw_token
    assert raw_token not in stored.token_hash
    assert len(stored.token_hash) == 64


def test_reset_password_revokes_old_sessions(auth_service: AuthService) -> None:
    user = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)
    raw_token = auth_service.create_session(user.id, timedelta(hours=1), user.id)

    auth_service.reset_password(user.id, "AnotherSecurePassword2", user.id)

    assert auth_service.resolve_session(raw_token) is None
    assert auth_service.authenticate("operator", "CorrectHorseBattery1") is None
    assert auth_service.authenticate("operator", "AnotherSecurePassword2") is not None
    assert "password_reset" in [
        event.event_type for event in auth_service.list_audit_events()
    ]


def test_list_users_and_audit_events_are_ordered(auth_service: AuthService) -> None:
    first = auth_service.create_user("first.user", "CorrectHorseBattery1", "user", None)
    second = auth_service.create_user(
        "second_user", "CorrectHorseBattery2", "admin", first.id
    )

    assert [user.id for user in auth_service.list_users()] == [first.id, second.id]
    events = auth_service.list_audit_events()
    assert [event.event_type for event in events] == ["user_created", "user_created"]
    assert events[-1].actor_user_id == first.id
    assert events[-1].subject_user_id == second.id


def test_write_and_audit_are_atomic(auth_service: AuthService, monkeypatch) -> None:
    from metro_agent.auth import service as service_module

    original = service_module.AuthAuditEvent

    def invalid_audit_event(**kwargs):
        event = original(**kwargs)
        event.event_type = None
        return event

    monkeypatch.setattr(service_module, "AuthAuditEvent", invalid_audit_event)

    with pytest.raises(IntegrityError):
        auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)

    assert auth_service.list_users() == []
