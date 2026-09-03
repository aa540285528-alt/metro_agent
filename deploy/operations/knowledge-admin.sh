#!/usr/bin/env sh
# Run governed knowledge publication commands as an effective root operator.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "knowledge administration requires an effective root operator" >&2
  exit 77
fi

operator="${SUDO_USER:-${USER:-$(id -un)}}"
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
  build-and-publish)
    force_reason=""
    if [ "$#" -eq 0 ]; then
      :
    elif [ "$#" -eq 2 ] && [ "$1" = "--force-rebuild" ]; then
      force_reason="$2"
      case "$force_reason" in
        indexer-upgrade|embedding-model-change|reranker-model-change|chunker-change|recovery) ;;
        *)
          echo "unknown or duplicate knowledge administration parameter" >&2
          exit 64
          ;;
      esac
    else
      echo "unknown or duplicate knowledge administration parameter" >&2
      exit 64
    fi
    if [ -n "$force_reason" ]; then
      exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
        build-and-publish --operator-assertion "$operator" --force-rebuild "$force_reason"
    fi
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
      build-and-publish --operator-assertion "$operator"
    ;;
  rollback)
    if [ "$#" -ne 0 ]; then
      echo "unknown or duplicate knowledge administration parameter" >&2
      exit 64
    fi
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
      rollback --operator-assertion "$operator"
    ;;
  verify|status)
    if [ "$#" -ne 0 ]; then
      echo "unknown or duplicate knowledge administration parameter" >&2
      exit 64
    fi
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer "$command"
    ;;
esac
