import importlib.util
import logging
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
logging.disable(logging.CRITICAL)
_spec = importlib.util.spec_from_file_location("ghostap_controller_link", ROOT / "ghostap" / "controller.py")
ctl = importlib.util.module_from_spec(_spec)
sys.modules["ghostap_controller_link"] = ctl
_spec.loader.exec_module(ctl)

ROUTE = "default via 172.20.10.1 proto dhcp src 172.20.10.12 metric 600"


class Net(ctl.Shell):
    """A shell that answers `ip route` and `ping` from a script and touches nothing real."""

    def __init__(self, route=(0, ROUTE), ping=(0, "2 received"), now=0.0):
        super().__init__(dry_run=False, quiet=True)
        self.route, self.ping, self.now, self.calls = route, ping, now, []

    def probe(self, *cmd):
        self.calls.append(cmd)
        return self.route if cmd[0] == "ip" else self.ping

    def run(self, *cmd, check=True):          # nothing in these tests may change the machine
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def monotonic(self):
        return self.now


class GatewayTests(unittest.TestCase):
    def test_it_reads_the_router_address(self):
        self.assertEqual(ctl.default_gateway(Net(), "wlan0"), "172.20.10.1")

    def test_no_default_route_means_none(self):
        self.assertIsNone(ctl.default_gateway(Net(route=(0, "")), "wlan0"))

    def test_a_failing_ip_command_means_none(self):
        self.assertIsNone(ctl.default_gateway(Net(route=(127, "")), "wlan0"))

    def test_it_only_asks_about_our_interface(self):
        n = Net()
        ctl.default_gateway(n, "wlan0")
        self.assertEqual(n.calls[0][-2:], ("dev", "wlan0"))


class LinkHealthTests(unittest.TestCase):
    def test_a_reply_means_reachable(self):
        self.assertEqual(ctl.link_health(Net(), "wlan0"),
                         {"gateway": "172.20.10.1", "reachable": True})

    def test_no_reply_means_not_reachable(self):
        self.assertEqual(ctl.link_health(Net(ping=(1, "")), "wlan0")["reachable"], False)

    def test_a_ping_that_cannot_run_is_unknown_not_a_false_alarm(self):
        for rc in (2, 127):
            self.assertIsNone(ctl.link_health(Net(ping=(rc, "Operation not permitted")), "wlan0")["reachable"])

    def test_no_gateway_is_reported_and_nothing_is_pinged(self):
        n = Net(route=(0, ""))
        self.assertEqual(ctl.link_health(n, "wlan0"), {"gateway": None, "reachable": False})
        self.assertFalse([c for c in n.calls if c[0] == "ping"])

    def test_only_the_router_is_pinged_never_the_internet(self):
        n = Net()
        ctl.link_health(n, "wlan0")
        pings = [c for c in n.calls if c[0] == "ping"]
        self.assertEqual(len(pings), 1)
        self.assertEqual(pings[0][-1], "172.20.10.1")
        self.assertIn("wlan0", pings[0])
        self.assertIn("-n", pings[0])

    def test_the_check_is_quick(self):
        n = Net()
        ctl.link_health(n, "wlan0")
        ping = [c for c in n.calls if c[0] == "ping"][0]
        self.assertLessEqual(int(ping[ping.index("-c") + 1]) * int(ping[ping.index("-W") + 1]), 3)

    def test_it_changes_nothing_on_the_machine(self):
        n = Net()
        ctl.link_health(n, "wlan0")
        self.assertEqual([c for c in n.calls if c[0] not in ("ip", "ping")], [])


class CacheTests(unittest.TestCase):
    def setUp(self):
        ctl._link_cache.clear()

    def test_the_router_is_not_pinged_on_every_poll(self):
        n = Net()
        ctl.cached_link_health(n, "wlan0")
        n.now += 3
        ctl.cached_link_health(n, "wlan0")
        self.assertEqual(len([c for c in n.calls if c[0] == "ping"]), 1)

    def test_it_asks_again_after_a_while(self):
        n = Net()
        ctl.cached_link_health(n, "wlan0")
        n.now += ctl.LINK_CACHE_SECONDS + 1
        n.ping = (1, "")
        self.assertFalse(ctl.cached_link_health(n, "wlan0")["reachable"])

    def test_each_interface_has_its_own_answer(self):
        n = Net()
        ctl.cached_link_health(n, "wlan0")
        ctl.cached_link_health(n, "wlan1")
        self.assertEqual(len([c for c in n.calls if c[0] == "ping"]), 2)


class WordsTests(unittest.TestCase):
    def test_each_state_reads_plainly(self):
        d = ctl._describe_link
        self.assertIn("answering", d({"gateway": "1.2.3.4", "reachable": True}))
        self.assertIn("NOT answering", d({"gateway": "1.2.3.4", "reachable": False}))
        self.assertIn("could not check", d({"gateway": "1.2.3.4", "reachable": None}))
        self.assertIn("no router address", d({"gateway": None, "reachable": False}))


class StatusTests(unittest.TestCase):
    def setUp(self):
        ctl._link_cache.clear()

    def _status(self, mode_client=True):
        cfg = ctl.Config(
            interface="wlan0", boot_timeout=45, ssid="ZeroFi", passphrase="testpass123",
            country="US", channel=6, address=ctl.ipaddress.IPv4Interface("192.168.4.1/24"),
            dhcp_start="192.168.4.10", dhcp_end="192.168.4.50", lease_time="1h",
            rescue_timeout=90, live_timeout=120, path=pathlib.Path("/nonexistent/ghostap.toml"),
            reconnect_timeout=20)
        for name, value in (("read_state", lambda: None), ("_ap_running", lambda sh: False)):
            p = mock.patch.object(ctl, name, value)
            p.start()
            self.addCleanup(p.stop)
        n = Net()
        # collect_status asks nmcli/systemctl through run(); answer "client" or "disconnected"
        def run(*cmd, check=True):
            out = ""
            if cmd[:2] == ("nmcli", "-t") and mode_client:
                out = "wlan0:wifi:connected\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        n.run = run
        return ctl.collect_status(cfg, n), n

    def test_a_joined_network_includes_the_link_answer(self):
        st, _ = self._status(True)
        self.assertEqual(st["mode"], "client")
        self.assertEqual(st["link"], {"gateway": "172.20.10.1", "reachable": True})

    def test_other_modes_do_not_ping(self):
        st, n = self._status(False)
        self.assertIsNone(st["link"])
        self.assertFalse([c for c in n.calls if c[0] == "ping"])


if __name__ == "__main__":
    unittest.main()
