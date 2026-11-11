#!/usr/bin/env python3
"""Check URL selection and three-file reconciliation without an ngrok account."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "scripts/ngrok-sync.py"
spec = importlib.util.spec_from_file_location("ngrok_sync", source)
assert spec and spec.loader
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class NgrokSyncTests(unittest.TestCase):
    def test_http_fixed_and_auto(self) -> None:
        payload = {"tunnels": [{"public_url": "https://vpn.example.ngrok.app"}]}
        self.assertEqual(sync.parse_public_url(payload, "http", "vpn.example.ngrok.app"),
                         "https://vpn.example.ngrok.app")
        self.assertEqual(sync.parse_public_url(payload, "http"),
                         "https://vpn.example.ngrok.app")
        self.assertIsNone(sync.parse_public_url(payload, "http", "wrong.example.com"))

    def test_tcp_and_untrusted_urls(self) -> None:
        payload = {"tunnels": [{"public_url": "tcp://1.tcp.ngrok.io:14005"}]}
        self.assertEqual(sync.parse_public_url(payload, "tcp"),
                         "http://1.tcp.ngrok.io:14005")
        for raw in ("tcp://user@1.tcp.ngrok.io:14005", "tcp://1.tcp.ngrok.io:99999",
                    "tcp://1.tcp.ngrok.io:14005/path", "tcp://127.0.0.1:14005"):
            self.assertIsNone(sync.parse_public_url({"tunnels": [{"public_url": raw}]}, "tcp"))
        self.assertIsNone(sync.parse_public_url(
            {"tunnels": [{"public_url": "https://a.ngrok.app"},
                         {"public_url": "https://b.ngrok.app"}]}, "http"
        ))

    def test_apply_updates_every_file_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "headscale").mkdir()
            (root / "headplane").mkdir()
            (root / "headscale.env").write_text(
                "HEADSCALE_BASE_DOMAIN=ts.local\nHEADSCALE_SERVER_URL=http://localhost:28080\n"
            )
            (root / "headscale/config.yaml").write_text(
                "server_url: http://localhost:28080\n"
            )
            (root / "headplane/config.yaml").write_text(
                '  public_url: "http://localhost:28080"\n'
            )
            url = "http://1.tcp.ngrok.io:14005"
            self.assertTrue(sync.apply_url(root, url))
            self.assertIn(url, (root / "headscale.env").read_text())
            self.assertIn(url, (root / "headscale/config.yaml").read_text())
            self.assertIn(url, (root / "headplane/config.yaml").read_text())
            self.assertFalse(sync.apply_url(root, url))
            with self.assertRaises(ValueError):
                sync.apply_url(root, "https://control.ts.local")
            env_file = root / "headscale.env"
            env_file.write_text(env_file.read_text() + "HEADSCALE_SERVER_URL=http://duplicate.example.net\n")
            with self.assertRaisesRegex(ValueError, "duplicated"):
                sync.apply_url(root, "https://new.example.net")

    def test_apply_rolls_back_if_a_later_file_cannot_be_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "headscale").mkdir()
            (root / "headplane").mkdir()
            contents = {
                "headscale.env": "HEADSCALE_SERVER_URL=http://old.example.net\n",
                "headscale/config.yaml": "server_url: http://old.example.net\n",
                "headplane/config.yaml": '  public_url: "http://old.example.net"\n',
            }
            for name, value in contents.items():
                (root / name).write_text(value)
            original = sync.atomic_replace
            calls = 0

            def fail_second(path: Path, content: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated write failure")
                original(path, content)

            with patch.object(sync, "atomic_replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    sync.apply_url(root, "https://new.example.net")
            self.assertEqual({name: (root / name).read_text() for name in contents}, contents)


if __name__ == "__main__":
    unittest.main()
