# Internal Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为内部试点实现 MySQL 本地账号、用户名密码登录、Cookie 会话、admin/user 权限和服务端数据隔离。

**Architecture:** 新增 metro_agent.auth 模块，单独连接 AUTH_DATABASE_URL 指向的 MySQL；PostgreSQL 保留会话历史、Trace 和评测数据。浏览器只持有 HttpOnly 随机会话 Cookie，所有业务 user_id 均由 FastAPI 认证依赖推导。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、MySQL 8、PyMySQL、pwdlib Argon2、pytest、Docker Compose。

---

## 文件边界

| 文件 | 职责 |
|---|---|
| src/metro_agent/auth/models.py | 用户、会话和认证审计的 MySQL ORM 模型。 |
| src/metro_agent/auth/database.py | AUTH_DATABASE_URL 的 Engine 和会话工厂。 |
| src/metro_agent/auth/passwords.py | 用户名规范化、密码策略、Argon2 和令牌哈希。 |
| src/metro_agent/auth/service.py | 用户、会话、撤销和审计事务。 |
| src/metro_agent/auth/dependencies.py | 当前用户和管理员 FastAPI 依赖。 |
| src/metro_agent/auth/router.py | 登录、登出、当前用户和用户管理接口。 |
| src/metro_agent/auth/bootstrap_admin.py | 交互式首个管理员初始化命令。 |
| auth_alembic 与 alembic-auth.ini | MySQL 独立迁移历史。 |
| src/metro_agent/api.py | 认证路由注册和业务接口身份替换。 |
| src/metro_agent/static/preview.html | 登录界面、登出、Cookie 身份迁移。 |

### Task 1: 建立独立 MySQL 认证库

**Files:**
- Create: src/metro_agent/auth/__init__.py
- Create: src/metro_agent/auth/models.py
- Create: src/metro_agent/auth/database.py
- Create: auth_alembic/env.py
- Create: auth_alembic/versions/20260824_01_create_auth_tables.py
- Create: alembic-auth.ini
- Modify: pyproject.toml
- Modify: .env.example
- Test: tests/test_auth_models.py

- [ ] **Step 1: 写失败测试。**

~~~python
from metro_agent.auth.models import AuthAuditEvent, AuthSession, AuthUser

def test_auth_models_are_separate_and_complete() -> None:
    assert set(AuthUser.metadata.tables) == {"users", "auth_sessions", "auth_audit_events"}
    assert AuthSession.__table__.c.token_hash.unique is True
    assert AuthAuditEvent.__table__.c.metadata_json.nullable is False
~~~

- [ ] **Step 2: 运行并确认失败。**

Run: python -m pytest tests/test_auth_models.py -q

Expected: ModuleNotFoundError: No module named metro_agent.auth.

- [ ] **Step 3: 添加依赖与配置。**

在 pyproject.toml 主依赖中添加 pymysql>=1.1,<2 和 pwdlib[argon2]>=0.2,<1。在 .env.example 添加：

~~~dotenv
AUTH_DATABASE_URL=mysql+pymysql://metro_auth:CHANGE_ME@localhost:3306/metro_auth
AUTH_SESSION_PEPPER=CHANGE_ME
AUTH_COOKIE_SECURE=false
AUTH_SESSION_TTL_SECONDS=28800
~~~

- [ ] **Step 4: 实现 ORM 和迁移。**

定义 AuthBase；AuthUser 包含 id、唯一 username、password_hash、role、is_active、created_at、updated_at、last_login_at。AuthSession 包含 id、user_id 外键、唯一 token_hash、created_at、expires_at、revoked_at。AuthAuditEvent 包含 id、actor_user_id、subject_user_id、event_type、非空 metadata_json、occurred_at。

database.py 只能读取 AUTH_DATABASE_URL。auth_alembic/env.py 复制现有 Alembic 流程，但 target_metadata 必须为 AuthBase.metadata，URL 必须为 AUTH_DATABASE_URL。第一条迁移创建三张表，并为 auth_sessions.user_id、auth_sessions.expires_at、auth_audit_events.occurred_at 建索引。

- [ ] **Step 5: 验证。**

Run: python -m pytest tests/test_auth_models.py -q

Run: python -m alembic -c alembic-auth.ini upgrade head --sql

Expected: 测试通过，SQL 输出创建 users、auth_sessions、auth_audit_events 三张表。

- [ ] **Step 6: Commit。**

~~~bash
git add pyproject.toml .env.example src/metro_agent/auth auth_alembic alembic-auth.ini tests/test_auth_models.py
git commit -m "feat: add MySQL authentication schema"
~~~

### Task 2: 密码、会话与审计服务

**Files:**
- Create: src/metro_agent/auth/passwords.py
- Create: src/metro_agent/auth/service.py
- Test: tests/test_auth_service.py

- [ ] **Step 1: 写失败测试。**

~~~python
from datetime import timedelta
from metro_agent.auth.passwords import normalize_username, password_hash, verify_password

def test_password_hash_and_normalized_username() -> None:
    assert normalize_username("  Metro.Admin ") == "metro.admin"
    encoded = password_hash("CorrectHorseBattery1")
    assert encoded != "CorrectHorseBattery1"
    assert verify_password("CorrectHorseBattery1", encoded) is True
    assert verify_password("wrong", encoded) is False

def test_disabling_user_invalidates_session(auth_service) -> None:
    user = auth_service.create_user("operator", "CorrectHorseBattery1", "user", None)
    raw_token = auth_service.create_session(user.id, timedelta(hours=8), None)
    auth_service.set_user_active(user.id, False, user.id)
    assert auth_service.resolve_session(raw_token) is None
~~~

- [ ] **Step 2: 运行并确认失败。**

Run: python -m pytest tests/test_auth_service.py -q

Expected: 导入失败，因为 passwords.py 和 service.py 尚不存在。

- [ ] **Step 3: 实现安全原语和服务。**

normalize_username 执行 strip().lower()，只接受 a-z、0-9、点、下划线和连字符，长度 3 至 64。密码最少 12 位，且至少同时含字母和数字。使用 pwdlib.PasswordHash.recommended() 生成和校验 Argon2。

会话令牌使用 secrets.token_urlsafe(32) 生成；用 AUTH_SESSION_PEPPER 与 SHA-256 得到 token_hash。AuthService 提供 create_user、authenticate、create_session、resolve_session、revoke_session、set_user_active、reset_password、list_users 和 list_audit_events。每次写操作在同一事务中插入 AuthAuditEvent；resolve_session 必须同时检查未撤销、未过期和用户 is_active。

- [ ] **Step 4: 补充撤销、过期和审计测试。**

~~~python
def test_expired_or_revoked_tokens_cannot_resolve(auth_service) -> None:
    user = auth_service.create_user("dispatcher", "CorrectHorseBattery1", "user", None)
    expired = auth_service.create_session(user.id, timedelta(seconds=-1), None)
    active = auth_service.create_session(user.id, timedelta(hours=1), None)
    auth_service.revoke_session(active, user.id, user.id)
    assert auth_service.resolve_session(expired) is None
    assert auth_service.resolve_session(active) is None
    assert auth_service.list_audit_events()[-1].event_type == "session_revoked"
~~~

- [ ] **Step 5: 验证。**

Run: python -m pytest tests/test_auth_service.py -q

Expected: 密码、会话过期、撤销、禁用和审计断言全部通过。

- [ ] **Step 6: Commit。**

~~~bash
git add src/metro_agent/auth/passwords.py src/metro_agent/auth/service.py tests/test_auth_service.py
git commit -m "feat: add local account session service"
~~~

### Task 3: 认证路由与后端强制授权

**Files:**
- Create: src/metro_agent/auth/dependencies.py
- Create: src/metro_agent/auth/router.py
- Modify: src/metro_agent/api.py
- Test: tests/test_auth_api.py

- [ ] **Step 1: 写失败 API 测试。**

~~~python
def test_login_me_logout_and_role_protection(client) -> None:
    assert client.get("/api/auth/me").status_code == 401
    login = client.post("/api/auth/login", json={"username": "admin", "password": "CorrectHorseBattery1"})
    assert login.status_code == 200
    assert login.json() == {"id": "admin-id", "username": "admin", "role": "admin"}
    assert "metro_session" in login.headers["set-cookie"]
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401

def test_user_cannot_access_admin_routes(user_client) -> None:
    assert user_client.get("/api/monitoring/summary").status_code == 403
    assert user_client.get("/api/admin/users").status_code == 403
~~~

- [ ] **Step 2: 运行并确认失败。**

Run: python -m pytest tests/test_auth_api.py -q

Expected: 404，因为认证路由尚未注册。

- [ ] **Step 3: 实现认证依赖与路由。**

定义不可变 CurrentUser(id, username, role)。get_current_user 从 metro_session Cookie 读取令牌，调用 AuthService.resolve_session；缺失、无效、过期、撤销和禁用均返回 HTTP 401 Not authenticated。require_admin 对非 admin 返回 HTTP 403 Admin role required。

实现 POST /api/auth/login、POST /api/auth/logout、GET /api/auth/me；登录成功后设置 Cookie：

~~~python
response.set_cookie(
    key="metro_session", value=raw_token, httponly=True,
    secure=settings.auth_cookie_secure, samesite="lax",
    max_age=settings.auth_session_ttl_seconds, path="/",
)
~~~

实现仅管理员可用的 POST /api/admin/users、GET /api/admin/users 和 PATCH /api/admin/users/{user_id}。响应模型绝不能包含 password_hash 或 token_hash。

- [ ] **Step 4: 替换 api.py 的不可信 user_id。**

删除 ChatRequest.user_id，删除所有会话与监控接口的 Query user_id。聊天、会话列表、详情、修改和删除路由均注入 CurrentUser，并把 current_user.id 传给 run_chat 和 ConversationHistoryService。全部 /api/monitoring 路由注入 require_admin，MonitoringFilter 不再含 user_id。create_app 新增 auth_service_factory，供测试注入临时认证库。

- [ ] **Step 5: 写跨用户和禁用回归测试。**

~~~python
def test_user_cannot_read_another_users_conversation(admin_client, user_client) -> None:
    thread_id = "admin-thread"
    assert admin_client.post("/api/chat/stream", json={"thread_id": thread_id, "message": "查询"}).status_code == 200
    assert user_client.get(f"/api/conversations/{thread_id}").status_code == 404

def test_disabled_user_session_is_rejected(admin_client, user_client, user_id) -> None:
    assert admin_client.patch(f"/api/admin/users/{user_id}", json={"is_active": False}).status_code == 200
    assert user_client.get("/api/conversations").status_code == 401
~~~

- [ ] **Step 6: 验证。**

Run: python -m pytest tests/test_auth_api.py -q

Expected: 认证、401、403、跨用户 404 和禁用立即失效均通过。

- [ ] **Step 7: Commit。**

~~~bash
git add src/metro_agent/auth/dependencies.py src/metro_agent/auth/router.py src/metro_agent/api.py tests/test_auth_api.py
git commit -m "feat: enforce authenticated API access"
~~~

### Task 4: 管理员初始化和登录限流

**Files:**
- Create: src/metro_agent/auth/bootstrap_admin.py
- Create: src/metro_agent/auth/rate_limit.py
- Modify: src/metro_agent/auth/router.py
- Test: tests/test_auth_bootstrap.py
- Test: tests/test_auth_rate_limit.py

- [ ] **Step 1: 写失败测试。**

~~~python
def test_bootstrap_creates_only_one_named_admin(monkeypatch, auth_service) -> None:
    monkeypatch.setattr("metro_agent.auth.bootstrap_admin.getpass", lambda _: "CorrectHorseBattery1")
    assert run_bootstrap(auth_service, "admin") == 0
    assert run_bootstrap(auth_service, "admin") == 1

def test_sixth_failed_login_is_rate_limited(client) -> None:
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"}).status_code == 429
~~~

- [ ] **Step 2: 运行并确认失败。**

Run: python -m pytest tests/test_auth_bootstrap.py tests/test_auth_rate_limit.py -q

Expected: 导入失败。

- [ ] **Step 3: 实现初始化与限流。**

bootstrap_admin 使用 argparse 接收必填 --username；使用 getpass 两次读取和确认密码；调用 create_user(username, password, "admin", None)。重复账号、密码不一致和策略错误必须返回非零，不得输出密码。

LoginRateLimiter 以规范化用户名与客户端地址为键，在 60 秒内允许 5 次失败；第六次返回 429。成功登录清除该键。使用线程锁；模块注释明确多副本部署时改用 Redis。

- [ ] **Step 4: 验证。**

Run: python -m pytest tests/test_auth_bootstrap.py tests/test_auth_rate_limit.py -q

Expected: 初始化、重复保护、失败限流和成功清零均通过。

- [ ] **Step 5: Commit。**

~~~bash
git add src/metro_agent/auth/bootstrap_admin.py src/metro_agent/auth/rate_limit.py src/metro_agent/auth/router.py tests/test_auth_bootstrap.py tests/test_auth_rate_limit.py
git commit -m "feat: add auth bootstrap and login throttling"
~~~

### Task 5: 浏览器登录态和 user_id 清理

**Files:**
- Modify: src/metro_agent/static/preview.html
- Test: tests/test_preview_auth.py

- [ ] **Step 1: 写失败的静态契约测试。**

~~~python
from pathlib import Path

def test_preview_uses_cookie_auth_not_browser_user_id() -> None:
    page = Path("src/metro_agent/static/preview.html").read_text(encoding="utf-8")
    assert "/api/auth/me" in page
    assert "/api/auth/login" in page
    assert "metro-chat-user-id" not in page
    assert "user_id:" not in page
    assert "?user_id=" not in page
~~~

- [ ] **Step 2: 运行并确认失败。**

Run: python -m pytest tests/test_preview_auth.py -q

Expected: 失败，因为页面仍保存 metro-chat-user-id 并发送 user_id。

- [ ] **Step 3: 实现页面迁移。**

增加用户名、密码和通用错误提示组成的登录视图。启动时请求 /api/auth/me：成功后显示现有工作区，401 时只显示登录视图。删除 STORAGE_KEYS.userId、browserUserId、monitoringUrl 的 user_id 参数、会话 URL 的查询参数和聊天 JSON 的 user_id。收到任何 401 时清空内存会话状态并返回登录视图。用户菜单显示用户名，登出调用 POST /api/auth/logout。仅 admin 显示并加载监控视图。

- [ ] **Step 4: 验证。**

Run: python -m pytest tests/test_preview_auth.py -q

Expected: 页面不再保存或发送客户端 user_id，仍保留 thread_id 的本地工作状态。

- [ ] **Step 5: Commit。**

~~~bash
git add src/metro_agent/static/preview.html tests/test_preview_auth.py
git commit -m "feat: add login state to preview client"
~~~

### Task 6: Compose、迁移和试点验收

**Files:**
- Modify: compose.yml
- Modify: .env.example
- Modify: README.md
- Modify: docs/architecture/overview.md
- Create: docs/operations/internal-pilot-auth-acceptance.md
- Test: tests/test_compose_auth.py

- [ ] **Step 1: 写失败部署配置测试。**

~~~python
from pathlib import Path

def test_compose_declares_private_auth_mysql_and_migration() -> None:
    compose = Path("compose.yml").read_text(encoding="utf-8")
    assert "mysql:" in compose
    assert "auth-migrate:" in compose
    assert "AUTH_DATABASE_URL" in compose
    assert "mysql_data:" in compose
    assert "127.0.0.1:3306:3306" not in compose
~~~

- [ ] **Step 2: 运行并确认失败。**

Run: python -m pytest tests/test_compose_auth.py -q

Expected: 失败，因为 Compose 未定义 MySQL 或认证迁移服务。

- [ ] **Step 3: 实现部署配置。**

新增 MySQL 8 服务，使用 metro_auth 数据库与命名卷 mysql_data，不暴露 ports，并使用 mysqladmin ping 健康检查。新增 auth-migrate 一次性服务，执行 python -m alembic -c alembic-auth.ini upgrade head，等待 MySQL 健康后退出。app 注入 AUTH_DATABASE_URL 并依赖 auth-migrate 的成功完成；app、PostgreSQL 和 Redis 增加健康检查。

README 写明：设置 PostgreSQL/MySQL 密码和 AUTH_SESSION_PEPPER，docker compose up --build -d，使用 docker compose run --rm app python -m metro_agent.auth.bootstrap_admin --username admin 创建首个管理员。架构文档写明 MySQL 身份库和 PostgreSQL 业务库隔离，生产环境 AUTH_COOKIE_SECURE 必须为 true。验收模板必须记录镜像 digest、迁移 revision、初始化记录、401/403/404/禁用测试、秘密扫描和 HTTPS Secure Cookie 结果。

- [ ] **Step 4: 验证。**

Run: python -m pytest tests/test_compose_auth.py tests/test_project_metadata.py -q

Run: docker compose config

Expected: 测试通过；Compose 可解析；MySQL 无主机端口映射；app 持有 AUTH_DATABASE_URL。

- [ ] **Step 5: 完整回归。**

Run: python -m ruff check src tests

Run: python -m pytest -m "not live and not integration" -q

Expected: Ruff 无错误，pytest 无失败。

- [ ] **Step 6: Commit。**

~~~bash
git add compose.yml .env.example README.md docs/architecture/overview.md docs/operations/internal-pilot-auth-acceptance.md tests/test_compose_auth.py
git commit -m "feat: deploy internal authentication services"
~~~

## 自检

- 任务 1 和 2 覆盖 MySQL 用户、密码、会话与审计。
- 任务 3 覆盖服务端身份推导、admin/user 授权和跨用户隔离。
- 任务 4 覆盖管理员写入 MySQL 的初始化路径与暴力破解控制。
- 任务 5 清除前端可伪造 user_id。
- 任务 6 覆盖独立部署、迁移和试点验收。
- 计划未引入公开注册、邮件、密码找回、SSO、多租户或工具/知识库细粒度权限。
