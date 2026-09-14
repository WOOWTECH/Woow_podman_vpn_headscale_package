#!/usr/bin/env bash
# tests/health-gate.sh: the readiness gate must work on a host where podman's transient
# healthcheck timers never fire for a Quadlet-started container.
#
#   tests/health-gate.sh
#
# On such a host (openclaw) `.State.Health.Status` stays "starting" for ever while
# `podman healthcheck run headscale` returns 0 and the control plane serves every request. A
# gate that waits PASSIVELY on that status burns its whole timeout and then declares a working
# stack dead - which is what scripts/install.sh, scripts/upgrade.sh, scripts/backup.sh and
# tests/smoke.sh used to do.
#
# Nothing here touches podman, systemd or the network: `podman` and `curl` are stubs on PATH
# whose answers each case sets, so the checks run in CI and on a workstation. What is exercised
# is the real hs_wait_ready from scripts/headscale-helpers.sh.
# shellcheck source-path=SCRIPTDIR
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

WORK=$(mktemp -d) || exit 1
trap 'rm -rf -- "$WORK"' EXIT
mkdir -p "$WORK/bin"

# ---- the stubs ----------------------------------------------------------------------------------
# STUB_STATUS       what `podman inspect` reports as .State.Status         (running | exited)
# STUB_HEALTH       what it reports as .State.Health.Status                (starting | healthy)
# STUB_HC_RC        the exit status of `podman healthcheck run`            (0 | 1)
# STUB_HTTP         the status code `curl -w %{http_code}` prints          (200 | 000 | ...)
cat >"$WORK/bin/podman" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  inspect) printf '%s|hc|%s|2026-09-15T00:00:00Z' "${STUB_STATUS:-running}" "${STUB_HEALTH:-starting}" ;;
  healthcheck) exit "${STUB_HC_RC:-0}" ;;
  *) exit 0 ;;
esac
STUB
cat >"$WORK/bin/curl" <<'STUB'
#!/usr/bin/env bash
printf '%s' "${STUB_HTTP:-000}"
STUB
chmod +x "$WORK/bin/podman" "$WORK/bin/curl"
PATH=$WORK/bin:$PATH

# ---- the env file the gate reads the published port from -----------------------------------------
printf 'WOOW_HEADSCALE_BIND=127.0.0.1\nWOOW_HEADSCALE_PORT=28080\n' >"$WORK/headscale.env"
chmod 600 "$WORK/headscale.env"

# shellcheck source=../scripts/lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=../scripts/common.sh
. "$REPO/scripts/common.sh"
# shellcheck source=../scripts/headscale-helpers.sh
. "$REPO/scripts/headscale-helpers.sh"
ql_env_load "$WORK/headscale.env"
export QL_POLL_INTERVAL=1 QL_HTTP_TIMEOUT=1

fails=0
pass() { printf 'ok   %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; fails=$((fails + 1)); }
check() { # check <expected rc> <description> <command...>
  local expect=$1 what=$2 rc=0
  shift 2
  "$@" >/dev/null 2>&1 || rc=$?
  if ((rc == expect)); then pass "$what"; else fail "$what (rc=$rc, want $expect)"; fi
}

if ! declare -F hs_wait_ready >/dev/null; then
  fail "scripts/headscale-helpers.sh defines hs_wait_ready (the health gate that does not wait on .State.Health.Status)"
  printf '%s failed\n' "$fails"
  exit 1
fi

# ---- 1. the openclaw shape: health status stuck at "starting", server answering -------------------
export STUB_STATUS=running STUB_HEALTH=starting STUB_HC_RC=0 STUB_HTTP=200
check 0 "the gate passes while .State.Health.Status is stuck at 'starting' and /health answers 200" \
  hs_wait_ready 6 headscale.service
# and this is why: the passive wait the scripts used to make their gate never returns on this host
passive_wait() { QL_HEALTH_ACTIVE=0 ql_wait_container_healthy headscale 4; }
check 1 "a PASSIVE wait on .State.Health.Status does time out on that host (the defect being fixed)" \
  passive_wait

# ---- 2. headscale genuinely down: the gate must still fail ----------------------------------------
export STUB_STATUS=exited STUB_HEALTH='' STUB_HC_RC=1 STUB_HTTP=000
check 1 "the gate FAILS when headscale is down (container exited, nothing answers /health)" \
  hs_wait_ready 4 headscale.service

# ---- 3. the HTTP probe is the authority, not the recorded status ----------------------------------
export STUB_STATUS=running STUB_HEALTH=healthy STUB_HC_RC=0 STUB_HTTP=503
check 1 "the gate FAILS when the container claims 'healthy' but /health answers 503" \
  hs_wait_ready 4 headscale.service

# ---- 4. a failing container healthcheck is a warning, never a veto --------------------------------
export STUB_STATUS=running STUB_HEALTH=starting STUB_HC_RC=1 STUB_HTTP=200
check 0 "the gate passes on /health 200 even when 'podman healthcheck run' does not pass" \
  hs_wait_ready 4 headscale.service

# ---- 5. no caller gates on a passive container-health wait any more -------------------------------
for f in scripts/install.sh scripts/upgrade.sh scripts/backup.sh tests/smoke.sh; do
  if grep -nE '(app_wait_healthy|ql_wait_container_healthy)[[:space:]]+headscale' "$REPO/$f" >/dev/null; then
    fail "5 $f still gates headscale on a container-health wait; use hs_wait_ready"
  else
    pass "5 $f does not gate headscale on a container-health wait"
  fi
done
# the repo-owned scripts must leave the active check on for the library wait that remains
if grep -q '^export QL_HEALTH_ACTIVE=' "$REPO/scripts/common.sh"; then
  pass "5 scripts/common.sh turns the library's active healthcheck on"
else
  fail "5 scripts/common.sh does not set QL_HEALTH_ACTIVE (vendored lib 1.6.0 defaults it off)"
fi

printf '%s failed\n' "$fails"
((fails == 0))
