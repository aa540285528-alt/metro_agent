# 架构概览

Metro Agent 是一个 FastAPI 应用，通过 LangGraph 将请求路由到知识检索、实时查询、诊断和通用协助 Agent。当前架构面向单组织私有化内部试点，认证数据与业务数据分库，调用方身份只由服务端会话决定。

## 信任边界与请求流

```text
内部浏览器
  | HTTPS + HttpOnly/Secure Cookie
  v
反向代理（TLS、来源与访问控制）
  | 仅转发到 127.0.0.1:8000
  v
FastAPI
  |-- MySQL：验证账号、会话摘要、角色与审计
  |-- PostgreSQL：会话历史、Trace、工具调用、评测元数据
  |-- Redis：短期 checkpoint，键空间包含 auth:<id>
  |-- knowledge-read-proxy -> 远程 Chroma：受发布 registry 与 artifact 校验约束的已发布知识索引
  |-- Chroma memory_chroma_db：按 auth:<id> 隔离的用户长期记忆
  `-- 工具/模型提供商：受网络和凭据边界约束
```

浏览器是不可信输入端。FastAPI 从 `metro_session` Cookie 解析当前用户，忽略并拒绝旧式 `user_id` 输入；普通用户只能访问自己的会话，管理员才能访问全局监控和账号管理。浏览器请求存在 `Origin` 时必须与请求基准地址规范化后同源；缺失 `Origin` 仅为受控内部 CLI 保留。应用不直接信任 `X-Forwarded-For`，转发头清理和受信代理范围属于反向代理边界。

## 身份与数据归属

- MySQL `metro_auth` 是身份真源，保存用户、Argon2id 密码哈希、会话令牌摘要和身份审计。原始密码与原始 token 不落库。
- PostgreSQL `metro_agent` 保存业务历史和本地可追责 Trace。会话 `owner_id` 与 Trace `user_id` 使用稳定主题 `auth:<id>`。
- LangGraph checkpoint 使用 `auth:<id>:<thread_id>` 命名空间，避免不同用户提交相同 thread ID 时共享短期上下文。
- Redis 不是权限判断依据；即使 checkpoint 存在，API 仍会在 Agent 开始前和写历史前复核 MySQL 会话。
- 共享知识索引位于远程 Chroma，由只读 `knowledge-read-proxy` 在每次转发前校验已发布 registry 指针与不可变 artifact；应用既不挂载也不读取本地知识 `chroma_db`，并且不能查询历史或草稿 collection。`memory_chroma_db` 保存用户长期记忆，metadata `user_id` 必须是 `auth:<id>`，检索以该字段隔离。
- 旧自由字符串或裸数字 owner 不会自动与 MySQL ID 绑定，必须按 README 的同一份审批映射同步迁移 PostgreSQL 和 `memory_chroma_db`。迁移前备份 PostgreSQL、受发布治理的远程知识索引与长期记忆；记录影响数量和验证证据，长期记忆回滚使用快照或原 metadata，绝不改写已发布知识 collection 的身份字段。

## 运行组件

- `metro_agent.api`：HTTP、SSE、认证依赖和管理员接口。
- `metro_agent.graph`：LangGraph 工作流与 Agent 路由。
- `metro_agent.tools`：RAG 与外部服务适配器；工具权限治理仍是独立交付项。
- `metro_agent.observability`：本地 Trace、工具调用、时延、Token 和评测引用。
- `metro_agent.storage`、`metro_agent.memory`：PostgreSQL 历史、Redis checkpoint、经只读代理访问的远程已发布知识索引与 `memory_chroma_db` 长期记忆适配器。
- `db-migrate`、`auth-migrate`：分别等待 PostgreSQL、MySQL healthy 后执行 Alembic；应用只在两个迁移成功后启动。

Compose 网络不发布 MySQL、PostgreSQL 或 Redis 端口。应用仅发布到宿主机回环地址；WireMock 仅在 `mock` profile 下启动，认证链路不依赖公网资源。`app_backend`、`knowledge_frontend`、`knowledge_backend` 与 `app_egress` 都是内部网络；应用经 `knowledge_frontend` 只访问只读知识代理，绝不加入 `knowledge_backend`。应用没有公网路由：它只能在 `app_egress` 访问 `egress-gateway`，并由受只读挂载的版本化 `deploy/egress/allowed-domains.txt` 驱动 Squid 的精确域名允许列表；只有该网关连接非内部 `controlled_egress`，应用使用固定的 HTTP(S) 代理环境变量。变更出口域名必须同时评审允许列表与部署配置。Chroma、知识代理、indexer 与数据服务均不加入出口网络。

短期会话检查点使用 LangGraph Redis checkpointer，依赖 Redis Search 的 `FT.*` 命令。Compose 固定使用内置 Search 能力的 Redis 8，并在健康检查中验证 `FT.INFO` 可用；普通 Redis 7 镜像不能替代。

## 受治理知识发布

知识管理与线上查询是两条不同的路径：

```text
管理员浏览器
  -> FastAPI 管理 API（require_admin；只创建固定类型任务）
  -> 持久化草稿/任务/审计元数据
  -> 单实例 knowledge publication worker
       -> staging（原 ZIP、受控解包源、校验报告）
       -> Redis knowledge:publication 锁
       -> Chroma backend（构建、发布、回滚） + artifact 卷

普通/管理员查询
  -> FastAPI
  -> knowledge-read-proxy（artifact 只读，校验 current descriptor）
  -> Chroma backend 的当前 collection
```

网页是管理员日常上传、校验、确认发布、查看历史和回滚的唯一入口。其 API 只接受服务端校验的 draft/release ID 与固定任务类型：`validate_draft`、`publish_draft`、`rollback_release`、`get_status`；不得传入命令、路径、操作者、collection 名或 Docker 参数。Web 应用、普通查询路径和 read-proxy 都不能读取 staging、写 artifact、直接连接 Chroma backend、访问 Docker socket 或启动 shell。worker 以最小权限独占 staging 读写、artifact 写入、backend 网络和 Redis 发布锁；它不是通用命令执行器。

上传只接收单个 `.zip` 包：根目录仅允许 Markdown 与 `release-smoke-queries.jsonl`。服务端在解包前后实施以下固定上限：压缩包最多 100 MiB，解包后合计最多 500 MiB，至多 10,000 个文件，每个 Markdown 最多 10 MiB。绝对或逃逸路径、符号链接/reparse point、重复或非法名称、嵌套归档、可执行或未声明文件一律失败关闭。解包资料还必须通过受控知识源的 metadata、日期、大小和 smoke-query 校验；上传完成不等于校验通过，更不等于发布。

发布 worker 仅在草稿处于 `ready_to_publish` 时重新执行关键校验、构建新的不可变 release 与 artifact，并在 Redis 锁内原子更新 current/previous registry 指针。任一步失败、锁冲突或 worker 重启均不修改 current release；进行中的读取可完成旧版本，单个读取请求不能混用版本。回滚由管理员针对已有历史 release 提交原因，同样经过 worker 与发布锁，不会重建或删除历史版本。

原始 ZIP、解包清单、哈希、校验报告、release、artifact、任务与审计记录永久保留，不设置自动清理。审计字段至少包括管理员身份、动作、draft/release ID、原始文件名、SHA-256、时间、结果、失败摘要和回滚原因。仅部署和事故处置可使用固定命令白名单的 `knowledge-admin.sh`/`knowledge-admin.ps1` 包装器；它们不能被 Web 请求触发，也不是日常发布入口。

## 可观测性与 Langfuse 边界

PostgreSQL 本地业务 Trace 是会话、工具、RAG artifact 与验收记录的权威关联，不由 Langfuse 替换。未来接入 Langfuse 时，只将其作为外部 LLMOps 平面，用于 Prompt 版本、数据集、自动/人工评测和跨版本比较；必须先完成敏感字段脱敏、租户/项目隔离、数据保留期限、出口网络审批和不可用时降级。

## 已知运行边界

- 登录限流为单进程内存状态，多副本必须改成共享原子存储。
- 同步 Agent 不能在模型或工具执行中途协作取消；禁用即时生效仅覆盖下一次鉴权和历史写入前复核。
- 当前没有 SSO、自注册和密码找回。
- 知识发布已实行管理员网页入口、最小权限 worker 与只读查询隔离；细粒度文档授权和多级内容审批仍不在当前试点范围。
