from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from metro_agent.knowledge.source_validation import (
    KnowledgeSourceValidationError,
    clean_git_commit,
    validate_source_root,
)


def _write_document(root: Path, relative_path: str = "guides/intro.md") -> None:
    document = root / relative_path
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(
        """---
owner: operations
source: handbook
updated: 2026-08-31
effective_date: 2026-08-31
expires_at: 2026-12-31
risk_level: general
---
# Intro
""",
        encoding="utf-8",
    )


def _write_smoke(root: Path, source: str = "guides/intro.md") -> None:
    (root / "release-smoke-queries.jsonl").write_text(
        json.dumps(
            {
                "query": "How do I begin?",
                "expected_source": source,
                "minimum_matches": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_validates_real_markdown_sources_and_release_smoke_queries(
    tmp_path: Path,
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    (tmp_path / "notes.txt").write_text("not source content", encoding="utf-8")

    validated = validate_source_root(tmp_path)

    assert [document.relative_path for document in validated.documents] == [
        "guides/intro.md"
    ]
    assert validated.documents[0].metadata["owner"] == "operations"
    assert validated.smoke_queries[0].expected_source == "guides/intro.md"
    assert len(validated.source_tree_sha256) == 64


@pytest.mark.parametrize(
    "metadata_patch",
    [
        {"owner": None},
        {"source": None},
        {"updated": None},
        {"effective_date": None},
        {"expires_at": None},
        {"risk_level": None},
    ],
)
def test_rejects_documents_missing_required_metadata(
    tmp_path: Path, metadata_patch: dict[str, object]
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    field = next(iter(metadata_patch))
    document.write_text(
        document.read_text(encoding="utf-8").replace(f"{field}: ", f"# {field}: "),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required metadata"):
        validate_source_root(tmp_path)


@pytest.mark.parametrize(
    "front_matter",
    ["---\nowner: [\n---\nbody", "---\n- not\n- a mapping\n---\nbody"],
)
def test_rejects_malformed_or_non_mapping_front_matter(
    tmp_path: Path, front_matter: str
) -> None:
    (tmp_path / "source.md").write_text(front_matter, encoding="utf-8")
    _write_smoke(tmp_path, "source.md")

    with pytest.raises(ValueError, match="front matter"):
        validate_source_root(tmp_path)


@pytest.mark.parametrize("field", ["updated", "effective_date", "expires_at"])
def test_rejects_invalid_iso_metadata_dates(tmp_path: Path, field: str) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            f"{field}: 2026-", f"{field}: not-a-date # 2026-"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid ISO date"):
        validate_source_root(tmp_path)


def test_rejects_unsupported_risk_level(tmp_path: Path) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "risk_level: general", "risk_level: secret"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="risk_level"):
        validate_source_root(tmp_path)


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("owner", "owner: "),
        ("owner", "owner: 42"),
        ("source", "source: "),
        ("source", "source: 42"),
    ],
)
def test_rejects_empty_or_non_string_owner_and_source(
    tmp_path: Path, field: str, replacement: str
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            f"{field}: operations" if field == "owner" else f"{field}: handbook",
            replacement,
        ),
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeSourceValidationError, match=field):
        validate_source_root(tmp_path)


@pytest.mark.parametrize("risk_value", ["risk_level: 42", "risk_level: []"])
def test_rejects_non_string_risk_level_as_a_validation_error(
    tmp_path: Path, risk_value: str
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace("risk_level: general", risk_value),
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeSourceValidationError, match="risk_level"):
        validate_source_root(tmp_path)


def test_read_errors_are_normalized_to_a_relative_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    original_read_text = Path.read_text

    def denied_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == document:
            raise PermissionError(13, "denied", str(document))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_read)

    with pytest.raises(KnowledgeSourceValidationError) as error:
        validate_source_root(tmp_path)

    assert "guides/intro.md" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_stat_errors_are_normalized_to_a_relative_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    original_stat = Path.stat

    def denied_stat(path: Path, *args: object, **kwargs: object) -> object:
        if path == document:
            raise PermissionError(13, "denied", str(document))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)

    with pytest.raises(KnowledgeSourceValidationError) as error:
        validate_source_root(tmp_path)

    assert "guides/intro.md" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_walk_errors_are_normalized_to_a_relative_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    inaccessible = tmp_path / "private"
    import metro_agent.knowledge.source_validation as source_validation

    def broken_walk(
        root: Path, *, onerror: object, followlinks: bool
    ) -> list[tuple[str, list[str], list[str]]]:
        assert followlinks is False
        assert callable(onerror)
        onerror(PermissionError(13, "denied", str(inaccessible)))
        return []

    monkeypatch.setattr(source_validation.os, "walk", broken_walk)

    with pytest.raises(KnowledgeSourceValidationError) as error:
        validate_source_root(tmp_path)

    assert "private" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_validation_errors_expose_only_relative_source_paths(tmp_path: Path) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "risk_level: general", "risk_level: secret"
        ),
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeSourceValidationError) as error:
        validate_source_root(tmp_path)

    assert "guides/intro.md" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_rejects_an_expired_document_using_the_supplied_shanghai_date(
    tmp_path: Path,
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    document = tmp_path / "guides/intro.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "expires_at: 2026-12-31", "expires_at: 2026-08-30"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expired"):
        validate_source_root(tmp_path, current_date=date(2026, 8, 31))


@pytest.mark.parametrize("source_root", [None, Path("does-not-exist")])
def test_rejects_missing_or_non_directory_source_root(source_root: Path | None) -> None:
    with pytest.raises(KnowledgeSourceValidationError, match="KNOWLEDGE_PATH"):
        validate_source_root(source_root)  # type: ignore[arg-type]


def test_requires_root_release_smoke_file(tmp_path: Path) -> None:
    _write_document(tmp_path)

    with pytest.raises(ValueError, match="release-smoke-queries.jsonl"):
        validate_source_root(tmp_path)


@pytest.mark.parametrize(
    "smoke_line, message",
    [
        (
            {"query": " ", "expected_source": "guides/intro.md", "minimum_matches": 1},
            "empty query",
        ),
        (
            {"query": "q", "expected_source": "guides\\intro.md", "minimum_matches": 1},
            "expected_source",
        ),
        (
            {"query": "q", "expected_source": "unknown.md", "minimum_matches": 1},
            "expected_source",
        ),
        (
            {"query": "q", "expected_source": "guides/intro.md", "minimum_matches": 0},
            "minimum_matches",
        ),
        (
            {
                "query": "q",
                "expected_source": "guides/intro.md",
                "minimum_matches": True,
            },
            "minimum_matches",
        ),
    ],
)
def test_rejects_invalid_release_smoke_query_fields(
    tmp_path: Path, smoke_line: dict[str, object], message: str
) -> None:
    _write_document(tmp_path)
    (tmp_path / "release-smoke-queries.jsonl").write_text(
        json.dumps(smoke_line) + "\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match=message):
        validate_source_root(tmp_path)


@pytest.mark.parametrize("smoke_text", ["not json\n", "[]\n", "\n"])
def test_rejects_malformed_or_non_object_smoke_jsonl(
    tmp_path: Path, smoke_text: str
) -> None:
    _write_document(tmp_path)
    (tmp_path / "release-smoke-queries.jsonl").write_text(smoke_text, encoding="utf-8")

    with pytest.raises(ValueError, match="(?:JSONL|object)"):
        validate_source_root(tmp_path)


def test_rejects_an_empty_release_smoke_file(tmp_path: Path) -> None:
    _write_document(tmp_path)
    (tmp_path / "release-smoke-queries.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="at least one"):
        validate_source_root(tmp_path)


@pytest.mark.parametrize(
    "front_matter",
    [
        "--- \nowner: operations\nsource: handbook\nupdated: 2026-08-31\neffective_date: 2026-08-31\nexpires_at: 2026-12-31\nrisk_level: general\n---\n",
        "----\nowner: operations\nsource: handbook\nupdated: 2026-08-31\neffective_date: 2026-08-31\nexpires_at: 2026-12-31\nrisk_level: general\n---\n",
        "---\nowner: operations\nsource: handbook\nupdated: 2026-08-31\neffective_date: 2026-08-31\nexpires_at: 2026-12-31\nrisk_level: general\n----\n",
    ],
)
def test_requires_exact_yaml_front_matter_delimiter_lines(
    tmp_path: Path, front_matter: str
) -> None:
    (tmp_path / "source.md").write_text(front_matter, encoding="utf-8")
    _write_smoke(tmp_path, "source.md")

    with pytest.raises(ValueError, match="delimiter"):
        validate_source_root(tmp_path)


def test_rejects_symbolic_link_in_a_configured_root_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "knowledge"
    _write_document(source_root)
    _write_smoke(source_root)
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == tmp_path)

    with pytest.raises(ValueError, match="symbolic link"):
        validate_source_root(source_root)


def test_rejects_reparse_point_in_a_configured_root_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "knowledge"
    _write_document(source_root)
    _write_smoke(source_root)
    import metro_agent.knowledge.source_validation as source_validation

    monkeypatch.setattr(
        source_validation, "_is_reparse_point", lambda path: path == tmp_path
    )

    with pytest.raises(ValueError, match="reparse point"):
        validate_source_root(source_root)


def test_rejects_symlinked_markdown_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_document(tmp_path, "linked.md")
    _write_smoke(tmp_path, "linked.md")
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "linked.md")

    with pytest.raises(ValueError, match="(?:symbolic link|reparse point)"):
        validate_source_root(tmp_path)


def test_rejects_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    import metro_agent.knowledge.source_validation as source_validation

    monkeypatch.setattr(
        source_validation,
        "_is_reparse_point",
        lambda path: path.name == "intro.md",
        raising=False,
    )

    with pytest.raises(ValueError, match="reparse point"):
        validate_source_root(tmp_path)


def test_rejects_document_over_the_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    import metro_agent.knowledge.source_validation as source_validation

    monkeypatch.setattr(source_validation, "MAX_DOCUMENT_BYTES", 1, raising=False)

    with pytest.raises(ValueError, match="10 MiB"):
        validate_source_root(tmp_path)


def test_rejects_more_than_the_document_count_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_document(tmp_path, "one.md")
    _write_document(tmp_path, "two.md")
    _write_smoke(tmp_path, "one.md")
    import metro_agent.knowledge.source_validation as source_validation

    monkeypatch.setattr(source_validation, "MAX_DOCUMENT_COUNT", 1, raising=False)

    with pytest.raises(ValueError, match="10,000"):
        validate_source_root(tmp_path)


def test_source_hash_changes_for_source_or_smoke_content(tmp_path: Path) -> None:
    _write_document(tmp_path)
    _write_smoke(tmp_path)
    original = validate_source_root(tmp_path).source_tree_sha256

    document = tmp_path / "guides/intro.md"
    document.write_text(
        document.read_text(encoding="utf-8") + "changed\n", encoding="utf-8"
    )
    changed_document = validate_source_root(tmp_path).source_tree_sha256
    smoke_path = tmp_path / "release-smoke-queries.jsonl"
    smoke_path.write_text(
        json.dumps(
            {
                "query": "What changed?",
                "expected_source": "guides/intro.md",
                "minimum_matches": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    changed_smoke = validate_source_root(tmp_path).source_tree_sha256

    assert original != changed_document != changed_smoke


def test_source_hash_is_deterministic_across_document_creation_order(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_document(first_root, "a.md")
    _write_document(first_root, "nested/b.md")
    _write_smoke(first_root, "a.md")
    _write_document(second_root, "nested/b.md")
    _write_document(second_root, "a.md")
    _write_smoke(second_root, "a.md")

    assert (
        validate_source_root(first_root).source_tree_sha256
        == validate_source_root(second_root).source_tree_sha256
    )


def test_clean_git_commit_returns_head_only_for_clean_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import metro_agent.knowledge.source_validation as source_validation

    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[-2:] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, stdout="")
        return subprocess.CompletedProcess(command, 0, stdout="deadbeef\n")

    monkeypatch.setattr(source_validation.subprocess, "run", fake_run)

    assert clean_git_commit(tmp_path) == "deadbeef"
    assert calls == [
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
    ]


def test_clean_git_commit_omits_dirty_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import metro_agent.knowledge.source_validation as source_validation

    monkeypatch.setattr(
        source_validation.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(
            command, 0, stdout=" M source.md\n"
        ),
    )

    assert clean_git_commit(tmp_path) is None
