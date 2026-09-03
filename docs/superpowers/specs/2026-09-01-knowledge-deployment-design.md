# 知识库部署闭环设计

**日期：** 2026-09-01
**范围：** 单组织、15 人以内、Docker Compose 私有部署的 P0 第一子项目。

## 目标与边界

将受控 Markdown 知识源、知识索引和 chunk artifact 变成可持久化、可追溯、可回滚的部署资源。新服务器必须能完成“导入受控知识源 → 构建 → 发布 → 查询 → 重启 → 回滚”。每次知识回答必须返回已发布的 `index_build_id`、来源文档和检索 chunk。

本项目不实现审核人、`draft`/`review`/`retired` 审批流、用户反馈页面、多组织与 SSO。这些属于后续“知识发布治理”子项目。本项目的“发布”仅指宿主机管理员将技术校验通过的构建设为唯一可查询版本。

Docker 宿主机及 Docker daemon 管理员是受信任的部署管理员，可绕过容器网络和卷边界；本项目防护的是普通用户与 Web 应用路径，后者绝不能修改、读取未发布或读取历史知识版本。

## 部署架构

```text
宿主机管理员
  -> admin wrapper (.ps1/.sh)
  -> docker compose run knowledge-indexer
       -> Redis publication lock
       -> Chroma backend
       -> source / artifact volumes

app -> knowledge-read-proxy -> Chroma backend
           (only published reads)       (no host port)
```

使用精确固定的 `chromadb==1.5.9` 客户端和 `chromadb/chroma:1.5.9` server 镜像；AMD64 部署与 CI 固定经验证的 manifest digest，其他平台固定其对应 digest，严禁使用 `latest`。任何版本升级都必须重跑持续查询、发布、重启、回滚与代理拒绝写入演练。

生产运行时完全禁止 `PersistentClient`。只有 `chroma` 服务挂载知识索引卷；`app` 与 `knowledge-indexer` 均使用 HTTP client。`chroma` 没有宿主机端口，仅处于内部 backend 网络。

`app` 只加入 read-proxy 网络，既不能解析也不能连接 Chroma。`knowledge-indexer` 只连接 Chroma backend 网络且不暴露端口。`knowledge-read-proxy` 连接 Chroma backend 网络，并以只读方式挂载 artifact 卷；它在转发前验证 registry 指针与当前 validated artifact 摘要。代理是最小 Python ASGI 服务，固定上游 Chroma 地址并显式白名单 Chroma 1.5.9 所需的 identity、tenant、database、registry/collection 读取、count、`get` 与 `query` 路由；它只允许 registry collection 与 registry 当前指针所指 collection。所有 create、add、update、upsert、delete、fork、reset、`search`、PUT 与 DELETE 路由返回 `403`。代理路由白名单与版本锁同步测试。

## 配置与持久化资源

| 配置 | 容器内路径或地址 | 使用方 |
| --- | --- | --- |
| `KNOWLEDGE_PATH` | indexer 的 `/knowledge/source` | indexer 只读 |
| `CHROMA_HOST`/`CHROMA_PORT` | 内部 `chroma:8000` | proxy、indexer |
| `CHROMA_DB_DIR` | chroma 的 `/chroma/chroma` | 仅 chroma 服务读写 |
| `KNOWLEDGE_ARTIFACT_ROOT` | `/var/lib/metro-agent/knowledge-artifacts` | indexer 读写；read-proxy、操作/备份服务只读 |

Compose 新增 `knowledge_chroma_data` 和 `knowledge_artifact_data` 命名卷。知识源是 `.env` 指向的宿主机目录，只读挂载进 indexer，绝不复制进镜像或 Git。应用不挂载知识源、Chroma 卷或 artifact 卷。

`knowledge-indexer` 是不自动启动、无端口的一次性 Compose 服务，固定入口为 `python -m metro_agent.tools.knowledge_indexer`。其子命令只能是 `build-and-publish`、`status`、`rollback`、`verify`。POSIX 包装器验证有效 root，并以 root 所有、目录 `0700`/文件 `0600` 写入固定 `/var/log/metro-agent/knowledge-admin-audit.log`；PowerShell 包装器验证 Windows Administrator，并以仅 Administrators 与 SYSTEM 可写的 ACL 写入固定 ProgramData 下的 `MetroAgent\knowledge-admin\audit.log`。两者记录 UTC 时间、已验证身份、子命令和无敏感值的参数状态，绝不把身份作为 CLI 参数传进容器。所有路径和上游地址均来自受控环境，命令不接受任意路径、URL 或操作者身份参数。

Windows 的包装器不会在运行时自动创建审计目录或日志文件：通用 .NET 路径创建无法原子保证整条 ProgramData 祖先链不经过 reparse point。部署安装程序必须先以管理员权限创建固定目录和日志、设置仅 Administrators/SYSTEM ACL；任一组件缺失、祖先或目标为 reparse point，或 ACL 不符合要求时包装器拒绝执行，且不启动 indexer。

## 知识源与预检

仅递归索引真实、位于 `KNOWLEDGE_PATH` 内的 `.md` 文件；符号链接、junction 和其他 reparse point 一律拒绝。每份文档必须使用安全 YAML front matter，且为映射并包含：

- `owner`、`source`：非空字符串；
- `updated`、`effective_date`、`expires_at`：ISO `YYYY-MM-DD`；
- `risk_level`：`general`、`controlled` 或 `high`。

有效期按部署的 `Asia/Shanghai` 日历日解释，文档在 `expires_at` 当日结束后失效；到期文档会拒绝整次发布。单文件最大 10 MiB，每次构建最多 10,000 份文档。任一解析或资源错误均拒绝整次发布、输出精确相对路径而不输出正文。

知识根目录必须有 `release-smoke-queries.jsonl`，其自身不入索引。每行包含 `query`、`expected_source` 和 `minimum_matches`；`expected_source` 是从知识根目录开始的精确 POSIX 相对路径。预检要求前 `minimum_matches` 项中至少一次命中该来源，且总结果数不低于 `minimum_matches`。

indexer 在构建开始和完成时计算 `source_tree_sha256`：按相对路径排序，对被索引的 Markdown 和 smoke-query 文件的内容摘要进行确定性哈希。两次不同即失败，确保不发布构建期间变化的混合知识源。若源目录是干净 Git 工作区，额外记录 commit ID。

## 构建、发布、回滚与审计

每个构建都有唯一 `index_build_id`、唯一 `__build_<id>` Chroma collection 和不可变 artifact。artifact 在发布前一次性写成 `validated`，记录 source hash、可选 Git commit、镜像/代码版本、Python、Chroma client/server、嵌入与重排模型标识、chunker 配置、collection 名称与校验摘要。

若 `source_tree_sha256` 和完整 provenance 与当前发布版本相同，默认成功 no-op，输出当前 build ID，不新增 collection 或操作事件。`--force-rebuild` 仅接受 `indexer-upgrade`、`embedding-model-change`、`reranker-model-change`、`chunker-change`、`recovery` 原因；理由缺失或不在枚举中即拒绝。

构建、发布、回滚和一致性备份先取得 Redis 锁 `knowledge:publication`。锁使用随机 token 与心跳续租，最长 15 分钟；无法取得/续租锁或 Redis 不可用时，以非零退出且不改当前状态。

Chroma registry 的单条 `published` 记录是唯一可见性开关，包含当前 collection、前一 collection 和当前 validated artifact 摘要。indexer 在写入该记录前验证非空 collection、artifact 摘要、source 元数据和全部 smoke 查询。单次 upsert 同时写入新当前与旧当前版本；read-proxy 只转发与 registry 摘要精确匹配的 validated artifact 对应版本。没有跨存储事务也不会产生可查询的中间版本。

回滚仅允许经过验证的前一 collection。它以单条 registry upsert 交换当前与前一版本，并保留所有 collection 和 artifact。首次发布没有前一版本时回滚失败，绝不清空当前版本。构建永久保留，不自动清理。

每次操作以 UUID 记录在 `KNOWLEDGE_ARTIFACT_ROOT/operations/`：开始时原子创建 `<id>.started.json`，结束时创建不可覆盖的 `<id>.succeeded.json` 或 `<id>.failed.json`。indexer 在内部从可信 OS 身份生成 `operator_identity`：Unix 使用 effective UID 对应的 `pwd` 记录，Windows 使用系统身份 API；CLI 和环境变量均不能覆盖它。事件还记录主机、UTC 时间、操作、当前/前一 build ID、source revision、强制原因和错误类别。容器事件记录的是容器内真实有效身份（通常为 root），而宿主包装器日志记录已验证的宿主管理员；这是避免将可伪造用户名跨边界传递的安全取舍。只有开始记录代表中断并需人工核验。

## 查询、保护与降级

read-proxy 对 Chroma 的连接超时为 2 秒、上游响应超时为 10 秒、请求体最大 1 MiB。它记录请求 ID、路由类别、允许/拒绝、状态码、耗时和上游错误类别，不记录查询正文、向量、chunk 文本、Cookie、认证头或完整 URL 参数。

代理不可达、拒绝请求或响应无效时，知识问答不允许直连 Chroma；返回“已发布知识库暂不可用，请稍后重试”的受控结果并记录原因。应用仍可提供登录、历史和非知识能力，`/api/ready` 必须报告知识服务未就绪。每次知识查询先经 proxy 读取 published pointer；正在执行的请求可以完成旧版本查询，但单次请求不得混用版本。

## 备份、恢复与验收

扩展 `backup-all.sh` 与 `restore-all.sh`，将整个 `knowledge_chroma_data` 与 `knowledge_artifact_data` 作为不可拆分的知识发布单元。备份获得 publication lock、停止 app、确认 indexer 未运行并停止 Chroma 后归档两个根目录；恢复先验证摘要，再同时恢复两者，验证 registry 与 validated artifact 摘要匹配后，才启动 Chroma、read-proxy 与 app。任一失败均不启动 app。

备份目录或目标由组织管理的静态加密存储保护，权限限于部署管理员；脚本只负责完整性、一致性和恢复验证，不保存加密密钥。首次上线与每次 Chroma 升级前都必须完成隔离恢复演练并记录加密控制和结果。

测试先于实现，至少覆盖：

1. 环境配置、固定 client/server 版本、网络与卷隔离；
2. metadata、路径逃逸、有效期、大小/数量、source-tree 变化、重复与强制构建；
3. 预检失败不改变 published 指针，发布/回滚的 registry-artifact 一致性；
4. Redis 锁的并发拒绝、续租失败和操作事件终态；
5. read-proxy 对所有读取白名单路径的成功，以及每个写路径的拒绝且无状态变化；
6. 代理超时、脱敏日志和知识不可用时的 ready/degrade 行为；
7. 备份/恢复同时还原 registry 与 artifact，并恢复到可查询版本；
8. `knowledge-e2e` Compose profile 使用临时知识源与确定性测试嵌入器，覆盖构建、发布、持续查询、重启与回滚，不依赖真实模型密钥。

确定性嵌入器仅可在 `KNOWLEDGE_E2E=1` 启用；默认 Compose 与生产 indexer 拒绝该变量，并有测试覆盖。实际部署验收使用批准模型目录的只读挂载，并记录一次完整演练。
