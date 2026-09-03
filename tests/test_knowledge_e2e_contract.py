from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "deploy" / "operations"


def _compose() -> dict[str, object]:
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def test_backup_archives_complete_knowledge_release_unit_under_publication_lock() -> None:
    backup = (OPERATIONS / "backup-all.sh").read_text(encoding="utf-8")
    lock_helper = OPERATIONS / "with-knowledge-publication-lock.py"
    compose = _compose()

    assert lock_helper.exists()
    assert "knowledge-chroma.tar.gz" in backup
    assert "knowledge-artifacts.tar.gz" in backup
    assert "knowledge:publication" in lock_helper.read_text(encoding="utf-8")
    assert "PublicationLock" in lock_helper.read_text(encoding="utf-8")
    assert "docker compose stop app knowledge-read-proxy chroma" in backup
    assert "knowledge-indexer" in backup
    # The backup container sees the Chroma volume root at /chroma; the running
    # Chroma service mounts that same root at /chroma/chroma.  Archive the
    # former as the latter so restore extracts directly into the live mount.
    assert compose["services"]["knowledge-backup"]["volumes"] == [
        "knowledge_chroma_data:/chroma:ro",
        "knowledge_artifact_data:/var/lib/metro-agent/knowledge-artifacts:ro",
    ]
    assert "a.add('/chroma',arcname='chroma')" in backup
    assert "safe-restore-tar.py replace-volume-contents /backup/knowledge-chroma.tar.gz" in (
        OPERATIONS / "restore-all.sh"
    ).read_text(encoding="utf-8")


def test_backup_holds_one_publication_token_before_stopping_knowledge_services() -> None:
    backup = (OPERATIONS / "backup-all.sh").read_text(encoding="utf-8")
    lock_helper = (OPERATIONS / "with-knowledge-publication-lock.py").read_text(
        encoding="utf-8"
    )

    assert " hold " in backup
    assert "verify-token" in backup
    assert backup.index("hold") < backup.index("knowledge-indexer")
    assert backup.index("hold") < backup.index("docker compose stop app knowledge-read-proxy chroma")
    assert backup.index("knowledge-chroma.tar.gz") < backup.index("SHA256SUMS")
    assert "trap release_publication_lock EXIT HUP INT TERM" in backup
    assert ": > \"$LOCK_RELEASE_FILE\"" in backup
    assert "def hold_lock" in lock_helper
    assert "def verify_token" in lock_helper
    assert "PublicationLock" in lock_helper
    # A process boundary cannot trust an ordinary GET: it must atomically prove
    # token ownership while renewing the same bounded publication lease.
    assert "RENEW_SCRIPT" in lock_helper
    assert "_redis_client().eval(" in lock_helper
    assert "PUBLICATION_LOCK_KEY," in lock_helper
    assert "MAX_TTL_SECONDS * 1000" in lock_helper
    # The lock may expire while checksums are being written, so prove ownership
    # again after the checksum manifest is complete.
    checksum = backup.index("SHA256SUMS")
    assert "verify_publication_lock" in backup[checksum:]
    # docker wait prints the holder's exit status; suppressing that output loses
    # a non-zero holder result and is therefore not a valid release check.
    assert 'lock_holder_status=$(docker wait "$LOCK_CONTAINER")' in backup
    assert '"${lock_holder_status:-1}" != "0"' in backup


def test_restore_requires_complete_knowledge_release_and_verifies_it_before_app() -> None:
    restore = (OPERATIONS / "restore-all.sh").read_text(encoding="utf-8")

    assert "knowledge-chroma.tar.gz" in restore
    assert "knowledge-artifacts.tar.gz" in restore
    assert "verify-restored-release" in restore
    assert restore.index("verify-restored-release") < restore.index("docker compose up -d --no-build app")


def test_restore_validates_archives_and_swaps_the_knowledge_volume_before_starting_proxy() -> None:
    restore = (OPERATIONS / "restore-all.sh").read_text(encoding="utf-8")
    helper = (OPERATIONS / "safe-restore-tar.py").read_text(encoding="utf-8")

    assert "safe-restore-tar.py" in restore
    assert "--expected-root chroma" in restore
    assert "--expected-root knowledge-artifacts" in restore
    assert "replace-volume-contents" in restore
    assert "merge-artifacts" in restore
    # Archive members are fully inspected before any extraction; never rely on
    # tar's default path/link behavior for an untrusted backup medium.
    for marker in ("is_absolute", "..", "issym", "islnk", "isdev", "extractall"):
        assert marker in helper
    assert "os.replace" in helper
    assert "rollback.previous" in helper
    assert "rmtree(destination" not in helper
    # Successful release verification gates proxy startup; app starts only after
    # that proxy is up and its heartbeat has answered.
    verify = restore.index("verify-restored-release")
    proxy = restore.index("docker compose up -d --wait knowledge-read-proxy")
    app = restore.index("docker compose up -d --no-build app")
    assert verify < proxy < app
    assert "docker compose exec -T knowledge-read-proxy" in restore
    assert "http://127.0.0.1:8000/api/v2/heartbeat" in restore


def test_e2e_profile_uses_only_deterministic_embedding_and_fixture() -> None:
    compose = _compose()
    service = compose["services"]["knowledge-e2e"]

    assert service["profiles"] == ["knowledge-e2e"]
    assert service["environment"]["KNOWLEDGE_E2E"] == "1"
    assert "DEEPSEEK_API_KEY" not in service["environment"]
    assert "GLM_API_KEY" not in service["environment"]
    assert "DASHSCOPE_API_KEY" not in service["environment"]
    assert "deterministic" in service["environment"]["KNOWLEDGE_EMBEDDER"].lower()
    assert any("fixtures/knowledge-e2e" in value and value.endswith(":ro") for value in service["volumes"])


def test_e2e_fixture_is_a_governed_versioned_knowledge_source() -> None:
    rules = ROOT / "fixtures" / "knowledge-e2e" / "rules.md"
    smoke = ROOT / "fixtures" / "knowledge-e2e" / "release-smoke-queries.jsonl"

    assert rules.exists() and smoke.exists()
    text = rules.read_text(encoding="utf-8")
    for field in ("owner:", "source:", "updated:", "effective_date:", "expires_at:", "risk_level:"):
        assert field in text
    assert '"expected_source":"rules.md"' in smoke.read_text(encoding="utf-8")


def test_compose_keeps_knowledge_chroma_unpublished_and_artifacts_retained() -> None:
    compose = _compose()
    services = compose["services"]
    assert "ports" not in services["chroma"]
    assert "knowledge_artifact_data" in compose["volumes"]

    documentation = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "README.md",
            "docs/operations/coordinated-backup-restore.md",
            "docs/operations/internal-pilot-auth-acceptance.md",
        )
    )
    assert "不自动清理" in documentation
    assert "加密由备份目标负责" in documentation
