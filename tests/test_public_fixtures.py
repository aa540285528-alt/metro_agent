from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_fixtures_are_synthetic_and_documented() -> None:
    fixture_root = ROOT / "fixtures"
    fixture_text = "\n".join(
        file.read_text(encoding="utf-8") for file in fixture_root.rglob("*") if file.is_file()
    )
    assert "API_KEY=" not in fixture_text
    assert "身份证" not in fixture_text
    assert "简历" not in fixture_text
    assert (fixture_root / "evaluation" / "sample_cases.jsonl").is_file()


def test_public_documentation_covers_architecture_and_evaluation() -> None:
    assert (ROOT / "docs" / "architecture" / "overview.md").is_file()
    assert (ROOT / "docs" / "evaluation.md").is_file()
