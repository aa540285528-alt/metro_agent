#!/bin/sh
set -eu

BACKUP_DIR=${1:?用法: backup-all.sh /absolute/new-backup-directory}
: "${METRO_AGENT_IMAGE:?备份前必须设置包含完整 digest 的 METRO_AGENT_IMAGE}"
printf '%s' "$METRO_AGENT_IMAGE" | grep -Eq '^.+@sha256:[0-9a-f]{64}$' || {
  echo "METRO_AGENT_IMAGE 必须使用完整 @sha256: digest" >&2
  exit 2
}
case "$BACKUP_DIR" in
  /*) ;;
  *) echo "备份目录必须是绝对路径" >&2; exit 2 ;;
esac
if [ -e "$BACKUP_DIR" ]; then
  echo "拒绝覆盖已存在的备份目录: $BACKUP_DIR" >&2
  exit 2
fi
mkdir -m 0700 "$BACKUP_DIR"

docker compose stop app
docker compose images app > "$BACKUP_DIR/image.txt"
printf '%s\n' "$METRO_AGENT_IMAGE" > "$BACKUP_DIR/metro-agent-image.txt"
docker compose run --rm --no-deps db-migrate alembic current \
  > "$BACKUP_DIR/alembic-current-postgres.txt"
docker compose run --rm --no-deps auth-migrate \
  alembic -c alembic-auth.ini current \
  > "$BACKUP_DIR/alembic-current-mysql.txt"

docker compose exec -T mysql sh -c \
  'exec mysqldump --single-transaction --routines --events --triggers -h mysql --ssl-mode=VERIFY_IDENTITY --ssl-ca=/etc/mysql/ca/ca.pem -uroot -p"$MYSQL_ROOT_PASSWORD" metro_auth' \
  > "$BACKUP_DIR/metro_auth.sql"
docker compose exec -T postgres \
  pg_dump -Fc -U metro_agent -d metro_agent \
  > "$BACKUP_DIR/metro_agent.dump"

docker compose run --rm --no-deps -v "$BACKUP_DIR:/backup" app python -c \
  "import tarfile; a=tarfile.open('/backup/chroma.tar.gz','w:gz'); a.add('/var/lib/metro-agent/chroma/current',arcname='current'); a.close(); b=tarfile.open('/backup/memory-chroma.tar.gz','w:gz'); b.add('/var/lib/metro-agent/memory-chroma/current',arcname='current'); b.close()"

docker compose exec -T redis redis-cli SAVE > "$BACKUP_DIR/redis-save.txt"
docker compose cp redis:/data/dump.rdb "$BACKUP_DIR/redis-dump.rdb"

docker compose exec -T postgres psql -X -U metro_agent -d metro_agent -At -F '|' \
  -c "SELECT 'postgres.conversations', owner_id, count(*) FROM conversations GROUP BY owner_id ORDER BY owner_id" \
  > "$BACKUP_DIR/owner-counts-before.txt"
docker compose exec -T postgres psql -X -U metro_agent -d metro_agent -At -F '|' \
  -c "SELECT 'postgres.agent_traces', user_id, count(*) FROM agent_traces GROUP BY user_id ORDER BY user_id" \
  >> "$BACKUP_DIR/owner-counts-before.txt"
docker compose run --rm --no-deps app python deploy/operations/chroma-owner-counts.py \
  knowledge /var/lib/metro-agent/chroma/current \
  >> "$BACKUP_DIR/owner-counts-before.txt"
docker compose run --rm --no-deps app python deploy/operations/chroma-owner-counts.py \
  memory /var/lib/metro-agent/memory-chroma/current \
  >> "$BACKUP_DIR/owner-counts-before.txt"

(cd "$BACKUP_DIR" && sha256sum \
  image.txt metro-agent-image.txt \
  alembic-current-postgres.txt alembic-current-mysql.txt \
  metro_auth.sql metro_agent.dump chroma.tar.gz memory-chroma.tar.gz \
  redis-save.txt redis-dump.rdb owner-counts-before.txt > SHA256SUMS)
printf '%s\n' "一致性备份完成，app 保持停止: $BACKUP_DIR"
