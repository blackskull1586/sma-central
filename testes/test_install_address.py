import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preparar", SOURCE / "deploy/preparar.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
PUBLIC = "93.184.216.34"
PRIVATE = "10.1.2.3"


def link(name, *addresses, kind=None):
    result = {
        "ifname": name, "flags": ["UP", "LOWER_UP"], "link_type": "ether",
        "addr_info": [{"family": "inet", "scope": "global", "local": addr} for addr in addresses],
    }
    if kind:
        result["linkinfo"] = {"info_kind": kind}
    return result


def network(routes, addresses=(), links=None):
    def ip_output(command, **kwargs):
        if command == ["ip", "-j", "-4", "route", "show", "default"]:
            return json.dumps(routes)
        if command == ["ip", "-j", "-4", "address", "show", "dev", "eth0", "up", "scope", "global"]:
            return json.dumps([link("eth0", *addresses)])
        if command == ["ip", "-j", "-4", "-details", "address", "show", "up", "scope", "global"]:
            return json.dumps(links or [])
        raise AssertionError(command)
    return patch.object(installer.subprocess, "check_output", side_effect=ip_output)


class InstallAddressTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime = self.root / "runtime"
        (self.root / "realtime").mkdir()
        shutil.copy(SOURCE / "realtime/srs.conf.template", self.root / "realtime")
        location = patch.object(installer, "ROOT", self.root)
        location.start()
        self.addCleanup(location.stop)

    def prepare(self, *args):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["preparar.py", *args]))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            installer.main()

    def files(self):
        return {p.name: p.read_bytes() for p in self.runtime.iterdir() if p.is_file()}

    def assert_repeat_preserves_files(self):
        old = self.files()
        with patch.object(installer, "detect_ipv4", side_effect=AssertionError("consultou a rede")):
            self.prepare()
        self.assertEqual(old, self.files())

    def test_public_address_reaches_https_rtmp_webrtc(self):
        with network([{"dev": "eth0"}], [PRIVATE, PUBLIC]):
            self.prepare()
        self.assertIn("https://" + PUBLIC, (self.runtime / "Caddyfile").read_text())
        self.assertIn("rtmp://" + PUBLIC, (self.runtime / "portal.env").read_text())
        self.assertIn("candidate " + PUBLIC, (self.runtime / "srs.conf").read_text())
        self.assert_repeat_preserves_files()

    def assert_lan_configuration(self, address):
        self.assertEqual(json.loads((self.runtime / "instancia.json").read_text())["ipv4_webrtc"], address)
        caddy = (self.runtime / "Caddyfile").read_text()
        self.assertIn("https://" + address, caddy)
        self.assertIn("tls internal", caddy)
        self.assertIn("rtmp://" + address, (self.runtime / "portal.env").read_text())
        self.assertIn("candidate " + address, (self.runtime / "srs.conf").read_text())
        self.assert_repeat_preserves_files()

    def test_lan_with_default_gateway(self):
        with network([{"dev": "eth0", "gateway": "10.1.2.1"}], [PRIVATE]):
            self.prepare()
        self.assert_lan_configuration(PRIVATE)

    def test_lan_without_gateway_ignores_container_interfaces(self):
        local_ip = "192.168.88.20"
        links = [link("enp1s0", local_ip), link("docker0", "172.17.0.1", kind="bridge"),
                 link("veth3", "172.18.0.5", kind="veth")]
        with network([], links=links):
            self.prepare()
        self.assert_lan_configuration(local_ip)

    def test_explicit_public_nat_override(self):
        with patch.object(installer, "detect_ipv4", side_effect=AssertionError("consultou a rede")):
            self.prepare(PUBLIC)
        self.assertEqual(json.loads((self.runtime / "instancia.json").read_text())["ipv4_webrtc"], PUBLIC)
        self.assertIn("profile shortlived", (self.runtime / "Caddyfile").read_text())
        self.assert_repeat_preserves_files()

    def test_ambiguous_or_unusable_network_stops_before_writing(self):
        for routes, addresses, links in [
            ([], [], []), ([{"dev": "eth0"}, {"dev": "eth1"}], [], []),
            ([{"nexthops": [{"dev": "eth0"}, {"dev": "eth1"}]}], [], []),
            ([{"dev": "eth0"}], ["100.64.0.1"], []), ([{"dev": "eth0"}], [PUBLIC, "1.1.1.1"], []),
            ([{"dev": "eth0"}], [PRIVATE, "172.17.1.2"], []),
            ([], [], [link("docker0", "172.17.0.1", kind="bridge")]),
            ([], [], [link("eth0", PRIVATE), link("wlan0", "192.168.1.25")]),
        ]:
            with self.subTest(routes=routes, addresses=addresses, links=links), network(routes, addresses, links):
                with self.assertRaises(SystemExit) as error:
                    self.prepare()
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(self.runtime.exists())

    def test_explicit_private_address_can_be_reused(self):
        with patch.object(installer, "detect_ipv4", side_effect=AssertionError("consultou a rede")):
            self.prepare(PRIVATE)
        self.assertIn("tls internal", (self.runtime / "Caddyfile").read_text())
        self.assert_repeat_preserves_files()

    def test_legacy_domain_retained_on_repeat_before_and_after_migration(self):
        self.prepare("central.example.org", PUBLIC)
        self.assert_repeat_preserves_files()
        self.prepare("--adicionar-ip", PUBLIC)
        self.assert_repeat_preserves_files()

    def test_invalid_arguments(self):
        for args in [("--adicionar-ip",), ("127.0.0.1",), ("texto",),
                     ("--adicionar-ip", PUBLIC, PUBLIC), ("",), (PUBLIC, "")]:
            with self.subTest(args=args), network([{"dev": "eth0"}], [PUBLIC]):
                with self.assertRaises(SystemExit):
                    self.prepare(*args)
                self.assertFalse(self.runtime.exists())


if __name__ == "__main__":
    unittest.main()
