import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "Containerfile.headscale"


class HeadscaleRuntimeImageTests(unittest.TestCase):
    def test_containerfile_build_and_image_config_when_podman_is_available(self):
        podman = shutil.which("podman")
        if podman is None:
            self.skipTest("Podman is not installed locally")
        info = subprocess.run(
            [podman, "info"], text=True, capture_output=True, check=False, timeout=30,
        )
        if info.returncode != 0:
            self.skipTest("Podman runtime is not available locally")

        tag = "localhost/woow-headscale:0.29.3-contract-test"
        with tempfile.TemporaryDirectory() as temporary:
            context = Path(temporary)
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

        inspect = subprocess.run(
            [podman, "image", "inspect", tag],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        config = json.loads(inspect.stdout)[0]["Config"]
        self.assertEqual(config["Entrypoint"], ["/ko-app/headscale"])
        self.assertEqual(
            config["Labels"]["org.woow-headscale.build-contract"],
            "headscale-shell-health-v1",
        )

        container_name = "woow-headscale-contract-healthcheck-test"
        create = subprocess.run(
            [podman, "create", "--name", container_name, "--network", "none", tag],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.addCleanup(
            subprocess.run, [podman, "rm", "--ignore", container_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        self.assertEqual(create.returncode, 0, create.stdout + create.stderr)
        container_inspect = subprocess.run(
            [podman, "container", "inspect", container_name],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(container_inspect.returncode, 0, container_inspect.stderr)
        healthcheck_test = json.loads(container_inspect.stdout)[0]["Config"]["Healthcheck"]["Test"]
        self.assertEqual(healthcheck_test, ["CMD", "/ko-app/headscale", "health"])
        self.assertNotIn("CMD-SHELL", healthcheck_test)

        shell = subprocess.run(
            [podman, "run", "--rm", "--entrypoint", "/bin/sh", tag, "-c", "test -x /ko-app/headscale"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)


if __name__ == "__main__":
    unittest.main()
