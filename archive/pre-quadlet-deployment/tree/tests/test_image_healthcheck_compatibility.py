from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / "deploy.sh").read_text(encoding="utf-8")


def shell_function(name):
    start = DEPLOY.index(f"{name}() {{")
    end = DEPLOY.index("\n}\n", start) + 3
    return DEPLOY[start:end]


class ImageHealthcheckCompatibilityTests(unittest.TestCase):
    def _run_contract(self, container_fixture, cleanup_fails=False):
        image_fixture = ROOT / "tests/fixtures/podman-4.9-image-inspect-no-healthcheck.json"
        with tempfile.TemporaryDirectory() as temporary:
            preflight = Path(temporary) / "preflight"
            preflight.mkdir(mode=0o700)
            log = Path(temporary) / "podman.log"
            harness = f"""
set -uo pipefail
PREFLIGHT_DIR={shlex.quote(str(preflight))}
HEADSCALE_IMAGE=localhost/woow-headscale:0.29.3
HEADSCALE_SOURCE_IMAGE=test-source
HEADSCALE_BUILD_CONTRACT=test-contract
HEADSCALE_RECIPE_SHA256=test-recipe
HEADSCALE_HEALTHCHECK_CONTAINERS=()
IMAGE_FIXTURE={shlex.quote(str(image_fixture))}
CONTAINER_FIXTURE={shlex.quote(str(container_fixture))}
PODMAN_LOG={shlex.quote(str(log))}
CLEANUP_FAIL={'true' if cleanup_fails else 'false'}
podman() {{
  printf '%s\n' "$*" >> "$PODMAN_LOG"
  if [[ $1 == image && $2 == inspect ]]; then
    cat "$IMAGE_FIXTURE"
  elif [[ $1 == create ]]; then
    shift
    while (($#)); do
      case $1 in
        --cidfile) cidfile=$2; shift 2 ;;
        --name|--network) shift 2 ;;
        *) shift ;;
      esac
    done
    printf '%s\n' fixture-container-id > "$cidfile"
    printf '%s\n' fixture-container-id
  elif [[ $1 == container && $2 == inspect ]]; then
    cat "$CONTAINER_FIXTURE"
  elif [[ $1 == rm ]]; then
    [[ $CLEANUP_FAIL == false ]]
  else
    return 64
  fi
}}
{shell_function('headscale_runtime_image_is_current')}
headscale_runtime_image_is_current
"""
            result = subprocess.run(
                ["bash"], input=harness, text=True, capture_output=True, check=False,
            )
            return result, log.read_text(encoding="utf-8").splitlines()

    def test_podman_49_omission_accepts_exact_healthcheck_from_never_started_container(self):
        fixture = ROOT / "tests/fixtures/podman-container-inspect-native-healthcheck.json"
        result, commands = self._run_contract(fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(line.startswith("create --name woow-headscale-image-check-") for line in commands))
        create = next(line for line in commands if line.startswith("create "))
        self.assertIn("--cidfile", create)
        self.assertIn("--network none", create)
        self.assertNotIn("--publish", create)
        self.assertNotIn("--volume", create)
        self.assertFalse(any(line.startswith(("run ", "start ")) for line in commands))
        self.assertTrue(any(line.startswith("container inspect fixture-container-id") for line in commands))
        self.assertTrue(any(line.startswith("rm --ignore woow-headscale-image-check-") for line in commands))

    def test_non_native_healthcheck_is_rejected_and_container_is_cleaned_up(self):
        fixture = ROOT / "tests/fixtures/podman-container-inspect-shell-healthcheck.json"
        result, commands = self._run_contract(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(line.startswith("rm --ignore woow-headscale-image-check-") for line in commands))

    def test_cleanup_failure_fails_the_image_contract(self):
        fixture = ROOT / "tests/fixtures/podman-container-inspect-native-healthcheck.json"
        result, commands = self._run_contract(fixture, cleanup_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(line.startswith("rm --ignore ") for line in commands), 1)


if __name__ == "__main__":
    unittest.main()
