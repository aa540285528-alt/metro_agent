from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "operations" / "safe-restore-tar.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("safe_restore_tar", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(archive: Path, root: str, files: dict[str, bytes]) -> None:
    with tarfile.open(archive, "w:gz") as output:
        root_info = tarfile.TarInfo(root)
        root_info.type = tarfile.DIRTYPE
        output.addfile(root_info)
        for name, content in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(content)
            output.addfile(info, io.BytesIO(content))


def test_swap_volume_rejects_path_traversal_before_touching_live_volume(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / "unsafe.tar.gz"
    _archive(archive, "chroma", {"../outside": b"blocked"})
    destination = tmp_path / "volume"
    live = destination / "chroma"
    live.mkdir(parents=True)
    (live / "live.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(module.UnsafeArchiveError):
        module.swap_volume(archive, destination, "chroma")

    assert (live / "live.txt").read_text(encoding="utf-8") == "keep"
    assert not (destination / "rollback.previous").exists()


def test_swap_volume_stages_then_switches_complete_chroma_root(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / "chroma.tar.gz"
    _archive(archive, "chroma", {"new.txt": b"new"})
    destination = tmp_path / "volume"
    live = destination / "chroma"
    live.mkdir(parents=True)
    (live / "old.txt").write_text("old", encoding="utf-8")

    module.swap_volume(archive, destination, "chroma")

    assert (destination / "chroma" / "new.txt").read_bytes() == b"new"
    assert (destination / "rollback.previous" / "old.txt").read_text(encoding="utf-8") == "old"


def test_replace_volume_contents_installs_chroma_at_volume_root(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / "chroma.tar.gz"
    _archive(archive, "chroma", {"sqlite.db": b"new"})
    destination = tmp_path / "volume"
    destination.mkdir()
    (destination / "old.sqlite").write_text("old", encoding="utf-8")

    module.replace_volume_contents(archive, destination, "chroma")

    assert (destination / "sqlite.db").read_bytes() == b"new"
    assert not (destination / "chroma").exists()
    assert (destination / "rollback.previous" / "old.sqlite").read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    ("name", "member_type"),
    [
        ("/absolute", tarfile.REGTYPE),
        ("../traversal", tarfile.REGTYPE),
        ("linked", tarfile.SYMTYPE),
        ("device", tarfile.CHRTYPE),
    ],
)
def test_swap_volume_rejects_unsafe_tar_member_types(
    tmp_path: Path, name: str, member_type: bytes
) -> None:
    module = _load_module()
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        root = tarfile.TarInfo("chroma")
        root.type = tarfile.DIRTYPE
        output.addfile(root)
        unsafe = tarfile.TarInfo(name)
        unsafe.type = member_type
        unsafe.size = 0
        output.addfile(unsafe)

    with pytest.raises(module.UnsafeArchiveError):
        module.swap_volume(archive, tmp_path / "volume", "chroma")


def test_swap_volume_rejects_duplicate_archive_root(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / "duplicate-root.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for _ in range(2):
            root = tarfile.TarInfo("chroma")
            root.type = tarfile.DIRTYPE
            output.addfile(root)

    with pytest.raises(module.UnsafeArchiveError):
        module.swap_volume(archive, tmp_path / "volume", "chroma")


def test_merge_artifacts_keeps_prior_release_history(tmp_path: Path) -> None:
    module = _load_module()
    archive = tmp_path / "artifacts.tar.gz"
    _archive(archive, "knowledge-artifacts", {"releases/new/validated.json": b"{}"})
    destination = tmp_path / "artifacts"
    previous = destination / "knowledge-artifacts" / "releases" / "old" / "validated.json"
    previous.parent.mkdir(parents=True)
    previous.write_text("old", encoding="utf-8")

    module.merge_artifacts(archive, destination, "knowledge-artifacts")

    assert previous.read_text(encoding="utf-8") == "old"
    assert (destination / "knowledge-artifacts" / "releases" / "new" / "validated.json").read_bytes() == b"{}"
