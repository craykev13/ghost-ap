#!/usr/bin/env python3
"""GhostAP controller (v0.2).

At boot: give NetworkManager `boot_timeout` seconds to join a known WiFi.
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
import ipaddress
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path("/etc/ghostap/ghostap.toml")
RUN_DIR = Path("/run/ghostap")
HOSTAPD_CONF = RUN_DIR / "hostapd.conf"
HOSTAPD_PID = RUN_DIR / "hostapd.pid"
DNSMASQ_CONF = RUN_DIR / "dnsmasq.conf"
DNSMASQ_PID = RUN_DIR / "dnsmasq.pid"
STATE_FILE = RUN_DIR / "state.json"
RESCUE_UNIT = "ghostap-rescue"
LOG_FILE = RUN_DIR / "ghostap.log"   # survives a dead SSH session; read it afterwards

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


def _read_secret(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        text = path.read_text().strip()
    except OSError as e:
        raise GhostAPError(f"can't read passphrase file {path}: {e}")
    if mode & 0o077:
        log.warning("%s is readable by other users. Run: chmod 600 %s", path, path)
    return text


def validate(cfg: Config) -> None:
    if not (8 <= len(cfg.passphrase) <= 63 and cfg.passphrase.isascii()):
        raise GhostAPError("passphrase must be 8-63 ASCII characters")
    if not (1 <= len(cfg.ssid.encode()) <= 32) or "\n" in cfg.ssid:
        raise GhostAPError("ssid must be 1-32 bytes with no newlines")
    if not 1 <= cfg.channel <= 11:
        raise GhostAPError("channel must be 1-11 (2.4 GHz)")
    if cfg.boot_timeout < 0:
        raise GhostAPError("boot_timeout can't be negative")
    # The rescue timer has to outlast a normal switch (hostapd check can take 15s).
    if cfg.rescue_timeout < 30:
        raise GhostAPError("rescue_timeout must be at least 30 seconds")
    if cfg.live_timeout < 30:
        raise GhostAPError("live_timeout must be at least 30 seconds")
    if cfg.reconnect_timeout < 5:
        raise GhostAPError("reconnect_timeout must be at least 5 seconds")
    net = cfg.address.network
    for ip in (cfg.dhcp_start, cfg.dhcp_end):
        if ipaddress.IPv4Address(ip) not in net:
            raise GhostAPError(f"DHCP address {ip} is outside {net}")
    if cfg.address.ip in (net.network_address, net.broadcast_address):
        raise GhostAPError("AP address can't be the network or broadcast address")


def load_config(path: Path) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise GhostAPError(f"config not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise GhostAPError(f"bad syntax in {path}: {e}")

    try:
        net, ap, dhcp = raw["network"], raw["access_point"], raw["dhcp"]
        cfg = Config(
            interface=net.get("interface", "wlan0"),
            boot_timeout=int(net.get("boot_timeout", 45)),
            ssid=ap["ssid"],
            passphrase=_read_secret(Path(ap["passphrase_file"])),
            country=ap.get("country", "US"),
            channel=int(ap.get("channel", 6)),
            address=ipaddress.IPv4Interface(ap.get("address", "192.168.4.1/24")),
            dhcp_start=dhcp["start"],
            dhcp_end=dhcp["end"],
            lease_time=dhcp.get("lease_time", "1h"),
            rescue_timeout=int(raw.get("recovery", {}).get("rescue_timeout", 90)),
            live_timeout=int(raw.get("testing", {}).get("live_timeout", 120)),
            path=Path(path).resolve(),
            reconnect_timeout=int(raw.get("recovery", {}).get("reconnect_timeout", 20)),
        )
        validate(cfg)
    except KeyError as e:
        raise GhostAPError(f"missing setting in config: {e}")
    except ValueError as e:
        raise GhostAPError(f"invalid value in config: {e}")
    return cfg


# --------------------------------------------------------------------------
# The ONLY place we touch the system: commands, clock, processes.
# Dry-run swaps them for log lines, and tests swap in a fake Shell.
# --------------------------------------------------------------------------

class Shell:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def run(self, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
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


def current_connection(sh: Shell, iface: str) -> str | None:
    """Name of the WiFi profile the interface is on right now (so we can go back to it)."""
    r = sh.run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface, check=False)
    name = r.stdout.strip()
    return name if name and name != "--" else None


def wait_for_wifi(sh: Shell, cfg: Config) -> bool:
    if sh.dry_run:
        log.info("(dry run) skipping the %ss wait and pretending there's no WiFi",
                 cfg.boot_timeout)
        return False
    deadline = sh.monotonic() + cfg.boot_timeout
    while True:
        if wifi_connected(sh, cfg.interface):
            return True
        if sh.monotonic() >= deadline:
            return False
        sh.sleep(1)


# --------------------------------------------------------------------------
# Config file generation
# --------------------------------------------------------------------------

def render_hostapd(cfg: Config) -> str:
    return "\n".join([
        f"interface={cfg.interface}",
        "driver=nl80211",
        "ctrl_interface=/run/hostapd",
        f"ssid={cfg.ssid}",
        f"country_code={cfg.country}",
        "hw_mode=g",
        f"channel={cfg.channel}",
        "wpa=2",
        f"wpa_passphrase={cfg.passphrase}",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        "ieee80211w=0",
        "auth_algs=1",
        "",
    ])


def render_dnsmasq(cfg: Config) -> str:
    # port=0 turns off dnsmasq's DNS side: we only need DHCP, and this avoids
    # fighting anything else for port 53.
    return "\n".join([
        f"interface={cfg.interface}",
        "bind-interfaces",
        "port=0",
        f"dhcp-range={cfg.dhcp_start},{cfg.dhcp_end},{cfg.lease_time}",
        f"dhcp-leasefile={RUN_DIR / 'dnsmasq.leases'}",
        f"pid-file={DNSMASQ_PID}",
        "",
    ])


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
        return 0
    log.warning("no WiFi connection after %ss", cfg.boot_timeout)
    _begin_ap(sh, cfg, purpose="boot", rescue_seconds=cfg.rescue_timeout,
              require_rescue=False)
    disarm_rescue(sh)  # AP verified: the safety net has done its job
    return 0


def cmd_stop(cfg: Config, sh: Shell) -> int:
    if not _daemons_present(sh) and read_state() is None:
        log.info("no AP running; nothing to stop")
        return 0
    return _report_restore(_unwind(sh, cfg, wait=True), "AP stopped", cfg)


def _seconds_left(state: dict | None, sh: Shell) -> int | None:
    deadline = (state or {}).get("deadline")
    return None if deadline is None else max(0, int(deadline - sh.wallclock()))


def cmd_status(cfg: Config, sh: Shell) -> int:
    state = read_state()
    if _ap_running(sh):
        print(f"mode: AP  (SSID '{cfg.ssid}', gateway {cfg.address.ip})")
    elif wifi_connected(sh, cfg.interface):
        print("mode: client (connected to WiFi)")
    else:
        print("mode: disconnected (no AP, no WiFi)")
    if state and state.get("purpose") == "test" and state.get("committed") is not True:
        left = _seconds_left(state, sh)
        when = f"rolls back in ~{left}s" if left is not None else "rolls back on its own"
        print(f"live test: UNCOMMITTED, {when}. "
              "Keep it: ghostap test --commit   Revert now: ghostap test --abort")
    armed = sh.run("systemctl", "is-active", f"{RESCUE_UNIT}.timer", check=False)
    print("rescue timer: " + ("armed" if armed.stdout.strip() == "active" else "not armed"))
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


COMMANDS = {"start": cmd_start, "stop": cmd_stop, "status": cmd_status,
            "recover": cmd_recover, "test": cmd_test_start}


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
    if (args.expect or args.original) and args.command != "recover":
        p.error("--expect and --original only apply to the 'recover' command")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    _setup_file_log(args.dry_run)
    _install_signal_handlers()
    try:
        cfg = load_config(args.config)
        sh = Shell(args.dry_run)
        if args.command == "recover":
            return cmd_recover(cfg, sh, expect=args.expect, original=args.original)
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
