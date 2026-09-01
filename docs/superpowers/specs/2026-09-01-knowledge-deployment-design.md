# 知识库部署闭环设计

**日期：** 2026-09-01
**范围：** 单组织、15 人以内、Docker Compose 私有部署的 P0 第一子项目。

## 目标与边界

将受控 Markdown 知识源、Chroma 知识索引和 chunk artifact 变成可持久化、可追溯、可回滚的部署资源。新服务器必须能完成“导入受控知识源 → 构建 → 发布 → 查询 → 重启 → 回滚”。每次回答继续返回已发布的 `index_build_id`、来源文档和检索 chunk。

本项目不实现资料审核人、正式的 `draft`/`review`/`retired` 审批流、用户反馈界面或多组织权限；这些属于随后独立的“知识发布治理”子项目。这里的“发布”仅指管理员将通过技术校验的构建设为当前可查询版本。

## 方案选择

采用“不可变构建 + Chroma 发布指针”而不是覆盖索引目录：

- 每次构建生成唯一 `index_build_id`、唯一 Chroma collection（现有 `__build_<id>` 命名）和不可变 artifact 目录 `chunks/<id>/`。
- Chroma registry 中的 `published` 记录是唯一的当前版本权威。应用仅解析该记录及匹配的 `published` manifest，不扫描或查询未发布 collection。
- registry 的单个 `published` 记录同时保存当前 `collection_name` 和 `previous_collection_name`。成功切换时先验证新 collection 和 manifest，再以一次 upsert 同时写入新当前版本和旧当前版本；回滚也以一次 upsert 交换二者。这样发布指针不会出现两个独立 registry 记录之间的中间状态。回滚不会删除 collection 或 artifact。
- 失败、空索引、artifact 缺失、抽样检索失败或发布后指针校验失败时，旧 `published` 不变；现有 `publish_uncertain` 处理继续保留为人工受控恢复路径。

不使用文件系统 `current` 软链接作为发布开关：在 Windows/Compose 环境中它不如 Chroma registry 可移植，且现有查询代码已经以 registry 为一致性边界。

## 部署资源与配置

新增并统一使用以下配置：

| 配置 | 宿主机含义 | 容器内路径 | 访问方 |
| --- | --- | --- | --- |
| `KNOWLEDGE_PATH` | 受控 Markdown 知识源目录 | `/knowledge/source` | 仅 `knowledge-indexer`，只读 |
| `CHROMA_DB_DIR` | Chroma 知识索引持久化卷 | `/var/lib/metro-agent/knowledge-chroma` | 应用只读；indexer 读写 |
| `KNOWLEDGE_ARTIFACT_ROOT` | chunk manifest、JSONL、预览和发布记录卷 | `/var/lib/metro-agent/knowledge-artifacts` | 应用只读；indexer 读写 |

Compose 新增两个命名卷：`knowledge_chroma_data` 与 `knowledge_artifact_data`。受控知识源使用 `.env` 所指向的宿主机目录绑定挂载，绝不复制进镜像或 Git。应用不挂载原始知识源；它只读挂载索引和 artifact 卷，因此运行中的 Web 进程没有构建、发布或删除生产知识数据的能力。

`knowledge-indexer` 是一次性 Compose 服务，不暴露端口、不自动随 `app` 启动。管理员通过受控命令显式运行它；它与应用使用同一镜像，挂载知识源为只读、索引与 artifact 为读写。服务执行完成后退出，任何非零退出码都表示没有完成发布。

## 构建、发布与回滚流程

```text
管理员运行 knowledge-indexer
  -> 读取 /knowledge/source
  -> 生成 collection + chunks/<build-id>/ artifact（staged）
  -> 校验元数据、非空索引、manifest 哈希和抽样检索
  -> 一次 upsert 写入新 published 与 previous_published
  -> 将 manifest 标记为 published
  -> 应用的下一次查询解析新 published 指针
```

发布前检查必须包括：知识源至少含一份可解析 Markdown；所有文档具备既有检索所需的来源标识；collection 节点数大于零；manifest 的 build ID 与 collection 绑定正确；指定的抽样查询至少返回一个带 `chunk_id` 和来源文件的结果。任一检查失败时，indexer 将新构建标记为失败并以非零退出，且不会切换 `published`。

发布后，indexer 重新读取 registry、collection 和 manifest 进行一致性确认。若确认失败，使用现有补偿逻辑恢复旧指针；若无法证明最终指向，保留 `publish_uncertain` 记录并拒绝应用读取该新版本。

回滚命令只能选择 `previous_collection_name` 指向的版本，并在切换前验证 collection 非空、manifest 状态为 `published`、manifest build ID 与 collection 匹配。回滚成功后，原当前版本成为新的前一版本，从而允许一次受控前滚。首次发布不存在前一版本时，回滚命令明确失败，不清空可查询索引。

## 应用查询行为

`Knowledge_RAGtools` 继续通过 `resolve_published_collection_name` 读取 registry 并验证对应 manifest；其缓存键为 collection 名称，因此发布或回滚后的下一次查询会创建匹配版本的查询引擎。读取失败、指针与 manifest 不一致、collection 为空或 artifact 缺失时，返回既有受控“索引不可用”错误，不会退回到未发布 collection。

RAG 结果必须包含：`index_build_id`、每个结果的 `chunk_id`、`file_name` 和完整 metadata。运行演练脚本应将一次查询响应与发布 build ID 保存为验收证据，但不记录用户会话或模型密钥。

## 管理命令与操作证据

提供独立的管理员命令入口，至少支持：

- `build-and-publish`：构建、预检、发布并输出 build ID、collection、文档数和 chunk 数；
- `status`：输出当前和前一已发布版本及它们的 manifest 路径；
- `rollback`：恢复前一已发布版本并输出新当前版本；
- `verify`：验证当前版本可由应用查询、重启后仍可查询，并打印检索来源与 chunk ID。

命令只接受固定选项，不接受任意宿主机路径；全部路径来自 Compose 环境变量。操作说明将列出全新部署、构建、发布、重启、回滚和备份前检查的逐步命令与预期输出。

## 测试与验收

测试先于实现，并覆盖下列契约：

1. 配置默认值和环境覆盖，以及 `.env.example` 的必填知识源变量；
2. Compose 中 indexer 不公开端口、不自动启动，且应用对知识索引和 artifact 使用只读挂载；
3. 构建成功会产生唯一 collection、`published` manifest 和可验证发布/前一版本指针；
4. 元数据、空 collection、artifact 或抽样检索失败不会改变旧 `published`；
5. 回滚只允许经过验证的上一版本，并使应用查询报告被恢复的 build ID；
6. 重新初始化应用查询组件后仍从持久化卷读取同一已发布版本；
7. 一个 Compose 集成演练以临时受控知识源完成构建、查询、重启、回滚，且不会依赖真实模型密钥。

完成条件是离线单元/集成测试和 Ruff 均通过，并在受控 Compose 环境记录一次完整“构建 → 发布 → 查询 → 重启 → 回滚”演练。现有数据库、Redis、内存 Chroma 和认证迁移服务不在本项目中删除或改为公网暴露。
