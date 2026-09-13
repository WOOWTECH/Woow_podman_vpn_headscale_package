#!/usr/bin/env bash
# Secure, idempotent orchestration for the Woow Headscale stack.
set -euo pipefail
umask 077

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
PROJECT_DIR=$(pwd -P)
ENV_FILE="$PROJECT_DIR/.env"
RUNTIME_DIR="$PROJECT_DIR/runtime"
HEADSCALE_IMAGE="localhost/woow-headscale:0.29.3"
HEADSCALE_SOURCE_IMAGE="docker.io/headscale/headscale:v0.29.3@sha256:0e7f1c6e4ce6c2a2a001103ecd3fa645a045adf30ac8a5234fe037b43000cd72"
HEADSCALE_BUILD_CONTRACT="headscale-shell-health-v1"

fail() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

if [[ $# -gt 1 || ( $# -eq 1 && $1 != "--from-systemd" && $1 != "--stop" ) ]]; then
  fail "usage: ./deploy.sh [--from-systemd|--stop]"
fi
FROM_SYSTEMD=false
[[ ${1:-} == "--from-systemd" ]] && FROM_SYSTEMD=true

redact_log() {
  local log_file=$1
  python3 - "$PROJECT_DIR" "$log_file" <<'PY'
import importlib.util
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
log_path = Path(sys.argv[2])
text = log_path.read_text(encoding="utf-8", errors="replace")
secrets = []
spec = importlib.util.spec_from_file_location("runtime_config", root / "scripts/runtime_config.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    token = module.parse_env(root / ".env").get("NGROK_AUTHTOKEN", "")
    if token:
        secrets.append(token)
except ValueError:
    pass
api_key = root / "runtime/headplane/api-key"
if api_key.is_file():
    value = api_key.read_text(encoding="utf-8", errors="ignore").strip()
    if value:
        secrets.append(value)
for secret in secrets:
    text = text.replace(secret, "[REDACTED]")
text = re.sub(r"\bhskey-[A-Za-z0-9._~-]+\b", "[REDACTED]", text)
text = re.sub(r"(?i)(NGROK_AUTHTOKEN\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
sys.stderr.write(text)
PY
}

capture_command() {
  local log_file status
  mkdir -p "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"
  log_file=$(mktemp "$RUNTIME_DIR/.command-log.XXXXXX")
  chmod 600 "$log_file"
  if "$@" >"$log_file" 2>&1; then
    rm -f "$log_file"
    return 0
  else
    status=$?
    redact_log "$log_file"
    rm -f "$log_file"
    return "$status"
  fi
}

headscale_runtime_image_is_current() {
  local check_dir cidfile container_name suffix status=1 cleanup_status=0

  # Podman 4.9 can omit Docker-format health metadata from image inspect, so use
  # image inspect only for the fields it reports reliably.
  podman image inspect "$HEADSCALE_IMAGE" 2>/dev/null | python3 -c '
import json
import sys
try:
    image = json.load(sys.stdin)[0]
    config = image.get("Config", {}) or {}
    labels = config.get("Labels", {}) or {}
    expected = {
        "org.opencontainers.image.version": "0.29.3",
        "org.woow-headscale.runtime": "headscale",
        "org.woow-headscale.source-image": sys.argv[1],
        "org.woow-headscale.build-contract": sys.argv[2],
        "org.woow-headscale.recipe-sha256": sys.argv[3],
    }
    valid = (
        all(labels.get(key) == value for key, value in expected.items())
        and config.get("Entrypoint") == ["/ko-app/headscale"]
    )
except (IndexError, TypeError, ValueError):
    valid = False
raise SystemExit(0 if valid else 1)
' "$HEADSCALE_SOURCE_IMAGE" "$HEADSCALE_BUILD_CONTRACT" "$HEADSCALE_RECIPE_SHA256" \
    || return 1

  # A never-started container exposes the inherited healthcheck reliably on
  # Podman 4.9. It receives no mounts or published ports and has networking off.
  check_dir=$(mktemp -d "$PREFLIGHT_DIR/.image-healthcheck.XXXXXX") || return 1
  chmod 700 "$check_dir" || return 1
  cidfile="$check_dir/cid"
  suffix=${check_dir##*.image-healthcheck.}
  container_name="woow-headscale-image-check-$$-$suffix"
  HEADSCALE_HEALTHCHECK_CONTAINERS+=("$container_name")
  if podman create --name "$container_name" --cidfile "$cidfile" --network none \
      "$HEADSCALE_IMAGE" >/dev/null 2>&1 \
    && [[ -s $cidfile ]] \
    && podman container inspect "$(<"$cidfile")" 2>/dev/null | python3 -c '
import json
import sys
try:
    config = (json.load(sys.stdin)[0].get("Config", {}) or {})
    health = (config.get("Healthcheck", {}) or {})
    valid = health.get("Test") == ["CMD", "/ko-app/headscale", "health"]
except (IndexError, TypeError, ValueError):
    valid = False
raise SystemExit(0 if valid else 1)
'; then
    status=0
  fi
  podman rm --ignore "$container_name" >/dev/null 2>&1 || cleanup_status=$?
  if [[ $cleanup_status -eq 0 ]]; then
    unset 'HEADSCALE_HEALTHCHECK_CONTAINERS[${#HEADSCALE_HEALTHCHECK_CONTAINERS[@]}-1]'
  fi
  rm -rf -- "$check_dir"
  [[ $status -eq 0 && $cleanup_status -eq 0 ]]
}

compose_base() {
  capture_command podman-compose -f "$PROJECT_DIR/podman-compose.yml" "$@"
}

compose_ngrok() {
  capture_command podman-compose \
    -f "$PROJECT_DIR/podman-compose.yml" \
    -f "$PROJECT_DIR/podman-compose.ngrok.yml" "$@"
}

for command_name in python3 podman podman-compose install; do
  require_command "$command_name"
done

[[ ! -L $ENV_FILE ]] || fail ".env must not be a symbolic link"
if [[ ! -f $ENV_FILE ]]; then
  install -m 600 "$PROJECT_DIR/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# Read canonical values through the strict parser. The dotenv file is data and is
# never sourced; exported validated values override every inherited Compose input.
env_value() {
  python3 - "$PROJECT_DIR" "$ENV_FILE" "$1" <<'PY'
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("runtime_config", Path(sys.argv[1]) / "scripts/runtime_config.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    values = module.validated_env(sys.argv[2])
except ValueError as exc:
    raise SystemExit(f"invalid .env: {exc}")
sys.stdout.write(values[sys.argv[3]])
PY
}

# Validate once before exporting anything, then explicitly replace all variables
# consumed by either Compose model. NGROK_COMMAND and WOOW_PROJECT_DIR are derived
# only from validated data and this canonical checkout path.
PREFLIGHT_DIR=$(mktemp -d "$PROJECT_DIR/.deploy-preflight.XXXXXX")
chmod 700 "$PREFLIGHT_DIR"
HEADSCALE_HEALTHCHECK_CONTAINERS=()
cleanup_preflight() {
  local status=$? container_name
  for container_name in "${HEADSCALE_HEALTHCHECK_CONTAINERS[@]}"; do
    podman rm --ignore "$container_name" >/dev/null 2>&1 || :
  done
  rm -rf -- "$PREFLIGHT_DIR"
  return "$status"
}
trap cleanup_preflight EXIT
python3 "$PROJECT_DIR/scripts/runtime_config.py" render \
  --env-file "$ENV_FILE" --output-dir "$PREFLIGHT_DIR/rendered"
COMPOSE_KEYS=(
  COMPOSE_PROJECT_NAME HEADSCALE_HOST_BIND_ADDR HEADSCALE_HOST_PORT HEADSCALE_PORT
  HEADSCALE_METRICS_HOST_BIND_ADDR HEADSCALE_METRICS_HOST_PORT HEADSCALE_METRICS_PORT
  HEADPLANE_HOST_BIND_ADDR HEADPLANE_HOST_PORT HEADPLANE_PORT NGROK_AUTHTOKEN
)
unset "${COMPOSE_KEYS[@]}" NGROK_COMMAND WOOW_PROJECT_DIR
for key in "${COMPOSE_KEYS[@]}"; do
  printf -v "$key" '%s' "$(env_value "$key")"
  export "$key"
done
WOOW_PROJECT_DIR=$PROJECT_DIR
export WOOW_PROJECT_DIR

NGROK_ENABLED=$(env_value NGROK_ENABLED)
NGROK_MODE=$(env_value NGROK_MODE)
NGROK_DOMAIN=$(env_value NGROK_DOMAIN)
CREATE_DEFAULT_USER=$(env_value CREATE_DEFAULT_USER)
NGROK_COMMAND="$NGROK_MODE headscale:$HEADSCALE_PORT"
if [[ -n $NGROK_DOMAIN ]]; then
  NGROK_COMMAND+=" --url=$NGROK_DOMAIN"
fi
export NGROK_COMMAND

# Global resource names are reusable only when every immutable ownership label
# identifies this exact stack, Compose project, and checkout. Existing containers
# must also mount each data volume at its expected destination.
for container_name in headscale headplane ngrok; do
  python3 "$PROJECT_DIR/scripts/volume_ownership.py" check-container \
    --project "$COMPOSE_PROJECT_NAME" --project-dir "$PROJECT_DIR" \
    --container "$container_name" \
    || fail "container name is already used by another checkout or project: $container_name"
done
for specification in \
    "headscale|/var/lib/headscale|headscale-data" \
    "headscale|/var/run/headscale|headscale-run" \
    "headplane|/var/lib/headplane|headplane-data"; do
  IFS='|' read -r container_name destination logical_name <<<"$specification"
  python3 "$PROJECT_DIR/scripts/volume_ownership.py" resolve \
    --project "$COMPOSE_PROJECT_NAME" --project-dir "$PROJECT_DIR" \
    --container "$container_name" --destination "$destination" \
    --logical "$logical_name" --allow-missing \
    || fail "volume is already used by another checkout or project: $logical_name"
done

if [[ ${1:-} == "--stop" ]]; then
  compose_ngrok down || fail "could not stop this stack safely"
  rm -rf -- "$PREFLIGHT_DIR"
  trap - EXIT
  exit 0
fi

for command_name in curl ss sleep; do
  require_command "$command_name"
done

preflight_compose_config() {
  local log_file="$PREFLIGHT_DIR/compose-config.log"
  if "$@" config >"$log_file" 2>&1; then
    rm -f "$log_file"
  else
    redact_log "$log_file"
    fail "podman-compose configuration validation failed"
  fi
}
preflight_compose_config podman-compose -f "$PROJECT_DIR/podman-compose.yml"
preflight_compose_config podman-compose \
  -f "$PROJECT_DIR/podman-compose.yml" -f "$PROJECT_DIR/podman-compose.ngrok.yml"

# The local tag is reusable only when its immutable source, image contract, and
# exact recipe are identified. Build with an empty private context so runtime
# secrets can never enter the build context or failure log.
HEADSCALE_RECIPE_SHA256=$(python3 - "$PROJECT_DIR/Containerfile.headscale" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
sys.stdout.write(sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)
if ! headscale_runtime_image_is_current; then
  mkdir "$PREFLIGHT_DIR/build-context"
  install -m 600 "$PROJECT_DIR/Containerfile.headscale" \
    "$PREFLIGHT_DIR/build-context/Containerfile.headscale"
  capture_command podman build --pull=always --format docker \
    --file "$PREFLIGHT_DIR/build-context/Containerfile.headscale" \
    --tag "$HEADSCALE_IMAGE" \
    --label "org.woow-headscale.recipe-sha256=$HEADSCALE_RECIPE_SHA256" \
    "$PREFLIGHT_DIR/build-context" \
    || fail "could not build the pinned Headscale runtime image"
fi
headscale_runtime_image_is_current \
  || fail "Headscale runtime image does not satisfy the expected build contract"

rm -rf -- "$PREFLIGHT_DIR"
trap - EXIT

bindings_overlap() {
  python3 "$PROJECT_DIR/scripts/port_bindings.py" overlap "$1" "$2" "$3" "$4"
}

check_port() {
  local address=$1 port=$2 owner=$3
  python3 "$PROJECT_DIR/scripts/port_bindings.py" check \
    --address "$address" --port "$port" --owner "$owner" \
    --project "$COMPOSE_PROJECT_NAME" --project-dir "$PROJECT_DIR" \
    || fail "host address and port are already in use: $address:$port"
}
if bindings_overlap "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT" \
    "$HEADSCALE_METRICS_HOST_BIND_ADDR" "$HEADSCALE_METRICS_HOST_PORT" \
  || bindings_overlap "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT" \
    "$HEADPLANE_HOST_BIND_ADDR" "$HEADPLANE_HOST_PORT" \
  || bindings_overlap "$HEADSCALE_METRICS_HOST_BIND_ADDR" "$HEADSCALE_METRICS_HOST_PORT" \
    "$HEADPLANE_HOST_BIND_ADDR" "$HEADPLANE_HOST_PORT"; then
  fail "configured project host bindings overlap"
fi
check_port "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT" headscale
check_port "$HEADSCALE_METRICS_HOST_BIND_ADDR" "$HEADSCALE_METRICS_HOST_PORT" headscale
check_port "$HEADPLANE_HOST_BIND_ADDR" "$HEADPLANE_HOST_PORT" headplane
if [[ $NGROK_ENABLED == true ]]; then
  if bindings_overlap "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT" 127.0.0.1 24040 \
    || bindings_overlap "$HEADSCALE_METRICS_HOST_BIND_ADDR" "$HEADSCALE_METRICS_HOST_PORT" 127.0.0.1 24040 \
    || bindings_overlap "$HEADPLANE_HOST_BIND_ADDR" "$HEADPLANE_HOST_PORT" 127.0.0.1 24040; then
    fail "configured project host bindings conflict with the ngrok API binding"
  fi
  check_port 127.0.0.1 24040 ngrok
fi

python3 "$PROJECT_DIR/scripts/runtime_config.py" init-extra-records --output-dir "$RUNTIME_DIR" \
  || fail "could not safely initialize Headscale extra records"
[[ ! -L $RUNTIME_DIR/headplane ]] || fail "runtime directories must not be symbolic links"
mkdir -p "$RUNTIME_DIR/headplane"
chmod 700 "$RUNTIME_DIR" "$RUNTIME_DIR/headscale" "$RUNTIME_DIR/headplane"

COOKIE_FILE="$RUNTIME_DIR/headplane/cookie-secret"
API_KEY_FILE="$RUNTIME_DIR/headplane/api-key"
[[ ! -L $API_KEY_FILE ]] || fail "runtime secret files must not be symbolic links"

# Validate before reuse, and rotate only the identifiable 64-hex value emitted by
# older releases. The helper publishes mode-600 files atomically without following
# the destination and never writes secret material to command output.
python3 "$PROJECT_DIR/scripts/cookie_secret.py" "$COOKIE_FILE" \
  --legacy "$PROJECT_DIR/config/headplane/cookie-secret" \
  || fail "could not safely initialize Headplane cookie secret"

# Migrate the API key from the pre-runtime-config layout without rotating it.
legacy_file="$PROJECT_DIR/config/headplane/api-key"
if [[ ! -s $API_KEY_FILE && -s $legacy_file ]]; then
  [[ ! -L $legacy_file ]] || fail "legacy secret files must not be symbolic links"
  mv -f "$legacy_file" "$API_KEY_FILE"
  chmod 600 "$API_KEY_FILE"
fi

render_args=()
if [[ $NGROK_ENABLED == false ]]; then
  if podman container exists ngrok; then
    capture_command podman rm -f ngrok || fail "could not remove disabled ngrok container"
  fi
else
  compose_ngrok up -d ngrok || fail "ngrok failed to start"

  NGROK_API_JSON="$RUNTIME_DIR/ngrok-api.json"
  api_tmp="$RUNTIME_DIR/.ngrok-api.tmp"
  rm -f "$NGROK_API_JSON" "$api_tmp"
  deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    remaining=$((deadline - SECONDS))
    (( remaining > 5 )) && request_timeout=5 || request_timeout=$remaining
    if curl --fail --silent --show-error --max-time "$request_timeout" \
      -o "$api_tmp" http://127.0.0.1:24040/api/tunnels 2>/dev/null; then
      chmod 600 "$api_tmp"
      mv -f "$api_tmp" "$NGROK_API_JSON"
      break
    fi
    remaining=$((deadline - SECONDS))
    (( remaining > 0 )) || break
    (( remaining > 5 )) && sleep_time=5 || sleep_time=$remaining
    sleep "$sleep_time"
  done
  [[ -s $NGROK_API_JSON ]] || fail "ngrok API did not become ready within 120 seconds"
  chmod 600 "$NGROK_API_JSON"

  ngrok_args=(ngrok-url --api-json "$NGROK_API_JSON" --mode "$NGROK_MODE")
  [[ -z $NGROK_DOMAIN ]] || ngrok_args+=(--domain "$NGROK_DOMAIN")
  DISCOVERED_URL=$(python3 "$PROJECT_DIR/scripts/runtime_config.py" "${ngrok_args[@]}") \
    || fail "no matching ngrok tunnel was found"
  render_args=(--server-url "$DISCOVERED_URL")
fi

python3 "$PROJECT_DIR/scripts/runtime_config.py" render \
  --env-file "$ENV_FILE" --output-dir "$RUNTIME_DIR" "${render_args[@]}"
compose_base up -d --force-recreate headscale || fail "Headscale failed to start"

probe_url() {
  local address=$1 port=$2
  case "$address" in
    0.0.0.0) address=127.0.0.1 ;;
    ::) address=::1 ;;
  esac
  [[ $address != *:* ]] || address="[$address]"
  printf 'http://%s:%s' "$address" "$port"
}
HEADSCALE_PROBE=$(probe_url "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT")
headscale_ok=false
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  remaining=$((deadline - SECONDS))
  (( remaining > 5 )) && request_timeout=5 || request_timeout=$remaining
  if curl --fail --silent --max-time "$request_timeout" "$HEADSCALE_PROBE/health" >/dev/null 2>&1; then
    headscale_ok=true
    break
  fi
  remaining=$((deadline - SECONDS))
  (( remaining > 0 )) || break
  (( remaining > 5 )) && sleep_time=5 || sleep_time=$remaining
  sleep "$sleep_time"
done
[[ $headscale_ok == true ]] || fail "Headscale did not become healthy within 120 seconds"
# Podman 4.9 can create the native healthcheck without successfully attaching its
# own systemd scheduler. Drive one live check now so "starting" cannot survive
# until final verification, while preserving a hard failure from the native check.
capture_command podman healthcheck run headscale \
  || fail "Headscale live container healthcheck failed"

if [[ $CREATE_DEFAULT_USER == true ]]; then
  users_json=$(mktemp "$RUNTIME_DIR/.users.XXXXXX")
  if ! podman exec headscale headscale users list --output json >"$users_json" 2>&1; then
    redact_log "$users_json"
    rm -f "$users_json"
    fail "could not list Headscale users"
  fi
  if python3 - "$users_json" <<'PY'
import json, sys
try:
    value=json.load(open(sys.argv[1], encoding="utf-8"))
    if value is None:
        value=[]
    elif isinstance(value, dict):
        value=value.get("users", value.get("items"))
    if not isinstance(value, list):
        raise ValueError
except (OSError, ValueError, TypeError):
    raise SystemExit(2)
raise SystemExit(0 if any(isinstance(item, dict) and item.get("name") == "default" for item in value) else 1)
PY
  then
    :
  else
    user_status=$?
    if [[ $user_status -eq 1 ]]; then
      capture_command podman exec headscale headscale users create default \
        || fail "could not create default Headscale user"
    else
      rm -f "$users_json"
      fail "Headscale returned an invalid user list"
    fi
  fi
  rm -f "$users_json"
fi

if [[ ! -s $API_KEY_FILE ]]; then
  key_output=$(mktemp "$RUNTIME_DIR/headplane/.apikey-output.XXXXXX")
  chmod 600 "$key_output"
  if ! podman exec headscale headscale apikeys create --expiration 3650d >"$key_output" 2>&1; then
    redact_log "$key_output"
    rm -f "$key_output"
    fail "could not create Headplane API key"
  fi
  key_tmp=$(mktemp "$RUNTIME_DIR/headplane/.apikey.XXXXXX")
  if ! python3 - "$key_output" "$key_tmp" <<'PY'
from pathlib import Path
import os, re, sys
lines=[line.strip() for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()]
if not lines or not re.fullmatch(r"hskey-api-[A-Za-z0-9_-]{12}-[A-Za-z0-9_-]{64}", lines[-1], re.ASCII):
    raise SystemExit("Headscale returned an invalid API key")
fd=os.open(sys.argv[2], os.O_WRONLY | os.O_TRUNC)
with os.fdopen(fd, "w", encoding="utf-8") as target:
    target.write(lines[-1] + "\n")
os.chmod(sys.argv[2], 0o600)
PY
  then
    rm -f "$key_output" "$key_tmp"
    fail "Headscale returned an invalid API key"
  fi
  rm -f "$key_output"
  mv -f "$key_tmp" "$API_KEY_FILE"
fi
chmod 600 "$API_KEY_FILE"

keys_json=$(mktemp "$RUNTIME_DIR/.apikey-list.XXXXXX")
chmod 600 "$keys_json"
if ! podman exec headscale headscale apikeys list --output json >"$keys_json" 2>&1; then
  redact_log "$keys_json"
  rm -f "$keys_json"
  fail "could not list Headscale API keys"
fi
if ! python3 "$PROJECT_DIR/scripts/validate_api_key.py" \
  --key-file "$API_KEY_FILE" --list-json "$keys_json"; then
  rm -f "$keys_json"
  fail "persisted Headplane API key is malformed, stale, revoked, or expired"
fi
rm -f "$keys_json"

compose_base up -d --force-recreate headplane || fail "Headplane failed to start"
HEADPLANE_PROBE=$(probe_url "$HEADPLANE_HOST_BIND_ADDR" "$HEADPLANE_HOST_PORT")
headplane_ok=false
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
  remaining=$((deadline - SECONDS))
  (( remaining > 5 )) && request_timeout=5 || request_timeout=$remaining
  status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
    --max-time "$request_timeout" "$HEADPLANE_PROBE/admin" || true)
  if [[ $status == 200 || $status == 302 ]]; then
    headplane_ok=true
    break
  fi
  remaining=$((deadline - SECONDS))
  (( remaining > 0 )) || break
  (( remaining > 5 )) && sleep_time=5 || sleep_time=$remaining
  sleep "$sleep_time"
done
[[ $headplane_ok == true ]] || fail "Headplane HTTP verification failed"

"$PROJECT_DIR/scripts/verify.sh" >/dev/null

if [[ $FROM_SYSTEMD == false ]]; then
  require_command systemctl
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  UNIT_NAMES=(
    "$COMPOSE_PROJECT_NAME.service"
    "${COMPOSE_PROJECT_NAME}_health.service"
    "${COMPOSE_PROJECT_NAME}_health.timer"
  )
  mkdir -p "$UNIT_DIR"
  for unit_name in "${UNIT_NAMES[@]}"; do
    python3 "$PROJECT_DIR/scripts/render_systemd_unit.py" \
      "$PROJECT_DIR/systemd/$unit_name" "$UNIT_DIR/$unit_name" "$PROJECT_DIR"
  done
  capture_command systemctl --user daemon-reload \
    || fail "could not reload the user systemd manager"
  capture_command systemctl --user enable \
    "$COMPOSE_PROJECT_NAME.service" "${COMPOSE_PROJECT_NAME}_health.timer" \
    || fail "could not enable the user systemd units"
fi

# ExecStop disables the scheduler during a main-unit restart. Re-arm only the
# timer after verification; its ordered service must not be started while the
# main oneshot is still activating.
capture_command systemctl --user start "${COMPOSE_PROJECT_NAME}_health.timer" \
  || fail "could not start the Headscale healthcheck scheduler"

printf 'Deployment verified. Server URL: %s\n' "${DISCOVERED_URL:-$(env_value SERVER_URL)}"
