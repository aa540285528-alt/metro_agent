#!/bin/sh
set -eu

BACKUP_DIR=${1:?用法: restore-all.sh /absolute/backup-directory}
case "$BACKUP_DIR" in
  /*) ;;
  *) echo "备份目录必须是绝对路径" >&2; exit 2 ;;
esac
for required in metro_auth.sql metro_agent.dump memory-chroma.tar.gz knowledge-chroma.tar.gz knowledge-artifacts.tar.gz redis-dump.rdb SHA256SUMS alembic-current-postgres.txt alembic-current-mysql.txt metro-agent-image.txt owner-counts-before.txt; do
  test -s "$BACKUP_DIR/$required" || { echo "缺少备份文件: $required" >&2; exit 2; }
done
(cd "$BACKUP_DIR" && sha256sum -c SHA256SUMS)

METRO_AGENT_IMAGE=$(cat "$BACKUP_DIR/metro-agent-image.txt")
if test "$(printf '%s' "$METRO_AGENT_IMAGE" | wc -l)" -ne 0 || \
   ! printf '%s' "$METRO_AGENT_IMAGE" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'; then
  echo "备份中的 metro-agent-image.txt 必须是完整的小写 sha256 digest" >&2
  exit 2
fi
export METRO_AGENT_IMAGE

docker compose stop app knowledge-read-proxy chroma
docker compose up -d --wait mysql postgres

echo RESTORE_STEP=MySQL
docker compose exec -T mysql sh -c \
  'exec mysql -h mysql --ssl-mode=VERIFY_IDENTITY --ssl-ca=/etc/mysql/ca/ca.pem -uroot -p"$MYSQL_ROOT_PASSWORD"' <<'SQL'
DROP DATABASE IF EXISTS metro_auth;
CREATE DATABASE metro_auth CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
GRANT ALL PRIVILEGES ON metro_auth.* TO 'metro_auth'@'%';
SQL
docker compose exec -T mysql sh -c \
  'exec mysql -h mysql --ssl-mode=VERIFY_IDENTITY --ssl-ca=/etc/mysql/ca/ca.pem -uroot -p"$MYSQL_ROOT_PASSWORD" metro_auth' \
  < "$BACKUP_DIR/metro_auth.sql"

echo RESTORE_STEP=PostgreSQL
docker compose exec -T postgres \
  pg_restore --clean --if-exists --no-owner -U metro_agent -d metro_agent \
  < "$BACKUP_DIR/metro_agent.dump"

echo RESTORE_STEP=Chroma
docker compose run --rm --no-deps -v "$BACKUP_DIR:/backup:ro" app \
  python deploy/operations/safe-restore-tar.py swap-volume /backup/memory-chroma.tar.gz /var/lib/metro-agent/memory-chroma --expected-root current
# The full knowledge volume is staged and switched only after every tar member
# has been inspected.  It is never extracted over the live Chroma root.
docker compose run --rm --no-deps -v "$BACKUP_DIR:/backup:ro" -v knowledge_chroma_data:/restore app \
  python deploy/operations/safe-restore-tar.py replace-volume-contents /backup/knowledge-chroma.tar.gz /restore --expected-root chroma
# Artifacts are append-only evidence.  Extraction never clears existing releases.
docker compose run --rm --no-deps -v "$BACKUP_DIR:/backup:ro" knowledge-indexer sh -ceu \
  'python deploy/operations/safe-restore-tar.py merge-artifacts /backup/knowledge-artifacts.tar.gz /var/lib/metro-agent --expected-root knowledge-artifacts'

echo RESTORE_STEP=Redis
docker compose stop redis
docker compose run --rm --no-deps -v "$BACKUP_DIR:/backup:ro" redis sh -c \
  'rm -rf /data/appendonlydir; cp /backup/redis-dump.rdb /data/dump.rdb; chmod 0644 /data/dump.rdb'
docker compose up -d redis

docker compose run --rm --no-deps db-migrate alembic current \
  > "$BACKUP_DIR/alembic-current-postgres.after.txt"
docker compose run --rm --no-deps auth-migrate \
  alembic -c alembic-auth.ini current \
  > "$BACKUP_DIR/alembic-current-mysql.after.txt"
cmp "$BACKUP_DIR/alembic-current-postgres.txt" "$BACKUP_DIR/alembic-current-postgres.after.txt"
cmp "$BACKUP_DIR/alembic-current-mysql.txt" "$BACKUP_DIR/alembic-current-mysql.after.txt"

docker compose exec -T postgres psql -X -U metro_agent -d metro_agent -At -F '|' \
  -c "SELECT 'postgres.conversations', owner_id, count(*) FROM conversations GROUP BY owner_id ORDER BY owner_id" \
  > "$BACKUP_DIR/owner-counts-after.txt"
docker compose exec -T postgres psql -X -U metro_agent -d metro_agent -At -F '|' \
  -c "SELECT 'postgres.agent_traces', user_id, count(*) FROM agent_traces GROUP BY user_id ORDER BY user_id" \
  >> "$BACKUP_DIR/owner-counts-after.txt"
docker compose run --rm --no-deps app python deploy/operations/chroma-owner-counts.py \
  memory /var/lib/metro-agent/memory-chroma/current \
  >> "$BACKUP_DIR/owner-counts-after.txt"
cmp "$BACKUP_DIR/owner-counts-before.txt" "$BACKUP_DIR/owner-counts-after.txt"

docker compose up -d --wait chroma redis
verify-restored-release() {
  docker compose --profile knowledge-admin run --rm --no-deps knowledge-indexer \
    python -m metro_agent.tools.knowledge_indexer verify
}
verify-restored-release

docker compose up -d --wait knowledge-read-proxy
attempt=0
until docker compose exec -T knowledge-read-proxy python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v2/heartbeat',timeout=3)"; do
  attempt=$((attempt + 1))
  test "$attempt" -lt 30 || { echo "knowledge-read-proxy 未就绪" >&2; exit 5; }
  sleep 2
done
docker compose up -d --no-build app
attempt=0
until docker compose exec -T app python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready',timeout=3)"; do
  attempt=$((attempt + 1))
  test "$attempt" -lt 30 || { echo "/api/ready 未就绪" >&2; exit 5; }
  sleep 2
done
printf '%s\n' "恢复和校验完成；验收后再删除 Chroma rollback.previous"
