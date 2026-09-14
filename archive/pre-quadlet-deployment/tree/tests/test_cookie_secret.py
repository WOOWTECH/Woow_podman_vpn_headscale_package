from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/cookie_secret.py"


class CookieSecretTests(unittest.TestCase):
    def run_helper(self, path, *arguments):
        return subprocess.run(
            [sys.executable, str(HELPER), str(path), *map(str, arguments)],
            text=True,
            capture_output=True,
            check=False,
        )

    def write_secret(self, path, value, mode=0o600):
        path.write_bytes(value)
        path.chmod(mode)

    def assert_valid_generated_secret(self, path):
        value = path.read_bytes()
        self.assertEqual(len(value), 32)
        self.assertRegex(value, re.compile(rb"\A[0-9a-f]{32}\Z"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return value

    def test_new_secret_is_exactly_32_ascii_hex_characters_and_mode_600(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cookie-secret"
            result = self.run_helper(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_valid_generated_secret(path)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_valid_32_character_secret_is_reused_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cookie-secret"
            expected = b"valid_COOKIE-secret_1234567890ab"
            self.write_secret(path, expected)
            inode = path.stat().st_ino

            result = self.run_helper(path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(path.stat().st_ino, inode)
            self.assertNotIn(expected.decode("ascii"), result.stdout + result.stderr)

    def test_known_64_hex_legacy_secret_is_rotated_atomically(self):
        for trailing_newline in (b"", b"\n"):
            with self.subTest(trailing_newline=bool(trailing_newline)), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "cookie-secret"
                legacy = b"a" * 64 + trailing_newline
                self.write_secret(path, legacy)
                inode = path.stat().st_ino

                result = self.run_helper(path)

                self.assertEqual(result.returncode, 0, result.stderr)
                generated = self.assert_valid_generated_secret(path)
                self.assertNotEqual(generated, legacy[:32])
                self.assertNotEqual(path.stat().st_ino, inode)
                self.assertNotIn(legacy.decode("ascii").strip(), result.stdout + result.stderr)

    def test_malformed_wrong_mode_and_symlink_secrets_are_rejected(self):
        cases = (b"short", b"!" * 32, b"a" * 33, b"A" * 64)
        for value in cases:
            with self.subTest(value=value[:8]), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "cookie-secret"
                self.write_secret(path, value)
                result = self.run_helper(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_bytes(), value)
                self.assertNotIn(value.decode("ascii"), result.stdout + result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cookie-secret"
            self.write_secret(path, b"b" * 32, mode=0o640)
            result = self.run_helper(path)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target"
            path = directory / "cookie-secret"
            self.write_secret(target, b"c" * 32)
            path.symlink_to(target)
            result = self.run_helper(path)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(path.is_symlink())
            self.assertEqual(target.read_bytes(), b"c" * 32)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cookie-secret"
            path.mkdir()
            result = self.run_helper(path)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(path.is_dir())

    def test_legacy_location_is_validated_and_migrated_without_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "runtime" / "cookie-secret"
            path.parent.mkdir()
            legacy = directory / "legacy-cookie-secret"
            value = b"fedcba9876543210fedcba9876543210"
            self.write_secret(legacy, value)

            result = self.run_helper(path, "--legacy", legacy)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), value)
            self.assertFalse(legacy.exists())
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
