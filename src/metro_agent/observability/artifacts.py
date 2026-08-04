from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    uri: str
    sha256: str


def _resolve_target(artifact_root: Path | str, relative_path: Path | str) -> Path:
    artifact_root_path = Path(artifact_root)
    _assert_no_reparse_ancestors(artifact_root_path)
    root = artifact_root_path.resolve()
    raw_path = str(relative_path)
    windows_path = PureWindowsPath(raw_path)
    candidate = Path(raw_path)
    if (
        not raw_path
        or raw_path == "."
        or candidate.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or ".." in windows_path.parts
    ):
        raise ValueError("artifact path must stay inside artifact root")

    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("artifact path is outside artifact root") from error
    _assert_no_reparse_components(root, target)
    if target == root:
        raise ValueError("artifact path must name a file inside artifact root")
    return target


def _is_reparse_point(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _assert_no_reparse_components(root: Path, target: Path) -> None:
    current = root
    for component in target.relative_to(root).parts:
        current /= component
        if current.exists() and _is_reparse_point(current):
            raise ValueError("artifact path contains a reparse-point component")


def _assert_no_reparse_ancestors(path: Path) -> None:
    current = path
    while True:
        if current.exists() and _is_reparse_point(current):
            raise ValueError("artifact root cannot be a reparse point")
        if current == current.parent:
            return
        current = current.parent


def _revalidate_destination(
    artifact_root: Path | str, relative_path: Path | str
) -> tuple[Path, Path]:
    artifact_root_path = Path(artifact_root)
    return artifact_root_path.resolve(), _resolve_target(artifact_root_path, relative_path)


def _atomic_write(artifact_root: Path | str, relative_path: Path | str, content: str) -> ArtifactRef:
    root, target = _revalidate_destination(artifact_root, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    root, target = _revalidate_destination(artifact_root, relative_path)
    content_bytes = content.encode("utf-8")
    sha256 = hashlib.sha256(content_bytes).hexdigest()

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temp_path = Path(temporary_file.name)
            temporary_file.write(content_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        root, target = _revalidate_destination(artifact_root, relative_path)
        if temp_path.parent.resolve() != target.parent.resolve():
            raise ValueError("artifact root changed before replace")
        temp_path.replace(target)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    return ArtifactRef(
        uri=target.relative_to(root).as_posix(),
        sha256=sha256,
    )


def write_json(
    artifact_root: Path | str,
    relative_path: Path | str,
    payload: Mapping[str, Any] | list[Any],
) -> ArtifactRef:
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return _atomic_write(artifact_root, relative_path, content)


def write_jsonl(
    artifact_root: Path | str,
    relative_path: Path | str,
    rows: Iterable[Mapping[str, Any]],
) -> ArtifactRef:
    content = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        for row in rows
    )
    return _atomic_write(artifact_root, relative_path, content)


def write_markdown(
    artifact_root: Path | str,
    relative_path: Path | str,
    content: str,
) -> ArtifactRef:
    return _atomic_write(artifact_root, relative_path, content)
