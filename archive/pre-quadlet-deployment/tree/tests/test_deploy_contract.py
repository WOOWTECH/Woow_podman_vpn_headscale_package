import json
import os
from pathlib import Path
import re
from shlex import quote as shlex_quote
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeployContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        cls.verify = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
        cls.compose = (ROOT / "podman-compose.yml").read_text(encoding="utf-8")
        cls.containerfile = (ROOT / "Containerfile.headscale").read_text(encoding="utf-8")
        cls.ngrok = (ROOT / "podman-compose.ngrok.yml").read_text(encoding="utf-8")
        cls.unit = (ROOT / "systemd/woow_headscale.service").read_text(encoding="utf-8")

    def test_shell_scripts_have_valid_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", "deploy.sh", "scripts/verify.sh"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compose_models_are_executable_when_podman_compose_is_available(self):
        executable = shutil.which("podman-compose")
        if executable is None:
            self.skipTest("podman-compose is not installed locally")
        environment = os.environ.copy()
        environment.update({
            "COMPOSE_PROJECT_NAME": "woow_headscale",
            "WOOW_PROJECT_DIR": str(ROOT),
            "NGROK_AUTHTOKEN": "disabled-test-token",
            "NGROK_COMMAND": "http headscale:8080",
        })
        commands = (
            [executable, "-f", "podman-compose.yml", "config"],
            [
                executable,
                "-f",
                "podman-compose.yml",
                "-f",
                "podman-compose.ngrok.yml",
                "config",
            ],
        )
        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("healthcheck:", result.stdout)
                self.assertNotRegex(
                    result.stdout,
                    r"(?m)^\s+test:",
                    "Compose must preserve the image-level native healthcheck command",
                )
                for timing in (
                    "interval: 10s", "timeout: 5s", "start_period: 10s", "retries: 12",
                ):
                    self.assertIn(timing, result.stdout)

    def test_headscale_healthcheck_inherits_native_test_and_sets_compose_timings(self):
        headscale = self.compose.split("  headscale:\n", 1)[1].split("\n  headplane:\n", 1)[0]
        healthcheck = headscale.split("    healthcheck:\n", 1)[1].split("\n    volumes:\n", 1)[0]
        self.assertEqual(
            healthcheck.splitlines(),
            [
                "      interval: 10s",
                "      timeout: 5s",
                "      start_period: 10s",
                "      retries: 12",
            ],
        )
        self.assertNotRegex(healthcheck, r"(?m)^\s*test:")
        self.assertIn(
            'HEALTHCHECK --interval=10s --timeout=5s --start-period=10s '
            '--retries=12 CMD ["/ko-app/headscale", "health"]',
            self.containerfile,
        )
        self.assertNotIn("CMD-SHELL", self.containerfile)

    def test_fixed_project_name_scopes_compose_labels_volumes_and_systemd(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("COMPOSE_PROJECT_NAME=woow_headscale", env_example)
        self.assertGreaterEqual(self.compose.count("com.docker.compose.project: ${COMPOSE_PROJECT_NAME}"), 5)
        self.assertGreaterEqual(self.compose.count("io.podman.compose.project: ${COMPOSE_PROJECT_NAME}"), 5)
        self.assertIn('"$PROJECT_DIR/systemd/$unit_name"', self.deploy)
        for unit_name in (
            "woow_headscale.service", "woow_headscale_health.service",
            "woow_headscale_health.timer",
        ):
            self.assertTrue((ROOT / "systemd" / unit_name).is_file())

    def test_images_storage_mounts_health_and_restart_contract(self):
        self.assertIn("localhost/woow-headscale:0.29.3", self.compose)
        self.assertNotIn("docker.io/headscale/headscale", self.compose)
        self.assertIn("ghcr.io/tale/headplane:0.7.0", self.compose)
        self.assertEqual(self.compose.count("restart: always"), 2)
        self.assertIn("./runtime/headscale/config.yaml:/etc/headscale/config.yaml:ro", self.compose)
        self.assertIn("./runtime/headplane/config.yaml:/etc/headplane/config.yaml:ro", self.compose)
        self.assertIn("headscale-data:/var/lib/headscale", self.compose)
        self.assertIn("headplane-data:/var/lib/headplane", self.compose)
        self.assertEqual(self.compose.count("healthcheck:"), 1)
        self.assertNotIn("test:", self.compose)
        self.assertIn("/admin", self.deploy)

    def test_headscale_runtime_uses_pinned_stages_and_identifying_labels(self):
        self.assertRegex(
            self.containerfile,
            r"FROM docker\.io/headscale/headscale:v0\.29\.3@sha256:[0-9a-f]{64} AS headscale",
        )
        self.assertRegex(
            self.containerfile,
            r"FROM docker\.io/library/debian:12\.11-slim@sha256:[0-9a-f]{64}",
        )
        self.assertIn("COPY --from=headscale /ko-app/headscale /ko-app/headscale", self.containerfile)
        self.assertIn("COPY --from=headscale /etc/ssl/certs/ca-certificates.crt", self.containerfile)
        self.assertIn('ENTRYPOINT ["/ko-app/headscale"]', self.containerfile)
        for label in (
            "org.opencontainers.image.version",
            "org.woow-headscale.runtime",
            "org.woow-headscale.source-image",
            "org.woow-headscale.build-contract",
        ):
            self.assertIn(label, self.containerfile)
        self.assertIn("org.woow-headscale.recipe-sha256", self.deploy)

    def test_runtime_image_build_is_private_validated_and_precedes_all_startup(self):
        inspect = self.deploy.index('podman image inspect "$HEADSCALE_IMAGE"')
        build = self.deploy.index(
            "capture_command podman build --pull=always --format docker"
        )
        first_startup = min(
            self.deploy.index("compose_ngrok up -d ngrok"),
            self.deploy.index("compose_base up -d --force-recreate headscale"),
        )
        self.assertLess(inspect, build)
        self.assertLess(build, first_startup)
        self.assertIn('"$PREFLIGHT_DIR/build-context"', self.deploy)
        self.assertIn("headscale_runtime_image_is_current", self.deploy)
        self.assertIn(
            'health.get("Test") == ["CMD", "/ko-app/headscale", "health"]',
            self.deploy,
        )
        self.assertIn("could not build the pinned Headscale runtime image", self.deploy)
        self.assertNotIn("podman build --quiet", self.deploy)

    def test_podman_49_image_inspect_health_omission_uses_temporary_container(self):
        fixture = ROOT / "tests/fixtures/podman-4.9-image-inspect-no-healthcheck.json"
        self.assertNotIn("Healthcheck", fixture.read_text(encoding="utf-8"))
        self.assertIn('podman create --name "$container_name" --cidfile "$cidfile" --network none', self.deploy)
        self.assertIn('podman container inspect "$(<"$cidfile")"', self.deploy)
        self.assertIn('podman rm --ignore "$container_name"', self.deploy)
        self.assertNotIn("buildah inspect", self.deploy)
        rejection = self.deploy.index("if ! headscale_runtime_image_is_current; then")
        docker_rebuild = self.deploy.index(
            "capture_command podman build --pull=always --format docker"
        )
        self.assertLess(rejection, docker_rebuild)

    def test_temporary_healthcheck_container_is_covered_by_exit_cleanup(self):
        self.assertIn('HEADSCALE_HEALTHCHECK_CONTAINERS+=("$container_name")', self.deploy)
        self.assertIn("trap cleanup_preflight EXIT", self.deploy)
        self.assertIn('for container_name in "${HEADSCALE_HEALTHCHECK_CONTAINERS[@]}"', self.deploy)

    def test_host_binding_defaults_are_least_privilege(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        expected = (
            "HEADSCALE_HOST_BIND_ADDR=0.0.0.0",
            "HEADSCALE_HOST_PORT=28080",
            "HEADSCALE_METRICS_HOST_BIND_ADDR=127.0.0.1",
            "HEADSCALE_METRICS_HOST_PORT=29090",
            "HEADPLANE_HOST_BIND_ADDR=127.0.0.1",
            "HEADPLANE_HOST_PORT=23000",
        )
        for setting in expected:
            self.assertIn(setting, env_example)
        self.assertNotIn("api-key", "\n".join(line for line in self.compose.splitlines() if "ports:" in line))

    def test_ngrok_is_isolated_from_base_compose_and_api_is_loopback(self):
        self.assertNotIn("ngrok:", self.compose)
        self.assertIn("docker.io/ngrok/ngrok:3", self.ngrok)
        self.assertIn('"127.0.0.1:24040:4040"', self.ngrok)
        self.assertIn("NGROK_AUTHTOKEN", self.ngrok)
        self.assertNotRegex(self.ngrok, r"(?m)^\s*profiles:")
        self.assertIn("if [[ $NGROK_ENABLED == false ]]", self.deploy)
        self.assertIn("podman rm -f ngrok", self.deploy)

    def test_preflight_precedes_collisions_and_canonical_render_is_last_before_headscale(self):
        preflight = self.deploy.index("PREFLIGHT_DIR=$(mktemp")
        collision = self.deploy.index("for container_name in headscale")
        discovery = self.deploy.index("DISCOVERED_URL=$(python3")
        canonical = self.deploy.index('--output-dir "$RUNTIME_DIR" "${render_args[@]}"')
        startup = self.deploy.index("compose_base up -d --force-recreate headscale")
        self.assertLess(preflight, collision)
        self.assertLess(collision, discovery)
        self.assertLess(discovery, canonical)
        self.assertLess(canonical, startup)
        self.assertNotIn('--output-dir "$RUNTIME_DIR"\n', self.deploy)
        self.assertIn("podman-compose configuration validation failed", self.deploy)
        self.assertIn('unset "${COMPOSE_KEYS[@]}" NGROK_COMMAND WOOW_PROJECT_DIR', self.deploy)

    def _stop_fixture(self, temporary, foreign_ngrok=False, volume_state="absent"):
        project = Path(temporary) / "checkout"
        fake_bin = Path(temporary) / "bin"
        (project / "scripts").mkdir(parents=True)
        fake_bin.mkdir()
        for relative in (
            ".env.example", "deploy.sh", "podman-compose.yml", "podman-compose.ngrok.yml",
            "scripts/runtime_config.py", "scripts/volume_ownership.py",
            "config/headscale/config.template.yaml", "config/headplane/config.template.yaml",
        ):
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        (project / "deploy.sh").chmod(0o755)
        (fake_bin / "podman-compose").write_text(
            "#!/bin/bash\n"
            "printf '%s|%s|%s|%s|%s|%s\\n' \"$COMPOSE_PROJECT_NAME\" "
            "\"$HEADSCALE_HOST_PORT\" \"$NGROK_AUTHTOKEN\" \"$NGROK_COMMAND\" "
            "\"$WOOW_PROJECT_DIR\" \"$*\" >> \"$TEST_LOG\"\n",
            encoding="utf-8",
        )
        owned_labels = {
            "org.woow-headscale.project": "woow-headscale",
            "org.woow-headscale.project-dir": str(project.resolve()),
            "com.docker.compose.project": "woow_headscale",
            "io.podman.compose.project": "woow_headscale",
        }
        if volume_state == "foreign":
            owned_labels["org.woow-headscale.project-dir"] = "/srv/another-checkout"
        inspected_volumes = {
            f"woow_headscale_{logical}": {
                "Name": f"woow_headscale_{logical}", "Labels": owned_labels,
            }
            for logical in ("headscale-data", "headscale-run", "headplane-data")
        }
        podman_body = (
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" >> \"$TEST_PODMAN_LOG\"\n"
            "if [[ $1 == container && $2 == exists ]]; then "
            + ("[[ $3 == ngrok ]]; exit $?; " if foreign_ngrok else "exit 1; ")
            + "fi\n"
            "if [[ $1 == volume && $2 == ls ]]; then\n"
            + (
                "  printf '%s\\n' woow_headscale_headscale-data woow_headscale_headscale-run woow_headscale_headplane-data\n"
                if volume_state != "absent" else "  :\n"
            )
            + "  exit 0\nfi\n"
            "if [[ $1 == volume && $2 == inspect ]]; then\n"
            f"  case $3 in\n"
            + "".join(
                f"    {name}) printf '%s' {shlex_quote(json.dumps([value]))} ;;\n"
                for name, value in inspected_volumes.items()
            )
            + "    *) exit 1 ;;\n  esac\n  exit 0\nfi\n"
            "if [[ $1 == inspect ]]; then printf '[{\"Config\":{\"Labels\":{\"org.woow-headscale.project\":\"somebody-else\"}}}]'; exit 0; fi\n"
            "exit 1\n"
        )
        (fake_bin / "podman").write_text(podman_body, encoding="utf-8")
        for path in (fake_bin / "podman", fake_bin / "podman-compose"):
            path.chmod(0o755)
        log = Path(temporary) / "compose.log"
        podman_log = Path(temporary) / "podman.log"
        environment = os.environ.copy()
        environment.update({
            "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
            "TEST_LOG": str(log),
            "TEST_PODMAN_LOG": str(podman_log),
            "COMPOSE_PROJECT_NAME": "hostile_project",
            "HEADSCALE_HOST_PORT": "65535",
            "NGROK_AUTHTOKEN": "hostile-secret-token",
            "NGROK_COMMAND": "tcp attacker:9",
            "WOOW_PROJECT_DIR": "/hostile/checkout",
        })
        return project, log, podman_log, environment

    def test_stop_render_overrides_hostile_inherited_compose_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, log, _podman_log, environment = self._stop_fixture(temporary)
            result = subprocess.run(
                [str(project / "deploy.sh"), "--stop"], cwd=project, env=environment,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = log.read_text(encoding="utf-8")
            self.assertIn("woow_headscale|28080||http headscale:8080|", rendered)
            self.assertIn(str(project.resolve()), rendered)
            self.assertNotIn("hostile", rendered)
            self.assertNotIn("65535", rendered)

    def test_disabled_ngrok_from_another_checkout_blocks_stop_without_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, log, _podman_log, environment = self._stop_fixture(
                temporary, foreign_ngrok=True
            )
            result = subprocess.run(
                [str(project / "deploy.sh"), "--stop"], cwd=project, env=environment,
                text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("another checkout or project: ngrok", result.stderr)
            self.assertFalse(log.exists(), "compose down must not touch the foreign ngrok")

    def test_deploy_volume_preflight_accepts_absent_and_owned_volumes(self):
        for volume_state in ("absent", "owned"):
            with self.subTest(volume_state=volume_state), tempfile.TemporaryDirectory() as temporary:
                project, log, _podman_log, environment = self._stop_fixture(
                    temporary, volume_state=volume_state
                )
                result = subprocess.run(
                    [str(project / "deploy.sh"), "--stop"], cwd=project, env=environment,
                    text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(log.exists(), "owned or absent volumes must permit compose down")

    def test_foreign_volume_without_containers_blocks_before_compose_or_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            project, log, podman_log, environment = self._stop_fixture(
                temporary, volume_state="foreign"
            )
            result = subprocess.run(
                [str(project / "deploy.sh")], cwd=project, env=environment,
                text=True, capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("another checkout or project: headscale-data", result.stderr)
            self.assertFalse(log.exists(), "compose must not run when an exact volume is foreign")
            podman_calls = podman_log.read_text(encoding="utf-8")
            self.assertNotIn("rm -f", podman_calls)

    def test_volume_preflight_precedes_stop_disabled_ngrok_removal_and_compose_up(self):
        volume_preflight = self.deploy.index('"headscale|/var/lib/headscale|headscale-data"')
        stop = self.deploy.index('if [[ ${1:-} == "--stop" ]]')
        disabled_removal = self.deploy.index("podman rm -f ngrok")
        first_up = self.deploy.index("compose_ngrok up -d ngrok")
        self.assertLess(volume_preflight, stop)
        self.assertLess(volume_preflight, disabled_removal)
        self.assertLess(volume_preflight, first_up)

    def test_stop_dispatch_precedes_startup_only_dependencies(self):
        stop = self.deploy.index('if [[ ${1:-} == "--stop" ]]')
        startup_dependencies = self.deploy.index("for command_name in curl ss sleep")
        self.assertLess(stop, startup_dependencies)

    def test_cookie_secret_uses_dedicated_safe_initializer(self):
        self.assertIn('scripts/cookie_secret.py" "$COOKIE_FILE"', self.deploy)
        self.assertIn('--legacy "$PROJECT_DIR/config/headplane/cookie-secret"', self.deploy)
        self.assertNotIn("openssl rand -hex 32", self.deploy)
        self.assertNotIn('chmod 600 "$COOKIE_FILE"', self.deploy)

    def test_deploy_never_sources_dotenv_or_creates_preauth_keys(self):
        self.assertNotRegex(self.deploy, r"(?m)^\s*(source|\.)\s+.*\.env")
        self.assertNotIn("eval ", self.deploy)
        self.assertNotIn("preauthkeys create", self.deploy)
        self.assertIn("module.parse_env", self.deploy)
        self.assertIn("--expiration 3650d", self.deploy)
        self.assertIn("users list --output json", self.deploy)

    def _run_default_user_block(self, users_output):
        start = self.deploy.index("if [[ $CREATE_DEFAULT_USER == true ]]; then")
        end = self.deploy.index("\n\nif [[ ! -s $API_KEY_FILE ]]", start)
        block = self.deploy[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            users_file = Path(temporary) / "users.json"
            calls_file = Path(temporary) / "podman-calls.log"
            users_file.write_text(users_output, encoding="utf-8")
            harness = (
                "set -euo pipefail\n"
                "CREATE_DEFAULT_USER=true\n"
                f"RUNTIME_DIR={shlex_quote(temporary)}\n"
                f"TEST_USERS_JSON={shlex_quote(str(users_file))}\n"
                f"TEST_PODMAN_CALLS={shlex_quote(str(calls_file))}\n"
                "podman() {\n"
                "  printf '%s\\n' \"$*\" >>\"$TEST_PODMAN_CALLS\"\n"
                "  if [[ $* == 'exec headscale headscale users list --output json' ]]; then\n"
                "    cat \"$TEST_USERS_JSON\"\n"
                "  fi\n"
                "}\n"
                "capture_command() { \"$@\"; }\n"
                "redact_log() { cat \"$1\" >&2; }\n"
                "fail() { printf 'FATAL: %s\\n' \"$*\" >&2; exit 1; }\n"
                f"{block}\n"
            )
            result = subprocess.run(
                ["bash"], input=harness, text=True, capture_output=True, check=False,
            )
            calls = calls_file.read_text(encoding="utf-8").splitlines()
        return result, calls

    def test_null_user_list_is_empty_and_creates_default_user(self):
        result, calls = self._run_default_user_block("null\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [
            "exec headscale headscale users list --output json",
            "exec headscale headscale users create default",
        ])

    def test_existing_default_user_remains_idempotent(self):
        result, calls = self._run_default_user_block('[{"name": "default"}]\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, ["exec headscale headscale users list --output json"])

    def test_invalid_user_lists_are_rejected_without_creating_default(self):
        for users_output in ("not json\n", '"wrong structure"\n', '{"users": null}\n'):
            with self.subTest(users_output=users_output):
                result, calls = self._run_default_user_block(users_output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Headscale returned an invalid user list", result.stderr)
                self.assertEqual(calls, ["exec headscale headscale users list --output json"])

    def test_deploy_polls_matching_ngrok_tunnel_and_redacts_command_logs(self):
        self.assertGreaterEqual(self.deploy.count("deadline=$((SECONDS + 120))"), 3)
        self.assertNotIn("seq 1 24", self.deploy)
        self.assertIn("ngrok-url", self.deploy)
        self.assertIn('NGROK_COMMAND+=" --url=$NGROK_DOMAIN"', self.deploy)
        self.assertNotIn('NGROK_COMMAND+=" --domain=', self.deploy)
        self.assertNotIn('tunnels[0]', self.deploy)
        self.assertIn("mktemp \"$RUNTIME_DIR/.command-log.", self.deploy)
        self.assertIn("chmod 600 \"$log_file\"", self.deploy)
        self.assertIn("[REDACTED]", self.deploy)
        self.assertRegex(self.deploy, re.compile(r"compose_base\(\).*capture_command", re.DOTALL))

    def test_collision_checks_include_addresses_ports_and_wildcards(self):
        helper = (ROOT / "scripts/port_bindings.py").read_text(encoding="utf-8")
        self.assertIn('check_port "$HEADSCALE_HOST_BIND_ADDR" "$HEADSCALE_HOST_PORT"', self.deploy)
        self.assertIn(
            'check_port "$HEADSCALE_METRICS_HOST_BIND_ADDR" "$HEADSCALE_METRICS_HOST_PORT"',
            self.deploy,
        )
        self.assertIn('binding.get("HostIp"', helper)
        self.assertIn('binding.get("HostPort"', helper)
        self.assertIn("addresses_overlap", helper)
        self.assertIn("for listener in relevant", helper)

    def test_readiness_deadlines_fit_systemd_start_timeout(self):
        self.assertEqual(self.deploy.count("deadline=$((SECONDS + 120))"), 3)
        self.assertIn("TimeoutStartSec=30min", self.unit)
        self.assertIn("initial image pulls", self.unit)

    def test_extra_records_use_atomic_nofollow_initializer(self):
        runtime_config = (ROOT / "scripts/runtime_config.py").read_text(encoding="utf-8")
        self.assertIn("init-extra-records", self.deploy)
        self.assertIn("os.O_NOFOLLOW", runtime_config)
        self.assertIn("os.replace(", runtime_config)
        self.assertNotIn("printf '[]\\n'", self.deploy)

    def test_readmes_use_bundled_unit_without_false_generated_key_claims(self):
        for readme_name in ("README.md", "README_zh-TW.md"):
            text = (ROOT / readme_name).read_text(encoding="utf-8")
            with self.subTest(readme=readme_name):
                self.assertIn("woow_headscale.service", text)
                self.assertIn("enable-linger", text)
                self.assertNotIn("podman generate systemd", text)
                self.assertNotIn("90-day", text)
                self.assertNotIn("90 天", text)
                self.assertNotIn("printed-preauth-key", text)
                self.assertNotIn("印出的-preauth-key", text)
                self.assertIn("v0.29.3", text)
                self.assertIn("config.template.yaml", text)
                self.assertIn("runtime/headplane", text)
                self.assertNotIn("config/headplane/cookie-secret", text)

    def test_verify_uses_exact_labelled_project_resources_without_suffix_matching(self):
        self.assertIn('scripts/volume_ownership.py" check-container', self.verify)
        self.assertIn('scripts/volume_ownership.py" resolve', self.verify)
        self.assertIn('--project "$COMPOSE_PROJECT_NAME" --project-dir "$PROJECT_DIR"', self.verify)
        self.assertNotIn("endswith", self.verify)

    def test_verify_covers_runtime_contract(self):
        checks = (
            "headscale version",
            "/health",
            "/admin",
            "container health",
            "host binding",
            "mode 600",
            "default Headscale user",
            "apikeys list",
            "exactly matches a valid Headscale key record",
            "restart policy is always",
            "mount is durable",
            "effective server URL",
            "ngrok container is absent",
            "ngrok container is running",
        )
        for check in checks:
            self.assertIn(check, self.verify)

    def test_systemd_reruns_orchestration_without_recursive_enable(self):
        self.assertIn("After=network-online.target", self.unit)
        self.assertIn("WorkingDirectory=@PROJECT_DIR@", self.unit)
        self.assertNotIn('WorkingDirectory="@PROJECT_DIR@"', self.unit)
        self.assertIn("ExecStart=@PROJECT_DIR@/deploy.sh --from-systemd", self.unit)
        self.assertIn("ExecStop=@PROJECT_DIR@/deploy.sh --stop", self.unit)
        self.assertIn("RemainAfterExit=yes", self.unit)
        self.assertIn("scripts/render_systemd_unit.py", self.deploy)
        self.assertIn("if [[ $FROM_SYSTEMD == false ]]", self.deploy)
        self.assertNotIn("systemctl --user enable --now", self.deploy)

    def test_rendered_systemd_unit_has_unquoted_absolute_paths_and_verifies(self):
        renderer = ROOT / "scripts/render_systemd_unit.py"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "woow_headscale.service"
            result = subprocess.run(
                [
                    "python3", str(renderer),
                    str(ROOT / "systemd/woow_headscale.service"),
                    str(target), str(ROOT),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = target.read_text(encoding="utf-8")
            self.assertIn(f"WorkingDirectory={ROOT}", rendered)
            self.assertIn(f"ExecStart={ROOT}/deploy.sh --from-systemd", rendered)
            self.assertIn(f"ExecStop={ROOT}/deploy.sh --stop", rendered)
            self.assertNotIn('WorkingDirectory="', rendered)
            self.assertNotIn("@PROJECT_DIR@", rendered)

            analyzer = shutil.which("systemd-analyze")
            if analyzer is not None:
                verify = subprocess.run(
                    [analyzer, "--user", "verify", str(target)],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_systemd_renderer_rejects_unsafe_project_paths(self):
        renderer = ROOT / "scripts/render_systemd_unit.py"
        source = ROOT / "systemd/woow_headscale.service"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "woow_headscale.service"
            for project in (
                "/srv/checkout with-space", "/srv/checkout\twith-tab",
                "/srv/checkout\nwith-newline", "/srv/checkout\x01with-control",
            ):
                with self.subTest(project=repr(project)):
                    result = subprocess.run(
                        ["python3", str(renderer), str(source), str(target), project],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unsupported whitespace or control", result.stderr)


if __name__ == "__main__":
    unittest.main()
