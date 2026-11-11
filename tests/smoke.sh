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
if [[ $(ql_env_get HEADSCALE_NGROK_ENABLED false) == true ]]; then
  if systemctl --user is-active --quiet headscale-ngrok.service; then
    pass "A1 optional ngrok tunnel is active"
  else
    fail "A1 optional ngrok tunnel is not active"
  fi
fi

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
check_port headscale-entrance 3000 HEADPLANE
if [[ -z $(podman port headplane 2>/dev/null) ]]; then pass "A3 headplane has no direct host port"; else fail "A3 headplane must not publish a direct host port"; fi

# A4 HTTP
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$(hs_url HEADSCALE /health)" || true)
if [[ $code == 200 ]]; then pass "A4 headscale /health returns 200"; else fail "A4 headscale /health returned $code"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$(hs_url HEADPLANE /admin)" || true)
if [[ $code == 401 ]]; then pass "A4 unauthenticated /admin returns 401"; else fail "A4 unauthenticated /admin returned $code"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$(hs_url HEADPLANE /healthz-upstream)" || true)
if [[ $code == 200 ]]; then pass "A4 headplane upstream health returns 200"; else fail "A4 headplane upstream health returned $code"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$(hs_url HEADSCALE_METRICS /metrics)" || true)
if [[ $code == 200 ]]; then pass "A4 metrics return 200"; else fail "A4 metrics returned $code"; fi
code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -u admin:invalid "$(hs_url HEADPLANE /admin/)" || true)
if [[ $code == 401 ]]; then pass "A4 wrong entrance password is rejected"; else fail "A4 wrong entrance password returned $code"; fi
filter_code() {
  { podman exec headscale-entrance /bin/sh -c "wget -q -S -O /dev/null http://127.0.0.1:8081$1 2>&1" || true; } \
    | sed -nE 's/^[[:space:]]*HTTP\/1\.[01] ([0-9]{3}).*/\1/p' | head -n1
}
code=$(filter_code /health)
if [[ $code == 200 ]]; then pass "A4 private control filter passes /health"; else fail "A4 private control filter /health returned $code"; fi
code=$(filter_code /swagger)
if [[ $code == 404 ]]; then pass "A4 private control filter blocks /swagger"; else fail "A4 private control filter /swagger returned $code"; fi

if ((quick)); then
  printf '%s passed, %s failed, %s warnings (quick)\n' "$npass" "$nfail" "$nwarn"
  ((nfail == 0))
  exit
fi

# Feed the password on stdin, never in argv, environment or a curl command line.
code=$(app_secret_read "$SECRET_ENTRANCE_PASSWORD" | python3 -c '
import base64, sys, urllib.error, urllib.request
password = sys.stdin.read().strip()
url = sys.argv[1]
header = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
try:
    response = urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": header}), timeout=20)
    print(response.status if b"<html" in response.read(4096).lower() else 0)
except urllib.error.HTTPError as error:
    print(error.code)
' "$(hs_url HEADPLANE /admin/)")
if [[ $code == 200 ]]; then pass "A4 valid entrance password opens Headplane"; else fail "A4 valid entrance password returned $code"; fi

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
entrance_password=$(app_secret_read "$SECRET_ENTRANCE_PASSWORD" || true)
[[ -n $entrance_password ]] || fail "A7 entrance password secret is missing"
contains() { [[ -n $cookie && $1 == *"$cookie"* ]] || [[ -n $api_key && $1 == *"$api_key"* ]] || [[ -n $entrance_password && $1 == *"$entrance_password"* ]]; }
contains "$(podman inspect headscale headplane headscale-entrance 2>/dev/null || true)" && leaks+=("podman inspect")
contains "$(podman top headscale args 2>/dev/null; podman top headplane args 2>/dev/null; podman top headscale-entrance args 2>/dev/null)" && leaks+=("process arguments")
contains "$(journalctl --user -u headscale.service -u headplane.service -u headscale-entrance.service -o cat --no-pager 2>/dev/null || true)" && leaks+=("journal")
contains "$(podman logs --tail 2000 headscale 2>&1; podman logs --tail 2000 headplane 2>&1; podman logs --tail 2000 headscale-entrance 2>&1)" && leaks+=("container logs")
while IFS= read -r -d '' f; do
  [[ -f $REPO/$f ]] || continue
  if grep -aFq -- "$cookie" "$REPO/$f" || grep -aFq -- "$api_key" "$REPO/$f" || grep -aFq -- "$entrance_password" "$REPO/$f"; then
    leaks+=("tracked file $f")
  fi
done < <(git -C "$REPO" ls-files -z 2>/dev/null || true)
if ((${#leaks[@]} == 0)); then pass "A7 no secret in inspect, argv, journal, logs or tracked files"; else fail "A7 a secret appears in: ${leaks[*]}"; fi
unset cookie api_key entrance_password

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
