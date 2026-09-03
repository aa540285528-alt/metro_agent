#!/usr/bin/env sh
# Run governed knowledge publication commands as an explicitly authorized operator.
set -eu

if [ "${METRO_AGENT_KNOWLEDGE_ADMIN:-}" != "1" ]; then
  echo "knowledge administration requires METRO_AGENT_KNOWLEDGE_ADMIN=1" >&2
  exit 77
fi

operator="${USER:-${USERNAME:-unknown}}"
command="${1:-}"
case "$command" in
  build-and-publish|rollback|verify|status) ;;
  *)
    echo "allowed commands: build-and-publish, rollback, verify, status" >&2
    exit 64
    ;;
esac
shift

case "$command" in
  build-and-publish|rollback)
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
      "$command" --operator-assertion "$operator" "$@"
    ;;
  verify|status)
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
      "$command" "$@"
    ;;
esac
