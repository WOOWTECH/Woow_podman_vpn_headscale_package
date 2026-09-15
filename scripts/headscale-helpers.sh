# shellcheck shell=bash
# scripts/headscale-helpers.sh: the parts of the install and smoke flow that are specific to
# Headscale and Headplane. Sourced after scripts/lib/quadlet-lib.sh and scripts/common.sh.

# hs_cli <args...>: run the headscale CLI inside the running container. It talks to the gRPC
# unix socket in the headscale-run volume, so this is the real server, not a second process.
hs_cli() { podman exec headscale headscale "$@"; }

# hs_render_configs <envfile> <out_dir>: render config/templates/** into <out_dir>/config/**,
# which ql_install_files then routes to ~/.config/headscale/. Both container configuration
# files are rendered here, from the same env file and the same whitelist as the units.
hs_render_configs() {
  local envf=$1 out=$2 vars=$REPO/config/templates/render-vars
  ql_render "$REPO/config/templates/headscale" "$envf" "$vars" "$out/config/headscale"
  ql_render "$REPO/config/templates/headplane" "$envf" "$vars" "$out/config/headplane"
}

# hs_units_for_changed <changed file list>: the units that have to restart because one of the
# rendered configuration files under ~/.config/headscale changed. ql_install_files deliberately
# does not do this itself: a config file is not a unit, so Quadlet cannot see that it moved.
hs_units_for_changed() {
  local line
  local -A want=()
  while IFS= read -r line; do
    case $line in
      config/headscale/*) want[headscale.service]=1 ;;
      config/headplane/*) want[headplane.service]=1 ;;
    esac
  done <<<"$1"
  if ((${#want[@]})); then printf '%s\n' "${!want[@]}"; fi
}

# hs_user_exists <name>: true when the headscale user exists. `users list --output json` has
# been a bare list, a {"users": [...]} object and literal null across releases.
hs_user_exists() {
  local json
  json=$(hs_cli users list --output json 2>/dev/null) || return 2
  printf '%s' "$json" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except ValueError:
    raise SystemExit(2)
if value is None:
    value = []
elif isinstance(value, dict):
    value = value.get("users", value.get("items", []))
if not isinstance(value, list):
    raise SystemExit(2)
want = sys.argv[1]
raise SystemExit(0 if any(isinstance(u, dict) and u.get("name") == want for u in value) else 1)
' "$1"
}

# hs_user_id <name>: that user's NUMERIC id on stdout, empty and status 1 when it is not there,
# status 2 when the list cannot be read.
#
# headscale 0.29's `preauthkeys create -u/--user` takes a `uint` - a user ID, not a name. Passing
# the name fails with `invalid argument "default" for "-u, --user" flag: strconv.ParseUint`, so
# every instruction that spells a name there is a command nobody can run. The id is not always 1
# either: it is whatever headscale assigned, so it has to be looked up rather than assumed.
hs_user_id() {
  local json
  json=$(hs_cli users list --output json 2>/dev/null) || return 2
  printf '%s' "$json" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except ValueError:
    raise SystemExit(2)
if value is None:
    value = []
elif isinstance(value, dict):
    value = value.get("users", value.get("items", []))
if not isinstance(value, list):
    raise SystemExit(2)
want = sys.argv[1]
for u in value:
    if isinstance(u, dict) and u.get("name") == want:
        uid = u.get("id")
        if uid is None:
            raise SystemExit(2)
        print(uid)
        raise SystemExit(0)
raise SystemExit(1)
' "$1"
}

# hs_ensure_default_user: create the headscale user "default" when the env file asks for it.
# The autoApprovers in config/templates/headscale/policy.json name this user.
hs_ensure_default_user() {
  local rc=0
  [[ $(ql_env_get HEADSCALE_CREATE_DEFAULT_USER false) == true ]] || return 0
  hs_user_exists default || rc=$?
  case $rc in
    0) ql_info "headscale user 'default' exists" ;;
    1)
      hs_cli users create default >/dev/null || ql_die "could not create the headscale user 'default'"
      ql_info "created the headscale user 'default'"
      ;;
    *) ql_die "headscale returned a user list this script cannot read; check: podman exec headscale headscale users list" ;;
  esac
}

# hs_api_key_valid: true when the stored podman secret is a live, unexpired API key of THIS
# headscale. The key reaches python through a pipe, so it is in no argument list and no log.
hs_api_key_valid() {
  local list rc=0
  podman secret exists "$SECRET_API_KEY" >/dev/null 2>&1 || return 1
  list=$(mktemp "${TMPDIR:-/tmp}/$APP-apikeys.XXXXXX") || return 1
  chmod 600 "$list"
  if hs_cli apikeys list --output json >"$list" 2>/dev/null; then
    app_secret_read "$SECRET_API_KEY" | python3 "$REPO/scripts/validate-api-key.py" --list-json "$list" || rc=$?
  else
    rc=1
  fi
  rm -f -- "$list"
  return "$rc"
}

# hs_ensure_api_key [--rotate]: make sure $SECRET_API_KEY holds a usable Headplane API key,
# creating one through the headscale CLI when it does not. Sets HS_API_KEY_CHANGED=1 when the
# secret was written, so the caller can restart Headplane. The key is captured into a shell
# variable and handed to ql_secret_ensure by variable name; it never becomes an argument.
hs_ensure_api_key() {
  local rotate=0 out
  [[ ${1:-} == --rotate ]] && rotate=1
  HS_API_KEY_CHANGED=0
  if ((rotate == 0)) && hs_api_key_valid; then
    ql_info "the stored Headplane API key is valid"
    return 0
  fi
  if podman secret exists "$SECRET_API_KEY" >/dev/null 2>&1; then
    ((rotate)) || ql_warn "the stored Headplane API key is missing, revoked or expired: creating a new one"
  fi
  out=$(hs_cli apikeys create --expiration 3650d 2>/dev/null) || ql_die "could not create a Headscale API key"
  local HS_NEW_API_KEY
  HS_NEW_API_KEY=$(printf '%s' "$out" | sed -e 's/[[:space:]]*$//' -e '/^$/d' | tail -n1)
  out=''
  [[ $HS_NEW_API_KEY =~ ^hskey-api-[A-Za-z0-9_-]{12}-[A-Za-z0-9_-]{64}$ ]] \
    || ql_die "headscale did not return a key in the expected format; nothing was stored"
  ql_secret_ensure "$SECRET_API_KEY" env:HS_NEW_API_KEY --replace
  HS_NEW_API_KEY=''
  # shellcheck disable=SC2034 # read by scripts/install.sh to restart headplane.service
  HS_API_KEY_CHANGED=1
}

# hs_url <bind key prefix> <path>: the URL this host uses to reach one of the published ports
hs_url() {
  local host port
  host=$(app_local_host "$(ql_env_get "WOOW_${1}_BIND")")
  port=$(ql_env_get "WOOW_${1}_PORT")
  printf 'http://%s:%s%s' "$host" "$port" "${2:-}"
}

# hs_wait_ready [timeout_s] [unit]: the readiness gate for the Headscale control plane.
#
# The HTTP /health endpoint is the AUTHORITY here, not the container's recorded health status.
# On some hosts (openclaw) podman's transient healthcheck timers never fire for a container
# Quadlet started: `.State.Health.Status` stays "starting" for ever even though
# `podman healthcheck run headscale` returns 0 and the server answers every request. A gate
# that waits PASSIVELY on that status burns its whole timeout and then reports a working
# control plane as dead - which is why nothing in this repository gates on it any more.
#
# The container healthcheck is still run, ACTIVELY and once, as corroboration: a mismatch is
# reported as a warning, never as a veto. A headscale that is genuinely down answers neither,
# so this returns 1 and the caller fails - see tests/health-gate.sh.
hs_wait_ready() {
  local timeout=${1:-300} unit=${2:-headscale.service} url
  [[ -n ${QL_ENV_FILE:-} ]] || app_env_load
  url=$(hs_url HEADSCALE /health)
  if ! ql_wait_http "$url" '200' "$timeout"; then
    ql_warn "headscale is not ready: $url did not answer 200 within ${timeout}s; see: journalctl --user -u $unit -n 100"
    return 1
  fi
  if podman healthcheck run headscale >/dev/null 2>&1; then
    ql_info "headscale is ready ($url -> 200, container healthcheck passes)"
  else
    ql_warn "headscale answers $url but 'podman healthcheck run headscale' does not pass; going by the HTTP probe"
  fi
  return 0
}
