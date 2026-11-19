#!/usr/bin/env python3
"""Tests for trying settings from the admin page: the page's request side, the root helper
that carries it out, and the web route in front of them.

    python3 -m unittest discover -s tests
"""
import dataclasses
import http.client
import importlib.util
import json
import logging
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


logging.disable(logging.CRITICAL)
ctl = _load("ghostap_controller_apply", ROOT / "ghostap" / "controller.py")
gui = _load("ghostap_gui_apply", ROOT / "ghostap" / "gui.py")


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.request = self.dir / "request"
        self.result = self.dir / "result"


class ApplierTests(Folder):
    def applier(self):
        return ctl.Applier(self.request, self.result)

    def test_a_request_is_one_small_file_and_nothing_else_is_left(self):
        a = self.applier()
        a.request("test")
        self.assertEqual(json.loads(self.request.read_text()), {"action": "test"})
        self.assertTrue(a.pending())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["request"])

    def test_only_the_three_known_actions_can_be_asked_for(self):
        a = self.applier()
        for action in ("stop", "start", "TEST", "", None, 1, ["test"], "test; reboot",
                       "recover", "../x"):
            with self.assertRaises(ctl.ApplyRejected, msg=repr(action)):
                a.request(action)
        self.assertFalse(a.pending())
        for action in ("test", "commit", "abort"):
            self.request.unlink(missing_ok=True)
            a.request(action)
            self.assertEqual(json.loads(self.request.read_text())["action"], action)

    def test_a_second_request_is_refused_while_one_is_waiting(self):
        a = self.applier()
        a.request("test")
        with self.assertRaises(ctl.ApplyRejected):
            a.request("abort")
        self.assertEqual(json.loads(self.request.read_text())["action"], "test")

    def test_a_missing_folder_is_a_clear_refusal(self):
        a = ctl.Applier(self.dir / "gone" / "request", self.result)
        with self.assertRaises(ctl.ApplyRejected) as cm:
            a.request("test")
        self.assertIn("couldn't send", str(cm.exception))

    def test_the_last_result_is_read_back_and_nothing_extra_leaks(self):
        self.result.write_text(json.dumps({"action": "abort", "ok": True, "message": "Reverted.",
                                           "at": 7, "secret": "x"}))
        self.assertEqual(self.applier().last(),
                         {"action": "abort", "ok": True, "message": "Reverted.", "at": 7})

    def test_a_missing_or_garbled_result_is_just_none(self):
        a = self.applier()
        self.assertIsNone(a.last())
        for junk in ("not json", "[]", "null", '{"ok": true}', '{"message": 5}'):
            self.result.write_text(junk)
            self.assertIsNone(a.last(), junk)

    def test_two_requests_at_once_cannot_both_get_in(self):
        a = self.applier()
        out = []
        real_replace = os.replace

        def slow_replace(src, dst):         # widen the window so a missing lock shows up
            import time
            time.sleep(0.15)
            real_replace(src, dst)
        p = mock.patch.object(ctl.os, "replace", slow_replace)
        p.start()
        self.addCleanup(p.stop)

        def go(action):
            try:
                a.request(action)
                out.append("ok")
            except ctl.ApplyRejected:
                out.append("no")
        ts = [threading.Thread(target=go, args=(x,)) for x in ("test", "abort")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(out), ["no", "ok"])


class PowerRequestTests(Folder):
    def applier(self, name="PiFi3"):
        return ctl.Applier(self.request, self.result, hostname=lambda: name)

    def test_the_pis_name_has_to_be_typed_exactly(self):
        a = self.applier()
        for typed in (None, "", "   ", "pifi3", "PIFI3", "PiFi", "PiFi33", "PiFi3 x", 3,
                      ["PiFi3"], "PiFi3\n\nreboot", "yes"):
            with self.assertRaises(ctl.ApplyRejected, msg=repr(typed)):
                a.request("reboot", typed)
            with self.assertRaises(ctl.ApplyRejected, msg=repr(typed)):
                a.request("shutdown", typed)
        self.assertFalse(self.request.exists())

    def test_the_right_name_sends_the_request_with_it(self):
        a = self.applier()
        a.request("reboot", "PiFi3")
        self.assertEqual(json.loads(self.request.read_text()),
                         {"action": "reboot", "confirm": "PiFi3"})

    def test_stray_spaces_around_the_name_are_forgiven_but_not_stored(self):
        self.applier().request("shutdown", "  PiFi3 ")
        self.assertEqual(json.loads(self.request.read_text())["confirm"], "PiFi3")

    def test_a_pi_with_no_name_can_never_be_confirmed(self):
        for typed in ("", " ", None):
            with self.assertRaises(ctl.ApplyRejected):
                self.applier(name="").request("reboot", typed)

    def test_the_other_actions_still_need_no_name(self):
        for action in ("test", "commit", "abort"):
            self.request.unlink(missing_ok=True)
            self.applier().request(action)
            self.assertEqual(json.loads(self.request.read_text()), {"action": action})

    def test_a_typed_name_alone_does_not_unlock_anything_else(self):
        with self.assertRaises(ctl.ApplyRejected):
            self.applier().request("recover", "PiFi3")


class HelperTests(Folder):
    """cmd_apply_request, the root side. The real actions are swapped for recorders."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.raises = None
        self.seen_request = []
        for name in ("cmd_test_start", "cmd_test_commit", "cmd_test_abort"):
            p = mock.patch.object(ctl, name, self.recorder(name))
            p.start()
            self.addCleanup(p.stop)
        for name, val in (("APPLY_REQUEST", self.request), ("APPLY_RESULT", self.result),
                          ("RUN_DIR", self.dir)):
            p = mock.patch.object(ctl, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = dataclasses.replace(_cfg(), ssid="ZeroFi", live_timeout=120, gui_port=8480)

    def recorder(self, name):
        def fn(cfg, sh):
            self.calls.append(name)
            self.seen_request.append(self.request.exists())
            if self.raises:
                raise self.raises
            return 0
        return fn

    def ask(self, text):
        self.request.write_text(text)
        return ctl.cmd_apply_request(self.cfg, None)

    def outcome(self):
        return json.loads(self.result.read_text())

    def test_each_action_runs_exactly_its_own_command(self):
        for action, fn in (("test", "cmd_test_start"), ("commit", "cmd_test_commit"),
                           ("abort", "cmd_test_abort")):
            self.calls.clear()
            self.assertEqual(self.ask(json.dumps({"action": action})), 0)
            self.assertEqual(self.calls, [fn])
            self.assertEqual((self.outcome()["action"], self.outcome()["ok"]), (action, True))

    def test_the_request_is_deleted_before_anything_happens(self):
        self.ask('{"action": "test"}')
        self.assertEqual(self.seen_request, [False])
        self.assertFalse(self.request.exists())

    def test_a_failing_action_is_reported_and_cannot_make_systemd_retry_forever(self):
        self.raises = ctl.GhostAPError("an AP or test is already active")
        self.assertEqual(self.ask('{"action": "test"}'), 0)
        self.assertFalse(self.request.exists())
        o = self.outcome()
        self.assertEqual((o["ok"], o["message"]), (False, "an AP or test is already active"))

    def test_nothing_else_can_be_asked_for(self):
        for text in ('{"action": "stop"}', '{"action": "test; reboot"}', "[]", "null",
                     "not json", '{"act": "test"}', '{"action": ["test"]}', ""):
            self.calls.clear()
            self.assertEqual(self.ask(text), 0, text)
            self.assertEqual(self.calls, [], text)
            self.assertFalse(self.outcome()["ok"], text)
            self.assertFalse(self.request.exists(), text)

    def test_no_request_means_nothing_happens(self):
        self.assertEqual(ctl.cmd_apply_request(self.cfg, None), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.result.exists())

    def test_the_result_is_private_to_root_which_is_who_reads_it(self):
        self.ask('{"action": "abort"}')
        self.assertEqual(stat.S_IMODE(self.result.stat().st_mode), 0o600)
        self.assertIsInstance(self.outcome()["at"], int)

    def test_after_a_test_starts_the_message_says_how_to_get_back_in(self):
        self.ask('{"action": "test"}')
        m = self.outcome()["message"]
        for bit in ("ZeroFi", f"http://{self.cfg.address.ip}:8480", "120", "Keep"):
            self.assertIn(bit, m)

    # ---- restart and shut down ----
    class Sh:
        def __init__(self, outer, fail=False):
            self.outer, self.fail, self.ran = outer, fail, []

        def run(self, *cmd, check=True):
            self.ran.append(cmd)
            self.outer.result_when_run = self.outer.result.exists()
            if self.fail:
                raise ctl.GhostAPError("systemctl failed (1): Access denied")
            return None

    def ask_power(self, action, confirm="PiFi3", sh=None, state=None):
        sh = sh or self.Sh(self)
        self.request.write_text(json.dumps({"action": action, "confirm": confirm}))
        with mock.patch.object(ctl.socket, "gethostname", return_value="PiFi3"), \
                mock.patch.object(ctl, "read_state", return_value=state):
            ctl.cmd_apply_request(self.cfg, sh)
        return sh

    def test_restart_and_shut_down_run_exactly_one_systemctl_command_each(self):
        for action, verb in (("reboot", "reboot"), ("shutdown", "poweroff")):
            sh = self.ask_power(action)
            self.assertEqual(sh.ran, [("systemctl", verb)])
            o = self.outcome()
            self.assertEqual((o["action"], o["ok"]), (action, True))
        self.assertEqual(self.calls, [])        # and none of the network actions ran

    def test_the_result_is_written_before_the_machine_goes_down(self):
        self.result_when_run = None
        self.ask_power("reboot")
        self.assertTrue(self.result_when_run)

    def test_the_request_is_deleted_before_the_power_command(self):
        self.ask_power("shutdown")
        self.assertFalse(self.request.exists())

    def test_the_messages_say_what_happens_next(self):
        self.ask_power("reboot")
        self.assertIn("reload", self.outcome()["message"])
        self.ask_power("shutdown")
        self.assertIn("powered on again", self.outcome()["message"])

    def test_without_the_right_name_the_root_side_does_nothing_even_if_asked(self):
        for confirm in (None, "", "pifi3", "other", 3, ["PiFi3"]):
            sh = self.ask_power("reboot", confirm=confirm)
            self.assertEqual(sh.ran, [], repr(confirm))
            self.assertFalse(self.outcome()["ok"], repr(confirm))
            self.assertIn("didn't match", self.outcome()["message"])

    def test_a_request_with_no_confirm_field_at_all_does_nothing(self):
        self.request.write_text('{"action": "shutdown"}')
        sh = self.Sh(self)
        with mock.patch.object(ctl.socket, "gethostname", return_value="PiFi3"):
            ctl.cmd_apply_request(self.cfg, sh)
        self.assertEqual(sh.ran, [])

    def test_it_refuses_while_a_live_test_is_waiting_to_be_kept(self):
        sh = self.ask_power("reboot", state={"purpose": "test", "committed": False})
        self.assertEqual(sh.ran, [])
        self.assertIn("test is running", self.outcome()["message"])
        sh = self.ask_power("shutdown", state={"purpose": "test"})
        self.assertEqual(sh.ran, [])

    def test_a_kept_test_or_a_boot_ap_does_not_block_it(self):
        for state in ({"purpose": "test", "committed": True}, {"purpose": "boot"}, None):
            self.assertEqual(self.ask_power("reboot", state=state).ran, [("systemctl", "reboot")])

    def test_if_the_power_command_fails_the_page_is_told(self):
        sh = self.ask_power("reboot", sh=self.Sh(self, fail=True))
        o = self.outcome()
        self.assertFalse(o["ok"])
        self.assertIn("Couldn't reboot", o["message"])
        self.assertFalse(self.request.exists())

    def test_the_command_exists_needs_the_real_password_and_is_not_read_only(self):
        self.assertIs(ctl.COMMANDS["apply-request"], ctl.cmd_apply_request)
        self.assertNotIn("apply-request", ctl.READ_ONLY)


def _cfg():
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "pw").write_text("hunter2hunter2")
    f = d / "c.toml"
    f.write_text(f'[access_point]\nssid = "ZeroFi"\npassphrase_file = "{d / "pw"}"\n'
                 '[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n')
    return ctl.load_config(f)


class FakeApplier:
    Rejected = ctl.ApplyRejected

    def __init__(self):
        self.asked, self.confirms, self.raises = [], [], None

    def request(self, action, confirm=None):
        if self.raises:
            raise self.raises
        self.asked.append(action)
        self.confirms.append(confirm)


PASSWORD = "correct horse battery"


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.applier = FakeApplier()
        self.srv = gui.make_server(lambda: {}, gui.hash_password(PASSWORD, iterations=1000),
                                   "127.0.0.1", 0, applier=self.applier)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.cookie = self.req("POST", "/api/login", {"password": PASSWORD}, cookie=None)[1][
            "set-cookie"].split(";")[0]

    def req(self, method, path, body=None, cookie="default", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"X-GhostAP": "1"}
        if cookie == "default":
            cookie = self.cookie
        if cookie:
            h["Cookie"] = cookie
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        c.request(method, path, data, h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, \
            (json.loads(payload) if payload else None)

    def test_a_signed_in_page_can_ask(self):
        s, _, b = self.req("POST", "/api/apply", {"action": "test"})
        self.assertEqual((s, b), (200, {"ok": True}))
        self.assertEqual(self.applier.asked, ["test"])

    def test_the_typed_name_is_passed_on(self):
        st, _, body = self.req("POST", "/api/apply", {"action": "reboot", "confirm": "PiFi3"})
        self.assertEqual((st, body), (200, {"ok": True}))
        self.assertEqual((self.applier.asked, self.applier.confirms), (["reboot"], ["PiFi3"]))

    def test_a_wrong_name_is_a_400_with_the_reason(self):
        self.applier.raises = ctl.ApplyRejected("Type this Pi's name exactly to confirm.")
        st, _, body = self.req("POST", "/api/apply", {"action": "shutdown", "confirm": "x"})
        self.assertEqual((st, body), (400, {"error": "Type this Pi's name exactly to confirm."}))

    def test_request_text_cannot_forge_log_lines(self):
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs(gui.log, level="INFO") as cm:
            self.req("POST", "/api/apply", {"action": "test\nINFO forged: line"})
        for rec in cm.records:
            self.assertNotIn("\n", rec.getMessage())

    def test_signed_out_it_cannot(self):
        s, _, _ = self.req("POST", "/api/apply", {"action": "test"}, cookie=None)
        self.assertEqual(s, 401)
        self.assertEqual(self.applier.asked, [])

    def test_a_forged_or_cross_site_post_is_refused(self):
        body = {"action": "test"}
        self.assertEqual(self.req("POST", "/api/apply", body, headers={"X-GhostAP": "0"})[0], 403)
        self.assertEqual(self.req("POST", "/api/apply", body,
                                  headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.applier.asked, [])

    def test_a_refusal_is_a_400_with_the_reason(self):
        self.applier.raises = ctl.ApplyRejected("Still working on the last request.")
        s, _, b = self.req("POST", "/api/apply", {"action": "test"})
        self.assertEqual((s, b["error"]), (400, "Still working on the last request."))

    def test_an_unexpected_failure_shows_no_internal_detail(self):
        self.applier.raises = RuntimeError("secret /run detail")
        s, _, b = self.req("POST", "/api/apply", {"action": "test"})
        self.assertEqual(s, 500)
        self.assertNotIn("secret", json.dumps(b))

    def test_a_missing_action_is_passed_on_to_be_refused_not_guessed(self):
        self.applier.raises = ctl.ApplyRejected("unknown action")
        s, _, _ = self.req("POST", "/api/apply", {})
        self.assertEqual(s, 400)

    def test_without_an_applier_there_is_no_route(self):
        srv = gui.make_server(lambda: {}, gui.hash_password(PASSWORD, iterations=1000),
                              "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("POST", "/api/apply", "{}", {"X-GhostAP": "1",
                                               "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 404)


class PageTests(unittest.TestCase):
    js = (ROOT / "ghostap" / "web" / "app.js").read_text()

    def test_the_page_only_ever_asks_for_the_known_actions(self):
        asked = set(re.findall(r'sendApply\("(\w+)"\)', self.js))
        asked |= set(re.findall(r'\["[^"]+", "(\w+)", "(?:primary|secondary)"\]', self.js))
        asked |= set(re.findall(r'\{ action: "(\w+)", title:', self.js))
        self.assertEqual(asked, set(ctl.APPLY_ACTIONS))

    def test_restart_and_shut_down_are_only_sent_with_the_typed_name(self):
        # the one place that sends them takes the typed text, and only fires on an exact match
        self.assertEqual(re.findall(r'sendPower\(', self.js).__len__(), 2)   # definition + one call
        self.assertIn("input.value.trim() === s.hostname", self.js)
        self.assertIn("go.disabled = input.value.trim() !== s.hostname", self.js)
        self.assertNotRegex(self.js, r'sendApply\("(reboot|shutdown)"')

    def test_pressing_enter_in_the_confirmation_box_cannot_submit_the_settings_form(self):
        self.assertIn('e.key === "Enter") e.preventDefault()', self.js)

    def test_phone_keyboards_do_not_mangle_the_typed_name(self):
        for attr in ('"autocapitalize", "none"', '"autocorrect", "off"', '"spellcheck", "false"'):
            self.assertIn(attr, self.js)


class UnitFileTests(unittest.TestCase):
    path_unit = (ROOT / "systemd" / "ghostap-apply.path").read_text()
    service = (ROOT / "systemd" / "ghostap-apply.service").read_text()
    gui_unit = (ROOT / "systemd" / "ghostap-gui.service").read_text()

    def test_the_trigger_watches_the_folder_the_page_may_write(self):
        self.assertIn(f"PathExists={ctl.APPLY_REQUEST}", self.path_unit)
        self.assertIn("Unit=ghostap-apply.service", self.path_unit)
        self.assertIn("RuntimeDirectory=ghostap-gui", self.gui_unit)
        self.assertEqual(ctl.APPLY_DIR.name, "ghostap-gui")

    def test_the_helper_runs_only_the_one_command(self):
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/bin/ghostap apply-request",
                      self.service)
        self.assertEqual(self.service.count("ExecStart="), 1)

    def test_a_test_ap_outlives_the_helper_that_started_it(self):
        self.assertIn("KillMode=process", self.service)
        self.assertIn("Type=oneshot", self.service)

    def test_the_page_still_cannot_write_anywhere_it_should_not(self):
        self.assertEqual(os.path.dirname(str(ctl.APPLY_REQUEST)), "/run/ghostap-gui")
        self.assertEqual(
            [l for l in self.gui_unit.splitlines() if l.startswith("ReadWritePaths=")],
            ["ReadWritePaths=/etc/ghostap"])
        self.assertIn("ProtectSystem=strict", self.gui_unit)
        self.assertIn("CapabilityBoundingSet=CAP_DAC_READ_SEARCH", self.gui_unit)


if __name__ == "__main__":
    unittest.main()
