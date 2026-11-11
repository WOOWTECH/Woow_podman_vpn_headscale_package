#!/usr/bin/env bash
# scripts/install.sh: install or update the Woow Headscale stack (Headscale control plane +
# Headplane admin UI) as rootless Quadlet units (podman >= 4.9, systemd --user, linger).
# Idempotent: a re-run with nothing changed builds nothing, restarts nothing and keeps both
# secrets. Run it as the account that owns the containers, never with sudo.
#
#   scripts/install.sh [options]
#
#   --set KEY=VALUE     store a per-host setting in ~/.config/headscale/headscale.env first
#                       (repeatable), e.g. --set WOOW_HEADPLANE_PORT=23001. Only keys of
#                       config/headscale.env.example, and never a credential.
#   --rebuild           build localhost/woow-headscale again even if the tag exists
#   --no-build          never build; the pinned image must already exist
#   --build-only        build the image and stop: no config, no units, no restart
#   --rotate-api-key    issue a new Headplane API key even if the stored one still works
#   --accept-defaults   on the first run, continue with the example settings instead of stopping
#   --no-start          install the files and daemon-reload only
#   --no-smoke          skip tests/smoke.sh at the end
#   --dry-run           render and validate, report what would change, touch nothing
#
# Order: preflight -> env -> guards -> render -> dry-run -> build/pull -> cookie secret ->
# install -> start Headscale -> default user -> API key -> start Headplane -> smoke.
# Headscale has to be running before its API can mint the key Headplane needs, which is the
# only reason the two containers are started in two steps rather than one.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=common.sh
. "$REPO/scripts/common.sh"
# shellcheck source=headscale-helpers.sh
. "$REPO/scripts/headscale-helpers.sh"

sets=() build=auto build_only=0 rotate=0 accept=0 no_start=0 no_smoke=0
while (($#)); do
  case $1 in
    --set) (($# >= 2)) || ql_die "--set needs KEY=VALUE"; sets+=("$2"); shift ;;
    --set=*) sets+=("${1#--set=}") ;;
    --rebuild) build=always ;;
    --no-build) build=never ;;
    --build-only) build_only=1 ;;
    --rotate-api-key) rotate=1 ;;
    --accept-defaults) accept=1 ;;
    --no-start) no_start=1 ;;
    --no-smoke) no_smoke=1 ;;
    --dry-run) export QL_DRY_RUN=1 ;;
    -h | --help) sed -n '2,22p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done
dry() { [[ ${QL_DRY_RUN:-0} == 1 ]]; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/$APP-install.XXXXXX")
# a hook, not `trap ... EXIT`, which would replace the handler ql_lock arms
ql_cleanup work rm -rf "$WORK"

# ensure_image: build localhost/woow-headscale from ./Containerfile. The build context holds
# the Containerfile and nothing else, so no file of this checkout can end up in an image layer
# or in a build log.
ensure_image() {
  if [[ $build == never ]]; then
    podman image exists "$HEADSCALE_IMAGE" \
      || ql_die "$HEADSCALE_IMAGE does not exist and --no-build was given (run scripts/install.sh --build-only)"
    return 0
  fi
  if [[ $build == auto ]] && podman image exists "$HEADSCALE_IMAGE"; then
    ql_info "image $HEADSCALE_IMAGE is present (use --rebuild to build it again)"
    return 0
  fi
  if dry; then ql_info "[dry-run] would build $HEADSCALE_IMAGE from Containerfile"; return 0; fi
  ql_info "building $HEADSCALE_IMAGE (both bases are pinned by digest in Containerfile)"
  mkdir -p "$WORK/build-context"
  install -m 644 "$REPO/Containerfile" "$WORK/build-context/Containerfile"
  podman build -t "$HEADSCALE_IMAGE" -f "$WORK/build-context/Containerfile" "$WORK/build-context" \
    || ql_die "building $HEADSCALE_IMAGE failed; nothing was changed"
}

ensure_headplane_image() {
  if podman image exists "$HEADPLANE_IMAGE"; then
    if [[ $build != always ]]; then return 0; fi
  elif [[ $build == never ]]; then
    ql_die "$HEADPLANE_IMAGE does not exist and --no-build was given (run scripts/build-headplane.sh)"
  fi
  if dry; then
    ql_info "[dry-run] would build $HEADPLANE_IMAGE from pinned Headplane 0.7.1 source"
    return 0
  fi
  "$REPO/scripts/build-headplane.sh" "$HEADPLANE_IMAGE"
}

# ---- --build-only: the image and nothing else ---------------------------------------------------
if ((build_only)); then
  ql_require_rootless
  ql_require_podman_min "$PODMAN_MIN"
  ensure_image
  ensure_headplane_image
  ql_info "image ready: $HEADSCALE_IMAGE"
  exit 0
fi

# ---- 1. host preflight --------------------------------------------------------------------------
ql_preflight "$PODMAN_MIN"
ql_enable_linger
ql_lock "$APP"

# ---- 2. per-host settings -------------------------------------------------------------------
ql_env_ensure "$ENV_EXAMPLE" "$ENV_FILE"
if [[ $QL_ENV_CREATED == 1 && $accept == 0 && ${#sets[@]} == 0 ]]; then
  ql_info "review $ENV_FILE (at least HEADSCALE_SERVER_URL: it is the address Tailscale clients"
  ql_info "dial), then run $0 again -- or pass --accept-defaults to keep the example values."
  exit 0
fi
((${#sets[@]} == 0)) || app_apply_sets "${sets[@]}"
app_env_load
app_env_overlay "${sets[@]}"
app_refuse_env_secrets

# ---- 3. guards ----------------------------------------------------------------------------------
app_guard_legacy_units
app_guard_containers

# ---- 4. stage, render, validate -----------------------------------------------------------------
mkdir -p "$WORK/src" "$WORK/out"
cp -p "$REPO"/quadlet/*.container "$REPO"/quadlet/*.volume "$REPO"/quadlet/*.network "$WORK/src/"
if [[ $(ql_env_get HEADSCALE_NGROK_ENABLED false) == true ]]; then
  cp -p "$REPO/quadlet/optional/headscale-ngrok.container" "$WORK/src/"
  if ! dry; then
    podman secret exists "$SECRET_NGROK_TOKEN" \
      || ql_die "ngrok is enabled but the Podman secret $SECRET_NGROK_TOKEN is missing"
  fi
fi
RENDER_ARGS=()
# shellcheck source=render-args.sh
. "$REPO/scripts/render-args.sh"
render_args "$RENDER_ENV"
ql_render "$WORK/src" "$RENDER_ENV" "$REPO/quadlet/render-vars" "$WORK/out" "${RENDER_ARGS[@]}"
hs_render_configs "$RENDER_ENV" "$WORK/out"
install -m 755 "$REPO/scripts/ngrok-sync.py" "$WORK/out/config/ngrok-sync.py"
ql_dryrun "$WORK/out" --verify --ref-dir "$HOME/.config/containers/systemd" \
  || ql_die "the rendered units failed the Quadlet dry-run; nothing was installed"
for f in "$WORK/out"/*; do
  [[ -f $f ]] || continue
  u=$(ql_unit_for "$f")
  [[ -z $u ]] || ql_check_unit_shadow "$u" "$APP"
done

# ---- 5. images before any unit change -----------------------------------------------------------
ensure_image
ensure_headplane_image
if dry && { ! podman image exists "$HEADSCALE_IMAGE" || ! podman image exists "$HEADPLANE_IMAGE"; }; then
  ql_info "[dry-run] $HEADSCALE_IMAGE is not built yet; skipping the pull of the other image"
else
  ql_pull_images "$WORK/out"
fi

# ---- 6. private credentials ---------------------------------------------------------------------
# 32 characters is what Headplane 0.7.1 accepts. The API key needs a running Headscale and is
# created further down.
ql_secret_ensure "$SECRET_COOKIE" random:32
restart_headplane=0
[[ ${QL_SECRET_CHANGED:-0} == 1 ]] && restart_headplane=1
ql_secret_ensure "$SECRET_ENTRANCE_PASSWORD" random:32
restart_entrance=0
[[ ${QL_SECRET_CHANGED:-0} == 1 ]] && restart_entrance=1
# The high-entropy password is a Podman secret; only a deterministic apr1 hash is
# installed for nginx. A stable salt keeps an idempotent install from restarting it.
mkdir -p "$WORK/out/config/entrance"
if podman secret exists "$SECRET_ENTRANCE_PASSWORD" >/dev/null 2>&1; then
  entrance_hash=$(app_secret_read "$SECRET_ENTRANCE_PASSWORD" | openssl passwd -apr1 -salt woowhead -stdin)
else
  entrance_hash='dry-run-placeholder'
fi
printf 'admin:%s\n' "$entrance_hash" >"$WORK/out/config/entrance/htpasswd"
chmod 644 "$WORK/out/config/entrance/htpasswd"
unset entrance_hash

# ---- 7. install the files -----------------------------------------------------------------------
# A disabled optional tunnel must be stopped before its unit is pruned.
if ! dry && [[ $(ql_env_get HEADSCALE_NGROK_ENABLED false) != true ]]; then
  systemctl --user stop headscale-ngrok.service >/dev/null 2>&1 || true
fi
changed=$(ql_install_files "$WORK/out" "$APP" --prune)
[[ -z $changed ]] || ql_info "changed: $(tr '\n' ' ' <<<"$changed")"
# A rendered config file is not a unit: Quadlet cannot see that it changed, so say so here.
mapfile -t cfg_units < <(hs_units_for_changed "$changed")
if dry; then
  ((${#cfg_units[@]} == 0)) || ql_info "[dry-run] would restart ${cfg_units[*]} (rendered config changed)"
  ((restart_headplane == 0)) || ql_info "[dry-run] would restart headplane.service (cookie secret changed)"
  ((restart_entrance == 0)) || ql_info "[dry-run] would restart headscale-entrance.service (password changed)"
  ql_info "dry-run complete; nothing was changed"
  exit 0
fi
((${#cfg_units[@]} == 0)) || ql_mark_changed "$APP" "${cfg_units[@]}"
((restart_headplane == 0)) || ql_mark_changed "$APP" headplane.service
((restart_entrance == 0)) || ql_mark_changed "$APP" headscale-entrance.service
if ((no_start)); then
  systemctl --user daemon-reload
  ql_info "installed; not started (--no-start). Start with: systemctl --user start ${UNITS[*]}"
  exit 0
fi
systemctl --user daemon-reload
hs_ensure_quadlet_resources
hs_ensure_extra_records

# ---- 8. Resolve the actual ngrok URL before Headscale starts -------------------------------
if [[ $(ql_env_get HEADSCALE_NGROK_ENABLED false) == true ]]; then
  ql_apply_units "$APP" headscale-ngrok.service
  old_url=$(ql_env_get HEADSCALE_SERVER_URL)
  "$HOME/.config/headscale/ngrok-sync.py" --apply --timeout 90
  app_env_load
  if [[ $(ql_env_get HEADSCALE_SERVER_URL) != "$old_url" ]]; then
    hs_render_configs "$RENDER_ENV" "$WORK/out"
    ql_install_files "$WORK/out" "$APP" >/dev/null
    ql_mark_changed "$APP" headscale.service headplane.service
    ql_info "ngrok updated HEADSCALE_SERVER_URL from its live endpoint"
  fi
fi

# ---- 9. Headscale first ---------------------------------------------------------------------------
ql_apply_units "$APP" headscale.service
# One gate, and the HTTP probe is what it believes: on a host whose podman healthcheck timers
# never fire for a Quadlet-started container, waiting on .State.Health.Status hangs for the
# whole timeout and then calls a working control plane dead. See hs_wait_ready.
hs_wait_ready 300 headscale.service \
  || ql_die "headscale does not answer on $(hs_url HEADSCALE /health); see: journalctl --user -u headscale.service -n 100"

# ---- 10. the headscale-side objects Headplane needs ------------------------------------------------
hs_ensure_default_user
if ((rotate)); then hs_ensure_api_key --rotate; else hs_ensure_api_key; fi
((${HS_API_KEY_CHANGED:-0} == 0)) || ql_mark_changed "$APP" headplane.service

# ---- 11. Headplane ----------------------------------------------------------------------------
ql_apply_units "$APP" headplane.service
ql_apply_units "$APP" headscale-entrance.service
ql_wait_http "$(hs_url HEADPLANE /healthz-upstream)" '200' 180 \
  || ql_die "headplane does not answer through the entrance; see: journalctl --user -u headplane.service -u headscale-entrance.service -n 100"
# ---- 12. smoke ---------------------------------------------------------------------------------
if ((no_smoke == 0)); then
  "$REPO/tests/smoke.sh" || ql_die "tests/smoke.sh failed; see the FAIL lines above"
fi
# `preauthkeys create --user` takes the user's numeric id, not its name, so print the real one
# rather than a command that cannot run. Fall back to the lookup if the list cannot be read.
enrol_user=$(hs_user_id default 2>/dev/null) || enrol_user=''
if [[ -n $enrol_user ]]; then
  enrol_hint="podman exec headscale headscale preauthkeys create --user $enrol_user --reusable --expiration 24h"
else
  enrol_hint="podman exec headscale headscale users list   # then: preauthkeys create --user <ID> --reusable --expiration 24h"
fi
cat >&2 <<EOF
$APP is installed and healthy.
  Headscale     $(hs_url HEADSCALE)/   (server_url for clients: $(ql_env_get HEADSCALE_SERVER_URL))
  Headplane     $(hs_url HEADPLANE)/admin (HTTP basic auth: admin; password in Podman secret $SECRET_ENTRANCE_PASSWORD)
  Metrics       $(hs_url HEADSCALE_METRICS)/metrics
  Enrol a node  $enrol_hint
                then on the client: tailscale up --login-server $(ql_env_get HEADSCALE_SERVER_URL) --authkey <key>
                in the official tailscale container that flag goes in TS_EXTRA_ARGS, not TS_LOGIN_SERVER
  Settings      $ENV_FILE (edit, then run scripts/install.sh again)
  Backup        scripts/backup.sh    Upgrade  git pull && scripts/upgrade.sh
EOF
