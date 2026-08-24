from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_readme_states_pilot_scope_quality_and_deployment_flow() -> None:
    readme = _read("README.md")

    for expected in (
        "单组织私有化内部试点",
        "answer_relevance=0.8635",
        "answer_accuracy=0.7235",
        "不算模型质量验收通过",
        "docker compose up --build -d",
        "bootstrap_admin",
        "AUTH_COOKIE_SECURE=true",
        "无自注册",
    ):
        assert expected in readme
    assert "user_id request field is development-only" not in readme


def test_readme_documents_isolation_legacy_migration_and_limits() -> None:
    readme = _read("README.md")

    for expected in (
        "owner_id = auth:<id>",
        "checkpoint",
        "裸数字 owner",
        "单进程内存限流",
        "同步 Agent",
        "无 SSO",
        "Langfuse",
        "不替换本地业务 Trace",
    ):
        assert expected in readme


def test_architecture_and_acceptance_documents_cover_trust_boundaries() -> None:
    architecture = _read("docs/architecture/overview.md")
    acceptance = _read("docs/operations/internal-pilot-auth-acceptance.md")

    for expected in (
        "信任边界",
        "MySQL",
        "PostgreSQL",
        "Redis",
        "auth:<id>",
        "Langfuse",
    ):
        assert expected in architecture

    for expected in (
        "镜像 digest",
        "Alembic revision",
        "401",
        "403",
        "跨用户 404",
        "Secure Cookie",
        "备份恢复",
        "MySQL 并发锁",
        "legacy",
        "模型质量未过门槛",
        "签字",
    ):
        assert expected in acceptance


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
