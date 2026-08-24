from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def run_auth_migration(database_url: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if database_url is None:
        env.pop("AUTH_DATABASE_URL", None)
    else:
        env["AUTH_DATABASE_URL"] = database_url

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic-auth.ini",
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_auth_migration_requires_auth_database_url() -> None:
    result = run_auth_migration(None)

    assert result.returncode != 0
    assert "AUTH_DATABASE_URL" in result.stderr


def test_auth_migration_uses_validated_url_and_utc_schema() -> None:
    result = run_auth_migration(
        "mysql+pymysql://metro:secret@localhost:3306/metro_auth"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("DEFAULT UTC_TIMESTAMP()") == 4
    assert "DEFAULT now()" not in result.stdout
    for index_name in {
        "ix_auth_sessions_user_id",
        "ix_auth_sessions_expires_at",
        "ix_auth_audit_events_occurred_at",
    }:
        assert f"CREATE INDEX {index_name}" in result.stdout
