from __future__ import annotations

import json
from pathlib import Path

import pytest

from metro_agent.tools.knowledge_indexer import (
    FORCE_REBUILD_REASONS,
    KnowledgeIndexer,
    OperationEvents,
    parse_args,
)


def test_cli_exposes_only_governed_commands_and_fixed_force_reasons() -> None:
    assert parse_args(["build-and-publish"]).command == "build-and-publish"
    assert parse_args(["status"]).command == "status"
    assert parse_args(["rollback"]).command == "rollback"
    assert parse_args(["verify"]).command == "verify"
    assert set(FORCE_REBUILD_REASONS) == {
        "indexer-upgrade",
        "embedding-model-change",
        "reranker-model-change",
        "chunker-change",
        "recovery",
    }
    with pytest.raises(SystemExit):
        parse_args(["build-and-publish", "--force-rebuild", "not-approved"])


def test_operation_events_are_uuid_immutable_and_capture_operator_context(tmp_path: Path) -> None:
    events = OperationEvents(tmp_path, operator_assertion="alice", host="node-a")
    operation_id = events.started(
        operation="build-and-publish",
        build_id="build-a",
        previous_build_id=None,
        source_revision="abc123",
        force_reason=None,
    )
    events.succeeded(operation_id, operation="build-and-publish", build_id="build-a")

    started = json.loads((tmp_path / "operations" / f"{operation_id}.started.json").read_text())
    succeeded = json.loads((tmp_path / "operations" / f"{operation_id}.succeeded.json").read_text())
    assert started["operator_assertion"] == "alice"
    assert started["host"] == "node-a"
    assert started["timestamp_utc"].endswith("Z")
    assert succeeded["operation"] == "build-and-publish"
    with pytest.raises(FileExistsError):
        events.succeeded(operation_id, operation="build-and-publish", build_id="build-a")


class _Pointer:
    current_build_id = "current"


def test_build_noops_when_source_and_provenance_match_current(monkeypatch, tmp_path: Path) -> None:
    indexer = KnowledgeIndexer(client=object(), artifact_root=tmp_path, redis_client=object())
    monkeypatch.setattr(indexer, "_validated_source", lambda: object())
    monkeypatch.setattr(indexer, "_source_sha", lambda source: "same-source")
    monkeypatch.setattr(indexer, "_provenance", lambda: {"code": "same"})
    monkeypatch.setattr(indexer, "_current_release_descriptor", lambda: {
        "source_tree_sha256": "same-source", "provenance": {"code": "same"}
    })
    monkeypatch.setattr(
        "metro_agent.tools.knowledge_indexer.read_release_pointer",
        lambda *_args, **_kwargs: _Pointer(),
    )

    assert indexer.build_and_publish(operator_assertion="alice") == {"status": "no-op", "build_id": "current"}
