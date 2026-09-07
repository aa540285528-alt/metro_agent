from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from chromadb.errors import NotFoundError

from metro_agent.knowledge.releases import (
    INDEX_REGISTRY_COLLECTION_NAME,
    ReleasePointer,
    ReleaseValidationError,
    create_validated_release,
    publish_validated_release,
    read_release_pointer,
    restore_validated_release,
    rollback_release_pointer,
)
from metro_agent.knowledge.publication_lock import PublicationLockError
from metro_agent.tools.knowledge_index_registry import (
    KnowledgeIndexUnavailableError,
    clear_published_collection_name,
    publish_collection_name,
)


class _Collection:
    def __init__(self, *, count: int = 1) -> None:
        self.record: dict[str, object] | None = None
        self.upserts: list[dict[str, object]] = []
        self.count_value = count

    def upsert(self, **kwargs: object) -> None:
        self.upserts.append(kwargs)
        self.record = dict(kwargs["metadatas"][0])  # type: ignore[index]

    def get(self, **_: object) -> dict[str, object]:
        return {"metadatas": [self.record] if self.record else []}

    def count(self) -> int:
        return self.count_value


class _TracingCollection(_Collection):
    def __init__(self, trace: list[str], *, count: int = 1) -> None:
        super().__init__(count=count)
        self.trace = trace

    def upsert(self, **kwargs: object) -> None:
        self.trace.append("upsert")
        super().upsert(**kwargs)


class _Client:
    def __init__(self) -> None:
        self.collection = _Collection()

    def get_or_create_collection(self, _: str) -> _Collection:
        return self.collection

    def get_collection(self, _: str) -> _Collection:
        return self.collection


class _CollectionsClient:
    def __init__(self, *, registry: _Collection, collections: dict[str, _Collection]) -> None:
        self.collection = registry
        self._collections = collections

    def get_or_create_collection(self, name: str) -> _Collection:
        if name == INDEX_REGISTRY_COLLECTION_NAME:
            return self.collection
        return self._collections.setdefault(name, _Collection())

    def get_collection(self, name: str) -> _Collection:
        if name == INDEX_REGISTRY_COLLECTION_NAME:
            return self.collection
        return self._collections[name]


def _descriptor(build_id: str) -> dict[str, object]:
    return {
        "index_build_id": build_id,
        "collection_name": f"metro__build_{build_id}",
        "source_tree_sha256": "a" * 64,
        "source_revision": "a" * 64,
        "provenance": {
            "image_version": "image",
            "code_version": "test",
            "python_version": "3.12",
            "chroma_client_version": "1.5.9",
            "chroma_server_version": "1.5.9",
            "embedding_model_id": "embedding",
            "reranker_model_id": "reranker",
            "chunker_config": {"parser": "markdown"},
        },
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


def test_restore_validated_release_swaps_the_selected_history_to_current_and_previous(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    client = _CollectionsClient(
        registry=_TracingCollection(trace),
        collections={
            "metro__build_first": _Collection(),
            "metro__build_second": _Collection(),
            "metro__build_third": _Collection(),
        },
    )
    first = create_validated_release(tmp_path, _descriptor("first"))
    second = create_validated_release(tmp_path, _descriptor("second"))
    third = create_validated_release(tmp_path, _descriptor("third"))
    publish_validated_release(client, first)
    publish_validated_release(client, second)
    publish_validated_release(client, third)

    trace.clear()
    restored = restore_validated_release(
        client,
        tmp_path,
        "first",
        before_publish=lambda: trace.append("before"),
    )

    assert restored == ReleasePointer(
        current_build_id="first",
        current_collection_name="metro__build_first",
        current_artifact_sha256=first.sha256,
        previous_build_id="third",
        previous_collection_name="metro__build_third",
        previous_artifact_sha256=third.sha256,
    )
    assert trace == ["before", "upsert"]
    assert len(client.collection.upserts) == 4


def test_rollback_rejects_missing_or_empty_previous_collection(tmp_path: Path) -> None:
    client = _Client()
    first = create_validated_release(tmp_path, _descriptor("first"))
    publish_validated_release(client, first)

    with pytest.raises(ReleaseValidationError, match="previous"):
        rollback_release_pointer(client, tmp_path)


def test_restore_rejects_an_empty_target_collection_before_pointer_mutation(
    tmp_path: Path,
) -> None:
    client = _CollectionsClient(
        registry=_Collection(),
        collections={"metro__build_first": _Collection(count=0)},
    )
    create_validated_release(tmp_path, _descriptor("first"))

    with pytest.raises(ReleaseValidationError, match="empty"):
        restore_validated_release(client, tmp_path, "first")

    assert len(client.collection.upserts) == 0


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


def test_read_pointer_returns_none_only_for_an_absent_record() -> None:
    client = _Client()
    assert read_release_pointer(client, required=False) is None

    client.collection.record = {"current_build_id": "malformed"}
    with pytest.raises(ReleaseValidationError, match="current_collection_name"):
        read_release_pointer(client, required=False)


def test_publish_fails_closed_when_registry_is_unavailable(tmp_path: Path) -> None:
    release = create_validated_release(tmp_path, _descriptor("first"))

    with pytest.raises(ReleaseValidationError, match="pointer is unavailable"):
        publish_validated_release(object(), release)


def test_publish_creates_only_a_truly_absent_registry_before_first_pointer(tmp_path: Path) -> None:
    class MissingRegistryClient(_Client):
        def get_collection(self, _: str) -> _Collection:
            raise NotFoundError("Collection Metro_Knowledge_Index_Registry_v1 does not exist")

    client = MissingRegistryClient()
    release = create_validated_release(tmp_path, _descriptor("first"))

    pointer = publish_validated_release(client, release)

    assert pointer.current_build_id == "first"
    assert len(client.collection.upserts) == 1


def test_publish_does_not_create_or_overwrite_after_an_unavailable_registry_error(tmp_path: Path) -> None:
    class UnavailableRegistryClient(_Client):
        def get_collection(self, _: str) -> _Collection:
            raise RuntimeError("service unavailable")

    client = UnavailableRegistryClient()
    release = create_validated_release(tmp_path, _descriptor("first"))

    with pytest.raises(ReleaseValidationError, match="pointer is unavailable"):
        publish_validated_release(client, release)
    assert client.collection.upserts == []


def test_validated_descriptor_requires_complete_typed_provenance(tmp_path: Path) -> None:
    descriptor = _descriptor("first")
    descriptor["provenance"] = {"code_version": "only-one-field"}

    with pytest.raises(ReleaseValidationError, match="image_version"):
        create_validated_release(tmp_path, descriptor)


def test_rollback_does_not_upsert_after_a_prepublication_lease_loss(tmp_path: Path) -> None:
    client = _Client()
    first = create_validated_release(tmp_path, _descriptor("first"))
    second = create_validated_release(tmp_path, _descriptor("second"))
    publish_validated_release(client, first)
    publish_validated_release(client, second)

    def lose_lease() -> None:
        raise PublicationLockError("publication lock renew lost ownership")

    with pytest.raises(PublicationLockError, match="renew"):
        rollback_release_pointer(client, tmp_path, before_publish=lose_lease)
    assert len(client.collection.upserts) == 2


def test_restore_rejects_a_mismatched_descriptor_before_pointer_mutation(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    client = _CollectionsClient(
        registry=_TracingCollection(trace),
        collections={"metro__build_first": _Collection()},
    )
    release = create_validated_release(tmp_path, _descriptor("first"))
    release.path.write_text(
        json.dumps(
            {
                **_descriptor("first"),
                "collection_name": "metro__build_other",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseValidationError, match="collection_name"):
        restore_validated_release(client, tmp_path, "first")

    assert trace == []
    assert len(client.collection.upserts) == 0
