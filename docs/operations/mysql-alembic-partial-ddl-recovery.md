# MySQL / Alembic 部分 DDL 失败处置手册

适用于 `auth-migrate` 在 MySQL 上失败、可能已有部分 DDL 隐式提交的情况。`auth-migrate` 失败会继续阻断 `app`，在 DBA 完成核验前不得绕过该依赖。

## 采集现场

在仓库根目录执行以下只读检查脚本；证据目录必须是绝对路径：

```sh
deploy/operations/inspect-mysql-partial-ddl.sh /srv/metro-evidence/ddl-20260826
```

脚本先执行 `docker compose stop app`，然后保存失败日志、`alembic -c alembic-auth.ini current`、`alembic -c alembic-auth.ini show head`，以及 `information_schema.tables`、`information_schema.columns`、`information_schema.statistics`、`information_schema.table_constraints`、`information_schema.key_column_usage` 的实际状态。脚本不执行 DDL，也不修改 revision。

## 对账与处置

1. DBA 逐条阅读目标 revision 的 `upgrade()`，把每个表、列、索引、外键与证据目录中的实际状态对账；同时检查业务行数和约束是否可验证。
2. 全新安装且身份库确认无任何需保留数据时，从已验证备份恢复；没有备份但身份库为空时，经双人审批后删除专用空身份库重建。
3. 已有身份数据时，不得重建。DBA 编写独立、可审查、可重复执行的补偿迁移，只补齐缺失对象；在隔离副本演练后执行，并重新采集全部证据。
4. 对账完成后运行 `docker compose up auth-migrate`。只有迁移作业退出码为 `0`、`current` 与预期 revision 一致、表/列/索引/外键和行数验证通过，才能恢复 `app`。

## 明确禁令

禁止盲目执行 `alembic stamp`。MySQL DDL 隐式提交意味着 revision 表和真实 schema 可能分离；`stamp` 只改版本记录，不会补齐 DDL。也禁止盲目 `stamp` 后启动应用、直接重跑未知状态的迁移，或在未备份时手工删除已有数据对象。无法确认实际状态时保持应用停止并升级给 DBA。
