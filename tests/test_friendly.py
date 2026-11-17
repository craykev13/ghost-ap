import importlib.util
import logging
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
logging.disable(logging.CRITICAL)
_spec = importlib.util.spec_from_file_location("ghostap_controller_friendly", ROOT / "ghostap" / "controller.py")
ctl = importlib.util.module_from_spec(_spec)
sys.modules["ghostap_controller_friendly"] = ctl
_spec.loader.exec_module(ctl)


class FriendlyConnectionTests(unittest.TestCase):
    def test_the_netplan_prefix_is_dropped(self):
        self.assertEqual(ctl.friendly_connection("netplan-wlan0-crashbandiclute", "wlan0"), "crashbandiclute")

    def test_a_network_name_with_dashes_keeps_them(self):
        self.assertEqual(ctl.friendly_connection("netplan-wlan0-my-home-wifi", "wlan0"), "my-home-wifi")

    def test_other_names_are_left_alone(self):
        for n in ("crashbandiclute", "netplan-wlan1-x", "netplan-wlan0-", None):
            self.assertEqual(ctl.friendly_connection(n, "wlan0"), n)
