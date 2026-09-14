#!/bin/sh
set -eu

BACKUP_DIR=${1:?用法: backup-all.sh /absolute/new-backup-directory}
: "${METRO_AGENT_ENV_FILE:?请设置唯一部署 .env 的绝对路径}"
: "${METRO_AGENT_COMPOSE_PROJECT_NAME:?请设置固定 Compose 项目名}"
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

compose() {
  docker compose --env-file "$METRO_AGENT_ENV_FILE" --project-name "$METRO_AGENT_COMPOSE_PROJECT_NAME" "$@"
}

LOCK_TOKEN_FILE="$BACKUP_DIR/.knowledge-publication-token"
LOCK_RELEASE_FILE="$BACKUP_DIR/.knowledge-publication-release"
LOCK_CONTAINER=$(compose --profile knowledge-backup run -d --no-deps \
  -v "$BACKUP_DIR:/backup" knowledge-backup \
  python deploy/operations/with-knowledge-publication-lock.py hold \
  --token-file /backup/.knowledge-publication-token \
  --release-file /backup/.knowledge-publication-release)

release_publication_lock() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "${LOCK_CONTAINER:-}" ]; then
    : > "$LOCK_RELEASE_FILE"
    lock_holder_status=$(docker wait "$LOCK_CONTAINER") || {
      echo "知识发布锁持有器异常退出" >&2
      status=1
    }
    if test "${lock_holder_status:-1}" != "0"; then
      echo "知识发布锁持有器以非零状态退出: ${lock_holder_status:-unknown}" >&2
      status=1
    fi
    docker rm "$LOCK_CONTAINER" >/dev/null 2>&1 || true
  fi
  rm -f "$LOCK_TOKEN_FILE" "$LOCK_RELEASE_FILE"
  exit "$status"
}
trap release_publication_lock EXIT HUP INT TERM

attempt=0
until test -s "$LOCK_TOKEN_FILE"; do
  attempt=$((attempt + 1))
  test "$attempt" -lt 30 || { echo "未能获得知识发布锁" >&2; exit 3; }
  sleep 1
done
LOCK_TOKEN=$(cat "$LOCK_TOKEN_FILE")
verify_publication_lock() {
  compose --profile knowledge-backup run --rm --no-deps knowledge-backup \
    python deploy/operations/with-knowledge-publication-lock.py verify-token \
    --token "$LOCK_TOKEN"
}

verify_publication_lock
if compose ps --services --filter status=running | grep -qx 'knowledge-indexer'; then
  echo "知识 indexer 仍在运行，拒绝备份" >&2
  exit 3
fi
verify_publication_lock
compose stop app knowledge-read-proxy chroma
verify_publication_lock
compose images app > "$BACKUP_DIR/image.txt"
printf '%s\n' "$METRO_AGENT_IMAGE" > "$BACKUP_DIR/metro-agent-image.txt"
compose run --rm --no-deps db-migrate alembic current \
  > "$BACKUP_DIR/alembic-current-postgres.txt"
compose run --rm --no-deps auth-migrate \
  alembic -c alembic-auth.ini current \
  > "$BACKUP_DIR/alembic-current-mysql.txt"

compose exec -T mysql sh -c \
  'exec mysqldump --single-transaction --routines --events --triggers -h mysql --ssl-mode=VERIFY_IDENTITY --ssl-ca=/etc/mysql/ca/ca.pem -uroot -p"$MYSQL_ROOT_PASSWORD" metro_auth' \
  > "$BACKUP_DIR/metro_auth.sql"
compose exec -T postgres \
  pg_dump -Fc -U metro_agent -d metro_agent \
  > "$BACKUP_DIR/metro_agent.dump"

compose run --rm --no-deps -v "$BACKUP_DIR:/backup" app python -c \
  "import tarfile; a=tarfile.open('/backup/memory-chroma.tar.gz','w:gz'); a.add('/var/lib/metro-agent/memory-chroma/current',arcname='current'); a.close()"
verify_publication_lock
compose --profile knowledge-backup run --rm --no-deps -v "$BACKUP_DIR:/backup" \
  knowledge-backup python -c \
  "import tarfile; a=tarfile.open('/backup/knowledge-chroma.tar.gz','w:gz'); a.add('/chroma',arcname='chroma'); a.close(); b=tarfile.open('/backup/knowledge-artifacts.tar.gz','w:gz'); b.add('/var/lib/metro-agent/knowledge-artifacts',arcname='knowledge-artifacts'); b.close()"

compose exec -T redis redis-cli SAVE > "$BACKUP_DIR/redis-save.txt"
compose cp redis:/data/dump.rdb "$BACKUP_DIR/redis-dump.rdb"

compose exec -T postgres psql -X -U metro_agent -d metro_agent -At -F '|' \
  -c "SELECT 'postgres.conversations', owner_id, count(*) FROM conversations GROUP BY owner_id ORDER BY owner_id" \
  > "$BACKUP_DIR/owner-counts-before.txt"
compose exec -T postgres psql -X -U metro_agent -d metro_agent -At -F '|' \
  -c "SELECT 'postgres.agent_traces', user_id, count(*) FROM agent_traces GROUP BY user_id ORDER BY user_id" \
  >> "$BACKUP_DIR/owner-counts-before.txt"
compose run --rm --no-deps app python deploy/operations/chroma-owner-counts.py \
  memory /var/lib/metro-agent/memory-chroma/current \
  >> "$BACKUP_DIR/owner-counts-before.txt"

verify_publication_lock
(cd "$BACKUP_DIR" && sha256sum \
  image.txt metro-agent-image.txt \
  alembic-current-postgres.txt alembic-current-mysql.txt \
  metro_auth.sql metro_agent.dump memory-chroma.tar.gz \
  knowledge-chroma.tar.gz knowledge-artifacts.tar.gz \
  redis-save.txt redis-dump.rdb owner-counts-before.txt > SHA256SUMS)
verify_publication_lock
printf '%s\n' "一致性备份完成，app 保持停止: $BACKUP_DIR"
