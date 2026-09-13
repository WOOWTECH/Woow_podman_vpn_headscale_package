# `archive/pre-quadlet-deployment/` — the openclaw compose-era deployment layer

**Reference material. Not installed, not executed, not tested, and not the deployment path of this
repository.** The current deployment is `./deploy.sh` + `podman-compose.yml` at the repository root.
Everything under `tree/` is a **different, self-consistent compose design** that ran on one host; it
is kept so the work is not lost, and it must not be mixed into the root deployment.

本目錄為**歷史參考資料**，不是本倉庫的部署路徑，請勿執行；實際部署請用根目錄的 `./deploy.sh`。

`tree/` is a byte-identical copy of the host directory, minus the files listed under
*What was excluded* below. No preserved file was edited.

## Where it came from / 來源

Host `woowtechopenclaw`, directory `~/Woow_vpn_headscale_package`.

That directory was assumed to be a clone; it is not. It has **no `.git` at all** — no remote, no
history, no bundle — and the SHA recorded in its `.deployed-commit` (preserved in `tree/`) resolves
in no repository in the organisation. A file-by-file comparison against this repository's `main`
and an org-wide code search found the material below in **no git repository anywhere**.

The archived `Woow_vpn_headscale_package` repository was split by platform on 2026-09-11 into
`Woow_podman_vpn_headscale_package` (this repository), `Woow_k3s_vpn_headscale_package` and
`Woow_ha_vpn_headscale_package`. The host tree is the **podman** variant — it ships
`podman-compose.yml`, `podman-compose.ngrok.yml` and `deploy.sh` — so this repository is its home.
The split took the compose file, the configs, the docs and the screenshots; it did not take the
scripts, the systemd units, the tests, the Containerfile or the ngrok overlay.

Headscale is **not running** on that host (service disabled, no containers), so unlike the other
four openclaw trees this one is not live code — it is the last state of a deployment that was
stopped.

Extracted from the local backup
`~/.local/share/woow-openclaw-deployment-backup/20260913-234957/deployment-layer.tgz`
(sha256 `13b07d18c3f511e602f63ba4bcff17705cca5bcdd69ef063cf136ac0bf87778f`, captured 2026-09-13).
Nothing was read from, or changed on, the host.

## Why it is archived rather than merged into the repository root

This repository has no Quadlet layer yet, so there was a real question of whether some of this
belongs in the repository proper. It does not, and the reason is concrete: **the host tree and the
repository root implement two different designs, and neither is a superset of the other.**

| | repository root (current) | `tree/` (preserved) |
|---|---|---|
| image | `docker.io/headscale/headscale:v0.29.2` | `localhost/woow-headscale:0.29.3`, built from `Containerfile.headscale` |
| rendered config | `config/headscale/config.runtime.yaml` | `runtime/headscale/config.yaml` |
| renderer | `deploy.sh` (5 KB, `sed`) | `deploy.sh` (21 KB) + `scripts/runtime_config.py` (18 KB, strict parser) |
| ports | literal `28080:8080` | `${HEADSCALE_HOST_BIND_ADDR}:${HEADSCALE_HOST_PORT}:${HEADSCALE_PORT}` |
| `.env` keys | 1 (`SERVER_URL`) | 24 |
| pod | `x-podman: in_pod: true` | ownership labels, no pod |
| lifecycle | deploy only | deploy, backup, restore, remove, verify, systemd units, 12 pytest files |

`Containerfile.headscale`, `podman-compose.ngrok.yml` and the two `config.template.yaml` files are
not "missing pieces" of the root deployment — they are parts of the *other* design, and the root
`deploy.sh` and `podman-compose.yml` reference none of them. Cherry-picking any of them into the
root would leave this repository with two half-wired deployments, which is the exact collision the
`archive/` convention exists to prevent, and it would land on top of the Quadlet rewrite that is
next for this repository.

The `docs/` on the two sides have also genuinely diverged rather than one being newer:
`docs/EXTERNAL-ACCESS.md` here carries a paragraph on validating a reverse proxy with a real client
that the root copy lacks, while the root copy has a refined nginx `map` block that this one lacks.
The root docs were left untouched; both host copies are preserved here.

## What it deployed / 部署內容

Headscale (control plane) + Headplane (admin UI) via `podman-compose`, with headscale built locally
from `Containerfile.headscale` so it carries a native `HEALTHCHECK`, published on
`${HEADSCALE_HOST_PORT}` with metrics and the admin UI defaulting to loopback, plus an optional
ngrok overlay (`podman-compose.ngrok.yml`) for external access. `systemd/woow_headscale.service`
with a `…_health.service` + `.timer` pair drove it.

The substance of the tree is the layer around the compose file, about 6 400 lines:

* `scripts/runtime_config.py` (18 KB) — a strict `.env` parser and config renderer that refuses
  shell quoting, expansion and inline comments, and validates hostnames, prefixes and log levels.
* `scripts/archive_security.py` — backup-archive validation.
* `scripts/validate_api_key.py` — knows the real Headplane API-key shape
  (`hskey-api-<12>-<64>`) and never prints more than a masked prefix.
* `scripts/cookie_secret.py` — cookie-secret generation and the legacy-format migration.
* `scripts/port_bindings.py`, `volume_ownership.py`, `render_systemd_unit.py`, and
  `backup.sh` / `restore.sh` / `remove.sh` / `verify.sh` (15 KB).
* **12 pytest files, about 3 900 lines**, including a 42 KB maintenance contract and a 28 KB deploy
  contract.

## What looks reusable / 可再利用之處

`tree/tests/fixtures/` is hand-captured **real podman output** and is the reason this tree matters:

* `podman-inspect-empty-host-ip.json` — a published port as podman actually prints it:
  `{"HostIp": "", "HostPort": "28080"}`. The key is `HostIp` (podman's JSON tag, not the Go field
  `HostIP`), and a catch-all bind is the **empty string** — not `0.0.0.0`, and not an absent key.
  This exact shape has caused shipped bugs across the WOOWTECH podman repositories.
* `podman-container-inspect-native-healthcheck.json` vs
  `podman-container-inspect-shell-healthcheck.json` — `["CMD", …]` against `["CMD-SHELL", …]`, with
  `Interval` and `Timeout` in **nanoseconds**. A double that emits seconds, or that assumes one
  `Test` form, passes tests that a real container fails.
* `podman-4.9-image-inspect-no-healthcheck.json` — an image whose `Config` has **no `Healthcheck`
  key at all**, which is how podman reports an image without one; a double that supplies an empty
  dict hides the case the code has to handle.

**Assessment for the current repository.** The root has no tests at all — CI is `bash -n` on
`deploy.sh`, ShellCheck, and two `git grep` secret gates. So every one of these could strengthen it,
and the `HostIp` fixture is the highest-value one: the root `deploy.sh` and `podman-compose.yml`
publish ports, and nothing offline checks that the published bind is what was asked for.
**Deliberately not done in this change** — preservation first, and the Quadlet rewrite that comes
next will decide the shape of the test suite these fixtures should plug into.

## What was excluded / 已排除的內容

No preserved file was modified; files that could not be preserved were dropped whole rather than
edited, so nothing under `tree/` is a partial or doctored version of a host file.

**Excluded for secrets (4 files)**

* `.env` — the host environment file. It carried a **populated `NGROK_AUTHTOKEN` (49 characters)**,
  directly against its own comment "Never commit a populated NGROK_AUTHTOKEN". **Treat that token as
  disclosed and rotate it.** The preserved `tree/.env.example` carries the identical 24-key list
  with placeholder values, so nothing but the values is lost.
* `runtime/headplane/api-key` — a live Headplane API key.
* `runtime/headplane/cookie-secret` — a live session cookie secret.
* `runtime/ngrok-api.json` — the live ngrok tunnel record (tunnel id and public URL).

**Excluded as worthless (15 files)**

* `backups/woow-headscale-20260826T222125Z.tar.gz` — a runtime archive of live headscale state. Its
  format is fully described by the preserved `scripts/backup.sh`, `restore.sh` and
  `archive_security.py`.
* `runtime/headplane/config.yaml`, `runtime/headscale/config.yaml`,
  `runtime/headscale/extra_records.json` — host runtime state rendered by `deploy.sh` from the
  preserved templates.
* Eleven `__pycache__/*.pyc` bytecode caches.

**Excluded as already present (13 files)** — byte-identical to files already on `main`:
`config/headplane/config.yaml`, `config/headscale/config.yaml`, `config/headscale/policy.json`,
`docs/DEPLOYMENT-REPORT.md`, and the nine `docs/screenshots/*.png`.

A value-shaped secret scan over `tree/` (GitHub PATs, `sk-` keys, Slack tokens, AWS key ids, PEM
private keys and certificates, JWTs, tailscale/headscale auth keys, bcrypt and crypt hashes, and
non-placeholder `*PASSWORD|SECRET|TOKEN|_KEY=` assignments) reports **0 hits**. Five
assignment-shaped matches were read by hand and are all regular-expression definitions inside
`scripts/cookie_secret.py`, `runtime_config.py` and `validate_api_key.py`, plus the empty
`NGROK_AUTHTOKEN=` line of `.env.example`.
