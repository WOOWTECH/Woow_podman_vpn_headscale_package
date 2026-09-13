#!/usr/bin/env bash
# Destructively restore a validated Woow Headscale backup, with automatic rollback.
set -euo pipefail
umask 077

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PROJECT_DIR=$(pwd -P)
ARCHIVE=""
CONFIRMED=false
WORK=""
SUCCESS=false
DESTRUCTIVE_PHASE=false
CLEANUP_ACTIVE=false
PROJECT_NAME=""

fail() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"; }
usage() { printf 'Usage: %s --archive BACKUP.tar.gz --confirm-destructive-restore\n' "$0"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) [[ $# -ge 2 ]] || fail "--archive requires a path"; ARCHIVE=$2; shift 2 ;;
    --confirm-destructive-restore) CONFIRMED=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ -n $ARCHIVE ]] || fail "--archive is required"
[[ $CONFIRMED == true ]] || fail "refusing destructive restore without --confirm-destructive-restore"
for command_name in python3 podman podman-compose mktemp chmod rm install date; do require_command "$command_name"; done
PROJECT_NAME=$(python3 "$PROJECT_DIR/scripts/runtime_config.py" project-name --env-file "$PROJECT_DIR/.env") \
  || fail "invalid .env"

cleanup() {
  local status=$? rollback_status=0
  if [[ $CLEANUP_ACTIVE == true ]]; then
    exit "$status"
  fi
  CLEANUP_ACTIVE=true
  trap - EXIT
  # Cleanup must not recursively re-enter or be interrupted halfway through rollback.
  trap '' INT TERM HUP
  if [[ $DESTRUCTIVE_PHASE == true && $SUCCESS != true ]]; then
    printf 'Restore did not complete; applying preserved rollback backup.\n' >&2
    "$PROJECT_DIR/deploy.sh" --stop >/dev/null 2>&1 || true
    if ! apply_stage "$WORK/rollback" \
      || ! "$PROJECT_DIR/deploy.sh" >/dev/null \
      || ! "$PROJECT_DIR/scripts/verify.sh" >/dev/null; then
      printf 'FATAL: automatic rollback could not be restarted and verified\n' >&2
      rollback_status=1
    else
      printf 'Previous state was rolled back, restarted, and verified.\n' >&2
    fi
  fi
  # Preserve both stages until rollback and its verification have finished.
  [[ -z $WORK ]] || rm -rf -- "$WORK"
  if (( status == 0 && rollback_status != 0 )); then status=$rollback_status; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

WORK=$(mktemp -d "$PROJECT_DIR/.restore-stage.XXXXXX")
chmod 700 "$WORK"
python3 "$PROJECT_DIR/scripts/archive_security.py" extract --archive "$ARCHIVE" --destination "$WORK/requested"
# Validate restored operator data as data. Nothing from the archive is sourced.
python3 "$PROJECT_DIR/scripts/runtime_config.py" render \
  --env-file "$WORK/requested/project/.env" --output-dir "$WORK/requested-validation" >/dev/null
python3 - "$WORK/requested/project/config/headscale/policy.json" \
  "$WORK/requested/project/runtime/headscale/extra_records.json" <<'PY'
import json, sys
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as source:
        json.load(source)
PY

BACKUP_DIR="$PROJECT_DIR/backups"
if [[ ! -e $BACKUP_DIR ]]; then mkdir -m 700 -- "$BACKUP_DIR"; fi
ROLLBACK_ARCHIVE="$BACKUP_DIR/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
"$PROJECT_DIR/scripts/backup.sh" --output "$ROLLBACK_ARCHIVE" >/dev/null
python3 "$PROJECT_DIR/scripts/archive_security.py" extract \
  --archive "$ROLLBACK_ARCHIVE" --destination "$WORK/rollback"

resolve_volume() {
  python3 "$PROJECT_DIR/scripts/volume_ownership.py" resolve \
    --project "$PROJECT_NAME" --project-dir "$PROJECT_DIR" \
    --container "$1" --destination "$2" --logical "$3"
}
HEADSCALE_VOLUME=$(resolve_volume headscale /var/lib/headscale headscale-data) || fail "cannot resolve Headscale durable volume"
HEADPLANE_VOLUME=$(resolve_volume headplane /var/lib/headplane headplane-data) || fail "cannot resolve Headplane durable volume"

apply_stage() {
  local stage=$1
  # Explicit propagation is required because callers use this function in
  # conditionals, where Bash otherwise suppresses errexit inside functions.
  podman volume rm -f "$HEADSCALE_VOLUME" >/dev/null || return 1
  podman volume rm -f "$HEADPLANE_VOLUME" >/dev/null || return 1
  podman volume create \
    --label "org.woow-headscale.project=woow-headscale" \
    --label "org.woow-headscale.project-dir=$PROJECT_DIR" \
    --label "com.docker.compose.project=$PROJECT_NAME" \
    --label "io.podman.compose.project=$PROJECT_NAME" "$HEADSCALE_VOLUME" >/dev/null || return 1
  podman volume create \
    --label "org.woow-headscale.project=woow-headscale" \
    --label "org.woow-headscale.project-dir=$PROJECT_DIR" \
    --label "com.docker.compose.project=$PROJECT_NAME" \
    --label "io.podman.compose.project=$PROJECT_NAME" "$HEADPLANE_VOLUME" >/dev/null || return 1
  podman volume import "$HEADSCALE_VOLUME" "$stage/volumes/headscale-data.tar" >/dev/null || return 1
  podman volume import "$HEADPLANE_VOLUME" "$stage/volumes/headplane-data.tar" >/dev/null || return 1

  [[ ! -L $PROJECT_DIR/runtime && ! -L $PROJECT_DIR/.env ]] || return 1
  rm -rf -- "$PROJECT_DIR/runtime" || return 1
  mkdir -m 700 -p "$PROJECT_DIR/runtime/headscale" "$PROJECT_DIR/runtime/headplane" || return 1
  install -m 600 "$stage/project/.env" "$PROJECT_DIR/.env" || return 1
  install -m 600 "$stage/project/config/headscale/policy.json" "$PROJECT_DIR/config/headscale/policy.json" || return 1
  install -m 600 "$stage/project/runtime/headscale/config.yaml" "$PROJECT_DIR/runtime/headscale/config.yaml" || return 1
  install -m 600 "$stage/project/runtime/headscale/extra_records.json" "$PROJECT_DIR/runtime/headscale/extra_records.json" || return 1
  install -m 600 "$stage/project/runtime/headplane/config.yaml" "$PROJECT_DIR/runtime/headplane/config.yaml" || return 1
  install -m 600 "$stage/project/runtime/headplane/cookie-secret" "$PROJECT_DIR/runtime/headplane/cookie-secret" || return 1
  install -m 600 "$stage/project/runtime/headplane/api-key" "$PROJECT_DIR/runtime/headplane/api-key" || return 1
}

# From this point onward every signal or failing command must restore the
# preserved state before either staging tree is removed.
DESTRUCTIVE_PHASE=true
if ! "$PROJECT_DIR/deploy.sh" --stop >/dev/null; then
  fail "could not stop this stack safely"
fi
if ! apply_stage "$WORK/requested"; then
  fail "could not apply requested restore state"
fi
if ! "$PROJECT_DIR/deploy.sh" >/dev/null; then
  fail "could not restart requested restore state"
fi
if ! "$PROJECT_DIR/scripts/verify.sh" >/dev/null; then
  fail "requested restore state failed verification"
fi
SUCCESS=true
printf 'Restore completed and verified. Preserved rollback backup: %s\n' "$ROLLBACK_ARCHIVE"
