from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VERIFY = (ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")


def shell_function(name):
    start = VERIFY.index(f"{name}() {{")
    end = VERIFY.index("\n}\n", start) + 3
    return VERIFY[start:end]


def run_bash(script, environment=None):
    return subprocess.run(
        ["bash"],
        input="set -uo pipefail\n" + script,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )


class VerifyCompatibilityTests(unittest.TestCase):
    def test_live_container_requires_exact_native_healthcheck(self):
        function = shell_function("headscale_container_healthcheck_is_native")
        native = ROOT / "tests/fixtures/podman-container-inspect-native-healthcheck.json"
        shell = ROOT / "tests/fixtures/podman-container-inspect-shell-healthcheck.json"
        native_result = run_bash(
            f'FIXTURE={shlex.quote(str(native))}\npodman() {{ cat "$FIXTURE"; }}\n{function}\n'
            "headscale_container_healthcheck_is_native\n"
        )
        shell_result = run_bash(
            f'FIXTURE={shlex.quote(str(shell))}\npodman() {{ cat "$FIXTURE"; }}\n{function}\n'
            "headscale_container_healthcheck_is_native\n"
        )
        self.assertEqual(native_result.returncode, 0, native_result.stderr)
        self.assertNotEqual(shell_result.returncode, 0)

    def test_image_contract_does_not_depend_on_podman_49_omitted_healthcheck(self):
        function = shell_function("headscale_image_contract_is_current")
        self.assertNotIn('config.get("Healthcheck"', function)
        self.assertIn('config.get("Entrypoint")', function)

    def test_empty_podman_host_ip_is_wildcard_but_not_a_specific_address(self):
        fixture = ROOT / "tests" / "fixtures" / "podman-inspect-empty-host-ip.json"
        function = shell_function("binding_exists")
        harness = (
            f"FIXTURE={shlex.quote(str(fixture))}\n"
            'podman() { cat "$FIXTURE"; }\n'
            f"{function}\n"
        )
        wildcard = run_bash(harness + "binding_exists headscale 8080 0.0.0.0 28080\n")
        specific = run_bash(harness + "binding_exists headscale 8080 127.0.0.1 28080\n")
        self.assertEqual(wildcard.returncode, 0, wildcard.stderr)
        self.assertNotEqual(specific.returncode, 0)

    def _health_harness(self, statuses, timeout=120, poll_interval=2):
        with tempfile.TemporaryDirectory() as temporary:
            sequence = Path(temporary) / "statuses"
            counter = Path(temporary) / "counter"
            sequence.write_text("\n".join(statuses) + "\n", encoding="utf-8")
            counter.write_text("0\n", encoding="utf-8")
            functions = shell_function("container_health_status") + shell_function(
                "wait_container_healthy"
            )
            harness = f"""
SEQUENCE={shlex.quote(str(sequence))}
COUNTER={shlex.quote(str(counter))}
podman() {{
  count=$(cat "$COUNTER")
  status=$(sed -n "$((count + 1))p" "$SEQUENCE")
  [[ -n $status ]] || status=$(tail -n 1 "$SEQUENCE")
  printf '%s\n' "$((count + 1))" > "$COUNTER"
  printf '[{{"State":{{"Health":{{"Status":"%s"}}}}}}]' "$status"
}}
{functions}
wait_container_healthy headscale {timeout} {poll_interval}
"""
            return run_bash(harness)

    def test_container_health_waits_from_starting_to_healthy(self):
        result = self._health_harness(["starting", "healthy"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_container_health_fails_immediately_when_unhealthy(self):
        result = self._health_harness(["unhealthy"], timeout=120)
        self.assertNotEqual(result.returncode, 0)

    def test_container_health_starting_state_has_absolute_timeout(self):
        result = self._health_harness(["starting"], timeout=1, poll_interval=1)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
