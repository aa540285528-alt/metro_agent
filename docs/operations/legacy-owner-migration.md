# Legacy Owner 两阶段迁移手册

该流程只处理 PostgreSQL 的 `conversations.owner_id` 与 `agent_traces.user_id`。`conversation_messages` 和 Trace 子表通过外键继承归属，不直接更新。长期记忆 Chroma 必须使用同一份审批映射另行迁移，不能用 SQL 或全文替换。

## 前置条件

备份是 apply 的前置条件。先执行协调备份并验证校验和，记录绝对路径或工单号作为 `BACKUP_REFERENCE`；停止 `app`，确认没有写流量，并由身份管理员批准唯一的 `legacy_owner -> auth:<id>` 映射。

## 第一阶段：只读预览

```sh
docker compose stop app
docker compose exec -T postgres psql -X -U metro_agent -d metro_agent \
  -v LEGACY_OWNER=legacy-alice -v TARGET_OWNER=auth:42 \
  < deploy/operations/legacy-owner-preview.sql
```

`legacy-owner-preview.sql` 在 `BEGIN TRANSACTION READ ONLY` 中报告会话候选数、Trace 候选数和精确总数 `candidate_count`，最后无条件 `ROLLBACK`。把精确总数写入审批单，进入**人工停点**；预览脚本不会自动调用 apply。

## 第二阶段：人工给数后应用

审批人把预览总数原样填入 `EXPECTED_COUNT`，并填写已验证备份引用：

```sh
docker compose exec -T postgres psql -X -U metro_agent -d metro_agent \
  -v LEGACY_OWNER=legacy-alice -v TARGET_OWNER=auth:42 \
  -v EXPECTED_COUNT=24 -v BACKUP_REFERENCE=/srv/backups/pilot-20260826 \
  < deploy/operations/legacy-owner-apply.sql
```

apply 在同一事务中锁定两张表，重新计算候选数，再通过两个 `UPDATE ... RETURNING` 取得精确影响数。候选数不等于 EXPECTED_COUNT、实际更新总数不一致、目标值不匹配 `auth:<数字 id>`、缺少备份引用或任何 SQL 错误时，脚本都会在 `COMMIT` 前回滚并非零退出。旧模板把 psql 变量放进 `DO` / `GET DIAGNOSTICS`，变量替换边界不可靠；当前脚本不依赖该写法。

完成后再次运行 preview，要求旧 owner 的 `candidate_count` 为 `0`，并核对目标 owner 增量、跨用户 `404`、Trace 查询和 Chroma 长期记忆元数据。影响数不一致时不要修改审批数来迁就现场；保持 `app` 停止，从备份恢复或由 DBA 查明新增写入来源。
