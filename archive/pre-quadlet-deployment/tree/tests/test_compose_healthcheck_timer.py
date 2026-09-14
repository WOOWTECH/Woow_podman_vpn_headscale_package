import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "Containerfile.headscale"


class ComposeHealthcheckTimerLiveTests(unittest.TestCase):
    def test_timing_only_compose_healthcheck_schedules_inherited_native_command(self):
        podman = shutil.which("podman")
        podman_compose = shutil.which("podman-compose")
        if podman is None or podman_compose is None:
            self.skipTest("Podman and podman-compose are required for the live timer test")
        info = subprocess.run(
            [podman, "info"], text=True, capture_output=True, check=False, timeout=30,
        )
        if info.returncode != 0:
            self.skipTest("Podman runtime is not available locally")

        suffix = str(os.getpid())
        tag = f"localhost/woow-headscale:compose-health-timer-{suffix}"
        container_name = f"woow-headscale-health-timer-{suffix}"
        project_name = f"woow_health_timer_{suffix}"
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary) / "context"
            context.mkdir()
            shutil.copy2(CONTAINERFILE, context / "Containerfile.headscale")
            build = subprocess.run(
                [
                    podman, "build", "--pull=always", "--format", "docker", "--file",
                    str(context / "Containerfile.headscale"), "--tag", tag, str(context),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=600,
            )
            self.addCleanup(
                subprocess.run, [podman, "image", "rm", "--force", tag],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)

            compose_file = Path(temporary) / "compose.yml"
            compose_file.write_text(
                "services:\n"
                "  timer:\n"
                f"    image: {tag}\n"
                f"    container_name: {container_name}\n"
                '    entrypoint: ["/bin/sh", "-c"]\n'
                '    command: ["while :; do sleep 1; done"]\n'
                "    healthcheck:\n"
                "      interval: 1s\n"
                "      timeout: 1s\n"
                "      start_period: 1s\n"
                "      retries: 1\n",
                encoding="utf-8",
            )
            compose = [podman_compose, "-p", project_name, "-f", str(compose_file)]
            up = subprocess.run(
                compose + ["up", "-d"],
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.addCleanup(
                subprocess.run, compose + ["down"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=60,
            )
            self.addCleanup(
                subprocess.run, [podman, "rm", "--force", "--ignore", container_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            self.assertEqual(up.returncode, 0, up.stdout + up.stderr)

            deadline = time.monotonic() + 30
            latest = None
            while time.monotonic() < deadline:
                inspect = subprocess.run(
                    [podman, "container", "inspect", container_name],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(inspect.returncode, 0, inspect.stderr)
                latest = json.loads(inspect.stdout)[0]
                health = latest.get("State", {}).get("Health", {}) or {}
                if health.get("Log") and health.get("Status") != "starting":
                    break
                time.sleep(0.5)

            self.assertIsNotNone(latest)
            healthcheck = latest["Config"]["Healthcheck"]
            self.assertEqual(healthcheck["Test"], ["CMD", "/ko-app/headscale", "health"])
            self.assertNotIn("CMD-SHELL", healthcheck["Test"])
            health = latest.get("State", {}).get("Health", {}) or {}
            self.assertTrue(health.get("Log"), "automatic healthcheck did not create a log within 30s")
            self.assertIn(health.get("Status"), ("healthy", "unhealthy"))


if __name__ == "__main__":
    unittest.main()
