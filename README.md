# Headscale + Headplane on rootless Podman (Quadlet + systemd)

A self-hosted Tailscale control plane on a single machine: **Headscale 0.29.3** serving the VPN
and **Headplane 0.7.0** as its admin UI, installed as rootless Quadlet units managed by
`systemd --user`. Official Tailscale clients register against it unchanged.

繁體中文說明：[README_zh-TW.md](README_zh-TW.md)

> **Docker / podman-compose users:** the compose deployment is no longer on `main`. It is kept,
> unchanged and working, at the **[`compose-final`](../../tree/compose-final)** tag:
> `git checkout compose-final`.

---

## What gets installed

| Unit | Container | Image | Published by default |
|---|---|---|---|
| `headscale.service` | `headscale` | `localhost/woow-headscale:0.29.3-r1` (built locally) | `127.0.0.1:28080` → 8080, `127.0.0.1:29090` → 9090 |
| `headplane.service` | `headplane` | `ghcr.io/tale/headplane:0.7.0` (digest-pinned) | `127.0.0.1:23000` → 3000 |
| `headscale-network.service` | – | – | podman network `headscale-net` |
| `headscale-data-volume.service` | – | – | volume `headscale-data` (SQLite + noise key) |
| `headscale-run-volume.service` | – | – | volume `headscale-run` (the gRPC unix socket) |
| `headplane-data-volume.service` | – | – | volume `headplane-data` |

**How the two halves are split.** Headscale *is* the VPN: it holds the node database, the noise
key and the policy, and it is the only thing a Tailscale client ever talks to. Headplane is a web
UI in front of Headscale's REST API and nothing more — stop it and not one node notices. They are
two units rather than one so that:

* Headplane can be restarted, upgraded or removed without touching the control plane;
* Headplane reaches Headscale over the podman network by container name
  (`http://headscale:8080`), so the API never has to be published to the host for its benefit;
* the API key Headplane authenticates with can only be minted by a **running** Headscale, so the
  install has to start one unit, create the key, then start the other. That ordering is baked into
  `scripts/install.sh`, and into `headplane.container`'s `Requires=`/`After=` plus an
  `ExecStartPre=` loop that waits for `headscale health` at boot.

## Requirements

* Ubuntu 24.04 (or anything with **podman ≥ 4.9** and systemd 255), rootless, with linger
* `git`, `curl`, `python3`, and outbound network access on the first install (it builds an image)
* No root anywhere: run every script as the account that will own the containers, never with `sudo`

## Install

```bash
git clone https://github.com/WOOWTECH/Woow_podman_vpn_headscale_package.git
cd Woow_podman_vpn_headscale_package
scripts/install.sh                 # first run writes the settings file and stops
$EDITOR ~/.config/headscale/headscale.env
scripts/install.sh                 # builds, installs, starts, smoke-tests
```

The first run creates `~/.config/headscale/headscale.env` (mode 0600) from
`config/headscale.env.example` and stops so the settings can be reviewed. `--accept-defaults`
skips that pause. `scripts/install.sh` is idempotent: a re-run with nothing changed builds nothing,
restarts nothing and keeps both secrets.

Useful flags: `--set KEY=VALUE` (store a setting first), `--dry-run` (render, validate and report
without touching anything), `--rebuild`, `--no-build`, `--build-only`, `--rotate-api-key`,
`--no-start`, `--no-smoke`.

### Settings

Everything per-host lives in `~/.config/headscale/headscale.env` — `KEY=VALUE`, no quotes, and
**no `# comment` after a value**. It is never mounted into a container: `scripts/install.sh`
renders it into the unit files and into both containers' configuration at install time
(decision D2), which is why editing it means running `scripts/install.sh` again.

| Key | What it does |
|---|---|
| `WOOW_HEADSCALE_BIND` / `_PORT` | where the control plane is published (`127.0.0.1`, an address of this host, or `all`) |
| `WOOW_HEADSCALE_METRICS_BIND` / `_PORT` | the Prometheus endpoint |
| `WOOW_HEADPLANE_BIND` / `_PORT` | the admin UI |
| `HEADSCALE_SERVER_URL` | **the URL clients dial.** The reverse proxy or port-forward in front of this host, not this container's port |
| `HEADSCALE_BASE_DOMAIN` | MagicDNS suffix. It must not be, or contain, the `HEADSCALE_SERVER_URL` host — headscale refuses to start |
| `HEADSCALE_IPV4_PREFIX` / `_IPV6_PREFIX` | the address pools handed to nodes |
| `HEADSCALE_LOG_LEVEL` | `trace`…`error` |
| `HEADSCALE_CREATE_DEFAULT_USER` | create the headscale user `default` on the first install |
| `HEADPLANE_COOKIE_SECURE` | `true` only when something in front of Headplane terminates TLS |

Every value is validated by `scripts/render-args.sh` **before a file is written** — a bad bind
address, a port collision, a `server_url` with a path or credentials, or a base domain that would
swallow the server URL all stop the install with nothing changed. `tests/dryrun.sh` proves each of
those rejections in CI.

### Enrolling a node

```bash
podman exec headscale headscale preauthkeys create --user default --reusable --expiration 24h
# on the client:
tailscale up --login-server "$HEADSCALE_SERVER_URL" --authkey <key>
```

### External access

The Tailscale control protocol (TS2021) is a `POST` with a non-standard `Upgrade:` header followed
by a Noise handshake. **A Cloudflare tunnel breaks it** — it strips the header, and clients fail at
`/machine/register`. Ports are therefore published on loopback by default and a protocol-safe path
goes in front: a router port-forward to nginx / Traefik / NPM with `Upgrade` and `Connection`
passed through and `proxy_buffering off`. The details, with the tested compatibility matrix, are in
[`docs/EXTERNAL-ACCESS.md`](docs/EXTERNAL-ACCESS.md).

The compose deployment also carried an optional **ngrok** sidecar for bootstrapping. It is not part
of the Quadlet deployment: its free-tier URL changes on every restart, which would leave the
rendered `server_url` stale after any reboot of a unit that restarts automatically. It is still
available at the `compose-final` tag.

## Secrets

Nothing secret is stored in this repository, in `~/.config`, or in any unit file. Headplane's two
credentials are podman secrets, mounted read-only at mode 0400:

| Secret | Where it comes from | Mounted at |
|---|---|---|
| `headscale-headplane-cookie` | 32 random characters, generated by `scripts/install.sh` | `/etc/headplane/cookie-secret` |
| `headscale-headplane-api-key` | `headscale apikeys create --expiration 3650d`, run by `scripts/install.sh` once the control plane is up | `/etc/headplane/api-key` |

The API key is checked on every install against `headscale apikeys list` and re-minted when it is
missing, revoked or expired; `scripts/install.sh --rotate-api-key` forces a new one. The key never
appears in an argument list: it is captured into a shell variable and handed to the library by
variable name, and the validator reads it from stdin. `tests/smoke.sh` (check A7) asserts that
neither secret appears in `podman inspect`, process arguments, the journal, container logs or any
tracked file.

There is **no `EnvironmentFile=`** in either container: every per-host value reaches the containers
as a rendered configuration file, so nothing sensitive can leak through `podman inspect`.

## Verify

```bash
tests/smoke.sh              # units, health, published ports, HTTP, API key, secret hygiene
tests/smoke.sh --quick      # units, health, ports and HTTP only
systemctl --user status headscale.service headplane.service
journalctl --user -u headscale.service -f
```

## Upgrade

```bash
git pull && scripts/upgrade.sh
```

Backup → snapshot the installed units → `scripts/install.sh` (which builds and pulls the new pinned
images *before* touching a unit) → `tests/smoke.sh`. If install or smoke fails, the saved units are
put back and the stack is restarted on the previous images.

Versions are pinned in this repository and nowhere else — `quadlet/*.container`, `Containerfile`
and `scripts/common.sh` must agree, and `tests/lint-repo.sh` fails the build if they drift. Note
that Headscale migrates its SQLite schema on start and does **not** migrate back: rolling the units
back across a schema change is not enough on its own, so restore the pre-upgrade archive
(`scripts/restore.sh`) if that happens. Its path is printed at the start of every upgrade.

## Backup and restore

```bash
scripts/backup.sh                      # prints the archive path on stdout
scripts/backup.sh --include-secrets    # also stores the cookie secret and API key
scripts/restore.sh --archive ~/.local/share/woow-backups/headscale/headscale-<stamp>.tar \
                   --confirm-restore headscale [--restore-secrets] [--restore-config]
```

Both containers are stopped for the capture, so the SQLite database (which runs with a
write-ahead log) is one consistent point in time. The archive holds the `headscale-data` and
`headplane-data` volume exports, the rendered configuration, `metadata.json` and `SHA256SUMS`;
`headscale-run` is skipped because it only carries the gRPC unix socket.

A restore verifies the archive against its own `SHA256SUMS` before touching anything, takes a
pre-restore backup while the stack is stopped, and rolls that back automatically if the restore
fails after the first destructive step. Restored nodes keep their keys and reconnect without
re-registering, as long as `HEADSCALE_SERVER_URL` still points at this host.

## Uninstall

```bash
scripts/uninstall.sh                                     # units only; all data kept
scripts/uninstall.sh --purge --confirm-purge headscale   # also volumes, network, secrets, settings
```

`--purge` is the only way this repository deletes data, and it takes a final cold backup —
including both secrets and the settings file — before it does. Deleting `headscale-data` means
every node has to be enrolled again. Images and backups are never deleted.

## Why there is no `migrate-legacy.sh`

The other sixteen WOOWTECH podman repositories ship a `scripts/migrate-legacy.sh` that adopts a
running compose deployment in place. This one deliberately does not, because **there is nothing
running to adopt**:

* on `woowtechopenclaw`, `woow_headscale.service` is **disabled and inactive**, with no `headscale`
  or `headplane` container present; its three compose volumes are orphaned;
* no other WOOWTECH host runs the podman variant of this stack at all.

Writing an adoption path would mean shipping, and asking someone to trust, a data-migration script
that has never been run against a live deployment. Instead:

* `scripts/install.sh` **refuses to start** while any compose-era unit
  (`woow_headscale.service`, `woow_headscale_health.{service,timer}`) is active, and refuses to
  take over a container named `headscale` or `headplane` that Quadlet does not manage — it prints
  the `podman rename` command rather than letting `podman run --replace` delete it;
* the Quadlet volumes (`headscale-data`, `headscale-run`, `headplane-data`) deliberately do **not**
  reuse the compose names (`woow_headscale_*`), so an install on a host that once ran the compose
  stack is a clean, side-by-side install and the old data stays untouched;
* if data ever does have to come across from a compose host, take a `podman volume export` of
  `woow_headscale_headscale-data` there and `podman volume import` it into `headscale-data` here,
  with both units stopped.

### Coming from the compose deployment

```bash
systemctl --user disable --now woow_headscale_health.timer woow_headscale_health.service woow_headscale.service
scripts/install.sh
```

The old containers, volumes and images are left alone; remove them yourself once the Quadlet stack
has soaked.

## Image and versions

`localhost/woow-headscale` is built on the host by `scripts/install.sh` from `Containerfile`
(decision D3 — there is no WOOWTECH registry image yet; publishing to GHCR through CI is future
work). The build is a two-stage copy: the headscale binary and CA bundle come from
`docker.io/headscale/headscale:v0.29.3`, pinned by digest, on top of a digest-pinned
`debian:12.11-slim`. The Debian layer exists only to provide `/bin/sh`: Quadlet's `HealthCmd=`
becomes `podman run --health-cmd`, which podman runs through a shell, and the upstream ko image has
none. The headscale binary itself is the unmodified upstream build. The build context contains the
`Containerfile` and nothing else, so no file of this checkout can end up in an image layer.

Headplane is pulled from GHCR pinned by tag **and** digest. No floating tag is used anywhere
(decision D4).

## Security notes

* Ports are published on `127.0.0.1` by default; the admin UI and the metrics endpoint should stay
  that way and be reached through a proxy or an SSH tunnel.
* `NoNewPrivileges=true` on both containers.
* `/etc/headscale` and `/etc/headplane/config.yaml` are mounted **read-only**; `tests/smoke.sh`
  asserts it.
* `config/templates/headscale/policy.json` ships the permissive starter policy the verified
  deployment used — `accept *:*` for every node, with `autoApprovers` for RFC1918 subnet routes and
  exit nodes for the `default` user. **Tighten it before letting untrusted devices in**, then run
  `scripts/install.sh` again to install the new policy and restart the control plane.
* The metrics endpoint is unauthenticated: keep it on loopback.
* `tests/lint-repo.sh` fails the build on any credential assignment, any Headscale-key-shaped
  string, an ngrok auth token, or an inline comment after a value in the settings example.

## Layout

```
Containerfile                       the locally built headscale runtime image
quadlet/*.container|volume|network  the units, carrying @@TOKEN@@ placeholders
quadlet/render-vars                 the whitelist of tokens install.sh may substitute
config/headscale.env.example        the per-host settings, copied to ~/.config/headscale/
config/templates/**                 the two containers' configuration, rendered at install time
config/templates/render-vars        the whitelist for those templates (separate on purpose)
scripts/install.sh upgrade.sh uninstall.sh backup.sh restore.sh
scripts/common.sh headscale-helpers.sh render-args.sh validate-api-key.py
scripts/lib/quadlet-lib.sh          vendored shared library (decision D8) - never edit here
tests/dryrun.sh dryrun.local.sh     the real Quadlet generator + systemd-analyze, in CI
tests/smoke.sh lint-repo.sh test-validate-api-key.py
docs/EXTERNAL-ACCESS.md             how to expose the control plane without breaking TS2021
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| A client registers but never connects | `HEADSCALE_SERVER_URL` is not the address the client can reach. Fix it and run `scripts/install.sh` again |
| `500` on `/machine/register` | Something in front is rewriting the HTTP upgrade — a Cloudflare tunnel does. See `docs/EXTERNAL-ACCESS.md` |
| headscale exits on start | `HEADSCALE_BASE_DOMAIN` contains the `HEADSCALE_SERVER_URL` host. The installer refuses this; a hand-edited installed config does not |
| The Headplane login bounces back to itself | `HEADPLANE_COOKIE_SECURE=true` without TLS in front |
| `headplane.service` restarts in a loop | Its API key is gone. Run `scripts/install.sh --rotate-api-key` |
| `install.sh` refuses to start | A compose-era unit is still active, or a foreign container owns the name. The message prints the exact command |

## Sibling repositories

* [`Woow_k3s_vpn_headscale_package`](https://github.com/WOOWTECH/Woow_k3s_vpn_headscale_package) — the multi-tenant Kubernetes/K3s edition
* [`Woow_ha_vpn_headscale_package`](https://github.com/WOOWTECH/Woow_ha_vpn_headscale_package) — the Home Assistant OS add-on
* [`Woow_podman_vpn_tailscale_package`](https://github.com/WOOWTECH/Woow_podman_vpn_tailscale_package) — a tailnet *node* (the client side) on the same Quadlet standard
