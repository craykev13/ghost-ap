#!/usr/bin/env python3
"""GhostAP controller (v0.2).

At boot: for up to `boot_timeout` seconds, rescan and ask NetworkManager to join a known WiFi.
If it doesn't, bring up a fallback access point (hostapd + dnsmasq).
Runs once and exits. Nothing scans or polls afterward.

Safety net: before touching the radio, a one-shot systemd timer is armed that
runs `ghostap recover`. That timer belongs to systemd, not to this process, so
it still fires if the controller is killed, times out, or loses its SSH session.

Commands:
    start      decide once: join WiFi, or start the AP
    stop       stop the AP and hand the radio back to NetworkManager
    status     show what mode the Pi is in
    recover    look at what is really running and fix it (the rescue timer runs this;
               --expect/--original carry what the timer was armed for)
    test       live AP test that reverts by itself unless you run `test --commit`
               (`test --abort` reverts right now)

Add --dry-run to any command to see what it WOULD do without touching anything.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path("/etc/ghostap/ghostap.toml")
RUN_DIR = Path("/run/ghostap")
HOSTAPD_CONF = RUN_DIR / "hostapd.conf"
HOSTAPD_PID = RUN_DIR / "hostapd.pid"
DNSMASQ_CONF = RUN_DIR / "dnsmasq.conf"
DNSMASQ_PID = RUN_DIR / "dnsmasq.pid"
STATE_FILE = RUN_DIR / "state.json"
RESCUE_UNIT = "ghostap-rescue"
FAILOVER_UNIT = "ghostap-failover"
LOG_FILE = RUN_DIR / "ghostap.log"   # survives a dead SSH session; read it afterwards
# Radio snapshots live on disk (not /run) so they survive the reboot that follows a bad AP.
SNAPSHOT_FILE = Path("/var/lib/ghostap/ap-snapshots.log")
SNAPSHOT_UNIT = "ghostap-snapshot"
SNAPSHOT_DELAY = 90          # seconds after the AP is up for the follow-up snapshot
SNAPSHOT_MAX_BYTES = 256 * 1024

# The live-test timer is armed BEFORE the AP exists. Without headroom for startup
# (hostapd verification alone can take 15s) the usable test window would shrink by
# however long startup took. live_timeout is therefore a MINIMUM usable window.
STARTUP_ALLOWANCE = 30

# `stop`, `test --abort` and `recover` return this when NetworkManager has the radio
# back but WiFi did not actually reconnect. Not the same thing as success.
EXIT_NOT_RECONNECTED = 3

REQUIRED_TOOLS = ("nmcli", "ip", "hostapd", "hostapd_cli", "dnsmasq",
                  "systemd-run", "systemctl", "pgrep")
HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

log = logging.getLogger("ghostap")


class GhostAPError(Exception):
    """Something we can explain to the user in one line."""


class Interrupted(GhostAPError):
    """We were asked to stop (SIGTERM/SIGINT). Always unwind, never swallow."""


class NotStarted(GhostAPError):
    """We refused before touching anything, so there is nothing to undo."""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    interface: str
    boot_timeout: int
    ssid: str
    passphrase: str
    country: str
    channel: int
    address: ipaddress.IPv4Interface
    dhcp_start: str
    dhcp_end: str
    lease_time: str
    rescue_timeout: int
    live_timeout: int
    path: Path
    reconnect_timeout: int = 20
    hostapd_debug: bool = False       # log every handshake step to the journal
    failover_grace: int = 0           # seconds WiFi may stay down before failover; 0 = off
    failover_join_timeout: int = 30   # one last look for WiFi before raising the AP
    # AP radio and security. Defaults reproduce the original fixed config exactly.
    hw_mode: str = "g"
    wpa: int = 2
    wpa_key_mgmt: str = "WPA-PSK"
    rsn_pairwise: str = "CCMP"
    ieee80211w: int = 0
    auth_algs: int = 1
    ieee80211n: bool | None = None            # None = don't write the line at all
    wmm_enabled: bool | None = None
    ht_capab: str = ""
    ignore_broadcast_ssid: bool | None = None
    max_num_sta: int = 0                      # 0 = don't write the line
    beacon_int: int = 0                       # 0 = don't write the line
    extra_hostapd: tuple[str, ...] = ()
    # DHCP
    dns_port: int = 0                         # 0 = DHCP only (dnsmasq's DNS side off)
    router: str = ""                          # "" = dnsmasq's default; "none" = no gateway
    dns_servers: tuple[str, ...] = ()
    extra_dnsmasq: tuple[str, ...] = ()
    # Diagnostics
    snapshots: bool = True
    snapshot_delay: int = SNAPSHOT_DELAY
    # Admin web page
    gui_enabled: bool = False
    gui_port: int = 8480
    gui_bind: str = "0.0.0.0"


# --------------------------------------------------------------------------
# The settings list. ONE place that says what every setting is called, what it
# does, what it defaults to and what it may be. Config loading, validation, the
# example config, `ghostap schema` (for installers and GUIs) all read this list.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    section: str
    key: str
    field: str                    # attribute on Config
    kind: str                     # str | int | bool | ipif | secret_file | list
    default: object
    description: str
    example: object = None        # shown in the example config for required/unset settings
    minimum: int | None = None
    maximum: int | None = None
    zero_or_min: bool = False     # 0 means "off"; anything else must be >= minimum
    choices: tuple | None = None  # allowed values
    token_choices: tuple | None = None   # each space-separated word must be one of these
    unit: str = ""
    required: bool = False
    advanced: bool = False
    commented: bool = False       # in the example config, leave it commented out


def _S(section, key, kind, default, description, field=None, **kw) -> Setting:
    return Setting(section, key, field or key, kind, default, description, **kw)


SETTINGS: tuple[Setting, ...] = (
    # ---- [network] ----
    _S("network", "interface", "str", "wlan0",
       "The WiFi interface GhostAP controls."),
    _S("network", "boot_timeout", "int", 60,
       "Seconds to look for and join a known WiFi at boot before starting the AP.",
       minimum=0, unit=" seconds"),
    # ---- [access_point] ----
    _S("access_point", "ssid", "str", None,
       "The name of the fallback WiFi network (1-32 bytes).",
       required=True, example="ZeroFi"),
    _S("access_point", "passphrase_file", "secret_file", None,
       "File holding the AP password (8-63 characters), so the password itself stays "
       "out of this file. Lock that file down with chmod 600.",
       field="passphrase", required=True, example="/etc/ghostap/passphrase"),
    _S("access_point", "country", "str", "US",
       "Two-letter regulatory country code. It decides which channels and power "
       "levels are legal where you are."),
    _S("access_point", "channel", "int", 6,
       "WiFi channel. 2.4 GHz: 1-14 (what's legal depends on country). 5 GHz "
       "(hw_mode = \"a\"): 36-177. A quiet channel helps a lot in crowded buildings.",
       minimum=1, maximum=177),
    _S("access_point", "address", "ipif", "192.168.4.1/24",
       "The AP's own address and network size."),
    _S("access_point", "hw_mode", "str", "g",
       "Radio band: \"g\" = 2.4 GHz, \"a\" = 5 GHz (only on hardware that has it), "
       "\"b\" = old 2.4 GHz 802.11b.",
       choices=("g", "a", "b"), advanced=True),
    _S("access_point", "wpa", "int", 2,
       "WPA version: 1, 2 or 3 (a bitmask: 1 = WPA, 2 = WPA2). Leave on 2 unless you "
       "know you need otherwise.",
       choices=(1, 2, 3), advanced=True),
    _S("access_point", "wpa_key_mgmt", "str", "WPA-PSK",
       "Key management, space separated. WPA-PSK is the normal home-network mode; "
       "WPA-PSK-SHA256 and SAE (WPA3) exist on newer clients. SAE also needs a "
       "sae_password line in extra_hostapd.",
       token_choices=("WPA-PSK", "WPA-PSK-SHA256", "SAE", "FT-PSK", "FT-SAE"),
       advanced=True),
    _S("access_point", "rsn_pairwise", "str", "CCMP",
       "Encryption cipher(s), space separated. CCMP (AES) is the safe choice. TKIP is "
       "old and weak, only for ancient devices.",
       token_choices=("CCMP", "TKIP", "GCMP", "CCMP-256", "GCMP-256"), advanced=True),
    _S("access_point", "ieee80211w", "int", 0,
       "Protected management frames: 0 = off, 1 = optional, 2 = required. Some "
       "phones need 0 or 1 to join without trouble.",
       choices=(0, 1, 2), advanced=True),
    _S("access_point", "auth_algs", "int", 1,
       "Authentication algorithms: 1 = open system (what WPA uses), 2 = shared key, "
       "3 = both.",
       choices=(1, 2, 3), advanced=True),
    _S("access_point", "ieee80211n", "bool", None,
       "Turn 802.11n (faster speeds) on or off. Not set = left off, as before.",
       example=True, commented=True, advanced=True),
    _S("access_point", "wmm_enabled", "bool", None,
       "WiFi multimedia (needed for 802.11n speeds on many clients).",
       example=True, commented=True, advanced=True),
    _S("access_point", "ht_capab", "str", "",
       "802.11n capability string, for example \"[SHORT-GI-20]\". Empty = not written.",
       example="[SHORT-GI-20]", commented=True, advanced=True),
    _S("access_point", "ignore_broadcast_ssid", "bool", None,
       "Hide the network name (true) or show it (false). Not set = hostapd's default "
       "(shown). Hidden networks are harder to join and not really more secure.",
       example=True, commented=True, advanced=True),
    _S("access_point", "max_num_sta", "int", 0,
       "Most devices allowed to join at once. 0 = don't write the line (hostapd's "
       "own limit).",
       minimum=0, example=8, commented=True, advanced=True),
    _S("access_point", "beacon_int", "int", 0,
       "Time between beacon announcements, in 1.024 ms units (hostapd's default is "
       "100). 0 = don't write the line.",
       minimum=0, example=100, commented=True, advanced=True),
    _S("access_point", "hostapd_debug", "bool", False,
       "true: log every handshake step so you can see why a device can't join "
       "(journalctl -t hostapd). Chatty: turn off when you're done."),
    _S("access_point", "extra_hostapd", "list", (),
       "Raw hostapd.conf lines, added untouched at the end. For anything not listed "
       "above. Settings that have their own entry here (ssid, channel, ...) and the "
       "ones GhostAP needs to supervise hostapd (interface, ctrl_interface, driver) "
       "can't be set this way.",
       example=["max_listen_interval=100"], commented=True, advanced=True),
    # ---- [dhcp] ----
    _S("dhcp", "start", "str", None,
       "First address handed out. Must be inside the AP's network.",
       field="dhcp_start", required=True, example="192.168.4.10"),
    _S("dhcp", "end", "str", None,
       "Last address handed out.",
       field="dhcp_end", required=True, example="192.168.4.50"),
    _S("dhcp", "lease_time", "str", "1h",
       "How long a device keeps its address: a number plus m, h or d (for example "
       "\"12h\"), or \"infinite\"."),
    _S("dhcp", "dns_port", "int", 0,
       "0 = DHCP only (dnsmasq's DNS side stays off so it can't fight anything on "
       "port 53). Set a port, such as 53, to turn DNS forwarding on.",
       minimum=0, maximum=65535, advanced=True),
    _S("dhcp", "router", "str", "",
       "Gateway to tell devices about. Empty = dnsmasq's default (the AP itself). "
       "An address, or \"none\" to hand out no gateway at all.",
       example="none", commented=True, advanced=True, field="router"),
    _S("dhcp", "dns_servers", "list", (),
       "DNS servers to tell devices to use, for example [\"1.1.1.1\"]. Empty = none.",
       example=["1.1.1.1"], commented=True, advanced=True),
    _S("dhcp", "extra_dnsmasq", "list", (),
       "Raw dnsmasq.conf lines, added untouched at the end. For example a fixed "
       "address for one device: \"dhcp-host=aa:bb:cc:dd:ee:ff,192.168.4.20\". The "
       "lines GhostAP needs to supervise dnsmasq (interface, pid-file, ...) can't be "
       "set this way.",
       example=["dhcp-host=aa:bb:cc:dd:ee:ff,192.168.4.20"], commented=True,
       advanced=True),
    # ---- [recovery] ----
    _S("recovery", "rescue_timeout", "int", 90,
       "Safety net: if the controller dies mid-switch, systemd runs `ghostap recover` "
       "after this many seconds.",
       minimum=30, unit=" seconds"),
    _S("recovery", "reconnect_timeout", "int", 20,
       "After handing the radio back, wait this long for WiFi to actually reconnect "
       "and report it honestly.",
       minimum=5, unit=" seconds"),
    _S("recovery", "failover_grace", "int", 0,
       "0 = off. If WiFi drops AFTER boot and stays down this many seconds, take one "
       "last look and then raise the AP. Needs the NetworkManager hook "
       "(nm-dispatcher/90-ghostap). Suggested: 120.",
       minimum=15, zero_or_min=True, unit=" seconds"),
    _S("recovery", "failover_join_timeout", "int", 30,
       "That last look for WiFi before failing over.",
       minimum=10, unit=" seconds"),
    # ---- [testing] ----
    _S("testing", "live_timeout", "int", 120,
       "`ghostap test` gives you AT LEAST this long once the AP is up, then reverts "
       "by itself unless you run `ghostap test --commit`. The timer is armed ~30 s "
       "earlier to cover AP startup.",
       minimum=30, unit=" seconds"),
    # ---- [diagnostics] ----
    _S("diagnostics", "snapshots", "bool", True,
       "Save a snapshot of the radio's state whenever an AP comes up "
       "(/var/lib/ghostap/ap-snapshots.log). Handy when a device can't see the AP."),
    _S("diagnostics", "snapshot_delay", "int", SNAPSHOT_DELAY,
       "Take a second snapshot this long after the AP is up, to see if it degrades.",
       minimum=10, unit=" seconds", advanced=True),
    # ---- [gui] ----
    _S("gui", "enabled", "bool", False,
       "Serve the admin web page (run by the ghostap-gui service). It needs a password "
       "set with `sudo ghostap gui-password`.", field="gui_enabled"),
    _S("gui", "port", "int", 8480,
       "Port for the admin page. Open http://<the Pi's address>:<port> in a browser.",
       minimum=1024, maximum=65535, field="gui_port"),
    _S("gui", "bind", "str", "0.0.0.0",
       "Which address the admin page listens on. 0.0.0.0 = every network it is on "
       "(home WiFi and its own AP). 127.0.0.1 = only the Pi itself. Never exposed to "
       "the internet unless your router forwards the port.",
       field="gui_bind", advanced=True),
)

_SECTION_ORDER = ("network", "access_point", "dhcp", "recovery", "testing", "diagnostics",
                  "gui")
_BY_SECTION: dict[str, dict[str, Setting]] = {}
for _s in SETTINGS:
    _BY_SECTION.setdefault(_s.section, {})[_s.key] = _s
del _s

# Settings GhostAP itself relies on to supervise the daemons. Not settable through
# the raw "extra" lines (or through the named ones: those are fixed in code).
_HOSTAPD_FIXED = frozenset({"interface", "ctrl_interface", "driver"})
_DNSMASQ_FIXED = frozenset({"interface", "bind-interfaces", "pid-file", "dhcp-leasefile"})
# Raw-line keys that have a named setting: say which one to use instead.
_HOSTAPD_NAMED = {
    "ssid": "ssid", "country_code": "country", "hw_mode": "hw_mode", "channel": "channel",
    "wpa": "wpa", "wpa_passphrase": "passphrase_file", "wpa_key_mgmt": "wpa_key_mgmt",
    "rsn_pairwise": "rsn_pairwise", "ieee80211w": "ieee80211w", "auth_algs": "auth_algs",
    "ieee80211n": "ieee80211n", "wmm_enabled": "wmm_enabled", "ht_capab": "ht_capab",
    "ignore_broadcast_ssid": "ignore_broadcast_ssid", "max_num_sta": "max_num_sta",
    "beacon_int": "beacon_int", "logger_syslog": "hostapd_debug",
    "logger_syslog_level": "hostapd_debug",
}
_DNSMASQ_NAMED = {"port": "dns_port"}


# Stands in for the passphrase when a read-only command (status, preflight) loads the
# config: those never touch the password, so they shouldn't need root to read it.
_UNREAD_SECRET = "(not read)"


def _real_passphrase(cfg) -> str:
    if cfg.passphrase == _UNREAD_SECRET:
        raise GhostAPError("internal error: tried to build an AP from a config loaded "
                           "without its passphrase")
    return cfg.passphrase


def _read_secret(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        text = path.read_text().strip()
    except OSError as e:
        raise GhostAPError(f"can't read passphrase file {path}: {e}")
    if mode & 0o077:
        log.warning("%s is readable by other users. Run: chmod 600 %s", path, path)
    return text


def _check_extra_lines(setting: str, lines, *, fixed, named, key_re: str) -> None:
    pattern = re.compile(key_re)
    for line in lines:
        if not isinstance(line, str) or not line.strip():
            raise GhostAPError(f"{setting}: every entry must be a non-empty line of text")
        if any(c in line for c in "\n\r\0") or len(line) > 512:
            raise GhostAPError(f"{setting}: each entry must be ONE line (max 512 characters), "
                               f"no line breaks: {line[:40]!r}")
        if line.lstrip().startswith("#"):
            continue
        m = pattern.match(line)
        if not m:
            raise GhostAPError(f"{setting}: doesn't look like a config line: {line!r}")
        key = m.group(1)
        if key in fixed:
            raise GhostAPError(f"{setting}: '{key}' is fixed because GhostAP uses it to "
                               "supervise the daemon")
        if key in named:
            raise GhostAPError(f"{setting}: '{key}' has its own setting; use '{named[key]}'")


def validate(cfg: Config) -> None:
    for s in SETTINGS:
        v = getattr(cfg, s.field)
        if s.kind == "int":
            if s.choices is not None and v not in s.choices:
                raise GhostAPError(f"{s.key} must be one of {', '.join(map(str, s.choices))}")
            if s.zero_or_min and v == 0:
                continue
            if s.minimum is not None and v < s.minimum:
                what = "0 (off) or at least" if s.zero_or_min else "at least"
                raise GhostAPError(f"{s.key} must be {what} {s.minimum}{s.unit}"
                                   if s.minimum else f"{s.key} can't be negative")
            if s.maximum is not None and v > s.maximum:
                raise GhostAPError(f"{s.key} must be at most {s.maximum}{s.unit}")
        elif s.kind == "str":
            if s.choices is not None and v not in s.choices:
                raise GhostAPError(f"{s.key} must be one of {', '.join(s.choices)}")
            if s.token_choices is not None:
                words = v.split()
                bad = [w for w in words if w not in s.token_choices]
                if not words or bad:
                    raise GhostAPError(f"{s.key}: unknown value {' '.join(bad) or repr(v)!r}; "
                                       f"use any of {', '.join(s.token_choices)}")
    if not (8 <= len(cfg.passphrase) <= 63 and cfg.passphrase.isascii()):
        raise GhostAPError("passphrase must be 8-63 ASCII characters")
    if not (1 <= len(cfg.ssid.encode()) <= 32) or "\n" in cfg.ssid:
        raise GhostAPError("ssid must be 1-32 bytes with no newlines")
    if not re.fullmatch(r"[A-Za-z0-9]{2}", cfg.country):
        raise GhostAPError("country must be a two-letter code like US or GB "
                           f"(got {cfg.country!r})")
    # Which channels exist depends on the band. Whether one is LEGAL where you are is
    # for hostapd and the country code to say; a refusal shows up as an AP that fails
    # to start, which GhostAP rolls back from.
    lo, hi = (36, 177) if cfg.hw_mode == "a" else (1, 14)
    if not lo <= cfg.channel <= hi:
        band = "5 GHz" if cfg.hw_mode == "a" else "2.4 GHz"
        raise GhostAPError(f"channel must be {lo}-{hi} for {band} (hw_mode = \"{cfg.hw_mode}\")")
    try:
        ipaddress.ip_address(cfg.gui_bind)
    except ValueError:
        raise GhostAPError(f"bind must be an IP address like 0.0.0.0 or 127.0.0.1 "
                           f"(got {cfg.gui_bind!r})")
    net = cfg.address.network
    for ip in (cfg.dhcp_start, cfg.dhcp_end):
        if ipaddress.IPv4Address(ip) not in net:
            raise GhostAPError(f"DHCP address {ip} is outside {net}")
    if cfg.address.ip in (net.network_address, net.broadcast_address):
        raise GhostAPError("AP address can't be the network or broadcast address")
    if cfg.router and cfg.router != "none":
        ipaddress.IPv4Address(cfg.router)
    for ip in cfg.dns_servers:
        ipaddress.IPv4Address(ip)
    _check_extra_lines("extra_hostapd", cfg.extra_hostapd, fixed=_HOSTAPD_FIXED,
                       named=_HOSTAPD_NAMED, key_re=r"^\s*([A-Za-z0-9_]+(?:\[\d+\])?)=")
    _check_extra_lines("extra_dnsmasq", cfg.extra_dnsmasq, fixed=_DNSMASQ_FIXED,
                       named=_DNSMASQ_NAMED, key_re=r"^\s*([a-z][a-z0-9-]*)(?:=|$)")


def _warn_if_weak(cfg: Config) -> None:
    if "TKIP" in cfg.rsn_pairwise.split() or cfg.wpa == 1:
        log.warning("this AP allows old, weak WiFi security (TKIP / WPA1). "
                    "Fine for a device that needs it; not a good default")
    if cfg.ieee80211w == 0 and "SAE" in cfg.wpa_key_mgmt.split():
        log.warning("SAE (WPA3) normally needs ieee80211w = 1 or 2")


def _coerce(s: Setting, value, secrets: bool = True):
    if s.kind == "str":
        if not isinstance(value, str):
            raise GhostAPError(f"{s.key} must be text, in quotes")
        return value
    if s.kind == "int":
        try:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError
            return int(value)
        except ValueError:
            raise GhostAPError(f"{s.key} must be a whole number")
    if s.kind == "bool":
        if not isinstance(value, bool):
            raise GhostAPError(f"{s.key} must be true or false")
        return value
    if s.kind == "ipif":
        return ipaddress.IPv4Interface(str(value))
    if s.kind == "secret_file":
        if not secrets:
            return _UNREAD_SECRET       # read-only commands never need the password
        return _read_secret(Path(str(value)))
    if s.kind == "list":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise GhostAPError(f"{s.key} must be a list of text lines, in quotes")
        return tuple(value)
    raise AssertionError(f"unknown setting kind {s.kind}")


def load_config(path: Path, secrets: bool = True) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise GhostAPError(f"config not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise GhostAPError(f"bad syntax in {path}: {e}")

    for section, body in raw.items():
        known = _BY_SECTION.get(section)
        if known is None or not isinstance(body, dict):
            log.warning("unknown section [%s] in the config; ignoring it", section)
            continue
        for key in body:
            if key not in known:
                log.warning("unknown setting '%s' in [%s]; ignoring it (typo?)", key, section)

    kwargs: dict = {}
    try:
        for s in SETTINGS:
            body = raw.get(s.section)
            body = body if isinstance(body, dict) else {}
            if s.key in body:
                kwargs[s.field] = _coerce(s, body[s.key], secrets)
            elif s.required:
                raise GhostAPError(f"missing setting in config: '{s.key}' in [{s.section}]")
            else:
                kwargs[s.field] = s.default if s.kind != "ipif" else \
                    ipaddress.IPv4Interface(s.default)
        cfg = Config(path=Path(path).resolve(), **kwargs)
        validate(cfg)
    except ValueError as e:
        raise GhostAPError(f"invalid value in config: {e}")
    _warn_if_weak(cfg)
    return cfg


def settings_schema() -> list[dict]:
    """The settings list as plain data: what an installer or GUI builds its forms from."""
    out = []
    for s in SETTINGS:
        default = s.default
        if isinstance(default, tuple):
            default = list(default)
        out.append({
            "section": s.section, "key": s.key, "type": s.kind, "default": default,
            "description": s.description, "required": s.required, "advanced": s.advanced,
            "minimum": s.minimum, "maximum": s.maximum, "zero_means_off": s.zero_or_min,
            "choices": list(s.choices) if s.choices else None,
            "allowed_words": list(s.token_choices) if s.token_choices else None,
            "unit": s.unit.strip() or None, "example": s.example,
        })
    return out


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(v)          # a JSON string is a valid TOML basic string


def render_example_config() -> str:
    """ghostap.toml.example, generated from the settings list so it can't drift."""
    import textwrap
    out = ["# Copy to /etc/ghostap/ghostap.toml and edit.",
           "# Generated from the settings list in controller.py:",
           "#   python3 ghostap/controller.py example-config > ghostap.toml.example",
           "# Lines starting with a single # are settings that are off until you remove it.",
           ""]
    for section in _SECTION_ORDER:
        out.append(f"[{section}]")
        for s in (x for x in SETTINGS if x.section == section):
            for line in textwrap.wrap(s.description, 76):
                out.append(f"# {line}")
            shown = s.example if (s.required or s.commented) else s.default
            line = f"{s.key} = {_toml_value(shown)}"
            out.append(("# " + line) if s.commented else line)
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------
# Changing settings in the config file (what the admin page's Settings form uses).
# Edits only the lines that change, so the person's own comments and layout survive.
# --------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_MULTILINE_STRING = ('"' * 3, "'" * 3)


def _is_editable(s: Setting) -> bool:
    """The password file path and the admin page's own settings stay a job for the
    command line: a mistake there can lock you out of the page that's editing them."""
    return s.kind != "secret_file" and s.section != "gui"


def _value_end(lines: list[str], i: int) -> tuple[int, str]:
    """Last line of the value that starts on lines[i] (arrays can span lines), plus any
    trailing comment on that last line. Raises if it can't be sure (multi-line strings)."""
    depth, quote, n = 0, "", i
    text = lines[i].split("=", 1)[1]
    if text.lstrip().startswith(_MULTILINE_STRING):
        raise GhostAPError("a setting uses a multi-line string; edit the file by hand")
    while True:
        j, comment = 0, ""
        while j < len(text):
            ch = text[j]
            if quote:
                if quote == '"' and ch == "\\":
                    j += 1
                elif ch == quote:
                    quote = ""
            elif ch in ('"', "'"):
                quote = ch
            elif ch == "#":
                gap = len(text[:j]) - len(text[:j].rstrip())    # keep the spacing before it
                comment = text[j - gap:].rstrip("\n")
                break
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            j += 1
        if depth <= 0:
            return n, comment
        n += 1
        if n >= len(lines):
            raise GhostAPError("the config has an unfinished list; edit the file by hand")
        text = lines[n]


def apply_edits(text: str, edits: dict) -> str:
    """edits maps (section, key) -> a value to write, or None to remove the line (the
    setting goes back to its default). Returns the new file text."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for (section, key), value in edits.items():
        bounds = None
        for i, line in enumerate(lines):
            m = _HEADER_RE.match(line)
            if m is None:
                continue
            if bounds is not None:
                bounds = (bounds[0], i)
                break
            if m.group(1).strip() == section:
                bounds = (i, len(lines))
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        found = None
        if bounds:
            for i in range(bounds[0] + 1, bounds[1]):
                if key_re.match(lines[i]):
                    found = i
                    break
        if found is not None:
            end, comment = _value_end(lines, found)
            if value is None:
                lines[found:end + 1] = []
            else:
                tail = comment if comment.startswith((" ", "\t")) else f"  {comment}" if comment else ""
                lines[found:end + 1] = [f"{key} = {_toml_value(value)}{tail}\n"]
            continue
        if value is None:
            continue                       # not in the file: already the default
        new = f"{key} = {_toml_value(value)}\n"
        if bounds is None:
            if lines and lines[-1].strip():
                lines.append("\n")
            lines += [f"[{section}]\n", new]
            continue
        at = bounds[1]
        while at > bounds[0] + 1 and not lines[at - 1].strip():
            at -= 1                        # keep blank lines between sections
        commented = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
        for i in range(bounds[0] + 1, bounds[1]):
            if commented.match(lines[i]):
                at = i + 1                 # right under the example line
                break
        lines.insert(at, new)
    return "".join(lines)


class SettingsRejected(GhostAPError):
    """The change wasn't saved. `fields` maps a setting (section.key, or "" for the
    form as a whole) to what's wrong; `conflict` means the file changed under you."""
    def __init__(self, message: str, fields: dict | None = None, conflict: bool = False):
        super().__init__(message)
        self.fields = fields or {}
        self.conflict = conflict


def _default_of(section: str, key: str):
    d = _BY_SECTION[section][key].default
    return list(d) if isinstance(d, tuple) else d


class SettingsStore:
    Rejected = SettingsRejected

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> tuple[bytes, dict]:
        try:
            data = self.path.read_bytes()
            raw = tomllib.loads(data.decode())
        except (OSError, UnicodeDecodeError) as e:
            raise GhostAPError(f"can't read {self.path}: {e}")
        except tomllib.TOMLDecodeError as e:
            raise GhostAPError(f"bad syntax in {self.path}: {e}")
        return data, raw

    def _values(self, raw: dict) -> dict:
        out = {}
        for s in SETTINGS:
            body = raw.get(s.section)
            body = body if isinstance(body, dict) else {}
            out[f"{s.section}.{s.key}"] = body.get(s.key, _default_of(s.section, s.key))
        return out

    def get(self) -> dict:
        data, raw = self._read()
        schema = []
        for item, s in zip(settings_schema(), SETTINGS):
            item["editable"] = _is_editable(s)
            item["nullable"] = s.kind == "bool" and s.default is None
            schema.append(item)
        return {"version": hashlib.sha256(data).hexdigest(), "schema": schema,
                "values": self._values(raw)}

    def save(self, changes: dict, version: str) -> dict:
        with self._lock:
            data, raw = self._read()
            if hashlib.sha256(data).hexdigest() != version:
                raise SettingsRejected(
                    "The config file changed since this page loaded. Reload and try again.",
                    conflict=True)
            current = self._values(raw)
            by_name = {f"{s.section}.{s.key}": s for s in SETTINGS}
            edits, errors = {}, {}
            for name, value in changes.items():
                s = by_name.get(name)
                if s is None or not _is_editable(s):
                    errors[name] = "this setting can't be changed here"
                elif value is None:
                    if s.required:
                        errors[name] = "this one is needed"
                    elif s.key in (raw.get(s.section) or {}):
                        edits[(s.section, s.key)] = None
                else:
                    try:
                        coerced = _coerce(s, value)
                    except (GhostAPError, ValueError) as e:
                        errors[name] = str(e)
                        continue
                    if s.kind == "ipif":
                        coerced = str(coerced)
                    elif isinstance(coerced, tuple):
                        coerced = list(coerced)
                    if coerced == [] and s.kind == "list":
                        if s.key in (raw.get(s.section) or {}):
                            edits[(s.section, s.key)] = None     # none left: drop the line
                    elif coerced != current[name]:
                        edits[(s.section, s.key)] = coerced
            if errors:
                raise SettingsRejected("Some settings need fixing.", errors)
            if not edits:
                return {"saved": [], "version": version}
            try:
                new_text = apply_edits(data.decode(), edits)
            except GhostAPError as e:
                raise SettingsRejected(str(e))
            tmp = self.path.with_name(f".{self.path.name}.new")
            try:
                tmp.write_text(new_text)
                os.chmod(tmp, stat.S_IMODE(self.path.stat().st_mode))
                try:
                    load_config(tmp, secrets=False)
                except GhostAPError as e:
                    msg = str(e)
                    word = re.match(r"\w+", msg)
                    hit = [f"{a}.{b}" for a, b in edits if word and b == word.group()]
                    raise SettingsRejected(msg, {hit[0]: msg} if hit else {"": msg})
                shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
                os.replace(tmp, self.path)
            except OSError as e:
                raise SettingsRejected(f"couldn't write {self.path}: {e}")
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
            log.info("admin page changed settings: %s",
                     ", ".join(f"{a}.{b}" for a, b in edits))
            return {"saved": [f"{a}.{b}" for a, b in edits],
                    "version": hashlib.sha256(new_text.encode()).hexdigest()}


# --------------------------------------------------------------------------
# The ONLY place we touch the system: commands, clock, processes.
# Dry-run swaps them for log lines, and tests swap in a fake Shell.
# --------------------------------------------------------------------------

class Shell:
    def __init__(self, dry_run: bool = False, quiet: bool = False):
        self.dry_run = dry_run
        self.quiet = quiet          # the admin page polls often; don't log every command

    def run(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        if not self.quiet:
            log.info("$ %s", " ".join(cmd))
        if self.dry_run:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            if check:
                raise GhostAPError(f"{cmd[0]}: {e}")
            return subprocess.CompletedProcess(cmd, 1, "", str(e))
        if check and r.returncode != 0:
            detail = (r.stderr or r.stdout).strip()
            raise GhostAPError(f"{cmd[0]} failed ({r.returncode}): {detail}")
        return r

    def capture(self, *cmd: str) -> str:
        """Run a read-only command for a diagnostic dump. Never raises, never logs the
        command line, and returns whatever it printed (or why it couldn't run)."""
        if self.dry_run:
            return ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            return f"(could not run: {e})"
        return (r.stdout + r.stderr).rstrip()

    def probe(self, *cmd: str) -> tuple[int, str]:
        """Silent read-only look at the machine: (exit code, output). Never raises."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return 127, ""
        return r.returncode, (r.stdout + r.stderr).strip()

    def read_text(self, path: str):
        try:
            return Path(path).read_text(errors="replace")
        except OSError:
            return None

    def which(self, name: str):
        return shutil.which(name)

    def sleep(self, seconds: float) -> None:
        if not self.dry_run:
            time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    def wallclock(self) -> float:
        return time.time()

    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def kill(self, pid: int) -> None:
        log.info("kill %s", pid)
        if self.dry_run:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def mark(self, what: str) -> None:
        """Hook after a file change. Does nothing here; tests use it to inject crashes."""

    def environment_problems(self, cfg: Config) -> list[str]:
        problems = [f"missing tool: {t}" for t in REQUIRED_TOOLS if not shutil.which(t)]
        if not Path(f"/sys/class/net/{cfg.interface}").exists():
            problems.append(f"no interface named {cfg.interface}")
        if os.geteuid() != 0:
            problems.append("must run as root (use sudo)")
        return problems


def preflight(cfg: Config, sh: Shell) -> None:
    problems = sh.environment_problems(cfg)
    if not problems:
        return
    if sh.dry_run:
        for p in problems:
            log.warning("(dry run) would fail: %s", p)
        return
    raise GhostAPError("; ".join(problems))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


def _daemon_alive(sh: Shell, pid_file: Path) -> bool:
    pid = _read_pid(pid_file)
    return pid is not None and sh.pid_alive(pid)


def _ap_running(sh: Shell) -> bool:
    return _daemon_alive(sh, HOSTAPD_PID)


def _daemons_present(sh: Shell) -> bool:
    """Is any hostapd/dnsmasq of ours alive? Pid files can be missing or stale,
    so also look for processes started from our own config files."""
    if _daemon_alive(sh, HOSTAPD_PID) or _daemon_alive(sh, DNSMASQ_PID):
        return True
    if sh.dry_run:
        return False
    for conf in (HOSTAPD_CONF, DNSMASQ_CONF):
        if sh.run("pgrep", "-f", "--", str(conf), check=False).returncode == 0:
            return True
    return False


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def write_state(sh: Shell, state: dict) -> None:
    """Written BEFORE the radio is touched, replaced atomically."""
    if sh.dry_run:
        log.info("(dry run) would record state: %s", state)
        return
    RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_FILE)
    sh.mark("state:write")


def clear_state(sh: Shell) -> None:
    if sh.dry_run:
        return
    STATE_FILE.unlink(missing_ok=True)
    sh.mark("state:clear")


def wifi_connected(sh: Shell, iface: str) -> bool:
    r = sh.run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", check=False)
    prefix = f"{iface}:wifi:connected"
    return any(line.startswith(prefix) for line in r.stdout.splitlines())


def friendly_connection(name: str | None, iface: str) -> str | None:
    """The name to show a person. Netplan-made profiles are called
    'netplan-wlan0-MyNetwork'; the network is just 'MyNetwork'. Display only:
    the real name is still what we hand to nmcli."""
    prefix = f"netplan-{iface}-"
    if name and name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix):]
    return name


def current_connection(sh: Shell, iface: str) -> str | None:
    """Name of the WiFi profile the interface is on right now (so we can go back to it)."""
    r = sh.run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface, check=False)
    name = r.stdout.strip()
    return name if name and name != "--" else None


PING_COUNT = 2
PING_WAIT = 1            # seconds to wait for each reply
LINK_CACHE_SECONDS = 15  # the admin page polls; don't ping the phone every 3 s


def default_gateway(sh: Shell, iface: str) -> str | None:
    """The address of the router (here, the phone) this interface sends traffic to."""
    rc, out = sh.probe("ip", "-4", "route", "show", "default", "dev", iface)
    if rc != 0:
        return None
    for line in out.splitlines():
        m = re.match(r"default via (\d+\.\d+\.\d+\.\d+)\b", line.strip())
        if m:
            return m.group(1)
    return None


def link_health(sh: Shell, iface: str) -> dict:
    """Is the network we joined actually answering? NetworkManager says "connected"
    once it has an address; this asks the router. Only the router is pinged, never
    the internet (this project works offline).

    reachable: True (answered), False (no answer), None (couldn't check: no ping
    program, or it isn't allowed to run here)."""
    gw = default_gateway(sh, iface)
    if gw is None:
        return {"gateway": None, "reachable": False}
    rc, _ = sh.probe("ping", "-n", "-c", str(PING_COUNT), "-W", str(PING_WAIT),
                     "-I", iface, gw)
    # iputils ping: 0 = got a reply, 1 = no reply, anything else = it couldn't run.
    return {"gateway": gw, "reachable": True if rc == 0 else False if rc == 1 else None}


_link_cache: dict = {}


def cached_link_health(sh: Shell, iface: str) -> dict:
    now = sh.monotonic()
    hit = _link_cache.get(iface)
    if hit and now - hit[0] < LINK_CACHE_SECONDS:
        return hit[1]
    value = link_health(sh, iface)
    _link_cache[iface] = (now, value)
    return value


def _describe_link(link: dict) -> str:
    if link["gateway"] is None:
        return "joined, but no router address (gateway) was handed out"
    if link["reachable"] is None:
        return f"router {link['gateway']} (could not check whether it answers)"
    if link["reachable"]:
        return f"router {link['gateway']} is answering"
    return f"router {link['gateway']} is NOT answering"


NUDGE_EVERY = 10      # seconds between "look for the network and join it" attempts
SCAN_SETTLE = 4       # a rescan on the Pi Zero W takes a few seconds


def wifi_state(sh: Shell, iface: str) -> str:
    """NetworkManager's state word for the interface: connected, connecting,
    disconnected, unavailable, unmanaged ... or "" if it isn't listed (yet)."""
    r = sh.run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", check=False)
    for line in r.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == iface and parts[1] == "wifi":
            return parts[2]
    return ""


def _look_and_join(sh: Shell, cfg: Config) -> None:
    """Don't wait for NetworkManager's own autoconnect, which on a Pi Zero W can
    take a minute or more to get going. Rescan, then ask it to join a known
    network that is in range (rolling back to a network takes ~4 s this way)."""
    sh.run("nmcli", "device", "wifi", "rescan", "ifname", cfg.interface, check=False)
    sh.sleep(SCAN_SETTLE)
    r = sh.run("nmcli", "-w", "0", "device", "connect", cfg.interface, check=False)
    if r.returncode == 0:
        log.info("asked NetworkManager to join a known WiFi")
    else:
        log.info("no known WiFi to join yet (%s)", (r.stderr or "").strip() or r.returncode)


def _log_visible_networks(sh: Shell, cfg: Config, timeout: int) -> None:
    """Say WHY we are falling back: what the radio could actually see."""
    r = sh.run("nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list",
               "ifname", cfg.interface, check=False)
    seen = []
    for line in r.stdout.splitlines():
        ssid, _, sig = line.rpartition(":")
        ssid = ssid.replace("\\:", ":")
        if ssid:
            seen.append(f"{ssid} ({sig})")
    shown = ", ".join(list(dict.fromkeys(seen))[:12]) or "nothing"
    log.warning("no WiFi connection after %ss. NetworkManager state: %s. In range: %s",
                timeout, wifi_state(sh, cfg.interface) or "unknown", shown)


def wait_for_wifi(sh: Shell, cfg: Config, timeout: int | None = None) -> bool:
    timeout = cfg.boot_timeout if timeout is None else timeout
    if sh.dry_run:
        log.info("(dry run) not waiting for a WiFi connection; assuming none")
        return False
    deadline = sh.monotonic() + timeout
    last_nudge: float | None = None
    while True:
        state = wifi_state(sh, cfg.interface)
        if state.startswith("connected"):
            return True
        now = sh.monotonic()
        if now >= deadline:
            _log_visible_networks(sh, cfg, timeout)
            return False
        # Only nudge a device that is idle and ready. Never interrupt one that is
        # already connecting, and don't poke one that isn't up yet.
        if state.startswith("disconnected") and (
                last_nudge is None or now - last_nudge >= NUDGE_EVERY):
            last_nudge = now
            _look_and_join(sh, cfg)
        sh.sleep(1)


# --------------------------------------------------------------------------
# Config file generation
# --------------------------------------------------------------------------

def _flag(v: bool) -> str:
    return "1" if v else "0"


def render_hostapd(cfg: Config) -> str:
    lines = [
        f"interface={cfg.interface}",
        "driver=nl80211",
        "ctrl_interface=/run/hostapd",
        f"ssid={cfg.ssid}",
        f"country_code={cfg.country}",
        f"hw_mode={cfg.hw_mode}",
        f"channel={cfg.channel}",
        f"wpa={cfg.wpa}",
        f"wpa_passphrase={_real_passphrase(cfg)}",
        f"wpa_key_mgmt={cfg.wpa_key_mgmt}",
        f"rsn_pairwise={cfg.rsn_pairwise}",
        f"ieee80211w={cfg.ieee80211w}",
        f"auth_algs={cfg.auth_algs}",
    ]
    # Optional lines are written only when the setting is set, so the default file
    # is the same as it always was.
    if cfg.ieee80211n is not None:
        lines.append(f"ieee80211n={_flag(cfg.ieee80211n)}")
    if cfg.wmm_enabled is not None:
        lines.append(f"wmm_enabled={_flag(cfg.wmm_enabled)}")
    if cfg.ht_capab:
        lines.append(f"ht_capab={cfg.ht_capab}")
    if cfg.ignore_broadcast_ssid is not None:
        lines.append(f"ignore_broadcast_ssid={_flag(cfg.ignore_broadcast_ssid)}")
    if cfg.max_num_sta:
        lines.append(f"max_num_sta={cfg.max_num_sta}")
    if cfg.beacon_int:
        lines.append(f"beacon_int={cfg.beacon_int}")
    if cfg.hostapd_debug:
        lines += ["# diagnostics: read with  journalctl -t hostapd",
                  "logger_syslog=-1", "logger_syslog_level=0"]
    if cfg.extra_hostapd:
        lines += ["# extra_hostapd (your own lines)", *cfg.extra_hostapd]
    return "\n".join([*lines, ""])


def render_dnsmasq(cfg: Config) -> str:
    # dns_port = 0 turns off dnsmasq's DNS side: we only need DHCP, and this avoids
    # fighting anything else for port 53.
    lines = [
        f"interface={cfg.interface}",
        "bind-interfaces",
        f"port={cfg.dns_port}",
        f"dhcp-range={cfg.dhcp_start},{cfg.dhcp_end},{cfg.lease_time}",
        f"dhcp-leasefile={RUN_DIR / 'dnsmasq.leases'}",
        f"pid-file={DNSMASQ_PID}",
    ]
    if cfg.router == "none":
        lines.append("dhcp-option=3")
    elif cfg.router:
        lines.append(f"dhcp-option=3,{cfg.router}")
    if cfg.dns_servers:
        lines.append("dhcp-option=6," + ",".join(cfg.dns_servers))
    if cfg.extra_dnsmasq:
        lines += ["# extra_dnsmasq (your own lines)", *cfg.extra_dnsmasq]
    return "\n".join([*lines, ""])


# --------------------------------------------------------------------------
# Rescue timer: a one-shot systemd timer that runs `ghostap recover`.
# It lives in systemd, so it doesn't care what happens to this process.
# --------------------------------------------------------------------------

def disarm_rescue(sh: Shell) -> None:
    # Stops the timer only. Never the .service: when recover itself is the
    # caller, stopping its own service would kill it mid-run.
    sh.run("systemctl", "stop", f"{RESCUE_UNIT}.timer", check=False)


def recover_command(cfg: Config, *, expect: str, original: str | None = None) -> list[str]:
    """What the rescue timer runs. It carries what the timer was armed FOR (and which
    WiFi to go back to), so a lost state file can't turn an uncommitted live test
    into a permanent AP, or lose track of the original network."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "recover",
           "--config", str(cfg.path), f"--expect={expect}"]
    if original:
        cmd.append(f"--original={original}")
    return cmd


def arm_rescue(sh: Shell, seconds: int, command: list[str]) -> None:
    disarm_rescue(sh)  # a leftover timer would make the unit name collide
    sh.run("systemctl", "reset-failed", f"{RESCUE_UNIT}.service",
           f"{RESCUE_UNIT}.timer", check=False)
    # --collect: unload the units afterwards even if `recover` failed, so a failed
    # run can never leave the name taken for the next arming.
    sh.run("systemd-run", f"--unit={RESCUE_UNIT}", f"--on-active={seconds}s",
           "--timer-property=AccuracySec=1s", "--collect",
           "--description=GhostAP rescue", "--", *command)


def disarm_failover(sh: Shell) -> None:
    # Stop only the TIMER. If the failover run itself is already going, leave it be.
    sh.run("systemctl", "stop", f"{FAILOVER_UNIT}.timer", check=False)


def arm_failover(sh: Shell, cfg: Config) -> None:
    """Start the countdown to failover. Re-arming restarts it, so the grace period
    always counts from the LAST time WiFi dropped."""
    disarm_failover(sh)
    sh.run("systemctl", "reset-failed", f"{FAILOVER_UNIT}.service",
           f"{FAILOVER_UNIT}.timer", check=False)
    sh.run("systemd-run", f"--unit={FAILOVER_UNIT}", f"--on-active={cfg.failover_grace}s",
           "--timer-property=AccuracySec=1s", "--collect",
           # hostapd and dnsmasq daemonize but stay in this service's cgroup. Without
           # KillMode=process, systemd kills them the moment this job exits.
           "--property=KillMode=process",
           "--description=GhostAP failover", "--",
           sys.executable, str(Path(__file__).resolve()), "failover", "--config", str(cfg.path))


# --------------------------------------------------------------------------
# AP lifecycle
# --------------------------------------------------------------------------

def ap_healthy(sh: Shell, cfg: Config) -> bool:
    """An IP on wlan0 proves nothing. Ask hostapd if it's really serving."""
    r = sh.run("hostapd_cli", "-i", cfg.interface, "status", check=False)
    if "state=ENABLED" not in r.stdout:
        return False
    if not _daemon_alive(sh, DNSMASQ_PID):
        return False
    r = sh.run("ip", "-4", "-o", "addr", "show", "dev", cfg.interface, check=False)
    return f"{cfg.address.ip}/" in r.stdout


def verify_ap(sh: Shell, cfg: Config, timeout: int = 15) -> None:
    if sh.dry_run:
        log.info("(dry run) would confirm hostapd reports state=ENABLED and dnsmasq is running")
        return
    deadline = sh.monotonic() + timeout
    while sh.monotonic() < deadline:
        if ap_healthy(sh, cfg):
            log.info("AP is up: SSID '%s', gateway %s", cfg.ssid, cfg.address.ip)
            return
        sh.sleep(1)
    raise GhostAPError(
        "AP didn't come up (hostapd not ENABLED, or dnsmasq not running). "
        "See: journalctl -b | grep -E 'hostapd|dnsmasq'"
    )


STOP_PATIENCE = 10.0     # seconds to let a signalled daemon finish shutting down


def _wait_gone(sh: Shell, pids: list[int], patience: float) -> bool:
    """Wait for already-signalled daemons to exit. Never signals them again."""
    deadline = sh.monotonic() + patience
    while any(sh.pid_alive(p) for p in pids):
        if sh.monotonic() >= deadline:
            return False
        sh.sleep(0.2)
    return True


def _kill_daemons(sh: Shell) -> bool:
    """Stop our hostapd/dnsmasq: ONE SIGTERM each, then wait.

    A second signal while hostapd is still tearing the AP down interrupts the
    driver's wait for the WiFi firmware (-ERESTARTSYS, "-512" in dmesg). On the
    Pi Zero W's brcmfmac that leaves the firmware out of step with the driver
    and the link stays flaky until reboot. Seen on hardware: the pid-file kill
    was followed 140 ms later by a pkill sweep. So: signal once, wait for exit,
    and only sweep up processes we have not already signalled.
    """
    signalled: list[int] = []
    for pid_file in (DNSMASQ_PID, HOSTAPD_PID):
        pid = _read_pid(pid_file)
        if pid is not None and sh.pid_alive(pid):
            log.info("stopping pid %s (%s)", pid, pid_file.name)
            sh.kill(pid)
            signalled.append(pid)
        if not sh.dry_run:
            pid_file.unlink(missing_ok=True)
    if signalled and not sh.dry_run:
        if not _wait_gone(sh, signalled, STOP_PATIENCE):
            stuck = [p for p in signalled if sh.pid_alive(p)]
            log.warning("pid(s) %s still shutting down after %.0fs; not signalling again",
                        stuck, STOP_PATIENCE)
    killed = bool(signalled)
    # A daemon can outlive its pid file, or never get one (the controller can die
    # between `hostapd -B` and the pid file appearing). Ours are the ones started
    # from our own config files. Find them, skip anything already signalled.
    orphans: list[int] = []
    for conf in (DNSMASQ_CONF, HOSTAPD_CONF):
        r = sh.run("pgrep", "-f", "--", str(conf), check=False)
        if r.returncode == 0 and not sh.dry_run:
            for tok in r.stdout.split():
                if tok.isdigit() and int(tok) not in signalled and int(tok) != os.getpid():
                    orphans.append(int(tok))
    for pid in dict.fromkeys(orphans):
        log.info("stopping orphan pid %s", pid)
        sh.kill(pid)
        killed = True
    if orphans and not sh.dry_run:
        _wait_gone(sh, orphans, STOP_PATIENCE)
    if killed:
        sh.sleep(1)       # let the driver finish its own teardown
    return killed


def _restore_client_mode(sh: Shell, cfg: Config, original: str | None) -> None:
    iface = cfg.interface
    sh.run("ip", "addr", "flush", "dev", iface, check=False)
    sh.run("nmcli", "device", "set", iface, "managed", "yes", check=False)
    if original:
        r = sh.run("nmcli", "-w", "0", "connection", "up", "id", original, check=False)
        if r.returncode == 0:
            return
        log.warning("couldn't rejoin '%s'; letting NetworkManager choose", original)
    sh.run("nmcli", "-w", "0", "device", "connect", iface, check=False)


@contextlib.contextmanager
def _signals_blocked():
    """Hold SIGTERM/SIGINT/SIGHUP back so a second Ctrl-C can't cut cleanup in half.
    A signal that arrives meanwhile is delivered when the block ends."""
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, HANDLED_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def wait_for_reconnect(sh: Shell, cfg: Config) -> bool | None:
    """Handing the radio back to NetworkManager is not the same as being connected.
    Returns True once WiFi is up, False if it never came up, None if not checked."""
    if sh.dry_run:
        log.info("(dry run) would wait up to %ss for WiFi to reconnect", cfg.reconnect_timeout)
        return None
    deadline = sh.monotonic() + cfg.reconnect_timeout
    while True:
        if wifi_connected(sh, cfg.interface):
            return True
        if sh.monotonic() >= deadline:
            return False
        sh.sleep(1)


def _unwind(sh: Shell, cfg: Config, *, wait: bool | None = None,
            original_hint: str | None = None) -> bool | None:
    """Back to normal client WiFi. Safe to run from any half-switched state and
    safe to run twice. The cleanup itself is signal-proof; the wait for WiFi to
    come back is not, so Ctrl-C still works while waiting.

    Returns True (reconnected), False (radio is NetworkManager's again but no WiFi
    connection), or None (not waited: there was no network to go back to)."""
    with _signals_blocked():
        state = read_state() or {}
        original = state.get("original_connection") or original_hint
        _kill_daemons(sh)
        _restore_client_mode(sh, cfg, original)
        disarm_rescue(sh)
        clear_state(sh)
    if wait is None:
        wait = original is not None
    return wait_for_reconnect(sh, cfg) if wait else None


def _abort_transition(sh: Shell, cfg: Config) -> bool | None:
    """Undo only what a failed or interrupted switch actually did. If it died before
    the radio was touched, flushing addresses and reconnecting would itself disturb a
    working connection, so just take the timer down."""
    with _signals_blocked():
        untouched = read_state() is None and not _daemons_present(sh)
        if untouched:
            disarm_rescue(sh)
    if untouched:
        log.info("nothing was changed yet; only disarmed the rescue timer")
        return None
    result = _unwind(sh, cfg)
    _report_restore(result, "rolled back", cfg)
    return result


def _report_restore(reconnected: bool | None, what: str, cfg: Config) -> int:
    if reconnected is True:
        log.info("%s; NetworkManager has the radio back and WiFi is connected", what)
        return 0
    if reconnected is False:
        log.warning("%s; NetworkManager has the radio back, but WiFi did NOT reconnect "
                    "within %ss. You are not locked out: turn your hotspot/router on, or "
                    "run: nmcli device connect %s", what, cfg.reconnect_timeout, cfg.interface)
        return EXIT_NOT_RECONNECTED
    log.info("%s; NetworkManager has the radio back (not waiting for a WiFi connection)", what)
    return 0


def start_ap(sh: Shell, cfg: Config) -> None:
    log.info("starting fallback AP '%s' on %s", cfg.ssid, cfg.interface)
    hostapd_conf = render_hostapd(cfg)
    if sh.dry_run:
        shown = hostapd_conf.replace(cfg.passphrase, "********")
        log.info("(dry run) hostapd.conf would be:\n%s", shown)
        log.info("(dry run) dnsmasq.conf would be:\n%s", render_dnsmasq(cfg))
    else:
        RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write_private(HOSTAPD_CONF, hostapd_conf)
        DNSMASQ_CONF.write_text(render_dnsmasq(cfg))

    iface = cfg.interface
    sh.run("nmcli", "device", "set", iface, "managed", "no")
    sh.run("ip", "addr", "flush", "dev", iface)
    sh.run("ip", "addr", "add", str(cfg.address), "dev", iface)
    sh.run("ip", "link", "set", iface, "up")
    sh.run("hostapd", "-B", "-P", str(HOSTAPD_PID), str(HOSTAPD_CONF))
    sh.run("dnsmasq", f"--conf-file={DNSMASQ_CONF}")
    verify_ap(sh, cfg)


# --------------------------------------------------------------------------
# Radio snapshots: evidence for "why can't my phone see the AP?"
# --------------------------------------------------------------------------

def _snapshot_commands(iface: str) -> tuple[tuple[str, ...], ...]:
    return (
        ("iw", "dev"),
        ("iw", "dev", iface, "station", "dump"),
        ("iw", "dev", iface, "get", "power_save"),
        ("iw", "reg", "get"),
        ("hostapd_cli", "-i", iface, "status"),
        ("pgrep", "-a", "hostapd"),
        ("pgrep", "-a", "wpa_supplicant"),
        ("pgrep", "-a", "dnsmasq"),
        ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"),
        ("ip", "-br", "addr", "show", "dev", iface),
        ("rfkill", "list"),
        ("journalctl", "-k", "-n", "40", "--no-pager"),
        ("journalctl", "-t", "hostapd", "-n", "30", "--no-pager"),
    )


def take_snapshot(sh: Shell, cfg: Config, label: str) -> None:
    """Append what the radio looks like right now to SNAPSHOT_FILE. Best effort: a
    diagnostic must never be the reason an AP fails to come up."""
    try:
        state = read_state() or {}
        try:
            uptime = float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError):
            uptime = -1.0
        parts = [f"===== {time.strftime('%Y-%m-%d %H:%M:%S')}  [{label}]  "
                 f"purpose={state.get('purpose', 'none')}  uptime={uptime:.0f}s ====="]
        for cmd in _snapshot_commands(cfg.interface):
            parts.append(f"--- {' '.join(cmd)}")
            parts.append(sh.capture(*cmd) or "(no output)")
        text = "\n".join(parts) + "\n\n"
        if sh.dry_run:
            return
        SNAPSHOT_FILE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try:
            if SNAPSHOT_FILE.stat().st_size > SNAPSHOT_MAX_BYTES:
                keep = SNAPSHOT_FILE.read_bytes()[-SNAPSHOT_MAX_BYTES // 2:]
                SNAPSHOT_FILE.write_bytes(keep)
        except FileNotFoundError:
            pass
        with SNAPSHOT_FILE.open("a") as f:
            f.write(text)
        log.info("radio snapshot '%s' saved to %s", label, SNAPSHOT_FILE)
    except Exception as e:      # noqa: BLE001 - diagnostics are strictly best effort
        log.warning("couldn't save a radio snapshot: %s", e)


def arm_snapshot(sh: Shell, cfg: Config) -> None:
    """One follow-up snapshot a little later: does the radio go bad AFTER it looks fine?"""
    try:
        sh.run("systemctl", "stop", f"{SNAPSHOT_UNIT}.timer", check=False)
        sh.run("systemd-run", f"--unit={SNAPSHOT_UNIT}", f"--on-active={cfg.snapshot_delay}s",
               "--timer-property=AccuracySec=1s", "--collect",
               "--description=GhostAP follow-up snapshot", "--",
               sys.executable, str(Path(__file__).resolve()), "snapshot",
               f"+{cfg.snapshot_delay}s", "--config", str(cfg.path))
    except GhostAPError as e:
        log.warning("couldn't schedule the follow-up snapshot: %s", e)


def _begin_ap(sh: Shell, cfg: Config, *, purpose: str, rescue_seconds: int,
              require_rescue: bool) -> dict:
    """The one door into AP mode. Order matters:
         1. remember how we got here (read-only)
         2. arm the rescue timer
         3. record state
         4. only then touch the radio
    A crash anywhere after step 2 is cleaned up by the timer. A signal anywhere from
    step 2 on is cleaned up right away, by looking at what actually happened."""
    original = current_connection(sh, cfg.interface) if purpose == "test" else None
    deadline = None
    try:
        due = sh.wallclock() + rescue_seconds
        try:
            arm_rescue(sh, rescue_seconds,
                       recover_command(cfg, expect=purpose, original=original))
            deadline = due
        except Interrupted:
            raise
        except GhostAPError as e:
            if require_rescue:
                # A live test is usually run over SSH on this very radio. No safety net, no switch.
                disarm_rescue(sh)
                raise NotStarted(f"refusing to switch networking without a rescue timer: {e}")
            # At boot there's no WiFi to lose, so an AP without a safety net beats no AP.
            log.warning("couldn't arm the rescue timer (%s); continuing without it", e)

        state = {
            "purpose": purpose,
            "committed": purpose == "boot",
            "original_connection": original,
            "started": sh.wallclock(),
            "deadline": deadline,
        }
        write_state(sh, state)
        start_ap(sh, cfg)
        if cfg.snapshots:
            take_snapshot(sh, cfg, "AP up")
            arm_snapshot(sh, cfg)
        return state
    except NotStarted:
        raise
    except GhostAPError:
        log.error("AP failed to start or was interrupted; handing WiFi back to NetworkManager")
        _abort_transition(sh, cfg)
        raise


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_start(cfg: Config, sh: Shell) -> int:
    preflight(cfg, sh)
    if _ap_running(sh):
        log.info("AP already running; nothing to do")
        return 0
    if wait_for_wifi(sh, cfg):
        log.info("connected to WiFi; AP not needed")
        if not sh.dry_run:
            link = link_health(sh, cfg.interface)
            (log.info if link["reachable"] is not False else log.warning)(
                "link check: %s", _describe_link(link))
        return 0
    _begin_ap(sh, cfg, purpose="boot", rescue_seconds=cfg.rescue_timeout,
              require_rescue=False)
    disarm_rescue(sh)  # AP verified: the safety net has done its job
    return 0


def cmd_snapshot(cfg: Config, sh: Shell, label: str = "manual") -> int:
    """Save the radio's current state. Run it any time: `sudo ghostap snapshot`."""
    take_snapshot(sh, cfg, label)
    if not sh.dry_run:
        log.info("read it with: sudo tail -80 %s", SNAPSHOT_FILE)
    return 0


def cmd_event(cfg: Config, sh: Shell, iface: str, action: str) -> int:
    """Called by the NetworkManager hook when an interface goes up or down. It only
    starts or cancels a countdown, so nothing runs in the background in between."""
    if cfg.failover_grace <= 0 or iface != cfg.interface:
        return 0
    if action == "up":
        disarm_failover(sh)
    elif action == "down":
        if _daemons_present(sh) or read_state() is not None:
            # We took the radio ourselves (AP up, or a switch under way).
            log.info("event: %s down while an AP/transition is active; ignoring", iface)
            return 0
        log.info("event: %s went down; failover in %ss unless WiFi returns",
                 iface, cfg.failover_grace)
        arm_failover(sh, cfg)
    return 0


def cmd_failover(cfg: Config, sh: Shell) -> int:
    """The countdown ended. If WiFi is still gone, look once more, then raise the AP."""
    preflight(cfg, sh)
    if _daemons_present(sh) or read_state() is not None:
        log.info("failover: an AP or a switch is already in progress; nothing to do")
        return 0
    if wifi_connected(sh, cfg.interface):
        log.info("failover: WiFi is back; nothing to do")
        return 0
    log.warning("failover: WiFi has been down for %ss; one last look", cfg.failover_grace)
    if wait_for_wifi(sh, cfg, timeout=cfg.failover_join_timeout):
        log.info("failover: rejoined WiFi; AP not needed")
        return 0
    log.warning("failover: still no WiFi; raising the fallback AP")
    _begin_ap(sh, cfg, purpose="boot", rescue_seconds=cfg.rescue_timeout,
              require_rescue=False)
    disarm_rescue(sh)
    return 0


def cmd_stop(cfg: Config, sh: Shell) -> int:
    if not _daemons_present(sh) and read_state() is None:
        log.info("no AP running; nothing to stop")
        return 0
    return _report_restore(_unwind(sh, cfg, wait=True), "AP stopped", cfg)


def _seconds_left(state: dict | None, sh: Shell) -> int | None:
    deadline = (state or {}).get("deadline")
    return None if deadline is None else max(0, int(deadline - sh.wallclock()))


def collect_status(cfg: Config, sh: Shell, link=link_health) -> dict:
    """What `status` shows, as data (the admin page reads this too)."""
    state = read_state()
    if _ap_running(sh):
        mode = "ap"
    elif wifi_connected(sh, cfg.interface):
        mode = "client"
    else:
        mode = "disconnected"
    live_test = None
    if state and state.get("purpose") == "test" and state.get("committed") is not True:
        live_test = {"seconds_left": _seconds_left(state, sh)}
    armed = sh.run("systemctl", "is-active", f"{RESCUE_UNIT}.timer", check=False)
    failover = {"enabled": cfg.failover_grace > 0, "grace": cfg.failover_grace,
                "countdown_running": False}
    if cfg.failover_grace > 0:
        fo = sh.run("systemctl", "is-active", f"{FAILOVER_UNIT}.timer", check=False)
        failover["countdown_running"] = fo.stdout.strip() == "active"
    return {"mode": mode, "ssid": cfg.ssid, "gateway": str(cfg.address.ip),
            "link": link(sh, cfg.interface) if mode == "client" and not sh.dry_run else None,
            "live_test": live_test, "rescue_armed": armed.stdout.strip() == "active",
            "failover": failover}


def collect_gui_status(cfg: Config, sh: Shell) -> dict:
    """collect_status plus the extra facts the admin page shows."""
    info = collect_status(cfg, sh, link=cached_link_health)
    addrs = []
    r = sh.run("ip", "-4", "-o", "addr", "show", "dev", cfg.interface, check=False)
    for line in r.stdout.splitlines():
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", line)
        if m:
            addrs.append(m.group(1))
    info.update({"hostname": socket.gethostname(), "interface": cfg.interface,
                 "addresses": addrs,
                 "wifi": friendly_connection(current_connection(sh, cfg.interface), cfg.interface)
                 if info["mode"] == "client" else None, "gui_port": cfg.gui_port, "live_timeout": cfg.live_timeout})
    helper = sh.run("systemctl", "is-active", APPLY_PATH_UNIT, check=False)
    applier = Applier()
    info["apply"] = {"available": helper.stdout.strip() == "active",
                     "pending": applier.pending(), "last": applier.last()}
    return info


def cmd_status(cfg: Config, sh: Shell) -> int:
    st = collect_status(cfg, sh)
    if st["mode"] == "ap":
        print(f"mode: AP  (SSID '{cfg.ssid}', gateway {cfg.address.ip})")
    elif st["mode"] == "client":
        print("mode: client (connected to WiFi)")
        if st.get("link"):
            print("link: " + _describe_link(st["link"]))
    else:
        print("mode: disconnected (no AP, no WiFi)")
    if st["live_test"] is not None:
        left = st["live_test"]["seconds_left"]
        when = f"rolls back in ~{left}s" if left is not None else "rolls back on its own"
        print(f"live test: UNCOMMITTED, {when}. "
              "Keep it: ghostap test --commit   Revert now: ghostap test --abort")
    print("rescue timer: " + ("armed" if st["rescue_armed"] else "not armed"))
    fo = st["failover"]
    if fo["enabled"]:
        print(f"failover: on ({fo['grace']}s grace), countdown "
              + ("RUNNING" if fo["countdown_running"] else "not running"))
    else:
        print("failover: off")
    if cfg.gui_enabled:
        print(f"admin page: port {cfg.gui_port} (http://{socket.gethostname()}.local:"
              f"{cfg.gui_port})")
    return 0


GUI_PASSWORD_FILE = Path("/etc/ghostap/gui-password")


def _gui_module():
    """gui.py sits next to this file. Loaded only when needed, so the controller
    (which runs the boot logic) never depends on the web code."""
    mod = sys.modules.get("ghostap_gui")
    if mod is None:
        spec = importlib.util.spec_from_file_location(
            "ghostap_gui", Path(__file__).resolve().parent / "gui.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    return mod


def cmd_gui(cfg: Config, sh: Shell) -> int:
    if not cfg.gui_enabled:
        log.error("the admin page is off. Turn it on with `enabled = true` under [gui] "
                  "in %s", cfg.path)
        return 1
    gui = _gui_module()
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    pw_file = Path(cred) / "gui-password" if cred else GUI_PASSWORD_FILE
    stored = gui.read_password_hash(pw_file)
    if stored is None:
        log.error("no admin password is set. Run: sudo ghostap gui-password")
        return 1
    quiet = Shell(quiet=True)
    try:
        server = gui.make_server(lambda: collect_gui_status(cfg, quiet), stored,
                                 cfg.gui_bind, cfg.gui_port,
                                 settings=SettingsStore(cfg.path), applier=Applier(),
                                 logs=LogReader())
    except OSError as e:
        raise GhostAPError(f"can't listen on {cfg.gui_bind}:{cfg.gui_port}: {e}")
    log.info("admin page on http://%s:%s", cfg.gui_bind, cfg.gui_port)
    try:
        server.serve_forever()
    except Interrupted:
        pass
    finally:
        server.server_close()
    return 0


def cmd_gui_password() -> int:
    import getpass
    gui = _gui_module()
    pw = getpass.getpass("New admin page password (8+ characters): ")
    if pw != getpass.getpass("Type it again: "):
        raise GhostAPError("those didn't match")
    try:
        gui.write_password_file(GUI_PASSWORD_FILE, pw)
    except (ValueError, OSError) as e:
        raise GhostAPError(f"couldn't save the password: {e}")
    print(f"Saved. Restart the page to use it: sudo systemctl restart ghostap-gui")
    return 0


def cmd_recover(cfg: Config, sh: Shell, expect: str | None = None,
                original: str | None = None) -> int:
    """Trust what's actually running, not what a dead process meant to do.

    An AP is kept only on POSITIVE evidence that it's meant to stay: a state file
    that says committed. A missing or unreadable state file never counts as that.
    The rescue timer also says what it was armed for (--expect), so if the state
    file vanishes while an uncommitted live test AP is up, we still revert."""
    state = read_state()
    committed = state is not None and state.get("committed") is True
    test_evidence = expect == "test" or (state or {}).get("purpose") == "test"
    uncommitted_test = test_evidence and not committed
    present = _daemons_present(sh)

    if present and not uncommitted_test and ap_healthy(sh, cfg):
        if state is None:
            log.warning("recover: state file is missing, so I can't tell why this AP is "
                        "up. It's healthy, so I'm leaving it. Revert with: ghostap stop")
        else:
            log.info("recover: the AP is healthy; keeping it")
        disarm_rescue(sh)
        return 0

    if not present and wifi_connected(sh, cfg.interface):
        log.info("recover: normal WiFi is up and no AP is running; nothing to undo")
        disarm_rescue(sh)
        clear_state(sh)
        return 0

    if uncommitted_test:
        why = "live test was never committed" if state else \
              "state file is gone and the timer was armed for a live test, so treating it as uncommitted"
        log.warning("recover: %s; restoring normal WiFi", why)
    else:
        log.warning("recover: AP is missing or unhealthy; restoring normal WiFi")
    return _report_restore(_unwind(sh, cfg, original_hint=original), "recovered", cfg)


def cmd_test_start(cfg: Config, sh: Shell) -> int:
    preflight(cfg, sh)
    if _daemons_present(sh) or read_state() is not None:
        raise GhostAPError("an AP or test is already active. Use 'ghostap test --commit', "
                           "'ghostap test --abort', or 'ghostap stop'.")
    log.warning("LIVE TEST: %s leaves normal WiFi now, so an SSH session over WiFi will drop. "
                "Output is also saved to %s", cfg.interface, LOG_FILE)
    state = _begin_ap(sh, cfg, purpose="test",
                      rescue_seconds=cfg.live_timeout + STARTUP_ALLOWANCE, require_rescue=True)
    # No disarm here: the rollback stays armed until someone commits.
    left = _seconds_left(state, sh)
    log.info("test AP is up. Join '%s' (gateway %s). It reverts by itself in ~%ss "
             "unless you run: ghostap test --commit",
             cfg.ssid, cfg.address.ip, left if left is not None else cfg.live_timeout)
    return 0


def cmd_test_commit(cfg: Config, sh: Shell) -> int:
    state = read_state()
    if state is None or state.get("purpose") != "test":
        raise GhostAPError("no live test is active (it may already have rolled back)")
    if state.get("committed"):
        log.info("already committed")
        return 0
    if not (_daemons_present(sh) and ap_healthy(sh, cfg)):
        result = _unwind(sh, cfg)
        note = " (WiFi has not reconnected yet)" if result is False else ""
        raise GhostAPError("the test AP isn't healthy, so I rolled back instead of "
                           "committing" + note)
    write_state(sh, {**state, "committed": True})
    disarm_rescue(sh)
    log.info("committed: the AP stays up until 'ghostap stop' or a reboot")
    return 0


def cmd_test_abort(cfg: Config, sh: Shell) -> int:
    state = read_state()
    if state is None or state.get("purpose") != "test":
        raise GhostAPError("no live test is active. (To stop a normal AP: ghostap stop)")
    return _report_restore(_unwind(sh, cfg, wait=True), "live test aborted", cfg)


# --------------------------------------------------------------------------
# Applying changes from the admin page.
#
# The page runs sandboxed and can't touch the network, on purpose. So it can't run
# `ghostap test` itself. Instead it drops a tiny request file; systemd (watching for
# that file) starts `ghostap apply-request` as root, which does ONE of three fixed
# things and writes down how it went. The page can ask for nothing else.
# --------------------------------------------------------------------------

APPLY_DIR = Path("/run/ghostap-gui")                  # the page's own writable folder
APPLY_REQUEST = APPLY_DIR / "request"
APPLY_RESULT = RUN_DIR / "apply-result"
APPLY_PATH_UNIT = "ghostap-apply.path"
APPLY_ACTIONS = ("test", "commit", "abort")


class ApplyRejected(GhostAPError):
    """A request from the page was turned away (and nothing was started)."""


class Applier:
    """The page's side: asks for an action and reads how the last one went."""
    Rejected = ApplyRejected

    def __init__(self, request: Path | None = None, result: Path | None = None):
        self.request_file = Path(request or APPLY_REQUEST)
        self.result_file = Path(result or APPLY_RESULT)
        self._lock = threading.Lock()

    def pending(self) -> bool:
        return self.request_file.exists()

    def last(self) -> dict | None:
        try:
            data = json.loads(self.result_file.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            return None
        return {k: data.get(k) for k in ("action", "ok", "message", "at")}

    def request(self, action) -> None:
        if action not in APPLY_ACTIONS:
            raise ApplyRejected("unknown action")
        with self._lock:
            if self.pending():
                raise ApplyRejected("Still working on the last request. Give it a moment.")
            tmp = self.request_file.with_name(self.request_file.name + ".new")
            try:
                tmp.write_text(json.dumps({"action": action}))
                os.replace(tmp, self.request_file)
            except OSError as e:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise ApplyRejected(f"couldn't send the request: {e}")


def _apply_message(action: str, cfg: Config) -> str:
    if action == "test":
        return (f"Trying the saved settings. Join '{cfg.ssid}', open "
                f"http://{cfg.address.ip}:{cfg.gui_port} and tap Keep within "
                f"{cfg.live_timeout} seconds, or it reverts by itself.")
    return "Kept. The test network stays up." if action == "commit" else "Reverted."


def _write_apply_result(action, ok: bool, message: str) -> None:
    RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write_private(APPLY_RESULT, json.dumps(
        {"action": action, "ok": ok, "message": message, "at": int(time.time())}))
    os.chmod(APPLY_RESULT, 0o644)


def cmd_apply_request(cfg: Config, sh: Shell) -> int:
    """Run by systemd when the page leaves a request. Reads it, deletes it, does it."""
    try:
        raw = APPLY_REQUEST.read_text()
    except FileNotFoundError:
        return 0
    except OSError as e:
        raise GhostAPError(f"can't read the request: {e}")
    APPLY_REQUEST.unlink(missing_ok=True)       # first, so a failure can't make it loop
    try:
        action = json.loads(raw).get("action")
    except (ValueError, AttributeError):
        action = None
    if action not in APPLY_ACTIONS:
        log.warning("ignoring a request that isn't one of %s", ", ".join(APPLY_ACTIONS))
        _write_apply_result(None, False, "That request wasn't understood.")
        return 0
    fn = {"test": cmd_test_start, "commit": cmd_test_commit, "abort": cmd_test_abort}[action]
    log.info("admin page asked for: %s", action)
    try:
        fn(cfg, sh)
    except GhostAPError as e:
        log.error("%s", e)
        _write_apply_result(action, False, str(e))
        return 0
    _write_apply_result(action, True, _apply_message(action, cfg))
    return 0


# --------------------------------------------------------------------------
# Logs for the admin page. Read-only, fixed sources, and a fixed set of commands: the page
# can pick WHICH source and HOW MANY lines, never what is run.
# --------------------------------------------------------------------------

LOG_UNITS = ("ghostap.service", "ghostap-gui.service", "ghostap-apply.service",
             f"{RESCUE_UNIT}.service", f"{FAILOVER_UNIT}.service")
LOG_DEFAULT_LINES, LOG_MAX_LINES, LOG_MAX_LINE_CHARS = 200, 1000, 400
_TAIL_BYTES = 512 * 1024
_SECRET_RE = re.compile(r"(?i)\b(wpa_passphrase|passphrase|psk|password)(\s*[=:]\s*)\S+")


def redact(line: str) -> str:
    """Logs may be pasted into a chat, so keep anything password-shaped out of them."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}********", line)


def _journal_argvs(lines: int, *, previous_boot: bool = False) -> list[list[str]]:
    """Two plain queries (our own units, then the daemons we start, which log under their
    own names), merged afterwards. One combined query would need journalctl's "+" (OR)
    syntax, which behaves differently from one systemd version to the next."""
    base = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(lines)]
    if previous_boot:
        base += ["-b", "-1"]
    units = list(base)
    for unit in LOG_UNITS:
        units += ["-u", unit]
    tags = list(base)
    for tag in ("hostapd", "dnsmasq", "ghostap"):
        tags += ["-t", tag]
    return [units, tags]


_STAMP_CHARS = 24           # "2026-11-17T14:03:01-0600": sorts correctly as plain text


class LogReader:
    """Which logs the page can show. `runner(argv)` returns (exit code, text); tests swap it."""
    SOURCES = {
        "activity": "Recent activity",
        "previous_boot": "Last boot",
        "test": "Last live test",
        "snapshots": "Radio snapshots",
    }

    def __init__(self, runner=None, test_log: Path | None = None,
                 snapshot_log: Path | None = None):
        self.runner = runner or self._run
        self.test_log = Path(test_log or LOG_FILE)
        self.snapshot_log = Path(snapshot_log or SNAPSHOT_FILE)

    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=15,
                               errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)
        return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)

    @staticmethod
    def _tail_file(path: Path) -> str:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            return f.read().decode("utf-8", "replace")

    def sources(self) -> list[dict]:
        return [{"id": k, "title": v} for k, v in self.SOURCES.items()]

    def read(self, source, lines=LOG_DEFAULT_LINES) -> dict:
        if source not in self.SOURCES:
            raise ValueError("unknown log")
        if isinstance(lines, bool) or not isinstance(lines, int):
            raise ValueError("lines must be a number")
        lines = max(10, min(LOG_MAX_LINES, lines))
        note = ""
        if source in ("activity", "previous_boot"):
            results = [self.runner(argv) for argv in _journal_argvs(
                lines, previous_boot=source == "previous_boot")]
            good = [text for rc, text in results if rc == 0]
            if not good and source == "previous_boot" and "boot entry" in results[0][1]:
                return {"source": source, "lines": [],
                        "note": "There's no earlier boot in the log. Logs are kept in memory "
                                "(and lost at reboot) unless persistent logs are turned on; "
                                "the installer can do that."}
            if not good:
                return {"source": source, "lines": [],
                        "note": "Couldn't read the system log: "
                                + redact(results[0][1].strip())[:200]}
            merged = [ln for text in good for ln in text.splitlines() if not ln.startswith("-- ")]
            merged.sort(key=lambda ln: ln[:_STAMP_CHARS])       # stable: ties keep their order
            text = "\n".join(merged)
        else:
            path = self.test_log if source == "test" else self.snapshot_log
            try:
                text = self._tail_file(path)
            except FileNotFoundError:
                what = ("No live test has run since the Pi last started."
                        if source == "test" else
                        "No snapshots yet. They're taken whenever the Pi starts its own network.")
                return {"source": source, "lines": [], "note": what}
            except OSError as e:
                return {"source": source, "lines": [], "note": f"Couldn't read it: {e.strerror}"}
        out = [redact(ln)[:LOG_MAX_LINE_CHARS] for ln in text.splitlines()
               if not ln.startswith("-- ")]
        if len(out) > lines:
            out = out[-lines:]
        if not out and not note:
            note = "Nothing logged yet."
        return {"source": source, "lines": out, "note": note}


# ---- preflight: "will this work on THIS machine?" ----------------------------------
# Two halves on purpose. gather_facts() looks at the real machine and does nothing else;
# judge() turns those facts into a verdict and touches nothing. Tests feed judge() whole
# made-up machines (a Pi on dhcpcd, a WiFi chip with no AP mode, ...), which is how we
# know it gives the right answer without owning every kind of Pi.

OK, INFO, WARN, BLOCK = "ok", "info", "warn", "block"


@dataclass
class Finding:
    level: str          # INFO, WARN or BLOCK
    title: str
    detail: str = ""
    fix: str = ""
    code: str = ""      # machine-readable tag, e.g. "missing:hostapd" (installer uses it)


@dataclass
class HostFacts:
    iface: str = "wlan0"
    model: str = ""
    os_name: str = ""
    has_systemd: bool = True
    tools: dict = field(default_factory=dict)       # name -> found?
    active: dict = field(default_factory=dict)      # service -> is-active text
    enabled: dict = field(default_factory=dict)     # service -> is-enabled text
    iface_exists: bool = True
    ap_mode: object = None          # True / False / None = couldn't tell
    reg_country: object = None      # "US", "00" (unset) or None = couldn't tell
    wifi_profiles: object = None    # saved client WiFi profiles, None = couldn't tell
    ap_profiles: tuple = ()         # NetworkManager profiles that are hotspots


_STACK_SERVICES = ("NetworkManager", "dhcpcd", "systemd-networkd", "wpa_supplicant")
_TOOLS = ("hostapd", "dnsmasq", "iw", "nmcli", "systemd-run", "ip")


def _ap_supported(iw_list: str):
    """True/False from `iw list`'s 'Supported interface modes' block; None if no such block."""
    lines = iw_list.splitlines()
    for i, line in enumerate(lines):
        if "Supported interface modes" in line:
            modes = []
            for nxt in lines[i + 1:]:
                if not nxt.strip().startswith("*"):
                    break
                modes.append(nxt.strip().lstrip("* ").strip())
            return "AP" in modes
    return None


def gather_facts(sh: Shell, iface: str = "wlan0") -> HostFacts:
    f = HostFacts(iface=iface)
    model = sh.read_text("/proc/device-tree/model")
    f.model = (model or "").replace("\0", "").strip()
    for line in (sh.read_text("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            f.os_name = line.split("=", 1)[1].strip().strip('"')
    f.has_systemd = sh.probe("test", "-d", "/run/systemd/system")[0] == 0
    f.tools = {t: sh.which(t) is not None for t in _TOOLS}
    for svc in (*_STACK_SERVICES, "hostapd", "dnsmasq"):
        f.active[svc] = sh.probe("systemctl", "is-active", svc)[1].split("\n")[0] or "unknown"
    for svc in ("hostapd", "dnsmasq"):
        f.enabled[svc] = sh.probe("systemctl", "is-enabled", svc)[1].split("\n")[0] or "unknown"
    f.iface_exists = sh.probe("ip", "link", "show", iface)[0] == 0
    rc, out = sh.probe("iw", "list")
    f.ap_mode = _ap_supported(out) if rc == 0 else None
    rc, out = sh.probe("iw", "reg", "get")
    if rc == 0:
        m = re.search(r"country (\w\w):", out)
        f.reg_country = m.group(1) if m else None
    if f.active.get("NetworkManager") == "active" and f.tools.get("nmcli"):
        rc, out = sh.probe("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
        if rc == 0:
            client, hot = 0, []
            for line in out.splitlines():
                name, _, kind = line.rpartition(":")
                if kind != "802-11-wireless":
                    continue
                name = name.replace("\\:", ":")
                mode = sh.probe("nmcli", "-g", "802-11-wireless.mode",
                                "connection", "show", name)[1]
                if mode == "ap":
                    hot.append(name)
                else:
                    client += 1
            f.wifi_profiles, f.ap_profiles = client, tuple(hot)
    return f


def judge(f: HostFacts) -> list:
    out: list = []
    def add(*a, **k):
        out.append(Finding(*a, **k))
    active = lambda svc: f.active.get(svc) == "active"

    if not f.has_systemd:
        add(BLOCK, "systemd isn't running",
            "GhostAP's safety net (the self-reverting rescue timer) is a systemd timer.")
    nm = active("NetworkManager")
    if not nm:
        others = [n for n in ("dhcpcd", "systemd-networkd") if active(n)]
        if others:
            add(BLOCK, f"This system manages WiFi with {' / '.join(others)}, not NetworkManager",
                "GhostAP can't drive that yet. Other network setups are planned.",
                "Switch to NetworkManager (Raspberry Pi OS Bookworm and newer use it by "
                "default), or wait for support.")
        else:
            add(BLOCK, "NetworkManager isn't running",
                "GhostAP uses it to join saved WiFi and to hand the radio over to the AP.",
                "sudo apt install network-manager && sudo systemctl enable --now NetworkManager")
    elif not f.tools.get("nmcli", True):
        add(BLOCK, "nmcli (NetworkManager's command-line tool) is missing", "",
            "sudo apt install network-manager")

    for tool, pkg in (("hostapd", "hostapd"), ("dnsmasq", "dnsmasq-base"), ("iw", "iw")):
        if not f.tools.get(tool, True):
            add(BLOCK, f"{tool} isn't installed", "", f"sudo apt install {pkg}",
                code=f"missing:{tool}")
    for tool in ("systemd-run", "ip"):
        if not f.tools.get(tool, True):
            add(BLOCK, f"{tool} isn't installed", "GhostAP needs it.")

    if not f.iface_exists:
        add(BLOCK, f"No WiFi interface named {f.iface}",
            "Either this machine has no WiFi chip, or it's named differently.",
            "Check `ip link`, then set `interface` under [network] in the config.")
    elif f.ap_mode is False:
        add(BLOCK, "This WiFi chip can't run as an access point",
            "Its driver doesn't list AP mode, so hostapd can't use it.",
            "Use a USB WiFi adapter that supports AP mode.")
    elif f.ap_mode is None:
        add(WARN, "Couldn't confirm the WiFi chip supports AP mode",
            "`iw list` gave no answer here. The Pi Zero W's chip is known to work.")

    if f.reg_country in (None, "00") and f.iface_exists:
        add(WARN, "WiFi country isn't set",
            "Without it, some channels are blocked and the radio may refuse to start an AP.",
            "sudo raspi-config  (Localisation > WLAN Country), or: sudo iw reg set US")

    for svc in ("hostapd", "dnsmasq"):
        if active(svc) or f.enabled.get(svc) == "enabled":
            add(WARN, f"The system {svc} service is running or enabled",
                f"GhostAP starts its own {svc}; a second one fights over the radio and "
                "the DHCP ports.", f"sudo systemctl disable --now {svc}")

    if f.ap_profiles:
        names = ", ".join(f.ap_profiles)
        add(WARN, f"A leftover NetworkManager hotspot profile exists: {names}",
            "NetworkManager may bring it up on its own and fight GhostAP's AP. This is what "
            "broke the first failover tests.",
            "sudo nmcli connection delete '<name>'   (if you don't use it)")
    if f.wifi_profiles == 0:
        add(INFO, "No saved WiFi networks",
            "There's nothing to join, so the AP will come up at every boot.")

    if "Raspberry Pi" not in f.model:
        add(INFO, f"This doesn't look like a Raspberry Pi ({f.model or 'board unknown'})",
            "GhostAP has only been tried on Raspberry Pis; most of it should still work.")
    elif "Zero" in f.model:
        add(INFO, "Pi Zero: 2.4 GHz only",
            "Keep hw_mode = \"g\" and a channel from 1-11 (the default).")
    return out


def verdict(findings: list) -> str:
    levels = {x.level for x in findings}
    return BLOCK if BLOCK in levels else WARN if WARN in levels else OK


_VERDICT_TEXT = {
    OK: "Looks compatible. Nothing in the way that we know of.",
    WARN: "Probably fine, but fix the warnings above first.",
    BLOCK: "Not supported on this machine as it is. See the problems above.",
}
_MARK = {BLOCK: "[X]", WARN: "[!]", INFO: "[i]"}


def render_preflight(f: HostFacts, findings: list) -> str:
    lines = [f"Checked: {f.model or 'unknown board'} / {f.os_name or 'unknown OS'} "
             f"(interface {f.iface})", ""]
    order = {BLOCK: 0, WARN: 1, INFO: 2}
    for x in sorted(findings, key=lambda x: order[x.level]):
        lines.append(f"{_MARK[x.level]} {x.title}")
        if x.detail:
            lines.append(f"      {x.detail}")
        if x.fix:
            lines.append(f"      Fix: {x.fix}")
    if findings:
        lines.append("")
    lines.append(_VERDICT_TEXT[verdict(findings)])
    lines.append("This is a best-effort look at your system, not a guarantee.")
    return "\n".join(lines)


def cmd_preflight(cfg: Config, sh: Shell) -> int:
    facts = gather_facts(sh, cfg.interface if cfg else "wlan0")
    findings = judge(facts)
    print(render_preflight(facts, findings))
    return 1 if verdict(findings) == BLOCK else 0


# Commands that only look, so they don't read the passphrase file (and don't need root to).
READ_ONLY = ("status", "preflight", "gui")

COMMANDS = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status,
            "recover": cmd_recover, "test": cmd_test_start,
            "failover": cmd_failover, "event": cmd_event,
            "snapshot": cmd_snapshot, "preflight": cmd_preflight, "gui": cmd_gui, "gui-password": None,
            "apply-request": cmd_apply_request,
            "schema": None, "example-config": None}


def _install_signal_handlers() -> None:
    def stop(signum, frame):
        raise Interrupted(f"interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # SIGHUP is what sshd sends when the SSH session dies, and a live test kills the
    # SSH session by design (the radio leaves WiFi). Treating it as "abort" would undo
    # a successful test at exactly the moment it works, so it does nothing. This is a
    # no-op Python handler rather than SIG_IGN on purpose: an ignored signal is
    # inherited across exec, and hostapd/dnsmasq use SIGHUP to reload.
    signal.signal(signal.SIGHUP, lambda signum, frame: None)


def _setup_file_log(dry_run: bool) -> None:
    """A live test drops the SSH session that would have shown the output, so also
    write it somewhere readable once you're back in through the AP."""
    if dry_run or os.geteuid() != 0:
        return
    try:
        RUN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_FILE)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ghostap",
        description="Join WiFi at boot, or fall back to your own access point.",
    )
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("event_args", nargs="*", metavar="IFACE ACTION",
                   help="(event) interface and up/down, as passed by NetworkManager; "
                        "(snapshot) an optional label")
    p.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen; change nothing")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--commit", action="store_true", help="(test) keep the test AP")
    group.add_argument("--abort", action="store_true", help="(test) revert the test now")
    p.add_argument("--expect", choices=("boot", "test"),
                   help="(recover) what the rescue timer was armed for")
    p.add_argument("--original", help="(recover) WiFi profile to rejoin if the state file is gone")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if (args.commit or args.abort) and args.command != "test":
        p.error("--commit and --abort only apply to the 'test' command")
    if args.event_args and args.command not in ("event", "snapshot"):
        p.error("extra arguments only apply to the 'event' and 'snapshot' commands")
    if args.command == "snapshot" and len(args.event_args) > 1:
        p.error("snapshot takes at most one label: ghostap snapshot [LABEL]")
    if args.command == "event" and len(args.event_args) != 2:
        p.error("event needs an interface and an action: ghostap event wlan0 down")
    if (args.expect or args.original) and args.command != "recover":
        p.error("--expect and --original only apply to the 'recover' command")

    if args.command in ("schema", "example-config"):
        if args.event_args:
            p.error(f"{args.command} takes no arguments")
        print(json.dumps(settings_schema(), indent=2) if args.command == "schema"
              else render_example_config(), end="" if args.command == "example-config" else "\n")
        return 0

    if args.command == "gui-password":
        try:
            return cmd_gui_password()
        except GhostAPError as e:
            print(f"ERROR {e}", file=sys.stderr)
            return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # commands that only look must not write into the log a live test is relying on
    _setup_file_log(args.dry_run or args.command in READ_ONLY)
    _install_signal_handlers()
    try:
        if args.command == "preflight" and not args.config.exists():
            cfg = None      # a fresh machine: nothing is installed yet, so check the defaults
        else:
            cfg = load_config(args.config, secrets=args.command not in READ_ONLY)
        sh = Shell(args.dry_run)
        if args.command == "recover":
            return cmd_recover(cfg, sh, expect=args.expect, original=args.original)
        if args.command == "event":
            return cmd_event(cfg, sh, *args.event_args)
        if args.command == "snapshot":
            return cmd_snapshot(cfg, sh, *(args.event_args or ["manual"]))
        if args.command == "test":
            fn = cmd_test_commit if args.commit else cmd_test_abort if args.abort else cmd_test_start
        else:
            fn = COMMANDS[args.command]
        return fn(cfg, sh)
    except GhostAPError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
