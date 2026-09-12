#!/usr/bin/env bash
# WoowTech Headscale VPN — Podman one-shot deployment
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

HS_PORT=28080
HP_PORT=23000

HS_CONFIG_SRC=config/headscale/config.yaml
HS_CONFIG_RUNTIME=config/headscale/config.runtime.yaml
API_KEY_FILE=config/headplane/api-key
PREAUTH_KEY_FILE=config/headscale/preauth-key

# ---------------------------------------------------------------
echo "==> [1/7] Load .env (SERVER_URL)"
[ -f .env ] || cp .env.example .env
# shellcheck disable=SC1091
source .env
SERVER_URL="${SERVER_URL:-http://localhost:${HS_PORT}}"
echo "    SERVER_URL = ${SERVER_URL}"

# Render server_url into an untracked runtime copy of the config —
# never edit the git-tracked config.yaml in place (that used to leave
# a permanent local diff / risked committing an environment-specific
# server_url). podman-compose.yml bind-mounts this rendered file over
# /etc/headscale/config.yaml.
sed "s|^server_url:.*|server_url: ${SERVER_URL}|" "$HS_CONFIG_SRC" > "$HS_CONFIG_RUNTIME"

# ---------------------------------------------------------------
echo "==> [2/7] Generate Headplane cookie secret (32 chars)"
if [ ! -f config/headplane/cookie-secret ]; then
  openssl rand -hex 16 | tr -d '\n' > config/headplane/cookie-secret
  echo "    created config/headplane/cookie-secret"
else
  echo "    already exists — keeping"
fi
# Placeholder api-key so the ro bind mount is complete on first start
[ -f "$API_KEY_FILE" ] || printf 'placeholder' > "$API_KEY_FILE"

# ---------------------------------------------------------------
echo "==> [3/7] Start Headscale"
podman-compose up -d headscale

echo "    waiting for /health ..."
for _ in $(seq 1 24); do
  if curl -sf "http://localhost:${HS_PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 5
done
curl -sf "http://localhost:${HS_PORT}/health" || { echo "FATAL: headscale not healthy"; podman logs --tail 20 headscale; exit 1; }
echo ""

# ---------------------------------------------------------------
echo "==> [4/7] Create default user (idempotent)"
podman exec headscale headscale users create default 2>/dev/null || echo "    user exists — ok"

# ---------------------------------------------------------------
echo "==> [5/7] Headplane API key (reused if one already exists)"
# headscale never lets us read back a previously-created key's secret
# value, so "reuse" means: keep whatever is already on disk instead of
# minting a fresh key (and orphaning the old one) on every run. A key's
# id is only revealed as a "<prefix>-***" stem (the full on-disk key is
# "<prefix>-<secret>"), so check the disk key still starts with a
# stem that headscale still lists.
EXISTING_KEY=""
[ -f "$API_KEY_FILE" ] && EXISTING_KEY=$(cat "$API_KEY_FILE")
STILL_VALID=0
if [ -n "$EXISTING_KEY" ] && [ "$EXISTING_KEY" != "placeholder" ]; then
  while IFS= read -r stem; do
    case "$EXISTING_KEY" in
      "${stem}"*) STILL_VALID=1 ;;
    esac
  done < <(podman exec headscale headscale apikeys list --output json 2>/dev/null \
             | grep -oE '"prefix": *"[^"]*-\*\*\*"' \
             | sed -E 's/.*"([^"]*)-\*\*\*"/\1/')
fi
if [ "$STILL_VALID" -eq 1 ]; then
  echo "    reusing existing API key (still listed in headscale) — not creating a new one"
else
  echo "    no valid API key on disk — creating one"
  podman exec headscale headscale apikeys create --expiration 90d | tail -1 | tr -d '[:space:]' > "$API_KEY_FILE"
fi
echo "    key is at ${API_KEY_FILE} (never printed to the terminal)"

# ---------------------------------------------------------------
echo "==> [6/7] Start Headplane"
podman-compose up -d headplane
sleep 5
HP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${HP_PORT}/admin" || true)
# 302 = redirect to login — healthy
if [ "$HP_CODE" != "302" ] && [ "$HP_CODE" != "200" ]; then
  # config was mounted before api-key existed → restart once
  podman restart headplane >/dev/null; sleep 5
  HP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${HP_PORT}/admin" || true)
fi
echo "    /admin -> HTTP ${HP_CODE}"

# ---------------------------------------------------------------
echo "==> [7/7] Create a reusable test PreAuthKey (72h)"
podman exec headscale headscale preauthkeys create --user 1 --reusable --expiration 72h \
  | tail -1 | tr -d '[:space:]' > "$PREAUTH_KEY_FILE"
echo "    preauth key written to ${PREAUTH_KEY_FILE} (never printed to the terminal)"

cat <<EOF

=============================================================
 ✅ Headscale + Headplane are up (rootless Podman)
=============================================================
 Headscale control plane : http://localhost:${HS_PORT}   (health: /health)
 Headplane admin UI      : http://localhost:${HP_PORT}/admin
 Headplane login API key : stored at ${API_KEY_FILE} (run: cat ${API_KEY_FILE})

 Connect a device:
   tailscale up --login-server=${SERVER_URL} --authkey=\$(cat ${PREAUTH_KEY_FILE})

 Expose externally (ngrok example — verified to pass the
 Tailscale noise protocol, unlike Cloudflare Tunnel):
   ngrok http ${HS_PORT}
   # then set SERVER_URL in .env to the https URL and re-run ./deploy.sh
=============================================================
EOF
