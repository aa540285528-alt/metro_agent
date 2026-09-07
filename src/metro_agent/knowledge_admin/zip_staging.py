"""Safe staging and validation for uploaded knowledge ZIP packages."""

from __future__ import annotations

import hashlib
import shutil
import stat
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from metro_agent.knowledge.source_validation import (
    KnowledgeSourceValidationError,
    ValidatedKnowledgeSource,
    validate_source_root,
)


MAX_ZIP_BYTES = 100 * 1024 * 1024
MAX_FILE_ENTRIES = 10_000
MAX_EXTRACTED_FILE_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
SMOKE_QUERY_FILE = "release-smoke-queries.jsonl"
_COPY_CHUNK_BYTES = 64 * 1024
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_CENTRAL_DIRECTORY_HEADER = b"PK\x01\x02"
_CENTRAL_DIRECTORY_FIXED_SIZE = 46


class KnowledgePackageStagingError(ValueError):
    """An upload is not a safe, valid knowledge package."""


@dataclass(frozen=True)
class StagedKnowledgePackage:
    package_sha256: str
    package_size_bytes: int
    source_root: Path
    validated_source: ValidatedKnowledgeSource


def stage_zip_knowledge_package(
    package_path: Path, *, draft_id: str, staging_root: Path
) -> StagedKnowledgePackage:
    """Extract one safe ZIP package into its draft-specific staging directory."""
    target = _draft_staging_directory(staging_root, draft_id)
    target_created = False
    try:
        package_size_bytes = _package_size(package_path)
        package_sha256 = _package_sha256(package_path)
        with zipfile.ZipFile(package_path) as archive:
            entries = archive.infolist()
            _validate_entries(entries, _raw_entry_names(package_path, archive))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir()
            target_created = True
            _extract_entries(archive, entries, target)
        validated_source = validate_source_root(target)
        return StagedKnowledgePackage(
            package_sha256=package_sha256,
            package_size_bytes=package_size_bytes,
            source_root=target,
            validated_source=validated_source,
        )
    except KnowledgePackageStagingError:
        _cleanup_failed_staging(target, target_created)
        raise
    except KnowledgeSourceValidationError as error:
        _cleanup_failed_staging(target, target_created)
        raise KnowledgePackageStagingError(str(error)) from error
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as error:
        _cleanup_failed_staging(target, target_created)
        raise KnowledgePackageStagingError("archive: cannot stage package") from error


def _package_size(package_path: Path) -> int:
    try:
        size = package_path.stat().st_size
    except OSError as error:
        raise KnowledgePackageStagingError("archive: cannot access package") from error
    if size > MAX_ZIP_BYTES:
        raise KnowledgePackageStagingError("archive: ZIP exceeds 100 MiB")
    return size


def _package_sha256(package_path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with package_path.open("rb") as package:
            while chunk := package.read(_COPY_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise KnowledgePackageStagingError("archive: cannot read package") from error
    return digest.hexdigest()


def _draft_staging_directory(staging_root: Path, draft_id: str) -> Path:
    if not _is_safe_single_path_component(draft_id):
        raise KnowledgePackageStagingError("draft: invalid draft identifier")
    root = staging_root.resolve()
    target = root / draft_id
    if target.parent != root:
        raise KnowledgePackageStagingError("draft: invalid draft identifier")
    return target


def _is_safe_single_path_component(value: str) -> bool:
    if not value or "\x00" in value or "\\" in value or "/" in value:
        return False
    if value in {".", ".."} or PureWindowsPath(value).drive:
        return False
    return not PurePosixPath(value).is_absolute()


def _validate_entries(entries: list[zipfile.ZipInfo], raw_names: list[str]) -> None:
    if not entries:
        raise KnowledgePackageStagingError("archive: package is empty")
    if len(entries) > MAX_FILE_ENTRIES:
        raise KnowledgePackageStagingError("archive: more than 10,000 file entries")
    if len(entries) != len(raw_names):
        raise KnowledgePackageStagingError("archive: invalid ZIP package")

    normalized_names: set[str] = set()
    total_extracted_bytes = 0
    smoke_found = False
    for entry, raw_name in zip(entries, raw_names, strict=True):
        name = _validate_entry_name(entry, raw_name)
        if name in normalized_names:
            raise KnowledgePackageStagingError(f"{name}: duplicate archive entry")
        normalized_names.add(name)
        _validate_entry_attributes(entry, name)
        if entry.file_size > MAX_EXTRACTED_FILE_BYTES:
            raise KnowledgePackageStagingError(f"{name}: file exceeds 10 MiB")
        if _compression_ratio_exceeds_limit(entry):
            raise KnowledgePackageStagingError(f"{name}: compression ratio exceeds 100:1")
        total_extracted_bytes += entry.file_size
        if total_extracted_bytes > MAX_EXTRACTED_BYTES:
            raise KnowledgePackageStagingError("archive: extracted content exceeds 500 MiB")
        if name == SMOKE_QUERY_FILE:
            smoke_found = True
        elif not name.endswith(".md"):
            raise KnowledgePackageStagingError(f"{name}: only Markdown files are allowed")

    if not smoke_found:
        raise KnowledgePackageStagingError(
            f"{SMOKE_QUERY_FILE}: missing required root smoke query file"
        )


def _validate_entry_name(entry: zipfile.ZipInfo, name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise KnowledgePackageStagingError("archive: invalid entry path")
    posix_path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        entry.is_dir()
        or posix_path.is_absolute()
        or windows_path.drive
        or "." in name.split("/")
        or ".." in name.split("/")
    ):
        raise KnowledgePackageStagingError("archive: invalid entry path")
    normalized_name = posix_path.as_posix()
    if not normalized_name or normalized_name == ".":
        raise KnowledgePackageStagingError("archive: invalid entry path")
    return normalized_name


def _raw_entry_names(package_path: Path, archive: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    try:
        with package_path.open("rb") as package:
            package.seek(archive.start_dir)
            for _ in archive.infolist():
                header = package.read(_CENTRAL_DIRECTORY_FIXED_SIZE)
                if len(header) != _CENTRAL_DIRECTORY_FIXED_SIZE or not header.startswith(
                    _CENTRAL_DIRECTORY_HEADER
                ):
                    raise KnowledgePackageStagingError("archive: invalid ZIP package")
                flag_bits = struct.unpack_from("<H", header, 8)[0]
                name_length, extra_length, comment_length = struct.unpack_from(
                    "<HHH", header, 28
                )
                raw_name = package.read(name_length)
                if len(raw_name) != name_length:
                    raise KnowledgePackageStagingError("archive: invalid ZIP package")
                encoding = "utf-8" if flag_bits & 0x800 else "cp437"
                try:
                    names.append(raw_name.decode(encoding))
                except UnicodeDecodeError as error:
                    raise KnowledgePackageStagingError(
                        "archive: invalid entry path"
                    ) from error
                package.seek(extra_length + comment_length, 1)
    except OSError as error:
        raise KnowledgePackageStagingError("archive: cannot read package") from error
    return names


def _validate_entry_attributes(entry: zipfile.ZipInfo, name: str) -> None:
    unix_mode = entry.external_attr >> 16
    windows_attributes = entry.external_attr & 0xFFFF
    if stat.S_ISLNK(unix_mode):
        raise KnowledgePackageStagingError(f"{name}: symbolic links are not allowed")
    if windows_attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        raise KnowledgePackageStagingError(f"{name}: reparse points are not allowed")
    if entry.flag_bits & 0x1:
        raise KnowledgePackageStagingError(f"{name}: encrypted entries are not allowed")


def _compression_ratio_exceeds_limit(entry: zipfile.ZipInfo) -> bool:
    return entry.file_size > 0 and (
        entry.compress_size == 0
        or entry.file_size > entry.compress_size * MAX_COMPRESSION_RATIO
    )


def _extract_entries(
    archive: zipfile.ZipFile, entries: list[zipfile.ZipInfo], target: Path
) -> None:
    for entry in entries:
        relative_name = PurePosixPath(entry.orig_filename).as_posix()
        destination = target.joinpath(*PurePosixPath(relative_name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(entry) as source, destination.open("xb") as output:
            while chunk := source.read(_COPY_CHUNK_BYTES):
                output.write(chunk)


def _cleanup_failed_staging(target: Path, target_created: bool) -> None:
    if not target_created:
        return
    try:
        shutil.rmtree(target)
    except OSError:
        pass
