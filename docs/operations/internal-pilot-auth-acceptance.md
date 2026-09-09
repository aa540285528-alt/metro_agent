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
| 知识发布元数据 schema/revision |  |
| 部署人/复核人 |  |
| 证据归档位置 |  |

## 部署与秘密

- [ ] 全新环境使用唯一 `.env` 执行 `docker compose --env-file <部署 .env> --project-name metro-agent-pilot --profile knowledge-admin up --build -d` 成功；数据库无宿主机端口，应用仅监听 `127.0.0.1:APP_HOST_PORT`。
- [ ] `.env` 的 `MODEL_DIR` 指向包含 `bge-m3/`、`bge-reranker/` 的宿主机目录，应用容器仅以只读方式挂载 `/models`。
- [ ] Redis 健康检查通过，且 `redis-cli COMMAND INFO FT.INFO` 返回命令信息；不得使用缺少 Search 能力的基础 Redis 7。
- [ ] `db-migrate`、`auth-migrate` 均退出码 `0`，已记录两个 Alembic revision。
- [ ] `/api/health` liveness 返回 `200`；首次发布前 `/api/ready` 返回 `503`，发布且重启后才返回 `200`，且 readiness 实际覆盖 PostgreSQL、MySQL、Redis 和已发布知识。
- [ ] CI 实际完成 Docker image build，并记录应用镜像 digest。
- [ ] `POSTGRES_PASSWORD`、`AUTH_MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD`、至少 32 字节 pepper 均独立生成且未使用样例值。
- [ ] gitleaks/秘密扫描通过；日志抽查不含密码、原始 Cookie、token 或工具敏感参数。
- [ ] HTTPS 反向代理可用，`AUTH_COOKIE_SECURE=true`；浏览器验证 Secure Cookie、`HttpOnly`、`SameSite=Lax`。
- [ ] 知识 worker 为单实例；仅其拥有 staging 读写、artifact 写、Chroma backend 网络和 Redis `knowledge:publication` 锁所需权限。Web 应用、read-proxy 和普通查询路径均无 staging 读取、Chroma 写入、Docker socket 或 shell 权限。
- [ ] 应用查询仅经 `knowledge-read-proxy` 到当前已发布 collection；代理以只读方式读取 artifact，拒绝 Chroma 写路由、草稿、失败版本和历史 collection。

## 知识管理专项验收

每项均须填写命令或网页操作、预期结果、实际观察值和证据位置；不得记录密码或 token。

| 项目 | 命令或网页操作 | 预期结果 | 实际观察值 | 证据位置 |
|---|---|---|---|---|
| 管理员登录 | HTTPS 页面以管理员账号登录 | 进入知识管理入口 |  |  |
| 上传脱敏真实 ZIP | 管理员上传真实但已脱敏的知识包 | 创建 draft，记录包 SHA-256 |  |  |
| 校验 | 等待或刷新 draft 状态 | 校验成功，可发布 |  |  |
| 发布 | 管理员确认发布 | 生成当前版本 ID |  |  |
| 带来源查询 | 对已发布来源提出可追溯问题 | 返回答案及来源，`/api/ready=200` |  |  |
| 非管理员隔离 | 普通用户请求知识管理 API | 返回 `403` |  |  |
| 发布失败保持版本 | 记录当前版本后触发受控失败发布 | 当前版本 ID 不变 |  |  |
| 回滚 | 回滚至旧版本并再次查询 | 查询命中旧版本 |  |  |
| 重启后知识就绪 | 重启 app 后请求 `/api/health` 与 `/api/ready` | 两者返回 `200` |  |  |
| 审计字段 | 填写版本 ID、包 SHA-256、操作者、镜像 digest | 字段完整且无秘密 |  |  |

## 身份、权限与隔离

- [ ] `bootstrap_admin` 交互执行成功；记录执行人、时间和审计事件，不记录密码。
- [ ] 无 Cookie 访问受保护 API 返回 `401`。
- [ ] 普通用户访问管理员 API 返回 `403`。
- [ ] 用户 A 请求用户 B 的 thread 返回跨用户 404，且没有启动模型/工具、没有写入历史。
- [ ] 禁用用户后旧会话失效；改密后旧会话失效；最后一个活动管理员不可禁用或降级。
- [ ] 登录失败达到阈值后返回 `429` 和有效 `Retry-After`，成功登录后预算行为符合设计。
- [ ] 账号创建、角色变更、禁用、改密、登录和登出均能在 MySQL 审计表追踪。
- [ ] 真实 MySQL 并发锁测试通过：两个 `multiprocessing` spawn 进程跨进程并发 bootstrap，最终只有一个首管理员。

## 知识网页发布与审计

- [ ] 只有管理员能看到知识管理页面并调用全部知识管理 API；普通用户对页面、静态操作入口及每个 API 均被拒绝（`403`）。
- [ ] 网页日常流程为“上传草稿 → 校验报告 → 管理员明确发布 → 查看历史/回滚”；上传者本人可在校验通过后发布，但上传本身绝不写 Chroma 或切换 current release。
- [ ] 上传仅接受一个根目录包含 Markdown 与 `release-smoke-queries.jsonl` 的 `.zip`；不接受文件夹、单文件 Markdown 或其他归档格式。
- [ ] ZIP 契约已用正反例验证：压缩包 `<= 100 MiB`、解包总大小 `<= 500 MiB`、文件数 `<= 10,000`、每个 Markdown `<= 10 MiB`；路径穿越/绝对路径、符号链接或 reparse point、重复或非法文件名、嵌套归档、可执行或未声明文件与压缩炸弹均被拒绝。
- [ ] 解包资料通过受控知识源的 front matter、有效期、大小、文件数与 `release-smoke-queries.jsonl` 校验；失败报告可在页面查看，失败草稿和当前发布版本均被保留且 current release 不变。
- [ ] 草稿状态流转已验收：`uploaded`、`validating`、`validation_failed`、`ready_to_publish`、`publishing`、`published`、`publish_failed`；发布和回滚进行中禁止重复提交。
- [ ] 管理 API 只创建服务端校验的 `validate_draft`、`publish_draft`、`rollback_release`、`get_status` 任务；请求不能提交/伪造 operator、文件路径、collection 名、Docker 参数或任意命令。
- [ ] 发布前 worker 在 Redis 发布锁下再次校验；构建或发布失败、锁冲突和 worker 重启都不改变 current release。持续查询可完成旧版本，单个请求不混用版本。
- [ ] 回滚仅允许管理员针对已有历史 release 发起，并记录非空原因；回滚经同一 worker 和 Redis 锁原子切换指针，失败时 current release 不变。
- [ ] 草稿 ZIP、解包清单、SHA-256、校验报告、不可变 release、artifact、任务和管理员审计记录均永久保留，且不存在自动清理任务。
- [ ] 审计记录至少可追溯管理员身份、动作、draft/release ID、原始文件名、SHA-256、时间、结果、失败摘要和回滚原因；管理员身份只取自认证上下文。
- [ ] `knowledge-admin.sh`/`knowledge-admin.ps1` 仅用于部署或事故处置，命令白名单和权限检查有效；确认它们不能由 HTTP/浏览器触发，也不作为日常发布入口。

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
- [ ] 一致性备份恢复演练使用脚本按 MySQL -> PostgreSQL -> Chroma -> Redis 恢复，Chroma 完成原子目录切换并保留回滚目录；完整知识 Chroma/artifact 以 `knowledge:publication` 锁保护并在 app 启动前验证 registry/pointer/descriptor。
- [ ] 首次部署和 Chroma 升级已在隔离环境运行 `knowledge-e2e` 确定性演练（构建、发布、查询、重启、回滚）；演练不使用真实模型密钥，release/artifact 默认永久保留且不自动清理，备份加密由备份目标负责。
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
