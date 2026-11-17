#!/usr/bin/env python3
"""Admin page tests. A real server runs on 127.0.0.1 (random port) and is talked to over
real HTTP, so cookies, headers and status codes are what a browser would see.

    python3 -m unittest discover -s tests
"""
import http.client
import importlib.util
import json
import logging
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
WEB = ROOT / "ghostap" / "web"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


logging.disable(logging.CRITICAL)      # the server logs failed logins on purpose; keep tests quiet
gui = _load("ghostap_gui_test", ROOT / "ghostap" / "gui.py")
FAST = 1000          # PBKDF2 rounds for tests (real use: gui.ITERATIONS)
PASSWORD = "correct horse battery"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class PasswordTests(unittest.TestCase):
    def test_right_password_only(self):
        h = gui.hash_password(PASSWORD, iterations=FAST)
        self.assertTrue(gui.verify_password(PASSWORD, h))
        for bad in ("", "wrong", PASSWORD + " ", PASSWORD.upper(), "x" * 500):
            self.assertFalse(gui.verify_password(bad, h), bad[:10])

    def test_the_hash_never_contains_the_password_and_is_salted(self):
        a = gui.hash_password(PASSWORD, iterations=FAST)
        b = gui.hash_password(PASSWORD, iterations=FAST)
        self.assertNotIn(PASSWORD, a)
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("pbkdf2_sha256$1000$"))

    def test_damaged_or_hostile_stored_values_are_just_wrong(self):
        for stored in ("", "junk", "a$b$c$d", "pbkdf2_sha256$x$00$00", "pbkdf2_sha256$0$00$00",
                       "pbkdf2_sha256$-5$00$00", "pbkdf2_sha256$10$zz$00",
                       "md5$1000$00$00", None):
            self.assertFalse(gui.verify_password(PASSWORD, stored), repr(stored))

    def test_absurd_iteration_counts_are_refused_not_run(self):
        # a tampered file must not be able to hang the Pi for hours
        stored = f"pbkdf2_sha256${10**9}$00ff$00ff"
        calls = []

        def spy(*args, **kw):
            calls.append(args)
            raise AssertionError("ran the hash with an absurd iteration count")
        with unittest.mock.patch.object(gui.hashlib, "pbkdf2_hmac", spy):
            self.assertFalse(gui.verify_password(PASSWORD, stored))
        self.assertEqual(calls, [])

    def test_password_file_is_private_and_readable_back(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        path = d / "sub" / "gui-password"
        gui.write_password_file(path, PASSWORD)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PASSWORD, path.read_text())
        self.assertTrue(gui.verify_password(PASSWORD, gui.read_password_hash(path)))
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_password_length_rules(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for bad in ("short", "x" * 200):
            with self.assertRaises(ValueError):
                gui.write_password_file(d / "p", bad)
        self.assertFalse((d / "p").exists())

    def test_missing_or_empty_password_file_reads_as_none(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.assertIsNone(gui.read_password_hash(d / "nope"))
        (d / "empty").write_text("\n")
        self.assertIsNone(gui.read_password_hash(d / "empty"))


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.auth = gui.Auth(gui.hash_password(PASSWORD, iterations=FAST), self.clock)

    def test_login_gives_a_long_random_token_that_works(self):
        tok, wait = self.auth.login("1.1.1.1", PASSWORD)
        self.assertEqual(wait, 0)
        self.assertGreaterEqual(len(tok), 40)
        self.assertTrue(self.auth.valid(tok))
        tok2, _ = self.auth.login("1.1.1.1", PASSWORD)
        self.assertNotEqual(tok, tok2)

    def test_garbage_tokens_are_not_valid(self):
        for t in (None, "", "abc", "x" * 50):
            self.assertFalse(self.auth.valid(t))

    def test_logout_ends_the_session(self):
        tok, _ = self.auth.login("a", PASSWORD)
        self.auth.logout(tok)
        self.assertFalse(self.auth.valid(tok))

    def test_idle_sessions_expire_but_activity_keeps_them_alive(self):
        tok, _ = self.auth.login("a", PASSWORD)
        for _ in range(5):
            self.clock.advance(gui.SESSION_IDLE - 60)
            self.assertTrue(self.auth.valid(tok))      # each look resets the idle clock
            if self.clock.t - 1000 > gui.SESSION_MAX:
                break
        tok2, _ = self.auth.login("a", PASSWORD)
        self.clock.advance(gui.SESSION_IDLE + 1)
        self.assertFalse(self.auth.valid(tok2))

    def test_sessions_have_a_hard_limit_even_if_used_constantly(self):
        tok, _ = self.auth.login("a", PASSWORD)
        while self.clock.t - 1000 < gui.SESSION_MAX - 600:
            self.clock.advance(600)
            self.assertTrue(self.auth.valid(tok))
        self.clock.advance(1200)
        self.assertFalse(self.auth.valid(tok))

    def test_five_misses_lock_that_address_out(self):
        for i in range(gui.LOGIN_FAILS):
            tok, wait = self.auth.login("9.9.9.9", "wrong")
            self.assertIsNone(tok)
        self.assertGreater(wait, 0)
        # even the RIGHT password is refused while locked out
        tok, wait = self.auth.login("9.9.9.9", PASSWORD)
        self.assertIsNone(tok)
        self.assertGreater(wait, 0)

    def test_lockout_ends_after_the_window(self):
        for _ in range(gui.LOGIN_FAILS):
            self.auth.login("9.9.9.9", "wrong")
        self.clock.advance(gui.LOGIN_WINDOW + 2)
        tok, wait = self.auth.login("9.9.9.9", PASSWORD)
        self.assertIsNotNone(tok)

    def test_one_address_locked_out_does_not_block_another(self):
        for _ in range(gui.LOGIN_FAILS):
            self.auth.login("9.9.9.9", "wrong")
        tok, _ = self.auth.login("8.8.8.8", PASSWORD)
        self.assertIsNotNone(tok)

    def test_a_good_login_clears_earlier_misses(self):
        for _ in range(gui.LOGIN_FAILS - 1):
            self.auth.login("a", "wrong")
        self.assertIsNotNone(self.auth.login("a", PASSWORD)[0])
        for _ in range(gui.LOGIN_FAILS - 1):
            self.assertIsNone(self.auth.login("a", "wrong")[0])
        self.assertIsNotNone(self.auth.login("a", PASSWORD)[0])

    def test_misses_during_a_lockout_dont_extend_it_forever(self):
        for _ in range(gui.LOGIN_FAILS):
            self.auth.login("a", "wrong")
        for _ in range(50):
            self.auth.login("a", "wrong")        # hammering while locked out
        self.clock.advance(gui.LOGIN_WINDOW + 2)
        self.assertIsNotNone(self.auth.login("a", PASSWORD)[0])


class CachedTests(unittest.TestCase):
    def test_the_provider_runs_once_per_window(self):
        clock, calls = Clock(), []
        c = gui.Cached(lambda: calls.append(1) or {"n": len(calls)}, 3, clock)
        self.assertEqual(c(), {"n": 1})
        self.assertEqual(c(), {"n": 1})
        clock.advance(2.9)
        self.assertEqual(c(), {"n": 1})
        clock.advance(0.2)
        self.assertEqual(c(), {"n": 2})


class Live(unittest.TestCase):
    """A real server, real HTTP."""

    status = {"mode": "client", "ssid": "ZeroFi", "interface": "wlan0"}

    def setUp(self):
        self.clock = Clock()
        self.calls = 0
        self.fail = False

        def provider():
            self.calls += 1
            if self.fail:
                raise RuntimeError("secret internal detail /etc/ghostap")
            return dict(self.status)
        self.srv = gui.make_server(provider, gui.hash_password(PASSWORD, iterations=FAST),
                                   "127.0.0.1", 0, self.clock)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))

    def req(self, method, path, body=None, headers=None, raw=None, csrf=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"X-GhostAP": "1"} if csrf else {}
        h.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body)
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, data, h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, payload

    def login(self, password=PASSWORD):
        s, h, b = self.req("POST", "/api/login", {"password": password})
        return s, h

    def cookie(self):
        s, h = self.login()
        self.assertEqual(s, 200)
        return h["set-cookie"].split(";")[0]

    def jget(self, path, cookie=None):
        s, h, b = self.req("GET", path, headers={"Cookie": cookie} if cookie else None)
        return s, json.loads(b)


class ServerTests(Live):
    def test_the_page_and_its_two_files_load(self):
        for path, ctype in (("/", "text/html"), ("/app.js", "text/javascript"),
                            ("/style.css", "text/css")):
            s, h, b = self.req("GET", path)
            self.assertEqual(s, 200, path)
            self.assertTrue(h["content-type"].startswith(ctype), path)
            self.assertGreater(len(b), 200)

    def test_only_those_files_can_ever_be_fetched(self):
        for path in ("/../ghostap/controller.py", "/%2e%2e/controller.py", "/web/app.js",
                     "/index.html", "/gui.py", "/app.js/", "/style.css/../gui.py",
                     "//etc/passwd", "/..%2f..%2fetc/passwd", "/\\..\\gui.py", "/static/app.js",
                     "http://evil.example/", "/api", "/api/", "/ghostap.toml",
                     "/app.js%00.html", "/APP.JS"):
            s, h, b = self.req("GET", path)
            self.assertEqual(s, 404, path)
            self.assertNotIn(b"import", b, path)

    def test_every_response_carries_the_protective_headers(self):
        for path in ("/", "/app.js", "/api/session", "/api/status", "/nope"):
            s, h, b = self.req("GET", path)
            self.assertIn("default-src 'none'", h["content-security-policy"], path)
            self.assertEqual(h["x-content-type-options"], "nosniff")
            self.assertEqual(h["x-frame-options"], "DENY")
            self.assertEqual(h["referrer-policy"], "no-referrer")
        s, h, b = self.req("GET", "/api/status")
        self.assertEqual(h["cache-control"], "no-store")

    def test_the_policy_blocks_everything_not_from_this_server(self):
        _, h, _ = self.req("GET", "/")
        csp = h["content-security-policy"]
        for part in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                     "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe", csp)
        self.assertNotRegex(csp, r"https?:|\*")

    def test_status_needs_a_login(self):
        s, h, b = self.req("GET", "/api/status")
        self.assertEqual(s, 401)
        self.assertNotIn(b"ZeroFi", b)
        self.assertEqual(self.calls, 0)             # not even looked up for strangers

    def test_session_endpoint_tells_the_page_who_it_is_talking_to(self):
        self.assertEqual(self.jget("/api/session"), (200, {"authenticated": False}))
        self.assertEqual(self.jget("/api/session", self.cookie()), (200, {"authenticated": True}))

    def test_wrong_password_gets_a_401_and_no_cookie(self):
        s, h = self.login("nope nope nope")
        self.assertEqual(s, 401)
        self.assertNotIn("set-cookie", h)

    def test_right_password_gives_a_locked_down_cookie(self):
        s, h = self.login()
        self.assertEqual(s, 200)
        cookie = h["set-cookie"]
        self.assertRegex(cookie, r"^ghostap_session=[A-Za-z0-9_-]{40,};")
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_status_with_a_login(self):
        s, data = self.jget("/api/status", self.cookie())
        self.assertEqual((s, data), (200, self.status))

    def test_a_forged_or_old_cookie_does_not_work(self):
        for ck in ("ghostap_session=abc", "ghostap_session=", "other=1",
                   "ghostap_session=" + "A" * 43):
            self.assertEqual(self.jget("/api/status", ck)[0], 401, ck)

    def test_logout_really_ends_it(self):
        ck = self.cookie()
        s, h, b = self.req("POST", "/api/logout", {}, headers={"Cookie": ck})
        self.assertEqual(s, 200)
        self.assertIn("Max-Age=0", h["set-cookie"])
        self.assertEqual(self.jget("/api/status", ck)[0], 401)

    def test_sessions_expire_on_the_server_side(self):
        ck = self.cookie()
        self.clock.advance(gui.SESSION_IDLE + 5)
        self.assertEqual(self.jget("/api/status", ck)[0], 401)

    def test_rate_limit_over_http(self):
        for _ in range(gui.LOGIN_FAILS - 1):
            self.assertEqual(self.login("wrong password")[0], 401)
        self.assertEqual(self.login("wrong password")[0], 429)   # the miss that trips it
        s, h, b = self.req("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(s, 429)
        self.assertGreater(int(h["retry-after"]), 0)
        self.assertNotIn("set-cookie", h)
        self.clock.advance(gui.LOGIN_WINDOW + 2)
        self.assertEqual(self.login()[0], 200)

    def test_status_is_cached_between_quick_polls(self):
        ck = self.cookie()
        for _ in range(5):
            self.jget("/api/status", ck)
        self.assertEqual(self.calls, 1)
        self.clock.advance(gui.STATUS_CACHE_SECONDS + 1)
        self.jget("/api/status", ck)
        self.assertEqual(self.calls, 2)

    def test_a_failing_provider_gives_a_plain_error_not_a_traceback(self):
        ck = self.cookie()
        self.fail = True
        s, h, b = self.req("GET", "/api/status", headers={"Cookie": ck})
        self.assertEqual(s, 500)
        self.assertNotIn(b"secret internal detail", b)
        self.assertNotIn(b"Traceback", b)
        self.assertNotIn(b"/etc/ghostap", b)

    def test_password_and_hash_never_appear_in_any_response(self):
        ck = self.cookie()
        blobs = [self.req("GET", p, headers={"Cookie": ck})[2] for p in
                 ("/", "/app.js", "/api/session", "/api/status", "/nope")]
        blobs.append(self.req("POST", "/api/login", {"password": "wrong"})[2])
        for b in blobs:
            self.assertNotIn(PASSWORD.encode(), b)
            self.assertNotIn(b"pbkdf2", b)


class RequestRulesTests(Live):
    def test_posts_need_the_custom_header(self):
        s, h, b = self.req("POST", "/api/login", {"password": PASSWORD}, csrf=False)
        self.assertEqual(s, 403)
        self.assertNotIn("set-cookie", h)

    def test_posts_from_another_site_are_refused(self):
        for origin in ("http://evil.example", "http://127.0.0.1:1", "null"):
            s, h, b = self.req("POST", "/api/login", {"password": PASSWORD},
                               headers={"Origin": origin})
            self.assertEqual(s, 403, origin)
            self.assertNotIn("set-cookie", h)

    def test_posts_from_our_own_page_are_fine(self):
        s, h, b = self.req("POST", "/api/login", {"password": PASSWORD},
                           headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(s, 200)

    def test_only_json_is_accepted(self):
        s, h, b = self.req("POST", "/api/login", raw="password=" + PASSWORD,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(s, 415)
        self.assertNotIn("set-cookie", h)

    def test_oversized_and_malformed_bodies_are_refused(self):
        s, _, _ = self.req("POST", "/api/login", raw="x" * (gui.MAX_BODY + 1),
                           headers={"Content-Type": "application/json"})
        self.assertEqual(s, 413)
        for raw in ("{not json", "[]", '"a string"', "null", "42"):
            s, _, _ = self.req("POST", "/api/login", raw=raw,
                               headers={"Content-Type": "application/json"})
            self.assertEqual(s, 400, raw)
        for pw in (None, 123, ["a"], {"a": 1}, True):
            s, _, _ = self.req("POST", "/api/login", {"password": pw})
            self.assertEqual(s, 400, repr(pw))

    def test_unknown_posts_and_other_methods_are_refused(self):
        self.assertEqual(self.req("POST", "/api/status", {})[0], 404)
        self.assertEqual(self.req("POST", "/", {})[0], 404)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            self.assertEqual(self.req(method, "/api/status")[0], 405, method)
        self.assertEqual(self.req("GET", "/api/login")[0], 404)

    def test_a_rejected_post_does_not_corrupt_the_next_request(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/login", "x" * 200, {"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 403)
        self.assertEqual(r.getheader("Connection"), "close")

    def test_a_server_without_a_password_refuses_to_exist(self):
        for pw in ("", None):
            with self.assertRaises(ValueError):
                gui.make_server(lambda: {}, pw, "127.0.0.1", 0)


class ServiceFileTests(unittest.TestCase):
    """The first real run failed because the service hid /home from a program that lives in
    /home. These pin down what has to be true of the unit file."""

    unit = (ROOT / "systemd" / "ghostap-gui.service").read_text()

    def test_it_can_read_the_program_in_a_home_folder(self):
        self.assertNotIn("DynamicUser", self.unit)
        self.assertNotRegex(self.unit, r"(?m)^ProtectHome=(yes|true|tmpfs)")
        self.assertIn("ProtectHome=read-only", self.unit)
        self.assertIn("CAP_DAC_READ_SEARCH", self.unit)

    def test_it_is_still_locked_down(self):
        for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateTmp=yes",
                     "CapabilityBoundingSet=CAP_DAC_READ_SEARCH"):
            self.assertIn(line, self.unit)
        caps = re.search(r"(?m)^CapabilityBoundingSet=(.*)$", self.unit).group(1).split()
        self.assertEqual(caps, ["CAP_DAC_READ_SEARCH"])        # nothing else

    def test_it_starts_the_same_command_the_boot_service_uses(self):
        boot = (ROOT / "systemd" / "ghostap.service").read_text()
        cmd = "/usr/bin/python3 /usr/local/bin/ghostap"
        self.assertIn(f"ExecStart={cmd} gui", self.unit)
        self.assertIn(cmd, boot)

    def test_a_missing_run_folder_does_not_stop_it_starting(self):
        # /run/ghostap only exists once an AP has been raised
        self.assertIn("ReadOnlyPaths=-/run/ghostap", self.unit)

    def test_it_restarts_if_it_dies(self):
        self.assertIn("Restart=on-failure", self.unit)


class PageFilesTests(unittest.TestCase):
    """The page must work offline and must not be able to run anything from outside."""

    files = {n: (WEB / n).read_text() for n in ("index.html", "app.js", "style.css")}

    def test_nothing_is_loaded_from_outside(self):
        for name, text in self.files.items():
            urls = re.findall(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s\"')]*", text)
            urls = [u for u in urls if u != "http://www.w3.org/2000/svg"]
            self.assertEqual(urls, [], name)
            self.assertNotIn("@import", text, name)
            self.assertNotRegex(text, r"url\(\s*['\"]?https?:", name)

    def test_the_page_uses_no_inline_scripts_or_styles(self):
        html = self.files["index.html"]
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=)")
        self.assertNotRegex(html, r"\sstyle=")
        self.assertNotRegex(html, r"\son[a-z]+=")
        self.assertNotIn("<style", html)
        self.assertNotIn("javascript:", html)

    def test_page_text_is_set_as_text_never_as_html(self):
        js = self.files["app.js"]
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                       "eval(", "new Function", "setTimeout(\"", "localStorage",
                       "sessionStorage"):
            self.assertNotIn(banned, js, banned)

    def test_every_element_the_script_looks_up_exists(self):
        ids = set(re.findall(r'\$\("([\w-]+)"\)', self.files["app.js"]))
        html_ids = set(re.findall(r'\sid="([\w-]+)"', self.files["index.html"]))
        self.assertEqual(ids - html_ids, set())

    def test_the_script_sends_the_header_the_server_requires(self):
        self.assertIn(f'"{gui.CSRF_HEADER}": "1"', self.files["app.js"])

    def test_the_page_is_small_enough_for_a_pi_zero(self):
        total = sum(len(t.encode()) for t in self.files.values())
        self.assertLess(total, 30_000)

    def test_the_page_files_are_exactly_the_ones_the_server_may_serve(self):
        on_disk = {p.name for p in WEB.iterdir()}
        self.assertEqual(on_disk, {n for n, _ in gui.STATIC.values()})


@unittest.skipUnless(shutil.which("node"), "node isn't installed here")
class ScriptSyntaxTests(unittest.TestCase):
    def test_app_js_parses(self):
        import subprocess
        r = subprocess.run(["node", "--check", str(WEB / "app.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
