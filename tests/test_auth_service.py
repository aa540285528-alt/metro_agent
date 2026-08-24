from __future__ import annotations

import hashlib
import traceback
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from metro_agent.auth.models import AuthBase, AuthSession, AuthUser, utc_now_naive
from metro_agent.auth.passwords import (
    normalize_username,
    password_hash,
    verify_password,
)
from metro_agent.auth.service import AuthService


@pytest.fixture
def auth_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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
    "encoded",
    [
        "not-a-password-hash",
        "$2b$12$not-an-enabled-hash",
        "$argon2id$v=19$m=65536,t=3,p=4$truncated",
    ],
)
def test_unknown_or_damaged_password_hash_is_rejected(encoded: str) -> None:
    assert verify_password("CorrectHorseBattery1", encoded) is False


def test_sqlite_auth_fixture_enforces_foreign_keys(auth_session_factory) -> None:
    with auth_session_factory() as session:
        assert session.scalar(text("PRAGMA foreign_keys")) == 1


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


def test_login_atomically_authenticates_and_creates_resolvable_session(
    auth_service: AuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)

    def unsafe_separate_step(*_args, **_kwargs):
        raise AssertionError("login must not compose public authentication steps")

    monkeypatch.setattr(auth_service, "authenticate", unsafe_separate_step)
    monkeypatch.setattr(auth_service, "create_session", unsafe_separate_step)

    result = auth_service.login(
        " OPERATOR ", "CorrectHorseBattery1", timedelta(hours=8)
    )

    assert result is not None
    user, raw_token = result
    assert user.id == created.id
    assert user.last_login_at is not None
    assert auth_service.resolve_session(raw_token) is not None
    assert [event.event_type for event in auth_service.list_audit_events()][-2:] == [
        "authentication_succeeded",
        "session_created",
    ]


def test_password_reset_between_separate_steps_cannot_authorize_old_login(
    auth_service: AuthService,
) -> None:
    user = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)
    authenticated = auth_service.authenticate("operator", "CorrectHorseBattery1")
    assert authenticated is not None

    auth_service.reset_password(user.id, "AnotherSecurePassword2", user.id)
    administrative_token = auth_service.create_session(
        authenticated.id, timedelta(hours=1), user.id
    )

    assert auth_service.resolve_session(administrative_token) is not None
    assert (
        auth_service.login("operator", "CorrectHorseBattery1", timedelta(hours=1))
        is None
    )
    assert (
        auth_service.login("operator", "AnotherSecurePassword2", timedelta(hours=1))
        is not None
    )


def test_failed_login_writes_only_failure_audit(auth_service: AuthService) -> None:
    assert auth_service.login("missing", "WrongPassword1", timedelta(hours=1)) is None

    assert [event.event_type for event in auth_service.list_audit_events()] == [
        "authentication_failed"
    ]


def test_duplicate_username_and_invalid_role_are_rejected(
    auth_service: AuthService,
) -> None:
    auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)

    with pytest.raises(ValueError, match="username"):
        auth_service.create_user(" OPERATOR ", "AnotherPassword2", "user", None)
    with pytest.raises(ValueError, match="role"):
        auth_service.create_user("dispatcher", "CorrectHorseBattery1", "owner", None)


def test_unique_constraint_error_does_not_expose_password_hash(
    auth_service: AuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)
    stored_hash = auth_service.list_users()[0].password_hash

    original_scalar = Session.scalar
    skip_precheck = True

    def scalar_without_duplicate_precheck(self, statement, *args, **kwargs):
        nonlocal skip_precheck
        if skip_precheck:
            skip_precheck = False
            return None
        return original_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", scalar_without_duplicate_precheck)

    with pytest.raises(ValueError, match="username already exists") as error:
        auth_service.create_user("operator", "AnotherSecurePassword2", "user", None)

    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert "$argon2" not in rendered
    assert stored_hash not in rendered


def test_disabling_user_invalidates_session(auth_service: AuthService) -> None:
    user = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)
    raw_token = auth_service.create_session(user.id, timedelta(hours=8), None)
    auth_service.set_user_active(user.id, False, user.id)
    assert auth_service.resolve_session(raw_token) is None


def test_expired_or_revoked_tokens_cannot_resolve(
    auth_service: AuthService, auth_session_factory
) -> None:
    user = auth_service.create_user("dispatcher", "CorrectHorseBattery1", "user", None)
    expired = "already-expired-token"
    expired_hash = hashlib.sha256(f"test-only-pepper:{expired}".encode()).hexdigest()
    with auth_session_factory.begin() as session:
        now = utc_now_naive()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=expired_hash,
                created_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
            )
        )
    active = auth_service.create_session(user.id, timedelta(hours=1), None)
    auth_service.revoke_session(active, user.id, user.id)
    assert auth_service.resolve_session(expired) is None
    assert auth_service.resolve_session(active) is None
    assert auth_service.list_audit_events()[-1].event_type == "session_revoked"


@pytest.mark.parametrize(
    "ttl",
    [timedelta(0), timedelta(seconds=-1), timedelta(hours=8, microseconds=1)],
)
def test_session_ttl_outside_allowed_range_is_rejected(
    auth_service: AuthService, ttl: timedelta
) -> None:
    user = auth_service.create_user("dispatcher", "CorrectHorseBattery1", "user", None)

    with pytest.raises(ValueError, match="ttl"):
        auth_service.create_session(user.id, ttl, user.id)

    assert [event.event_type for event in auth_service.list_audit_events()] == [
        "user_created"
    ]


def test_session_ttl_allows_eight_hour_boundary(auth_service: AuthService) -> None:
    user = auth_service.create_user("dispatcher", "CorrectHorseBattery1", "user", None)

    raw_token = auth_service.create_session(user.id, timedelta(hours=8), user.id)

    assert auth_service.resolve_session(raw_token) is not None


def test_user_locks_are_sorted_and_use_for_update() -> None:
    captured = []
    first = AuthUser(id=2, username="first", password_hash="hash", role="admin")
    second = AuthUser(id=9, username="second", password_hash="hash", role="user")

    class CapturingSession:
        def scalars(self, statement):
            captured.append(statement)
            return [first, second]

    locked = AuthService._lock_users_for_update(CapturingSession(), 9, 2, 9, None)
    compiled = captured[0].compile(dialect=mysql.dialect())
    sql = str(compiled)

    assert locked == {2: first, 9: second}
    assert "FOR UPDATE" in sql
    assert "ORDER BY users.id" in sql
    assert [2, 9] in compiled.params.values()


def test_user_and_session_mutations_share_sorted_user_lock_helper(
    auth_service: AuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = auth_service.create_user("admin", "CorrectHorseBattery1", "admin", None)
    subject = auth_service.create_user(
        "operator", "CorrectHorseBattery2", "user", actor.id
    )
    calls = []
    original = AuthService._lock_users_for_update

    def recording_lock(session, *user_ids):
        calls.append(user_ids)
        return original(session, *user_ids)

    monkeypatch.setattr(
        AuthService, "_lock_users_for_update", staticmethod(recording_lock)
    )

    token = auth_service.create_session(subject.id, timedelta(hours=1), actor.id)
    auth_service.set_user_active(subject.id, True, actor.id)
    auth_service.reset_password(subject.id, "AnotherSecurePassword2", actor.id)
    auth_service.revoke_session(token, subject.id, actor.id)

    assert calls == [
        (subject.id, actor.id),
        (subject.id, actor.id),
        (subject.id, actor.id),
        (subject.id, actor.id),
    ]


def test_revoke_locks_users_before_session_row(
    auth_service: AuthService,
    auth_session_factory,
) -> None:
    actor = auth_service.create_user("admin", "CorrectHorseBattery1", "admin", None)
    subject = auth_service.create_user(
        "operator", "CorrectHorseBattery2", "user", actor.id
    )
    token = auth_service.create_session(subject.id, timedelta(hours=1), actor.id)
    statements = []

    def capture_statement(_connection, _cursor, statement, parameters, *_args):
        statements.append((" ".join(statement.lower().split()), parameters))

    engine = auth_session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        auth_service.revoke_session(token, subject.id, actor.id)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    selects = [item for item in statements if item[0].startswith("select")]
    assert " from users " in f" {selects[0][0]} "
    assert tuple(selects[0][1]) == tuple(sorted((actor.id, subject.id)))
    assert " from auth_sessions " in f" {selects[1][0]} "


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


def test_repeated_revoke_does_not_record_false_state_change(
    auth_service: AuthService,
) -> None:
    user = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)
    token = auth_service.create_session(user.id, timedelta(hours=1), user.id)

    auth_service.revoke_session(token, user.id, user.id)
    auth_service.revoke_session(token, user.id, user.id)

    event_types = [event.event_type for event in auth_service.list_audit_events()]
    assert event_types.count("session_revoked") == 1


def test_repeated_user_state_does_not_record_false_state_change(
    auth_service: AuthService,
) -> None:
    user = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)

    auth_service.set_user_active(user.id, True, user.id)
    auth_service.set_user_active(user.id, False, user.id)
    auth_service.set_user_active(user.id, False, user.id)
    auth_service.set_user_active(user.id, True, user.id)
    auth_service.set_user_active(user.id, True, user.id)

    event_types = [event.event_type for event in auth_service.list_audit_events()]
    assert event_types.count("user_disabled") == 1
    assert event_types.count("user_enabled") == 1


def test_malformed_username_is_digest_only_in_audit(auth_service: AuthService) -> None:
    malformed = " Invalid User " + ("x" * 10_000)

    assert auth_service.authenticate(malformed, "WrongPassword1") is None

    metadata = auth_service.list_audit_events()[-1].metadata_json
    expected = hashlib.sha256(malformed.strip().lower().encode()).hexdigest()
    assert metadata == {"username_sha256": expected}
    assert malformed not in str(metadata)


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
