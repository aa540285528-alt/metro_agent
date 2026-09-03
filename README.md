# Metro Agent

Metro Agent 是面向地铁通信运维场景的多 Agent 助手。本仓库包含多 Agent 编排、RAG、实时工具、历史会话、本地 Trace 与离线评测基础，以及用于小范围内部使用的用户名密码认证。

当前交付定位是**单组织私有化内部试点**，不是通用 SaaS，也不宣称已经达到正式生产交付标准。公开仓库只包含源码、合成样例、确定性测试和部署说明，不包含生产密钥、真实运维记录、会话数据或知识索引。

## 当前质量结论

真实 Agent 验收中，路径和安全指标已达到当前门槛，但语义指标仍未通过：`answer_relevance=0.8635`、`answer_accuracy=0.7235`，两者都低于 `0.9`。因此当前版本**不算模型质量验收通过**，不得在验收报告中改写成“模型质量已达标”。

## 快速部署

前置条件：Docker Engine、Docker Compose v2，以及只允许本机或受控反向代理访问的服务器。

1. 创建本地配置，不要提交 `.env`：

   ```bash
   cp .env.example .env
   ```

2. 为 `POSTGRES_PASSWORD`、`AUTH_MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD` 分别生成独立的 URL 安全高熵值；为 `AUTH_SESSION_PEPPER` 生成至少 32 字节随机值。例如每次单独执行：

   ```bash
   python -c "import secrets; print(secrets.token_hex(24))"
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   将结果写入 `.env`。必填的密钥或路径缺失时 Compose 会直接拒绝启动，不存在可工作的 `CHANGE_ME` 回退。另将 `MODEL_DIR` 设置为宿主机模型根目录，该目录必须包含 `bge-m3/` 和 `bge-reranker/`；Compose 会把它只读挂载为容器内 `/models`。Windows 可填写 `D:/models`，Linux 可填写 `/srv/metro-agent/models`。

3. 构建并启动：

   ```bash
   docker compose up --build -d
   docker compose ps -a
   docker compose logs db-migrate auth-migrate
   ```

   `db-migrate` 和 `auth-migrate` 必须显示退出码 `0`；`/api/health` 仅表示进程存活，Compose 以会检查 MySQL、PostgreSQL、Redis 的 `/api/ready` 判断是否可接流量。需要本地模拟工具时使用 `docker compose --profile mock up --build -d`。

4. 首次且仅首次，交互式写入第一个管理员：

   ```bash
   docker compose run --rm app python -m metro_agent.auth.bootstrap_admin --username admin
   ```

   `run --rm` 会创建执行后即删除的一次性容器，并按 `app` 的 `depends_on` 启动和等待数据库健康、两项迁移完成及 Redis healthy 等依赖；不要求常驻 `app` 容器已经运行。密码不会作为命令行参数保存。并发执行时 MySQL 命名锁保证最终只创建一个首管理员。

5. 本机试用访问 `http://127.0.0.1:8000`。正式内部访问必须放在 HTTPS 反向代理之后，将 `.env` 设置为 `AUTH_COOKIE_SECURE=true`，重建应用，并确认浏览器收到 `Secure`、`HttpOnly`、`SameSite=Lax` Cookie。Compose 只把应用绑定到 `127.0.0.1:8000`，MySQL、PostgreSQL 和 Redis 不暴露宿主机端口。

## 账号管理

系统采用管理员预置账号，**无自注册**。管理员登录后可通过同源管理接口执行以下操作：

- `GET /api/admin/users`：查看账号。
- `POST /api/admin/users`：创建 `admin` 或 `user`。
- `PATCH /api/admin/users/{id}`：禁用/启用、改密或修改角色；改密和禁用会撤销活动会话。

写接口要求有效的管理员 Cookie。浏览器请求存在 `Origin` 时，服务端将其与请求基准地址解析为 scheme、hostname 和有效端口，必须规范化后同源；缺失 `Origin` 时允许受控的内部 CLI 调用。应用不直接解析也不信任 `X-Forwarded-For`，反向代理必须覆盖客户端传入的转发头，并只从受信代理向应用转发。除此之外，不允许管理员自降级或自禁用，也不允许禁用或降级最后一个活动管理员。操作会写入 MySQL 审计表；密码使用 Argon2id 哈希，原始会话令牌不落库。

## 数据隔离

| 存储 | 数据与边界 |
|---|---|
| MySQL `metro_auth` | 用户、Argon2id 密码哈希、会话摘要、身份审计；与业务数据物理分库。 |
| PostgreSQL `metro_agent` | 会话历史、Agent Trace、工具调用、Token/时延和评测元数据。 |
| Redis | 短期 LangGraph checkpoint；不是身份真源，也不保存密码。 |
| Chroma `chroma_db` | 已发布知识切片与知识索引；需另行执行知识权限、发布和回滚治理。 |
| Chroma `memory_chroma_db` | 用户长期记忆；按元数据 `user_id` 隔离，长期记忆 `user_id = auth:<id>`。 |

服务端从 Cookie 解析身份，业务归属使用 `owner_id = auth:<id>`。LangGraph checkpoint 使用带身份命名空间的 `auth:<id>:<thread_id>`，长期记忆元数据同样只使用 `auth:<id>`；客户端不能提交 `user_id` 来改变归属。监控接口只允许管理员访问。

## Legacy Owner 显式迁移

旧版本可能使用自由字符串或裸数字 owner。新账号**绝不自动继承裸数字 owner**，即使旧值刚好等于新的 MySQL 自增 ID。每个旧身份必须由管理员确认后显式映射。

先执行协调备份，再使用 `deploy/operations/legacy-owner-preview.sql` 做只读预览。预览事务固定 `ROLLBACK` 并报告精确 `candidate_count`；此后进入**人工停点**，审批人填写 `EXPECTED_COUNT` 和备份引用，才能运行 `legacy-owner-apply.sql`。脚本锁定候选表并验证更新影响数，任何影响数不一致都会中止。不要全量迁移裸数字 owner，也不要把 PostgreSQL 与 `memory_chroma_db` 的映射分别猜测。

完整可执行命令、Chroma 原子目录切换、Redis checkpoint 处理和回滚要求见 `docs/operations/legacy-owner-migration.md`。
协调流程必须同时保留长期记忆备份，并在验收单记录长期记忆回滚证据。

## 运维基线

- **健康与迁移**：`/api/health` 是 liveness，`/api/ready` 会实际执行两次 `SELECT 1` 和 Redis `PING`；任一依赖失败返回不含 DSN 的 `503`。监控容器健康状态和两个一次性迁移作业，每次发布记录主库与身份库 revision。
- **秘密与日志**：`.env` 仅限部署账号读取，定期轮换数据库密码。轮换 `AUTH_SESSION_PEPPER` 会使全部现有会话失效。日志和外部观测不得包含密码、原始 Cookie、完整工具敏感参数。
- **HTTPS**：正式环境必须由可信反向代理终止 TLS，设置 `AUTH_COOKIE_SECURE=true`，限制来源、请求体和访问日志，并验证 Cookie 属性。
- **验证**：离线执行 `python -m pytest -m "not live and not integration"`。CI 显式提供唯一 `AUTH_MYSQL_TEST_RUN_ID` 和完全匹配的 `AUTH_MYSQL_TEST_DATABASE`；测试只接收连接系统库 `mysql` 的 admin URL，并写入 run ID、数据库名、随机 token 三元所有权标记。只有三者精确匹配才允许 downgrade/drop。

### 身份迁移中断恢复

MySQL DDL 隐式提交。auth-migrate 失败会继续阻断 `app`。执行 `deploy/operations/inspect-mysql-partial-ddl.sh` 保存 Alembic 与 `information_schema` 证据；禁止盲目 `stamp`。空身份库可经审批删除专用空身份库重建，已有身份数据必须由 DBA 审核补偿迁移。完整处置见 `docs/operations/mysql-alembic-partial-ddl-recovery.md`。

### MySQL TLS 与证书轮换

`mysql-cert-init` 在只用于本机试点的命名卷生成 CA 与服务端证书，SAN 包含 `mysql`、`localhost`、`127.0.0.1`；CA 私钥生成签发后立即删除。MySQL 强制 `require_secure_transport=ON`，应用和 `auth-migrate` 只读挂载 CA，不接触服务端私钥，并同时验证证书链与主机身份。

轮换前先完成一致性备份并停止应用。执行 `docker compose down`，用 `docker volume ls` 确认当前 Compose 项目名后删除该项目的 `<project>_mysql_ca` 与 `<project>_mysql_server_certs`，运行 `docker compose run --rm mysql-cert-init` 重新生成，再执行 `docker compose up -d` 并验证 `/api/ready`。该流程会更换试点 CA，不能用于多主机信任分发。正式环境应以组织的生产 PKI 替换一次性证书卷，证书 SAN 必须覆盖 Compose 服务名 `mysql`，并保留同等的身份验证参数和私钥隔离。

### 一致性备份、恢复与镜像回滚

使用 `deploy/operations/backup-all.sh` 协调执行 `mysqldump --single-transaction`、`pg_dump`、Chroma 快照和 Redis `SAVE`。知识发布将 Chroma 卷和 artifact 卷作为单一版本化发布单元：备份在 `knowledge:publication` 锁预检后归档 `knowledge-chroma.tar.gz` 与 `knowledge-artifacts.tar.gz` 并写入 `SHA256SUMS`；恢复会先验证 registry/pointer/validated descriptor，再启动应用。release 和 artifact 是审计证据，默认永久保留，**不自动清理**。备份介质的静态加密、密钥保管和保留期限由备份目标负责。`restore-all.sh` 固定按 **MySQL -> PostgreSQL -> Chroma -> Redis** 恢复，并比较两个 Alembic revision、owner-counts 和 `/api/ready`。`rollback-image.sh` 只接受完整 `METRO_AGENT_IMAGE=...@sha256:...`，使用 `--no-build` 回滚镜像。可执行命令和中止条件见 `docs/operations/coordinated-backup-restore.md`。

### 知识发布恢复演练

`knowledge-e2e` profile 只使用仓库中版本化的合规 fixture 与确定性 embedding，不读取 `DEEPSEEK_API_KEY` 或其他模型密钥。首次部署、Chroma 大版本升级和任何恢复流程都必须在隔离环境完成该演练，覆盖构建、发布、查询、重启、回滚与 `verify`；演练不得删除历史 release 或 artifact。

## 已知限制

- 登录限流是**单进程内存限流**；多副本部署必须改为 Redis 等共享原子限流器。
- 当前为同步 Agent 执行，用户被禁用时会在开始执行和写历史前复核，但执行中的模型/工具调用无法协作取消。
- 无 SSO、LDAP、OAuth、密码找回和自注册；账号生命周期依赖管理员。
- 真实工具的细粒度权限、写操作二次确认、统一熔断和敏感参数治理不在本次身份改造范围。
- Langfuse 不替换本地业务 Trace。未来可作为外部 LLMOps 补充 Trace、Prompt、数据集与持续评测，但接入前必须确定字段脱敏、数据保留、网络边界和故障降级策略。

完整架构见 `docs/architecture/overview.md`，试点验收记录使用 `docs/operations/internal-pilot-auth-acceptance.md`。
