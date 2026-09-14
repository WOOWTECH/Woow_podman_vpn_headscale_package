#!/usr/bin/env bash
# tests/smoke.sh: post-install checks for the Woow Headscale stack, run on the host where it is
# installed (install.sh, upgrade.sh and restore.sh call it too). It only reads: no node, user,
# pre-auth key or API key is created or deleted.
#
#   tests/smoke.sh [--quick]
#
#   --quick   units, health, published ports and HTTP only
#
# Both secrets are read with `podman secret inspect --showsecret` into variables and compared
# in-process; nothing secret is printed, and no secret reaches an argument list.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=../scripts/common.sh
. "$REPO/scripts/common.sh"
# shellcheck source=../scripts/headscale-helpers.sh
. "$REPO/scripts/headscale-helpers.sh"
export QL_LOG_PREFIX=smoke

quick=0
while (($#)); do
  case $1 in
    --quick) quick=1 ;;
    -h | --help) sed -n '2,11p' "$0"; exit 0 ;;
    *) ql_die "unknown option $1 (see --help)" ;;
  esac
  shift
done

npass=0 nfail=0 nwarn=0
pass() { printf 'PASS %s\n' "$*"; npass=$((npass + 1)); }
fail() { printf 'FAIL %s\n' "$*"; nfail=$((nfail + 1)); }
warn() { printf 'WARN %s\n' "$*"; nwarn=$((nwarn + 1)); }

[[ -f $ENV_FILE ]] || ql_die "$ENV_FILE not found; is the stack installed?"
app_env_load

# A1 units
for u in "${UNITS[@]}"; do
  if systemctl --user is-active --quiet "$u"; then pass "A1 $u is active"; else fail "A1 $u is not active"; fi
done

# A2 health. Only headscale carries a HealthCmd (we build that image), and it is judged by its
# HTTP /health endpoint with an active `podman healthcheck run` as corroboration - never by the
# recorded .State.Health.Status, whose timer does not fire on every host (see hs_wait_ready).
# Headplane has no HealthCmd and counts as ready once podman has seen it stay up, which is what
# ql_wait_container_healthy falls back to.
if hs_wait_ready 180 headscale.service; then pass "A2 headscale is up"; else fail "A2 headscale is not up"; fi
if ql_wait_container_healthy headplane 180 2>/dev/null; then pass "A2 headplane is up"; else fail "A2 headplane is not up"; fi

# A3 published ports: exactly the three the settings ask for, on exactly those addresses
check_port() { # check_port <container> <container port> <bind key prefix>
  local want got bind port
  bind=$(ql_env_get "WOOW_${3}_BIND")
  port=$(ql_env_get "WOOW_${3}_PORT")
  [[ $bind == all ]] && bind=0.0.0.0
  want="$2/tcp -> $bind:$port"
  got=$(podman port "$1" 2>/dev/null | grep -F "$2/tcp" || true)
  if [[ $got == "$want" ]]; then pass "A3 $1 publishes $want"; else fail "A3 $1 publishes '${got:-nothing}', want '$want'"; fi
}
check_port headscale 8080 HEADSCALE
check_port headscale 9090 HEADSCALE_METRICS
check_port headplane 3000 HEADPLANE

# A4 HTTP
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$(hs_url HEADSCALE /health)" || true)
if [[ $code == 200 ]]; then pass "A4 headscale /health returns 200"; else fail "A4 headscale /health returned $code"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$(hs_url HEADPLANE /admin)" || true)
if [[ $code =~ ^(200|30[12378])$ ]]; then pass "A4 headplane /admin returns $code"; else fail "A4 headplane /admin returned $code"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$(hs_url HEADSCALE_METRICS /metrics)" || true)
if [[ $code == 200 ]]; then pass "A4 metrics return 200"; else fail "A4 metrics returned $code"; fi

if ((quick)); then
  printf '%s passed, %s failed, %s warnings (quick)\n' "$npass" "$nfail" "$nwarn"
  ((nfail == 0))
  exit
fi

# A5 the control plane answers its own CLI over the unix socket in headscale-run
if hs_cli nodes list --output json >/dev/null 2>&1; then
  pass "A5 the headscale CLI reaches the server over its unix socket"
else
  fail "A5 the headscale CLI cannot reach the server (the headscale-run volume?)"
fi

# A6 the API key Headplane uses is a live key of this headscale
if hs_api_key_valid; then pass "A6 the stored Headplane API key is valid"; else fail "A6 the stored Headplane API key is missing, revoked or expired"; fi

# A7 secret hygiene: podman inspect, process arguments, the journal, container logs, tracked files
cookie=$(app_secret_read "$SECRET_COOKIE" || true)
api_key=$(app_secret_read "$SECRET_API_KEY" || true)
[[ -n $cookie && -n $api_key ]] || fail "A7 secrets $SECRET_COOKIE / $SECRET_API_KEY are missing"
leaks=()
contains() { [[ -n $cookie && $1 == *"$cookie"* ]] || [[ -n $api_key && $1 == *"$api_key"* ]]; }
contains "$(podman inspect headscale headplane 2>/dev/null || true)" && leaks+=("podman inspect")
contains "$(podman top headscale args 2>/dev/null; podman top headplane args 2>/dev/null)" && leaks+=("process arguments")
contains "$(journalctl --user -u headscale.service -u headplane.service -o cat --no-pager 2>/dev/null || true)" && leaks+=("journal")
contains "$(podman logs --tail 2000 headscale 2>&1; podman logs --tail 2000 headplane 2>&1)" && leaks+=("container logs")
while IFS= read -r -d '' f; do
  [[ -f $REPO/$f ]] || continue
  if contains "$(<"$REPO/$f")"; then leaks+=("tracked file $f"); fi
done < <(git -C "$REPO" ls-files -z 2>/dev/null || true)
if ((${#leaks[@]} == 0)); then pass "A7 no secret in inspect, argv, journal, logs or tracked files"; else fail "A7 a secret appears in: ${leaks[*]}"; fi
unset cookie api_key

# A8 the secrets reach Headplane as 0400 files, and the configuration is read-only
for p in /etc/headplane/cookie-secret /etc/headplane/api-key; do
  perm=$(podman exec headplane stat -c '%a' "$p" 2>/dev/null || true)
  if [[ $perm == 400 ]]; then pass "A8 $p is 0400 inside headplane"; else warn "A8 $p reports mode '${perm:-unknown}' (no stat in this image?)"; fi
done
rw=$(podman inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/headscale"}}{{.RW}}{{end}}{{end}}' headscale 2>/dev/null || true)
if [[ $rw == false ]]; then pass "A8 /etc/headscale is mounted read-only"; else fail "A8 /etc/headscale mount RW=${rw:-missing}"; fi
rw=$(podman inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/headplane/config.yaml"}}{{.RW}}{{end}}{{end}}' headplane 2>/dev/null || true)
if [[ $rw == false ]]; then pass "A8 /etc/headplane/config.yaml is mounted read-only"; else fail "A8 /etc/headplane/config.yaml mount RW=${rw:-missing}"; fi

# A9 server_url is what a client would actually be told, and it is not the loopback default on a
# host that publishes anywhere else
url=$(ql_env_get HEADSCALE_SERVER_URL)
got=$(podman exec headscale sh -c 'sed -n "s/^server_url:[[:space:]]*//p" /etc/headscale/config.yaml' 2>/dev/null || true)
if [[ $got == "$url" ]]; then pass "A9 the running config carries HEADSCALE_SERVER_URL"; else fail "A9 the running config says '${got:-nothing}', the settings say '$url'"; fi
if [[ $url == *localhost* || $url == *127.0.0.1* ]]; then
  warn "A9 HEADSCALE_SERVER_URL is a loopback URL: only clients on this host can register"
fi

# A10 the image is the pinned one
got=$(podman inspect --format '{{.ImageName}}' headscale 2>/dev/null || true)
if [[ $got == "$HEADSCALE_IMAGE" ]]; then pass "A10 headscale runs $HEADSCALE_IMAGE"; else fail "A10 headscale runs '${got:-nothing}', want $HEADSCALE_IMAGE"; fi
got=$(podman inspect --format '{{.Image}}' headplane 2>/dev/null || true)
want=$(podman image inspect --format '{{.Id}}' "$HEADPLANE_IMAGE" 2>/dev/null || true)
if [[ -n $want && $got == "$want" ]]; then pass "A10 headplane runs the pinned digest"; else fail "A10 headplane does not run the pinned digest"; fi

printf '%s passed, %s failed, %s warnings\n' "$npass" "$nfail" "$nwarn"
((nfail == 0))
