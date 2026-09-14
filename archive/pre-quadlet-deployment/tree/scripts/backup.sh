#!/usr/bin/env bash
# Create a cold, private, atomic backup of this stack. Secret values are never logged.
set -euo pipefail
umask 077

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PROJECT_DIR=$(pwd -P)
RUNTIME_DIR="$PROJECT_DIR/runtime"
OUTPUT="$PROJECT_DIR/backups"
STAGE=""
TEMP_ARCHIVE=""
STACK_STOPPED=false
CLEANUP_ACTIVE=false
PROJECT_NAME=""

fail() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"; }

usage() { printf 'Usage: %s [--output DIRECTORY|ARCHIVE.tar.gz]\n' "$0"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) [[ $# -ge 2 ]] || fail "--output requires a path"; OUTPUT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
for command_name in python3 podman podman-compose tar mktemp cp chmod date; do require_command "$command_name"; done
PROJECT_NAME=$(python3 "$PROJECT_DIR/scripts/runtime_config.py" project-name --env-file "$PROJECT_DIR/.env") \
  || fail "invalid .env"

if [[ $OUTPUT == "$PROJECT_DIR/backups" && ! -e $OUTPUT ]]; then
  mkdir -m 700 -- "$OUTPUT"
fi
filename="woow-headscale-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
FINAL_ARCHIVE=$(python3 "$PROJECT_DIR/scripts/archive_security.py" output-path \
  --output "$OUTPUT" --filename "$filename") || fail "unsafe backup destination"

cleanup() {
  local original_status=$? restart_status=0
  if [[ $CLEANUP_ACTIVE == true ]]; then
    exit "$original_status"
  fi
  CLEANUP_ACTIVE=true
  trap - EXIT
  # Pending or repeated signals must not interrupt secret deletion or restart.
  trap '' INT TERM HUP
  [[ -z $TEMP_ARCHIVE ]] || rm -f -- "$TEMP_ARCHIVE"
  [[ -z $STAGE ]] || rm -rf -- "$STAGE"
  if [[ $STACK_STOPPED == true ]]; then
    if ! "$PROJECT_DIR/deploy.sh" >/dev/null; then
      printf 'FATAL: stack restart failed during backup cleanup\n' >&2
      restart_status=1
    elif ! "$PROJECT_DIR/scripts/verify.sh" >/dev/null; then
      printf 'FATAL: stack verification failed during backup cleanup\n' >&2
      restart_status=1
    fi
  fi
  if (( original_status != 0 )); then exit "$original_status"; fi
  exit "$restart_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# Use only exact compose-project volume names with matching ownership labels.
# An existing owned container is additional evidence and must mount that exact volume.
resolve_volume() {
  python3 "$PROJECT_DIR/scripts/volume_ownership.py" resolve \
    --project "$PROJECT_NAME" --project-dir "$PROJECT_DIR" \
    --container "$1" --destination "$2" --logical "$3"
}
HEADSCALE_VOLUME=$(resolve_volume headscale /var/lib/headscale headscale-data) || fail "cannot resolve Headscale durable volume"
HEADPLANE_VOLUME=$(resolve_volume headplane /var/lib/headplane headplane-data) || fail "cannot resolve Headplane durable volume"

# Mark before invoking stop so EXIT cleanup also repairs a partially failed stop.
STACK_STOPPED=true
"$PROJECT_DIR/deploy.sh" --stop >/dev/null

STAGE=$(mktemp -d "$PROJECT_DIR/.backup-stage.XXXXXX")
chmod 700 "$STAGE"
mkdir -m 700 -p "$STAGE/project/config/headscale" "$STAGE/project/runtime/headscale" \
  "$STAGE/project/runtime/headplane" "$STAGE/volumes"

copy_private_file() {
  local source=$1 target=$2
  [[ -f $source && ! -L $source ]] || fail "required backup source is missing or unsafe: ${source#"$PROJECT_DIR/"}"
  cp --no-dereference -- "$source" "$target"
  chmod 600 "$target"
}
copy_private_file "$PROJECT_DIR/.env" "$STAGE/project/.env"
copy_private_file "$PROJECT_DIR/config/headscale/policy.json" "$STAGE/project/config/headscale/policy.json"
copy_private_file "$RUNTIME_DIR/headscale/config.yaml" "$STAGE/project/runtime/headscale/config.yaml"
copy_private_file "$RUNTIME_DIR/headscale/extra_records.json" "$STAGE/project/runtime/headscale/extra_records.json"
copy_private_file "$RUNTIME_DIR/headplane/config.yaml" "$STAGE/project/runtime/headplane/config.yaml"
copy_private_file "$RUNTIME_DIR/headplane/cookie-secret" "$STAGE/project/runtime/headplane/cookie-secret"
copy_private_file "$RUNTIME_DIR/headplane/api-key" "$STAGE/project/runtime/headplane/api-key"

podman volume export "$HEADSCALE_VOLUME" --output "$STAGE/volumes/headscale-data.tar" >/dev/null
podman volume export "$HEADPLANE_VOLUME" --output "$STAGE/volumes/headplane-data.tar" >/dev/null
chmod 600 "$STAGE/volumes/"*.tar
python3 - "$STAGE/manifest.json" "$HEADSCALE_VOLUME" "$HEADPLANE_VOLUME" <<'PY'
import json, os, sys
path, headscale, headplane = sys.argv[1:]
with open(path, "x", encoding="utf-8") as output:
    json.dump({"format": "woow-headscale-backup-v1", "volumes": {
        "headscale-data": headscale, "headplane-data": headplane}}, output, sort_keys=True)
    output.write("\n")
os.chmod(path, 0o600)
PY

TEMP_ARCHIVE=$(mktemp "$(dirname -- "$FINAL_ARCHIVE")/.woow-headscale-backup.XXXXXX")
chmod 600 "$TEMP_ARCHIVE"
tar -C "$STAGE" -czf "$TEMP_ARCHIVE" manifest.json project volumes
python3 "$PROJECT_DIR/scripts/archive_security.py" validate --archive "$TEMP_ARCHIVE"
python3 - "$TEMP_ARCHIVE" <<'PY'
import os, sys
with open(sys.argv[1], "rb") as archive:
    os.fsync(archive.fileno())
PY
# A same-filesystem hard link publishes atomically to this exact path and fails
# with EEXIST for every existing inode, including directories and symlinks.
python3 - "$TEMP_ARCHIVE" "$FINAL_ARCHIVE" <<'PY'
import os, sys
source, destination = sys.argv[1:]
os.link(source, destination)
directory_fd = os.open(os.path.dirname(destination), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
rm -f -- "$TEMP_ARCHIVE"
TEMP_ARCHIVE=""
printf 'Backup created: %s\n' "$FINAL_ARCHIVE"
