from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/validate_api_key.py"


def load_module():
    spec = importlib.util.spec_from_file_location("validate_api_key", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidateAPIKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cls.prefix = "Abcd_efgh-12"
        cls.key = f"hskey-api-{cls.prefix}-" + "S" * 64

    def timestamp(self, value, nanos=0):
        return {"seconds": int(value.timestamp()), "nanos": nanos}

    def record(self, prefix=None, **overrides):
        value = {
            "prefix": prefix or f"hskey-api-{self.prefix}-***",
            "expiration": self.timestamp(self.now + timedelta(days=1), nanos=123),
            "created_at": self.timestamp(self.now - timedelta(days=1)),
        }
        value.update(overrides)
        return value

    def test_observed_schema_with_absent_status_fields_is_accepted(self):
        result = self.module.validate_api_key(self.key + "\n", [self.record()], now=self.now)
        self.assertEqual(result, self.prefix)
        self.assertEqual(len(self.key), 87)
        self.assertNotIn(".", self.key)

    def test_existing_iso_expiration_is_still_accepted(self):
        record = self.record(expiration=(self.now + timedelta(days=1)).isoformat())
        self.module.validate_api_key(self.key, [record], now=self.now)

    def test_unrelated_and_ambiguous_prefixes_are_rejected(self):
        cases = (
            [self.record(prefix="hskey-api-Zyxw_vuts-98-***")],
            [self.record(), self.record()],
        )
        for records in cases:
            with self.subTest(records=records):
                with self.assertRaisesRegex(ValueError, "no unique"):
                    self.module.validate_api_key(self.key, records, now=self.now)

    def test_empty_short_and_malformed_masked_prefixes_are_rejected(self):
        prefixes = (
            "***",
            "hskey-api-***",
            "hskey-api-short-***",
            f"hskey-api-{self.prefix}-",
            None,
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                record = self.record()
                record["prefix"] = prefix
                with self.assertRaisesRegex(ValueError, "no unique"):
                    self.module.validate_api_key(self.key, [record], now=self.now)

    def test_malformed_expired_and_explicitly_invalid_keys_are_rejected(self):
        cases = (
            ("not-a-key", [self.record()]),
            (
                self.key,
                [self.record(expiration=self.timestamp(self.now - timedelta(seconds=1)))],
            ),
            (self.key, [self.record(revoked=True)]),
            (self.key, [self.record(valid=False)]),
            (self.key, [self.record(status="REVOKED")]),
            (self.key, [self.record(status="invalid")]),
        )
        for key, records in cases:
            with self.subTest(key=key[:12], records=records):
                with self.assertRaises(ValueError):
                    self.module.validate_api_key(key, records, now=self.now)

    def test_malformed_protobuf_timestamps_are_rejected(self):
        expirations = (
            {"nanos": 0},
            {"seconds": "not-a-number", "nanos": 0},
            {"seconds": int(self.now.timestamp()), "nanos": 1_000_000_000},
        )
        for expiration in expirations:
            with self.subTest(expiration=expiration):
                with self.assertRaisesRegex(ValueError, "no valid expiration"):
                    self.module.validate_api_key(
                        self.key, [self.record(expiration=expiration)], now=self.now
                    )

    def test_cli_error_does_not_disclose_stored_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory, "api-key")
            list_path = Path(directory, "keys.json")
            key_path.write_text(self.key, encoding="utf-8")
            list_path.write_text(json.dumps([]), encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit):
                self.module.main(
                    ["--key-file", str(key_path), "--list-json", str(list_path)]
                )
            self.assertNotIn(self.key, stderr.getvalue())
            self.assertIn("no unique Headscale key record", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
