#!/usr/bin/env bash
# scripts/backup.sh: cold backup of the Woow Headscale stack. Both containers are stopped for
# the capture, so the SQLite database (which runs with a write-ahead log) and Headplane's state
# are one consistent point in time; whatever was running is started again afterwards.
#
#   scripts/backup.sh [--include-secrets]
#
#   --include-secrets  also store Headplane's cookie secret and API key in the archive (0600
#                      inside a 0700 directory). Needed to restore onto a host that has none.
#
# The archive path is printed on stdout:
#   ~/.local/share/woow-backups/headscale/headscale-<UTC stamp>.tar
#     volumes/headscale-data-*.tar  volumes/headplane-data-*.tar  config/...  metadata.json
#     SHA256SUMS  [secrets/...]
# headscale-run is not in it: that volume only carries the gRPC unix socket.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
umask 077
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=common.sh
. "$REPO/scripts/common.sh"

include_secrets=0
while (($#)); do
  case $1 in
    --include-secrets) include_secrets=1 ;;
    -h | --help) sed -n '2,15p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
ql_require_rootless
app_lock

was_running=()
for u in "${UNITS[@]}"; do
  systemctl --user is-active --quiet "$u" 2>/dev/null && was_running+=("$u")
done
staging=$(mktemp -d "${TMPDIR:-/tmp}/$APP-backup.XXXXXX")
restarted=0
cleanup() {
  local status=$?
  trap - EXIT
  rm -rf -- "$staging"
  if ((restarted == 0)) && ((${#was_running[@]})); then
    systemctl --user start "${was_running[@]}" >/dev/null 2>&1 || ql_warn "could not start ${was_running[*]} again"
  fi
  exit "$status"
}
trap cleanup EXIT

# Stop in dependency order: Headplane holds an API session against Headscale.
if ((${#was_running[@]})); then
  systemctl --user stop headplane.service headscale.service
  ql_info "stopped ${was_running[*]} for a consistent capture"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
root=$staging/$APP-backup-$stamp
mkdir -p "$root/volumes" "$root/config"
for v in "${BACKUP_VOLUMES[@]}"; do
  if podman volume exists "$v"; then
    ql_backup_volume "$v" "$root/volumes" >/dev/null
  else
    ql_warn "volume $v does not exist; the archive will not contain it"
  fi
done

# The rendered container configuration and the settings file. None of them holds a credential.
cfg=$HOME/.config/$APP
if [[ -d $cfg ]]; then
  (cd -- "$cfg" && tar -cf - .) | (cd -- "$root/config" && tar -xf -) || ql_die "cannot copy $cfg"
  find "$root/config" -type d -exec chmod 700 {} +
  find "$root/config" -type f -exec chmod 600 {} +
fi

if ((include_secrets)); then
  mkdir -m 700 "$root/secrets"
  for s in "$SECRET_COOKIE" "$SECRET_API_KEY"; do
    if podman secret exists "$s"; then
      (umask 077 && app_secret_read "$s" >"$root/secrets/${s#"$APP"-}") || ql_die "cannot read secret $s"
    else
      ql_warn "secret $s does not exist; it is not in the archive"
    fi
  done
  ql_warn "the archive contains Headplane's cookie secret and API key (--include-secrets): keep it as private as a password"
fi

secrets_json=false
((include_secrets == 0)) || secrets_json=true
printf '{"project":"%s","format":"woow-quadlet-headscale-v1","created_utc":"%s","volumes":[%s],"images":{"headscale":"%s","headplane":"%s"},"secrets_included":%s}\n' \
  "$APP" "$stamp" '"headscale-data","headplane-data"' "$HEADSCALE_IMAGE" "$HEADPLANE_IMAGE" "$secrets_json" \
  >"$root/metadata.json"
app_checksums "$root"

(umask 077 && mkdir -p -- "$BACKUP_ROOT") || ql_die "cannot create $BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
archive=$BACKUP_ROOT/$APP-$stamp.tar
[[ ! -e $archive ]] || archive=$BACKUP_ROOT/$APP-$stamp-$$.tar
tmp=$staging/publish.tar
tar -C "$staging" -cf "$tmp" "$APP-backup-$stamp"
chmod 600 "$tmp"
mv "$tmp" "$archive"
(cd -- "$BACKUP_ROOT" && sha256sum -- "${archive##*/}" >"${archive##*/}.sha256")
chmod 600 "$archive.sha256"

if ((${#was_running[@]})); then
  systemctl --user start "${was_running[@]}"
  restarted=1
  app_wait_healthy headscale 300 headscale.service
fi
ql_info "backup complete: $archive ($(du -h -- "$archive" | cut -f1))"
printf '%s\n' "$archive"
