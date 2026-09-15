from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_readme_states_pilot_scope_quality_architecture_and_deployment_flow() -> None:
    readme = _read("README.md")

    for expected in (
        "单组织私有化内部试点",
        "answer_relevance=0.8635",
        "answer_accuracy=0.7235",
        "不算模型质量验收通过",
        "MySQL",
        "PostgreSQL",
        "Redis",
        "auth:<id>",
        "chroma_db",
        "memory_chroma_db",
        "docker compose --env-file $envFile --project-name metro-agent-pilot --profile knowledge-admin up --build -d",
        "bootstrap_admin",
        "AUTH_COOKIE_SECURE=true",
        "PILOT_FQDN",
        "无自注册",
    ):
        assert expected in readme
    assert "docs/" not in readme


def test_env_example_lists_required_pilot_secrets() -> None:
    env_example = _read(".env.example")
    for variable in (
        "POSTGRES_PASSWORD=",
        "AUTH_MYSQL_PASSWORD=",
        "MYSQL_ROOT_PASSWORD=",
        "AUTH_SESSION_PEPPER=",
    ):
        assert variable in env_example
    assert "正式 HTTPS 部署必须改为 true" in env_example


def test_compose_keeps_internal_data_services_unpublished() -> None:
    compose = _read("compose.yml")
    for service in ("postgres:", "mysql:", "redis:", "chroma:", "knowledge-publisher:"):
        assert service in compose
    assert "knowledge-e2e:" not in compose
    assert "./fixtures/" not in compose


def test_read_only_deployment_verifier_and_caddy_template_define_https_contract() -> None:
    verifier_path = ROOT / "deploy" / "operations" / "verify-deployment.ps1"
    caddyfile_path = ROOT / "deploy" / "proxy" / "Caddyfile.example"

    assert verifier_path.exists()
    assert caddyfile_path.exists()
    verifier = verifier_path.read_text(encoding="utf-8")
    caddyfile = caddyfile_path.read_text(encoding="utf-8")

    for expected in (
        "docker compose --env-file",
        "config --quiet",
        "ps --format json",
        "ConvertFrom-Json -AsHashtable",
        '["services"]["app"]["ports"]',
        "POSTGRES_PASSWORD",
        "AUTH_MYSQL_PASSWORD",
        "MYSQL_ROOT_PASSWORD",
        "AUTH_SESSION_PEPPER",
        "MODEL_DIR",
        "KNOWLEDGE_SOURCE_DIR",
        "KNOWLEDGE_PUBLISHER_INTERNAL_BEARER_SECRET",
        "APP_HOST_PORT",
    ):
        assert expected in verifier
    assert "$config.services" not in verifier
    assert "$value" not in verifier
    assert not re.search(
        r"docker compose[^\r\n]*(?:\s)(?:up|down|start|stop|rm)\b",
        verifier,
    )

    assert "{$PILOT_FQDN}" in caddyfile
    assert "reverse_proxy 127.0.0.1:{$APP_HOST_PORT:8000}" in caddyfile
    assert "header_up Host {host}" in caddyfile
    assert "header_up X-Forwarded-Proto {scheme}" in caddyfile
