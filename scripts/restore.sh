#!/usr/bin/env bash
# scripts/restore.sh: restore an archive made by scripts/backup.sh. Destructive: both data
# volumes are deleted and recreated from the archive. A pre-restore backup is taken while the
# stack is already stopped, and a failure after the first destructive step rolls that archive
# back before the stack is allowed to serve again.
#
#   scripts/restore.sh --archive FILE --confirm-restore headscale [options]
#
#   --restore-secrets  also put Headplane's cookie secret and API key back (needs an archive
#                      made with --include-secrets). Without it the current host keeps its own,
#                      and scripts/install.sh mints a new API key if the old one is gone.
#   --restore-config   also put ~/.config/headscale/headscale.env back. The rendered container
#                      configuration is always re-rendered from it by scripts/install.sh, so it
#                      is never copied out of the archive.
#
# The nodes in the restored database keep their keys, so every Tailscale client reconnects
# without re-registering - as long as HEADSCALE_SERVER_URL still points at this host.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
umask 077
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=common.sh
. "$REPO/scripts/common.sh"

archive='' confirm='' restore_secrets=0 restore_config=0
while (($#)); do
  case $1 in
    --archive) (($# >= 2)) || ql_die "--archive needs a path"; archive=$2; shift ;;
    --confirm-restore) (($# >= 2)) || ql_die "--confirm-restore needs the word $APP"; confirm=$2; shift ;;
    --restore-secrets) restore_secrets=1 ;;
    --restore-config) restore_config=1 ;;
    -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
[[ -n $archive && $confirm == "$APP" ]] || ql_die "usage: scripts/restore.sh --archive FILE --confirm-restore $APP"
archive=$(realpath -- "$archive")
[[ -f $archive ]] || ql_die "archive not found: $archive"
ql_require_rootless
ql_lock "$APP"
rollback_mode=${WOOW_RESTORE_ROLLBACK:-false}

stage_parent=$(mktemp -d "${TMPDIR:-/tmp}/$APP-restore.XXXXXX")
destructive=0 pre_restore='' stage=''
# run as a ql_cleanup hook, not from `trap ... EXIT`: a bare trap would replace the handler
# ql_lock armed, so the lock directory would outlive a clean restore. Inside a hook $? is the
# status the script is ending with, which is what decides whether to roll back.
cleanup() {
  local status=$?
  if ((status != 0)) && ((destructive)) && [[ $rollback_mode == false && -n $pre_restore ]]; then
    ql_warn "restore failed after the first destructive step; putting the pre-restore archive back"
    if WOOW_RESTORE_ROLLBACK=true "$REPO/scripts/restore.sh" --archive "$pre_restore" \
      --confirm-restore "$APP" --restore-secrets --restore-config; then
      ql_warn "the pre-restore state is back and verified"
    else
      ql_warn "the rollback failed too. Recover by hand with:"
      ql_warn "  scripts/restore.sh --archive $pre_restore --confirm-restore $APP --restore-secrets --restore-config"
    fi
  fi
  rm -rf -- "$stage_parent"
}
ql_cleanup restore_rollback cleanup

# Freeze the caller's pathname into a private copy, then work only from that one file.
staged=$stage_parent/source.tar
cp -- "$archive" "$staged"
chmod 400 "$staged"
mkdir -m 700 "$stage_parent/extracted"
tar -C "$stage_parent/extracted" -xf "$staged" || ql_die "the archive could not be unpacked; nothing was changed"
stage=$(find "$stage_parent/extracted" -mindepth 1 -maxdepth 1 -type d -name "$APP-backup-*" -print -quit)
[[ -n $stage && -f $stage/metadata.json && -f $stage/SHA256SUMS ]] \
  || ql_die "this is not a scripts/backup.sh archive of $APP; nothing was changed"
(cd -- "$stage" && LC_ALL=C sha256sum --quiet -c SHA256SUMS) \
  || ql_die "the archive does not match its own SHA256SUMS; nothing was changed"
grep -q '"format":"woow-quadlet-headscale-v1"' "$stage/metadata.json" \
  || ql_die "unknown archive format; nothing was changed"

declare -A vol_tar=()
for v in "${BACKUP_VOLUMES[@]}"; do
  f=$(find "$stage/volumes" -maxdepth 1 -type f -name "$v-*.tar" -print -quit 2>/dev/null || true)
  [[ -n $f ]] || ql_die "the archive has no export of volume $v; nothing was changed"
  vol_tar[$v]=$f
done
if ((restore_secrets)); then
  [[ -f $stage/secrets/${SECRET_COOKIE#"$APP"-} && -f $stage/secrets/${SECRET_API_KEY#"$APP"-} ]] \
    || ql_die "--restore-secrets needs an archive made with --include-secrets"
fi
if ((restore_config)); then
  [[ -f $stage/config/$APP.env ]] || ql_die "--restore-config needs an archive that holds $APP.env"
fi

# --confirm-restore is the confirmation; there is no second prompt, so the script also works
# from the rollback path in cleanup() and from an unattended run.
systemctl --user stop headplane.service headscale.service >/dev/null 2>&1 || true
if [[ $rollback_mode == false ]]; then
  pre_restore=$("$REPO/scripts/backup.sh" --include-secrets) || ql_die "the pre-restore backup failed; nothing was changed"
  ql_info "pre-restore archive: $pre_restore"
  systemctl --user stop headplane.service headscale.service >/dev/null 2>&1 || true
fi

# ---- from here on, every failure takes the rollback path in cleanup() ---------------------------
destructive=1
for v in "${BACKUP_VOLUMES[@]}"; do
  podman volume rm "$v" >/dev/null 2>&1 || true
  systemctl --user start "$v-volume.service" || ql_die "could not recreate the volume $v"
  podman volume import "$v" "${vol_tar[$v]}" || ql_die "could not import the archived data into $v"
  ql_info "restored volume $v"
done
if ((restore_secrets)); then
  ql_secret_ensure "$SECRET_COOKIE" "file:$stage/secrets/${SECRET_COOKIE#"$APP"-}" --replace
  ql_secret_ensure "$SECRET_API_KEY" "file:$stage/secrets/${SECRET_API_KEY#"$APP"-}" --replace
fi
if ((restore_config)); then
  install -m 600 -- "$stage/config/$APP.env" "$ENV_FILE"
  ql_info "restored $ENV_FILE"
fi

# install.sh re-renders every file from the settings and brings both units up in the right
# order; it also mints a new API key when the restored one is not usable on this host.
"$REPO/scripts/install.sh" --accept-defaults --no-build --no-smoke \
  || ql_die "the stack did not come back up after the restore"
"$REPO/tests/smoke.sh" --quick || ql_die "the restored stack failed the smoke test"

if [[ $rollback_mode == false ]]; then
  ql_info "restore complete; the pre-restore archive is kept: $pre_restore"
else
  ql_info "pre-restore state restored and verified"
fi
