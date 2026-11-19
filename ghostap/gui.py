"""GhostAP admin page server. Standard library only, no outside files, works offline.

Run by the ghostap-gui service (`ghostap gui`). This file knows nothing about hostapd or
NetworkManager: it is handed a `provider()` that returns the status as a dict, so it can be
tested without a Pi.

Safety, in plain words:
  * Nothing works without the admin password (stored as a salted hash, never the password).
  * Wrong passwords are slowed down: 5 misses from one address locks it out for a minute.
  * The login cookie can't be read by page scripts (HttpOnly) and isn't sent when you
    arrive from another site (SameSite=Strict). Changing requests also need a header
    that other sites can't add.
  * It serves exactly three files from the `web` folder, by name. There is no way to ask
    it for any other file.
  * Every page forbids loading anything from outside this server, so a broken or hostile
    web page elsewhere can't use it, and it works with no internet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

log = logging.getLogger("ghostap.gui")

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC = {                      # the ONLY files ever served: url path -> (file, type)
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/wordmark.png": ("wordmark.png", "image/png"),
}
HASH_NAME = "pbkdf2_sha256"
ITERATIONS = 100_000            # slow enough to hurt guessing, fast enough for a Pi Zero
MAX_ITERATIONS = 1_000_000      # refuse absurd values from a tampered file (would hang us)
MIN_PASSWORD = 8
MAX_PASSWORD = 128
MAX_BODY = 32 * 1024
SESSION_IDLE = 60 * 60          # signed out after an hour of nothing
SESSION_MAX = 12 * 60 * 60      # and never stays signed in longer than half a day
LOGIN_FAILS = 5
LOGIN_WINDOW = 60
STATUS_CACHE_SECONDS = 3
CSRF_HEADER = "X-GhostAP"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# ---- passwords ---------------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None,
                  iterations: int = ITERATIONS) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{HASH_NAME}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """True only for the right password. A damaged or odd stored value is just False."""
    try:
        name, iters, salt_hex, want_hex = stored.strip().split("$")
        iterations = int(iters)
        if name != HASH_NAME or not 1 <= iterations <= MAX_ITERATIONS:
            return False
        salt, want = bytes.fromhex(salt_hex), bytes.fromhex(want_hex)
    except (ValueError, AttributeError):
        return False
    if not password or len(password) > MAX_PASSWORD:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(got, want)


def write_password_file(path: Path, password: str) -> None:
    if not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise ValueError(f"the password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(hash_password(password) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_password_hash(path: Path) -> str | None:
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return text or None


# ---- who is signed in -------------------------------------------------------------------

class Auth:
    """Sessions and login throttling. `clock` is swappable so tests don't have to wait."""

    def __init__(self, password_hash: str, clock=time.monotonic):
        self.hash, self.clock = password_hash, clock
        self.sessions: dict[str, tuple[float, float]] = {}    # token -> (created, last_seen)
        self.fails: dict[str, list[float]] = {}               # address -> miss times
        self.lock = threading.Lock()

    def retry_after(self, who: str) -> int:
        """Seconds until this address may try again (0 = may try now)."""
        now = self.clock()
        with self.lock:
            recent = [t for t in self.fails.get(who, []) if now - t < LOGIN_WINDOW]
            self.fails[who] = recent
            if len(recent) >= LOGIN_FAILS:
                return max(1, int(LOGIN_WINDOW - (now - recent[0])) + 1)
        return 0

    def login(self, who: str, password: str):
        """-> (token or None, seconds to wait). The password is not even checked while
        locked out, so a lockout can't be used to guess faster than the limit."""
        wait = self.retry_after(who)
        if wait:
            return None, wait
        if not verify_password(password, self.hash):
            with self.lock:
                self.fails.setdefault(who, []).append(self.clock())
            return None, self.retry_after(who)
        with self.lock:
            self.fails.pop(who, None)
            token = secrets.token_urlsafe(32)
            now = self.clock()
            self.sessions[token] = (now, now)
            self._expire(now)
        return token, 0

    def _expire(self, now):
        for t, (made, seen) in list(self.sessions.items()):
            if now - seen > SESSION_IDLE or now - made > SESSION_MAX:
                del self.sessions[t]

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = self.clock()
        with self.lock:
            self._expire(now)
            entry = self.sessions.get(token)
            if entry is None:
                return False
            self.sessions[token] = (entry[0], now)
            return True

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(token, None)


class Cached:
    """Several open tabs shouldn't each make the Pi run a dozen commands every few seconds."""

    def __init__(self, fn, seconds=STATUS_CACHE_SECONDS, clock=time.monotonic):
        self.fn, self.seconds, self.clock = fn, seconds, clock
        self.at, self.value, self.lock = None, None, threading.Lock()

    def __call__(self):
        with self.lock:
            now = self.clock()
            if self.at is None or now - self.at >= self.seconds:
                self.value, self.at = self.fn(), now
            return self.value


# ---- the web server ---------------------------------------------------------------------

class App:
    def __init__(self, provider, password_hash: str, clock=time.monotonic, settings=None,
                 applier=None, logs=None):
        self.logs = logs                # something with read(source, lines) and sources()
        self.settings = settings        # something with get() and save(changes, version)
        self.applier = applier          # something with request(action)
        self.auth = Auth(password_hash, clock)
        self.status = Cached(provider, clock=clock)


def _cookie_token(header: str | None) -> str | None:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "ghostap_session":
            return value
    return None


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GhostAP"
        sys_version = ""
        timeout = 10                    # a stalled connection can't hold a thread forever
        protocol_version = "HTTP/1.1"

        # -- plumbing --
        def log_message(self, fmt, *args):      # one quiet line, never bodies or cookies
            log.debug("%s %s", self.client_address[0], fmt % args)

        def _send(self, code, body: bytes, ctype: str, extra=(), cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj, extra=()):
            self._send(code, json.dumps(obj).encode(), "application/json", extra)

        def _error(self, code, message, extra=()):
            if self.command == "POST":
                # we may not have read the request body; don't let it be mistaken for
                # the start of the next request
                self.close_connection = True
                extra = [*extra, ("Connection", "close")]
            self._json(code, {"error": message}, extra)

        def _authed(self) -> bool:
            return app.auth.valid(_cookie_token(self.headers.get("Cookie")))

        def _same_origin_json_post(self) -> dict | None:
            """Checks a POST really came from our own page; returns its JSON, or None
            after having answered with the reason."""
            if self.headers.get(CSRF_HEADER) != "1":
                self._error(403, "missing request header")
                return None
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin.split("://", 1)[-1] != host:
                self._error(403, "request came from another site")
                return None
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._error(415, "send JSON")
                return None
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._error(411, "length required")
                return None
            if length < 0 or length > MAX_BODY:
                self._error(413, "too big")
                return None
            try:
                data = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                self._error(400, "not valid JSON")
                return None
            if not isinstance(data, dict):
                self._error(400, "expected a JSON object")
                return None
            return data

        # -- routes --
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in STATIC:
                name, ctype = STATIC[path]
                try:
                    body = (WEB_DIR / name).read_bytes()
                except OSError:
                    return self._error(500, "page files are missing")
                return self._send(200, body, ctype, cache="no-cache")
            if path == "/api/session":
                return self._json(200, {"authenticated": self._authed()})
            if path == "/api/status":
                if not self._authed():
                    return self._error(401, "sign in first")
                try:
                    return self._json(200, app.status())
                except Exception:           # never show the person a traceback
                    log.exception("status failed")
                    return self._error(500, "couldn't read the status")
            if path == "/api/logs" and app.logs is not None:
                if not self._authed():
                    return self._error(401, "sign in first")
                query = parse_qs(self.path.partition("?")[2])
                source = (query.get("source") or ["activity"])[0]
                try:
                    lines = int((query.get("lines") or [200])[0])
                except ValueError:
                    return self._error(400, "lines must be a number")
                try:
                    body = app.logs.read(source, lines)
                except ValueError as e:
                    return self._error(400, str(e))
                except Exception:
                    log.exception("reading logs failed")
                    return self._error(500, "couldn't read the logs")
                return self._json(200, {**body, "sources": app.logs.sources()})
            if path == "/api/settings" and app.settings is not None:
                if not self._authed():
                    return self._error(401, "sign in first")
                try:
                    return self._json(200, app.settings.get())
                except Exception:
                    log.exception("reading settings failed")
                    return self._error(500, "couldn't read the settings")
            return self._error(404, "not found")

        def _save_settings(self, data):
            changes, version = data.get("changes"), data.get("version")
            if not isinstance(changes, dict) or not isinstance(version, str):
                return self._error(400, "expected changes and version")
            try:
                return self._json(200, {"ok": True, **app.settings.save(changes, version)})
            except app.settings.Rejected as e:
                body = {"error": str(e), "fields": e.fields}
                return self._json(409 if e.conflict else 400, body)
            except Exception:
                log.exception("saving settings failed")
                return self._error(500, "couldn't save the settings")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/settings" and app.settings is not None:
                if not self._authed():
                    return self._error(401, "sign in first")
                data = self._same_origin_json_post()
                return None if data is None else self._save_settings(data)
            if path == "/api/apply" and app.applier is not None:
                if not self._authed():
                    return self._error(401, "sign in first")
                data = self._same_origin_json_post()
                if data is None:
                    return None
                try:
                    app.applier.request(data.get("action"), data.get("confirm"))
                except app.applier.Rejected as e:
                    return self._error(400, str(e))
                except Exception:
                    log.exception("apply request failed")
                    return self._error(500, "couldn't send the request")
                log.info("admin page asked to %s", data.get("action"))
                return self._json(200, {"ok": True})
            if path not in ("/api/login", "/api/logout"):
                return self._error(404, "not found")
            data = self._same_origin_json_post()
            if data is None:
                return
            if path == "/api/logout":
                app.auth.logout(_cookie_token(self.headers.get("Cookie")))
                return self._json(200, {"ok": True}, [
                    ("Set-Cookie", "ghostap_session=; Max-Age=0; Path=/; HttpOnly; "
                                   "SameSite=Strict")])
            who = self.client_address[0]
            password = data.get("password")
            if not isinstance(password, str):
                return self._error(400, "password missing")
            token, wait = app.auth.login(who, password)
            if token:
                return self._json(200, {"ok": True}, [
                    ("Set-Cookie", f"ghostap_session={token}; Max-Age={SESSION_MAX}; "
                                   "Path=/; HttpOnly; SameSite=Strict")])
            log.warning("failed admin-page login from %s", who)
            if wait:
                return self._error(429, f"too many tries, wait {wait} seconds",
                                   [("Retry-After", str(wait))])
            return self._error(401, "wrong password")

        def _no(self):
            self._error(405, "not allowed", [("Allow", "GET, POST")])
        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _no

    return Handler


def make_server(provider, password_hash: str, host: str, port: int,
                clock=time.monotonic, settings=None, applier=None,
                logs=None) -> ThreadingHTTPServer:
    if not password_hash:
        raise ValueError("no admin password is set")
    app = App(provider, password_hash, clock, settings, applier, logs)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    server.app = app
    return server
