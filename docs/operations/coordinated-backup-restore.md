# MySQL / PostgreSQL / Chroma / Redis 协调恢复手册

适用于单组织内部试点。所有命令从仓库根目录执行，备份目录使用绝对路径并由部署账号独占。恢复必须先在隔离环境演练。

## 一致性备份

将 `METRO_AGENT_IMAGE` 设置为已验收镜像的**完整 digest**，不能使用浮动 tag：

```sh
export METRO_AGENT_IMAGE=registry.example/metro-agent@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
deploy/operations/backup-all.sh /srv/metro-backups/pilot-20260826
```

脚本先执行 `docker compose stop app`，随后运行 `mysqldump --single-transaction`、`pg_dump`、两个 Chroma 目录快照和 Redis `SAVE`，并记录两个 Alembic revision、完整镜像 digest、PostgreSQL/Chroma owner-counts 与 `SHA256SUMS`。脚本拒绝覆盖已有目录；任何命令失败都会停止，应用保持关闭。

## 隔离恢复

```sh
deploy/operations/restore-all.sh /srv/metro-backups/pilot-20260826
```

脚本验证全部校验和后，按固定顺序 **MySQL -> PostgreSQL -> Chroma -> Redis** 恢复。Chroma 在卷内解压到临时目录，再执行原子目录切换；旧 `current` 保留为 `rollback.previous`。随后脚本用 `cmp` 强制比对两个 Alembic revision 和恢复前后的 owner-counts，启动应用并等待 `/api/ready`。任一 revision、owner-count 或 readiness 不一致都会非零退出；不要删除 `rollback.previous`，保持写流量关闭并查明差异。

恢复验收还必须抽查 MySQL admin/user 数量与状态、PostgreSQL `owner_id` 和 Trace `user_id`、长期记忆 metadata、跨用户不可见、知识检索和 Redis checkpoint。脚本比对的是精确计数，业务语义抽查由验收人记录。

## 镜像回滚

镜像回滚只接受完整 `METRO_AGENT_IMAGE` digest：

```sh
export METRO_AGENT_IMAGE=registry.example/metro-agent@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
deploy/operations/rollback-image.sh
```

脚本拒绝 tag 和短 digest，拉取同一 digest 的 `app`、`db-migrate`、`auth-migrate`，使用 `--no-build` 启动并检查 `/api/ready`。镜像回滚不会自动 downgrade 数据库；有损 downgrade 需单独审批，默认采用兼容旧镜像的前滚修复或从同一协调备份完整恢复。
