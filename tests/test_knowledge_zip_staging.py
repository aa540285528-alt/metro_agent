from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from metro_agent.knowledge.config import (
    KnowledgeSettings,
    require_knowledge_upload_staging_root,
)
from metro_agent.knowledge_admin.zip_staging import (
    KnowledgePackageStagingError,
    stage_zip_knowledge_package,
)


def _document() -> str:
    return """---
owner: operations
source: handbook
updated: 2026-08-31
effective_date: 2026-08-31
expires_at: 2026-12-31
risk_level: general
---
# Intro
"""


def _smoke_query() -> str:
    return json.dumps(
        {
            "query": "How do I begin?",
            "expected_source": "guides/intro.md",
            "minimum_matches": 1,
        }
    ) + "\n"


def _write_package(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _valid_members() -> dict[str, str]:
    return {
        "guides/intro.md": _document(),
        "release-smoke-queries.jsonl": _smoke_query(),
    }


def _write_package_with_raw_backslash_member(path: Path) -> None:
    _write_package(
        path,
        {
            "guides/escape.md": _document(),
            "release-smoke-queries.jsonl": _smoke_query(),
        },
    )
    path.write_bytes(
        path.read_bytes().replace(b"guides/escape.md", b"guides\\escape.md")
    )


def _write_package_with_nul_member(path: Path) -> None:
    _write_package(
        path,
        {
            "guides/escape.md": _document(),
            "release-smoke-queries.jsonl": _smoke_query(),
        },
    )
    path.write_bytes(
        path.read_bytes().replace(b"guides/escape.md", b"guides/\x00scape.md")
    )


def test_stages_and_validates_a_zip_knowledge_package(tmp_path: Path) -> None:
    package = tmp_path / "knowledge.zip"
    _write_package(
        package,
        _valid_members(),
    )

    staged = stage_zip_knowledge_package(
        package, draft_id="draft-123", staging_root=tmp_path / "staging"
    )

    assert staged.package_size_bytes == package.stat().st_size
    assert len(staged.package_sha256) == 64
    assert staged.source_root == tmp_path / "staging" / "draft-123"
    assert [document.relative_path for document in staged.validated_source.documents] == [
        "guides/intro.md"
    ]


def test_upload_staging_root_is_configured_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOWLEDGE_UPLOAD_STAGING_ROOT", "/var/lib/knowledge-staging")

    root = KnowledgeSettings.from_environment().upload_staging_root

    assert root == Path("/var/lib/knowledge-staging")
    assert require_knowledge_upload_staging_root(root) == root
    with pytest.raises(ValueError, match="KNOWLEDGE_UPLOAD_STAGING_ROOT"):
        require_knowledge_upload_staging_root(None)


def test_environment_example_documents_upload_staging_root() -> None:
    environment = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )

    assert "KNOWLEDGE_UPLOAD_STAGING_ROOT=" in environment


@pytest.mark.parametrize(
    "member_name",
    [
        "/absolute.md",
        "C:/drive.md",
        "guides/../escape.md",
        "./current.md",
        "folder/",
    ],
)
def test_rejects_unsafe_or_directory_zip_members(
    tmp_path: Path, member_name: str
) -> None:
    package = tmp_path / "knowledge.zip"
    members = _valid_members()
    members[member_name] = _document()
    _write_package(package, members)

    with pytest.raises(KnowledgePackageStagingError, match="archive: invalid entry path"):
        stage_zip_knowledge_package(
            package, draft_id="draft-123", staging_root=tmp_path / "staging"
        )


def test_rejects_raw_backslashes_in_zip_member_names(tmp_path: Path) -> None:
    package = tmp_path / "backslash.zip"
    _write_package_with_raw_backslash_member(package)

    with pytest.raises(KnowledgePackageStagingError, match="archive: invalid entry path"):
        stage_zip_knowledge_package(
            package, draft_id="backslash", staging_root=tmp_path / "staging"
        )


def test_rejects_nuls_in_raw_zip_member_names(tmp_path: Path) -> None:
    package = tmp_path / "nul.zip"
    _write_package_with_nul_member(package)

    with pytest.raises(KnowledgePackageStagingError, match="archive: invalid entry path"):
        stage_zip_knowledge_package(
            package, draft_id="nul", staging_root=tmp_path / "staging"
        )


def test_rejects_non_markdown_content_and_nested_smoke_queries(tmp_path: Path) -> None:
    package = tmp_path / "knowledge.zip"
    _write_package(
        package,
        {
            "guides/intro.md": _document(),
            "nested/release-smoke-queries.jsonl": _smoke_query(),
            "release-smoke-queries.jsonl": _smoke_query(),
            "nested/extra.txt": "not permitted",
        },
    )

    with pytest.raises(KnowledgePackageStagingError, match="only Markdown"):
        stage_zip_knowledge_package(
            package, draft_id="draft-123", staging_root=tmp_path / "staging"
        )


def test_rejects_an_empty_zip_or_a_missing_root_smoke_file(tmp_path: Path) -> None:
    empty_package = tmp_path / "empty.zip"
    _write_package(empty_package, {})
    missing_smoke_package = tmp_path / "missing-smoke.zip"
    _write_package(missing_smoke_package, {"guides/intro.md": _document()})

    with pytest.raises(KnowledgePackageStagingError, match="package is empty"):
        stage_zip_knowledge_package(
            empty_package, draft_id="empty", staging_root=tmp_path / "staging"
        )
    with pytest.raises(KnowledgePackageStagingError, match="missing required root"):
        stage_zip_knowledge_package(
            missing_smoke_package,
            draft_id="missing-smoke",
            staging_root=tmp_path / "staging",
        )


def test_rejects_duplicate_normalized_member_names(tmp_path: Path) -> None:
    package = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("guides/intro.md", _document())
        archive.writestr("guides//intro.md", _document())
        archive.writestr("release-smoke-queries.jsonl", _smoke_query())

    with pytest.raises(KnowledgePackageStagingError, match="duplicate archive entry"):
        stage_zip_knowledge_package(
            package, draft_id="duplicate", staging_root=tmp_path / "staging"
        )


def test_rejects_case_insensitive_member_name_collisions(tmp_path: Path) -> None:
    package = tmp_path / "case-collision.zip"
    members = _valid_members()
    members["Guides/intro.md"] = _document()
    _write_package(package, members)

    with pytest.raises(KnowledgePackageStagingError, match="case-insensitive"):
        stage_zip_knowledge_package(
            package, draft_id="case-collision", staging_root=tmp_path / "staging"
        )


@pytest.mark.parametrize("component", ["guides.", "guides "])
def test_rejects_components_ending_with_windows_dangerous_suffixes(
    tmp_path: Path, component: str
) -> None:
    package = tmp_path / "trailing-suffix.zip"
    members = _valid_members()
    members[f"{component}/intro.md"] = _document()
    _write_package(package, members)

    with pytest.raises(KnowledgePackageStagingError, match="invalid entry path"):
        stage_zip_knowledge_package(
            package, draft_id="trailing-suffix", staging_root=tmp_path / "staging"
        )


def test_rejects_ntfs_stream_syntax_in_member_names(tmp_path: Path) -> None:
    package = tmp_path / "ntfs-stream.zip"
    members = _valid_members()
    members["guides/intro:metadata.md"] = _document()
    _write_package(package, members)

    with pytest.raises(KnowledgePackageStagingError, match="invalid entry path"):
        stage_zip_knowledge_package(
            package, draft_id="ntfs-stream", staging_root=tmp_path / "staging"
        )


@pytest.mark.parametrize(
    "component",
    [
        "con.md",
        "PRN.extra.md",
        "aux.md",
        "NUL.metadata.md",
        "com1.md",
        "COM9.extra.md",
        "lpt1.md",
        "LPT9.metadata.md",
    ],
)
def test_rejects_reserved_dos_device_name_components(
    tmp_path: Path, component: str
) -> None:
    package = tmp_path / "dos-device.zip"
    members = _valid_members()
    members[f"guides/{component}"] = _document()
    _write_package(package, members)

    with pytest.raises(KnowledgePackageStagingError, match="invalid entry path"):
        stage_zip_knowledge_package(
            package, draft_id="dos-device", staging_root=tmp_path / "staging"
        )


@pytest.mark.parametrize(
    "external_attr, message",
    [
        (stat.S_IFLNK << 16, "symbolic links"),
        (0x400, "reparse points"),
    ],
)
def test_rejects_link_like_zip_members(
    tmp_path: Path, external_attr: int, message: str
) -> None:
    package = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(package, "w") as archive:
        unsafe = zipfile.ZipInfo("guides/intro.md")
        unsafe.external_attr = external_attr
        archive.writestr(unsafe, _document())
        archive.writestr("release-smoke-queries.jsonl", _smoke_query())

    with pytest.raises(KnowledgePackageStagingError, match=message):
        stage_zip_knowledge_package(
            package, draft_id="unsafe", staging_root=tmp_path / "staging"
        )


def test_rejects_encrypted_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "encrypted.zip"
    _write_package(package, _valid_members())
    original_infolist = zipfile.ZipFile.infolist

    def encrypted_infolist(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        entries = original_infolist(archive)
        entries[0].flag_bits |= 0x1
        return entries

    monkeypatch.setattr(zipfile.ZipFile, "infolist", encrypted_infolist)

    with pytest.raises(KnowledgePackageStagingError, match="encrypted entries"):
        stage_zip_knowledge_package(
            package, draft_id="encrypted", staging_root=tmp_path / "staging"
        )


def test_rejects_excessive_compression_ratio(tmp_path: Path) -> None:
    package = tmp_path / "compressed.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("guides/intro.md", _document() + "x" * 20_000)
        archive.writestr("release-smoke-queries.jsonl", _smoke_query())

    with pytest.raises(KnowledgePackageStagingError, match="compression ratio"):
        stage_zip_knowledge_package(
            package, draft_id="compressed", staging_root=tmp_path / "staging"
        )


@pytest.mark.parametrize(
    "limit_name, limit_value, message",
    [
        ("MAX_ZIP_BYTES", 0, "ZIP exceeds 100 MiB"),
        ("MAX_FILE_ENTRIES", 1, "more than 10,000 file entries"),
        ("MAX_EXTRACTED_FILE_BYTES", 1, "file exceeds 10 MiB"),
        ("MAX_EXTRACTED_BYTES", 1, "extracted content exceeds 500 MiB"),
    ],
)
def test_rejects_configured_archive_size_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
    message: str,
) -> None:
    package = tmp_path / "limited.zip"
    _write_package(package, _valid_members())
    import metro_agent.knowledge_admin.zip_staging as zip_staging

    monkeypatch.setattr(zip_staging, limit_name, limit_value)

    with pytest.raises(KnowledgePackageStagingError, match=message):
        stage_zip_knowledge_package(
            package, draft_id="limited", staging_root=tmp_path / "staging"
        )


def test_rejects_non_zip_files_and_unsafe_draft_ids(tmp_path: Path) -> None:
    package = tmp_path / "not-a-zip.txt"
    package.write_text("not an archive", encoding="utf-8")
    staging_root = tmp_path / "staging"
    keep = staging_root / "keep"
    keep.mkdir(parents=True)

    with pytest.raises(KnowledgePackageStagingError, match="cannot stage package"):
        stage_zip_knowledge_package(
            package, draft_id="not-zip", staging_root=staging_root
        )
    with pytest.raises(KnowledgePackageStagingError, match="invalid draft identifier"):
        stage_zip_knowledge_package(
            package, draft_id="../keep", staging_root=staging_root
        )

    assert keep.is_dir()


def test_root_resolution_errors_are_sanitized_without_creating_a_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "knowledge.zip"
    _write_package(package, _valid_members())
    staging_root = tmp_path / "private-staging"
    original_resolve = Path.resolve

    def denied_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == staging_root:
            raise PermissionError(13, "denied", str(staging_root))
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", denied_resolve)

    with pytest.raises(KnowledgePackageStagingError) as error:
        stage_zip_knowledge_package(
            package, draft_id="draft-123", staging_root=staging_root
        )

    assert str(staging_root) not in str(error.value)
    assert not (staging_root / "draft-123").exists()


def test_invalid_source_is_cleaned_up_and_does_not_leak_staging_path(
    tmp_path: Path,
) -> None:
    package = tmp_path / "invalid-source.zip"
    members = _valid_members()
    members["guides/intro.md"] = "# no front matter\n"
    _write_package(package, members)
    staging_root = tmp_path / "staging"
    sibling = staging_root / "keep"
    sibling.mkdir(parents=True)

    with pytest.raises(KnowledgePackageStagingError) as error:
        stage_zip_knowledge_package(
            package, draft_id="invalid", staging_root=staging_root
        )

    assert not (staging_root / "invalid").exists()
    assert sibling.is_dir()
    assert str(staging_root) not in str(error.value)
