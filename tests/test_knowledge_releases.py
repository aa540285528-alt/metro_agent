from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from metro_agent.knowledge.releases import (
    ReleasePointer,
    ReleaseValidationError,
    create_validated_release,
    publish_validated_release,
    read_release_pointer,
    rollback_release_pointer,
)
from metro_agent.tools.knowledge_index_registry import (
    KnowledgeIndexUnavailableError,
    clear_published_collection_name,
    publish_collection_name,
)


class _Collection:
    def __init__(self) -> None:
        self.record: dict[str, object] | None = None
        self.upserts: list[dict[str, object]] = []

    def upsert(self, **kwargs: object) -> None:
        self.upserts.append(kwargs)
        self.record = dict(kwargs["metadatas"][0])  # type: ignore[index]

    def get(self, **_: object) -> dict[str, object]:
        return {"metadatas": [self.record] if self.record else []}

    def count(self) -> int:
        return 1


class _Client:
    def __init__(self) -> None:
        self.collection = _Collection()

    def get_or_create_collection(self, _: str) -> _Collection:
        return self.collection

    def get_collection(self, _: str) -> _Collection:
        return self.collection


def _descriptor(build_id: str) -> dict[str, object]:
    return {
        "index_build_id": build_id,
        "collection_name": f"metro__build_{build_id}",
        "source_tree_sha256": "a" * 64,
        "provenance": {"code_version": "test", "chunker": "markdown"},
    }


def test_validated_release_is_canonical_immutable_and_sha_bound(tmp_path: Path) -> None:
    release = create_validated_release(tmp_path, _descriptor("first"))
    stored = release.path.read_bytes()

    assert release.path == tmp_path / "releases" / "first" / "validated.json"
    assert stored.endswith(b"\n")
    assert release.sha256 == hashlib.sha256(stored).hexdigest()
    assert json.loads(stored) == _descriptor("first")

    with pytest.raises(FileExistsError):
        create_validated_release(tmp_path, _descriptor("first"))


def test_publish_writes_current_and_previous_in_one_registry_upsert(tmp_path: Path) -> None:
    client = _Client()
    first = create_validated_release(tmp_path, _descriptor("first"))
    second = create_validated_release(tmp_path, _descriptor("second"))

    publish_validated_release(client, first)
    publish_validated_release(client, second)

    pointer = read_release_pointer(client)
    assert pointer == ReleasePointer(
        current_build_id="second",
        current_collection_name="metro__build_second",
        current_artifact_sha256=second.sha256,
        previous_build_id="first",
        previous_collection_name="metro__build_first",
        previous_artifact_sha256=first.sha256,
    )
    assert len(client.collection.upserts) == 2


def test_rollback_only_swaps_a_validated_nonempty_previous_release(tmp_path: Path) -> None:
    client = _Client()
    first = create_validated_release(tmp_path, _descriptor("first"))
    second = create_validated_release(tmp_path, _descriptor("second"))
    publish_validated_release(client, first)
    publish_validated_release(client, second)

    rolled_back = rollback_release_pointer(client, tmp_path)

    assert rolled_back.current_build_id == "first"
    assert rolled_back.previous_build_id == "second"
    assert len(client.collection.upserts) == 3


def test_rollback_rejects_missing_or_empty_previous_collection(tmp_path: Path) -> None:
    client = _Client()
    first = create_validated_release(tmp_path, _descriptor("first"))
    publish_validated_release(client, first)

    with pytest.raises(ReleaseValidationError, match="previous"):
        rollback_release_pointer(client, tmp_path)


def test_pointer_rejects_non_hex_artifact_digests() -> None:
    client = _Client()
    client.collection.record = {
        "current_build_id": "current",
        "current_collection_name": "metro__build_current",
        "current_artifact_sha256": "z" * 64,
        "previous_build_id": None,
        "previous_collection_name": None,
        "previous_artifact_sha256": None,
    }

    with pytest.raises(ReleaseValidationError, match="current_artifact_sha256"):
        read_release_pointer(client)


def test_legacy_registry_mutators_refuse_to_bypass_governed_indexer() -> None:
    client = _Client()

    with pytest.raises(KnowledgeIndexUnavailableError, match="governed knowledge_indexer"):
        publish_collection_name(client, "legacy-registry", "metro__build_legacy")
    with pytest.raises(KnowledgeIndexUnavailableError, match="governed knowledge_indexer"):
        clear_published_collection_name(client, "legacy-registry")

    assert client.collection.upserts == []
