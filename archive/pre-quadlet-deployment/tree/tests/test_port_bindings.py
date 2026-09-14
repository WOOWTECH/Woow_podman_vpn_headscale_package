import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "port_bindings.py"


def load_port_bindings():
    spec = importlib.util.spec_from_file_location("port_bindings", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bindings = load_port_bindings()

    def inspect(self, host_ip="127.0.0.1", host_port="23000"):
        return {
            "Config": {"Labels": {
                "org.woow-headscale.project": "woow-headscale",
                "org.woow-headscale.project-dir": str(ROOT),
                "com.docker.compose.project": "woow_headscale",
                "io.podman.compose.project": "woow_headscale",
            }},
            "State": {"Running": True},
            "NetworkSettings": {
                "Ports": {
                    "3000/tcp": [{"HostIp": host_ip, "HostPort": host_port}],
                }
            },
        }

    def test_listener_parser_handles_ipv4_ipv6_and_filters_port(self):
        output = "\n".join(
            (
                "LISTEN 0 4096 127.0.0.1:23000 0.0.0.0:*",
                "LISTEN 0 4096 [::]:23000 [::]:*",
                "LISTEN 0 4096 0.0.0.0:9999 0.0.0.0:*",
            )
        )
        self.assertEqual(
            self.bindings.parse_listener_addresses(output, 23000),
            ["127.0.0.1", "::"],
        )

    def test_wildcard_bindings_overlap_specific_addresses(self):
        self.assertTrue(self.bindings.addresses_overlap("0.0.0.0", "127.0.0.1"))
        self.assertTrue(self.bindings.addresses_overlap("127.0.0.1", "0.0.0.0"))
        self.assertTrue(self.bindings.addresses_overlap("192.0.2.1", "::"))
        self.assertFalse(self.bindings.addresses_overlap("127.0.0.1", "192.0.2.1"))

    def test_owner_must_match_both_host_ip_and_host_port(self):
        item = self.inspect()
        self.assertTrue(
            self.bindings.owner_covers_listeners(
                item, "127.0.0.1", 23000, ["127.0.0.1"], "woow_headscale", ROOT
            )
        )
        self.assertFalse(
            self.bindings.owner_covers_listeners(
                item, "127.0.0.1", 24000, ["127.0.0.1"], "woow_headscale", ROOT
            )
        )
        self.assertFalse(
            self.bindings.owner_covers_listeners(
                item, "0.0.0.0", 23000, ["192.0.2.1"], "woow_headscale", ROOT
            )
        )

    def test_unrelated_listener_on_same_port_is_not_accepted(self):
        item = self.inspect(host_ip="127.0.0.1")
        self.assertFalse(
            self.bindings.owner_covers_listeners(
                item,
                "0.0.0.0",
                23000,
                ["127.0.0.1", "192.0.2.1"],
                "woow_headscale",
                ROOT,
            )
        )

    def test_same_stack_label_from_another_checkout_is_not_accepted(self):
        item = self.inspect()
        item["Config"]["Labels"]["org.woow-headscale.project-dir"] = "/other/checkout"
        self.assertFalse(
            self.bindings.owner_covers_listeners(
                item, "127.0.0.1", 23000, ["127.0.0.1"], "woow_headscale", ROOT
            )
        )

    def test_non_overlapping_listener_does_not_block_specific_binding(self):
        item = self.inspect(host_ip="192.0.2.1")
        self.assertTrue(
            self.bindings.owner_covers_listeners(
                item, "127.0.0.1", 23000, ["192.0.2.1"], "woow_headscale", ROOT
            )
        )


if __name__ == "__main__":
    unittest.main()
