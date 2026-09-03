from __future__ import annotations

import json
from pathlib import Path

import pytest
from chromadb.errors import NotFoundError

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
    with pytest.raises(SystemExit):
        parse_args(["build-and-publish", "--operator-assertion", "forged"])
    with pytest.raises(SystemExit):
        parse_args(["rollback", "--operator-assertion", "forged"])


def test_operation_events_are_uuid_immutable_and_capture_trusted_operator_context(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "metro_agent.tools.knowledge_indexer.trusted_operator_identity",
        lambda: "uid:root",
    )
    events = OperationEvents(tmp_path, host="node-a")
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
    assert started["operator_identity"] == "uid:root"
    assert started["host"] == "node-a"
    assert started["timestamp_utc"].endswith("Z")
    assert succeeded["operation"] == "build-and-publish"
    with pytest.raises(FileExistsError):
        events.succeeded(operation_id, operation="build-and-publish", build_id="build-a")


def test_each_operation_event_keeps_the_full_audit_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "metro_agent.tools.knowledge_indexer.trusted_operator_identity",
        lambda: "uid:root",
    )
    events = OperationEvents(tmp_path, host="node-a")
    operation_id = events.started(
        operation="build-and-publish",
        build_id="candidate",
        previous_build_id="previous",
        source_revision="abc123",
        force_reason="recovery",
    )
    events.succeeded(operation_id, operation="build-and-publish", build_id="candidate")
    events.failed(
        operation_id,
        operation="build-and-publish",
        build_id="candidate",
        error=RuntimeError("broken"),
    )

    records = {
        path.suffixes[-2].lstrip("."): json.loads(path.read_text())
        for path in (tmp_path / "operations").iterdir()
    }
    for record in records.values():
        assert record["operation"] == "build-and-publish"
        assert record["build_id"] == "candidate"
        assert record["current_build_id"] is None
        assert record["previous_build_id"] == "previous"
        assert record["source_revision"] == "abc123"
        assert record["force_reason"] == "recovery"
    assert records["failed"]["error_category"] == "RuntimeError"


def test_terminal_event_updates_the_current_and_previous_release_ids(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "metro_agent.tools.knowledge_indexer.trusted_operator_identity",
        lambda: "uid:root",
    )
    events = OperationEvents(tmp_path, host="node-a")
    operation_id = events.started(
        operation="rollback",
        build_id="old-current",
        current_build_id="old-current",
        previous_build_id="rollback-target",
        source_revision="abc123",
        force_reason=None,
    )

    events.succeeded(
        operation_id,
        operation="rollback",
        build_id="rollback-target",
        current_build_id="rollback-target",
        previous_build_id="old-current",
    )

    succeeded = json.loads((tmp_path / "operations" / f"{operation_id}.succeeded.json").read_text())
    assert succeeded["current_build_id"] == "rollback-target"
    assert succeeded["previous_build_id"] == "old-current"


def test_provenance_captures_runtime_versions_model_ids_and_chunker_config(monkeypatch) -> None:
    monkeypatch.setenv("METRO_AGENT_IMAGE", "registry/metro@sha256:image")
    monkeypatch.setenv("METRO_AGENT_CODE_VERSION", "abc123")
    monkeypatch.setenv("CHROMA_SERVER_VERSION", "1.5.9")
    monkeypatch.setenv("EMBEDDING_MODEL_PATH", "/models/embed-v2")
    monkeypatch.setenv("RERANK_MODEL_PATH", "/models/rerank-v3")

    provenance = KnowledgeIndexer._provenance()

    assert provenance["image_version"] == "registry/metro@sha256:image"
    assert provenance["code_version"] == "abc123"
    assert provenance["chroma_server_version"] == "1.5.9"
    assert provenance["embedding_model_id"] == "/models/embed-v2"
    assert provenance["reranker_model_id"] == "/models/rerank-v3"
    assert provenance["chroma_client_version"]
    assert provenance["chunker_config"]["parser"] == "MarkdownNodeParser"


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

    assert indexer.build_and_publish() == {"status": "no-op", "build_id": "current"}


def test_fresh_deployment_build_creates_the_missing_registry_and_publishes(monkeypatch, tmp_path: Path) -> None:
    class Registry:
        def __init__(self) -> None:
            self.upserts: list[dict[str, object]] = []

        def get(self, **_: object) -> dict[str, object]:
            return {"metadatas": []}

        def upsert(self, **kwargs: object) -> None:
            self.upserts.append(kwargs)

    class FreshClient:
        def __init__(self) -> None:
            self.registry = Registry()

        def get_collection(self, _: str) -> Registry:
            raise NotFoundError("Collection Metro_Knowledge_Index_Registry_v1 does not exist")

        def get_or_create_collection(self, _: str) -> Registry:
            return self.registry

    class Source:
        git_commit = None

    client = FreshClient()
    indexer = KnowledgeIndexer(client=client, artifact_root=tmp_path, redis_client=object())
    monkeypatch.setattr(indexer, "_validated_source", lambda: Source())
    monkeypatch.setattr(indexer, "_source_sha", lambda _source: "a" * 64)
    monkeypatch.setattr(indexer, "_build_collection", lambda _source: ("first", "metro__build_first"))
    monkeypatch.setattr(indexer, "_ensure_collection_nonempty", lambda _name: None)
    monkeypatch.setattr(indexer, "_run_smoke_queries", lambda _source, _name: None)
    monkeypatch.setattr("metro_agent.tools.knowledge_indexer.PublicationLock", _Lock)

    assert indexer.build_and_publish() == {"status": "published", "build_id": "first"}
    assert len(client.registry.upserts) == 1


def test_current_release_descriptor_rejects_unavailable_registry() -> None:
    class UnavailableClient:
        def get_collection(self, _: str) -> object:
            raise RuntimeError("service unavailable")

    indexer = KnowledgeIndexer(client=UnavailableClient(), artifact_root="artifacts", redis_client=object())

    with pytest.raises(Exception, match="pointer is unavailable"):
        indexer._current_release_descriptor()


class _Lock:
    def __init__(self, *_: object) -> None:
        pass

    def __enter__(self) -> _Lock:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def assert_held(self) -> None:
        pass


class _RollbackPointer:
    current_build_id = "old-current"
    previous_build_id = "rollback-target"


def test_rollback_acquires_lock_before_reading_pointer_or_writing_started_event(monkeypatch, tmp_path: Path) -> None:
    trace: list[str] = []

    class Lock:
        def __init__(self, *_: object) -> None:
            pass

        def __enter__(self) -> Lock:
            trace.append("acquire")
            return self

        def __exit__(self, *_: object) -> None:
            trace.append("release")

        def assert_held(self) -> None:
            trace.append("assert")

    class Events:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def started(self, **_: object) -> str:
            trace.append("started")
            return "operation-id"

        def succeeded(self, *_: object, **__: object) -> None:
            trace.append("succeeded")

        def failed(self, *_: object, **__: object) -> None:
            trace.append("failed")

    def read_pointer(*_: object, **__: object) -> _RollbackPointer:
        trace.append("read")
        return _RollbackPointer()

    def rollback(*_: object, before_publish: object, **__: object) -> _RollbackPointer:
        trace.append("rollback")
        before_publish()
        return _RollbackPointer()

    indexer = KnowledgeIndexer(client=object(), artifact_root=tmp_path, redis_client=object())
    monkeypatch.setattr("metro_agent.tools.knowledge_indexer.PublicationLock", Lock)
    monkeypatch.setattr("metro_agent.tools.knowledge_indexer.OperationEvents", Events)
    monkeypatch.setattr("metro_agent.tools.knowledge_indexer.read_release_pointer", read_pointer)
    monkeypatch.setattr("metro_agent.tools.knowledge_indexer.rollback_release_pointer", rollback)

    assert indexer.rollback() == {"status": "rolled-back", "build_id": "old-current"}
    assert trace == ["acquire", "read", "started", "rollback", "assert", "release", "succeeded"]


def test_recheck_after_lock_returns_noop_before_creating_operation_events(monkeypatch, tmp_path: Path) -> None:
    indexer = KnowledgeIndexer(client=object(), artifact_root=tmp_path, redis_client=object())
    monkeypatch.setattr(indexer, "_validated_source", lambda: object())
    monkeypatch.setattr(indexer, "_source_sha", lambda source: "same")
    monkeypatch.setattr(indexer, "_provenance", lambda: {"code": "same"})
    descriptors = iter((
        {"source_tree_sha256": "other", "provenance": {"code": "same"}},
        {"source_tree_sha256": "same", "provenance": {"code": "same"}},
    ))
    monkeypatch.setattr(indexer, "_current_release_descriptor", lambda: next(descriptors))
    monkeypatch.setattr(
        "metro_agent.tools.knowledge_indexer.read_release_pointer",
        lambda *_args, **_kwargs: _Pointer(),
    )
    monkeypatch.setattr("metro_agent.tools.knowledge_indexer.PublicationLock", _Lock)

    assert indexer.build_and_publish() == {"status": "no-op", "build_id": "current"}
    assert not (tmp_path / "operations").exists()


def test_legacy_publication_functions_explicitly_refuse_bypass() -> None:
    legacy = (Path(__file__).parents[1] / "src" / "metro_agent" / "tools" / "build_obsidian_index.py").read_text(encoding="utf-8")
    build_source = legacy[legacy.index("def build_index"):legacy.index("def _restore_registry_pointer")]
    reconcile_source = legacy[legacy.index("def reconcile_publish_uncertain"):legacy.index("def _read_registry_pointer_or_none")]

    assert "raise IndexBuildError" in build_source
    assert "raise IndexBuildError" in reconcile_source
    assert "publish_collection_name(" not in build_source
    assert "clear_published_collection_name(" not in build_source
    assert "publish_collection_name(" not in reconcile_source
    assert "clear_published_collection_name(" not in reconcile_source
