from pathlib import Path
import re


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


def test_readme_uses_one_off_bootstrap_container_and_accurate_origin_rules() -> None:
    readme = _read("README.md")

    assert (
        "docker compose run --rm app python -m "
        "metro_agent.auth.bootstrap_admin --username admin"
    ) in readme
    assert "一次性容器" in readme
    assert "依赖" in readme
    assert "请求存在 `Origin` 时" in readme
    assert "规范化后同源" in readme
    assert "缺失 `Origin`" in readme
    assert "内部 CLI" in readme
    assert "不信任 `X-Forwarded-For`" in readme


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
        "chroma_db",
        "memory_chroma_db",
        "长期记忆 `user_id = auth:<id>`",
        "长期记忆备份",
        "长期记忆回滚",
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
        "chroma_db",
        "memory_chroma_db",
        "长期记忆",
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
        "memory_chroma_db",
        "元数据 `user_id`",
        "审批映射",
        "影响数量",
        "长期记忆回滚",
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


def test_docs_cover_mysql_ddl_recovery_tls_rotation_and_app_blocking() -> None:
    readme = _read("README.md")
    acceptance = _read("docs/operations/internal-pilot-auth-acceptance.md")
    recovery = _read("docs/operations/mysql-alembic-partial-ddl-recovery.md")

    for expected in (
        "MySQL DDL 隐式提交",
        "docker compose stop app",
        "alembic -c alembic-auth.ini current",
        "alembic -c alembic-auth.ini show",
        "information_schema",
        "禁止盲目 `stamp`",
        "删除专用空身份库重建",
        "DBA 审核补偿迁移",
        "auth-migrate 失败",
        "阻断 `app`",
        "证书轮换",
        "生产 PKI",
    ):
        assert expected in readme + recovery
    assert "迁移中断恢复演练" in acceptance
    assert "禁止盲目执行 `alembic stamp`" in recovery
    assert "deploy/operations/inspect-mysql-partial-ddl.sh" in recovery


def test_docs_define_guarded_legacy_and_executable_disaster_recovery() -> None:
    readme = _read("README.md")
    acceptance = _read("docs/operations/internal-pilot-auth-acceptance.md")
    legacy = _read("docs/operations/legacy-owner-migration.md")
    recovery = _read("docs/operations/coordinated-backup-restore.md")

    for expected in (
        "人工停点",
        "EXPECTED_COUNT",
        "GET DIAGNOSTICS",
        "影响数不一致",
        "原子目录切换",
        "mysqldump --single-transaction",
        "pg_dump",
        "docker compose stop app",
        "MySQL -> PostgreSQL -> Chroma -> Redis",
        "METRO_AGENT_IMAGE",
        "@sha256:",
    ):
        assert expected in readme + legacy + recovery
    for expected in (
        "一致性备份恢复演练",
        "EXPECTED_COUNT",
        "原子目录切换",
        "digest 回滚",
    ):
        assert expected in acceptance
    assert "备份是 apply 的前置条件" in legacy
    assert "候选数不等于 EXPECTED_COUNT" in legacy
    assert "MySQL -> PostgreSQL -> Chroma -> Redis" in recovery
    assert "完整 digest" in recovery


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
    assert "$value" not in verifier
    assert not re.search(
        r"docker compose[^\r\n]*(?:\s)(?:up|down|start|stop|rm)\b",
        verifier,
    )

    assert "{$PILOT_FQDN}" in caddyfile
    assert "reverse_proxy 127.0.0.1:{$APP_HOST_PORT:8000}" in caddyfile
    assert "header_up Host {host}" in caddyfile
    assert "header_up X-Forwarded-Proto {scheme}" in caddyfile
