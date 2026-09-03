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
  |-- Chroma chroma_db：已发布知识索引
  |-- Chroma memory_chroma_db：按 auth:<id> 隔离的用户长期记忆
  `-- 工具/模型提供商：受网络和凭据边界约束
```

浏览器是不可信输入端。FastAPI 从 `metro_session` Cookie 解析当前用户，忽略并拒绝旧式 `user_id` 输入；普通用户只能访问自己的会话，管理员才能访问全局监控和账号管理。浏览器请求存在 `Origin` 时必须与请求基准地址规范化后同源；缺失 `Origin` 仅为受控内部 CLI 保留。应用不直接信任 `X-Forwarded-For`，转发头清理和受信代理范围属于反向代理边界。

## 身份与数据归属

- MySQL `metro_auth` 是身份真源，保存用户、Argon2id 密码哈希、会话令牌摘要和身份审计。原始密码与原始 token 不落库。
- PostgreSQL `metro_agent` 保存业务历史和本地可追责 Trace。会话 `owner_id` 与 Trace `user_id` 使用稳定主题 `auth:<id>`。
- LangGraph checkpoint 使用 `auth:<id>:<thread_id>` 命名空间，避免不同用户提交相同 thread ID 时共享短期上下文。
- Redis 不是权限判断依据；即使 checkpoint 存在，API 仍会在 Agent 开始前和写历史前复核 MySQL 会话。
- `chroma_db` 保存共享的已发布知识索引，不承载用户身份映射；`memory_chroma_db` 保存用户长期记忆，metadata `user_id` 必须是 `auth:<id>`，检索以该字段隔离。
- 旧自由字符串或裸数字 owner 不会自动与 MySQL ID 绑定，必须按 README 的同一份审批映射同步迁移 PostgreSQL 和 `memory_chroma_db`。迁移前同时备份两类 Chroma；记录影响数量和验证证据，长期记忆回滚使用快照或原 metadata，绝不改写 `chroma_db` 身份字段。

## 运行组件

- `metro_agent.api`：HTTP、SSE、认证依赖和管理员接口。
- `metro_agent.graph`：LangGraph 工作流与 Agent 路由。
- `metro_agent.tools`：RAG 与外部服务适配器；工具权限治理仍是独立交付项。
- `metro_agent.observability`：本地 Trace、工具调用、时延、Token 和评测引用。
- `metro_agent.storage`、`metro_agent.memory`：PostgreSQL 历史、Redis checkpoint、`chroma_db` 知识索引与 `memory_chroma_db` 长期记忆适配器。
- `db-migrate`、`auth-migrate`：分别等待 PostgreSQL、MySQL healthy 后执行 Alembic；应用只在两个迁移成功后启动。

Compose 网络不发布 MySQL、PostgreSQL 或 Redis 端口。应用仅发布到宿主机回环地址；WireMock 仅在 `mock` profile 下启动，认证链路不依赖公网资源。`app_backend`、`knowledge_frontend` 与 `knowledge_backend` 都是内部网络；应用经 `knowledge_frontend` 只访问只读知识代理，绝不加入 `knowledge_backend`。唯一非内部网络 `controlled_egress` 只连接 `app`，仅用于经部署侧出口策略批准的模型和外部服务；Chroma、知识代理、indexer 与数据服务均不加入它。

短期会话检查点使用 LangGraph Redis checkpointer，依赖 Redis Search 的 `FT.*` 命令。Compose 固定使用内置 Search 能力的 Redis 8，并在健康检查中验证 `FT.INFO` 可用；普通 Redis 7 镜像不能替代。

## 可观测性与 Langfuse 边界

PostgreSQL 本地业务 Trace 是会话、工具、RAG artifact 与验收记录的权威关联，不由 Langfuse 替换。未来接入 Langfuse 时，只将其作为外部 LLMOps 平面，用于 Prompt 版本、数据集、自动/人工评测和跨版本比较；必须先完成敏感字段脱敏、租户/项目隔离、数据保留期限、出口网络审批和不可用时降级。

## 已知运行边界

- 登录限流为单进程内存状态，多副本必须改成共享原子存储。
- 同步 Agent 不能在模型或工具执行中途协作取消；禁用即时生效仅覆盖下一次鉴权和历史写入前复核。
- 当前没有 SSO、自注册和密码找回。
- 知识库权限、工具最小权限和写操作审批不属于本次认证边界。
