from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from metro_agent.auth.models import AuthBase
from metro_agent.auth.service import AuthService


class RecordingAuthService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str, int | None]] = []

    def create_user(
        self, username: str, password: str, role: str, actor_user_id: int | None
    ) -> SimpleNamespace:
        self.calls.append((username, password, role, actor_user_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=1, username=username, role=role)


@pytest.fixture
def bootstrap_module():
    from metro_agent.auth import bootstrap_admin

    return bootstrap_admin


@pytest.fixture
def auth_service() -> AuthService:
    database_name = f"bootstrap_{uuid4().hex}"
    engine = create_engine(
        f"sqlite+pysqlite:///file:{database_name}?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False},
    )
    AuthBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return AuthService(factory, session_pepper="bootstrap-test-pepper")


def set_passwords(monkeypatch, bootstrap_module, *passwords: str) -> None:
    values = iter(passwords)
    monkeypatch.setattr(bootstrap_module, "getpass", lambda _prompt: next(values))


def test_bootstrap_creates_first_admin_without_printing_password(
    bootstrap_module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = RecordingAuthService()
    secret = "CorrectHorseBattery1"
    set_passwords(monkeypatch, bootstrap_module, secret, secret)

    result = bootstrap_module.run_bootstrap(service, "metro.admin")

    captured = capsys.readouterr()
    assert result == 0
    assert service.calls == [("metro.admin", secret, "admin", None)]
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ValueError("username already exists"), "username already exists"),
        (ValueError("active admin already exists"), "active admin already exists"),
        (
            ValueError("username must be 3-64 characters"),
            "username must be 3-64 characters",
        ),
        (
            ValueError("password must be at least 12 characters"),
            "password must be at least 12 characters",
        ),
    ],
)
def test_bootstrap_reports_expected_errors_without_secret(
    bootstrap_module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    message: str,
) -> None:
    secret = "CorrectHorseBattery1"
    service = RecordingAuthService(error)
    set_passwords(monkeypatch, bootstrap_module, secret, secret)

    result = bootstrap_module.run_bootstrap(service, "metro.admin")

    captured = capsys.readouterr()
    assert result != 0
    assert message in captured.err
    assert secret not in captured.err


def test_bootstrap_rejects_mismatched_passwords_without_calling_service(
    bootstrap_module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = RecordingAuthService()
    first = "CorrectHorseBattery1"
    second = "CorrectHorseBattery2"
    set_passwords(monkeypatch, bootstrap_module, first, second)

    result = bootstrap_module.run_bootstrap(service, "metro.admin")

    captured = capsys.readouterr()
    assert result != 0
    assert service.calls == []
    assert "do not match" in captured.err.lower()
    assert first not in captured.err
    assert second not in captured.err


def test_existing_active_admin_blocks_a_different_bootstrap_account(
    bootstrap_module,
    auth_service: AuthService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_service.create_user(
        "first.admin", "CorrectHorseBattery1", "admin", None
    )
    set_passwords(
        monkeypatch,
        bootstrap_module,
        "CorrectHorseBattery2",
        "CorrectHorseBattery2",
    )

    result = bootstrap_module.run_bootstrap(auth_service, "second.admin")

    assert result != 0
    assert "active admin already exists" in capsys.readouterr().err
    assert [user.username for user in auth_service.list_users()] == ["first.admin"]


def test_bootstrap_rejects_duplicate_username_with_real_service(
    bootstrap_module,
    auth_service: AuthService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth_service.create_user(
        "existing.user", "CorrectHorseBattery1", "user", None
    )
    set_passwords(
        monkeypatch,
        bootstrap_module,
        "CorrectHorseBattery2",
        "CorrectHorseBattery2",
    )

    result = bootstrap_module.run_bootstrap(auth_service, "existing.user")

    assert result != 0
    assert "username already exists" in capsys.readouterr().err


def test_bootstrap_rejects_weak_password_with_real_service(
    bootstrap_module,
    auth_service: AuthService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_passwords(monkeypatch, bootstrap_module, "weak", "weak")

    result = bootstrap_module.run_bootstrap(auth_service, "metro.admin")

    assert result != 0
    assert "password must be at least 12 characters" in capsys.readouterr().err
    assert auth_service.list_users() == []


def test_bootstrap_hides_secret_from_unexpected_exception(
    bootstrap_module, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "CorrectHorseBattery1"
    service = RecordingAuthService(RuntimeError(f"database rejected {secret}"))
    set_passwords(monkeypatch, bootstrap_module, secret, secret)

    result = bootstrap_module.run_bootstrap(service, "metro.admin")

    assert result != 0
    assert secret not in capsys.readouterr().err


def test_concurrent_bootstrap_creates_only_one_first_admin(
    auth_service: AuthService,
) -> None:
    def create(username: str) -> str:
        try:
            auth_service.create_user(
                username, "CorrectHorseBattery1", "admin", None
            )
        except ValueError as exc:
            return str(exc)
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ["first.admin", "second.admin"]))

    assert results.count("created") == 1
    assert results.count("active admin already exists") == 1


def test_main_requires_username_argument(bootstrap_module) -> None:
    with pytest.raises(SystemExit) as exc_info:
        bootstrap_module.main([])

    assert exc_info.value.code != 0


def test_main_wires_parsed_username_through_default_service_factory(
    bootstrap_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory = object()
    auth_service = object()
    calls: list[tuple[object, ...]] = []

    def create_session_factory():
        calls.append(("factory",))
        return session_factory

    def create_service(factory):
        calls.append(("service", factory))
        return auth_service

    def run(service, username: str) -> int:
        calls.append(("run", service, username))
        return 7

    monkeypatch.setattr(
        bootstrap_module, "create_auth_session_factory", create_session_factory
    )
    monkeypatch.setattr(bootstrap_module, "AuthService", create_service)
    monkeypatch.setattr(bootstrap_module, "run_bootstrap", run)

    result = bootstrap_module.main(["--username", "Metro.Admin"])

    assert result == 7
    assert calls == [
        ("factory",),
        ("service", session_factory),
        ("run", auth_service, "Metro.Admin"),
    ]


@pytest.mark.parametrize("failure_stage", ["factory", "service"])
def test_main_configuration_failure_is_nonzero_and_hides_exception_secret(
    bootstrap_module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_stage: str,
) -> None:
    secret = "mysql://user:database-password@host/auth"

    def create_session_factory():
        if failure_stage == "factory":
            raise RuntimeError(secret)
        return object()

    def create_service(_factory):
        if failure_stage == "service":
            raise RuntimeError(secret)
        return object()

    monkeypatch.setattr(
        bootstrap_module, "create_auth_session_factory", create_session_factory
    )
    monkeypatch.setattr(bootstrap_module, "AuthService", create_service)
    monkeypatch.setattr(
        bootstrap_module,
        "run_bootstrap",
        lambda *_args: pytest.fail("run_bootstrap must not run after config failure"),
    )

    result = bootstrap_module.main(["--username", "metro.admin"])

    captured = capsys.readouterr()
    assert result != 0
    assert "authentication database configuration failed" in captured.err
    assert secret not in captured.err


class SimulatedMySQLConnection:
    def __init__(self, operations: list[str], release_result: int = 1) -> None:
        self.operations = operations
        self.release_result = release_result

    def __enter__(self):
        self.operations.append("connection_enter")
        return self

    def __exit__(self, *_args) -> None:
        self.operations.append("connection_exit")

    def scalar(self, statement):
        rendered = str(statement).lower()
        if "get_lock" in rendered:
            self.operations.append("acquire")
            return 1
        if "release_lock" in rendered:
            self.operations.append("release")
            return self.release_result
        raise AssertionError(f"unexpected connection statement: {rendered}")


class SimulatedMySQLEngine:
    def __init__(self, operations: list[str], release_result: int = 1) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self.connection = SimulatedMySQLConnection(operations, release_result)

    def connect(self) -> SimulatedMySQLConnection:
        return self.connection


class BindProbeSession:
    def __init__(self, engine: SimulatedMySQLEngine, operations: list[str]) -> None:
        self.bind = engine
        self.operations = operations

    def get_bind(self):
        self.operations.append("get_bind")
        return self.bind

    def close(self) -> None:
        self.operations.append("probe_close")


class ConnectionBoundSession:
    def __init__(
        self,
        *,
        bind,
        expire_on_commit: bool,
        join_transaction_mode: str,
        operations: list[str],
    ) -> None:
        assert isinstance(bind, SimulatedMySQLConnection)
        assert expire_on_commit is False
        assert join_transaction_mode == "control_fully"
        self.connection = bind
        self.operations = operations
        self.pending_user = None

    @contextmanager
    def begin(self):
        self.operations.append("transaction_begin")
        try:
            yield self
        except Exception:
            self.operations.append("rollback")
            raise
        else:
            self.operations.append("commit")

    def scalar(self, statement):
        rendered = str(statement).lower()
        if "users.role" in rendered and "users.is_active" in rendered:
            return None
        if "users.username" in rendered:
            return None
        raise AssertionError(f"unexpected session statement: {rendered}")

    def add(self, value) -> None:
        if value.__class__.__name__ == "AuthUser":
            self.pending_user = value

    def flush(self) -> None:
        assert self.pending_user is not None
        self.pending_user.id = 1

    def close(self) -> None:
        self.operations.append("session_close")


def mysql_lock_service(
    monkeypatch: pytest.MonkeyPatch, *, release_result: int = 1
) -> tuple[AuthService, SimulatedMySQLConnection, list[str]]:
    import metro_agent.auth.service as service_module

    operations: list[str] = []
    engine = SimulatedMySQLEngine(operations, release_result)

    def session_factory():
        return BindProbeSession(engine, operations)

    def connection_session(**kwargs):
        return ConnectionBoundSession(operations=operations, **kwargs)

    monkeypatch.setattr(service_module, "Session", connection_session)
    return (
        AuthService(session_factory, "mysql-lock-test-pepper"),
        engine.connection,
        operations,
    )


def test_mysql_advisory_lock_uses_one_connection_through_commit_and_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _connection, operations = mysql_lock_service(monkeypatch)

    created = service.create_user(
        "first.admin", "CorrectHorseBattery1", "admin", None
    )

    assert created.username == "first.admin"
    assert operations.count("connection_enter") == 1
    assert operations.index("acquire") < operations.index("commit")
    assert operations.index("commit") < operations.index("release")
    assert operations.index("release") < operations.index("connection_exit")


def test_mysql_advisory_lock_release_failure_is_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _connection, operations = mysql_lock_service(
        monkeypatch, release_result=0
    )

    with pytest.raises(RuntimeError, match="release first admin lock"):
        service.create_user("first.admin", "CorrectHorseBattery1", "admin", None)

    assert operations.index("commit") < operations.index("release")
