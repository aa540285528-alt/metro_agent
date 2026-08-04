from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_only_offline_checks_and_secret_scan() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '-m "not live and not integration"' in workflow
    assert "gitleaks" in workflow.lower()
    assert "ruff check" in workflow
