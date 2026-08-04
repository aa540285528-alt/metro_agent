"""Inspectable, immutable artifacts for an offline knowledge-index build."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from metro_agent.observability.artifacts import ArtifactRef, write_json, write_jsonl, write_markdown


TOKENIZER_NAME = "cl100k_base"
CHUNK_ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_CHUNKER_CONFIG = {
    "parser": "MarkdownNodeParser",
    "overlap_char_count": 0,
    "overlap_strategy": "none",
    "overlap_definition": "MarkdownNodeParser emits semantic chunks without configured overlap.",
}


class ChunkManifestUnavailableError(RuntimeError):
    """Raised when a chunk manifest is not a published evaluator input."""


@dataclass(frozen=True)
class ChunkArtifactResult:
    index_build_id: str
    manifest_uri: str
    manifest_sha256: str
    chunks_uri: str
    chunks_sha256: str
    preview_uri: str
    preview_sha256: str


def assign_deterministic_chunk_ids(nodes: Iterable[Any]) -> list[Any]:
    """Replace parser-generated IDs with IDs stable for the same source and text."""
    assigned = list(nodes)
    grouped: dict[str, list[tuple[Any, tuple[int, int, int]]]] = {}
    source_local_orders: dict[str, int] = {}
    for node in assigned:
        metadata = _metadata(node)
        source_path = _source_path(metadata)
        source_local_order = source_local_orders.get(source_path, 0)
        source_local_orders[source_path] = source_local_order + 1
        header_path = _header_path(metadata)
        start, end = _source_char_span(node, metadata)
        normalized_text = _normalize_text(_node_text(node))
        text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        material = "\n".join(
            (source_path, header_path, str(start), str(end), text_hash)
        )
        base_id = f"chunk-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"
        grouped.setdefault(base_id, []).append((node, (start, end, source_local_order)))

    for base_id, duplicates in grouped.items():
        duplicates.sort(key=lambda item: item[1])
        for occurrence, (node, _) in enumerate(duplicates, start=1):
            chunk_id = base_id if occurrence == 1 else f"{base_id}-{occurrence}"
            _set_node_id(node, chunk_id)
            if len(duplicates) > 1:
                _set_collision_metadata(node, base_id, occurrence, len(duplicates))
    return assigned


def write_chunk_artifacts(
    *,
    artifact_root: Path | str,
    index_build_id: str,
    nodes: Iterable[Any],
    document_count: int,
    chunker_config: Mapping[str, Any] | None = None,
) -> ChunkArtifactResult:
    """Write a reviewable snapshot before its corresponding collection is published."""
    _validate_index_build_id(index_build_id)
    root, chunks_dir, build_dir = _bundle_paths(artifact_root, index_build_id)
    if build_dir.exists():
        raise FileExistsError(f"chunk artifacts already exist for build {index_build_id}")

    tokenizer, tokenizer_manifest = _load_tokenizer()
    normalized_chunker_config = _normalize_chunker_config(chunker_config)
    rows = [
        _chunk_row(node, tokenizer, index_build_id, normalized_chunker_config)
        for node in nodes
    ]
    staging_name = f".{index_build_id}.staging-{uuid4().hex}"
    staging_dir = chunks_dir / staging_name
    staging_relative_dir = Path("chunks") / staging_name
    final_relative_dir = Path("chunks") / index_build_id
    try:
        chunks_ref = write_jsonl(root, staging_relative_dir / "chunks.jsonl", rows)
        preview_ref = write_markdown(
            root,
            staging_relative_dir / "chunks-preview.md",
            _render_preview(index_build_id, rows),
        )
        manifest = {
            "schema_version": CHUNK_ARTIFACT_SCHEMA_VERSION,
            "index_build_id": index_build_id,
            "document_count": document_count,
            "node_count": len(rows),
            "tokenizer": tokenizer_manifest,
            "chunker_config": normalized_chunker_config,
            "lifecycle": _manifest_lifecycle("staged", index_build_id),
            "chunk_fields": [
                "index_build_id",
                "chunk_id",
                "source_file",
                "source_path",
                "header_path",
                "text",
                "character_count",
                "overlap_char_count",
                "token_count",
                "token_count_source",
                "metadata",
                "source_hash",
                "collision",
            ],
            "artifacts": {
                "chunks_jsonl": _artifact_payload_for(
                    final_relative_dir / "chunks.jsonl", chunks_ref
                ),
                "chunks_preview": _artifact_payload_for(
                    final_relative_dir / "chunks-preview.md", preview_ref
                ),
            },
        }
        manifest_ref = write_json(root, staging_relative_dir / "manifest.json", manifest)
        _assert_safe_bundle_directory(root, chunks_dir, staging_dir)
        if build_dir.exists():
            raise FileExistsError(f"chunk artifacts already exist for build {index_build_id}")
        _promote_staging_directory(staging_dir, build_dir)
    except Exception:
        _cleanup_staging_directory(root, chunks_dir, staging_dir)
        raise

    return ChunkArtifactResult(
        index_build_id=index_build_id,
        manifest_uri=(final_relative_dir / "manifest.json").as_posix(),
        manifest_sha256=manifest_ref.sha256,
        chunks_uri=(final_relative_dir / "chunks.jsonl").as_posix(),
        chunks_sha256=chunks_ref.sha256,
        preview_uri=(final_relative_dir / "chunks-preview.md").as_posix(),
        preview_sha256=preview_ref.sha256,
    )


def _chunk_row(
    node: Any,
    tokenizer: Any | None,
    index_build_id: str,
    chunker_config: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = _metadata(node)
    text = _node_text(node)
    token_count = len(tokenizer.encode(text)) if tokenizer is not None else None
    return {
        "index_build_id": index_build_id,
        "chunk_id": _node_id(node),
        "source_file": _source_file(metadata),
        "source_path": _source_path(metadata),
        "header_path": _header_path(metadata),
        "text": text,
        "character_count": len(text),
        "overlap_char_count": chunker_config["overlap_char_count"],
        "token_count": token_count,
        "token_count_source": (
            f"tiktoken:{TOKENIZER_NAME}" if tokenizer is not None else "unavailable"
        ),
        "metadata": _json_value(metadata),
        "source_hash": _source_hash(metadata, text),
        "collision": _collision_payload(metadata),
    }


def _load_tokenizer() -> tuple[Any | None, dict[str, Any]]:
    try:
        import tiktoken

        return tiktoken.get_encoding(TOKENIZER_NAME), {
            "name": TOKENIZER_NAME,
            "source": "tiktoken",
            "available": True,
        }
    except Exception:
        return None, {"name": TOKENIZER_NAME, "source": "tiktoken", "available": False}


def _metadata(node: Any) -> dict[str, Any]:
    metadata = getattr(node, "metadata", None)
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _node_text(node: Any) -> str:
    getter = getattr(node, "get_content", None)
    if callable(getter):
        return str(getter())
    return str(getattr(node, "text", ""))


def _node_id(node: Any) -> str:
    value = getattr(node, "node_id", None) or getattr(node, "id_", None)
    if value:
        return str(value)
    digest = hashlib.sha256(_node_text(node).encode("utf-8")).hexdigest()
    return f"chunk-{digest[:32]}"


def _set_node_id(node: Any, chunk_id: str) -> None:
    try:
        setattr(node, "id_", chunk_id)
    except Exception:
        try:
            setattr(node, "node_id", chunk_id)
        except Exception:
            return


def _set_collision_metadata(
    node: Any, base_chunk_id: str, occurrence: int, count: int
) -> None:
    metadata = getattr(node, "metadata", None)
    if not isinstance(metadata, dict):
        return
    metadata["chunk_collision_base_id"] = base_chunk_id
    metadata["chunk_collision_occurrence"] = occurrence
    metadata["chunk_collision_count"] = count
    metadata["chunk_collision_stability"] = "source_local_order"


def _source_file(metadata: Mapping[str, Any]) -> str:
    value = metadata.get("file_name") or metadata.get("filename")
    if value:
        return str(value)
    source_path = _source_path(metadata)
    return Path(source_path).name if source_path else "unknown"


def _source_path(metadata: Mapping[str, Any]) -> str:
    value = metadata.get("relative_path") or metadata.get("file_path") or metadata.get("source_path")
    return str(value) if value else ""


def _header_path(metadata: Mapping[str, Any]) -> str:
    value = metadata.get("header_path") or metadata.get("heading_path") or metadata.get("header") or ""
    if isinstance(value, (list, tuple)):
        return " > ".join(str(item) for item in value)
    if value:
        return str(value)
    headers = [
        (key, value)
        for key, value in metadata.items()
        if isinstance(key, str) and key.startswith("Header_") and value
    ]
    headers.sort(key=lambda item: item[0])
    return " > ".join(str(value) for _, value in headers)


def _source_char_span(node: Any, metadata: Mapping[str, Any]) -> tuple[int, int]:
    start = getattr(node, "start_char_idx", None)
    end = getattr(node, "end_char_idx", None)
    if start is None:
        start = metadata.get("start_char_idx", -1)
    if end is None:
        end = metadata.get("end_char_idx", -1)
    return _location_int(start), _location_int(end)


def _location_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return -1


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _collision_payload(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    base_chunk_id = metadata.get("chunk_collision_base_id")
    occurrence = metadata.get("chunk_collision_occurrence")
    count = metadata.get("chunk_collision_count")
    stability = metadata.get("chunk_collision_stability")
    if not isinstance(base_chunk_id, str) or not isinstance(stability, str):
        return None
    if not isinstance(occurrence, int) or not isinstance(count, int):
        return None
    return {
        "base_chunk_id": base_chunk_id,
        "occurrence": occurrence,
        "count": count,
        "stability": stability,
    }


def _source_hash(metadata: Mapping[str, Any], fallback_text: str) -> str:
    source_path = metadata.get("file_path") or metadata.get("source_path")
    if isinstance(source_path, str) and source_path:
        try:
            source_bytes = Path(source_path).read_bytes()
        except OSError:
            source_bytes = fallback_text.encode("utf-8")
    else:
        source_bytes = fallback_text.encode("utf-8")
    return hashlib.sha256(source_bytes).hexdigest()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


def _artifact_payload_for(relative_path: Path, reference: ArtifactRef) -> dict[str, str]:
    return {"uri": relative_path.as_posix(), "sha256": reference.sha256}


def _render_preview(index_build_id: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Chunk Preview",
        "",
        f"- Index build: `{index_build_id}`",
        f"- Chunk count: `{len(rows)}`",
        "",
    ]
    current_source: str | None = None
    for row in rows:
        if row["source_file"] != current_source:
            current_source = row["source_file"]
            lines.extend([f"## {current_source}", ""])
        token_count = row["token_count"] if row["token_count"] is not None else "unavailable"
        lines.extend(
            [
                f"### {row['chunk_id']}",
                "",
                f"- Source path: `{row['source_path']}`",
                f"- Header path: `{row['header_path']}`",
                f"- Characters: `{row['character_count']}`",
                f"- Tokens: `{token_count}` ({row['token_count_source']})",
                "",
                "```text",
                row["text"],
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def _validate_index_build_id(index_build_id: str) -> None:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not index_build_id or any(character not in allowed for character in index_build_id):
        raise ValueError("index_build_id must contain only letters, digits, underscores, or hyphens")


def mark_chunk_manifest_published(
    artifact_root: Path | str, index_build_id: str, collection_name: str
) -> None:
    _validate_collection_binding(index_build_id, collection_name)
    manifest = _read_manifest(artifact_root, index_build_id)
    manifest["lifecycle"] = _manifest_lifecycle(
        "published", index_build_id, collection_name=collection_name
    )
    _write_manifest(artifact_root, index_build_id, manifest)


def mark_chunk_manifest_failed(
    artifact_root: Path | str, index_build_id: str, error: Exception | str
) -> None:
    manifest = _read_manifest(artifact_root, index_build_id)
    manifest["lifecycle"] = _manifest_lifecycle(
        "failed",
        index_build_id,
        error_type=error if isinstance(error, str) else type(error).__name__,
    )
    _write_manifest(artifact_root, index_build_id, manifest)


def mark_chunk_manifest_publish_uncertain(
    artifact_root: Path | str,
    index_build_id: str,
    primary_error: Exception,
    rollback_error: Exception | None,
    previous_collection_name: str | None,
    new_collection_name: str,
    *,
    verification_error: Exception | None = None,
) -> None:
    """Record a registry state that cannot yet be safely consumed."""
    manifest = _read_manifest(artifact_root, index_build_id)
    lifecycle: dict[str, str | None] = {
        **_manifest_lifecycle(
            "publish_uncertain",
            index_build_id,
            error_type=type(primary_error).__name__,
        ),
        "previous_collection_name": previous_collection_name,
        "new_collection_name": new_collection_name,
    }
    if rollback_error is not None:
        lifecycle["rollback_error_type"] = type(rollback_error).__name__
    if verification_error is not None:
        lifecycle["verification_error_type"] = type(verification_error).__name__
    manifest["lifecycle"] = lifecycle
    _write_manifest(artifact_root, index_build_id, manifest)


def read_published_chunk_manifest(
    artifact_root: Path | str, index_build_id: str
) -> dict[str, Any]:
    manifest = _read_manifest(artifact_root, index_build_id)
    lifecycle = manifest.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or lifecycle.get("status") != "published":
        raise ChunkManifestUnavailableError("chunk manifest is not published")
    collection_name = lifecycle.get("collection_name")
    try:
        _validate_collection_binding(index_build_id, collection_name)
    except ValueError as error:
        raise ChunkManifestUnavailableError("chunk manifest has an invalid collection binding") from error
    if lifecycle.get("index_build_id") != index_build_id:
        raise ChunkManifestUnavailableError("chunk manifest build binding does not match")
    return manifest


def read_chunk_manifest(
    artifact_root: Path | str, index_build_id: str
) -> dict[str, Any]:
    """Read an artifact manifest without treating it as an evaluator input."""
    return _read_manifest(artifact_root, index_build_id)


def _manifest_lifecycle(
    status: str,
    index_build_id: str,
    *,
    collection_name: str | None = None,
    error_type: str | None = None,
) -> dict[str, str | None]:
    return {
        "status": status,
        "collection_name": collection_name,
        "index_build_id": index_build_id,
        "error_type": error_type,
    }


def _normalize_chunker_config(
    chunker_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    values = {**DEFAULT_CHUNKER_CONFIG, **dict(chunker_config or {})}
    overlap = values.get("overlap_char_count")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0:
        raise ValueError("overlap_char_count must be a non-negative integer")
    return _json_value(values)


def _validate_collection_binding(index_build_id: str, collection_name: object) -> None:
    if not isinstance(collection_name, str) or not collection_name:
        raise ValueError("collection_name is required")
    prefix, separator, suffix = collection_name.rpartition("__build_")
    if not separator or not prefix or suffix != index_build_id:
        raise ValueError("collection_name must bind to index_build_id")


def _read_manifest(artifact_root: Path | str, index_build_id: str) -> dict[str, Any]:
    import json

    _validate_index_build_id(index_build_id)
    root, _, build_dir = _bundle_paths(artifact_root, index_build_id)
    manifest_path = build_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ChunkManifestUnavailableError("chunk manifest does not exist")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ChunkManifestUnavailableError("chunk manifest cannot be read") from error
    if not isinstance(payload, dict) or payload.get("index_build_id") != index_build_id:
        raise ChunkManifestUnavailableError("chunk manifest has an invalid build id")
    return payload


def _write_manifest(
    artifact_root: Path | str, index_build_id: str, manifest: Mapping[str, Any]
) -> None:
    root, _, _ = _bundle_paths(artifact_root, index_build_id)
    write_json(root, Path("chunks") / index_build_id / "manifest.json", dict(manifest))


def _bundle_paths(artifact_root: Path | str, index_build_id: str) -> tuple[Path, Path, Path]:
    root = Path(artifact_root)
    _assert_no_reparse_ancestors(root)
    resolved_root = root.resolve()
    chunks_dir = resolved_root / "chunks"
    build_dir = chunks_dir / index_build_id
    _assert_safe_bundle_directory(resolved_root, chunks_dir, build_dir)
    return resolved_root, chunks_dir, build_dir


def _promote_staging_directory(staging_dir: Path, final_dir: Path) -> None:
    staging_dir.replace(final_dir)


def _cleanup_staging_directory(root: Path, chunks_dir: Path, staging_dir: Path) -> None:
    if not staging_dir.exists() and not staging_dir.is_symlink():
        return
    _assert_safe_bundle_directory(root, chunks_dir, staging_dir)
    if _is_reparse_point(staging_dir):
        raise ValueError("refusing to remove a reparse-point staging directory")
    shutil.rmtree(staging_dir)


def _assert_safe_bundle_directory(root: Path, chunks_dir: Path, target: Path) -> None:
    resolved_root = root.resolve()
    for path in (chunks_dir, target):
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as error:
            raise ValueError("chunk artifact path is outside artifact root") from error
        if path.exists() and _is_reparse_point(path):
            raise ValueError("chunk artifact path contains a reparse-point component")
    if chunks_dir.exists() and _is_reparse_point(chunks_dir):
        raise ValueError("chunk artifact directory cannot be a reparse point")


def _assert_no_reparse_ancestors(path: Path) -> None:
    current = path
    while True:
        if current.exists() and _is_reparse_point(current):
            raise ValueError("artifact root cannot be a reparse point")
        if current == current.parent:
            return
        current = current.parent


def _is_reparse_point(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()
