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
    assert "api/ready" in " ".join(app["healthcheck"]["test"])
    assert app["restart"] == "unless-stopped"

    assert services["wiremock"]["profiles"] == ["mock"]
    assert set(services["mysql"]["depends_on"]) == {"mysql-cert-init"}


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


def test_compose_enforces_verified_mysql_tls_without_exposing_private_key() -> None:
    compose = _compose()
    services = compose["services"]
    cert_init = services["mysql-cert-init"]
    mysql = services["mysql"]

    assert cert_init["image"] == "mysql:8.4"
    assert cert_init["restart"] == "no"
    assert isinstance(cert_init["command"], list)
    assert len(cert_init["command"]) == 1
    cert_script = cert_init["command"][0]
    assert "subjectAltName" in cert_script
    assert all(name in cert_script for name in ("mysql", "localhost", "127.0.0.1"))
    assert "ca-key" in cert_script and "rm" in cert_script
    assert "/ca/ca-key" not in cert_script
    assert "mysql_ca:/ca" in cert_init["volumes"]
    assert "mysql_server_certs:/server" in cert_init["volumes"]
    assert mysql["depends_on"]["mysql-cert-init"]["condition"] == (
        "service_completed_successfully"
    )
    assert any("require-secure-transport=ON" in value for value in mysql["command"])
    assert "VERIFY_IDENTITY" in " ".join(mysql["healthcheck"]["test"])
    assert "--ssl-ca" in " ".join(mysql["healthcheck"]["test"])

    for service_name in ("app", "auth-migrate"):
        service = services[service_name]
        auth_url = service["environment"]["AUTH_DATABASE_URL"]
        assert "ssl_ca=" in auth_url
        assert "ssl_check_hostname=true" in auth_url
        assert "ssl_verify_cert" not in auth_url
        assert "ssl_verify_identity" not in auth_url
        mounts = " ".join(service["volumes"])
        assert "mysql_ca" in mounts
        assert "mysql_server_certs" not in mounts
        assert "server-key.pem" not in mounts

    assert set(compose["volumes"]) >= {
        "mysql_ca",
        "mysql_server_certs",
    }


def test_compose_supports_full_image_reference_for_digest_rollback() -> None:
    services = _compose()["services"]

    for service_name in ("app", "db-migrate", "auth-migrate"):
        assert services[service_name]["image"] == (
            "${METRO_AGENT_IMAGE:-metro-agent:local}"
        )


def test_app_mounts_local_models_read_only() -> None:
    app = _compose()["services"]["app"]

    assert app["environment"]["EMBEDDING_MODEL_PATH"] == "/models/bge-m3"
    assert app["environment"]["RERANK_MODEL_PATH"] == "/models/bge-reranker"
    assert "${MODEL_DIR:?请在 .env 中设置 MODEL_DIR}:/models:ro" in app["volumes"]


def test_redis_includes_search_capability_required_by_checkpointer() -> None:
    redis_service = _compose()["services"]["redis"]
    healthcheck = " ".join(redis_service["healthcheck"]["test"])

    assert redis_service["image"] == "redis:8.4-alpine"
    assert "COMMAND INFO FT.INFO" in healthcheck


def test_compose_persists_redis_memory_and_governed_knowledge_stores_for_recovery() -> None:
    compose = _compose()
    services = compose["services"]
    app = services["app"]
    redis = services["redis"]

    assert app["environment"]["MEMORY_CHROMA_DB_DIR"] == (
        "/var/lib/metro-agent/memory-chroma/current"
    )
    assert "CHROMA_DB_DIR" not in app["environment"]
    assert (
        "memory_chroma_data:/var/lib/metro-agent/memory-chroma"
        in app["volumes"]
    )
    assert "redis_data:/data" in redis["volumes"]
    assert "appendonly" in " ".join(redis["command"]).lower()
    assert set(compose["volumes"]) >= {
        "memory_chroma_data",
        "redis_data",
        "knowledge_chroma_data",
        "knowledge_artifact_data",
    }


def test_compose_isolates_published_knowledge_from_the_application() -> None:
    compose = _compose()
    services = compose["services"]

    assert services["chroma"]["image"] == "chromadb/chroma:1.5.9"
    assert "ports" not in services["chroma"]
    assert services["chroma"]["networks"] == ["knowledge_backend"]
    assert services["knowledge-read-proxy"]["networks"] == [
        "knowledge_frontend",
        "knowledge_backend",
    ]
    assert services["knowledge-read-proxy"]["command"][:2] == [
        "uvicorn",
        "metro_agent.knowledge_read_proxy:app",
    ]
    assert any(value.endswith(":ro") for value in services["knowledge-read-proxy"]["volumes"])
    assert services["app"]["networks"] == ["knowledge_frontend", "app_backend"]
    assert "knowledge_backend" not in services["app"]["networks"]
    assert "KNOWLEDGE_READ_PROXY_URL" not in services["app"]["environment"]


def test_compose_runs_knowledge_indexer_only_as_an_admin_profile() -> None:
    compose = _compose()
    indexer = compose["services"]["knowledge-indexer"]

    assert indexer["profiles"] == ["knowledge-admin"]
    assert indexer["networks"] == ["knowledge_backend", "app_backend"]
    assert any(value.endswith(":ro") for value in indexer["volumes"])
    assert any(not value.endswith(":ro") for value in indexer["volumes"])


def test_knowledge_admin_wrappers_only_accept_governed_commands() -> None:
    shell = (ROOT / "deploy" / "operations" / "knowledge-admin.sh").read_text(
        encoding="utf-8"
    )
    powershell = (ROOT / "deploy" / "operations" / "knowledge-admin.ps1").read_text(
        encoding="utf-8"
    )

    for command in ("build-and-publish", "rollback", "verify", "status"):
        assert command in shell
        assert command in powershell
    assert "id -un" in shell
    assert "$principal.Identity.Name" in powershell
    assert "id -u" in shell
    assert "WindowsPrincipal" in powershell
