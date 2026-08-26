#!/bin/sh
set -eu

EVIDENCE_DIR=${1:?用法: inspect-mysql-partial-ddl.sh /absolute/evidence-directory}

case "$EVIDENCE_DIR" in
  /*) ;;
  *) echo "证据目录必须是绝对路径" >&2; exit 2 ;;
esac

mkdir -p "$EVIDENCE_DIR"
docker compose stop app
docker compose logs --no-color auth-migrate > "$EVIDENCE_DIR/auth-migrate.log" 2>&1 || true
docker compose run --rm --no-deps auth-migrate \
  alembic -c alembic-auth.ini current \
  > "$EVIDENCE_DIR/alembic-current.txt" 2>&1 || true
docker compose run --rm --no-deps auth-migrate \
  alembic -c alembic-auth.ini show head \
  > "$EVIDENCE_DIR/alembic-head.txt" 2>&1
docker compose exec -T mysql sh -c \
  'exec mysql -h mysql --ssl-mode=VERIFY_IDENTITY --ssl-ca=/etc/mysql/ca/ca.pem -uroot -p"$MYSQL_ROOT_PASSWORD" --batch --raw' \
  > "$EVIDENCE_DIR/information-schema.txt" <<'SQL'
SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE, TABLE_COLLATION
FROM information_schema.tables
WHERE TABLE_SCHEMA = 'metro_auth'
ORDER BY TABLE_NAME;
SELECT TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE,
       IS_NULLABLE, COLUMN_DEFAULT, EXTRA
FROM information_schema.columns
WHERE TABLE_SCHEMA = 'metro_auth'
ORDER BY TABLE_NAME, ORDINAL_POSITION;
SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
FROM information_schema.statistics
WHERE TABLE_SCHEMA = 'metro_auth'
ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;
SELECT tc.TABLE_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE,
       kcu.COLUMN_NAME, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME
FROM information_schema.table_constraints AS tc
LEFT JOIN information_schema.key_column_usage AS kcu
  ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
 AND kcu.TABLE_NAME = tc.TABLE_NAME
 AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
WHERE tc.TABLE_SCHEMA = 'metro_auth'
ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION;
SQL

printf '%s\n' \
  "检查材料已写入 $EVIDENCE_DIR。保持 app 停止，由 DBA 对照 revision SQL 逐项核验。" \
  "禁止修改 revision 记录来掩盖缺失 DDL；按中文 runbook 选择恢复备份、重建空库或补偿迁移。"
