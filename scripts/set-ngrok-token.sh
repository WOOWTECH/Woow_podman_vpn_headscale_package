#!/usr/bin/env bash
# Read an ngrok authtoken from stdin without putting it in argv, environment,
# shell history, repository files, or the rendered Quadlet.
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
umask 077
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/quadlet-lib.sh
. "$REPO/scripts/lib/quadlet-lib.sh"
# shellcheck source=common.sh
. "$REPO/scripts/common.sh"

ql_require_rootless
tmp=$(mktemp "${TMPDIR:-/tmp}/headscale-ngrok-token.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
tr -d '\r\n' >"$tmp"
[[ -s $tmp ]] || ql_die "empty ngrok token on stdin"
ql_secret_ensure "$SECRET_NGROK_TOKEN" "file:$tmp" --replace
ql_info "stored $SECRET_NGROK_TOKEN as a Podman secret; run scripts/install.sh after enabling ngrok"
