from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _jobs() -> dict:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    return workflow["jobs"]


def _run_commands(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_ci_keeps_offline_checks_and_secret_scan() -> None:
    jobs = _jobs()
    test_commands = _run_commands(jobs["test"])
    secret_actions = [
        step["uses"] for step in jobs["secrets"]["steps"] if "uses" in step
    ]

    assert 'pytest -m "not live and not integration" -q' in test_commands
    assert "ruff check src tests" in test_commands
    assert any(action.startswith("gitleaks/gitleaks-action@") for action in secret_actions)


def test_ci_build_job_builds_the_application_image() -> None:
    jobs = _jobs()

    assert "docker build --tag metro-agent:${{ github.sha }} ." in _run_commands(
        jobs["build-image"]
    )


def test_ci_mysql_job_runs_migration_and_dedicated_integration_suite() -> None:
    job = _jobs()["auth-mysql-integration"]
    mysql = job["services"]["mysql"]
    environment = job["env"]
    commands = _run_commands(job)

    assert mysql["image"] == "mysql:8.4"
    assert "MYSQL_DATABASE" not in mysql["env"]
    assert environment["AUTH_MYSQL_ADMIN_URL"].startswith("mysql+pymysql://root:")
    assert environment["AUTH_MYSQL_ADMIN_URL"].endswith("/mysql")
    assert environment["AUTH_MYSQL_TEST_RUN_ID"] == (
        "github-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert environment["AUTH_MYSQL_TEST_DATABASE"] == (
        "metro_auth_test_github_${{ github.run_id }}_${{ github.run_attempt }}"
    )
    assert "AUTH_DATABASE_URL" not in environment
    assert "AUTH_MYSQL_TEST_URL" not in environment
    assert environment["AUTH_SESSION_PEPPER"]
    assert "pytest tests/integration/test_auth_mysql.py -q" in commands
