from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "deploy" / "operations"


def _read(name: str) -> str:
    return (OPERATIONS / name).read_text(encoding="utf-8")


def test_partial_ddl_recovery_is_executable_read_only_and_forbids_blind_stamp() -> None:
    script = OPERATIONS / "inspect-mysql-partial-ddl.sh"
    content = _read(script.name)

    assert content.startswith("#!/bin/sh\n")
    assert os.access(script, os.X_OK)
    assert "docker compose stop app" in content
    assert "alembic -c alembic-auth.ini current" in content
    assert "alembic -c alembic-auth.ini show" in content
    assert "information_schema.tables" in content
    assert "information_schema.columns" in content
    assert "information_schema.statistics" in content
    assert "information_schema.table_constraints" in content
    assert "information_schema.key_column_usage" in content
    assert "alembic stamp" not in content


def test_legacy_owner_migration_has_rollback_preview_and_guarded_apply() -> None:
    preview = _read("legacy-owner-preview.sql")
    apply = _read("legacy-owner-apply.sql")

    assert "BEGIN TRANSACTION READ ONLY" in preview
    assert "candidate_count" in preview
    assert preview.rstrip().endswith("ROLLBACK;")
    assert "UPDATE " not in preview

    assert "EXPECTED_COUNT" in apply
    assert "BACKUP_REFERENCE" in apply
    assert ":'BACKUP_REFERENCE' !~ '^[[:space:]]*$'" in apply
    assert "backup_reference_is_valid" in apply
    assert "LEGACY_OWNER' <> :'TARGET_OWNER" in apply
    assert "LOCK TABLE conversations, agent_traces" in apply
    assert "candidate_count" in apply
    assert "updated_count" in apply
    assert "\\quit" in apply
    assert apply.index("candidate_count") < apply.index("UPDATE conversations")
    assert apply.rstrip().endswith("COMMIT;")


def test_coordinated_recovery_scripts_cover_all_stores_and_restore_in_order() -> None:
    backup_path = OPERATIONS / "backup-all.sh"
    restore_path = OPERATIONS / "restore-all.sh"
    rollback_path = OPERATIONS / "rollback-image.sh"
    backup = _read(backup_path.name)
    restore = _read(restore_path.name)
    rollback = _read(rollback_path.name)

    for script in (backup_path, restore_path, rollback_path):
        assert script.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
        assert os.access(script, os.X_OK)

    for expected in (
        "mysqldump --single-transaction",
        "pg_dump",
        "chroma",
        "memory-chroma",
        "redis-cli SAVE",
        "alembic-current-postgres.txt",
        "alembic-current-mysql.txt",
        "metro-agent-image.txt",
        "owner-counts-before.txt",
    ):
        assert expected in backup

    mysql = restore.index("RESTORE_STEP=MySQL")
    postgres = restore.index("RESTORE_STEP=PostgreSQL")
    chroma = restore.index("RESTORE_STEP=Chroma")
    redis = restore.index("RESTORE_STEP=Redis")
    assert mysql < postgres < chroma < redis
    assert "metro-agent-image.txt" in restore
    assert "sha256sum -c SHA256SUMS" in restore
    assert "@sha256:[0-9a-f]{64}" in restore
    assert restore.index("export METRO_AGENT_IMAGE") < restore.index("docker compose")
    assert "docker compose up -d --wait mysql postgres" in restore
    assert "GRANT ALL PRIVILEGES ON metro_auth.* TO 'metro_auth'@'%'" in restore
    for expected in (
        "alembic-current-postgres.txt",
        "alembic-current-mysql.txt",
        "owner-counts-after.txt",
        "cmp",
        "/api/ready",
    ):
        assert expected in restore

    assert "METRO_AGENT_IMAGE" in rollback
    assert "@sha256:" in rollback
    assert "case" in rollback
    assert "--no-build" in rollback
