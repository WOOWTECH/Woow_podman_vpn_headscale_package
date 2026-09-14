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
- `tests/health-gate.sh` — the readiness gate under stubbed `podman`/`curl`: it passes while
  `.State.Health.Status` is stuck at `starting`, and still fails when headscale is down or answers
  `/health` with anything but 200. Wired into `.github/workflows/repo-checks.yml`.
- `tests/lock-release.sh` — vendored with the library: a script that ends normally must give the
  per-app lock back, and no script may carry a private lock-held flag or a bare `trap ... EXIT`
  after taking the lock.
- `tests/lint-scope.sh` — pins what `tests/lint-repo.sh` looks at: the secret-shape gate reads the
  preserved `archive/pre-quadlet-deployment/` tree and is fatal there (one file is exempt from the
  `hskey-` shape alone: the masked `tests/test_validate_api_key.py` fixture); the two non-secret
  heuristics (the ngrok placeholder, the D1 compose scan) skip that tree and stay fatal at the
  repository root; the value gates (literal credentials, `tests/leaked-value-scan.py`) and the
  tracked-path gate read everything.
- `.github/workflows/quadlet-ci.yml` and `.github/workflows/repo-checks.yml`.

### Changed
- The vendored `scripts/lib/quadlet-lib.sh` is **1.6.0** (`ql_unlock`, `ql_cleanup`,
  `ql_cleanup_clear`, the lock-fd and exec-resume fixes), the same copy the sibling packages
  carry, together with the shared `tests/dryrun.sh`, `tests/lock-release.sh` and
  `.github/workflows/quadlet-ci.yml`. All four are listed in `scripts/lib/quadlet-lib.manifest`
  and CI checks them with `sha256sum -c`.
- The per-app lock is taken with `ql_lock "$APP"` directly. The local `app_lock` wrapper and its
  private `WOOW_LOCK_HELD` flag are gone: the flag had no liveness check and defeated the
  library's exec-resume path when `upgrade.sh` hands off to `install.sh`. Tidy-up in
  `install.sh`, `backup.sh` and `restore.sh` is registered with `ql_cleanup` instead of
  `trap ... EXIT`, which used to replace the handler `ql_lock` arms and leave the lock directory
  behind after a clean run.
- `tests/lint-repo.sh` is aware of `archive/pre-quadlet-deployment/` (added on `main` after this
  branch forked). Its ngrok-token and D1 compose scans skip the preserved tree instead of demanding
  edits to files kept byte-identical on purpose — the same reasoning `main`'s ShellCheck
  `ignore_paths: archive` recorded. Its **secret-shape** scan does not skip it: a private key or a
  live Headscale key planted under the archive is as dangerous as one at the root, so only the one
  file that actually collides is exempt — `archive/pre-quadlet-deployment/tree/tests/`
  `test_validate_api_key.py`, a masked `hskey-api-…-***` fixture — and only from the `hskey-`
  pattern; every other shape is still fatal in that file.
  (An earlier revision of this branch excluded the whole archive from every shape gate. That lost
  real coverage relative to the deleted `.github/workflows/ci.yml`; it is corrected here and pinned
  by `tests/lint-scope.sh`.)
- `tests/lint-repo.sh` also refuses the full set of runtime secret/config paths the deleted
  `ci.yml` no-secrets job refused — `.env`, `config/headplane/cookie-secret`,
  `config/headplane/api-key`, `config/headscale/preauth-key`,
  `config/headscale/config.runtime.yaml` — alongside this deployment's `config/cookie-secret` and
  `config/api-key`.
- The D1 compose pattern is now anchored to the repository root like its `^deploy.sh$` neighbour.
- Readiness is judged by `hs_wait_ready` (`scripts/headscale-helpers.sh`) everywhere — in
  `install.sh`, `upgrade.sh`'s rollback check, `backup.sh`'s restart and `tests/smoke.sh`'s A2.
  It polls headscale's HTTP `/health` endpoint, which is the authority, and runs
  `podman healthcheck run headscale` once as corroboration (a mismatch is a warning, not a veto).
  Nothing waits passively on `.State.Health.Status` any more: on a host whose podman healthcheck
  timers never fire for Quadlet-started containers that status never leaves `starting`, so the old
  gate spent its 300 s timeout and then aborted an install of a control plane that was serving.
  `scripts/common.sh` additionally exports `QL_HEALTH_ACTIVE=1`, so the library wait that remains
  (headplane, which has no `HealthCmd=`) runs the check instead of watching it; the vendored 1.6.0
  library defaults that off and 1.7.0 defaults it on, which makes the export a no-op later.
- Both READMEs are Quadlet-first, and explain why this repository has no `migrate-legacy.sh`.
- Ports are published on `127.0.0.1` by default. The compose deployment published the control
  plane on `0.0.0.0`.
- Headplane's cookie secret and API key are podman secrets instead of files under `runtime/`.
- `docs/DEPLOYMENT-REPORT.md` is marked historical, and its truncated pre-auth key fragment is
  redacted.

### Removed
- `podman-compose.yml`, `deploy.sh`, `.env.example`, the pre-rendered `config/headscale/*` and
  `config/headplane/*`, and the compose-era `.github/workflows/ci.yml` (decision D1). The whole
  compose deployment is kept at the **`compose-final`** tag. Its `bash -n` step covered only
  `deploy.sh`, which this branch removes; its ShellCheck job is replaced by `quadlet-ci.yml`'s
  (`scripts/**/*.sh tests/*.sh` — every shell script this repository owns, the preserved tree
  excluded either way).
  Its **no-secrets** job is replaced by `tests/lint-repo.sh` plus `tests/leaked-value-scan.py` —
  but only after the fixes recorded under *Changed* above. As first written, this branch claimed
  that job was "a subset" of the new tests and that nothing was lost. **That was wrong**: the new
  linter excluded the entire archive from its shape gates while the deleted job scanned the whole
  tree with one file excluded, and it checked two of the job's five tracked runtime paths. Both
  gaps are closed, so the replacement is now a genuine superset: the deleted job's whole-tree
  private-key and `hskey-` scans and its full five-path tracked-file gate are all present, and the
  new tests additionally refuse other key/token shapes and the three live values the compose host
  kept outside git.
- The optional ngrok sidecar. Its free-tier URL changes on every restart, which would leave the
  rendered `server_url` stale after a unit restart. Still available at `compose-final`.
- The `woow_headscale_health.{service,timer}` fallback health scheduler. It is **not** replaced by
  podman's native healthcheck timer, which an earlier draft of this entry claimed: on at least one
  target host (openclaw) the transient `*-healthcheck.timer` never fires for a container Quadlet
  started, so `.State.Health.Status` stays `starting` for ever. What replaces the scheduler is
  `hs_wait_ready`: the HTTP `/health` probe as the authority plus an active
  `podman healthcheck run`, neither of which needs a timer. `HealthCmd=` stays on the unit — it is
  what `podman healthcheck run` and Headplane's `ExecStartPre=` execute — but nothing in this
  repository gates on the status podman records for it.
