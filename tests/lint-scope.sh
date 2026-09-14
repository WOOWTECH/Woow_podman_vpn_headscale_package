#!/usr/bin/env bash
# tests/lint-scope.sh: what tests/lint-repo.sh does and does not look at.
#
# Why this exists
# ---------------
# archive/pre-quadlet-deployment/ is the compose-era tree preserved byte-identical from the
# openclaw host. The first time lint-repo.sh met it, three checks failed on preserved reference
# material: a masked key fixture, a placeholder ngrok-token assignment in documentation and the two compose
# files the archive exists to hold. Editing preserved files to please a linter would defeat the
# point of preserving them, so the three SHAPE heuristics skip that tree.
#
# An exclusion like that is only safe if it is narrow, so this test pins BOTH halves of it:
#
#   shape gates (key/token shapes, ngrok token, D1 compose files)
#       ignored under archive/pre-quadlet-deployment/ ... and still fatal at the repo root
#   value gates (literal credential assignments, tests/leaked-value-scan.py)
#       still read the archive, so a real secret dropped in there is still caught
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
# The placeholder shape is the one actually preserved in the archive: the ngrok gate accepts no
# placeholders, while the credential gate below skips a value starting with `<`.
ngrok_placeholder() { printf 'NGROK_%s=<token>\n' 'AUTHTOKEN'; }
ngrok_value() { printf 'NGROK_%s=2%s_%s\n' 'AUTHTOKEN' 'Abcdefghijklmnopqrstuvwxy' 'zAbcdefghijklmnopqrstuv'; }
cred_line() { printf 'HEADPLANE_%s=%s\n' 'SECRET' 'sUperV4lue1234'; }

# case <name> <expect pass|fail> <planted path> <content-producing function> <expected marker>
case_() {
  local name=$1 expect=$2 path=$3 body=$4 marker=${5:-} work out rc
  work=$ROOT/$name
  cp -a -- "$REPO" "$work" || { echo "lint-scope: could not copy $REPO"; exit 1; }
  if [[ -n $path ]]; then
    mkdir -p -- "$work/$(dirname -- "$path")"
    "$body" >"$work/$path"
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

# --- shape gates: quiet inside the preserved tree ----------------------------------------------
case_ t_a_key_shape_inside_the_archive_is_ignored     pass "$ARCHIVE/planted-note.md" key_shaped 'all checks passed'
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

echo "----"
echo "passed: $npass  failed: $nfail"
((nfail == 0)) || { echo "failed: ${FAILED[*]}"; exit 1; }
