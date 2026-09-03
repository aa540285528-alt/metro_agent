from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL_WRAPPER = ROOT / "deploy" / "operations" / "knowledge-admin.sh"
POWERSHELL_WRAPPER = ROOT / "deploy" / "operations" / "knowledge-admin.ps1"


def test_shell_wrapper_requires_effective_root_and_has_no_environment_admin_bypass() -> None:
    content = SHELL_WRAPPER.read_text(encoding="utf-8")

    assert 'id -u' in content
    assert "METRO_AGENT_KNOWLEDGE_ADMIN" not in content


def test_powershell_wrapper_uses_windows_administrator_role() -> None:
    content = POWERSHELL_WRAPPER.read_text(encoding="utf-8")

    assert "WindowsPrincipal" in content
    assert "WindowsBuiltInRole]::Administrator" in content
    assert "METRO_AGENT_KNOWLEDGE_ADMIN" not in content


def test_powershell_environment_variable_cannot_grant_admin_access() -> None:
    environment = os.environ | {"METRO_AGENT_KNOWLEDGE_ADMIN": "1"}
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_WRAPPER),
            "not-an-allowed-command",
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert result.returncode != 0
    assert "METRO_AGENT_KNOWLEDGE_ADMIN" not in (result.stdout + result.stderr)


def test_wrappers_whitelist_force_rebuild_and_reject_operator_override() -> None:
    shell = SHELL_WRAPPER.read_text(encoding="utf-8")
    powershell = POWERSHELL_WRAPPER.read_text(encoding="utf-8")

    for reason in (
        "indexer-upgrade",
        "embedding-model-change",
        "reranker-model-change",
        "chunker-change",
        "recovery",
    ):
        assert reason in shell
        assert reason in powershell
    for content in (shell, powershell):
        assert "--operator-assertion" in content
        assert "unknown or duplicate" in content
        assert '"$@"' not in content
        assert "$IndexerArguments" not in content
