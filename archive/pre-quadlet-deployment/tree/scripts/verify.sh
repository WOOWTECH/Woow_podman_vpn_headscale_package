#!/usr/bin/env bash
# Runtime verification for the Woow Headscale stack. No secret values are printed.
set -uo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PROJECT_DIR=$(pwd -P)
ENV_FILE="$PROJECT_DIR/.env"
RUNTIME_DIR="$PROJECT_DIR/runtime"
failures=0

pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }
check_command() {
  if command -v "$1" >/dev/null 2>&1; then :; else fail "required command $1 is available"; fi
}
for command_name in python3 podman curl stat; do check_command "$command_name"; done
[[ $failures -eq 0 ]] || exit 1

if ! python3 "$PROJECT_DIR/scripts/runtime_config.py" render \
  --env-file "$ENV_FILE" --output-dir "$RUNTIME_DIR/.verify-render" >/dev/null; then
  fail "strict environment validation"
  exit 1
fi
rm -rf "$RUNTIME_DIR/.verify-render"

env_value() {
  python3 - "$PROJECT_DIR" "$ENV_FILE" "$1" <<'PY'
import importlib.util
from pathlib import Path
import sys
spec=importlib.util.spec_from_file_location("runtime_config", Path(sys.argv[1]) / "scripts/runtime_config.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.stdout.write(module.validated_env(sys.argv[2])[sys.argv[3]])
PY
}

COMPOSE_PROJECT_NAME=$(env_value COMPOSE_PROJECT_NAME)
NGROK_ENABLED=$(env_value NGROK_ENABLED)
NGROK_MODE=$(env_value NGROK_MODE)
NGROK_DOMAIN=$(env_value NGROK_DOMAIN)
HEADSCALE_PORT=$(env_value HEADSCALE_PORT)
HEADSCALE_HOST_BIND_ADDR=$(env_value HEADSCALE_HOST_BIND_ADDR)
HEADSCALE_HOST_PORT=$(env_value HEADSCALE_HOST_PORT)
HEADSCALE_METRICS_PORT=$(env_value HEADSCALE_METRICS_PORT)
HEADSCALE_METRICS_HOST_BIND_ADDR=$(env_value HEADSCALE_METRICS_HOST_BIND_ADDR)
HEADSCALE_METRICS_HOST_PORT=$(env_value HEADSCALE_METRICS_HOST_PORT)
HEADPLANE_PORT=$(env_value HEADPLANE_PORT)
HEADPLANE_HOST_BIND_ADDR=$(env_value HEADPLANE_HOST_BIND_ADDR)
HEADPLANE_HOST_PORT=$(env_value HEADPLANE_HOST_PORT)
CREATE_DEFAULT_USER=$(env_value CREATE_DEFAULT_USER)

probe_url() {
  local address=$1 port=$2
  case "$address" in 0.0.0.0) address=127.0.0.1 ;; ::) address=::1 ;; esac
  [[ $address != *:* ]] || address="[$address]"
  printf 'http://%s:%s' "$address" "$port"
}

container_running() {
  podman inspect "$1" 2>/dev/null | python3 -c '
import json, sys
value=json.load(sys.stdin)[0]
raise SystemExit(0 if value.get("State", {}).get("Running") is True else 1)
'
}

container_image_is() {
  podman inspect "$1" 2>/dev/null | python3 -c '
import json, sys
value=json.load(sys.stdin)[0]
actual=value.get("ImageName") or value.get("Config", {}).get("Image", "")
raise SystemExit(0 if actual == sys.argv[1] else 1)
' "$2"
}

headscale_image_contract_is_current() {
  local recipe_sha
  recipe_sha=$(python3 - "$PROJECT_DIR/Containerfile.headscale" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
sys.stdout.write(sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)
  podman image inspect localhost/woow-headscale:0.29.3 2>/dev/null | python3 -c '
import json, sys
try:
    config=(json.load(sys.stdin)[0].get("Config", {}) or {})
    labels=config.get("Labels", {}) or {}
    expected={
        "org.opencontainers.image.version": "0.29.3",
        "org.woow-headscale.runtime": "headscale",
        "org.woow-headscale.source-image": "docker.io/headscale/headscale:v0.29.3@sha256:0e7f1c6e4ce6c2a2a001103ecd3fa645a045adf30ac8a5234fe037b43000cd72",
        "org.woow-headscale.build-contract": "headscale-shell-health-v1",
        "org.woow-headscale.recipe-sha256": sys.argv[1],
    }
    valid=(all(labels.get(k) == v for k, v in expected.items())
           and config.get("Entrypoint") == ["/ko-app/headscale"])
except (IndexError, TypeError, ValueError):
    valid=False
raise SystemExit(0 if valid else 1)
' "$recipe_sha"
}

headscale_container_healthcheck_is_native() {
  podman inspect headscale 2>/dev/null | python3 -c '
import json, sys
try:
    config=(json.load(sys.stdin)[0].get("Config", {}) or {})
    health=(config.get("Healthcheck", {}) or {})
    valid=health.get("Test") == ["CMD", "/ko-app/headscale", "health"]
except (IndexError, TypeError, ValueError):
    valid=False
raise SystemExit(0 if valid else 1)
'
}

binding_exists() {
  podman inspect "$1" 2>/dev/null | python3 -c '
import json, sys
value=json.load(sys.stdin)[0]
container_port, expected_ip, expected_port=sys.argv[1:]
ports=value.get("NetworkSettings", {}).get("Ports", {}) or {}
bindings=ports.get(container_port + "/tcp", []) or []
def matches(binding):
    actual_ip=str(binding.get("HostIp", "")) or "0.0.0.0"
    return actual_ip == expected_ip and str(binding.get("HostPort", "")) == expected_port
raise SystemExit(0 if any(matches(binding) for binding in bindings) else 1)
' "$2" "$3" "$4"
}

container_health_status() {
  podman inspect "$1" 2>/dev/null | python3 -c '
import json, sys
value=json.load(sys.stdin)[0]
sys.stdout.write(str(value.get("State", {}).get("Health", {}).get("Status", "")))
'
}

wait_container_healthy() {
  local container=$1 timeout=${2:-120} poll_interval=${3:-2}
  local deadline status remaining sleep_time
  deadline=$((SECONDS + timeout))
  while :; do
    if ! status=$(container_health_status "$container"); then
      status=unknown
    fi
    case "$status" in
      healthy) return 0 ;;
      unhealthy|"") return 1 ;;
    esac
    remaining=$((deadline - SECONDS))
    (( remaining > 0 )) || return 1
    (( remaining > poll_interval )) && sleep_time=$poll_interval || sleep_time=$remaining
    sleep "$sleep_time"
  done
}

restart_policy_is_always() {
  podman inspect "$1" 2>/dev/null | python3 -c '
import json, sys
value=json.load(sys.stdin)[0]
policy=value.get("HostConfig", {}).get("RestartPolicy", {}) or {}
raise SystemExit(0 if str(policy.get("Name", "")).lower() == "always" else 1)
'
}

mount_exists() {
  podman inspect "$1" 2>/dev/null | python3 -c '
import json, sys
value=json.load(sys.stdin)[0]
destination, expected_type, expected_source=sys.argv[1:]
for mount in value.get("Mounts", []) or []:
    if mount.get("Destination") != destination or str(mount.get("Type", "")).lower() != expected_type:
        continue
    actual=str(mount.get("Source", "")) if expected_type == "bind" else str(mount.get("Name", ""))
    if actual == expected_source:
        raise SystemExit(0)
raise SystemExit(1)
' "$2" "$3" "$4"
}

for name in headscale headplane ngrok; do
  if python3 "$PROJECT_DIR/scripts/volume_ownership.py" check-container \
    --project "$COMPOSE_PROJECT_NAME" --project-dir "$PROJECT_DIR" --container "$name"; then
    pass "$name container ownership is exact"
  else
    fail "$name container ownership is exact"
  fi
done
for name in headscale headplane; do
  if container_running "$name"; then pass "$name container is running"; else fail "$name container is running"; fi
done
if container_image_is headscale localhost/woow-headscale:0.29.3; then pass "Headscale image is pinned to the local v0.29.3 runtime"; else fail "Headscale image is pinned to the local v0.29.3 runtime"; fi
if headscale_image_contract_is_current; then pass "Headscale image labels and entrypoint match the build contract"; else fail "Headscale image labels and entrypoint match the build contract"; fi
if headscale_container_healthcheck_is_native; then pass "Live Headscale container has the exact native healthcheck"; else fail "Live Headscale container has the exact native healthcheck"; fi
if container_image_is headplane ghcr.io/tale/headplane:0.7.0; then pass "Headplane image is pinned to 0.7.0"; else fail "Headplane image is pinned to 0.7.0"; fi
for name in headscale headplane; do
  if restart_policy_is_always "$name"; then pass "$name restart policy is always"; else fail "$name restart policy is always"; fi
done

while IFS='|' read -r container destination mount_type expected_source label; do
  if mount_exists "$container" "$destination" "$mount_type" "$expected_source"; then
    pass "$label mount is durable"
  else
    fail "$label mount is durable"
  fi
done <<EOF
headscale|/etc/headscale/config.yaml|bind|$RUNTIME_DIR/headscale/config.yaml|Headscale config
headscale|/etc/headscale/extra_records.json|bind|$RUNTIME_DIR/headscale/extra_records.json|Headscale extra records
headscale|/etc/headscale/policy.json|bind|$PROJECT_DIR/config/headscale/policy.json|Headscale policy
headplane|/etc/headplane/config.yaml|bind|$RUNTIME_DIR/headplane/config.yaml|Headplane config
headplane|/etc/headplane/cookie-secret|bind|$RUNTIME_DIR/headplane/cookie-secret|Headplane cookie secret
headplane|/etc/headplane/api-key|bind|$RUNTIME_DIR/headplane/api-key|Headplane API key
EOF

while IFS='|' read -r container destination logical label; do
  if python3 "$PROJECT_DIR/scripts/volume_ownership.py" resolve \
    --project "$COMPOSE_PROJECT_NAME" --project-dir "$PROJECT_DIR" \
    --container "$container" --destination "$destination" --logical "$logical" >/dev/null; then
    pass "$label volume is exact and project-owned"
  else
    fail "$label volume is exact and project-owned"
  fi
done <<EOF
headscale|/var/lib/headscale|headscale-data|Headscale data
headscale|/var/run/headscale|headscale-run|Headscale run state
headplane|/var/lib/headplane|headplane-data|Headplane data
EOF

version_file=$(mktemp "$RUNTIME_DIR/.verify-version.XXXXXX")
if podman exec headscale headscale version >"$version_file" 2>&1 && grep -q '0\.29\.3' "$version_file"; then
  pass "Headscale reports version 0.29.3"
else
  fail "Headscale reports version 0.29.3"
fi
rm -f "$version_file"

HEADSCALE_PROBE=$(probe_url "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT")
if curl --fail --silent --max-time 5 "$HEADSCALE_PROBE/health" >/dev/null; then
  pass "Headscale HTTP health"
  # Explicitly drive the native check before reading its state. This covers
  # Podman releases whose internal systemd scheduler could not be attached.
  if podman healthcheck run headscale >/dev/null 2>&1; then
    pass "Headscale live container healthcheck"
  else
    fail "Headscale live container healthcheck"
  fi
  if [[ $(container_health_status headscale) == healthy ]]; then
    pass "Headscale container health is healthy"
  else
    fail "Headscale container health is healthy"
  fi
else
  fail "Headscale HTTP health"
  fail "Headscale live container healthcheck after HTTP health"
  fail "Headscale container health is healthy after HTTP health"
fi

HEADPLANE_PROBE=$(probe_url "$HEADPLANE_HOST_BIND_ADDR" "$HEADPLANE_HOST_PORT")
headplane_status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 "$HEADPLANE_PROBE/admin" || true)
if [[ $headplane_status == 200 || $headplane_status == 302 ]]; then pass "Headplane HTTP responds successfully"; else fail "Headplane HTTP responds successfully"; fi

if binding_exists headscale "$HEADSCALE_PORT" "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT"; then pass "Headscale control-plane host binding"; else fail "Headscale control-plane host binding"; fi
if binding_exists headscale "$HEADSCALE_METRICS_PORT" "$HEADSCALE_METRICS_HOST_BIND_ADDR" "$HEADSCALE_METRICS_HOST_PORT"; then pass "Headscale metrics host binding"; else fail "Headscale metrics host binding"; fi
if binding_exists headplane "$HEADPLANE_PORT" "$HEADPLANE_HOST_BIND_ADDR" "$HEADPLANE_HOST_PORT"; then pass "Headplane host binding"; else fail "Headplane host binding"; fi

mode_is() { [[ -e $1 && $(stat -c '%a' "$1") == "$2" ]]; }
for directory in "$RUNTIME_DIR" "$RUNTIME_DIR/headscale" "$RUNTIME_DIR/headplane"; do
  if mode_is "$directory" 700; then pass "$(basename "$directory") runtime directory mode is 700"; else fail "$(basename "$directory") runtime directory mode is 700"; fi
done
for secret in "$ENV_FILE" "$RUNTIME_DIR/headplane/cookie-secret" "$RUNTIME_DIR/headplane/api-key" "$RUNTIME_DIR/headscale/config.yaml" "$RUNTIME_DIR/headplane/config.yaml"; do
  label=$(basename "$secret")
  if [[ -s $secret ]] && mode_is "$secret" 600; then pass "$label exists with mode 600"; else fail "$label exists with mode 600"; fi
done

if [[ $CREATE_DEFAULT_USER == true ]]; then
  users_file=$(mktemp "$RUNTIME_DIR/.verify-users.XXXXXX")
  if podman exec headscale headscale users list --output json >"$users_file" 2>/dev/null && python3 - "$users_file" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
if isinstance(value, dict): value=value.get("users", value.get("items", []))
raise SystemExit(0 if any(isinstance(item, dict) and item.get("name") == "default" for item in value) else 1)
PY
  then pass "default Headscale user exists"; else fail "default Headscale user exists"; fi
  rm -f "$users_file"
fi

keys_file=$(mktemp "$RUNTIME_DIR/.verify-keys.XXXXXX")
if podman exec headscale headscale apikeys list --output json >"$keys_file" 2>/dev/null \
  && python3 "$PROJECT_DIR/scripts/validate_api_key.py" \
    --key-file "$RUNTIME_DIR/headplane/api-key" --list-json "$keys_file" >/dev/null 2>&1; then
  pass "persistent Headplane API key exactly matches a valid Headscale key record"
else
  fail "persistent Headplane API key exactly matches a valid Headscale key record"
fi
rm -f "$keys_file"

SERVER_URL=$(python3 - "$RUNTIME_DIR/headscale/config.yaml" <<'PY'
from pathlib import Path
import sys
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.startswith("server_url: "):
        print(line.removeprefix("server_url: "))
        break
else:
    raise SystemExit(1)
PY
) || SERVER_URL=""
if [[ -n $SERVER_URL ]]; then pass "effective server URL: $SERVER_URL"; else fail "effective server URL is rendered"; fi

if [[ $NGROK_ENABLED == true ]]; then
  if container_running ngrok; then pass "ngrok container is running when enabled"; else fail "ngrok container is running when enabled"; fi
  if restart_policy_is_always ngrok; then pass "ngrok restart policy is always"; else fail "ngrok restart policy is always"; fi
  if binding_exists ngrok 4040 127.0.0.1 24040; then pass "ngrok API is loopback-only"; else fail "ngrok API is loopback-only"; fi
  ngrok_json=$(mktemp "$RUNTIME_DIR/.verify-ngrok.XXXXXX")
  if curl --fail --silent --max-time 5 -o "$ngrok_json" http://127.0.0.1:24040/api/tunnels; then
    ngrok_args=(ngrok-url --api-json "$ngrok_json" --mode "$NGROK_MODE")
    [[ -z $NGROK_DOMAIN ]] || ngrok_args+=(--domain "$NGROK_DOMAIN")
    selected=$(python3 "$PROJECT_DIR/scripts/runtime_config.py" "${ngrok_args[@]}" 2>/dev/null || true)
    if [[ -n $selected && $selected == "$SERVER_URL" ]]; then pass "rendered URL matches the selected ngrok tunnel"; else fail "rendered URL matches the selected ngrok tunnel"; fi
  else
    fail "ngrok API is reachable"
  fi
  rm -f "$ngrok_json"
else
  if ! podman container exists ngrok; then pass "ngrok container is absent when disabled"; else fail "ngrok container is absent when disabled"; fi
  if [[ $SERVER_URL == "$(env_value SERVER_URL)" ]]; then pass "static SERVER_URL is effective when ngrok is disabled"; else fail "static SERVER_URL is effective when ngrok is disabled"; fi
fi

if [[ $failures -eq 0 ]]; then
  printf 'Verification passed.\n'
  exit 0
fi
printf 'Verification failed: %d check(s).\n' "$failures" >&2
exit 1
