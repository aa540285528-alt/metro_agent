"""Immutable descriptors and the single published-knowledge pointer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from chromadb.errors import NotFoundError


INDEX_REGISTRY_COLLECTION_NAME = "Metro_Knowledge_Index_Registry_v1"
PUBLISHED_INDEX_ID = "published"
PROVENANCE_TEXT_FIELDS = (
    "image_version",
    "code_version",
    "python_version",
    "chroma_client_version",
    "chroma_server_version",
    "embedding_model_id",
    "reranker_model_id",
)


class ReleaseValidationError(RuntimeError):
    """A release descriptor or publication pointer is not safe to use."""


@dataclass(frozen=True)
class ValidatedRelease:
    build_id: str
    collection_name: str
    source_tree_sha256: str
    provenance: Mapping[str, Any]
    path: Path
    sha256: str
    descriptor: Mapping[str, Any]


@dataclass(frozen=True)
class ReleasePointer:
    current_build_id: str
    current_collection_name: str
    current_artifact_sha256: str
    previous_build_id: str | None = None
    previous_collection_name: str | None = None
    previous_artifact_sha256: str | None = None


def create_validated_release(
    artifact_root: Path | str, descriptor: Mapping[str, Any]
) -> ValidatedRelease:
    """Persist a canonical descriptor once; artifacts are never rewritten."""
    payload = _validated_descriptor(descriptor)
    build_id = str(payload["index_build_id"])
    target = Path(artifact_root) / "releases" / build_id / "validated.json"
    target.parent.mkdir(parents=True, exist_ok=False)
    content = _canonical_json(payload)
    try:
        with target.open("xb") as handle:
            handle.write(content)
    except Exception:
        # The parent is retained as evidence of a failed creation; never overwrite it.
        raise
    return _release_from_payload(target, payload, content)


def read_validated_release(artifact_root: Path | str, build_id: str) -> ValidatedRelease:
    _validate_build_id(build_id)
    target = Path(artifact_root) / "releases" / build_id / "validated.json"
    try:
        content = target.read_bytes()
        payload = json.loads(content)
    except (OSError, ValueError, TypeError) as exc:
        raise ReleaseValidationError(f"validated release {build_id} cannot be read") from exc
    if not isinstance(payload, dict):
        raise ReleaseValidationError(f"validated release {build_id} is not an object")
    release = _release_from_payload(target, payload, content)
    if release.build_id != build_id or _canonical_json(payload) != content:
        raise ReleaseValidationError(f"validated release {build_id} is not canonical")
    return release


def publish_validated_release(client: Any, release: ValidatedRelease) -> ReleasePointer:
    """Atomically change visibility by upserting the one published record."""
    try:
        registry = client.get_collection(INDEX_REGISTRY_COLLECTION_NAME)
    except NotFoundError:
        try:
            registry = client.get_or_create_collection(INDEX_REGISTRY_COLLECTION_NAME)
        except Exception as exc:
            raise ReleaseValidationError("published release pointer is unavailable") from exc
    except Exception as exc:
        raise ReleaseValidationError("published release pointer is unavailable") from exc
    current = _read_pointer_from_registry(registry, required=False)
    pointer = ReleasePointer(
        current_build_id=release.build_id,
        current_collection_name=release.collection_name,
        current_artifact_sha256=release.sha256,
        previous_build_id=current.current_build_id if current else None,
        previous_collection_name=current.current_collection_name if current else None,
        previous_artifact_sha256=current.current_artifact_sha256 if current else None,
    )
    _upsert_pointer(client, pointer)
    return pointer


def read_release_pointer(client: Any, *, required: bool = True) -> ReleasePointer | None:
    try:
        registry = client.get_collection(INDEX_REGISTRY_COLLECTION_NAME)
    except NotFoundError as exc:
        if not required:
            return None
        raise ReleaseValidationError("published release pointer is unavailable") from exc
    except Exception as exc:
        raise ReleaseValidationError("published release pointer is unavailable") from exc
    return _read_pointer_from_registry(registry, required=required)


def _read_pointer_from_registry(registry: Any, *, required: bool) -> ReleasePointer | None:
    try:
        record = registry.get(ids=[PUBLISHED_INDEX_ID], include=["metadatas"])
    except Exception as exc:
        raise ReleaseValidationError("published release pointer is unavailable") from exc
    if not isinstance(record, Mapping):
        raise ReleaseValidationError("published release pointer record is corrupt")
    metadatas = record.get("metadatas")
    if not isinstance(metadatas, list):
        raise ReleaseValidationError("published release pointer record is corrupt")
    if not metadatas:
        if required:
            raise ReleaseValidationError("published release pointer is unavailable")
        return None
    metadata = metadatas[0]
    if not isinstance(metadata, Mapping):
        raise ReleaseValidationError("published release pointer record is corrupt")
    try:
        return ReleasePointer(
            current_build_id=_required_text(metadata, "current_build_id"),
            current_collection_name=_required_text(metadata, "current_collection_name"),
            current_artifact_sha256=_required_sha(metadata, "current_artifact_sha256"),
            previous_build_id=_optional_text(metadata, "previous_build_id"),
            previous_collection_name=_optional_text(metadata, "previous_collection_name"),
            previous_artifact_sha256=_optional_sha(metadata, "previous_artifact_sha256"),
        )
    except ReleaseValidationError:
        raise


def rollback_release_pointer(
    client: Any,
    artifact_root: Path | str,
    *,
    before_publish: Callable[[], None] | None = None,
) -> ReleasePointer:
    """Swap current and previous only when the previous artifact still validates."""
    pointer = read_release_pointer(client)
    assert pointer is not None
    if not pointer.previous_build_id or not pointer.previous_collection_name or not pointer.previous_artifact_sha256:
        raise ReleaseValidationError("published release has no validated previous version")
    previous = read_validated_release(artifact_root, pointer.previous_build_id)
    if previous.sha256 != pointer.previous_artifact_sha256:
        raise ReleaseValidationError("previous validated artifact digest does not match pointer")
    if previous.collection_name != pointer.previous_collection_name:
        raise ReleaseValidationError("previous validated artifact collection does not match pointer")
    _ensure_collection_nonempty(client, previous.collection_name)
    swapped = ReleasePointer(
        current_build_id=previous.build_id,
        current_collection_name=previous.collection_name,
        current_artifact_sha256=previous.sha256,
        previous_build_id=pointer.current_build_id,
        previous_collection_name=pointer.current_collection_name,
        previous_artifact_sha256=pointer.current_artifact_sha256,
    )
    if before_publish is not None:
        before_publish()
    _upsert_pointer(client, swapped)
    return swapped


def _upsert_pointer(client: Any, pointer: ReleasePointer) -> None:
    metadata = {
        "current_build_id": pointer.current_build_id,
        "current_collection_name": pointer.current_collection_name,
        "current_artifact_sha256": pointer.current_artifact_sha256,
        "previous_build_id": pointer.previous_build_id,
        "previous_collection_name": pointer.previous_collection_name,
        "previous_artifact_sha256": pointer.previous_artifact_sha256,
    }
    client.get_or_create_collection(INDEX_REGISTRY_COLLECTION_NAME).upsert(
        ids=[PUBLISHED_INDEX_ID],
        documents=["published index"],
        embeddings=[[0.0]],
        metadatas=[metadata],
    )


def _ensure_collection_nonempty(client: Any, collection_name: str) -> None:
    try:
        collection = client.get_collection(collection_name)
        if collection.count() <= 0:
            raise ReleaseValidationError("previous collection is empty")
    except ReleaseValidationError:
        raise
    except Exception as exc:
        raise ReleaseValidationError("previous collection is unavailable") from exc


def _validated_descriptor(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(descriptor, Mapping):
        raise ReleaseValidationError("validated descriptor must be an object")
    result = dict(descriptor)
    build_id = result.get("index_build_id")
    collection_name = result.get("collection_name")
    source_sha = result.get("source_tree_sha256")
    provenance = result.get("provenance")
    source_revision = result.get("source_revision")
    git_commit = result.get("git_commit")
    if not isinstance(build_id, str):
        raise ReleaseValidationError("index_build_id is required")
    _validate_build_id(build_id)
    if not isinstance(collection_name, str) or not collection_name.endswith(f"__build_{build_id}"):
        raise ReleaseValidationError("collection_name must bind to index_build_id")
    if not _is_sha256(source_sha):
        raise ReleaseValidationError("source_tree_sha256 must be a SHA-256 digest")
    if not isinstance(provenance, Mapping):
        raise ReleaseValidationError("provenance must be an object")
    for field in PROVENANCE_TEXT_FIELDS:
        value = provenance.get(field)
        if not isinstance(value, str) or not value:
            raise ReleaseValidationError(f"provenance.{field} must be a non-empty string")
    chunker_config = provenance.get("chunker_config")
    if not isinstance(chunker_config, Mapping) or not chunker_config:
        raise ReleaseValidationError("provenance.chunker_config must be a non-empty object")
    if not isinstance(source_revision, str) or not source_revision:
        raise ReleaseValidationError("source_revision must be a non-empty string")
    if git_commit is not None and (not isinstance(git_commit, str) or not git_commit):
        raise ReleaseValidationError("git_commit must be a non-empty string when present")
    return result


def _release_from_payload(path: Path, payload: Mapping[str, Any], content: bytes) -> ValidatedRelease:
    descriptor = _validated_descriptor(payload)
    return ValidatedRelease(
        build_id=str(descriptor["index_build_id"]),
        collection_name=str(descriptor["collection_name"]),
        source_tree_sha256=str(descriptor["source_tree_sha256"]),
        provenance=dict(descriptor["provenance"]),
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        descriptor=descriptor,
    )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_build_id(build_id: str) -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if not build_id or any(character not in allowed for character in build_id):
        raise ReleaseValidationError("index_build_id contains invalid characters")


def _required_text(metadata: Mapping[str, Any], name: str) -> str:
    value = metadata.get(name)
    if not isinstance(value, str) or not value:
        raise ReleaseValidationError(f"published pointer has no {name}")
    return value


def _optional_text(metadata: Mapping[str, Any], name: str) -> str | None:
    value = metadata.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ReleaseValidationError(f"published pointer has invalid {name}")
    return value


def _required_sha(metadata: Mapping[str, Any], name: str) -> str:
    value = _required_text(metadata, name)
    if not _is_sha256(value):
        raise ReleaseValidationError(f"published pointer has invalid {name}")
    return value


def _optional_sha(metadata: Mapping[str, Any], name: str) -> str | None:
    value = _optional_text(metadata, name)
    if value is not None and not _is_sha256(value):
        raise ReleaseValidationError(f"published pointer has invalid {name}")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )
