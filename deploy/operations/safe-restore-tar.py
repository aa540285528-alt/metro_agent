
"""Validate backup tarballs before restoring a governed volume.

The backup directory is an external trust boundary.  This utility deliberately
accepts only one archive root, ordinary files and directories, and makes every
archive member pass validation before it calls ``extractall``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(RuntimeError):
    """The archive cannot be safely restored into a named volume."""


def _validate_members(archive: Path, expected_root: str) -> list[tarfile.TarInfo]:
    try:
        with tarfile.open(archive, "r:*") as contents:
            members = contents.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise UnsafeArchiveError("backup archive cannot be read") from exc

    roots: set[str] = set()
    root_entries = 0
    for member in members:
        name = member.name
        path = PurePosixPath(name)
        if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
            raise UnsafeArchiveError(f"unsafe archive member: {name!r}")
        if not path.parts or path.parts[0] in {"", "."}:
            raise UnsafeArchiveError(f"unsafe archive member: {name!r}")




        if (
            member.issym()
            or member.islnk()
            or member.isdev()
            or not (member.isdir() or member.isreg())
        ):
            raise UnsafeArchiveError(f"unsafe archive member type: {name!r}")
        roots.add(path.parts[0])
        if tuple(path.parts) == (expected_root,) and member.isdir():
            root_entries += 1

    if roots != {expected_root} or root_entries != 1:
        raise UnsafeArchiveError("backup archive must contain exactly its expected root")
    return members


def _extract_validated(archive: Path, destination: Path, expected_root: str) -> Path:
    _validate_members(archive, expected_root)
    stage = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination))
    try:
        with tarfile.open(archive, "r:*") as contents:

            contents.extractall(stage, filter="data")
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise UnsafeArchiveError("backup archive extraction failed") from exc
    restored_root = stage / expected_root
    if not restored_root.is_dir():
        shutil.rmtree(stage, ignore_errors=True)
        raise UnsafeArchiveError("backup archive root is not a directory")
    return stage


def swap_volume(archive: Path, destination: Path, expected_root: str) -> None:
    """Stage a complete volume then atomically switch its one expected root."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = _extract_validated(archive, destination, expected_root)
    restored_root = stage / expected_root
    live_root = destination / expected_root
    rollback = destination / "rollback.previous"
    try:
        if rollback.exists():
            raise UnsafeArchiveError("previous rollback volume must be handled before restoring again")
        if live_root.exists():
            os.replace(live_root, rollback)
        os.replace(restored_root, live_root)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def replace_volume_contents(archive: Path, destination: Path, expected_root: str) -> None:
    """Replace a named Docker volume's *contents* after full staging.

    A named volume is mounted as its root, so putting ``expected_root`` below
    it would silently create an extra directory level. Move all existing
    volume entries into a recoverable ``rollback.previous`` directory, then
    install the fully validated staged root's children at the volume root.
    """
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = _extract_validated(archive, destination, expected_root)
    staged_root = stage / expected_root
    rollback = destination / "rollback.previous"
    moved_previous: list[Path] = []
    installed: list[Path] = []
    try:
        if rollback.exists() or rollback.is_symlink():
            raise UnsafeArchiveError("previous rollback volume must be handled before restoring again")
        rollback.mkdir(mode=0o700)
        for entry in list(destination.iterdir()):
            if entry == stage or entry == rollback:
                continue
            target = rollback / entry.name
            os.replace(entry, target)
            moved_previous.append(entry)
        for entry in list(staged_root.iterdir()):
            target = destination / entry.name
            os.replace(entry, target)
            installed.append(target)
    except Exception:


        for target in reversed(installed):
            if target.exists() or target.is_symlink():
                os.replace(target, staged_root / target.name)
        for original in reversed(moved_previous):
            rolled_back = rollback / original.name
            if rolled_back.exists() or rolled_back.is_symlink():
                os.replace(rolled_back, original)
        if rollback.exists() and not any(rollback.iterdir()):
            rollback.rmdir()
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def merge_artifacts(archive: Path, destination: Path, expected_root: str) -> None:
    """Append validated immutable artifacts without deleting prior releases."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = _extract_validated(archive, destination, expected_root)
    staged_root = stage / expected_root
    try:
        for source in sorted(staged_root.rglob("*")):
            relative = source.relative_to(staged_root)
            target = destination / expected_root / relative
            if source.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                if target.exists():
                    raise UnsafeArchiveError(f"artifact already exists: {relative.as_posix()}")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(source, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely restore a validated Metro Agent tar archive.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("swap-volume", "replace-volume-contents", "merge-artifacts"):
        subparser = commands.add_parser(command)
        subparser.add_argument("archive", type=Path)
        subparser.add_argument("destination", type=Path)
        subparser.add_argument("--expected-root", required=True)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    if args.command == "swap-volume":
        swap_volume(args.archive, args.destination, args.expected_root)
    elif args.command == "replace-volume-contents":
        replace_volume_contents(args.archive, args.destination, args.expected_root)
    else:
        merge_artifacts(args.archive, args.destination, args.expected_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
