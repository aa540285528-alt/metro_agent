from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_auth_migration_requires_auth_database_url() -> None:
    env = os.environ.copy()
    env.pop("AUTH_DATABASE_URL", None)

    result = subprocess.run(
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

    assert result.returncode != 0
    assert "AUTH_DATABASE_URL" in result.stderr
