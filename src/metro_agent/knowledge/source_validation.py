"""Validation for a file-backed knowledge source tree."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml


REQUIRED_METADATA_FIELDS = (
    "owner",
    "source",
    "updated",
    "effective_date",
    "expires_at",
    "risk_level",
)
ALLOWED_RISK_LEVELS = {"general", "controlled", "high"}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_COUNT = 10_000
SMOKE_QUERY_FILE = "release-smoke-queries.jsonl"
_REPARSE_POINT = 0x400


class KnowledgeSourceValidationError(ValueError):
    """A source tree violates a release safety requirement."""


@dataclass(frozen=True)
class KnowledgeDocument:
    path: Path
    relative_path: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class SmokeQuery:
    query: str
    expected_source: str
    minimum_matches: int


@dataclass(frozen=True)
class ValidatedKnowledgeSource:
    root: Path
    documents: tuple[KnowledgeDocument, ...]
    smoke_queries: tuple[SmokeQuery, ...]
    source_tree_sha256: str
    git_commit: str | None = None


def validate_source_root(
    source_root: Path | None, *, current_date: date | None = None
) -> ValidatedKnowledgeSource:
    root = validate_knowledge_source_root(source_root)
    try:
        documents = _discover_documents(root, current_date or _shanghai_today())
        smoke_path = root / SMOKE_QUERY_FILE
        _reject_link_or_reparse(smoke_path, SMOKE_QUERY_FILE)
        if not smoke_path.is_file():
            raise _validation_error(
                SMOKE_QUERY_FILE, "missing required smoke query file"
            )
        smoke_queries = _read_jsonl(
            smoke_path, {document.relative_path for document in documents}
        )
        return ValidatedKnowledgeSource(
            root=root,
            documents=tuple(documents),
            smoke_queries=tuple(smoke_queries),
            source_tree_sha256=source_tree_sha256(documents, smoke_path),
            git_commit=clean_git_commit(root),
        )
    except OSError as error:
        raise _access_error(root, error) from error


def validate_knowledge_source_root(source_root: Path | None) -> Path:
    """Return a real configured knowledge directory."""
    if source_root is None:
        raise _validation_error(".", "KNOWLEDGE_PATH must be configured")
    root = source_root.absolute()
    try:
        _reject_link_or_reparse_ancestors(root)
        if not root.is_dir():
            raise _validation_error(".", "KNOWLEDGE_PATH is not a directory")
        return root
    except OSError as error:
        raise _access_error(root, error) from error


def _discover_documents(root: Path, current_date: date) -> list[KnowledgeDocument]:
    documents: list[KnowledgeDocument] = []
    for directory, directories, filenames in os.walk(
        root, onerror=lambda error: _raise_walk_error(root, error), followlinks=False
    ):
        parent = Path(directory)
        for name in directories + filenames:
            path = parent / name
            _reject_link_or_reparse(path, _relative_path(root, path))
        for name in filenames:
            path = parent / name
            relative_path = _relative_path(root, path)
            if path.suffix != ".md":
                continue
            if not path.is_file():
                raise _validation_error(relative_path, "source is not a real file")
            if path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise _validation_error(relative_path, "source exceeds 10 MiB")
            metadata = _front_matter(path, relative_path)
            _validate_metadata(relative_path, metadata, current_date)
            documents.append(KnowledgeDocument(path, relative_path, metadata))
            if len(documents) > MAX_DOCUMENT_COUNT:
                raise _validation_error(
                    relative_path, "source contains more than 10,000 Markdown files"
                )
    return sorted(documents, key=lambda document: document.relative_path)


def _front_matter(path: Path, relative_path: str) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise KnowledgeSourceValidationError(
            f"{relative_path} has malformed YAML front matter delimiter"
        )
    try:
        closing_line = lines.index("---", 1)
    except ValueError:
        raise KnowledgeSourceValidationError(
            f"{relative_path} has malformed YAML front matter delimiter"
        ) from None
    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing_line]))
    except yaml.YAMLError as error:
        raise KnowledgeSourceValidationError(
            f"{relative_path} has malformed YAML front matter"
        ) from error
    if not isinstance(metadata, dict):
        raise KnowledgeSourceValidationError(
            f"{relative_path} front matter must be a mapping"
        )
    return metadata


def _validate_metadata(
    relative_path: str, metadata: Mapping[str, Any], current_date: date
) -> None:
    missing = [field for field in REQUIRED_METADATA_FIELDS if field not in metadata]
    if missing:
        raise KnowledgeSourceValidationError(
            f"{relative_path} is missing required metadata: {', '.join(missing)}"
        )
    for field in ("owner", "source"):
        value = metadata[field]
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeSourceValidationError(
                f"{relative_path} has an invalid {field}"
            )
    for field in ("updated", "effective_date", "expires_at"):
        try:
            value = metadata[field]
            parsed = date.fromisoformat(
                value.isoformat() if isinstance(value, date) else value
            )
        except (TypeError, ValueError):
            raise KnowledgeSourceValidationError(
                f"{relative_path} has an invalid ISO date in {field}"
            ) from None
        if field == "expires_at" and parsed < current_date:
            raise KnowledgeSourceValidationError(f"{relative_path} has expired")
    risk_level = metadata["risk_level"]
    if not isinstance(risk_level, str) or risk_level not in ALLOWED_RISK_LEVELS:
        raise KnowledgeSourceValidationError(
            f"{relative_path} has an invalid risk_level"
        )


def _read_jsonl(path: Path, source_paths: set[str]) -> list[SmokeQuery]:
    queries: list[SmokeQuery] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise KnowledgeSourceValidationError(
                f"{SMOKE_QUERY_FILE}: invalid JSONL at line {line_number}"
            ) from error
        if not isinstance(item, dict):
            raise KnowledgeSourceValidationError(
                f"{SMOKE_QUERY_FILE}: line {line_number} must be an object"
            )
        query = item.get("query")
        expected_source = item.get("expected_source")
        minimum_matches = item.get("minimum_matches")
        if not isinstance(query, str) or not query.strip():
            raise KnowledgeSourceValidationError(
                f"{SMOKE_QUERY_FILE}: line {line_number} has an empty query"
            )
        if (
            not _is_posix_relative_source(expected_source)
            or expected_source not in source_paths
        ):
            raise KnowledgeSourceValidationError(
                f"{SMOKE_QUERY_FILE}: line {line_number} has an invalid expected_source"
            )
        if (
            isinstance(minimum_matches, bool)
            or not isinstance(minimum_matches, int)
            or minimum_matches < 1
        ):
            raise KnowledgeSourceValidationError(
                f"{SMOKE_QUERY_FILE}: line {line_number} has an invalid minimum_matches"
            )
        queries.append(SmokeQuery(query, expected_source, minimum_matches))
    if not queries:
        raise KnowledgeSourceValidationError(
            "release-smoke-queries.jsonl must contain at least one query"
        )
    return queries


def _is_posix_relative_source(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and "." not in path.parts
        and ".." not in path.parts
        and path.as_posix() == value
    )


def source_tree_sha256(documents: list[KnowledgeDocument], smoke_path: Path) -> str:
    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.relative_path):
        try:
            _digest_part(digest, document.relative_path.encode("utf-8"))
            _digest_part(digest, document.path.read_bytes())
        except OSError as error:
            raise _validation_error(
                document.relative_path, "cannot read source for digest"
            ) from error
    try:
        _digest_part(digest, SMOKE_QUERY_FILE.encode("utf-8"))
        _digest_part(digest, smoke_path.read_bytes())
    except OSError as error:
        raise _validation_error(
            SMOKE_QUERY_FILE, "cannot read source for digest"
        ) from error
    return digest.hexdigest()


def clean_git_commit(source_root: Path) -> str | None:
    """Return HEAD only when the source tree belongs to a clean Git worktree."""
    try:
        status = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
        if status.stdout:
            return None
        head = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return head.stdout.strip() or None


def _reject_link_or_reparse(path: Path, relative_path: str) -> None:
    if path.is_symlink() or _is_reparse_point(path):
        raise KnowledgeSourceValidationError(
            f"{relative_path}: symbolic link or reparse point is not allowed"
        )


def _reject_link_or_reparse_ancestors(path: Path) -> None:
    for ancestor in (path, *path.parents):
        _reject_link_or_reparse(ancestor, ".")


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_POINT)
    except FileNotFoundError:
        return False


def _shanghai_today() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _digest_part(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _validation_error(
    relative_path: str, message: str
) -> KnowledgeSourceValidationError:
    return KnowledgeSourceValidationError(f"{relative_path}: {message}")


def _access_error(root: Path, error: OSError) -> KnowledgeSourceValidationError:
    filename = error.filename
    relative_path = _relative_path(root, Path(filename)) if filename else "."
    return _validation_error(relative_path, "cannot access source")


def _raise_walk_error(root: Path, error: OSError) -> None:
    raise _access_error(root, error) from error


def _relative_path(root: Path, path: Path) -> str:
    try:
        relative_path = path.relative_to(root)
    except ValueError:
        return "."
    return relative_path.as_posix() or "."
