#!/bin/sh
set -eu

: "${METRO_AGENT_IMAGE:?必须设置包含完整 digest 的 METRO_AGENT_IMAGE}"
case "$METRO_AGENT_IMAGE" in
  *@sha256:*) ;;
  *) echo "METRO_AGENT_IMAGE 必须使用 @sha256: 完整 digest" >&2; exit 2 ;;
esac
printf '%s' "$METRO_AGENT_IMAGE" | grep -Eq '^.+@sha256:[0-9a-f]{64}$' || {
  echo "METRO_AGENT_IMAGE digest 必须是 64 位小写十六进制" >&2
  exit 2
}
export METRO_AGENT_IMAGE

docker compose stop app
docker compose pull app db-migrate auth-migrate
docker compose up -d --no-build app
attempt=0
until docker compose exec -T app python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready',timeout=3)"; do
  attempt=$((attempt + 1))
  test "$attempt" -lt 30 || { echo "/api/ready 未就绪" >&2; exit 5; }
  sleep 2
done
