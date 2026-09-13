# shellcheck shell=bash
# scripts/render-args.sh: the values computed from ~/.config/headscale/headscale.env, plus the
# validation of everything that is rendered straight from it. Sourced by scripts/install.sh and
# tests/dryrun.sh, so CI renders and validates exactly what a host gets.
#
# render_args <envfile>: QL_ENV is already loaded from <envfile>; sets RENDER_ARGS=(KEY=VALUE...).
# It dies on a bad value, before any file is written.

# _ra_publish <bind> <port>: the PublishPort= host part. "all" has to drop the address, because
# "0.0.0.0:PORT:PORT" would bind IPv4 only while a bare "PORT:PORT" covers both families.
_ra_publish() {
  if [[ $1 == all ]]; then printf '%s' "$2"; else printf '%s:%s' "$1" "$2"; fi
}

# _ra_bind_port <KEY prefix>: validate WOOW_<prefix>_BIND / _PORT and set RA_BIND / RA_PORT.
# It must not print its answer through a command substitution: ql_assert_match and ql_die exit,
# and an exit inside $(...) only ends the subshell - a bad value would then be accepted with an
# empty bind and port. So the result comes back in two variables.
_ra_bind_port() {
  RA_BIND=$(ql_env_get "WOOW_${1}_BIND")
  ql_assert_match "WOOW_${1}_BIND" "$RA_BIND" 'all|[0-9]{1,3}(\.[0-9]{1,3}){3}'
  RA_PORT=$(ql_env_get "WOOW_${1}_PORT")
  ql_assert_match "WOOW_${1}_PORT" "$RA_PORT" '[0-9]{1,5}'
  ((RA_PORT >= 1 && RA_PORT <= 65535)) || ql_die "WOOW_${1}_PORT: $RA_PORT is not a TCP port"
}

# _ra_overlap <bindA> <portA> <bindB> <portB>: true when the two host bindings collide. Different
# ports never collide; the same port collides when either side binds every address.
_ra_overlap() {
  [[ $2 == "$4" ]] || return 1
  [[ $1 == all || $3 == all || $1 == "$3" ]]
}

# _ra_host_of <url>: the hostname of an http(s) origin URL, lowercased and without the port
_ra_host_of() {
  local rest=${1#*://}
  rest=${rest%%/*}
  rest=${rest%%\?*}
  rest=${rest##*@}
  printf '%s' "${rest%%:*}" | tr '[:upper:]' '[:lower:]'
}

render_args() {
  local hs_bind hs_port mx_bind mx_port hp_bind hp_port url host base
  local RA_BIND RA_PORT
  _ra_bind_port HEADSCALE;         hs_bind=$RA_BIND hs_port=$RA_PORT
  _ra_bind_port HEADSCALE_METRICS; mx_bind=$RA_BIND mx_port=$RA_PORT
  _ra_bind_port HEADPLANE;         hp_bind=$RA_BIND hp_port=$RA_PORT
  if _ra_overlap "$hs_bind" "$hs_port" "$mx_bind" "$mx_port" \
    || _ra_overlap "$hs_bind" "$hs_port" "$hp_bind" "$hp_port" \
    || _ra_overlap "$mx_bind" "$mx_port" "$hp_bind" "$hp_port"; then
    ql_die "two of the three published host bindings are the same address and port; give each of
WOOW_HEADSCALE_PORT, WOOW_HEADSCALE_METRICS_PORT and WOOW_HEADPLANE_PORT its own port"
  fi

  # An http(s) origin: scheme, host, optional port. No credentials, path, query or fragment -
  # headscale joins its own paths onto server_url, and a client given a path never registers.
  url=$(ql_env_get HEADSCALE_SERVER_URL)
  ql_assert_match HEADSCALE_SERVER_URL "$url" 'https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[1-9][0-9]{0,4})?'
  # A MagicDNS suffix: dotted DNS labels, at least one dot, no trailing dot.
  base=$(ql_env_get HEADSCALE_BASE_DOMAIN | tr '[:upper:]' '[:lower:]')
  ql_assert_match HEADSCALE_BASE_DOMAIN "$base" '[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+'
  # headscale refuses to start when its own hostname resolves inside the tailnet.
  host=$(_ra_host_of "$url")
  if [[ $host == "$base" || $host == *".$base" ]]; then
    ql_die "HEADSCALE_SERVER_URL host '$host' is inside HEADSCALE_BASE_DOMAIN '$base'. headscale
refuses that: its own URL would resolve through MagicDNS. Pick an unrelated base domain"
  fi

  ql_assert_match HEADSCALE_IPV4_PREFIX "$(ql_env_get HEADSCALE_IPV4_PREFIX)" '[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}'
  ql_assert_match HEADSCALE_IPV6_PREFIX "$(ql_env_get HEADSCALE_IPV6_PREFIX)" '[0-9a-fA-F:]+/[0-9]{1,3}'
  ql_assert_match HEADSCALE_LOG_LEVEL "$(ql_env_get HEADSCALE_LOG_LEVEL)" 'trace|debug|info|warn|error'
  ql_assert_match HEADSCALE_CREATE_DEFAULT_USER "$(ql_env_get HEADSCALE_CREATE_DEFAULT_USER)" 'true|false'
  ql_assert_match HEADPLANE_COOKIE_SECURE "$(ql_env_get HEADPLANE_COOKIE_SECURE)" 'true|false'
  # A secure cookie is dropped by the browser over plain HTTP, and Headplane speaks HTTP inside
  # the container: the admin UI would accept the password and bounce straight back to the login.
  if [[ $(ql_env_get HEADPLANE_COOKIE_SECURE) == true && $url == http://* ]]; then
    ql_warn "HEADPLANE_COOKIE_SECURE=true with a plain-http HEADSCALE_SERVER_URL: set it only when
something in front of Headplane terminates TLS, otherwise the admin login loops"
  fi

  # shellcheck disable=SC2034 # RENDER_ARGS is read by the caller
  RENDER_ARGS=(
    "HEADSCALE_PUBLISH=$(_ra_publish "$hs_bind" "$hs_port")"
    "HEADSCALE_METRICS_PUBLISH=$(_ra_publish "$mx_bind" "$mx_port")"
    "HEADPLANE_PUBLISH=$(_ra_publish "$hp_bind" "$hp_port")"
  )
}
