# 内部试点身份与部署验收记录

> 适用范围：单组织、小范围、私有化内部试点。每次候选版本单独复制本模板，填写证据位置并签字。未填写项视为未验收。

## 基本信息

| 项目 | 记录 |
|---|---|
| 验收环境/日期 |  |
| Git commit |  |
| 应用镜像 digest |  |
| PostgreSQL Alembic revision |  |
| MySQL 身份库 Alembic revision |  |
| 部署人/复核人 |  |
| 证据归档位置 |  |

## 部署与秘密

- [ ] 全新环境执行 `docker compose up --build -d` 成功；数据库无宿主机端口，应用仅监听 `127.0.0.1:8000`。
- [ ] `db-migrate`、`auth-migrate` 均退出码 `0`，已记录两个 Alembic revision。
- [ ] `/api/health` liveness 与 `/api/ready` readiness 均通过；readiness 已实际覆盖 PostgreSQL、MySQL、Redis。
- [ ] CI 实际完成 Docker image build，并记录应用镜像 digest。
- [ ] `POSTGRES_PASSWORD`、`AUTH_MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD`、至少 32 字节 pepper 均独立生成且未使用样例值。
- [ ] gitleaks/秘密扫描通过；日志抽查不含密码、原始 Cookie、token 或工具敏感参数。
- [ ] HTTPS 反向代理可用，`AUTH_COOKIE_SECURE=true`；浏览器验证 Secure Cookie、`HttpOnly`、`SameSite=Lax`。

## 身份、权限与隔离

- [ ] `bootstrap_admin` 交互执行成功；记录执行人、时间和审计事件，不记录密码。
- [ ] 无 Cookie 访问受保护 API 返回 `401`。
- [ ] 普通用户访问管理员 API 返回 `403`。
- [ ] 用户 A 请求用户 B 的 thread 返回跨用户 404，且没有启动模型/工具、没有写入历史。
- [ ] 禁用用户后旧会话失效；改密后旧会话失效；最后一个活动管理员不可禁用或降级。
- [ ] 登录失败达到阈值后返回 `429` 和有效 `Retry-After`，成功登录后预算行为符合设计。
- [ ] 账号创建、角色变更、禁用、改密、登录和登出均能在 MySQL 审计表追踪。
- [ ] 真实 MySQL 并发锁测试通过：两个 `multiprocessing` spawn 进程跨进程并发 bootstrap，最终只有一个首管理员。

## 数据与恢复

- [ ] MySQL 身份库、PostgreSQL 业务库、知识索引 `chroma_db` 和用户长期记忆 `memory_chroma_db` 均完成加密备份并记录校验值。
- [ ] 在隔离环境完成备份恢复演练，记录 RPO、RTO、校验行数和负责人。
- [ ] 执行身份库 `upgrade -> downgrade -> upgrade` 测试，确认表、外键和指定索引。
- [ ] legacy owner 已盘点并形成审批映射；裸数字 owner 未自动继承，逐项执行 `legacy_owner -> auth:<id>`，PostgreSQL 与 Chroma 使用同一映射。
- [ ] legacy 迁移同时核对 `conversations.owner_id`、`agent_traces.user_id`、`memory_chroma_db` 元数据 `user_id`、关联 artifact 和 Redis 短期 checkpoint；记录每个来源的备份、影响数量、验证结果和回滚证据。
- [ ] `memory_chroma_db` 迁移后旧 `user_id` 计数为零、目标 `auth:<id>` 增量与影响数量一致，抽查检索和跨用户隔离；长期记忆回滚已用快照或原 metadata 演练，且未改写 `chroma_db`。
- [ ] 镜像回滚和数据库前滚修复步骤已演练；任何有损 downgrade 都有单独审批。
- [ ] 迁移中断恢复演练完成：已保存 `information_schema` 对账证据，未盲目 `stamp`，`auth-migrate` 失败时 `app` 保持阻断。
- [ ] legacy 迁移已先运行回滚预览，经人工填写 `EXPECTED_COUNT` 和备份引用后才 apply；不一致时已验证自动中止。
- [ ] 一致性备份恢复演练使用脚本按 MySQL -> PostgreSQL -> Chroma -> Redis 恢复，Chroma 完成原子目录切换并保留回滚目录。
- [ ] digest 回滚仅使用完整 `METRO_AGENT_IMAGE=...@sha256:...`，两个 Alembic revision、owner-counts 和 `/api/ready` 均复核通过。

## 质量与已知限制

- [ ] 离线测试、Ruff、Compose 配置校验、CSS 构建一致性和 MySQL 集成测试均通过。
- [ ] 已记录模型质量未过门槛：`answer_relevance=0.8635 < 0.9`、`answer_accuracy=0.7235 < 0.9`，本次不标记模型质量验收通过。
- [ ] 已向试点用户说明单进程内存限流；多副本上线前必须迁移到 Redis 等共享限流器。
- [ ] 已说明同步 Agent 执行中无法协作取消，禁用仅在开始和写历史前复核。
- [ ] 已说明无 SSO、密码找回、自注册；工具细粒度权限治理不在本次范围。
- [ ] 如启用 Langfuse，已审批脱敏、出口网络、保留策略和降级方案；Langfuse 不替换本地业务 Trace。

## 验收结论与签字

| 角色 | 姓名/签字 | 日期 | 结论或保留意见 |
|---|---|---|---|
| 产品/业务负责人 |  |  |  |
| 安全负责人 |  |  |  |
| 运维负责人 |  |  |  |
| 技术负责人 |  |  |  |
