from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "runtime_config.py"


def load_runtime_config():
    spec = importlib.util.spec_from_file_location("runtime_config", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime_config = load_runtime_config()

    def write_env(self, directory, overrides=None):
        values = {
            "COMPOSE_PROJECT_NAME": "woow_headscale",
            "SERVER_URL": "https://vpn.example.com",
            "LOG_LEVEL": "info",
            "IPV4_PREFIX": "100.64.0.0/10",
            "IPV6_PREFIX": "fd7a:115c:a1e0::/48",
            "MAGIC_DNS_BASE_DOMAIN": "tail.example.net",
            "CREATE_DEFAULT_USER": "true",
            "NGROK_ENABLED": "false",
            "NGROK_AUTHTOKEN": "",
            "NGROK_MODE": "http",
            "NGROK_DOMAIN": "",
            "HEADSCALE_BIND_ADDR": "0.0.0.0",
            "HEADSCALE_PORT": "8080",
            "HEADSCALE_METRICS_BIND_ADDR": "0.0.0.0",
            "HEADSCALE_METRICS_PORT": "9090",
            "HEADPLANE_BIND_ADDR": "0.0.0.0",
            "HEADPLANE_PORT": "3000",
            "HEADSCALE_HOST_BIND_ADDR": "0.0.0.0",
            "HEADSCALE_HOST_PORT": "28080",
            "HEADSCALE_METRICS_HOST_BIND_ADDR": "127.0.0.1",
            "HEADSCALE_METRICS_HOST_PORT": "29090",
            "HEADPLANE_HOST_BIND_ADDR": "127.0.0.1",
            "HEADPLANE_HOST_PORT": "23000",
        }
        values.update(overrides or {})
        env_path = Path(directory) / ".env"
        env_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
        return env_path

    def test_compose_project_name_is_fixed_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary)
            self.assertEqual(
                self.runtime_config.compose_project_name(env_path), "woow_headscale"
            )
            env_path = self.write_env(
                temporary, {"COMPOSE_PROJECT_NAME": "hostile_project"}
            )
            with self.assertRaisesRegex(ValueError, "must be woow_headscale"):
                self.runtime_config.validated_env(env_path)

    def test_static_url_rendering_and_templates_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary)
            output = Path(temporary) / "runtime"
            templates = [
                ROOT / "config/headscale/config.template.yaml",
                ROOT / "config/headplane/config.template.yaml",
            ]
            before = [path.read_bytes() for path in templates]

            paths = self.runtime_config.render_runtime_configs(env_path, output)

            headscale = paths["headscale"].read_text(encoding="utf-8")
            headplane = paths["headplane"].read_text(encoding="utf-8")
            self.assertIn("server_url: https://vpn.example.com", headscale)
            self.assertIn("base_domain: tail.example.net", headscale)
            self.assertIn("write_ahead_log: true", headscale)
            self.assertIn("extra_records_path: /etc/headscale/extra_records.json", headscale)
            self.assertIn('cookie_secret_path: "/etc/headplane/cookie-secret"', headplane)
            self.assertIn('api_key_path: "/etc/headplane/api-key"', headplane)
            self.assertEqual(before, [path.read_bytes() for path in templates])

    def test_task1_env_without_host_binding_keys_uses_secure_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary)
            lines = env_path.read_text(encoding="utf-8").splitlines()
            env_path.write_text(
                "\n".join(line for line in lines if "_HOST_" not in line) + "\n",
                encoding="utf-8",
            )
            values = self.runtime_config.parse_env(env_path)
            self.assertEqual(values["HEADSCALE_HOST_BIND_ADDR"], "0.0.0.0")
            self.assertEqual(values["HEADSCALE_HOST_PORT"], "28080")
            self.assertEqual(values["HEADSCALE_METRICS_HOST_BIND_ADDR"], "127.0.0.1")
            self.assertEqual(values["HEADPLANE_HOST_BIND_ADDR"], "127.0.0.1")

    def test_render_accepts_discovered_server_url_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary)
            paths = self.runtime_config.render_runtime_configs(
                env_path, Path(temporary) / "runtime", server_url="https://discovered.ngrok.app"
            )
            rendered = paths["headscale"].read_text(encoding="utf-8")
            self.assertIn("server_url: https://discovered.ngrok.app", rendered)
            self.assertNotIn("vpn.example.com", rendered)

    def test_https_headscale_url_does_not_enable_headplane_secure_cookie(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary, {"SERVER_URL": "https://vpn.example.com"})
            paths = self.runtime_config.render_runtime_configs(env_path, Path(temporary) / "runtime")
            rendered = paths["headplane"].read_text(encoding="utf-8")
            self.assertIn("cookie_secure: false", rendered)

    def test_headplane_secure_cookie_can_be_enabled_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary, {"HEADPLANE_COOKIE_SECURE": "true"})
            paths = self.runtime_config.render_runtime_configs(env_path, Path(temporary) / "runtime")
            rendered = paths["headplane"].read_text(encoding="utf-8")
            self.assertIn("cookie_secure: true", rendered)

    def test_ipv6_host_bind_addresses_are_rejected(self):
        host_keys = (
            "HEADSCALE_HOST_BIND_ADDR",
            "HEADSCALE_METRICS_HOST_BIND_ADDR",
            "HEADPLANE_HOST_BIND_ADDR",
        )
        for key in host_keys:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                env_path = self.write_env(temporary, {key: "2001:db8::1"})
                with self.assertRaisesRegex(ValueError, key):
                    self.runtime_config.render_runtime_configs(
                        env_path, Path(temporary) / "runtime"
                    )

    def test_ipv6_headscale_bind_addresses_are_bracketed(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(
                temporary,
                {
                    "HEADSCALE_BIND_ADDR": "::",
                    "HEADSCALE_METRICS_BIND_ADDR": "::1",
                },
            )
            paths = self.runtime_config.render_runtime_configs(env_path, Path(temporary) / "runtime")
            rendered = paths["headscale"].read_text(encoding="utf-8")
            self.assertIn('listen_addr: "[::]:8080"', rendered)
            self.assertIn('metrics_listen_addr: "[::1]:9090"', rendered)

    def test_http_ngrok_url_selection(self):
        payload = {"tunnels": [{"name": "headscale", "public_url": "https://random.ngrok-free.app"}]}
        self.assertEqual(
            self.runtime_config.select_ngrok_public_url(payload, "http"),
            "https://random.ngrok-free.app",
        )

    def test_tcp_ngrok_url_is_normalized_for_headscale(self):
        cases = (
            ("tcp://2.tcp.ngrok.io:12345", "http://2.tcp.ngrok.io:12345"),
            ("tcp://192.0.2.1:12345", "http://192.0.2.1:12345"),
            ("tcp://[2001:db8::1]:12345", "http://[2001:db8::1]:12345"),
        )
        for public_url, expected in cases:
            with self.subTest(public_url=public_url):
                payload = {"tunnels": [{"name": "headscale", "public_url": public_url}]}
                self.assertEqual(
                    self.runtime_config.select_ngrok_public_url(payload, "tcp"),
                    expected,
                )

    def test_non_ascii_domains_and_urls_are_rejected(self):
        for overrides in (
            {"MAGIC_DNS_BASE_DOMAIN": "tail.ſample.net"},
            {"NGROK_DOMAIN": "vpn.ſample.com"},
            {"SERVER_URL": "https://vpn.ſample.com"},
        ):
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as temporary:
                    env_path = self.write_env(temporary, overrides)
                    with self.assertRaises(ValueError):
                        self.runtime_config.render_runtime_configs(env_path, Path(temporary) / "runtime")

        with self.assertRaisesRegex(ValueError, "no matching"):
            self.runtime_config.select_ngrok_public_url(
                {"tunnels": [{"public_url": "https://vpn.ſample.com"}]}, "http"
            )

    def test_malformed_tcp_tunnel_hostnames_are_rejected(self):
        for public_url in ("tcp://bad_host:12345", "tcp://vpn.ſample.com:12345"):
            with self.subTest(public_url=public_url):
                with self.assertRaisesRegex(ValueError, "no matching"):
                    self.runtime_config.select_ngrok_public_url(
                        {"tunnels": [{"public_url": public_url}]}, "tcp"
                    )

    def test_fixed_domain_selects_matching_http_tunnel(self):
        payload = {
            "tunnels": [
                {"public_url": "https://other.ngrok.app"},
                {"public_url": "https://vpn.example.com"},
            ]
        }
        self.assertEqual(
            self.runtime_config.select_ngrok_public_url(payload, "http", domain="vpn.example.com"),
            "https://vpn.example.com",
        )

    def test_ambiguous_or_missing_ngrok_tunnels_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.runtime_config.select_ngrok_public_url(
                {"tunnels": [{"public_url": "https://one.example"}, {"public_url": "https://two.example"}]},
                "http",
            )
        with self.assertRaisesRegex(ValueError, "no matching"):
            self.runtime_config.select_ngrok_public_url({"tunnels": []}, "http")
        with self.assertRaisesRegex(ValueError, "no matching"):
            self.runtime_config.select_ngrok_public_url(
                {"tunnels": [{"public_url": "https://other.example"}]}, "http", domain="vpn.example.com"
            )

    def test_magic_dns_equal_to_or_parent_of_server_host_is_rejected(self):
        for server_url, base_domain in (
            ("https://tail.example.com", "tail.example.com"),
            ("https://vpn.tail.example.com", "tail.example.com"),
        ):
            with self.subTest(server_url=server_url):
                with tempfile.TemporaryDirectory() as temporary:
                    env_path = self.write_env(
                        temporary,
                        {"SERVER_URL": server_url, "MAGIC_DNS_BASE_DOMAIN": base_domain},
                    )
                    with self.assertRaisesRegex(ValueError, "MagicDNS"):
                        self.runtime_config.render_runtime_configs(env_path, Path(temporary) / "runtime")

    def test_generated_files_and_directories_have_restrictive_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary)
            output = Path(temporary) / "runtime"
            paths = self.runtime_config.render_runtime_configs(env_path, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "headscale").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "headplane").stat().st_mode), 0o700)
            for path in paths.values():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def assert_cli_output_error_is_safe(self, env_path, output_path):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            self.runtime_config.main(
                ["render", "--env-file", str(env_path), "--output-dir", str(output_path)]
            )
        error = stderr.getvalue()
        self.assertIn("error: unable to write runtime configuration", error)
        self.assertNotIn("Traceback", error)
        self.assertNotIn(str(output_path), error)
        self.assertNotIn("sensitive-input", error)

    def test_cli_reports_existing_file_output_path_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary)
            output = Path(temporary) / "sensitive-output-name"
            output.write_text("already a file", encoding="utf-8")
            self.assert_cli_output_error_is_safe(env_path, output)

    def test_cli_reports_permission_error_without_sensitive_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(temporary)
            output = Path(temporary) / "sensitive-output-name"
            error = PermissionError("permission denied: sensitive-input")
            with mock.patch.object(self.runtime_config, "_secure_directory", side_effect=error):
                self.assert_cli_output_error_is_safe(env_path, output)

    def test_strict_env_rejects_malformed_duplicate_and_unsafe_data(self):
        invalid_contents = (
            "NOT AN ASSIGNMENT\n",
            "SERVER_URL=https://one.example\nSERVER_URL=https://two.example\n",
            "SERVER_URL=$(touch /tmp/not-allowed)\n",
            "UNKNOWN_OPTION=value\n",
        )
        for contents in invalid_contents:
            with self.subTest(contents=contents):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / ".env"
                    path.write_text(contents, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        self.runtime_config.parse_env(path)

    def test_invalid_required_values_booleans_modes_and_urls_are_rejected(self):
        invalid_overrides = (
            {"SERVER_URL": ""},
            {"SERVER_URL": "ftp://vpn.example.com"},
            {"CREATE_DEFAULT_USER": "yes"},
            {"HEADPLANE_COOKIE_SECURE": "yes"},
            {"NGROK_ENABLED": "1"},
            {"NGROK_MODE": "ssh"},
            {"HEADSCALE_PORT": "70000"},
            {"HEADSCALE_HOST_PORT": "70000"},
            {"HEADPLANE_HOST_BIND_ADDR": "localhost"},
            {"IPV4_PREFIX": "not-a-network"},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as temporary:
                    env_path = self.write_env(temporary, overrides)
                    with self.assertRaises(ValueError):
                        self.runtime_config.render_runtime_configs(env_path, Path(temporary) / "runtime")

    def test_tcp_ngrok_rejects_fixed_domain(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_path = self.write_env(
                temporary,
                {"NGROK_MODE": "tcp", "NGROK_DOMAIN": "vpn.example.com"},
            )
            with self.assertRaisesRegex(ValueError, "cannot be used"):
                self.runtime_config.render_runtime_configs(
                    env_path, Path(temporary) / "runtime"
                )

    def test_extra_records_initialization_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime"
            headscale = output / "headscale"
            headscale.mkdir(parents=True)
            outside = Path(temporary) / "outside.json"
            outside.write_text('{"protected": true}\n', encoding="utf-8")
            (headscale / "extra_records.json").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                self.runtime_config.initialize_extra_records(output)

            self.assertEqual(outside.read_text(encoding="utf-8"), '{"protected": true}\n')
            self.assertTrue((headscale / "extra_records.json").is_symlink())

    def test_extra_records_initialization_is_atomic_and_restrictive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime"
            self.runtime_config.initialize_extra_records(output)
            target = output / "headscale/extra_records.json"
            self.assertEqual(target.read_text(encoding="utf-8"), "[]\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(list(target.parent.glob(".extra-records-*")), [])

    def test_extra_records_repeat_initialization_preserves_existing_file_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime"
            self.runtime_config.initialize_extra_records(output)
            target = output / "headscale/extra_records.json"
            existing = b'[{"name":"router.example","type":"A","value":"192.0.2.4"}]'
            target.write_bytes(existing)
            target.chmod(0o640)
            before = target.stat()

            self.runtime_config.initialize_extra_records(output)

            after = target.stat()
            self.assertEqual(target.read_bytes(), existing)
            self.assertEqual(stat.S_IMODE(after.st_mode), 0o640)
            self.assertEqual(after.st_ino, before.st_ino)

    def test_extra_records_initialization_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "runtime"
            target = output / "headscale/extra_records.json"
            target.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "safe regular"):
                self.runtime_config.initialize_extra_records(output)

    def test_ngrok_enabled_requires_safe_authtoken(self):
        for token in ("", "token with spaces", "$(steal-secret)"):
            with self.subTest(token=token):
                with tempfile.TemporaryDirectory() as temporary:
                    env_path = self.write_env(
                        temporary,
                        {"NGROK_ENABLED": "true", "NGROK_AUTHTOKEN": token},
                    )
                    with self.assertRaises(ValueError):
                        self.runtime_config.render_runtime_configs(env_path, Path(temporary) / "runtime")


if __name__ == "__main__":
    unittest.main()
