#!/usr/bin/env sh
# Run governed knowledge publication commands as an effective root operator.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "knowledge administration requires an effective root operator" >&2
  exit 77
fi

operator="$(id -un)"
command="${1:-}"
case "$command" in
  build-and-publish|rollback|verify|status) ;;
  *)
    echo "allowed commands: build-and-publish, rollback, verify, status" >&2
    exit 64
    ;;
esac
shift

write_audit_record() {
  audit_directory="/var/log/metro-agent"
  audit_file="$audit_directory/knowledge-admin-audit.log"
  if [ -L "$audit_directory" ] || [ -L "$audit_file" ]; then
    echo "knowledge administration audit path is unsafe" >&2
    exit 73
  fi
  install -d -m 0700 -o root -g root "$audit_directory"
  touch "$audit_file"
  chown root:root "$audit_file"
  chmod 0600 "$audit_file"
  printf 'timestamp_utc=%s operator_identity=%s command=%s parameter_status=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$operator" "$1" "$2" >> "$audit_file"
}

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
      write_audit_record "build-and-publish" "force-rebuild"
      exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
        build-and-publish --force-rebuild "$force_reason"
    fi
    write_audit_record "build-and-publish" "none"
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
      build-and-publish
    ;;
  rollback)
    if [ "$#" -ne 0 ]; then
      echo "unknown or duplicate knowledge administration parameter" >&2
      exit 64
    fi
    write_audit_record "rollback" "none"
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer \
      rollback
    ;;
  verify|status)
    if [ "$#" -ne 0 ]; then
      echo "unknown or duplicate knowledge administration parameter" >&2
      exit 64
    fi
    write_audit_record "$command" "none"
    exec docker compose --profile knowledge-admin run --rm knowledge-indexer "$command"
    ;;
esac
