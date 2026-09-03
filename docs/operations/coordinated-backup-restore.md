# MySQL / PostgreSQL / Chroma / Redis 协调恢复手册

适用于单组织内部试点。所有命令从仓库根目录执行，备份目录使用绝对路径并由部署账号独占。恢复必须先在隔离环境演练。

## 一致性备份

将 `METRO_AGENT_IMAGE` 设置为已验收镜像的**完整 digest**，不能使用浮动 tag：

```sh
export METRO_AGENT_IMAGE=registry.example/metro-agent@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
deploy/operations/backup-all.sh /srv/metro-backups/pilot-20260826
```

脚本在检查 `knowledge-indexer`、停止 `app` / `knowledge-read-proxy` / `chroma` **之前**取得一个可续租的 `knowledge:publication` token；同一 token 持续覆盖知识卷归档及 `SHA256SUMS` 写入，并在每个知识临界边界重新校验。随后运行 `mysqldump --single-transaction`、`pg_dump`、长期记忆 Chroma 快照、完整 `knowledge_chroma_data` 与完整 `knowledge_artifact_data` 归档、Redis `SAVE`，并记录两个 Alembic revision、完整镜像 digest、owner-counts 与 `SHA256SUMS`。知识归档固定命名为 `knowledge-chroma.tar.gz`、`knowledge-artifacts.tar.gz`，二者缺一不可。脚本拒绝覆盖已有目录；任何命令失败都会停止，应用保持关闭。

release 和 artifact 是不可变审计证据，默认永久保留，**不自动清理**。归档文件的静态加密、密钥保管和保留期限由备份目标负责；脚本不替代备份介质加密。

## 隔离恢复

```sh
deploy/operations/restore-all.sh /srv/metro-backups/pilot-20260826
```

脚本验证全部校验和后，从备份中的 `metro-agent-image.txt` 读取并验证完整的小写 `@sha256:` digest，在执行任何 Compose 命令前导出为 `METRO_AGENT_IMAGE`，确保恢复全过程使用备份时记录的应用镜像。随后按固定顺序 **MySQL -> PostgreSQL -> Chroma -> Redis** 恢复。所有 tar 成员会在解压前校验：仅允许一个预期根目录下的普通文件和目录，拒绝绝对路径、`..`、链接和设备节点。长期记忆 Chroma 在卷内解压到临时目录，再执行原子目录切换；旧 `current` 保留为 `rollback.previous`。知识 Chroma 则在完整暂存、验证后替换**整卷内容**（不产生额外嵌套目录），并将恢复前卷内容保留为 `rollback.previous`；artifact 不执行清空操作，避免自动清理历史 release 证据。恢复 Chroma 和 Redis 后，脚本在启动 app 前运行 `knowledge-indexer verify`，验证 registry、已发布 pointer、validated descriptor 摘要和当前 collection；失败时 app 保持停止。验证成功后会先启动并探测 `knowledge-read-proxy` 心跳，最后才启动 app 并等待 `/api/ready`。任一 revision、owner-count、知识验证或 readiness 不一致都会非零退出；不要删除 `rollback.previous`，保持写流量关闭并查明差异。

首次部署、Chroma 升级和灾难恢复必须在隔离环境完成 `docker compose --profile knowledge-e2e` 的确定性构建、发布、查询、重启与回滚演练。该 profile 使用版本化 synthetic fixture，不使用任何真实模型密钥。

恢复验收还必须抽查 MySQL admin/user 数量与状态、PostgreSQL `owner_id` 和 Trace `user_id`、长期记忆 metadata、跨用户不可见、知识检索和 Redis checkpoint。脚本比对的是精确计数，业务语义抽查由验收人记录。

## 镜像回滚

镜像回滚只接受完整 `METRO_AGENT_IMAGE` digest：

```sh
export METRO_AGENT_IMAGE=registry.example/metro-agent@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
deploy/operations/rollback-image.sh
```

脚本拒绝 tag 和短 digest，拉取同一 digest 的 `app`、`db-migrate`、`auth-migrate`，使用 `--no-build` 启动并检查 `/api/ready`。镜像回滚不会自动 downgrade 数据库；有损 downgrade 需单独审批，默认采用兼容旧镜像的前滚修复或从同一协调备份完整恢复。
