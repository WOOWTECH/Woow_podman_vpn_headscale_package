#!/usr/bin/env bash
# tests/lint-repo.sh: static repository checks for CI (.github/workflows/repo-checks.yml) and
# local use. Creates nothing, starts nothing and needs no podman.
#
#   1. no plaintext credentials, key-shaped strings, or any of the three live values the
#      compose host kept outside git (tests/leaked-value-scan.py)
#   2. the compose deployment is gone (decision D1: Docker users use the compose-final tag)
#   3. both READMEs lead with the Quadlet install and point Docker users at compose-final
#   4. repo-specific checks: image pin parity between quadlet/, Containerfile and
#      scripts/common.sh, the settings example carries no credential, and both configuration
#      templates keep their secrets outside the repo
#
# Matches are reported as file:line only; the matched text is never printed.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$REPO"
fails=0
fail() { printf 'FAIL %s\n' "$*"; fails=$((fails + 1)); }
ok() { printf 'ok   %s\n' "$*"; }
where() { cut -d: -f1,2 | sed 's/^/     /'; }

# The vendored library and this script itself carry the patterns by nature.
mapfile -t files < <(git ls-files --cached --others --exclude-standard \
  | grep -vE '^(scripts/lib/quadlet-lib\.sh|tests/lint-repo\.sh)$' || true)
text=()
for f in "${files[@]}"; do [[ -f $f ]] && grep -Iq . "$f" 2>/dev/null && text+=("$f"); done

# ---- 1. credentials --------------------------------------------------------------------------
cred_re='(^|[^A-Za-z0-9_])[A-Z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN|_KEY)=[^[:space:]$@<"'\''`{}(%]'
hits=$(grep -nHE "$cred_re" "${text[@]}" 2>/dev/null \
  | grep -vE '(_FILE|_PATH)=' \
  | grep -viE '=[A-Za-z0-9_-]*(dummy|example|placeholder|changeme|redacted|your[_-]?)[A-Za-z0-9_-]*([[:space:]]|$)' || true)
if [[ -n $hits ]]; then fail "literal credential assignments at:"; where <<<"$hits"; else ok "no literal credential assignments"; fi

# Well-known defaults, token formats, and the two Headscale key shapes. A truncated key with an
# ellipsis is still refused: this repo does not carry key-shaped strings at all.
known='ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{32,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}'
known+='|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJhbGciOi[A-Za-z0-9_-]{20,}\.'
known+='|hskey-(auth|api|node)-[A-Za-z0-9_-]{8,}|2[A-Za-z0-9]{25}_[A-Za-z0-9]{20,}'
hits=$(grep -nHE "$known" "${text[@]}" 2>/dev/null || true)
if [[ -n $hits ]]; then fail "key-shaped or token-shaped strings at:"; where <<<"$hits"; else ok "no key-shaped or token-shaped strings"; fi

# The three live values the compose host kept outside git (ngrok token, Headplane API key and
# cookie secret) must never appear here. Only their sha256 is recorded, in the scanner.
python3 tests/leaked-value-scan.py || fail "a live host secret is in the tracked tree"

# An ngrok auth token was part of the compose deployment's .env. It must never reach this repo.
hits=$(grep -nHE 'NGROK_AUTHTOKEN[[:space:]]*[=:][[:space:]]*[^[:space:]#]' "${text[@]}" 2>/dev/null || true)
if [[ -n $hits ]]; then fail "an ngrok auth token assignment at:"; where <<<"$hits"; else ok "no ngrok auth token"; fi

# ---- 2. D1: compose files are gone -------------------------------------------------------------
left=$(printf '%s\n' "${files[@]}" | grep -E '(^|/)(docker|podman)-compose[^/]*\.ya?ml$|^compose/|^\.env\.example$|^deploy\.sh$' || true)
if [[ -n $left ]]; then fail "compose deployment files remain (D1):"; while IFS= read -r l; do printf "     %s\n" "$l"; done <<<"$left"; else ok "no compose files (D1)"; fi

# ---- 3. READMEs ---------------------------------------------------------------------------------
for r in README.md README_zh-TW.md; do
  if [[ ! -f $r ]]; then fail "$r is missing"; continue; fi
  grep -q 'scripts/install.sh' "$r" || fail "$r does not document scripts/install.sh"
  grep -q 'compose-final' "$r" || fail "$r does not point Docker users at the compose-final tag"
  grep -q 'migrate-legacy' "$r" || fail "$r does not explain why there is no migrate-legacy.sh"
  if grep -qi 'portainer' "$r"; then fail "$r still mentions Portainer"; fi
done
ok "README checks done"

# ---- 4. repo-specific ---------------------------------------------------------------------------
unit_hs=$(sed -n 's/^Image=//p' quadlet/headscale.container)
unit_hp=$(sed -n 's/^Image=//p' quadlet/headplane.container)
grep -qxF "HEADSCALE_IMAGE=$unit_hs" scripts/common.sh || fail "scripts/common.sh HEADSCALE_IMAGE differs from quadlet/headscale.container"
grep -qxF "HEADPLANE_IMAGE=$unit_hp" scripts/common.sh || fail "scripts/common.sh HEADPLANE_IMAGE differs from quadlet/headplane.container"
[[ $unit_hp == *@sha256:* ]] || fail "the headplane image is not pinned by digest: $unit_hp"
[[ $unit_hs == localhost/* ]] || fail "the headscale image is expected to be the locally built one: $unit_hs"
grep -qx 'Pull=never' quadlet/headscale.container || fail "the localhost/ image needs Pull=never"
# The Containerfile's upstream source must be the one scripts/common.sh records, digest and all.
src=$(sed -n 's/^ARG HEADSCALE_SOURCE=//p' Containerfile)
grep -qxF "HEADSCALE_SOURCE_IMAGE=$src" scripts/common.sh || fail "Containerfile HEADSCALE_SOURCE differs from scripts/common.sh"
grep -cE '^FROM .*@sha256:|^ARG HEADSCALE_SOURCE=.*@sha256:' Containerfile | grep -qx 2 \
  || fail "both Containerfile bases must be pinned by digest"
# The locally built tag must carry the headscale version it is built from.
ver=${src##*:v}; ver=${ver%@*}
[[ $unit_hs == "localhost/woow-headscale:$ver-r"* ]] || fail "the local image tag does not carry headscale $ver"
# Nothing in the settings example may look like a credential, and the file must have no inline
# comment after a value: podman --env-file would keep it as part of the value.
if grep -qE '^[A-Za-z_][A-Za-z0-9_]*=.*[^[:space:]][[:space:]]+#' config/headscale.env.example; then
  fail "config/headscale.env.example has an inline comment after a value"
fi
if grep -qE '^[A-Z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN|_KEY)=' config/headscale.env.example; then
  fail "config/headscale.env.example defines a credential key"
fi
# Both Headplane secrets come from podman secrets, never from a tracked file.
grep -q 'cookie_secret_path: "/etc/headplane/cookie-secret"' config/templates/headplane/config.yaml \
  || fail "the headplane template no longer reads the cookie secret from its mount path"
grep -q 'api_key_path: "/etc/headplane/api-key"' config/templates/headplane/config.yaml \
  || fail "the headplane template no longer reads the API key from its mount path"
for s in cookie-secret api-key; do
  if git ls-files --error-unmatch "config/$s" >/dev/null 2>&1; then fail "config/$s is tracked; it must be a podman secret"; fi
done
ok "Headscale checks done"

((fails == 0)) && echo "lint-repo: all checks passed" || echo "lint-repo: $fails check(s) failed"
((fails == 0))
