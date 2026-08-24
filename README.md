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

   将结果写入 `.env`。四项为空时 Compose 会直接拒绝启动，不存在可工作的 `CHANGE_ME` 回退。

3. 构建并启动：

   ```bash
   docker compose up --build -d
   docker compose ps -a
   docker compose logs db-migrate auth-migrate
   ```

   `db-migrate` 和 `auth-migrate` 必须显示退出码 `0`；`app` 的 `/api/health` 必须为 healthy。需要本地模拟工具时使用 `docker compose --profile mock up --build -d`。

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

1. 停止写流量，备份 PostgreSQL、Redis、知识索引 `chroma_db` 和长期记忆 `memory_chroma_db`，并记录两个 Alembic revision、镜像 digest、Chroma collection 名称与快照校验值。长期记忆备份必须与 PostgreSQL 映射前快照属于同一变更窗口。
2. 在 MySQL 管理视图确认目标账号 ID，形成经审批的 `legacy_owner -> auth:<id>` 映射表；一名旧用户只能映射到一个新账号。该审批映射必须同时用于 PostgreSQL owner/Trace 和 `memory_chroma_db` 元数据，不能分别猜测。
3. 先做冲突和影响检查。以下模板使用 `psql` 变量的安全字面量引用；不要拼接未经审核的 SQL，也不要全量更新数字 owner：

   ```sql
   \set legacy_owner 'legacy-alice'
   \set target_owner 'auth:42'
   BEGIN;
   SELECT owner_id, count(*) FROM conversations
     WHERE owner_id IN (:'legacy_owner', :'target_owner') GROUP BY owner_id;
   SELECT user_id, count(*) FROM agent_traces
     WHERE user_id IN (:'legacy_owner', :'target_owner') GROUP BY user_id;
   SELECT owner_id, count(*) FROM conversations
     WHERE owner_id ~ '^[0-9]+$' GROUP BY owner_id ORDER BY owner_id;
   ROLLBACK;
   ```

4. 逐条映射并核对影响行数：

   ```sql
   \set legacy_owner 'legacy-alice'
   \set target_owner 'auth:42'
   BEGIN;
   UPDATE conversations SET owner_id = :'target_owner'
     WHERE owner_id = :'legacy_owner';
   UPDATE agent_traces SET user_id = :'target_owner'
     WHERE user_id = :'legacy_owner';
   SELECT owner_id, count(*) FROM conversations
     WHERE owner_id = :'target_owner' GROUP BY owner_id;
   SELECT user_id, count(*) FROM agent_traces
     WHERE user_id = :'target_owner' GROUP BY user_id;
   COMMIT;
   ```

5. `conversation_messages` 通过 `conversation_id` 随父会话归属，无需单独更新；Trace 子表通过 `trace_id` 关联，也不得直接改写。历史 JSON artifact 如含旧用户标识，应按保留策略单独清点，不能用无条件 JSON 替换。
6. 对 `memory_chroma_db` 的长期记忆先按 metadata `user_id = legacy_owner` 导出 ID、完整 metadata 和影响数量，再仅把这批记录的 `user_id` 更新为经审批的 `auth:<id>`。不得修改 `chroma_db` 知识索引，不得用全文替换猜测身份。验证旧 ID 计数为 `0`、目标 ID 计数按影响数量增加，并抽查内容、向量检索和跨用户不可见。
7. Redis checkpoint 是短期状态。排空旧请求后，仅删除已确认映射用户的旧 checkpoint，要求用户从已迁移的 PostgreSQL 历史开启新轮次；不要猜测或批量改写 Redis 内部键格式。
8. 验证目标用户可见、其他用户得到 `404`，保存 SQL、Chroma 导出、审批人、备份位置、影响数量、验证证据与回滚结果。数据库回滚使用事务/备份；长期记忆回滚使用变更前 `memory_chroma_db` 快照或导出的原 metadata 恢复，并再次验证旧身份与目标身份计数。

## 运维基线

- **健康与迁移**：监控 `/api/health`、容器健康状态和两个一次性迁移作业；每次发布记录主库与身份库 revision。
- **备份恢复**：分别备份 MySQL、PostgreSQL、知识索引 `chroma_db` 和用户长期记忆 `memory_chroma_db`；Redis checkpoint 可按短期状态重建。恢复演练必须在隔离环境完成并记录 RPO/RTO、长期记忆备份校验和长期记忆回滚结果。
- **秘密与日志**：`.env` 仅限部署账号读取，定期轮换数据库密码。轮换 `AUTH_SESSION_PEPPER` 会使全部现有会话失效。日志和外部观测不得包含密码、原始 Cookie、完整工具敏感参数。
- **升级回滚**：发布前备份，记录镜像 digest；迁移成功后再切换镜像。回滚前检查 Alembic downgrade 是否会丢数据，默认优先前滚修复，不能盲目降库。
- **HTTPS**：正式环境必须由可信反向代理终止 TLS，设置 `AUTH_COOKIE_SECURE=true`，限制来源、请求体和访问日志，并验证 Cookie 属性。
- **验证**：离线执行 `python -m pytest -m "not live and not integration"`；真实 MySQL 集成测试只允许对名称以 `_test` 结尾的专用库运行。

## 已知限制

- 登录限流是**单进程内存限流**；多副本部署必须改为 Redis 等共享原子限流器。
- 当前为同步 Agent 执行，用户被禁用时会在开始执行和写历史前复核，但执行中的模型/工具调用无法协作取消。
- 无 SSO、LDAP、OAuth、密码找回和自注册；账号生命周期依赖管理员。
- 真实工具的细粒度权限、写操作二次确认、统一熔断和敏感参数治理不在本次身份改造范围。
- Langfuse 不替换本地业务 Trace。未来可作为外部 LLMOps 补充 Trace、Prompt、数据集与持续评测，但接入前必须确定字段脱敏、数据保留、网络边界和故障降级策略。

完整架构见 `docs/architecture/overview.md`，试点验收记录使用 `docs/operations/internal-pilot-auth-acceptance.md`。
