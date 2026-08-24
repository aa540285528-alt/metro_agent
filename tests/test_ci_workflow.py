from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_keeps_offline_checks_and_secret_scan() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '-m "not live and not integration"' in workflow
    assert "gitleaks" in workflow.lower()
    assert "ruff check" in workflow


def test_ci_builds_image_and_runs_mysql_auth_integration() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "docker build" in workflow
    assert "mysql:8" in workflow
    assert "alembic-auth.ini upgrade head" in workflow
    assert "tests/integration/test_auth_mysql.py" in workflow
    assert "AUTH_DATABASE_URL" in workflow
