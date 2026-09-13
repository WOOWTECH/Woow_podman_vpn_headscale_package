# Changelog

## Unreleased — Quadlet + systemd as the primary deployment (v1)

### Added
- `quadlet/` — `headscale.container`, `headplane.container`, `headscale.network` and three
  `.volume` units, rendered at install time from `@@TOKEN@@` placeholders whitelisted in
  `quadlet/render-vars` (decision D2).
- `config/headscale.env.example` — the per-host settings, installed to
  `~/.config/headscale/headscale.env` (mode 0600). No credential lives here.
- `config/templates/**` — the Headscale and Headplane configuration files, rendered at install
  time from the same settings under their own whitelist.
- `Containerfile` — the locally built `localhost/woow-headscale` image (decision D3), a
  digest-pinned upstream headscale binary on a digest-pinned Debian base, which supplies the
  `/bin/sh` that `HealthCmd=` needs.
- `scripts/install.sh`, `upgrade.sh` (backup → build/pull → restart → smoke → automatic
  rollback), `uninstall.sh` (`--purge` is the only way data is deleted), `backup.sh`,
  `restore.sh`, plus `common.sh`, `headscale-helpers.sh`, `render-args.sh` and
  `validate-api-key.py`.
- `scripts/lib/quadlet-lib.sh` — the shared library, vendored unmodified (decision D8).
- `tests/dryrun.sh` (the real podman 4.9.3 Quadlet generator plus `systemd-analyze --user
  verify`), `tests/dryrun.local.sh`, `tests/smoke.sh`, `tests/lint-repo.sh`,
  `tests/leaked-value-scan.py`, `tests/test-validate-api-key.py`.
- `.github/workflows/quadlet-ci.yml` and `.github/workflows/repo-checks.yml`.

### Changed
- Both READMEs are Quadlet-first, and explain why this repository has no `migrate-legacy.sh`.
- Ports are published on `127.0.0.1` by default. The compose deployment published the control
  plane on `0.0.0.0`.
- Headplane's cookie secret and API key are podman secrets instead of files under `runtime/`.
- `docs/DEPLOYMENT-REPORT.md` is marked historical, and its truncated pre-auth key fragment is
  redacted.

### Removed
- `podman-compose.yml`, `deploy.sh`, `.env.example`, the pre-rendered `config/headscale/*` and
  `config/headplane/*`, and the compose-era `.github/workflows/ci.yml` (decision D1). The whole
  compose deployment is kept at the **`compose-final`** tag.
- The optional ngrok sidecar. Its free-tier URL changes on every restart, which would leave the
  rendered `server_url` stale after a unit restart. Still available at `compose-final`.
- The `woow_headscale_health.{service,timer}` fallback health scheduler: podman's native
  healthcheck timer does that job under Quadlet.
