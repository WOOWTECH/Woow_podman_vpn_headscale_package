#!/usr/bin/env bash
# scripts/upgrade.sh: move the Woow Headscale stack to the versions pinned in this checkout,
# with an automatic rollback of the units when the upgrade fails.
#
#   git pull && scripts/upgrade.sh [--no-backup] [--rebuild]
#
#   --no-backup  skip the pre-upgrade archive. Only for a stack with nothing worth keeping:
#                the Headscale database holds every node's registration.
#   --rebuild    build localhost/woow-headscale again even when the tag already exists
#
# Steps: backup -> snapshot the installed units -> scripts/install.sh (which builds and pulls
# the new pinned images before it touches a unit) -> tests/smoke.sh. When install or smoke
# fails, the saved units are put back and the stack is restarted on the previous images.
#
# Headscale migrates its SQLite schema on start and does not migrate back, so a rollback of
# the units across a schema change is not enough by itself: restore the pre-upgrade archive
# with scripts/restore.sh. The archive path is printed at the start of every run.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=common.sh
. "$REPO/scripts/common.sh"

no_backup=0 rebuild=()
while (($#)); do
  case $1 in
    --no-backup) no_backup=1 ;;
    --rebuild) rebuild=(--rebuild) ;;
    -h | --help) sed -n '2,17p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
ql_preflight "$PODMAN_MIN"
[[ -f $ENV_FILE ]] || ql_die "$ENV_FILE does not exist: run scripts/install.sh first"
ql_lock "$APP"

snap=$(app_new_backup_dir upgrade)
data=''
if ((no_backup == 0)); then
  data=$("$REPO/scripts/backup.sh" --include-secrets) || ql_die "backup failed; nothing was changed"
  ql_info "pre-upgrade backup: $data"
fi
app_snapshot "$snap/units"
podman inspect --format '{{.Name}} {{.ImageName}} {{.Image}}' headscale headplane >"$snap/images.txt" 2>/dev/null || true

if "$REPO/scripts/install.sh" --accept-defaults --no-smoke "${rebuild[@]}" && "$REPO/tests/smoke.sh"; then
  ql_info "upgrade complete (unit snapshot: $snap)"
  exit 0
fi

ql_warn "upgrade failed; rolling back to the units saved in $snap/units"
app_snapshot_restore "$snap/units" \
  || ql_die "rollback failed: no usable snapshot. Inspect $snap and journalctl --user -u headscale.service"
systemctl --user restart headscale.service headplane.service || true
if ql_wait_container_healthy headscale 300 && "$REPO/tests/smoke.sh" --quick; then
  ql_die "upgrade failed and was rolled back; the previous version is running again"
fi
ql_die "upgrade failed and the rollback is unhealthy too. Restore the data with:
  scripts/restore.sh --archive ${data:-<no backup was taken>} --confirm-restore $APP --restore-secrets"
