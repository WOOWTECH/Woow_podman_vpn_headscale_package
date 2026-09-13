# shellcheck shell=bash
# tests/dryrun.local.sh: Headscale-specific assertions. Sourced at the end of tests/dryrun.sh
# (vendored), which provides run_variant, render_variant, $WORK, $REPO, $base and $failures.
# shellcheck disable=SC2154 # the variables above are defined by tests/dryrun.sh

check() { # check <description> <command...>
  if "${@:2}"; then echo "ok   $1"; else echo "FAIL $1"; failures=$((failures + 1)); fi
}
has_line() { grep -qxF -- "$3" "$WORK/$1/out/$2"; }

# ---- the rendered units -------------------------------------------------------------------------
check "example publishes headscale on loopback only" has_line example headscale.container 'PublishPort=127.0.0.1:28080:8080'
check "example publishes the metrics on loopback only" has_line example headscale.container 'PublishPort=127.0.0.1:29090:9090'
check "example publishes headplane on loopback only" has_line example headplane.container 'PublishPort=127.0.0.1:23000:3000'
check "BIND=all drops the host address" has_line fixture-lan headscale.container 'PublishPort=8080:8080'
check "a per-host address is kept" has_line fixture-lan headplane.container 'PublishPort=192.168.2.191:23000:3000'
check "the reverse-proxy fixture still publishes on loopback" has_line fixture-proxy headscale.container 'PublishPort=127.0.0.1:28080:8080'
check "headplane mounts both secrets, never a repo file" bash -c "grep -qx 'Secret=headscale-headplane-cookie,type=mount,target=/etc/headplane/cookie-secret,mode=0400' '$WORK/example/out/headplane.container' && grep -qx 'Secret=headscale-headplane-api-key,type=mount,target=/etc/headplane/api-key,mode=0400' '$WORK/example/out/headplane.container'"
check "headscale reads its configuration read-only" has_line example headscale.container 'Volume=%h/.config/headscale/headscale:/etc/headscale:ro,Z'
check "no EnvironmentFile= reaches a container (both secrets are podman secrets)" \
  bash -c "! grep -q '^EnvironmentFile=' '$WORK'/example/out/*.container"
check "the locally built image is never pulled" has_line example headscale.container 'Pull=never'
check "headplane is ordered after headscale" has_line example headplane.container 'Requires=headscale.service'
check "the ExecStartPre readiness loop uses no shell variable (systemd would expand it)" \
  bash -c "! grep -E '^ExecStartPre=' '$WORK/example/out/headplane.container' | grep -q '[\$]'"

# ---- the rendered container configuration ---------------------------------------------------------
render_cfg() { # render_cfg <variant> <envfile>
  local out=$WORK/$1/cfg
  rm -rf "$out"
  mkdir -p "$out/headscale" "$out/headplane"
  ql_render "$REPO/config/templates/headscale" "$2" "$REPO/config/templates/render-vars" "$out/headscale"
  ql_render "$REPO/config/templates/headplane" "$2" "$REPO/config/templates/render-vars" "$out/headplane"
}
if (render_cfg example "$REPO/config/headscale.env.example") >/dev/null 2>&1; then
  echo "ok   the example settings render both configuration files"
  cfg=$WORK/example/cfg
  check "server_url comes from the settings" grep -qx 'server_url: http://localhost:28080' "$cfg/headscale/config.yaml"
  check "the MagicDNS base domain is rendered" grep -qx '  base_domain: ts.local' "$cfg/headscale/config.yaml"
  check "headscale listens on every address inside its own namespace" grep -qx 'listen_addr: "0.0.0.0:8080"' "$cfg/headscale/config.yaml"
  check "no token survives in the rendered headscale config" bash -c "! grep -q '@@' '$cfg/headscale/config.yaml'"
  check "headplane reaches headscale by container name, not by a published port" \
    grep -qx '  url: "http://headscale:8080"' "$cfg/headplane/config.yaml"
  check "headplane reads both secrets from their mount paths" bash -c \
    "grep -qx '  cookie_secret_path: \"/etc/headplane/cookie-secret\"' '$cfg/headplane/config.yaml' && grep -qx '  api_key_path: \"/etc/headplane/api-key\"' '$cfg/headplane/config.yaml'"
  check "no token survives in the rendered headplane config" bash -c "! grep -q '@@' '$cfg/headplane/config.yaml'"
else
  echo "FAIL the example settings render both configuration files"
  failures=$((failures + 1))
fi
if (render_cfg proxy "$REPO/tests/fixtures/proxy.env") >/dev/null 2>&1; then
  check "a TLS front end renders cookie_secure: true" grep -qx '  cookie_secure: true' "$WORK/proxy/cfg/headplane/config.yaml"
  check "an https server_url is rendered verbatim" grep -qx 'server_url: https://vpn.example.com' "$WORK/proxy/cfg/headscale/config.yaml"
else
  echo "FAIL the proxy fixture renders"
  failures=$((failures + 1))
fi

# ---- settings that must be refused before anything is written ---------------------------------------
reject_n=0
reject_env() { # reject_env <description> <envfile> <KEY> <bad value>
  local dir
  reject_n=$((reject_n + 1))
  dir=$WORK/bad-$reject_n
  mkdir -p "$dir/src" "$dir/out"
  sed "s|^$3=.*|$3=$4|" "$2" >"$dir/env"
  grep -qx -- "$3=$4" "$dir/env" || { echo "FAIL $1: the fixture line $3 was not replaced"; failures=$((failures + 1)); return; }
  cp -p -- "${base[@]}" "$dir/src/"
  if (render_variant "$dir/src" "$dir/env" "$dir/out") >/dev/null 2>&1; then
    echo "FAIL $1 was accepted"
    failures=$((failures + 1))
  else
    echo "ok   $1 is refused"
  fi
}
reject() { reject_env "$1" "$REPO/config/headscale.env.example" "$2" "$3"; }

reject "a bind address that is not an IPv4 address or 'all'" WOOW_HEADSCALE_BIND 'example.com'
reject "a port above 65535" WOOW_HEADPLANE_PORT '70000'
reject "a port that is not a number" WOOW_HEADPLANE_PORT 'http'
reject "a server_url with a path" HEADSCALE_SERVER_URL 'http://vpn.example.com/headscale'
reject "a server_url with credentials" HEADSCALE_SERVER_URL 'http://user:pw@vpn.example.com'
reject "a server_url that is not http(s)" HEADSCALE_SERVER_URL 'vpn.example.com:8080'
reject "a log level headscale does not have" HEADSCALE_LOG_LEVEL 'verbose'
reject "a non-boolean cookie_secure" HEADPLANE_COOKIE_SECURE 'yes'
reject "an IPv4 prefix that is not a CIDR" HEADSCALE_IPV4_PREFIX '100.64.0.0'
reject "a headplane port that collides with headscale" WOOW_HEADPLANE_PORT '28080'
# The server_url host must not live inside the MagicDNS base domain: headscale refuses to start,
# because its own URL would resolve through the tailnet.
reject_env "a base domain that swallows the server_url host" "$REPO/tests/fixtures/proxy.env" \
  HEADSCALE_BASE_DOMAIN 'example.com'
