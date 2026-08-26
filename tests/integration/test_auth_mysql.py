from __future__ import annotations

import multiprocessing
import os
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from queue import Empty
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from metro_agent.auth.models import AuthSession, AuthUser
from metro_agent.auth.service import AuthService


pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
MARKER_TABLE = "__metro_agent_test_ownership"
DATABASE_PREFIX = "metro_auth_test_"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,46}$")
DATABASE_NAME_PATTERN = re.compile(r"^metro_auth_test_[A-Za-z0-9_]{1,47}$")
EXPECTED_INDEXES = {
    "auth_sessions": {"ix_auth_sessions_user_id", "ix_auth_sessions_expires_at"},
    "auth_audit_events": {"ix_auth_audit_events_occurred_at"},
}


@dataclass(frozen=True)
class OwnedMySQLTestDatabase:
    admin_url: URL
    database_url: URL
    database_name: str
    run_id: str
    owner_token: str

    @classmethod
    def create(
        cls,
        admin_url_value: str,
        *,
        run_id: str,
        database_name: str,
    ) -> "OwnedMySQLTestDatabase":
        admin_url = make_url(admin_url_value)
        if admin_url.drivername != "mysql+pymysql" or admin_url.database != "mysql":
            raise RuntimeError(
                "AUTH_MYSQL_ADMIN_URL must use mysql+pymysql and the mysql database"
            )
        cls.validate_identity(run_id, database_name)

        owner_token = uuid.uuid4().hex
        database_url = admin_url.set(database=database_name)
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        database_created = False
        try:
            with admin_engine.connect() as connection:
                existing = connection.scalar(
                    text(
                        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                        "WHERE SCHEMA_NAME = :database_name"
                    ),
                    {"database_name": database_name},
                )
                if existing is not None:
                    raise RuntimeError(
                        "refusing to reuse an existing MySQL integration database"
                    )
                connection.exec_driver_sql(
                    f"CREATE DATABASE `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
                database_created = True
            database_engine = create_engine(database_url)
            try:
                with database_engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"CREATE TABLE `{MARKER_TABLE}` ("
                        "marker_key VARCHAR(32) PRIMARY KEY NOT NULL, "
                        "run_id VARCHAR(47) NOT NULL, "
                        "database_name VARCHAR(64) NOT NULL, "
                        "owner_token CHAR(32) NOT NULL)"
                    )
                    connection.execute(
                        text(
                            f"INSERT INTO `{MARKER_TABLE}` "
                            "(marker_key, run_id, database_name, owner_token) "
                            "VALUES ('owner', :run_id, :database_name, :owner_token)"
                        ),
                        {
                            "run_id": run_id,
                            "database_name": database_name,
                            "owner_token": owner_token,
                        },
                    )
            finally:
                database_engine.dispose()
        except BaseException as exc:
            if database_created:
                raise RuntimeError(
                    "test database creation was incomplete; it was left in place "
                    "because no verified ownership marker is available"
                ) from exc
            raise
        finally:
            admin_engine.dispose()

        return cls(admin_url, database_url, database_name, run_id, owner_token)

    @staticmethod
    def validate_identity(run_id: str, database_name: str) -> None:
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise RuntimeError("AUTH_MYSQL_TEST_RUN_ID has an unsafe format")
        expected_name = f"{DATABASE_PREFIX}{run_id.replace('-', '_')}"
        if (
            DATABASE_NAME_PATTERN.fullmatch(database_name) is None
            or database_name != expected_name
        ):
            raise RuntimeError(
                "AUTH_MYSQL_TEST_DATABASE must exactly match the unique run id"
            )

    def assert_owned(self) -> None:
        self.validate_identity(self.run_id, self.database_name)
        if self.database_url.database != self.database_name:
            raise RuntimeError("refusing destructive operation on unowned database")

        engine = create_engine(self.database_url)
        try:
            with engine.connect() as connection:
                marker = connection.execute(
                    text(
                        f"SELECT run_id, database_name, owner_token "
                        f"FROM `{MARKER_TABLE}` WHERE marker_key = 'owner'"
                    )
                ).one_or_none()
        except Exception:
            raise RuntimeError(
                "refusing destructive operation without ownership marker"
            ) from None
        finally:
            engine.dispose()
        expected_marker = (self.run_id, self.database_name, self.owner_token)
        if marker is None or tuple(marker) != expected_marker:
            raise RuntimeError("refusing destructive operation with mismatched marker")

    def alembic(self, revision: str, *, downgrade: bool = False) -> None:
        self.assert_owned()
        os.environ["AUTH_DATABASE_URL"] = self.database_url.render_as_string(
            hide_password=False
        )
        config = Config(str(ROOT / "alembic-auth.ini"))
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)

    def drop(self) -> None:
        self.assert_owned()
        admin_engine = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        try:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{self.database_name}`")
        finally:
            admin_engine.dispose()


@pytest.fixture(scope="session")
def owned_test_database() -> OwnedMySQLTestDatabase:
    admin_url = os.environ.get("AUTH_MYSQL_ADMIN_URL")
    if not admin_url:
        pytest.skip(
            "AUTH_MYSQL_ADMIN_URL 未设置；本机不创建真实 MySQL 专用集成测试库"
        )
    run_id = os.environ.get("AUTH_MYSQL_TEST_RUN_ID")
    database_name = os.environ.get("AUTH_MYSQL_TEST_DATABASE")
    if not run_id or not database_name:
        pytest.fail(
            "AUTH_MYSQL_TEST_RUN_ID and AUTH_MYSQL_TEST_DATABASE are required "
            "when AUTH_MYSQL_ADMIN_URL is set"
        )
    database = OwnedMySQLTestDatabase.create(
        admin_url,
        run_id=run_id,
        database_name=database_name,
    )
    try:
        yield database
    finally:
        database.drop()


@pytest.fixture(autouse=True)
def migrated_auth_database(
    owned_test_database: OwnedMySQLTestDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    database_url = owned_test_database.database_url.render_as_string(
        hide_password=False
    )
    monkeypatch.setenv("AUTH_DATABASE_URL", database_url)
    monkeypatch.setenv("AUTH_SESSION_PEPPER", "ci-test-pepper-not-for-production")
    owned_test_database.alembic("base", downgrade=True)
    owned_test_database.alembic("head")
    return database_url


def _service(database_url: str) -> tuple[AuthService, Any]:
    engine = create_engine(database_url, pool_pre_ping=True, hide_parameters=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return AuthService(factory, "ci-test-pepper-not-for-production"), engine


def _create_first_admin_process(
    database_url: str,
    username: str,
    barrier: Any,
    result_queue: Any,
) -> None:
    service, engine = _service(database_url)
    try:
        barrier.wait(timeout=30)
        try:
            service.create_user(username, "Admin-password-123!", "admin", None)
        except ValueError:
            result_queue.put("rejected")
        else:
            result_queue.put("created")
    except BaseException as exc:
        result_queue.put(f"error:{type(exc).__name__}")
        raise
    finally:
        engine.dispose()


def test_auth_migration_creates_tables_indexes_and_round_trips(
    migrated_auth_database: str,
    owned_test_database: OwnedMySQLTestDatabase,
) -> None:
    engine = create_engine(migrated_auth_database)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        MARKER_TABLE,
        "alembic_version",
        "users",
        "auth_sessions",
        "auth_audit_events",
    }
    for table_name, expected in EXPECTED_INDEXES.items():
        actual = {index["name"] for index in inspector.get_indexes(table_name)}
        assert expected <= actual

    engine.dispose()
    owned_test_database.alembic("base", downgrade=True)
    probe = create_engine(migrated_auth_database)
    assert set(inspect(probe).get_table_names()) <= {
        MARKER_TABLE,
        "alembic_version",
    }
    probe.dispose()
    owned_test_database.alembic("head")


def test_ownership_marker_is_required_before_destructive_operations(
    migrated_auth_database: str,
    owned_test_database: OwnedMySQLTestDatabase,
) -> None:
    engine = create_engine(migrated_auth_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE `{MARKER_TABLE}` SET owner_token = 'mismatch' "
                    "WHERE marker_key = 'owner'"
                )
            )
        with pytest.raises(RuntimeError, match="mismatched marker"):
            owned_test_database.assert_owned()
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(f"UPDATE `{MARKER_TABLE}` SET owner_token = :owner_token"),
                {"owner_token": owned_test_database.owner_token},
            )
        engine.dispose()


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
    resolved_admin = service.resolve_session(admin_token)
    assert resolved_admin is not None and resolved_admin.id == admin.id
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


def test_concurrent_first_admin_across_spawned_processes_creates_only_one(
    migrated_auth_database: str,
) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_create_first_admin_process,
            args=(migrated_auth_database, username, barrier, result_queue),
        )
        for username in ("admin-one", "admin-two")
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=45)
        for process in processes:
            assert not process.is_alive(), "spawned admin process timed out"
            assert process.exitcode == 0
        try:
            results = [result_queue.get(timeout=5) for _ in processes]
        except Empty as exc:
            raise AssertionError("spawned admin process returned no result") from exc
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()

    assert sorted(results) == ["created", "rejected"]
    engine = create_engine(migrated_auth_database)
    with sessionmaker(bind=engine)() as session:
        admins = list(session.scalars(select(AuthUser).where(AuthUser.role == "admin")))
    assert len(admins) == 1
    engine.dispose()
