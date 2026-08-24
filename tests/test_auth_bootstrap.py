from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
