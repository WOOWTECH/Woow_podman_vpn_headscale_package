# shellcheck shell=bash disable=SC2034 # the settings are read by the scripts that source this file
# scripts/common.sh: settings and helpers shared by this repo's scripts. Sourced after
# scripts/lib/quadlet-lib.sh (which is vendored and must not be edited). This file is repo-owned;
# the helpers below the settings block are the same in Woow_podman_emqx, Woow_podman_hermes,
# Woow_podman_odoo and Woow_podman_opendesign.

# ---- per-repo settings ------------------------------------------------------------------------
APP=headscale
PODMAN_MIN=4.9
ENV_EXAMPLE=$REPO/config/$APP.env.example
ENV_FILE=$HOME/.config/$APP/$APP.env
BACKUP_ROOT=$HOME/.local/share/woow-backups/$APP
# container:unit pairs the units create (legacy-collision guard)
CONTAINERS=("headscale:headscale.service" "headplane:headplane.service")
UNITS=(headscale.service headplane.service)
# every volume the units declare; BACKUP_VOLUMES holds the ones worth an archive
# (headscale-run only carries the gRPC unix socket)
VOLUMES=(headscale-data headscale-run headplane-data)
BACKUP_VOLUMES=(headscale-data headplane-data)
# podman secrets; both are Headplane's, and neither is ever written into this repo
SECRET_COOKIE=$APP-headplane-cookie
SECRET_API_KEY=$APP-headplane-api-key
# env keys allowed to carry user-supplied credentials (ERE on the whole key; empty = none)
ENV_CREDENTIAL_ALLOW=''
# images. tests/lint-repo.sh checks these against quadlet/ and Containerfile.
HEADSCALE_IMAGE=localhost/woow-headscale:0.29.3-r1
HEADSCALE_SOURCE_IMAGE=docker.io/headscale/headscale:v0.29.3@sha256:0e7f1c6e4ce6c2a2a001103ecd3fa645a045adf30ac8a5234fe037b43000cd72
HEADPLANE_IMAGE=ghcr.io/tale/headplane:0.7.0@sha256:7bd6523a14567a43eb4ffa1e95e3d95456b8539c9d081757df3f228b9e836fb5
# the compose-era user units (see the compose-final tag). None of them may be active when the
# Quadlet units take over the container names headscale and headplane.
LEGACY_UNITS=(woow_headscale.service woow_headscale_health.timer woow_headscale_health.service)
# ------------------------------------------------------------------------------------------------
export QL_APP=$APP

app_state_dir() { printf '%s/%s' "${QL_STATE_ROOT:-$HOME/.local/state/woow-quadlet}" "$APP"; }

# app_env_keys <file>: the KEY names a KEY=VALUE file defines
app_env_keys() { sed -nE 's/^([A-Za-z_][A-Za-z0-9_]*)=.*/\1/p' "$1"; }

# app_is_credential_key <KEY>: names whose values are credentials
app_is_credential_key() { [[ $1 =~ (PASSWORD|PASSWD|SECRET|TOKEN|_KEY)$ ]]; }

# app_apply_sets KEY=VALUE...: store per-host settings (install.sh --set) in the env file. Only keys
# the example defines are accepted, and never credentials (they would end up in argv and history).
app_apply_sets() {
  local kv k
  for kv in "$@"; do
    [[ $kv == *=* ]] || ql_die "--set $kv: expected KEY=VALUE"
    k=${kv%%=*}
    [[ $k =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || ql_die "--set $kv: invalid key"
    app_env_keys "$ENV_EXAMPLE" | grep -qxF -- "$k" || ql_die "--set $k: not a setting in ${ENV_EXAMPLE##*/}"
    if app_is_credential_key "$k"; then
      ql_die "--set $k: put credentials into $ENV_FILE with an editor, not on the command line"
    fi
    if [[ ! -f $ENV_FILE && ${QL_DRY_RUN:-0} == 1 ]]; then
      ql_info "[dry-run] would set $k in $ENV_FILE"
      continue
    fi
    ql_env_set "$ENV_FILE" "$k" "${kv#*=}"
    ql_info "set $k in $ENV_FILE"
  done
}

# app_env_load: load the env file (the example when --dry-run runs before the file exists) into
# QL_ENV. Sets RENDER_ENV to the file that was loaded.
app_env_load() {
  if [[ -f $ENV_FILE ]]; then
    ql_env_load "$ENV_FILE"
    RENDER_ENV=$ENV_FILE
  else
    QL_ENV_MODE_CHECK=0 ql_env_load "$ENV_EXAMPLE"
    RENDER_ENV=$ENV_EXAMPLE
  fi
}

# app_env_overlay KEY=VALUE...: apply --set values to QL_ENV as well, so a --dry-run (which does not
# write the env file) renders what a real run would.
app_env_overlay() {
  local kv
  for kv in "$@"; do QL_ENV[${kv%%=*}]=${kv#*=}; done
}

# app_refuse_env_secrets: generated credentials live in podman secrets, never in the env file.
app_refuse_env_secrets() {
  local k
  for k in "${QL_ENV_KEYS[@]}"; do
    app_is_credential_key "$k" || continue
    [[ -n ${QL_ENV[$k]} ]] || continue
    if [[ -n $ENV_CREDENTIAL_ALLOW && $k =~ ^($ENV_CREDENTIAL_ALLOW)$ ]]; then continue; fi
    ql_die "$ENV_FILE sets $k. Generated credentials live in podman secrets (see README, Secrets); remove the line and run again"
  done
}

# app_secret_read <name>: print a podman secret's value on stdout. Capture it; never echo it.
app_secret_read() { podman secret inspect --showsecret --format '{{.SecretData}}' "$1" 2>/dev/null; }

# app_guard_containers: a same-named container that Quadlet does not manage would be deleted by
# `podman run --replace`; refuse, and print the rename command.
app_guard_containers() {
  local c
  for c in "${CONTAINERS[@]}"; do ql_check_container_collision "${c%%:*}" "${c#*:}"; done
}

# app_guard_legacy_units: the compose-era units must be stopped before the Quadlet ones take the
# container names. They are not on any live host (see README, "Why there is no migrate-legacy.sh"),
# so this is a guard, not a migration.
app_guard_legacy_units() {
  local u
  for u in "${LEGACY_UNITS[@]}"; do
    if systemctl --user is-active --quiet "$u" 2>/dev/null; then
      ql_die "the compose-era unit $u is active on this host. Stop and disable it first:
  systemctl --user disable --now ${LEGACY_UNITS[*]}
Its containers and volumes are left alone; see README, 'Coming from the compose deployment'"
    fi
  done
}

# app_wait_healthy <container> <timeout_s> <unit>: health gate that points at the logs on failure
app_wait_healthy() {
  ql_wait_container_healthy "$1" "$2" || ql_die "$1 did not become healthy; see: journalctl --user -u $3 -n 100"
}

# app_local_host <bind>: the address this host uses to reach a port published on <bind>
app_local_host() {
  case $1 in all | 0.0.0.0 | '') printf '127.0.0.1' ;; *) printf '%s' "$1" ;; esac
}

# app_confirm <word> <yes 0|1> <what>: require typing <word> unless --yes (or --dry-run)
app_confirm() {
  local word=$1 yes=$2 what=$3 answer
  ((yes)) && return 0
  [[ ${QL_DRY_RUN:-0} == 1 ]] && return 0
  [[ -t 0 ]] || ql_die "$what; add --yes to confirm non-interactively"
  read -r -p "Type '$word' to confirm ($what): " answer
  [[ $answer == "$word" ]] || ql_die "aborted; nothing was changed"
}

# app_new_backup_dir <label>: create a private directory under $BACKUP_ROOT and print its path
app_new_backup_dir() {
  local d i=2
  (umask 077 && mkdir -p -- "$BACKUP_ROOT") || ql_die "cannot create $BACKUP_ROOT"
  chmod 700 "$BACKUP_ROOT"
  d=$BACKUP_ROOT/$1-$(date +%Y%m%d-%H%M%S)
  while [[ -e $d ]]; do d=$BACKUP_ROOT/$1-$(date +%Y%m%d-%H%M%S)-$i; i=$((i + 1)); done
  (umask 077 && mkdir -- "$d") || ql_die "cannot create $d"
  printf '%s' "$d"
}

# app_checksums <dir>: SHA256SUMS for every file in <dir> (relative paths)
app_checksums() {
  (cd -- "$1" && umask 077 && find . -type f ! -name 'SHA256SUMS*' -printf '%P\0' | LC_ALL=C sort -z \
    | xargs -0 -r sha256sum -- >SHA256SUMS.partial && mv -f SHA256SUMS.partial SHA256SUMS) \
    || ql_die "cannot write checksums in $1"
}

# app_snapshot <dir>: save every installed file of the app plus its manifest (upgrade rollback)
app_snapshot() {
  local dir=$1 mf
  mf="$(app_state_dir)/manifest"
  (umask 077 && mkdir -p -- "$dir") || ql_die "cannot create $dir"
  if [[ ! -f $mf ]]; then
    ql_warn "$APP has no installed files recorded; nothing to snapshot"
    return 0
  fi
  cp -p -- "$mf" "$dir/manifest"
  awk '{ sub(/^[^ ]+  /, ""); print }' "$mf" >"$dir/files.list"
  (umask 077 && tar -cPf "$dir/files.tar" -T "$dir/files.list") || ql_die "cannot snapshot the installed files"
  ql_info "saved the installed units in $dir"
}

# app_snapshot_restore <dir>: put the snapshot back (files and manifest), remove files that a failed
# run added, daemon-reload. Returns 1 when there is nothing to restore.
app_snapshot_restore() {
  local dir=$1 mf p
  mf="$(app_state_dir)/manifest"
  [[ -f $dir/manifest && -f $dir/files.tar ]] || { ql_warn "no snapshot in $dir"; return 1; }
  if [[ -f $mf ]]; then
    while read -r _ p; do
      [[ -n $p ]] || continue
      grep -qxF -- "$p" "$dir/files.list" || rm -f -- "$p"
    done <"$mf"
  fi
  tar -xPf "$dir/files.tar" || return 1
  cp -p -- "$dir/manifest" "$mf" || return 1
  rm -f -- "$(app_state_dir)/pending-restart"
  systemctl --user daemon-reload
}
