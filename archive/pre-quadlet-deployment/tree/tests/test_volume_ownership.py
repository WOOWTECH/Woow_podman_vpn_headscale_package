import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "volume_ownership.py"
PROJECT = "woow_headscale"
PROJECT_DIR = "/srv/woow-headscale"


def load_module():
    spec = importlib.util.spec_from_file_location("volume_ownership", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def labels(project=PROJECT, project_dir=PROJECT_DIR):
    return {
        "org.woow-headscale.project": "woow-headscale",
        "org.woow-headscale.project-dir": project_dir,
        "com.docker.compose.project": project,
        "io.podman.compose.project": project,
    }


class VolumeOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def result(self, stdout="", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    def test_exact_project_scoped_labelled_volume_is_selected(self):
        expected = f"{PROJECT}_headscale-data"

        def run(arguments, check=True):
            if arguments[:3] == ["podman", "container", "exists"]:
                return self.result(returncode=1)
            if arguments[:3] == ["podman", "volume", "ls"]:
                return self.result(expected + "\nunrelated_headscale-data\n")
            if arguments[:3] == ["podman", "volume", "inspect"]:
                return self.result(json.dumps([{"Name": expected, "Labels": labels()}]))
            raise AssertionError(arguments)

        with mock.patch.object(self.module, "_run", side_effect=run):
            self.assertEqual(
                self.module.resolve_volume(
                    "headscale", "/var/lib/headscale", "headscale-data",
                    PROJECT, PROJECT_DIR,
                ),
                expected,
            )

    def test_exact_global_volume_from_another_checkout_is_rejected(self):
        expected = f"{PROJECT}_headscale-data"

        def run(arguments, check=True):
            if arguments[:3] == ["podman", "container", "exists"]:
                return self.result(returncode=1)
            if arguments[:3] == ["podman", "volume", "ls"]:
                return self.result(expected + "\n")
            if arguments[:3] == ["podman", "volume", "inspect"]:
                foreign = labels(project_dir="/srv/other-checkout")
                return self.result(json.dumps([{"Name": expected, "Labels": foreign}]))
            raise AssertionError(arguments)

        with mock.patch.object(self.module, "_run", side_effect=run):
            with self.assertRaisesRegex(self.module.OwnershipError, "another checkout"):
                self.module.resolve_volume(
                    "headscale", "/var/lib/headscale", "headscale-data",
                    PROJECT, PROJECT_DIR,
                )

    def test_container_requires_both_exact_compose_labels(self):
        for missing_or_wrong in (
            {"com.docker.compose.project": None},
            {"io.podman.compose.project": "somebody_else"},
        ):
            with self.subTest(labels=missing_or_wrong):
                container_labels = labels()
                container_labels.update(missing_or_wrong)

                def run(arguments, check=True):
                    if arguments[:3] == ["podman", "container", "exists"]:
                        return self.result(returncode=0)
                    if arguments[:2] == ["podman", "inspect"]:
                        return self.result(json.dumps([{
                            "Config": {"Labels": container_labels}, "Mounts": []
                        }]))
                    raise AssertionError(arguments)

                with mock.patch.object(self.module, "_run", side_effect=run):
                    with self.assertRaisesRegex(
                        self.module.OwnershipError, "compose project ownership"
                    ):
                        self.module.validate_container("headscale", PROJECT, PROJECT_DIR)


if __name__ == "__main__":
    unittest.main()
