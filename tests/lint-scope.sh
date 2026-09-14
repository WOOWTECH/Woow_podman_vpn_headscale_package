#!/usr/bin/env bash
# tests/lint-scope.sh: what tests/lint-repo.sh does and does not look at.
#
# Why this exists
# ---------------
# archive/pre-quadlet-deployment/ is the compose-era tree preserved byte-identical from the
# openclaw host. The first time lint-repo.sh met it, three checks failed on preserved reference
# material: a masked key fixture, a placeholder ngrok-token assignment in documentation and the two compose
# files the archive exists to hold. Editing preserved files to please a linter would defeat the
# point of preserving them, so those collisions are exempted - as narrowly as each one allows.
#
# An exclusion like that is only safe if it is narrow, so this test pins EVERY half of it:
#
#   secret-shape gate (key/token shapes, private keys, Headscale keys)
#       reads the archive like any other tree, and is fatal there.
#       Exempt: archive/.../tests/test_validate_api_key.py, from the hskey shape ALONE -
#       that file is a masked fixture; any other shape planted in it is still fatal.
#   non-secret heuristics (ngrok placeholder, D1 compose files)
#       ignored under archive/pre-quadlet-deployment/ ... and still fatal at the repo root
#   value gates (literal credential assignments, tests/leaked-value-scan.py)
#       still read the archive, so a real secret dropped in there is still caught
#   tracked-path gate
#       the runtime secret/config files deploy.sh generates must never be tracked
#
# It also pins the D1 regex being anchored to the repo root, like its ^deploy.sh$ neighbour:
# D1 is about this repository's own compose deployment, not about the word "compose" anywhere.
#
# Each case runs lint-repo.sh in a throwaway copy of this checkout with one planted file, so the
# working tree is never touched. The copy is `cp -a`, not `git clone`: lint-repo.sh reads the
# working tree, so a clone would test the last commit rather than the tree under test.
# Nothing is started, installed or networked.
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/lint-scope.XXXXXX")
trap 'rm -rf -- "$ROOT"' EXIT
ARCHIVE=archive/pre-quadlet-deployment/tree
npass=0 nfail=0
FAILED=()

# quote <text>: reproduce lint-repo's output under a "      | " prefix
quote() { local line; while IFS= read -r line; do printf '      | %s\n' "$line"; done <<<"$1"; }

# A key-shaped string and an ngrok assignment, both built at runtime so this file does not
# carry the patterns it tests for (lint-repo.sh reads every tracked file, this one included).
key_shaped() { printf 'hskey-%s-%s\n' 'api' 'Abcdefgh1234567890'; }
# The two shapes the deleted .github/workflows/ci.yml no-secrets job scanned the whole tree for.
private_key() { printf -- '-----%s OPENSSH PRIVATE KEY-----\n' 'BEGIN'; }
# Real PGP armor puts the dashes after BLOCK, not after KEY. A pattern anchored on "KEY-----"
# misses it, which is how it slipped past once. The deleted ci.yml job named PGP explicitly.
pgp_private_key() { printf -- '-----%s PGP PRIVATE KEY BLOCK-----\n' 'BEGIN'; }
live_hskey() { printf 'hskey-%s-%s\n' 'api' 'Zq9WmPl2Kx7Rt4Nv8Bc3Hd6Fg1Js5Aw'; }
# The placeholder shape is the one actually preserved in the archive: the ngrok gate accepts no
# placeholders, while the credential gate below skips a value starting with `<`.
ngrok_placeholder() { printf 'NGROK_%s=<token>\n' 'AUTHTOKEN'; }
ngrok_value() { printf 'NGROK_%s=2%s_%s\n' 'AUTHTOKEN' 'Abcdefghijklmnopqrstuvwxy' 'zAbcdefghijklmnopqrstuv'; }
cred_line() { printf 'HEADPLANE_%s=%s\n' 'SECRET' 'sUperV4lue1234'; }

# case <name> <expect pass|fail> <planted path> <content-producing function> <expected marker>
# TRACK=1 in the environment of a call also `git add -f`s the planted file, for the gates that
# ask git whether a path is tracked rather than reading the working tree.
case_() {
  local name=$1 expect=$2 path=$3 body=$4 marker=${5:-} work out rc
  work=$ROOT/$name
  cp -a -- "$REPO" "$work" || { echo "lint-scope: could not copy $REPO"; exit 1; }
  if [[ -n $path ]]; then
    mkdir -p -- "$work/$(dirname -- "$path")"
    "$body" >"$work/$path"
    [[ ${TRACK:-0} == 1 ]] && (cd "$work" && git add -f -- "$path" >/dev/null 2>&1)
  fi
  out=$(cd "$work" && bash tests/lint-repo.sh 2>&1)
  rc=$?
  local got=pass; ((rc == 0)) || got=fail
  if [[ $got != "$expect" ]]; then
    nfail=$((nfail + 1)); FAILED+=("$name")
    printf 'FAIL  %s\n      expected lint-repo to %s, it %sed. Output:\n' "$name" "$expect" "$got"
    quote "$out"
    return
  fi
  if [[ -n $marker && $out != *"$marker"* ]]; then
    nfail=$((nfail + 1)); FAILED+=("$name")
    printf 'FAIL  %s\n      output lacks [%s]:\n' "$name" "$marker"
    quote "$out"
    return
  fi
  npass=$((npass + 1)); printf 'ok    %s\n' "$name"
}

echo "lint-scope against $REPO"

# The tree as it stands, archive and all, is clean. Everything below is measured against this.
case_ t_the_repository_as_committed_passes            pass '' : 'all checks passed'

# --- shape gates: fatal at the repo root ------------------------------------------------------
case_ t_a_compose_file_at_the_root_is_still_fatal     fail 'podman-compose.yml'      : 'compose deployment files remain'
case_ t_a_key_shape_outside_the_archive_is_fatal      fail 'docs/planted-note.md'    key_shaped 'key-shaped or token-shaped'
case_ t_an_ngrok_token_outside_the_archive_is_fatal   fail 'docs/planted-env.txt'    ngrok_placeholder 'ngrok auth token'

# --- the secret-shape gate reads the preserved tree too ----------------------------------------
# The deleted .github/workflows/ci.yml no-secrets job scanned the WHOLE tree for private-key and
# Headscale-key material, with one file excluded. Losing that over the archive was the coverage
# regression these three cases exist to prevent coming back.
case_ t_a_private_key_inside_the_archive_is_fatal     fail "$ARCHIVE/planted_id_ed25519" private_key 'key-shaped or token-shaped'
# Each armor spelling gets its own case: restoring WHERE the gate scans is not the same as
# restoring WHAT it matches, and the PGP spelling is the one that got away.
case_ t_a_pgp_private_key_inside_the_archive_is_fatal fail "$ARCHIVE/planted_pgp.asc"    pgp_private_key 'key-shaped or token-shaped'
case_ t_a_pgp_private_key_outside_the_archive_is_fatal fail 'docs/planted_pgp.asc'       pgp_private_key 'key-shaped or token-shaped'

# --- no file is exempt from the shape gate -----------------------------------------------------
# These two paths were once stripped from the file list before every gate, on the grounds that
# they "carry the patterns by nature". Measured, both scored zero hits — so the exemption bought
# nothing and hid a real key. These cases exist so it cannot come back silently.
case_ t_a_private_key_in_the_vendored_lib_is_fatal    fail 'scripts/lib/quadlet-lib.sh' private_key 'key-shaped or token-shaped'
case_ t_a_headscale_key_in_the_vendored_lib_is_fatal  fail 'scripts/lib/quadlet-lib.sh' live_hskey  'key-shaped or token-shaped'
case_ t_a_headscale_key_inside_the_archive_is_fatal   fail "$ARCHIVE/planted_key.txt"    live_hskey  'key-shaped or token-shaped'
case_ t_a_key_shape_inside_the_archive_is_fatal       fail "$ARCHIVE/planted-note.md"    key_shaped  'key-shaped or token-shaped'

# --- ... except the one masked fixture, and only for the hskey shape ---------------------------
# archive/.../tests/test_validate_api_key.py carries a masked "hskey-api-...-***" fixture: that
# is why the tree as committed passes above. The exemption is one pattern wide, not one file.
case_ t_a_private_key_in_the_masked_fixture_is_fatal  fail "$ARCHIVE/tests/test_validate_api_key.py" private_key 'key-shaped or token-shaped'

# --- non-secret heuristics: quiet inside the preserved tree ------------------------------------
case_ t_an_ngrok_token_inside_the_archive_is_ignored  pass "$ARCHIVE/planted-env.txt" ngrok_placeholder 'all checks passed'
case_ t_a_compose_file_inside_the_archive_is_ignored  pass "$ARCHIVE/podman-compose.planted.yml" : 'all checks passed'

# --- the value gate still reads the preserved tree ---------------------------------------------
# The exclusion above must not become "the archive is not scanned". A literal credential
# assignment is a value, not a shape, and is still refused wherever it is.
case_ t_a_credential_inside_the_archive_is_still_fatal fail "$ARCHIVE/planted-cred.env" cred_line 'literal credential assignments'
# and a real ngrok token, as opposed to the placeholder above, is a value: still fatal in there.
case_ t_a_live_ngrok_value_inside_the_archive_is_fatal fail "$ARCHIVE/planted-live.env" ngrok_value 'literal credential assignments'

# --- D1 is about the repository root ------------------------------------------------------------
# `^deploy.sh$` was already root-anchored; the compose pattern was not, so any path ending in
# -compose.yml tripped it. D1 removed the root deployment, and that is what it checks.
case_ t_a_compose_named_file_in_a_subdirectory_is_not_D1 pass 'docs/podman-compose.example.yml' : 'no compose files (D1)'

# --- the tracked-path gate ----------------------------------------------------------------------
# deploy.sh generates these at runtime and gitignores them; tracking one ships a real secret.
# The last five came from the deleted ci.yml no-secrets job and had no replacement until now.
secret_body() { printf 'planted\n'; }
for p in config/cookie-secret config/api-key .env \
         config/headplane/cookie-secret config/headplane/api-key \
         config/headscale/preauth-key config/headscale/config.runtime.yaml; do
  TRACK=1 case_ "t_tracked_${p//[\/.]/_}_is_fatal" fail "$p" secret_body "$p is tracked"
done

echo "----"
echo "passed: $npass  failed: $nfail"
((nfail == 0)) || { echo "failed: ${FAILED[*]}"; exit 1; }
