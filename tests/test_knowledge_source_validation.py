from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from metro_agent.knowledge.source_validation import (
    KnowledgeSourceValidationError,
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
