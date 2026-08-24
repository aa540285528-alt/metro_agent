from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from metro_agent.auth.models import AuthSession, AuthUser
from metro_agent.auth.service import AuthService


pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
EXPECTED_INDEXES = {
    "auth_sessions": {"ix_auth_sessions_user_id", "ix_auth_sessions_expires_at"},
    "auth_audit_events": {"ix_auth_audit_events_occurred_at"},
}


def _test_database_url() -> str:
    database_url = os.environ.get("AUTH_MYSQL_TEST_URL")
    if not database_url:
        pytest.skip("AUTH_MYSQL_TEST_URL 未设置；本机不运行真实 MySQL 集成测试")
    parsed = make_url(database_url)
    if parsed.drivername != "mysql+pymysql" or not (
        parsed.database and parsed.database.endswith("_test")
    ):
        pytest.fail("AUTH_MYSQL_TEST_URL 必须指向名称以 _test 结尾的 mysql+pymysql 测试库")
    return database_url


def _alembic_config(database_url: str) -> Config:
    os.environ["AUTH_DATABASE_URL"] = database_url
    return Config(str(ROOT / "alembic-auth.ini"))


@pytest.fixture(autouse=True)
def migrated_auth_database(monkeypatch: pytest.MonkeyPatch):
    database_url = _test_database_url()
    monkeypatch.setenv("AUTH_DATABASE_URL", database_url)
    monkeypatch.setenv("AUTH_SESSION_PEPPER", "ci-test-pepper-not-for-production")
    config = _alembic_config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield database_url
    command.downgrade(config, "base")


def _service(database_url: str) -> tuple[AuthService, object]:
    engine = create_engine(database_url, pool_pre_ping=True, hide_parameters=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return AuthService(factory, "ci-test-pepper-not-for-production"), engine


def test_auth_migration_creates_tables_indexes_and_round_trips(
    migrated_auth_database: str,
) -> None:
    database_url = migrated_auth_database
    engine = create_engine(database_url)
    config = _alembic_config(database_url)

    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        "alembic_version",
        "users",
        "auth_sessions",
        "auth_audit_events",
    }
    for table_name, expected in EXPECTED_INDEXES.items():
        actual = {index["name"] for index in inspector.get_indexes(table_name)}
        assert expected <= actual

    engine.dispose()
    command.downgrade(config, "base")
    probe = create_engine(database_url)
    assert set(inspect(probe).get_table_names()) <= {"alembic_version"}
    probe.dispose()
    command.upgrade(config, "head")


def test_auth_service_login_revoke_disable_and_foreign_key(
    migrated_auth_database: str,
) -> None:
    service, engine = _service(migrated_auth_database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    admin = service.create_user("pilot-admin", "Admin-password-123!", "admin", None)
    assert admin.password_hash.startswith("$argon2")

    logged_in = service.login(
        "pilot-admin", "Admin-password-123!", timedelta(hours=1)
    )
    assert logged_in is not None
    _, admin_token = logged_in
    assert service.resolve_session(admin_token).id == admin.id
    service.revoke_session(admin_token, admin.id, admin.id)
    assert service.resolve_session(admin_token) is None

    user = service.create_user(
        "pilot-user", "User-password-123!", "user", admin.id
    )
    user_login = service.login("pilot-user", "User-password-123!", timedelta(hours=1))
    assert user_login is not None
    _, user_token = user_login
    service.set_user_active(user.id, False, admin.id)
    assert service.resolve_session(user_token) is None

    with pytest.raises(IntegrityError), factory.begin() as session:
        session.add(
            AuthSession(
                user_id=9_999_999,
                token_hash="f" * 64,
                expires_at=admin.created_at + timedelta(hours=1),
            )
        )
        session.flush()
    engine.dispose()


def test_concurrent_first_admin_across_connections_creates_only_one(
    migrated_auth_database: str,
) -> None:
    database_url = migrated_auth_database
    first_service, first_engine = _service(database_url)
    second_service, second_engine = _service(database_url)

    def create(service: AuthService, username: str) -> str:
        try:
            service.create_user(username, "Admin-password-123!", "admin", None)
        except ValueError:
            return "rejected"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: create(*args),
                ((first_service, "admin-one"), (second_service, "admin-two")),
            )
        )

    assert sorted(results) == ["created", "rejected"]
    with sessionmaker(bind=first_engine)() as session:
        admins = list(session.scalars(select(AuthUser).where(AuthUser.role == "admin")))
    assert len(admins) == 1
    first_engine.dispose()
    second_engine.dispose()
