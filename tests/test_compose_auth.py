from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def test_compose_defines_isolated_healthy_datastores() -> None:
    compose = _compose()
    services = compose["services"]

    assert services["mysql"]["image"] == "mysql:8.4"
    assert services["mysql"]["environment"]["MYSQL_DATABASE"] == "metro_auth"
    assert services["mysql"]["environment"]["MYSQL_USER"] == "metro_auth"
    assert "mysqladmin ping" in " ".join(services["mysql"]["healthcheck"]["test"])
    assert "utf8mb4" in " ".join(services["mysql"]["command"])

    for name in ("mysql", "postgres", "redis"):
        assert "ports" not in services[name]
        assert "healthcheck" in services[name]

    assert "mysql_data:/var/lib/mysql" in services["mysql"]["volumes"]
    assert set(compose["volumes"]) >= {"mysql_data", "postgres_data"}


def test_compose_runs_migrations_before_starting_app() -> None:
    services = _compose()["services"]

    assert services["db-migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["auth-migrate"]["command"] == [
        "alembic",
        "-c",
        "alembic-auth.ini",
        "upgrade",
        "head",
    ]
    assert services["db-migrate"]["depends_on"]["postgres"]["condition"] == (
        "service_healthy"
    )
    assert services["auth-migrate"]["depends_on"]["mysql"]["condition"] == (
        "service_healthy"
    )
    assert services["db-migrate"]["restart"] == "no"
    assert services["auth-migrate"]["restart"] == "no"

    app_dependencies = services["app"]["depends_on"]
    assert app_dependencies["db-migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert app_dependencies["auth-migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert app_dependencies["redis"]["condition"] == "service_healthy"


def test_compose_injects_auth_configuration_and_limits_public_ports() -> None:
    services = _compose()["services"]
    app = services["app"]
    environment = app["environment"]

    assert app["ports"] == ["127.0.0.1:8000:8000"]
    assert "postgres:5432/metro_agent" in environment["DATABASE_URL"]
    assert "mysql:3306/metro_auth" in environment["AUTH_DATABASE_URL"]
    assert environment["SHORT_TERM_MEMORY_REDIS_URL"] == "redis://redis:6379"
    assert environment["WIREMOCK_BASE_URL"] == "http://wiremock:8080"
    assert "AUTH_SESSION_PEPPER" in environment
    assert "AUTH_COOKIE_SECURE" in environment
    assert "AUTH_SESSION_TTL_SECONDS" in environment
    for provider_variable in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "GLM_API_KEY",
        "GLM_API_URL",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "OLLAMA_BASE_URL",
    ):
        assert provider_variable in environment
    assert "api/health" in " ".join(app["healthcheck"]["test"])
    assert app["restart"] == "unless-stopped"

    assert services["wiremock"]["profiles"] == ["mock"]
    assert "depends_on" not in services["mysql"]


def test_compose_requires_database_passwords_and_session_pepper() -> None:
    services = _compose()["services"]
    serialized = yaml.safe_dump(services, allow_unicode=True)

    for variable in (
        "POSTGRES_PASSWORD",
        "AUTH_MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
        "AUTH_SESSION_PEPPER",
    ):
        assert f"${{{variable}:?" in serialized

    assert "CHANGE_ME" not in serialized
