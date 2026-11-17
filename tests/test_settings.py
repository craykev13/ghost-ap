#!/usr/bin/env python3
"""Tests for changing settings from the admin page: the config-file editor, the store that
validates and saves, and the web routes in front of them.

    python3 -m unittest discover -s tests
"""
import hashlib
import http.client
import importlib.util
import json
import logging
import os
import pathlib
import stat
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
ctl = _load("ghostap_controller_settings", ROOT / "ghostap" / "controller.py")
gui = _load("ghostap_gui_settings", ROOT / "ghostap" / "gui.py")

BASE = """# my notes
[access_point]
ssid = "ZeroFi"   # the name
passphrase_file = "{pw}"

[dhcp]
start = "192.168.4.10"
end = "192.168.4.50"
"""


class ApplyEditsTests(unittest.TestCase):
    def test_replaces_a_value_in_place_and_keeps_its_comment(self):
        out = ctl.apply_edits('[a]\nx = 1  # why\ny = 2\n', {("a", "x"): 5})
        self.assertEqual(out, '[a]\nx = 5  # why\ny = 2\n')

    def test_everything_else_in_the_file_is_untouched(self):
        text = '# top\n\n[a]\n# about x\nx = 1\n\n[b]\nz = "q"\n'
        out = ctl.apply_edits(text, {("a", "x"): 2})
        self.assertEqual(out, text.replace("x = 1", "x = 2"))

    def test_a_list_spread_over_several_lines_is_replaced_whole(self):
        text = '[a]\nl = [\n  "p",\n  "q",\n]\nafter = 1\n'
        out = ctl.apply_edits(text, {("a", "l"): ["z"]})
        self.assertEqual(out, '[a]\nl = ["z"]\nafter = 1\n')

    def test_brackets_and_hashes_inside_strings_do_not_confuse_it(self):
        text = '[a]\nl = ["a]=1", "# not a comment"]  # real\nnext = 1\n'
        out = ctl.apply_edits(text, {("a", "l"): ["k"]})
        self.assertEqual(out, '[a]\nl = ["k"]  # real\nnext = 1\n')

    def test_a_new_key_goes_at_the_end_of_its_section_before_the_blank_line(self):
        out = ctl.apply_edits('[a]\nx = 1\n\n[b]\ny = 2\n', {("a", "n"): 7})
        self.assertEqual(out, '[a]\nx = 1\nn = 7\n\n[b]\ny = 2\n')

    def test_a_new_key_in_the_last_section_goes_after_its_last_line(self):
        out = ctl.apply_edits('[a]\nx = 1\n\n\n', {("a", "n"): 7})
        self.assertEqual(out, '[a]\nx = 1\nn = 7\n\n\n')

    def test_a_commented_example_line_gets_the_real_one_right_under_it(self):
        out = ctl.apply_edits('[a]\n# n = 3\nx = 1\n', {("a", "n"): 7})
        self.assertEqual(out, '[a]\n# n = 3\nn = 7\nx = 1\n')

    def test_a_missing_section_is_added(self):
        self.assertEqual(ctl.apply_edits('[a]\nx = 1\n', {("b", "n"): True}),
                         '[a]\nx = 1\n\n[b]\nn = true\n')
        self.assertEqual(ctl.apply_edits('', {("b", "n"): 1}), '[b]\nn = 1\n')

    def test_a_key_is_only_found_in_its_own_section(self):
        out = ctl.apply_edits('[a]\nx = 1\n[b]\nx = 2\n', {("b", "x"): 9})
        self.assertEqual(out, '[a]\nx = 1\n[b]\nx = 9\n')

    def test_a_key_with_a_similar_name_is_left_alone(self):
        out = ctl.apply_edits('[a]\nxx = 1\nx = 2\n', {("a", "x"): 9})
        self.assertEqual(out, '[a]\nxx = 1\nx = 9\n')

    def test_none_removes_the_line_or_the_whole_list(self):
        self.assertEqual(ctl.apply_edits('[a]\nx = 1\ny = 2\n', {("a", "x"): None}),
                         '[a]\ny = 2\n')
        self.assertEqual(ctl.apply_edits('[a]\nl = [\n "p",\n]\ny = 2\n', {("a", "l"): None}),
                         '[a]\ny = 2\n')
        self.assertEqual(ctl.apply_edits('[a]\ny = 2\n', {("a", "x"): None}), '[a]\ny = 2\n')

    def test_values_are_written_as_valid_toml(self):
        import tomllib
        vals = {"s": 'qu"ote\\back', "i": 3, "b": False, "l": ["a b", "c=d"]}
        out = ctl.apply_edits("[a]\n", {("a", k): v for k, v in vals.items()})
        self.assertEqual(tomllib.loads(out)["a"], vals)

    def test_an_escaped_quote_inside_a_list_does_not_end_the_string(self):
        text = '[a]\nl = [\n  "x\\"]",\n  "y",\n]\nafter = 1\n'
        out = ctl.apply_edits(text, {("a", "l"): ["z"]})
        self.assertEqual(out, '[a]\nl = ["z"]\nafter = 1\n')

    def test_the_spacing_before_a_comment_is_kept(self):
        out = ctl.apply_edits('[a]\nx = 1\t\t# why\ny = 2 #tight\n', {("a", "x"): 5, ("a", "y"): 6})
        self.assertEqual(out, '[a]\nx = 5\t\t# why\ny = 6 #tight\n')

    def test_a_file_without_a_final_newline_is_handled(self):
        self.assertEqual(ctl.apply_edits('[a]\nx = 1', {("a", "y"): 2}), '[a]\nx = 1\ny = 2\n')

    def test_it_refuses_what_it_cannot_edit_safely(self):
        with self.assertRaises(ctl.GhostAPError):
            ctl.apply_edits('[a]\nx = """one\ntwo"""\n', {("a", "x"): 1})
        with self.assertRaises(ctl.GhostAPError):
            ctl.apply_edits('[a]\nx = [\n "p",\n', {("a", "x"): 1})


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        (self.dir / "pw").write_text("hunter2hunter2")
        self.path = self.dir / "ghostap.toml"
        self.path.write_text(BASE.format(pw=self.dir / "pw"))
        os.chmod(self.path, 0o640)
        self.store = ctl.SettingsStore(self.path)
        self.v = self.store.get()["version"]

    def save(self, changes, version=None):
        return self.store.save(changes, version or self.v)

    def rejected(self, changes, version=None):
        with self.assertRaises(ctl.SettingsRejected) as cm:
            self.save(changes, version)
        return cm.exception

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir())


class StoreReadTests(StoreCase):
    def test_values_come_from_the_file_with_defaults_filled_in(self):
        v = self.store.get()["values"]
        self.assertEqual(v["access_point.ssid"], "ZeroFi")
        self.assertEqual(v["access_point.channel"], 6)
        self.assertEqual(v["access_point.extra_hostapd"], [])
        self.assertIsNone(v["access_point.ieee80211n"])

    def test_every_setting_is_described_and_flagged(self):
        data = self.store.get()
        by = {f'{i["section"]}.{i["key"]}': i for i in data["schema"]}
        self.assertEqual(set(by), set(data["values"]))
        self.assertTrue(by["access_point.ssid"]["editable"])
        self.assertFalse(by["access_point.passphrase_file"]["editable"])
        self.assertFalse(by["gui.port"]["editable"])
        self.assertTrue(by["access_point.ieee80211n"]["nullable"])
        self.assertFalse(by["access_point.hostapd_debug"]["nullable"])

    def test_the_version_is_the_hash_of_the_file(self):
        self.assertEqual(self.v, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_a_missing_or_broken_file_is_a_clear_error(self):
        with self.assertRaises(ctl.GhostAPError):
            ctl.SettingsStore(self.dir / "nope.toml").get()
        self.path.write_text("[a\n")
        with self.assertRaises(ctl.GhostAPError):
            self.store.get()


class StoreSaveTests(StoreCase):
    def test_saving_changes_the_file_and_keeps_notes_and_mode(self):
        r = self.save({"access_point.channel": 11, "access_point.ssid": "New"})
        self.assertEqual(sorted(r["saved"]), ["access_point.channel", "access_point.ssid"])
        text = self.path.read_text()
        self.assertIn('ssid = "New"   # the name', text)
        self.assertIn("channel = 11", text)
        self.assertTrue(text.startswith("# my notes"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(r["version"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.store.get()["values"]["access_point.channel"], 11)

    def test_the_old_file_is_kept_beside_it_and_nothing_else_is_left_behind(self):
        old = self.path.read_text()
        self.save({"access_point.channel": 11})
        self.assertEqual((self.dir / "ghostap.toml.bak").read_text(), old)
        self.assertEqual(self.leftovers(), ["ghostap.toml", "ghostap.toml.bak", "pw"])

    def test_the_new_version_lets_the_next_save_through(self):
        r = self.save({"access_point.channel": 11})
        self.save({"access_point.channel": 1}, r["version"])
        self.assertEqual(self.store.get()["values"]["access_point.channel"], 1)

    def test_saving_what_is_already_there_writes_nothing(self):
        r = self.save({"access_point.ssid": "ZeroFi", "access_point.channel": 6,
                       "access_point.extra_hostapd": [], "network.boot_timeout": None})
        self.assertEqual(r["saved"], [])
        self.assertEqual(r["version"], self.v)
        self.assertEqual(self.leftovers(), ["ghostap.toml", "pw"])      # no .bak either

    def test_none_puts_a_setting_back_to_its_default(self):
        self.save({"access_point.channel": 11})
        r = self.save({"access_point.channel": None}, self.store.get()["version"])
        self.assertEqual(r["saved"], ["access_point.channel"])
        self.assertNotIn("channel", self.path.read_text())
        self.assertEqual(self.store.get()["values"]["access_point.channel"], 6)

    def test_an_emptied_list_removes_its_line(self):
        self.save({"access_point.extra_hostapd": ["ap_isolate=1"]})
        self.assertIn("extra_hostapd", self.path.read_text())
        self.save({"access_point.extra_hostapd": []}, self.store.get()["version"])
        self.assertNotIn("extra_hostapd", self.path.read_text())

    def test_the_address_is_stored_in_its_plain_form(self):
        self.save({"access_point.address": "10.0.0.1/24", "dhcp.start": "10.0.0.10",
                   "dhcp.end": "10.0.0.20"})
        self.assertEqual(self.store.get()["values"]["access_point.address"], "10.0.0.1/24")

    def test_every_field_error_comes_back_at_once(self):
        e = self.rejected({"network.boot_timeout": "abc", "access_point.ssid": None,
                           "gui.port": 80})
        self.assertEqual(set(e.fields), {"network.boot_timeout", "access_point.ssid",
                                         "gui.port"})
        self.assertFalse(e.conflict)

    def test_a_rule_about_the_value_is_pinned_on_its_field(self):
        e = self.rejected({"access_point.country": "USA"})
        self.assertEqual(list(e.fields), ["access_point.country"])
        self.assertIn("two-letter", e.fields["access_point.country"])

    def test_a_bad_save_changes_nothing(self):
        before = self.path.read_bytes()
        self.rejected({"access_point.channel": 99})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.leftovers(), ["ghostap.toml", "pw"])

    def test_a_rule_that_spans_settings_is_still_enforced(self):
        # channel 40 is fine on 5 GHz but not on 2.4; and the DHCP range must fit the network
        e = self.rejected({"access_point.channel": 40})
        self.assertIn("access_point.channel", e.fields)
        e = self.rejected({"access_point.address": "10.9.9.1/24"})
        self.assertIn("outside", str(e))
        self.assertEqual(self.path.read_text(), BASE.format(pw=self.dir / "pw"))

    def test_the_wrong_kind_of_value_is_refused_not_guessed_at(self):
        for name, bad in (("access_point.channel", True), ("access_point.channel", 6.5),
                          ("access_point.ieee80211n", "yes"), ("access_point.ssid", 5),
                          ("access_point.extra_hostapd", "a=1"),
                          ("access_point.address", "nonsense")):
            self.assertIn(name, self.rejected({name: bad}).fields, (name, bad))

    def test_unknown_and_locked_settings_cannot_be_written(self):
        for name in ("access_point.passphrase_file", "gui.enabled", "gui.bind",
                     "nope.nothing", "access_point"):
            self.assertIn(name, self.rejected({name: "x"}).fields)

    def test_a_stale_page_cannot_overwrite_a_newer_file(self):
        self.path.write_text(self.path.read_text() + "\n# edited over ssh\n")
        e = self.rejected({"access_point.channel": 11})
        self.assertTrue(e.conflict)
        self.assertIn("# edited over ssh", self.path.read_text())

    def test_an_unwritable_folder_is_reported_not_crashed_on(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        self.assertIn("couldn't write", str(self.rejected({"access_point.channel": 11})))

    def test_the_passphrase_is_never_read_or_returned(self):
        (self.dir / "pw").write_text("x")                  # would fail a real validation
        self.save({"access_point.channel": 11})
        self.assertNotIn("hunter2", json.dumps(self.store.get()))

    def test_two_saves_at_once_cannot_both_win(self):
        results = []

        def go(ch):
            try:
                self.save({"access_point.channel": ch})
                results.append("ok")
            except ctl.SettingsRejected as e:
                results.append("conflict" if e.conflict else "other")
        ts = [threading.Thread(target=go, args=(c,)) for c in (1, 11)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["conflict", "ok"])

    def test_the_result_loads_as_a_real_config(self):
        self.save({"access_point.channel": 11, "dhcp.lease_time": "2h",
                   "recovery.failover_grace": 120})
        cfg = ctl.load_config(self.path)
        self.assertEqual((cfg.channel, cfg.lease_time, cfg.failover_grace), (11, "2h", 120))
        self.assertEqual(cfg.ssid, "ZeroFi")


PASSWORD = "correct horse battery"


class FakeStore:
    Rejected = ctl.SettingsRejected

    def __init__(self):
        self.saved = []
        self.raises = None

    def get(self):
        if self.raises:
            raise self.raises
        return {"version": "v1", "schema": [], "values": {}}

    def save(self, changes, version):
        if self.raises:
            raise self.raises
        self.saved.append((changes, version))
        return {"saved": list(changes), "version": "v2"}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.srv = gui.make_server(lambda: {}, gui.hash_password(PASSWORD, iterations=1000),
                                   "127.0.0.1", 0, settings=self.store)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.cookie = self.req("POST", "/api/login", {"password": PASSWORD})[1]["set-cookie"] \
            .split(";")[0]

    def req(self, method, path, body=None, cookie="default", headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"X-GhostAP": "1"}
        if cookie == "default":
            cookie = getattr(self, "cookie", None)
        if cookie:
            h["Cookie"] = cookie
        h.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body)
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, data, h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, \
            (json.loads(payload) if payload else None)

    def test_signed_in_people_can_read_the_settings(self):
        s, _, b = self.req("GET", "/api/settings")
        self.assertEqual((s, b["version"]), (200, "v1"))

    def test_signed_out_people_can_neither_read_nor_write(self):
        self.assertEqual(self.req("GET", "/api/settings", cookie=None)[0], 401)
        s, _, _ = self.req("POST", "/api/settings", {"version": "v1", "changes": {"a.b": 1}},
                           cookie=None)
        self.assertEqual(s, 401)
        self.assertEqual(self.store.saved, [])

    def test_saving_passes_the_changes_and_the_version_on(self):
        s, _, b = self.req("POST", "/api/settings",
                           {"version": "v1", "changes": {"access_point.channel": 11}})
        self.assertEqual((s, b["ok"], b["saved"], b["version"]),
                         (200, True, ["access_point.channel"], "v2"))
        self.assertEqual(self.store.saved, [({"access_point.channel": 11}, "v1")])

    def test_a_post_from_another_site_or_without_the_header_is_refused(self):
        body = {"version": "v1", "changes": {"access_point.channel": 11}}
        s, _, _ = self.req("POST", "/api/settings", body, headers={"X-GhostAP": "0"})
        self.assertEqual(s, 403)
        s, _, _ = self.req("POST", "/api/settings", body, headers={"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        s, _, _ = self.req("POST", "/api/settings", raw="a=1",
                           headers={"Content-Type": "text/plain"})
        self.assertEqual(s, 415)
        self.assertEqual(self.store.saved, [])

    def test_a_malformed_request_is_a_400_and_saves_nothing(self):
        for body in ({"changes": {}}, {"version": "v1"}, {"version": 1, "changes": {}},
                     {"version": "v1", "changes": [1]}):
            self.assertEqual(self.req("POST", "/api/settings", body)[0], 400, body)
        self.assertEqual(self.store.saved, [])

    def test_a_refusal_comes_back_with_what_is_wrong_per_field(self):
        self.store.raises = ctl.SettingsRejected("Some settings need fixing.",
                                                 {"access_point.channel": "too high"})
        s, _, b = self.req("POST", "/api/settings", {"version": "v1", "changes": {"a.b": 1}})
        self.assertEqual(s, 400)
        self.assertEqual(b["fields"], {"access_point.channel": "too high"})
        self.assertEqual(b["error"], "Some settings need fixing.")

    def test_a_changed_file_is_a_409(self):
        self.store.raises = ctl.SettingsRejected("changed", conflict=True)
        s, _, b = self.req("POST", "/api/settings", {"version": "v1", "changes": {"a.b": 1}})
        self.assertEqual((s, b["error"]), (409, "changed"))

    def test_an_unexpected_failure_shows_no_internal_detail(self):
        self.store.raises = RuntimeError("secret /etc/ghostap detail")
        for method, body in (("GET", None), ("POST", {"version": "v1", "changes": {"a.b": 1}})):
            s, _, b = self.req(method, "/api/settings", body)
            self.assertEqual(s, 500)
            self.assertNotIn("secret", json.dumps(b))

    def test_a_long_list_of_extra_lines_still_fits(self):
        lines = [f"option_{i}=value" for i in range(1000)]
        self.assertGreater(len(json.dumps(lines)), 8 * 1024)
        s, _, _ = self.req("POST", "/api/settings",
                           {"version": "v1", "changes": {"access_point.extra_hostapd": lines}})
        self.assertEqual(s, 200)

    def test_a_huge_request_is_refused(self):
        s, _, _ = self.req("POST", "/api/settings", raw="x" * (gui.MAX_BODY + 1),
                           headers={"Content-Type": "application/json"})
        self.assertEqual(s, 413)

    def test_without_a_store_there_are_no_settings_routes(self):
        srv = gui.make_server(lambda: {}, gui.hash_password(PASSWORD, iterations=1000),
                              "127.0.0.1", 0)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/settings")
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
