#!/usr/bin/env bash
# Remove only this Podman stack. Durable data is retained unless doubly confirmed.
set -euo pipefail
umask 077

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PROJECT_DIR=$(pwd -P)
PURGE=false
RETAIN_REQUESTED=false
PURGE_REQUESTED=false
CONFIRMED=false
PROJECT_NAME=""

fail() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"; }
usage() {
  printf 'Usage: %s [--retain-data | --purge-data --confirm-purge-data]\n' "$0"
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --retain-data)
      [[ $PURGE_REQUESTED == false ]] || fail "retain and purge modes are mutually exclusive"
      RETAIN_REQUESTED=true
      shift
      ;;
    --purge-data)
      [[ $RETAIN_REQUESTED == false ]] || fail "retain and purge modes are mutually exclusive"
      PURGE_REQUESTED=true
      PURGE=true
      shift
      ;;
    --confirm-purge-data) CONFIRMED=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
if [[ $PURGE == true && $CONFIRMED != true ]]; then
  fail "refusing data purge without --confirm-purge-data"
fi
if [[ $PURGE == false && $CONFIRMED == true ]]; then
  fail "--confirm-purge-data is valid only with --purge-data"
fi
for command_name in python3 podman podman-compose; do require_command "$command_name"; done
PROJECT_NAME=$(python3 "$PROJECT_DIR/scripts/runtime_config.py" project-name --env-file "$PROJECT_DIR/.env") \
  || fail "invalid .env"

# Refuse to operate if a globally named container has mismatched stack or
# compose-project ownership labels.
for name in headscale headplane ngrok; do
  python3 "$PROJECT_DIR/scripts/volume_ownership.py" check-container \
    --project "$PROJECT_NAME" --project-dir "$PROJECT_DIR" --container "$name" \
    || fail "container $name is not owned by this stack"
done

volumes=()
if [[ $PURGE == true ]]; then
  volume_output=$(
    for specification in \
      "headscale|/var/lib/headscale|headscale-data" \
      "headscale|/var/run/headscale|headscale-run" \
      "headplane|/var/lib/headplane|headplane-data"; do
      IFS='|' read -r container destination logical <<<"$specification"
      python3 "$PROJECT_DIR/scripts/volume_ownership.py" resolve \
        --project "$PROJECT_NAME" --project-dir "$PROJECT_DIR" --container "$container" \
        --destination "$destination" --logical "$logical" --allow-missing \
        || exit 1
    done
  ) || fail "could not identify this stack's volumes safely"
  if [[ -n $volume_output ]]; then
    mapfile -t volumes <<<"$volume_output"
  fi
fi

# Disable only the generated unit that points at this checkout. A missing user
# manager/unit is harmless, and a same-named unit for another checkout is untouched.
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_TARGET="$UNIT_DIR/$PROJECT_NAME.service"
UNIT_IS_OURS=false
if [[ -f $UNIT_TARGET && ! -L $UNIT_TARGET ]] && grep -Fq "$PROJECT_DIR/deploy.sh" "$UNIT_TARGET"; then
  UNIT_IS_OURS=true
fi
HEALTH_UNITS=()
for suffix in health.service health.timer; do
  health_name="${PROJECT_NAME}_${suffix}"
  health_target="$UNIT_DIR/$health_name"
  health_source="$PROJECT_DIR/systemd/$health_name"
  if [[ -f $health_target && ! -L $health_target && -f $health_source ]] \
      && cmp -s -- "$health_source" "$health_target"; then
    HEALTH_UNITS+=("$health_name")
  fi
done
if command -v systemctl >/dev/null 2>&1; then
  # The timer is disabled/stopped before container teardown so a retain-data
  # removal cannot race a live healthcheck against the stack stop.
  if [[ ${#HEALTH_UNITS[@]} -gt 0 ]]; then
    systemctl --user disable --now "${HEALTH_UNITS[@]}" >/dev/null 2>&1 || true
  fi
  if [[ $UNIT_IS_OURS == true ]]; then
    systemctl --user disable --now "$PROJECT_NAME.service" >/dev/null 2>&1 || true
  fi
fi
"$PROJECT_DIR/deploy.sh" --stop >/dev/null

UNITS_REMOVED=false
if [[ $UNIT_IS_OURS == true ]]; then
  rm -f -- "$UNIT_TARGET"
  UNITS_REMOVED=true
fi
for health_name in "${HEALTH_UNITS[@]}"; do
  rm -f -- "$UNIT_DIR/$health_name"
  UNITS_REMOVED=true
done
if [[ $UNITS_REMOVED == true ]] && command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

if [[ $PURGE == true ]]; then
  for volume in "${volumes[@]}"; do
    podman volume rm "$volume" >/dev/null
  done
  [[ ! -L $PROJECT_DIR/runtime ]] || fail "refusing to remove symbolic-link runtime path"
  rm -rf -- "$PROJECT_DIR/runtime"
  printf 'Purged: this stack\x27s Headscale/Headplane named volumes and %s/runtime (generated configs and secrets).\n' "$PROJECT_DIR"
  printf 'Retained: %s/.env and all version-controlled operator configuration.\n' "$PROJECT_DIR"
else
  printf 'Removed: this stack\x27s containers, network, and generated user unit.\n'
  printf 'Retained: this stack\x27s named volumes, %s/runtime (including secrets), and %s/.env.\n' "$PROJECT_DIR" "$PROJECT_DIR"
fi
printf 'No unrelated Podman containers, networks, images, or volumes were removed.\n'
