#!/usr/bin/env bash
# scripts/uninstall.sh: remove the Woow Headscale Quadlet units. Keeps all data by default.
#
#   scripts/uninstall.sh                        stop and remove the units; keep the three
#                                               volumes, the network, both secrets, the images,
#                                               ~/.config/headscale and every backup
#   scripts/uninstall.sh --purge --confirm-purge headscale     (or --purge --yes)
#                                               also delete the volumes, the network, the
#                                               headscale-* secrets and ~/.config/headscale,
#                                               after a final cold backup that includes them
#   scripts/uninstall.sh --dry-run              report what would be removed
#
# --purge is the only way this repo deletes data, and deleting headscale-data means every
# Tailscale node has to be enrolled again. Images and backups are never deleted.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=common.sh
. "$REPO/scripts/common.sh"

purge=0 yes=0
while (($#)); do
  case $1 in
    --purge) purge=1 ;;
    --yes) yes=1 ;;
    --confirm-purge) (($# >= 2)) || ql_die "--confirm-purge needs the word $APP"; [[ $2 == "$APP" ]] || ql_die "--confirm-purge needs the word $APP"; yes=1; shift ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    -h | --help) sed -n '2,16p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
ql_require_rootless
ql_lock "$APP"

if ((!purge)); then
  ql_uninstall_units "$APP"
  exit 0
fi

app_confirm "$APP" "$yes" "--purge deletes the Headscale database (every node has to re-enrol), Headplane's state, the network, both secrets and the settings"
if [[ ${QL_DRY_RUN:-0} != 1 ]]; then
  final=$(app_new_backup_dir final)
  # Cold copies: both containers stop first, so the SQLite database is consistent.
  systemctl --user stop headplane.service headscale.service >/dev/null 2>&1 || true
  for v in "${BACKUP_VOLUMES[@]}"; do
    if podman volume exists "$v"; then ql_backup_volume "$v" "$final" >/dev/null; fi
  done
  (umask 077 && mkdir -- "$final/secrets") || ql_die "cannot create $final/secrets"
  for s in "$SECRET_COOKIE" "$SECRET_API_KEY"; do
    if podman secret exists "$s"; then
      (umask 077 && app_secret_read "$s" >"$final/secrets/${s#"$APP"-}") || ql_warn "cannot read secret $s"
    fi
  done
  if [[ -f $ENV_FILE ]]; then install -m 600 -- "$ENV_FILE" "$final/${ENV_FILE##*/}"; fi
  app_checksums "$final"
  ql_info "final backup: $final"
fi
ql_uninstall_units "$APP" --purge
if [[ ${QL_DRY_RUN:-0} == 1 ]]; then
  ql_info "[dry-run] --purge would also remove $HOME/.config/$APP"
else
  rm -rf -- "$HOME/.config/$APP"
  ql_info "removed $HOME/.config/$APP (a copy of the settings is in the final backup)"
fi
