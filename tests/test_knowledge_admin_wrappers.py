from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL_WRAPPER = ROOT / "deploy" / "operations" / "knowledge-admin.sh"
POWERSHELL_WRAPPER = ROOT / "deploy" / "operations" / "knowledge-admin.ps1"
POWERSHELL_AUDIT_MODULE = ROOT / "deploy" / "operations" / "knowledge-admin-audit.psm1"
GIT_SH = Path(r"C:\Program Files\Git\usr\bin\sh.exe")


def test_shell_wrapper_requires_effective_root_and_has_no_environment_admin_bypass() -> None:
    content = SHELL_WRAPPER.read_text(encoding="utf-8")

    assert 'id -u' in content
    assert 'operator="$(/usr/bin/id -un)"' in content
    assert "SUDO_USER" not in content
    assert "${USER" not in content
    assert "METRO_AGENT_KNOWLEDGE_ADMIN" not in content
    assert "/var/log/metro-agent" in content
    assert "install -d -m 0700" in content
    assert "chmod 0600" in content
    assert "date -u" in content
    assert "parameter_status" in content
    lines = content.splitlines()
    assert lines[:2] == ["#!/bin/sh", "PATH=/usr/sbin:/usr/bin:/sbin:/bin"]
    assert 'operator="$(/usr/bin/id -un)"' in content
    assert 'if [ "$(/usr/bin/id -u)" -ne 0 ]; then' in content


def test_powershell_wrapper_uses_windows_administrator_role() -> None:
    content = POWERSHELL_WRAPPER.read_text(encoding="utf-8")
    audit_module = POWERSHELL_AUDIT_MODULE.read_text(encoding="utf-8")

    assert "WindowsPrincipal" in content
    assert "WindowsBuiltInRole]::Administrator" in content
    assert "$principal.Identity.Name" in content
    assert "$env:USERNAME" not in content
    assert "METRO_AGENT_KNOWLEDGE_ADMIN" not in content
    assert "CommonApplicationData" in audit_module
    assert "Add-Content" not in audit_module
    assert "parameter_status" in audit_module
    assert "Write-Verbose" not in content


def test_shell_wrapper_ignores_an_attacker_controlled_path(tmp_path: Path) -> None:
    malicious = tmp_path / "malicious"
    malicious.mkdir()
    marker = tmp_path / "used-malicious-id"
    fake_id = malicious / "id"
    fake_id.write_text(f"#!/bin/sh\ntouch '{marker}'\necho 0\n", encoding="utf-8")

    result = subprocess.run(
        [str(GIT_SH), str(SHELL_WRAPPER), "status"],
        capture_output=True,
        text=True,
        env=os.environ | {"PATH": str(malicious)},
        check=False,
    )

    assert result.returncode == 77
    assert not marker.exists()


def test_powershell_audit_line_has_only_the_approved_fields() -> None:
    command = (
        f"Import-Module '{POWERSHELL_AUDIT_MODULE}'; "
        "New-KnowledgeAdminAuditLine -Identity 'trusted-admin' "
        "-Operation 'verify' -ParameterStatus 'none'"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    import json

    record = json.loads(result.stdout)
    assert set(record) == {
        "timestamp_utc",
        "operator_identity",
        "command",
        "parameter_status",
    }
    assert record["timestamp_utc"].endswith("Z")


def test_powershell_audit_module_rejects_a_junction_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    create = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"New-Item -ItemType Junction -Path '{junction}' -Target '{target}' | Out-Null",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert create.returncode == 0, create.stderr
    command = (
        f"Import-Module '{POWERSHELL_AUDIT_MODULE}'; "
        f"Assert-KnowledgeAdminAuditAncestors -Path '{junction / 'child'}'"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unsafe" in (result.stdout + result.stderr)


def test_powershell_audit_module_fails_closed_instead_of_creating_a_racy_path() -> None:
    content = POWERSHELL_AUDIT_MODULE.read_text(encoding="utf-8")

    assert "New-Item -ItemType Directory" not in content
    assert "New-Item -ItemType File" not in content
    assert "[IO.FileMode]::Open," in content
    assert "[IO.FileOptions]::OpenReparsePoint" in content
    assert "Add-Content" not in content


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
        assert "unknown or duplicate" in content
        assert '"$@"' not in content
        assert "$IndexerArguments" not in content
        assert "--operator-assertion" not in content
