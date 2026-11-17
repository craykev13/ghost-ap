#!/usr/bin/env python3
"""Tests for the admin page's Logs: what it reads, what it hides, and the web route.

    python3 -m unittest discover -s tests
"""
import http.client
import importlib.util
import json
import logging
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


logging.disable(logging.CRITICAL)
ctl = _load("ghostap_controller_logs", ROOT / "ghostap" / "controller.py")
gui = _load("ghostap_gui_logs", ROOT / "ghostap" / "gui.py")


class Rig(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.test_log = self.dir / "ghostap.log"
        self.snap = self.dir / "snap.log"
        self.calls = []
        self.answer = (0, "")
        self.reader = ctl.LogReader(self.runner, self.test_log, self.snap)

    def runner(self, argv):
        self.calls.append(argv)
        return self.answer


class RedactTests(unittest.TestCase):
    def test_password_shaped_values_are_hidden(self):
        for line in ("wpa_passphrase=hunter2hunter2", "passphrase: hunter2hunter2",
                     "psk=abcdef0123", "Password = secret123", "a WPA_PASSPHRASE=xyz here"):
            out = ctl.redact(line)
            self.assertNotRegex(out, r"hunter2|abcdef0123|secret123|xyz", line)
            self.assertIn("********", out)

    def test_ordinary_lines_are_left_alone(self):
        for line in ("wlan0: AP-STA-CONNECTED aa:bb:cc:dd:ee:ff", "mode: AP",
                     "joined crashbandiclute"):
            self.assertEqual(ctl.redact(line), line)


class JournalTests(Rig):
    def test_it_asks_for_our_units_and_the_daemons_we_start_and_nothing_else(self):
        self.reader.read("activity", 50)
        argv = self.calls[0]
        self.assertEqual(argv[0], "journalctl")
        self.assertIn("--no-pager", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "50")
        units = [argv[i + 1] for i, a in enumerate(argv) if a == "-u"]
        self.assertEqual(units, list(ctl.LOG_UNITS))
        tags = [argv[i + 1] for i, a in enumerate(argv) if a == "-t"]
        self.assertEqual(tags, ["hostapd", "dnsmasq", "ghostap"])
        self.assertEqual(argv.count("+"), 3)
        self.assertNotIn("-b", argv)

    def test_the_last_boot_uses_the_previous_boot_only(self):
        self.reader.read("previous_boot", 50)
        argv = self.calls[0]
        i = argv.index("-b")
        self.assertEqual(argv[i + 1], "-1")

    def test_lines_come_back_oldest_first_and_trimmed_to_the_count(self):
        self.answer = (0, "\n".join(f"line {i}" for i in range(100)))
        out = self.reader.read("activity", 20)["lines"]
        self.assertEqual((len(out), out[0], out[-1]), (20, "line 80", "line 99"))

    def test_journal_marker_lines_are_dropped(self):
        self.answer = (0, "-- Boot abc --\nreal line\n-- No entries --")
        self.assertEqual(self.reader.read("activity")["lines"], ["real line"])

    def test_secrets_are_hidden_and_long_lines_cut(self):
        self.answer = (0, "x wpa_passphrase=hunter2hunter2 y\n" + "z" * 5000)
        out = self.reader.read("activity")["lines"]
        self.assertNotIn("hunter2", out[0])
        self.assertEqual(len(out[1]), ctl.LOG_MAX_LINE_CHARS)

    def test_a_failure_becomes_a_note_not_an_error(self):
        self.answer = (1, "Failed to open journal: wpa_passphrase=hunter2hunter2")
        r = self.reader.read("activity")
        self.assertEqual(r["lines"], [])
        self.assertIn("Couldn't read the system log", r["note"])
        self.assertNotIn("hunter2", r["note"])

    def test_an_empty_journal_says_so(self):
        self.answer = (0, "")
        self.assertEqual(self.reader.read("activity")["note"], "Nothing logged yet.")


class FileTests(Rig):
    def test_the_live_test_log_and_snapshots_are_read_from_their_files(self):
        self.test_log.write_text("a\nb\n")
        self.snap.write_text("snap1\nsnap2\n")
        self.assertEqual(self.reader.read("test")["lines"], ["a", "b"])
        self.assertEqual(self.reader.read("snapshots")["lines"], ["snap1", "snap2"])
        self.assertEqual(self.calls, [])                  # no command is run for files

    def test_a_missing_file_is_a_friendly_note(self):
        for source in ("test", "snapshots"):
            r = self.reader.read(source)
            self.assertEqual(r["lines"], [])
            self.assertTrue(r["note"].startswith("No "), r["note"])

    def test_only_the_end_of_a_huge_file_is_read(self):
        self.snap.write_text("old\n" * 400_000 + "NEWEST\n")
        r = self.reader.read("snapshots", 10)
        self.assertEqual(r["lines"][-1], "NEWEST")
        self.assertEqual(len(r["lines"]), 10)

    def test_a_huge_file_is_never_loaded_whole(self):
        self.snap.write_text("x" * 5_000_000)
        self.assertLessEqual(len(self.reader._tail_file(self.snap)), ctl._TAIL_BYTES)

    def test_a_cut_through_a_multibyte_character_does_not_crash(self):
        self.snap.write_bytes("é".encode() * 400_000 + b"\nend\n")
        self.assertEqual(self.reader.read("snapshots")["lines"][-1], "end")

    def test_secrets_in_files_are_hidden_too(self):
        self.test_log.write_text("hostapd.conf: wpa_passphrase=hunter2hunter2\n")
        self.assertNotIn("hunter2", self.reader.read("test")["lines"][0])


class ChoiceTests(Rig):
    def test_only_the_listed_logs_can_be_asked_for(self):
        for bad in ("../etc/passwd", "", None, "Activity", "journal", "activity; reboot"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.reader.read(bad)
        self.assertEqual(self.calls, [])

    def test_the_line_count_is_kept_in_bounds(self):
        self.reader.read("activity", 10**9)
        self.assertEqual(self.calls[-1][self.calls[-1].index("-n") + 1], str(ctl.LOG_MAX_LINES))
        self.reader.read("activity", -5)
        self.assertEqual(self.calls[-1][self.calls[-1].index("-n") + 1], "10")
        for bad in ("5", None, 1.5, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.reader.read("activity", bad)

    def test_the_page_is_told_which_logs_exist(self):
        self.assertEqual([s["id"] for s in self.reader.sources()],
                         ["activity", "previous_boot", "test", "snapshots"])


PASSWORD = "correct horse battery"


class FakeLogs:
    def __init__(self):
        self.asked, self.raises = [], None

    def sources(self):
        return [{"id": "activity", "title": "Recent activity"}]

    def read(self, source, lines):
        if self.raises:
            raise self.raises
        self.asked.append((source, lines))
        return {"source": source, "lines": ["hello"], "note": ""}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.logs = FakeLogs()
        self.srv = gui.make_server(lambda: {}, gui.hash_password(PASSWORD, iterations=1000),
                                   "127.0.0.1", 0, logs=self.logs)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.cookie = self.get_login()

    def get_login(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/login", json.dumps({"password": PASSWORD}),
                  {"X-GhostAP": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.getheader("set-cookie").split(";")[0]

    def get(self, path, cookie="default"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {}
        if cookie == "default":
            cookie = self.cookie
        if cookie:
            h["Cookie"] = cookie
        c.request("GET", path, headers=h)
        r = c.getresponse()
        body = json.loads(r.read() or b"null")
        c.close()
        return r.status, body

    def test_signed_in_people_get_lines_and_the_list_of_logs(self):
        s, b = self.get("/api/logs?source=test&lines=50")
        self.assertEqual(s, 200)
        self.assertEqual((b["lines"], b["sources"][0]["id"]), (["hello"], "activity"))
        self.assertEqual(self.logs.asked, [("test", 50)])

    def test_the_defaults_are_the_activity_log_and_200_lines(self):
        self.get("/api/logs")
        self.assertEqual(self.logs.asked, [("activity", 200)])

    def test_signed_out_people_get_nothing(self):
        s, _ = self.get("/api/logs", cookie=None)
        self.assertEqual(s, 401)
        self.assertEqual(self.logs.asked, [])

    def test_a_bad_request_is_a_400_with_no_detail(self):
        self.logs.raises = ValueError("unknown log")
        self.assertEqual(self.get("/api/logs?source=x")[0], 400)
        self.assertEqual(self.get("/api/logs?lines=abc")[0], 400)

    def test_an_unexpected_failure_shows_no_internal_detail(self):
        self.logs.raises = RuntimeError("secret /var/log detail")
        s, b = self.get("/api/logs")
        self.assertEqual(s, 500)
        self.assertNotIn("secret", json.dumps(b))

    def test_without_a_reader_there_is_no_route(self):
        srv = gui.make_server(lambda: {}, gui.hash_password(PASSWORD, iterations=1000),
                              "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/api/logs")
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 404)


class PageTests(unittest.TestCase):
    def test_logs_is_a_real_page_now(self):
        html = (ROOT / "ghostap" / "web" / "index.html").read_text()
        self.assertIn('id="nav-logs"', html)
        self.assertNotIn("soon", html)
        self.assertIn('id="logs-view"', html)

    def test_log_text_is_set_as_text(self):
        js = (ROOT / "ghostap" / "web" / "app.js").read_text()
        self.assertIn('el("div", "logline "', js)
        self.assertNotIn("innerHTML", js)


class ServiceTests(unittest.TestCase):
    def test_the_page_can_read_the_journal_without_gaining_powers(self):
        unit = (ROOT / "systemd" / "ghostap-gui.service").read_text()
        self.assertIn("CapabilityBoundingSet=CAP_DAC_READ_SEARCH", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertEqual(re.findall(r"(?m)^ReadWritePaths=(.*)$", unit), ["/etc/ghostap"])


if __name__ == "__main__":
    unittest.main()
