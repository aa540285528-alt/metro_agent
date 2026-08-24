from __future__ import annotations

import pytest

from metro_agent.auth.database import create_auth_engine, validate_auth_database_url


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        "",
        "   ",
        "postgresql+psycopg://metro:secret@localhost/metro_auth",
        "sqlite:///metro_auth.db",
        "mysql+pymysql://metro:secret@localhost",
        "mysql+pymysql://metro:secret@localhost/",
    ],
)
def test_validate_auth_database_url_rejects_unsafe_values(
    database_url: str | None,
) -> None:
    with pytest.raises(ValueError, match="AUTH_DATABASE_URL"):
        validate_auth_database_url(database_url)


def test_validate_auth_database_url_accepts_dedicated_mysql_database() -> None:
    database_url = "mysql+pymysql://metro:secret@localhost:3306/metro_auth"

    assert validate_auth_database_url(database_url) == database_url


def test_auth_engine_hides_sql_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AUTH_DATABASE_URL",
        "mysql+pymysql://metro:secret@localhost:3306/metro_auth",
    )

    engine = create_auth_engine()
    try:
        assert engine.hide_parameters is True
    finally:
        engine.dispose()
