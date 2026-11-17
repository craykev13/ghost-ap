#!/usr/bin/env python3
"""GhostAP safety tests. No Pi, no root, no hostapd needed.

Run from the folder holding controller.py (or from the repo root):

    python3 test_recovery.py -v            # if this file sits next to controller.py
    python3 tests/test_recovery.py -v      # from the repo root

How it works: a FakeSystem pretends to be NetworkManager, hostapd, dnsmasq and
systemd timers. It can "kill" the controller right after ANY step, exactly like
SIGKILL: no cleanup code runs. Then the rescue timer fires `recover`, and we
check the Pi ended up in one of two sane states and never a half-switched one:

    AP state:     radio unmanaged, address set, hostapd ENABLED + dnsmasq running
    client state: radio managed, no daemons of ours, no leftover AP address
"""
import contextlib
import dataclasses
import importlib.util
import io
import itertools
import json
import logging
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent


def _load_controller():
    for candidate in (HERE.parent / "ghostap" / "controller.py",
                      HERE / "controller.py", HERE.parent / "controller.py"):
        if candidate.exists():
            spec = importlib.util.spec_from_file_location("ghostap_controller", candidate)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod      # dataclasses needs this
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit("can't find controller.py next to or above this test file")


ctl = _load_controller()
if not os.environ.get("GHOSTAP_TEST_LOG"):
    logging.disable(logging.CRITICAL)          # set GHOSTAP_TEST_LOG=1 to see controller logs
IFACE = "wlan0"


def ipaddress_if(text):
    return ctl.ipaddress.IPv4Interface(text)
ORIGINAL = "PhoneHotspot"


class SimulatedCrash(BaseException):
    """Like SIGKILL: not an Exception, so no `except GhostAPError` cleanup can run."""


READS = (("nmcli", "-t"), ("nmcli", "-g"), ("ip", "-4"), ("hostapd_cli",), ("pgrep",),
         ("systemctl", "is-active"), ("nmcli", "device", "wifi", "rescan"))


class FakeSystem(ctl.Shell):
    def __init__(self, *, connected=False, connect_after=None, decoy=False,
                 reconnect_delay=0, hostapd_enable_delay=0, daemon_exit_delay=1.0):
        super().__init__(dry_run=False)
        shutil.rmtree(ctl.RUN_DIR, ignore_errors=True)   # a new machine has a clean /run
        self.now = 0.0
        self.managed = True
        self.known = [ORIGINAL]          # saved WiFi profiles
        self.in_range = {ORIGINAL} if (connected or connect_after is not None) else set()
        self.client = ORIGINAL if connected else None
        if decoy:
            # A saved network NetworkManager would happily pick instead. Only an explicit
            # "rejoin the ORIGINAL connection" gets us back to where the user was.
            self.known.insert(0, "OtherWiFi")
            self.in_range.add("OtherWiFi")
        self.connect_after = connect_after
        self.reconnect_delay = reconnect_delay      # seconds NM takes to finish connecting
        self.hostapd_enable_delay = hostapd_enable_delay
        self.daemon_exit_delay = daemon_exit_delay   # seconds a signalled daemon takes to exit
        self.rescans = 0                             # `nmcli device wifi rescan` calls
        self.join_calls = 0                          # `nmcli device connect` calls
        self.double_signalled = []                   # pids signalled again while shutting down
        self.pending = None                         # (profile, ready_at) while NM connects
        self.last_exit = None
        self.ip = None
        self.procs = {}                  # pid -> {name, cmdline, enabled}
        self.next_pid = 1000
        self.timers = {}                 # unit -> {"due": seconds, "argv": [...]}
        self.log = []
        self.captures = []
        self.effect_count = 0
        self.crash_at = None             # raise SimulatedCrash after this many effects
        self.interrupt_at = None         # raise ctl.Interrupted after this many effects
        # failure switches
        self.fail_hostapd = False
        self.hostapd_broken = False      # starts, but never reaches ENABLED
        self.hostapd_no_pidfile = False
        self.fail_systemd_run = False
        self.systemd_run_half_creates = False   # timer gets created, THEN the call errors
        self.fail_snapshot_timer = False        # only the follow-up snapshot timer can't be armed

    @property
    def timer(self):                     # the rescue timer; most tests only care about this one
        return self.timers.get("ghostap-rescue")

    @timer.setter
    def timer(self, value):
        if value is None:
            self.timers.pop("ghostap-rescue", None)
        else:
            self.timers["ghostap-rescue"] = value

    @property
    def failover_timer(self):
        return self.timers.get("ghostap-failover")

    # ---- fault injection -------------------------------------------------
    def _tick(self):
        self.effect_count += 1
        if self.crash_at == self.effect_count:
            raise SimulatedCrash(f"killed after effect #{self.effect_count}")
        if self.interrupt_at == self.effect_count:
            raise ctl.Interrupted("simulated signal")

    @staticmethod
    def _is_effect(cmd):
        return not any(tuple(cmd[:len(p)]) == p for p in READS)

    def reap_children(self):
        """What systemd does to a service's cgroup when the service is killed."""
        self.procs.clear()

    # ---- Shell interface -------------------------------------------------
    def sleep(self, seconds):
        self.now += seconds
        self._reap()

    def monotonic(self):
        return self.now

    def wallclock(self):
        return 1_000_000.0 + self.now

    def _reap(self):
        for pid, p in list(self.procs.items()):
            if p.get("exit_at") is not None and self.now >= p["exit_at"]:
                del self.procs[pid]

    def _signal(self, pid):
        p = self.procs.get(pid)
        if p is None:
            return
        if p.get("exit_at") is not None:
            # Second signal mid-teardown: on real brcmfmac this wedges the firmware.
            self.double_signalled.append(pid)
            return
        p["exit_at"] = self.now + self.daemon_exit_delay

    def pid_alive(self, pid):
        self._reap()
        return pid in self.procs

    def kill(self, pid):
        self._reap()
        self._signal(pid)
        self._tick()

    def mark(self, what):
        self._tick()

    def environment_problems(self, cfg):
        return []

    def capture(self, *cmd):
        """Diagnostic dumps are read-only: they must never count as an effect."""
        self.captures.append(cmd)
        return f"(fake output of {' '.join(cmd)})"

    def run(self, *cmd, check=True):
        self.log.append(cmd)
        cp = self._dispatch(list(cmd))
        failed_join = cmd[:5] == ("nmcli", "-w", "0", "device", "connect") and cp.returncode != 0
        if self._is_effect(cmd) and not failed_join:   # a join that found nothing changes nothing
            self._tick()
        if check and cp.returncode != 0:
            raise ctl.GhostAPError(f"{cmd[0]} failed: {cp.stderr}")
        return cp

    # ---- the simulated machine ------------------------------------------
    def _cp(self, cmd, rc=0, out="", err=""):
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def _settle(self):
        if self.pending and self.managed and self.now >= self.pending[1]:
            self.client, self.pending = self.pending[0], None

    def _begin_connect(self, name):
        if name in self.in_range:
            self.pending = (name, self.now + self.reconnect_delay)
            self._settle()

    def _maybe_autoconnect(self):
        if (self.managed and self.client is None and self.connect_after is not None
                and self.now >= self.connect_after):
            self.client = self.known[0]

    def _dispatch(self, c):
        prog = c[0]
        if prog == "nmcli":
            if c[1] == "-t" and "list" in c:           # nmcli -t -f SSID,SIGNAL device wifi list
                return self._cp(c, out="".join(f"{n}:70\n" for n in sorted(self.in_range)))
            if c[1] == "-t":
                self._maybe_autoconnect()
                self._settle()
                if not self.managed:
                    state = "unmanaged"
                elif self.client:
                    state = "connected"
                elif self.pending:
                    state = "connecting (getting IP configuration)"
                else:
                    state = "disconnected"
                return self._cp(c, out=f"{IFACE}:wifi:{state}\nlo:loopback:unmanaged\n")
            if c[1:4] == ["device", "wifi", "rescan"]:
                self.rescans += 1
                return self._cp(c)
            if c[1] == "-g":
                self._settle()
                name = self.client if self.managed else None
                return self._cp(c, out=(name or "--") + "\n")
            if c[1:3] == ["device", "set"]:
                self.managed = c[5] == "yes"
                if not self.managed:
                    self.client = self.pending = None   # NetworkManager drops the connection
                return self._cp(c)
            if c[1:3] == ["-w", "0"] and c[3:5] == ["connection", "up"]:
                # Like the real `-w 0`: success means "activation REQUESTED", not "connected".
                if not self.managed:
                    return self._cp(c, 1, err="device not managed")
                if c[6] not in self.known:
                    return self._cp(c, 10, err="unknown connection")
                self._begin_connect(c[6])
                return self._cp(c)
            if c[1:3] == ["-w", "0"] and c[3:5] == ["device", "connect"]:
                self.join_calls += 1
                for name in self.known:
                    if self.managed and name in self.in_range:
                        self._begin_connect(name)
                        return self._cp(c)
                return self._cp(c, 1, err="no suitable connection")
        if prog == "ip":
            if c[1:3] == ["addr", "flush"]:
                self.ip = None
                return self._cp(c)
            if c[1:3] == ["addr", "add"]:
                self.ip = c[3]
                return self._cp(c)
            if c[1:3] == ["link", "set"]:
                return self._cp(c)
            if c[1] == "-4":
                out = f"3: {IFACE}    inet {self.ip} scope global {IFACE}\n" if self.ip else ""
                return self._cp(c, out=out)
        if prog == "hostapd":
            if self.fail_hostapd:
                return self._cp(c, 1, err="nl80211: could not configure driver mode")
            pid = self._spawn("hostapd", " ".join(c), enabled=not self.hostapd_broken,
                              enable_at=self.now + self.hostapd_enable_delay)
            if not self.hostapd_no_pidfile:
                pathlib.Path(c[3]).write_text(str(pid))
            return self._cp(c)
        if prog == "dnsmasq":
            pid = self._spawn("dnsmasq", " ".join(c), enabled=True)
            ctl.DNSMASQ_PID.write_text(str(pid))
            return self._cp(c)
        if prog == "hostapd_cli":
            if any(p["name"] == "hostapd" and p["enabled"] and self.now >= p.get("enable_at", 0)
                   for p in self.procs.values()):
                return self._cp(c, out="state=ENABLED\nssid[0]=ZeroFi\n")
            return self._cp(c, 1, err="Failed to connect to hostapd")
        if prog in ("pkill", "pgrep"):
            pattern = c[-1]
            self._reap()
            hits = [pid for pid, p in self.procs.items() if pattern in p["cmdline"]]
            if prog == "pkill":
                for pid in hits:
                    self._signal(pid)
            return self._cp(c, 0 if hits else 1, out="\n".join(map(str, hits)))
        if prog == "systemd-run":
            if self.fail_systemd_run:
                return self._cp(c, 1, err="Failed to connect to bus")
            unit = next(a for a in c if a.startswith("--unit=")).split("=", 1)[1]
            if self.fail_snapshot_timer and unit == "ghostap-snapshot":
                return self._cp(c, 1, err="Failed to connect to bus")
            if self.systemd_run_half_creates and unit == "ghostap-rescue":
                self.timer = {"due": self.now + 1, "secs": 1, "argv": [], "cmd": tuple(c)}
                return self._cp(c, 1, err="Failed to start transient service unit")
            if unit in self.timers:
                return self._cp(c, 1, err=f"Unit {unit}.timer already exists.")
            secs = int(next(a for a in c if a.startswith("--on-active=")).split("=")[1][:-1])
            self.timers[unit] = {"due": self.now + secs, "secs": secs,
                                 "argv": c[c.index("--") + 1:], "cmd": tuple(c)}
            return self._cp(c)
        if prog == "systemctl":
            if c[1] == "is-active":
                armed = c[2].removesuffix(".timer") in self.timers
                return self._cp(c, 0 if armed else 3, out="active\n" if armed else "inactive\n")
            if c[1] == "stop" and c[2].endswith(".timer"):
                had = self.timers.pop(c[2].removesuffix(".timer"), None) is not None
                return self._cp(c, 0 if had else 5)
            return self._cp(c)
        raise AssertionError(f"FakeSystem doesn't know this command: {c}")

    def _spawn(self, name, cmdline, enabled, enable_at=0):
        pid = self.next_pid
        self.next_pid += 1
        self.procs[pid] = {"name": name, "cmdline": cmdline, "enabled": enabled,
                           "enable_at": enable_at}
        return pid

    # ---- time passing: the rescue timer fires ---------------------------
    def fire_timer(self, cfg):
        """systemd runs the armed timer's command in a fresh process."""
        if self.timer is None:
            return False
        argv = self.timer["argv"]
        self.now = max(self.now, self.timer["due"])
        self.timer = None
        self.crash_at = self.interrupt_at = None
        args = ctl.build_parser().parse_args(argv[2:])      # [python, script, "recover", ...]
        assert args.command == "recover", argv
        self.last_exit = ctl.cmd_recover(cfg, self, expect=args.expect, original=args.original)
        return True


    def fire_failover(self, cfg):
        """The failover countdown ends: systemd runs `ghostap failover` in a fresh process."""
        timer = self.timers.pop("ghostap-failover", None)
        if timer is None:
            return False
        self.now = max(self.now, timer["due"])
        self.crash_at = self.interrupt_at = None
        args = ctl.build_parser().parse_args(timer["argv"][2:])
        assert args.command == "failover", timer["argv"]
        self.last_exit = ctl.cmd_failover(cfg, self)
        # The service has finished. systemd now cleans up its cgroup: by default that
        # kills every process still in it, including a daemonized hostapd and dnsmasq.
        if "--property=KillMode=process" not in timer["cmd"]:
            self.reap_children()
        return True


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = pathlib.Path(tmp.name) / "run"
        for name, value in dict(
            RUN_DIR=run, HOSTAPD_CONF=run / "hostapd.conf", HOSTAPD_PID=run / "hostapd.pid",
            DNSMASQ_CONF=run / "dnsmasq.conf", DNSMASQ_PID=run / "dnsmasq.pid",
            STATE_FILE=run / "state.json", SNAPSHOT_FILE=pathlib.Path(tmp.name) / "snap" / "ap-snapshots.log",
        ).items():
            p = mock.patch.object(ctl, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = ctl.Config(
            interface=IFACE, boot_timeout=45, ssid="ZeroFi", passphrase="testpass123",
            country="US", channel=6, address=ctl.ipaddress.IPv4Interface("192.168.4.1/24"),
            dhcp_start="192.168.4.10", dhcp_end="192.168.4.50", lease_time="1h",
            rescue_timeout=90, live_timeout=120, path=pathlib.Path("/etc/ghostap/ghostap.toml"),
            reconnect_timeout=20,
        )

    # ---- the two sane end states ---------------------------------------
    def is_ap(self, s):
        names = sorted(p["name"] for p in s.procs.values())
        return (not s.managed and s.ip == str(self.cfg.address)
                and names == ["dnsmasq", "hostapd"]
                and all(p["enabled"] for p in s.procs.values()))

    def is_client(self, s):
        return s.managed and not s.procs and s.ip is None

    def describe(self, s):
        return (f"managed={s.managed} ip={s.ip} client={s.client} timer={s.timer is not None} "
                f"procs={[p['name'] for p in s.procs.values()]} "
                f"state={ctl.read_state()}")

    def crash_points(self, build_system, action, *, kill_children_too=True):
        """Run `action` once per possible crash point (and once more to find the end).
        Yields (system, crash_at, kill_children) AFTER the crash, BEFORE recovery."""
        variants = (False, True) if kill_children_too else (False,)
        for kill_children in variants:
            for crash_at in itertools.count(1):
                s = build_system()
                s.crash_at = s.effect_count + crash_at
                try:
                    action(s)
                except SimulatedCrash:
                    pass
                else:
                    break                      # ran to the end: no more crash points
                if kill_children:
                    s.reap_children()
                s.crash_at = None
                yield s, crash_at, kill_children


class BootFlowTests(Base):
    def test_wifi_connects_so_no_ap_and_no_timer(self):
        s = FakeSystem(connect_after=10)
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(s.managed and s.client == ORIGINAL)
        self.assertIsNone(s.timer)
        self.assertFalse(s.procs)
        self.assertIsNone(ctl.read_state())
        self.assertFalse([c for c in s.log if c[0] == "systemd-run"])

    def test_no_wifi_starts_verified_ap_and_disarms_timer(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_ap(s), self.describe(s))
        self.assertIsNone(s.timer, "boot AP verified: rescue timer should be disarmed")
        state = ctl.read_state()
        self.assertEqual((state["purpose"], state["committed"]), ("boot", True))

    def test_rescue_timer_is_armed_before_the_radio_is_touched(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        names = [c[0] + " " + " ".join(c[1:3]) for c in s.log]
        armed = next(i for i, c in enumerate(s.log) if c[0] == "systemd-run")
        switched = next(i for i, c in enumerate(s.log) if c[:3] == ("nmcli", "device", "set"))
        self.assertLess(armed, switched, names)

    def test_timer_command_runs_recover_with_this_config(self):
        s = FakeSystem()
        s.crash_at = 4          # die right after arming + state write
        with self.assertRaises(SimulatedCrash):
            ctl.cmd_start(self.cfg, s)
        self.assertEqual(s.timer["secs"], 90)
        self.assertEqual(s.timer["argv"][2:],
                         ["recover", "--config", str(self.cfg.path), "--expect=boot"])

    def test_crash_at_every_step_recovers_to_a_sane_state(self):
        covered = 0
        for s, crash_at, kill_children in self.crash_points(FakeSystem, lambda s: ctl.cmd_start(self.cfg, s)):
            if kill_children and s.timer is None and not s.procs:
                # The controller died AFTER the timer was disarmed AND systemd then
                # killed hostapd too. The shipped unit uses KillMode=process so this
                # can't happen (pinned by test_service_unit_keeps_daemons_alive).
                continue
            s.fire_timer(self.cfg)
            with self.subTest(crash_at=crash_at, kill_children=kill_children):
                self.assertTrue(self.is_ap(s) or self.is_client(s),
                                "half-switched! " + self.describe(s))
                self.assertIsNone(s.timer, "timer still armed: " + self.describe(s))
            covered += 1
        self.assertGreaterEqual(covered, 16, "expected many crash points to be exercised")

    def test_service_unit_keeps_daemons_alive_if_controller_dies(self):
        """Once the timer is disarmed, nothing else would rescue a stranded radio, so
        systemd must not kill hostapd/dnsmasq just because the controller process died."""
        for candidate in (HERE.parent / "systemd" / "ghostap.service", HERE / "ghostap.service",
                          HERE.parent / "ghostap.service"):
            if candidate.exists():
                text = candidate.read_text()
                break
        else:
            self.skipTest("ghostap.service not found next to the tests")
        directives = [l.strip() for l in text.splitlines() if not l.lstrip().startswith("#")]
        self.assertIn("KillMode=process", directives)
        self.assertIn("RemainAfterExit=yes", directives)

    def test_crash_before_any_change_leaves_network_alone(self):
        s = FakeSystem()
        s.crash_at = 1                       # dies right after the first harmless step
        with self.assertRaises(SimulatedCrash):
            ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_client(s))

    def test_hostapd_command_fails_so_we_roll_back(self):
        s = FakeSystem()
        s.fail_hostapd = True
        with self.assertRaises(ctl.GhostAPError):
            ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertIsNone(s.timer)
        self.assertIsNone(ctl.read_state())

    def test_hostapd_starts_but_never_enables_so_we_roll_back(self):
        s = FakeSystem()
        s.hostapd_broken = True
        with self.assertRaises(ctl.GhostAPError):
            ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertIsNone(s.timer)
        self.assertGreaterEqual(s.now, 15, "should have waited the full verify window")

    def test_signal_at_every_step_unwinds_immediately(self):
        for k in itertools.count(1):
            s = FakeSystem()
            s.interrupt_at = k
            try:
                ctl.cmd_start(self.cfg, s)
            except ctl.Interrupted:
                pass
            else:
                break
            s.interrupt_at = None
            s.fire_timer(self.cfg)           # harmless if it was left armed
            with self.subTest(interrupt_at=k):
                # Mid-switch signals unwind at once. A signal after the AP was
                # verified legitimately leaves the working AP in place.
                self.assertTrue(self.is_client(s) or self.is_ap(s), self.describe(s))
                self.assertIsNone(s.timer)
                if self.is_client(s):
                    self.assertIsNone(ctl.read_state())
        self.assertGreater(k, 10)

    def test_boot_proceeds_without_safety_net_when_arming_fails(self):
        s = FakeSystem()
        s.fail_systemd_run = True
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_ap(s), "no WiFi to lose at boot, so still start the AP")

    def test_already_running_ap_is_left_alone(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        before = len(s.log)
        ctl.cmd_start(self.cfg, s)
        self.assertEqual(len(s.log), before)
        self.assertTrue(self.is_ap(s))


class RecoverTests(Base):
    def test_healthy_client_is_not_disturbed(self):
        s = FakeSystem(connected=True)
        ctl.cmd_recover(self.cfg, s)
        self.assertEqual(s.client, ORIGINAL)
        self.assertFalse([c for c in s.log if c[:3] == ("ip", "addr", "flush")],
                         "recover must not flush the address of a working connection")
        self.assertFalse([c for c in s.log if c[:3] == ("nmcli", "device", "set")])

    def test_healthy_boot_ap_is_preserved(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        s.timer = {"due": 0, "secs": 90, "argv": []}      # pretend the timer is still armed
        ctl.cmd_recover(self.cfg, s)
        self.assertTrue(self.is_ap(s))
        self.assertIsNone(s.timer)

    def test_recover_twice_changes_nothing_the_second_time(self):
        s = FakeSystem()
        s.hostapd_broken = True
        s.crash_at = 9                                    # hostapd just started
        try:
            ctl.cmd_start(self.cfg, s)
        except SimulatedCrash:
            pass
        s.crash_at = None
        s.fire_timer(self.cfg)
        self.assertTrue(self.is_client(s))
        snapshot = (s.managed, s.client, s.ip, dict(s.procs), s.timer)
        ctl.cmd_recover(self.cfg, s)
        self.assertEqual(snapshot, (s.managed, s.client, s.ip, dict(s.procs), s.timer))

    def test_orphaned_hostapd_without_pidfile_is_still_killed(self):
        """Controller died between `hostapd -B` and its pid file appearing."""
        s = FakeSystem()
        s.hostapd_no_pidfile = True
        s.crash_at = 9
        with self.assertRaises(SimulatedCrash):
            ctl.cmd_start(self.cfg, s)
        self.assertTrue(any(p["name"] == "hostapd" for p in s.procs.values()))
        self.assertFalse(ctl.HOSTAPD_PID.exists())
        s.crash_at = None
        s.fire_timer(self.cfg)
        self.assertTrue(self.is_client(s), self.describe(s))

    def test_lone_dnsmasq_without_hostapd_is_cleaned_up(self):
        s = FakeSystem()
        s.fail_hostapd = True
        with self.assertRaises(ctl.GhostAPError):
            ctl.cmd_start(self.cfg, s)
        s._spawn("dnsmasq", f"dnsmasq --conf-file={ctl.DNSMASQ_CONF}", True)
        ctl.cmd_recover(self.cfg, s)
        self.assertTrue(self.is_client(s))

    def test_recover_itself_killed_midway_then_rerun_converges(self):
        def live_test_ap_up():
            s = FakeSystem(connected=True, decoy=True)
            ctl.cmd_test_start(self.cfg, s)
            return s

        def boot_crashed_with_lone_hostapd():
            s = FakeSystem()
            s.crash_at = 9
            try:
                ctl.cmd_start(self.cfg, s)
            except SimulatedCrash:
                pass
            s.crash_at = None
            return s

        for label, build in (("live test", live_test_ap_up),
                             ("boot crash", boot_crashed_with_lone_hostapd)):
            covered = 0
            for crash_at in itertools.count(1):
                s = build()
                s.timer, original_timer = None, s.timer      # the timer is about to fire
                s.crash_at = s.effect_count + crash_at
                try:
                    ctl.cmd_recover(self.cfg, s)
                except SimulatedCrash:
                    pass
                else:
                    break
                s.crash_at = None
                ctl.cmd_recover(self.cfg, s)                 # rerun: must be safe from any point
                with self.subTest(scenario=label, recover_crash_at=crash_at):
                    self.assertTrue(self.is_client(s), self.describe(s))
                    self.assertIsNone(s.timer)
                covered += 1
            self.assertGreaterEqual(covered, 4, label)


class ApDiagnosticsTests(Base):
    """"Incorrect password" on an iPhone can mean any failed handshake. With
    hostapd_debug on, hostapd says which step failed (journalctl -t hostapd)."""

    def test_debug_logging_is_off_by_default(self):
        self.assertNotIn("logger_syslog", ctl.render_hostapd(self.cfg))

    def test_debug_logging_goes_to_the_journal_when_asked(self):
        cfg = dataclasses.replace(self.cfg, hostapd_debug=True)
        conf = ctl.render_hostapd(cfg)
        self.assertIn("logger_syslog=-1", conf.splitlines())
        self.assertIn("logger_syslog_level=0", conf.splitlines())

    def test_the_switch_is_read_from_the_config_file(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pw").write_text("testpass123\n")
        os.chmod(d / "pw", 0o600)
        for flag, want in (("true", True), ("false", False), (None, False)):
            extra = f"hostapd_debug = {flag}\n" if flag else ""
            (d / "c.toml").write_text(
                '[network]\ninterface = "wlan0"\n[access_point]\nssid = "ZeroFi"\n'
                f'passphrase_file = "{d / "pw"}"\n{extra}'
                '[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n')
            self.assertEqual(ctl.load_config(d / "c.toml").hostapd_debug, want, flag)


class FailoverTests(Base):
    """Connection loss AFTER boot: NetworkManager reports the drop, a countdown runs,
    and if WiFi is still gone the Pi raises the AP. Event-driven: no polling."""

    def setUp(self):
        super().setUp()
        self.cfg = dataclasses.replace(self.cfg, failover_grace=120, failover_join_timeout=30)

    def test_failover_ap_survives_the_failover_job_finishing(self):
        """Seen on hardware: the AP came up, then vanished ~90s later. hostapd and
        dnsmasq were started inside the transient failover service, and systemd killed
        them when that service ended."""
        s = self.drop(findable=False)
        ctl.cmd_event(self.cfg, s, IFACE, "down")
        self.assertTrue(s.fire_failover(self.cfg))
        self.assertTrue(self.is_ap(s), self.describe(s))

    def test_failover_job_is_started_with_killmode_process(self):
        s = self.drop(findable=False)
        ctl.cmd_event(self.cfg, s, IFACE, "down")
        self.assertIn("--property=KillMode=process", s.failover_timer["cmd"])

    def drop(self, *, findable):
        """A Pi that was connected and then lost WiFi. `findable`: the network is still
        in range, NetworkManager just isn't getting to it."""
        s = FakeSystem(connected=True, connect_after=10 ** 6 if findable else None)
        s.client = None
        s.pending = None
        if not findable:
            s.in_range = set()
        return s

    def down(self, s):
        return ctl.cmd_event(self.cfg, s, IFACE, "down")

    # ---- the countdown ---------------------------------------------------
    def test_wifi_going_down_starts_the_countdown(self):
        s = self.drop(findable=False)
        self.down(s)
        self.assertIsNotNone(s.failover_timer)
        self.assertEqual(s.failover_timer["secs"], 120)
        self.assertIsNone(s.timer, "the rescue timer is for AP switches, not for this")

    def test_wifi_coming_back_cancels_it(self):
        s = self.drop(findable=False)
        self.down(s)
        ctl.cmd_event(self.cfg, s, IFACE, "up")
        self.assertIsNone(s.failover_timer)

    def test_flapping_restarts_the_countdown_from_the_last_drop(self):
        s = self.drop(findable=False)
        self.down(s)
        s.sleep(100)
        self.down(s)
        self.assertEqual(s.failover_timer["due"], s.now + 120)

    def test_off_by_default(self):
        s = self.drop(findable=False)
        ctl.cmd_event(dataclasses.replace(self.cfg, failover_grace=0), s, IFACE, "down")
        self.assertFalse(s.timers)
        self.assertFalse([c for c in s.log if c[0] == "systemd-run"])

    def test_other_interfaces_and_actions_are_ignored(self):
        s = self.drop(findable=False)
        ctl.cmd_event(self.cfg, s, "eth0", "down")
        self.assertFalse(s.timers, "another interface must not start our countdown")
        ctl.cmd_event(self.cfg, s, IFACE, "dhcp4-change")
        self.assertFalse(s.timers, "only up/down mean anything")
        ctl.cmd_event(self.cfg, s, IFACE, "up")          # nothing armed: harmless
        self.assertFalse(s.timers)

    def test_a_down_event_while_we_hold_the_radio_is_ignored(self):
        s = FakeSystem(connected=True, decoy=True)
        ctl.cmd_test_start(self.cfg, s)                  # we took the radio ourselves
        self.assertTrue(self.is_ap(s))
        self.down(s)                                     # NetworkManager reports it going down
        self.assertIsNone(s.failover_timer)

    # ---- when the countdown ends ----------------------------------------
    def test_failover_rejoins_if_the_network_is_still_findable(self):
        s = self.drop(findable=True)
        self.down(s)
        s.fire_failover(self.cfg)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertFalse(s.procs, "no AP should have been raised")
        self.assertLess(s.now - 120, 40)

    def test_failover_raises_the_ap_when_wifi_is_really_gone(self):
        s = self.drop(findable=False)
        self.down(s)
        s.fire_failover(self.cfg)
        self.assertTrue(self.is_ap(s), self.describe(s))
        self.assertGreaterEqual(s.now, 120 + 30, "should look for WiFi once more first")
        state = ctl.read_state()
        self.assertEqual((state["purpose"], state["committed"]), ("boot", True))
        self.assertIsNone(s.timer, "rescue timer must be disarmed once the AP is verified")

    def test_the_failover_ap_stays_up(self):
        s = self.drop(findable=False)
        self.down(s)
        s.fire_failover(self.cfg)
        self.assertFalse(s.fire_timer(self.cfg), "nothing left to fire")
        self.assertTrue(self.is_ap(s), self.describe(s))

    def test_failover_does_nothing_if_wifi_came_back(self):
        s = self.drop(findable=True)
        self.down(s)
        s.client = ORIGINAL                              # it reconnected by itself
        before = len(s.log)
        s.fire_failover(self.cfg)
        self.assertTrue(self.is_client(s))
        self.assertFalse(s.procs)
        self.assertFalse([c for c in s.log[before:] if c[0] in ("hostapd", "dnsmasq")])

    def test_failover_does_nothing_if_an_ap_is_already_running(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)                       # boot fallback AP
        self.assertTrue(self.is_ap(s))
        snapshot = (s.managed, s.ip, dict(s.procs), dict(s.timers))
        ctl.cmd_failover(self.cfg, s)
        self.assertEqual(snapshot, (s.managed, s.ip, dict(s.procs), dict(s.timers)))

    def test_failover_does_nothing_during_a_live_test(self):
        s = FakeSystem(connected=True, decoy=True)
        ctl.cmd_test_start(self.cfg, s)
        procs = dict(s.procs)
        ctl.cmd_failover(self.cfg, s)
        self.assertEqual(procs, dict(s.procs))
        self.assertEqual(ctl.read_state()["purpose"], "test")

    def test_if_the_ap_cannot_start_the_radio_goes_back_to_networkmanager(self):
        s = self.drop(findable=False)
        s.fail_hostapd = True
        self.down(s)
        with self.assertRaises(ctl.GhostAPError):
            s.fire_failover(self.cfg)
        self.assertTrue(s.managed)
        self.assertFalse(s.procs)
        self.assertIsNone(ctl.read_state())

    # ---- wiring -----------------------------------------------------------
    def test_status_shows_the_countdown(self):
        s = self.drop(findable=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ctl.cmd_status(self.cfg, s)
        self.assertIn("countdown not running", out.getvalue())
        self.down(s)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ctl.cmd_status(self.cfg, s)
        self.assertIn("countdown RUNNING", out.getvalue())

    def test_event_arguments_parse(self):
        args = ctl.build_parser().parse_args(["event", "wlan0", "down"])
        self.assertEqual((args.command, args.event_args), ("event", ["wlan0", "down"]))
        for bad in (["event", "wlan0"], ["start", "wlan0"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                ctl.main(bad)

    def test_settings_come_from_the_config_file_and_are_validated(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pw").write_text("testpass123\n")
        os.chmod(d / "pw", 0o600)

        def load(recovery):
            (d / "c.toml").write_text(
                '[network]\ninterface = "wlan0"\n[access_point]\nssid = "ZeroFi"\n'
                f'passphrase_file = "{d / "pw"}"\n[recovery]\n{recovery}'
                '[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n')
            return ctl.load_config(d / "c.toml")

        cfg = load("failover_grace = 90\nfailover_join_timeout = 25\n")
        self.assertEqual((cfg.failover_grace, cfg.failover_join_timeout), (90, 25))
        self.assertEqual(load("").failover_grace, 0, "must be opt-in")
        self.assertEqual(load("failover_grace = 0\n").failover_grace, 0)
        with self.assertRaises(ctl.GhostAPError):
            load("failover_grace = 5\n")
        with self.assertRaises(ctl.GhostAPError):
            load("failover_join_timeout = 3\n")

    def test_the_networkmanager_hook_only_forwards_up_and_down(self):
        hook = HERE.parent / "nm-dispatcher" / "90-ghostap"
        if not hook.exists():
            self.skipTest("nm-dispatcher/90-ghostap not found next to the tests")
        lines = [l.strip() for l in hook.read_text().splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
        self.assertIn("up|down)", " ".join(lines))
        self.assertTrue(any('ghostap event "$1" "$2"' in l for l in lines), lines)
        self.assertEqual(lines[-1], "exit 0", "a hook must never fail NetworkManager")


class BootJoinTests(Base):
    """Hardware finding: NetworkManager's own autoconnect on the Pi Zero W took about
    as long as the old boot window, so the Pi gave up and raised the AP with the
    hotspot sitting right there. The boot flow now rescans and asks to join."""

    @staticmethod
    @contextlib.contextmanager
    def logs_enabled():
        """The suite silences logging globally; these tests assert on log output."""
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        try:
            yield
        finally:
            logging.disable(previous)

    def test_joins_a_visible_network_without_waiting_for_slow_autoconnect(self):
        s = FakeSystem(connect_after=500)          # NM would not get to it for 500 s
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertIsNone(s.timer, "no AP, so no rescue timer should have been armed")
        self.assertLess(s.now, 30, "should join within a couple of attempts")
        self.assertGreaterEqual(s.rescans, 1)

    def test_already_connected_is_left_alone(self):
        s = FakeSystem(connected=True)
        ctl.cmd_start(self.cfg, s)
        self.assertEqual((s.rescans, s.join_calls), (0, 0))
        self.assertTrue(self.is_client(s))

    def test_an_activation_in_progress_is_not_interrupted(self):
        s = FakeSystem(connect_after=500, reconnect_delay=25)   # connecting takes 25 s
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertEqual(s.join_calls, 1, "asked again while a connection was in progress")

    def test_falls_back_to_the_ap_when_nothing_known_is_in_range(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_ap(s), self.describe(s))
        self.assertGreaterEqual(s.rescans, 3, "should keep looking until the deadline")
        self.assertLessEqual(s.rescans, 7, "rescanning every few seconds would hammer the radio")
        self.assertGreaterEqual(s.now, self.cfg.boot_timeout)

    def test_fallback_says_what_was_visible(self):
        s = FakeSystem()
        s.in_range.add("CafeWiFi")                 # visible, but not a saved network
        with self.logs_enabled(), self.assertLogs(ctl.log, level="WARNING") as cm:
            ctl.cmd_start(self.cfg, s)
        text = "\n".join(cm.output)
        self.assertIn("CafeWiFi", text)
        self.assertIn("no WiFi connection", text)

    def test_fallback_says_when_nothing_was_visible(self):
        s = FakeSystem()
        with self.logs_enabled(), self.assertLogs(ctl.log, level="WARNING") as cm:
            ctl.cmd_start(self.cfg, s)
        self.assertIn("In range: nothing", "\n".join(cm.output))


class GentleShutdownTests(Base):
    """Hardware finding (Stage B): a second SIGTERM sent to hostapd while it was
    still tearing the AP down made brcmfmac log -512 (ERESTARTSYS) and left the
    WiFi firmware out of step with the driver. Every daemon gets ONE signal."""

    def assertGentle(self, s):
        self.assertEqual(s.double_signalled, [],
                         "daemon signalled again while shutting down: " + self.describe(s))

    def test_rollback_signals_each_daemon_once(self):
        s = FakeSystem(connected=True, decoy=True)
        ctl.cmd_test_start(self.cfg, s)
        s.fire_timer(self.cfg)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertGentle(s)

    def test_stop_signals_each_daemon_once(self):
        s = FakeSystem(connected=False)
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(self.is_ap(s))
        ctl.cmd_stop(self.cfg, s)
        self.assertGentle(s)

    def test_slow_teardown_is_waited_for_not_repeated(self):
        s = FakeSystem(connected=True, decoy=True, daemon_exit_delay=6.0)
        ctl.cmd_test_start(self.cfg, s)
        s.fire_timer(self.cfg)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertGentle(s)
        self.assertFalse(s.procs, "daemons must really be gone before NM gets the radio back")

    def test_radio_is_not_handed_back_while_a_daemon_is_still_exiting(self):
        s = FakeSystem(connected=True, decoy=True, daemon_exit_delay=3.0)
        ctl.cmd_test_start(self.cfg, s)
        seen = []
        orig = s._dispatch

        def spy(c):
            if c[:4] == ["nmcli", "device", "set", IFACE] and c[-1] == "yes":
                seen.append(sorted(s.procs))
            return orig(c)
        s._dispatch = spy
        s.fire_timer(self.cfg)
        self.assertEqual(seen, [[]], "NetworkManager got the radio while hostapd was alive")

    def test_orphan_without_pid_file_gets_exactly_one_signal(self):
        s = FakeSystem(connected=True, decoy=True)
        ctl.cmd_test_start(self.cfg, s)
        ctl.HOSTAPD_PID.unlink()
        ctl.DNSMASQ_PID.unlink()
        s.fire_timer(self.cfg)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertFalse(s.procs)
        self.assertGentle(s)

    def test_a_stuck_daemon_is_not_signalled_a_second_time(self):
        s = FakeSystem(connected=True, decoy=True, daemon_exit_delay=10_000)
        ctl.cmd_test_start(self.cfg, s)
        s.fire_timer(self.cfg)
        self.assertGentle(s)


class LiveTestTests(Base):
    def live(self, **kw):
        s = FakeSystem(connected=True, decoy=True, **kw)
        ctl.cmd_test_start(self.cfg, s)
        return s

    def test_live_test_keeps_rollback_armed_after_ap_starts(self):
        s = self.live()
        self.assertTrue(self.is_ap(s))
        self.assertIsNotNone(s.timer, "live test must stay armed until committed")
        self.assertEqual(s.timer["secs"], 120 + ctl.STARTUP_ALLOWANCE)
        state = ctl.read_state()
        self.assertEqual((state["purpose"], state["committed"], state["original_connection"]),
                         ("test", False, ORIGINAL))

    def test_uncommitted_test_rolls_back_even_though_the_ap_is_healthy(self):
        s = self.live()
        s.fire_timer(self.cfg)
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertEqual(s.client, ORIGINAL, "should rejoin the ORIGINAL connection")
        self.assertIsNone(ctl.read_state())

    def test_committed_test_survives_the_timer(self):
        s = self.live()
        ctl.cmd_test_commit(self.cfg, s)
        self.assertIsNone(s.timer)
        self.assertFalse(s.fire_timer(self.cfg))
        self.assertTrue(self.is_ap(s))
        self.assertTrue(ctl.read_state()["committed"])

    def test_abort_reverts_right_now(self):
        s = self.live()
        ctl.cmd_test_abort(self.cfg, s)
        self.assertTrue(self.is_client(s))
        self.assertEqual(s.client, ORIGINAL)
        self.assertIsNone(s.timer)

    def test_commit_after_rollback_says_so(self):
        s = self.live()
        s.fire_timer(self.cfg)
        with self.assertRaises(ctl.GhostAPError) as cm:
            ctl.cmd_test_commit(self.cfg, s)
        self.assertIn("rolled back", str(cm.exception))

    def test_commit_refuses_when_ap_died_and_rolls_back(self):
        s = self.live()
        s.procs.clear()                                  # hostapd and dnsmasq die
        with self.assertRaises(ctl.GhostAPError):
            ctl.cmd_test_commit(self.cfg, s)
        self.assertTrue(self.is_client(s))
        self.assertEqual(s.client, ORIGINAL)
        self.assertIsNone(s.timer)

    def test_refuses_to_switch_when_rescue_timer_cannot_be_armed(self):
        s = FakeSystem(connected=True)
        s.fail_systemd_run = True
        with self.assertRaises(ctl.GhostAPError) as cm:
            ctl.cmd_test_start(self.cfg, s)
        self.assertIn("rescue timer", str(cm.exception))
        self.assertTrue(s.managed and s.client == ORIGINAL and s.ip is None,
                        "network must be untouched: " + self.describe(s))
        self.assertIsNone(ctl.read_state())

    def test_orphaned_daemon_without_pidfile_blocks_a_new_test(self):
        s = FakeSystem(connected=True, decoy=True)
        s._spawn("hostapd", f"hostapd -B -P /x {ctl.HOSTAPD_CONF}", True)   # no pid file
        with self.assertRaises(ctl.GhostAPError) as cm:
            ctl.cmd_test_start(self.cfg, s)
        self.assertIn("already active", str(cm.exception))
        self.assertEqual(s.client, ORIGINAL, "must not touch the network")

    def test_second_test_while_one_is_active_is_refused(self):
        s = self.live()
        with self.assertRaises(ctl.GhostAPError):
            ctl.cmd_test_start(self.cfg, s)
        self.assertTrue(self.is_ap(s))

    def test_crash_at_every_step_of_live_test_always_returns_to_original_wifi(self):
        covered = 0
        build = lambda: FakeSystem(connected=True, decoy=True)
        for s, crash_at, kill_children in self.crash_points(build, lambda s: ctl.cmd_test_start(self.cfg, s)):
            s.fire_timer(self.cfg)
            with self.subTest(crash_at=crash_at, kill_children=kill_children):
                self.assertTrue(self.is_client(s), "half-switched! " + self.describe(s))
                self.assertEqual(s.client, ORIGINAL, "lost the original connection: " + self.describe(s))
                self.assertIsNone(s.timer)
            covered += 1
        self.assertGreaterEqual(covered, 16)

    def test_crash_during_commit_never_leaves_a_half_state(self):
        covered = 0
        for s, crash_at, _ in self.crash_points(lambda: self.live(), lambda s: ctl.cmd_test_commit(self.cfg, s),
                                                kill_children_too=False):
            s.fire_timer(self.cfg)
            with self.subTest(crash_at=crash_at):
                # the state write IS the commit point, so a crash after it keeps the AP
                self.assertTrue(self.is_ap(s), self.describe(s))
                self.assertIsNone(s.timer)
            covered += 1
        self.assertGreaterEqual(covered, 2)


class SignalHandlingTests(Base):
    """Review point 2: signals across the whole transition."""

    def install_handlers(self):
        saved = {sig: signal.getsignal(sig) for sig in ctl.HANDLED_SIGNALS}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        ctl._install_signal_handlers()

    def test_live_test_signal_at_every_step_reverts_immediately(self):
        covered = 0
        for k in itertools.count(1):
            s = FakeSystem(connected=True, decoy=True)
            s.interrupt_at = k
            try:
                ctl.cmd_test_start(self.cfg, s)
            except ctl.Interrupted:
                pass
            else:
                break
            s.interrupt_at = None
            with self.subTest(interrupt_at=k):
                # Right away, WITHOUT waiting for the rescue timer.
                self.assertTrue(self.is_client(s), self.describe(s))
                self.assertEqual(s.client, ORIGINAL, self.describe(s))
                self.assertIsNone(s.timer, "timer left armed: " + self.describe(s))
                self.assertIsNone(ctl.read_state())
            covered += 1
        self.assertGreaterEqual(covered, 9)

    def test_signal_while_arming_leaves_a_working_connection_alone(self):
        """Died before the radio was touched: flushing addresses or reconnecting would
        itself disturb the connection. Only the timer should come down."""
        for k in (1, 2, 3):
            s = FakeSystem(connected=True, decoy=True)
            s.interrupt_at = k
            with self.assertRaises(ctl.Interrupted):
                ctl.cmd_test_start(self.cfg, s)
            touched = [c for c in s.log if c[:3] in (("ip", "addr", "flush"),
                                                     ("nmcli", "device", "set"),
                                                     ("nmcli", "-w", "0"))]
            with self.subTest(interrupt_at=k):
                self.assertFalse(touched, touched)
                self.assertEqual(s.client, ORIGINAL)
                self.assertIsNone(s.timer)

    def test_refusal_without_a_timer_changes_nothing(self):
        s = FakeSystem(connected=True, decoy=True)
        s.fail_systemd_run = True
        with self.assertRaises(ctl.NotStarted):
            ctl.cmd_test_start(self.cfg, s)
        self.assertFalse([c for c in s.log if c[:3] in (("ip", "addr", "flush"),
                                                        ("nmcli", "device", "set"))])
        self.assertEqual(s.client, ORIGINAL)

    def test_refusal_does_not_leave_a_half_created_timer_behind(self):
        s = FakeSystem(connected=True, decoy=True)
        s.systemd_run_half_creates = True
        with self.assertRaises(ctl.NotStarted):
            ctl.cmd_test_start(self.cfg, s)
        self.assertIsNone(s.timer, "a stray timer would fire `recover` at some unrelated moment")
        self.assertEqual(s.client, ORIGINAL)

    def test_sighup_is_a_noop_but_sigterm_and_sigint_interrupt(self):
        self.install_handlers()
        signal.raise_signal(signal.SIGHUP)        # an SSH session dying must NOT abort a test
        self.assertNotEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN,
                            "SIG_IGN would be inherited by hostapd/dnsmasq across exec")
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.assertRaises(ctl.Interrupted):
                signal.raise_signal(sig)

    def test_cleanup_cannot_be_cut_short_but_the_signal_is_not_lost(self):
        self.install_handlers()
        finished = []
        with self.assertRaises(ctl.Interrupted):
            with ctl._signals_blocked():
                signal.raise_signal(signal.SIGTERM)
                finished.append("cleanup ran to the end")
        self.assertEqual(finished, ["cleanup ran to the end"])


class ReconnectTests(Base):
    """Review point 3: NetworkManager owning the radio is not the same as being connected."""

    def test_abort_reports_success_only_when_wifi_is_really_back(self):
        s = FakeSystem(connected=True, decoy=True)
        ctl.cmd_test_start(self.cfg, s)
        self.assertEqual(ctl.cmd_test_abort(self.cfg, s), 0)
        self.assertEqual(s.client, ORIGINAL)

    def test_wifi_that_never_returns_is_reported_as_not_reconnected(self):
        s = FakeSystem(connected=True)
        ctl.cmd_test_start(self.cfg, s)
        s.in_range.clear()                         # hotspot was switched off meanwhile
        t0 = s.now
        self.assertEqual(ctl.cmd_test_abort(self.cfg, s), ctl.EXIT_NOT_RECONNECTED)
        # ownership IS restored, and cleanup is complete even though WiFi isn't back
        self.assertTrue(s.managed and not s.procs and s.ip is None)
        self.assertIsNone(ctl.read_state())
        self.assertIsNone(s.timer)
        waited = s.now - t0
        self.assertGreaterEqual(waited, self.cfg.reconnect_timeout)
        self.assertLess(waited, self.cfg.reconnect_timeout + 5, "the wait must be bounded")

    def test_slow_reconnect_is_waited_for(self):
        s = FakeSystem(connected=True, reconnect_delay=8)
        ctl.cmd_test_start(self.cfg, s)
        t0 = s.now
        self.assertEqual(ctl.cmd_test_abort(self.cfg, s), 0)
        self.assertGreaterEqual(s.now - t0, 8)
        self.assertLess(s.now - t0, self.cfg.reconnect_timeout)

    def test_timer_rollback_surfaces_a_failed_reconnect_in_its_exit_status(self):
        s = FakeSystem(connected=True)
        ctl.cmd_test_start(self.cfg, s)
        s.in_range.clear()
        s.fire_timer(self.cfg)
        self.assertEqual(s.last_exit, ctl.EXIT_NOT_RECONNECTED)
        self.assertTrue(self.is_client(s))

    def test_stop_waits_for_the_network_you_probably_want_back(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)                 # boot AP
        s.in_range.add(ORIGINAL)                   # hotspot switched on afterwards
        self.assertEqual(ctl.cmd_stop(self.cfg, s), 0)
        self.assertEqual(s.client, ORIGINAL)
        s2 = FakeSystem()
        ctl.cmd_start(self.cfg, s2)
        self.assertEqual(ctl.cmd_stop(self.cfg, s2), ctl.EXIT_NOT_RECONNECTED)

    def test_no_pointless_wait_when_there_was_never_a_network(self):
        s = FakeSystem()
        s.hostapd_broken = True
        with self.assertRaises(ctl.GhostAPError):
            ctl.cmd_start(self.cfg, s)
        self.assertLess(s.now, 45 + 15 + 5, "boot rollback must not wait for WiFi that was never there")

        s = FakeSystem()
        s.hostapd_no_pidfile = True
        s.crash_at = 9
        with self.assertRaises(SimulatedCrash):
            ctl.cmd_start(self.cfg, s)
        s.crash_at = None
        due = s.timer["due"]
        s.fire_timer(self.cfg)
        self.assertLess(s.now - due, 5, "recovering a boot crash must not wait for WiFi")
        self.assertEqual(s.last_exit, 0)

    def test_connected_state_variants_still_count_as_connected(self):
        class Variant(FakeSystem):
            def _dispatch(self, c):
                cp = super()._dispatch(c)
                if c[:2] == ["nmcli", "-t"]:
                    cp.stdout = cp.stdout.replace("wifi:connected", "wifi:connected (site only)")
                return cp
        self.assertTrue(ctl.wifi_connected(Variant(connected=True), IFACE))


class LiveWindowTests(Base):
    """Review point 4: the rollback timer is armed before the AP exists."""

    def test_usable_window_is_at_least_live_timeout_even_when_startup_is_slow(self):
        for startup in (0, 6, 14):
            s = FakeSystem(connected=True, hostapd_enable_delay=startup)
            ctl.cmd_test_start(self.cfg, s)
            left = s.timer["due"] - s.now           # time left once the AP is verified
            with self.subTest(startup_seconds=startup):
                self.assertGreaterEqual(left, self.cfg.live_timeout)
                self.assertAlmostEqual(ctl.read_state()["deadline"] - s.wallclock(), left, delta=1)

    def test_status_tells_you_how_long_is_left(self):
        s = FakeSystem(connected=True)
        ctl.cmd_test_start(self.cfg, s)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ctl.cmd_status(self.cfg, s)
        out = buf.getvalue()
        self.assertIn("UNCOMMITTED", out)
        self.assertRegex(out, r"rolls back in ~1[2-5]\d+s|rolls back in ~\d+s")
        self.assertIn("rescue timer: armed", out)
        ctl.cmd_test_commit(self.cfg, s)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ctl.cmd_status(self.cfg, s)
        self.assertNotIn("UNCOMMITTED", buf.getvalue())
        self.assertIn("rescue timer: not armed", buf.getvalue())


class LostStateTests(Base):
    """Review point 5: an AP is kept only on POSITIVE evidence that it should stay."""

    def live_ap(self):
        s = FakeSystem(connected=True, decoy=True)
        ctl.cmd_test_start(self.cfg, s)
        return s

    def assert_rolled_back_to_original(self, s):
        self.assertTrue(self.is_client(s), self.describe(s))
        self.assertEqual(s.client, ORIGINAL, "should rejoin the ORIGINAL network, not the decoy")
        self.assertIsNone(s.timer)

    def test_state_file_deleted_during_uncommitted_test_still_rolls_back(self):
        s = self.live_ap()
        ctl.STATE_FILE.unlink()
        s.fire_timer(self.cfg)
        self.assert_rolled_back_to_original(s)

    def test_corrupt_state_file_is_the_same_as_a_missing_one(self):
        s = self.live_ap()
        ctl.STATE_FILE.write_text("{ this is not json")
        s.fire_timer(self.cfg)
        self.assert_rolled_back_to_original(s)

    def test_state_without_a_committed_flag_is_not_committed(self):
        s = self.live_ap()
        ctl.STATE_FILE.write_text('{"purpose": "test"}')
        s.fire_timer(self.cfg)
        self.assert_rolled_back_to_original(s)

    def test_committed_only_counts_when_it_is_literally_true(self):
        for junk in ('"false"', '1', '"yes"', 'null'):
            s = self.live_ap()
            ctl.STATE_FILE.write_text('{"purpose": "test", "committed": %s}' % junk)
            s.fire_timer(self.cfg)
            with self.subTest(committed=junk):
                self.assert_rolled_back_to_original(s)

    def test_commit_that_crashed_before_disarming_keeps_the_ap(self):
        s = self.live_ap()
        ctl.write_state(s, {**ctl.read_state(), "committed": True})   # commit's write landed
        s.fire_timer(self.cfg)                                          # ...but disarm never did
        self.assertTrue(self.is_ap(s))

    def test_boot_ap_with_lost_state_is_kept_because_the_timer_says_boot(self):
        s = FakeSystem()
        s.crash_at = 11                      # boot AP up, controller killed before disarming
        with self.assertRaises(SimulatedCrash):
            ctl.cmd_start(self.cfg, s)
        s.crash_at = None
        ctl.STATE_FILE.unlink()
        s.fire_timer(self.cfg)
        self.assertTrue(self.is_ap(s), self.describe(s))

    def test_stop_kills_an_orphaned_daemon_that_has_no_pid_file_or_state(self):
        s = FakeSystem(connected=True)
        s._spawn("hostapd", f"hostapd -B -P /x {ctl.HOSTAPD_CONF}", True)
        s.managed, s.ip, s.client = False, "192.168.4.1/24", None
        self.assertIsNone(ctl.read_state())
        self.assertFalse(ctl.HOSTAPD_PID.exists())
        ctl.cmd_stop(self.cfg, s)
        self.assertTrue(self.is_client(s), self.describe(s))

    def test_manual_recover_with_no_evidence_either_way_leaves_a_healthy_ap_alone(self):
        s = self.live_ap()
        ctl.STATE_FILE.unlink()
        ctl.cmd_recover(self.cfg, s)         # operator ran it by hand: no --expect
        self.assertTrue(self.is_ap(s))

    def test_timer_carries_what_it_was_armed_for(self):
        s = self.live_ap()
        argv = s.timer["argv"]
        self.assertIn("--expect=test", argv)
        self.assertIn(f"--original={ORIGINAL}", argv)

    def test_awkward_profile_names_survive_the_command_line(self):
        for name in ("My iPhone", "-dash-first", "a b=c", "caf\u00e9"):
            args = ctl.build_parser().parse_args(
                ["recover", "--expect=test", f"--original={name}"])
            self.assertEqual(args.original, name)

    def test_expect_and_original_are_refused_on_other_commands(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ctl.main(["start", "--expect=test"])


class RescueTimerCommandTests(Base):
    """Review point 1, the parts checkable without systemd. The real lifecycle is checked on the
    Pi by tools/check_rescue_timer.py."""

    def test_timer_is_created_with_the_flags_the_lifecycle_depends_on(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        call = next(c for c in s.log if c[0] == "systemd-run")
        self.assertIn(f"--unit={ctl.RESCUE_UNIT}", call)
        self.assertIn("--timer-property=AccuracySec=1s", call)   # default accuracy is a whole minute
        self.assertIn("--collect", call)                          # a failed run must not hold the name
        self.assertIn("--on-active=90s", call)

    def test_old_timer_is_taken_down_before_a_new_one_is_created(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        names = [c[0] + " " + c[1] for c in s.log if c[0] in ("systemctl", "systemd-run")]
        self.assertLess(names.index("systemctl stop"), names.index("systemd-run --unit=ghostap-rescue"))

    def test_only_the_timer_is_ever_stopped_never_the_service_running_recover(self):
        s = FakeSystem(connected=True, decoy=True)
        ctl.cmd_test_start(self.cfg, s)
        s.fire_timer(self.cfg)
        ctl.cmd_test_start(self.cfg, s)
        ctl.cmd_test_commit(self.cfg, s)
        ctl.cmd_stop(self.cfg, s)
        stops = [c[2] for c in s.log if c[:2] == ("systemctl", "stop")]
        self.assertTrue(stops)
        self.assertTrue(all(x.endswith(".timer") for x in stops), stops)


class DryRunAndConfigTests(Base):
    def test_dry_run_changes_nothing(self):
        sh = ctl.Shell(dry_run=True)
        with mock.patch("subprocess.run") as run:
            for fn in (ctl.cmd_start, ctl.cmd_stop, ctl.cmd_recover, ctl.cmd_test_start):
                fn(self.cfg, sh)
            run.assert_not_called()
        self.assertFalse(ctl.RUN_DIR.exists())

    def write_config(self, extra=""):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        pw = d / "pw"
        pw.write_text("testpass123\n")
        pw.chmod(0o600)
        cfg = d / "ghostap.toml"
        cfg.write_text(f'''[network]
interface = "wlan0"
[access_point]
ssid = "ZeroFi"
passphrase_file = "{pw}"
[dhcp]
start = "192.168.4.10"
end = "192.168.4.50"
{extra}''')
        return cfg

    def test_new_settings_have_defaults(self):
        cfg = ctl.load_config(self.write_config())
        self.assertEqual((cfg.rescue_timeout, cfg.live_timeout), (90, 120))

    def test_reconnect_timeout_has_a_default_and_a_floor(self):
        self.assertEqual(ctl.load_config(self.write_config()).reconnect_timeout, 20)
        with self.assertRaises(ctl.GhostAPError):
            ctl.load_config(self.write_config("[recovery]\nreconnect_timeout = 1\n"))

    def test_rescue_timeout_must_outlast_a_normal_switch(self):
        with self.assertRaises(ctl.GhostAPError):
            ctl.load_config(self.write_config("[recovery]\nrescue_timeout = 10\n"))


class SnapshotTests(Base):
    """When an AP won't show up on a phone, the evidence is the radio's state at that
    moment. It is saved to disk, so it is still there after the reboot you do next."""

    def read(self):
        return ctl.SNAPSHOT_FILE.read_text()

    def test_an_ap_coming_up_saves_a_snapshot_to_disk(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        text = self.read()
        self.assertIn("[AP up]", text)
        self.assertIn("purpose=boot", text)
        for cmd in ("iw dev", "hostapd_cli -i wlan0 status", "pgrep -a hostapd",
                    "iw dev wlan0 station dump", "journalctl -k"):
            self.assertIn(f"--- {cmd}", text, cmd)

    def test_no_wifi_needed_means_no_snapshot(self):
        s = FakeSystem(connect_after=10)
        ctl.cmd_start(self.cfg, s)
        self.assertFalse(ctl.SNAPSHOT_FILE.exists())
        self.assertNotIn("ghostap-snapshot", s.timers)

    def test_snapshot_commands_are_read_only_and_never_count_as_changes(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        self.assertTrue(s.captures)
        self.assertFalse([c for c in s.log if c[0] in ("iw", "rfkill", "journalctl")],
                         "diagnostics must go through capture(), not run()")

    def test_a_follow_up_snapshot_is_scheduled_and_runs_later(self):
        s = FakeSystem()
        ctl.cmd_start(self.cfg, s)
        timer = s.timers["ghostap-snapshot"]
        self.assertEqual(timer["secs"], ctl.SNAPSHOT_DELAY)
        self.assertEqual(timer["argv"][2:], ["snapshot", f"+{ctl.SNAPSHOT_DELAY}s",
                                             "--config", str(self.cfg.path)])
        args = ctl.build_parser().parse_args(timer["argv"][2:])
        ctl.cmd_snapshot(self.cfg, s, *args.event_args)
        self.assertEqual(self.read().count("====="), 4)      # two headers, each has two sides
        self.assertIn(f"[+{ctl.SNAPSHOT_DELAY}s]", self.read())

    def test_manual_snapshot_uses_a_default_label(self):
        s = FakeSystem()
        ctl.cmd_snapshot(self.cfg, s)
        self.assertIn("[manual]", self.read())

    def test_a_broken_snapshot_never_stops_the_ap(self):
        s = FakeSystem()
        s.capture = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertEqual(ctl.cmd_start(self.cfg, s), 0)
        self.assertTrue(self.is_ap(s), self.describe(s))

    def test_an_unwritable_snapshot_folder_never_stops_the_ap(self):
        blocker = ctl.SNAPSHOT_FILE.parent.parent / "blocker"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("a file where a folder should be")
        with mock.patch.object(ctl, "SNAPSHOT_FILE", blocker / "ap-snapshots.log"):
            s = FakeSystem()
            self.assertEqual(ctl.cmd_start(self.cfg, s), 0)
        self.assertTrue(self.is_ap(s), self.describe(s))

    def test_failing_to_schedule_the_follow_up_never_stops_the_ap(self):
        s = FakeSystem()
        s.fail_snapshot_timer = True
        self.assertEqual(ctl.cmd_start(self.cfg, s), 0)
        self.assertTrue(self.is_ap(s), self.describe(s))
        self.assertIn("[AP up]", self.read())

    def test_failover_ap_gets_a_snapshot_too(self):
        cfg = dataclasses.replace(self.cfg, failover_grace=120, failover_join_timeout=30)
        s = FakeSystem(connected=True, connect_after=None)
        s.client = s.pending = None
        s.in_range = set()                     # the hotspot really is gone
        ctl.cmd_event(cfg, s, IFACE, "down")
        s.fire_failover(cfg)
        self.assertTrue(self.is_ap(s), self.describe(s))
        self.assertIn("[AP up]", self.read())

    def test_the_file_is_trimmed_instead_of_growing_forever(self):
        ctl.SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ctl.SNAPSHOT_FILE.write_text("x" * (ctl.SNAPSHOT_MAX_BYTES + 5000))
        ctl.cmd_snapshot(self.cfg, FakeSystem())
        self.assertLess(ctl.SNAPSHOT_FILE.stat().st_size, ctl.SNAPSHOT_MAX_BYTES)
        self.assertIn("[manual]", self.read())

    def test_dry_run_writes_nothing(self):
        ctl.cmd_snapshot(self.cfg, ctl.Shell(dry_run=True))
        self.assertFalse(ctl.SNAPSHOT_FILE.exists())

    def test_command_line_takes_zero_or_one_label(self):
        args = ctl.build_parser().parse_args(["snapshot"])
        self.assertEqual(args.event_args, [])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ctl.main(["snapshot", "one", "two"])


class SettingsTests(Base):
    """Nothing about the AP is fixed in code any more: every value is a setting with a
    default, and the defaults write exactly the files the Pi has always been given."""

    GOLDEN_HOSTAPD = 'interface=wlan0\ndriver=nl80211\nctrl_interface=/run/hostapd\nssid=ZeroFi\ncountry_code=US\nhw_mode=g\nchannel=6\nwpa=2\nwpa_passphrase=testpass123\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\nieee80211w=0\nauth_algs=1\n'
    GOLDEN_HOSTAPD_DEBUG = 'interface=wlan0\ndriver=nl80211\nctrl_interface=/run/hostapd\nssid=ZeroFi\ncountry_code=US\nhw_mode=g\nchannel=6\nwpa=2\nwpa_passphrase=testpass123\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\nieee80211w=0\nauth_algs=1\n# diagnostics: read with  journalctl -t hostapd\nlogger_syslog=-1\nlogger_syslog_level=0\n'
    GOLDEN_DNSMASQ = 'interface=wlan0\nbind-interfaces\nport=0\ndhcp-range=192.168.4.10,192.168.4.50,1h\ndhcp-leasefile=/run/ghostap/dnsmasq.leases\npid-file=/run/ghostap/dnsmasq.pid\n'

    @staticmethod
    @contextlib.contextmanager
    def logs_enabled():
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        try:
            yield
        finally:
            logging.disable(previous)

    def golden_dnsmasq(self):
        # the suite points RUN_DIR at a temp folder; on the Pi it is /run/ghostap
        return self.GOLDEN_DNSMASQ.replace("/run/ghostap", str(ctl.RUN_DIR))

    def cfg_with(self, **kw):
        return dataclasses.replace(self.cfg, **kw)

    def write_config(self, body=""):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pw").write_text("testpass123\n")
        os.chmod(d / "pw", 0o600)
        (d / "c.toml").write_text(
            f'[access_point]\nssid = "ZeroFi"\npassphrase_file = "{d / "pw"}"\n'
            '[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n' + body)
        return d / "c.toml"

    # ---- defaults change nothing -------------------------------------------
    def test_default_files_are_byte_for_byte_what_they_always_were(self):
        self.assertEqual(ctl.render_hostapd(self.cfg), self.GOLDEN_HOSTAPD)
        self.assertEqual(ctl.render_hostapd(self.cfg_with(hostapd_debug=True)),
                         self.GOLDEN_HOSTAPD_DEBUG)
        self.assertEqual(ctl.render_dnsmasq(self.cfg), self.golden_dnsmasq())

    def test_a_minimal_config_loads_every_default_from_the_settings_list(self):
        cfg = ctl.load_config(self.write_config())
        for s in ctl.SETTINGS:
            if s.required:
                continue
            want = ipaddress_if(s.default) if s.kind == "ipif" else s.default
            self.assertEqual(getattr(cfg, s.field), want, s.key)

    def test_loaded_defaults_render_the_original_files(self):
        cfg = ctl.load_config(self.write_config())
        self.assertEqual(ctl.render_hostapd(cfg).replace("testpass123", "testpass123"),
                         self.GOLDEN_HOSTAPD)
        self.assertEqual(ctl.render_dnsmasq(cfg), self.golden_dnsmasq())

    def test_the_network_section_is_optional(self):
        self.assertEqual(ctl.load_config(self.write_config()).interface, "wlan0")

    # ---- the settings list and the code agree ------------------------------
    def test_every_config_field_has_a_setting_and_the_reverse(self):
        fields = {f.name for f in dataclasses.fields(ctl.Config)} - {"path"}
        self.assertEqual(fields, {s.field for s in ctl.SETTINGS})

    def test_defaults_in_the_list_match_the_dataclass_defaults(self):
        defaults = {f.name: f.default for f in dataclasses.fields(ctl.Config)
                    if f.default is not dataclasses.MISSING}
        for s in ctl.SETTINGS:
            if s.field in defaults:
                self.assertEqual(defaults[s.field], s.default, s.key)

    def test_the_schema_command_prints_the_list_as_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ctl.main(["schema"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual([(d["section"], d["key"]) for d in data],
                         [(s.section, s.key) for s in ctl.SETTINGS])
        for d in data:
            self.assertTrue(d["description"], d["key"])
            self.assertIn(d["type"], ("str", "int", "bool", "ipif", "secret_file", "list"))

    def test_the_committed_example_config_is_the_generated_one(self):
        committed = (HERE.parent / "ghostap.toml.example").read_text()
        self.assertEqual(committed, ctl.render_example_config(),
                         "regenerate: python3 ghostap/controller.py example-config > ghostap.toml.example")

    def test_the_example_config_loads_and_documents_every_setting(self):
        text = ctl.render_example_config()
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pw").write_text("testpass123")
        os.chmod(d / "pw", 0o600)
        (d / "c.toml").write_text(text.replace("/etc/ghostap/passphrase", str(d / "pw")))
        ctl.load_config(d / "c.toml")
        for s in ctl.SETTINGS:
            self.assertIn(f"{s.key} = ", text, s.key)

    # ---- the optional hostapd lines ----------------------------------------
    def test_optional_hostapd_lines_appear_only_when_set(self):
        out = ctl.render_hostapd(self.cfg_with(
            ieee80211n=True, wmm_enabled=False, ht_capab="[SHORT-GI-20]",
            ignore_broadcast_ssid=True, max_num_sta=8, beacon_int=100)).splitlines()
        for line in ("ieee80211n=1", "wmm_enabled=0", "ht_capab=[SHORT-GI-20]",
                     "ignore_broadcast_ssid=1", "max_num_sta=8", "beacon_int=100"):
            self.assertIn(line, out)
        self.assertLess(out.index("auth_algs=1"), out.index("ieee80211n=1"))

    def test_the_security_settings_reach_the_file(self):
        out = ctl.render_hostapd(self.cfg_with(
            wpa=3, wpa_key_mgmt="WPA-PSK SAE", rsn_pairwise="CCMP GCMP",
            ieee80211w=2, auth_algs=3, hw_mode="a", channel=36)).splitlines()
        for line in ("wpa=3", "wpa_key_mgmt=WPA-PSK SAE", "rsn_pairwise=CCMP GCMP",
                     "ieee80211w=2", "auth_algs=3", "hw_mode=a", "channel=36"):
            self.assertIn(line, out)

    # ---- limits ---------------------------------------------------------------
    def test_channels_follow_the_band(self):
        ok = [dict(hw_mode="g", channel=14), dict(hw_mode="a", channel=36),
              dict(hw_mode="a", channel=165), dict(hw_mode="g", channel=1)]
        bad = [dict(hw_mode="g", channel=15), dict(hw_mode="g", channel=0),
               dict(hw_mode="a", channel=6), dict(hw_mode="a", channel=178)]
        for kw in ok:
            ctl.validate(self.cfg_with(**kw))
        for kw in bad:
            with self.assertRaises(ctl.GhostAPError, msg=str(kw)):
                ctl.validate(self.cfg_with(**kw))

    def test_values_outside_the_allowed_choices_are_refused(self):
        for kw in (dict(hw_mode="x"), dict(wpa=4), dict(ieee80211w=3), dict(auth_algs=0),
                   dict(rsn_pairwise="CCMP FOO"), dict(wpa_key_mgmt=""), dict(max_num_sta=-1),
                   dict(dns_port=70000), dict(snapshot_delay=1)):
            with self.assertRaises(ctl.GhostAPError, msg=str(kw)):
                ctl.validate(self.cfg_with(**kw))

    def test_the_old_numeric_limits_still_hold(self):
        for kw in (dict(rescue_timeout=29), dict(live_timeout=29), dict(boot_timeout=-1),
                   dict(failover_grace=14), dict(failover_join_timeout=9),
                   dict(reconnect_timeout=4)):
            with self.assertRaises(ctl.GhostAPError, msg=str(kw)):
                ctl.validate(self.cfg_with(**kw))
        for kw in (dict(failover_grace=0), dict(failover_grace=15), dict(boot_timeout=0)):
            ctl.validate(self.cfg_with(**kw))

    # ---- the raw lines (the escape hatch) -------------------------------------
    def test_extra_lines_are_written_untouched_at_the_end(self):
        out = ctl.render_hostapd(self.cfg_with(
            hostapd_debug=True, extra_hostapd=("max_listen_interval=100", "# note")))
        self.assertTrue(out.rstrip("\n").endswith("max_listen_interval=100\n# note".rstrip()))
        self.assertLess(out.index("logger_syslog=-1"), out.index("max_listen_interval=100"))
        out = ctl.render_dnsmasq(self.cfg_with(
            extra_dnsmasq=("dhcp-host=aa:bb:cc:dd:ee:ff,192.168.4.20",)))
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:ff,192.168.4.20", out.splitlines())

    def test_raw_lines_may_not_break_out_of_their_line(self):
        for bad in ("a=1\nssid=evil", "a=1\rb=2", "a=1\0", "x" * 600 + "=1"):
            for field in ("extra_hostapd", "extra_dnsmasq"):
                with self.assertRaises(ctl.GhostAPError, msg=f"{field} {bad[:20]!r}"):
                    ctl.validate(self.cfg_with(**{field: (bad,)}))
        for bad in ("", "   ", "not a config line", "=value"):
            with self.assertRaises(ctl.GhostAPError, msg=repr(bad)):
                ctl.validate(self.cfg_with(extra_hostapd=(bad,)))

    def test_raw_lines_cannot_touch_what_ghostap_supervises(self):
        for key in ("interface=wlan1", "ctrl_interface=/tmp/x", "driver=wired"):
            with self.assertRaises(ctl.GhostAPError, msg=key):
                ctl.validate(self.cfg_with(extra_hostapd=(key,)))
        for key in ("interface=eth0", "pid-file=/tmp/x", "dhcp-leasefile=/tmp/y", "bind-interfaces"):
            with self.assertRaises(ctl.GhostAPError, msg=key):
                ctl.validate(self.cfg_with(extra_dnsmasq=(key,)))

    def test_raw_lines_point_to_the_named_setting_when_there_is_one(self):
        with self.assertRaises(ctl.GhostAPError) as cm:
            ctl.validate(self.cfg_with(extra_hostapd=("channel=1",)))
        self.assertIn("channel", str(cm.exception))
        with self.assertRaises(ctl.GhostAPError) as cm:
            ctl.validate(self.cfg_with(extra_dnsmasq=("port=53",)))
        self.assertIn("dns_port", str(cm.exception))

    # ---- DHCP ------------------------------------------------------------------
    def test_dhcp_options(self):
        self.assertIn("dhcp-option=3", ctl.render_dnsmasq(self.cfg_with(router="none")).splitlines())
        self.assertIn("dhcp-option=3,192.168.4.9",
                      ctl.render_dnsmasq(self.cfg_with(router="192.168.4.9")).splitlines())
        self.assertIn("dhcp-option=6,1.1.1.1,9.9.9.9",
                      ctl.render_dnsmasq(self.cfg_with(dns_servers=("1.1.1.1", "9.9.9.9"))).splitlines())
        self.assertIn("port=53", ctl.render_dnsmasq(self.cfg_with(dns_port=53)).splitlines())
        for kw in (dict(router="nope"), dict(dns_servers=("1.1.1",))):
            with self.assertRaises((ctl.GhostAPError, ValueError), msg=str(kw)):
                ctl.validate(self.cfg_with(**kw))

    # ---- loading ----------------------------------------------------------------
    def test_every_new_setting_can_be_set_from_the_file(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pw").write_text("testpass123")
        os.chmod(d / "pw", 0o600)
        (d / "c.toml").write_text(f"""[access_point]
ssid = "Other"
passphrase_file = "{d / 'pw'}"
hw_mode = "g"
channel = 11
wpa_key_mgmt = "WPA-PSK WPA-PSK-SHA256"
ieee80211n = true
extra_hostapd = ["max_listen_interval=100"]
[dhcp]
start = "192.168.4.10"
end = "192.168.4.50"
router = "none"
dns_servers = ["1.1.1.1"]
extra_dnsmasq = ["dhcp-host=aa:bb:cc:dd:ee:ff,192.168.4.20"]
[diagnostics]
snapshots = false
snapshot_delay = 30
""")
        cfg = ctl.load_config(d / "c.toml")
        self.assertEqual((cfg.ssid, cfg.channel, cfg.ieee80211n, cfg.router, cfg.snapshots,
                          cfg.snapshot_delay), ("Other", 11, True, "none", False, 30))
        self.assertEqual(cfg.extra_hostapd, ("max_listen_interval=100",))
        self.assertIn("ieee80211n=1", ctl.render_hostapd(cfg))

    def test_typos_in_the_config_are_called_out_not_silently_ignored(self):
        cfg_path = self.write_config('[recovery]\nrescue_timout = 90\n[nonsense]\nx = 1\n')
        with self.logs_enabled(), self.assertLogs(ctl.log, level="WARNING") as cm:
            ctl.load_config(cfg_path)
        text = "\n".join(cm.output)
        self.assertIn("rescue_timout", text)
        self.assertIn("[nonsense]", text)

    def test_a_missing_required_setting_names_it(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "c.toml").write_text('[access_point]\nssid = "x"\n[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n')
        with self.assertRaises(ctl.GhostAPError) as cm:
            ctl.load_config(d / "c.toml")
        self.assertIn("passphrase_file", str(cm.exception))

    def test_wrong_types_are_refused_with_a_clear_message(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pw").write_text("testpass123")
        os.chmod(d / "pw", 0o600)
        cases = (
            ('hostapd_debug = "yes"', "", "hostapd_debug"),
            ('channel = "six"', "", "channel"),
            ('extra_hostapd = "a=1"', "", "extra_hostapd"),
            ("", "[recovery]\nrescue_timeout = true", "rescue_timeout"),
        )
        for ap_line, tail, word in cases:
            (d / "c.toml").write_text(
                f'[access_point]\nssid = "ZeroFi"\npassphrase_file = "{d / "pw"}"\n{ap_line}\n'
                f'[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n{tail}\n')
            with self.assertRaises(ctl.GhostAPError, msg=word) as cm:
                ctl.load_config(d / "c.toml")
            self.assertIn(word, str(cm.exception))

    def test_country_must_be_a_two_character_code(self):
        def load(country):
            path = self.write_config()
            path.write_text(path.read_text().replace(
                'ssid = "ZeroFi"', f'ssid = "ZeroFi"\ncountry = "{country}"'))
            return ctl.load_config(path)

        for good in ("US", "gb", "00"):
            self.assertEqual(load(good).country, good)
        for bad in ("USA", "U", "", "U S", "\u00dcS"):
            with self.assertRaises(ctl.GhostAPError, msg=repr(bad)) as cm:
                load(bad)
            self.assertIn("country", str(cm.exception))

    def test_weak_security_is_allowed_but_flagged(self):
        cfg_path = self.write_config('')
        cfg = ctl.load_config(cfg_path)
        with self.logs_enabled(), self.assertLogs(ctl.log, level="WARNING") as cm:
            ctl._warn_if_weak(dataclasses.replace(cfg, rsn_pairwise="CCMP TKIP"))
        self.assertIn("weak", "\n".join(cm.output))

    # ---- diagnostics follow the settings ----------------------------------------
    def test_snapshots_can_be_turned_off(self):
        cfg = self.cfg_with(snapshots=False)
        s = FakeSystem()
        ctl.cmd_start(cfg, s)
        self.assertTrue(self.is_ap(s), self.describe(s))
        self.assertFalse(ctl.SNAPSHOT_FILE.exists())
        self.assertNotIn("ghostap-snapshot", s.timers)

    def test_the_follow_up_snapshot_delay_is_a_setting(self):
        cfg = self.cfg_with(snapshot_delay=30)
        s = FakeSystem()
        ctl.cmd_start(cfg, s)
        timer = s.timers["ghostap-snapshot"]
        self.assertEqual(timer["secs"], 30)
        self.assertEqual(timer["argv"][3], "+30s")


IW_LIST_ZERO_W = (
    "Wiphy phy0\n\tmax # scan SSIDs: 10\n\tSupported interface modes:\n\t\t * IBSS\n"
    "\t\t * managed\n\t\t * AP\n\t\t * P2P-client\n\t\t * P2P-GO\n\t\t * P2P-device\n"
    "\tBand 1:\n\t\tCapabilities: 0x1062\n")
IW_LIST_NO_AP = IW_LIST_ZERO_W.replace("\t\t * AP\n", "")


class FakeHost(ctl.Shell):
    """A made-up machine for the preflight to look at: files, commands and installed tools
    are whatever the test says. Anything not listed behaves like 'not there'."""

    def __init__(self, files=None, cmds=None, tools=()):
        super().__init__(dry_run=False)
        self.files, self.cmds, self.tools = files or {}, cmds or {}, set(tools)

    def probe(self, *cmd):
        return self.cmds.get(cmd, (127, ""))

    def read_text(self, path):
        return self.files.get(path)

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None


def pi_host(*, stack="NetworkManager", iw_list=IW_LIST_ZERO_W, country="US",
            iface="wlan0", systemd=True, tools=None, hostapd=("inactive", "masked"),
            dnsmasq=("inactive", "disabled"), wifi=("Home",), hotspots=(), model=None,
            reg_works=True):
    """A healthy Raspberry Pi Zero W on Bookworm, unless a test breaks something."""
    c = {}
    if systemd:
        c[("test", "-d", "/run/systemd/system")] = (0, "")
    for svc in ("NetworkManager", "dhcpcd", "systemd-networkd", "wpa_supplicant"):
        c[("systemctl", "is-active", svc)] = (0 if svc == stack else 3,
                                              "active" if svc == stack else "inactive")
    c[("systemctl", "is-active", "wpa_supplicant")] = (0, "active")   # NM runs it too
    for svc, (act, en) in (("hostapd", hostapd), ("dnsmasq", dnsmasq)):
        c[("systemctl", "is-active", svc)] = (0 if act == "active" else 3, act)
        c[("systemctl", "is-enabled", svc)] = (0, en)
    if iface:
        c[("ip", "link", "show", "wlan0")] = (0, "")
    if iw_list is not None:
        c[("iw", "list")] = (0, iw_list)
    if reg_works:
        c[("iw", "reg", "get")] = (0, f"global\ncountry {country}: DFS-FCC\n")
    lines = [f"{n}:802-11-wireless" for n in (*wifi, *hotspots)] + ["lo:loopback"]
    c[("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")] = (0, "\n".join(lines))
    for n in wifi:
        c[("nmcli", "-g", "802-11-wireless.mode", "connection", "show", n)] = (0, "infrastructure")
    for n in hotspots:
        c[("nmcli", "-g", "802-11-wireless.mode", "connection", "show", n)] = (0, "ap")
    files = {"/proc/device-tree/model": (model or "Raspberry Pi Zero W Rev 1.1") + "\0",
             "/etc/os-release": 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'}
    t = tools if tools is not None else ("hostapd", "dnsmasq", "iw", "nmcli", "systemd-run", "ip")
    return FakeHost(files, c, t)


class PreflightTests(unittest.TestCase):
    def check(self, host):
        facts = ctl.gather_facts(host)
        return facts, ctl.judge(facts)

    def titles(self, findings, level=None):
        return " | ".join(x.title for x in findings if level in (None, x.level))

    # ---- the healthy machine ----------------------------------------------
    def test_a_healthy_pi_zero_w_passes_with_only_notes(self):
        facts, found = self.check(pi_host())
        self.assertEqual(ctl.verdict(found), ctl.OK)
        self.assertEqual({x.level for x in found}, {ctl.INFO})
        self.assertIn("Pi Zero", self.titles(found))
        self.assertEqual((facts.model, facts.os_name),
                         ("Raspberry Pi Zero W Rev 1.1", "Debian GNU/Linux 12 (bookworm)"))
        self.assertTrue(facts.ap_mode)
        self.assertEqual((facts.wifi_profiles, facts.ap_profiles, facts.reg_country),
                         (1, (), "US"))

    # ---- things that stop it ------------------------------------------------
    def test_dhcpcd_machine_is_blocked_and_says_why(self):
        _, found = self.check(pi_host(stack="dhcpcd"))
        self.assertEqual(ctl.verdict(found), ctl.BLOCK)
        self.assertIn("dhcpcd", self.titles(found, ctl.BLOCK))

    def test_systemd_networkd_machine_is_blocked(self):
        _, found = self.check(pi_host(stack="systemd-networkd"))
        self.assertEqual(ctl.verdict(found), ctl.BLOCK)
        self.assertIn("systemd-networkd", self.titles(found, ctl.BLOCK))

    def test_no_network_manager_at_all_is_blocked(self):
        _, found = self.check(pi_host(stack="none"))
        self.assertEqual(ctl.verdict(found), ctl.BLOCK)
        self.assertIn("NetworkManager isn't running", self.titles(found, ctl.BLOCK))

    def test_wpa_supplicant_alone_is_not_mistaken_for_another_stack(self):
        # NetworkManager runs wpa_supplicant itself, so seeing it is normal
        _, found = self.check(pi_host())
        self.assertNotIn("dhcpcd", self.titles(found))

    def test_no_systemd_is_blocked(self):
        _, found = self.check(pi_host(systemd=False))
        self.assertIn("systemd", self.titles(found, ctl.BLOCK))

    def test_missing_tools_are_blocked_with_the_install_command(self):
        _, found = self.check(pi_host(tools=("iw", "nmcli", "systemd-run", "ip")))
        titles = self.titles(found, ctl.BLOCK)
        self.assertIn("hostapd isn't installed", titles)
        self.assertIn("dnsmasq isn't installed", titles)
        fixes = " ".join(x.fix for x in found)
        self.assertIn("apt install hostapd", fixes)
        self.assertIn("apt install dnsmasq-base", fixes)

    def test_wifi_chip_without_ap_mode_is_blocked(self):
        _, found = self.check(pi_host(iw_list=IW_LIST_NO_AP))
        self.assertIn("can't run as an access point", self.titles(found, ctl.BLOCK))

    def test_missing_wifi_interface_is_blocked(self):
        _, found = self.check(pi_host(iface=None))
        self.assertIn("No WiFi interface named wlan0", self.titles(found, ctl.BLOCK))

    # ---- things worth a warning ------------------------------------------
    def test_unanswered_ap_question_is_a_warning_not_a_block(self):
        facts, found = self.check(pi_host(iw_list=None))
        self.assertIsNone(facts.ap_mode)
        self.assertEqual(ctl.verdict(found), ctl.WARN)
        self.assertIn("Couldn't confirm", self.titles(found, ctl.WARN))

    def test_unset_country_warns_and_names_the_fix(self):
        for country in ("00", None):
            host = pi_host(country=country or "US", reg_works=country is not None)
            _, found = self.check(host)
            self.assertEqual(ctl.verdict(found), ctl.WARN, country)
            self.assertIn("country", self.titles(found, ctl.WARN))
        self.assertIn("raspi-config", " ".join(x.fix for x in found))

    def test_system_hostapd_or_dnsmasq_service_warns(self):
        for kw in ({"hostapd": ("active", "enabled")}, {"hostapd": ("inactive", "enabled")},
                   {"dnsmasq": ("active", "enabled")}, {"dnsmasq": ("inactive", "enabled")}):
            _, found = self.check(pi_host(**kw))
            self.assertEqual(ctl.verdict(found), ctl.WARN, kw)
            self.assertIn(list(kw)[0], self.titles(found, ctl.WARN))

    def test_masked_or_disabled_services_are_fine(self):
        _, found = self.check(pi_host(hostapd=("inactive", "masked"),
                                      dnsmasq=("inactive", "disabled")))
        self.assertEqual(ctl.verdict(found), ctl.OK)

    def test_leftover_hotspot_profile_warns_and_is_named(self):
        facts, found = self.check(pi_host(hotspots=("ZeroFi-Hotspot",)))
        self.assertEqual(facts.ap_profiles, ("ZeroFi-Hotspot",))
        self.assertEqual(facts.wifi_profiles, 1)         # the hotspot isn't a client network
        self.assertEqual(ctl.verdict(found), ctl.WARN)
        self.assertIn("ZeroFi-Hotspot", self.titles(found, ctl.WARN))

    def test_no_saved_wifi_is_just_a_note(self):
        _, found = self.check(pi_host(wifi=()))
        self.assertEqual(ctl.verdict(found), ctl.OK)
        self.assertIn("No saved WiFi", self.titles(found, ctl.INFO))

    def test_profile_names_with_colons_are_read_correctly(self):
        host = pi_host(wifi=(), hotspots=())
        host.cmds[("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")] = (
            0, "Cafe\\: Guest:802-11-wireless")
        host.cmds[("nmcli", "-g", "802-11-wireless.mode", "connection", "show",
                   "Cafe: Guest")] = (0, "ap")
        facts, _ = self.check(host)
        self.assertEqual(facts.ap_profiles, ("Cafe: Guest",))

    def test_other_boards_get_a_note_not_a_block(self):
        _, found = self.check(pi_host(model="Orange Pi 5"))
        self.assertEqual(ctl.verdict(found), ctl.OK)
        self.assertIn("doesn't look like a Raspberry Pi", self.titles(found, ctl.INFO))

    def test_a_pi_4_gets_no_zero_note(self):
        _, found = self.check(pi_host(model="Raspberry Pi 4 Model B Rev 1.5"))
        self.assertNotIn("Zero", self.titles(found))

    # ---- parsing and output --------------------------------------------------
    def test_ap_mode_parser(self):
        self.assertIs(ctl._ap_supported(IW_LIST_ZERO_W), True)
        self.assertIs(ctl._ap_supported(IW_LIST_NO_AP), False)
        self.assertIsNone(ctl._ap_supported("nothing useful"))
        # 'AP/VLAN' and 'P2P-GO' are other modes, not plain AP
        self.assertIs(ctl._ap_supported(
            "\tSupported interface modes:\n\t\t * AP/VLAN\n\t\t * P2P-GO\n"), False)

    def test_verdict_is_the_worst_finding(self):
        F = ctl.Finding
        self.assertEqual(ctl.verdict([]), ctl.OK)
        self.assertEqual(ctl.verdict([F(ctl.INFO, "a")]), ctl.OK)
        self.assertEqual(ctl.verdict([F(ctl.INFO, "a"), F(ctl.WARN, "b")]), ctl.WARN)
        self.assertEqual(ctl.verdict([F(ctl.WARN, "b"), F(ctl.BLOCK, "c")]), ctl.BLOCK)

    def test_report_puts_blockers_first_and_includes_fixes_and_the_caveat(self):
        host = pi_host(iw_list=IW_LIST_NO_AP, hotspots=("ZeroFi-Hotspot",))
        facts, found = self.check(host)
        text = ctl.render_preflight(facts, found)
        self.assertLess(text.index("[X]"), text.index("[!]"))
        self.assertLess(text.index("[!]"), text.index("[i]"))
        self.assertIn("Fix:", text)
        self.assertIn("Not supported on this machine", text)
        self.assertIn("not a guarantee", text)

    def test_exit_code_is_nonzero_only_for_blockers(self):
        cfg = types.SimpleNamespace(interface="wlan0")
        for host, code in ((pi_host(), 0), (pi_host(country="00"), 0),
                           (pi_host(stack="dhcpcd"), 1)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ctl.cmd_preflight(cfg, host), code)

    def test_preflight_runs_on_a_fresh_machine_with_no_config_and_no_root(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ctl.main(["preflight", "-c", "/nonexistent/ghostap.toml"])
        self.assertIn(rc, (0, 1))
        self.assertIn("Checked:", out.getvalue())


class ReadOnlyCommandTests(unittest.TestCase):
    """`status` only looks, so it must not need the password file (or root to read it)."""

    def conf(self, pw_exists=True):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        if pw_exists:
            (d / "pw").write_text("testpass123\n")
            os.chmod(d / "pw", 0o000)          # like root-only: this user can't read it
        (d / "c.toml").write_text(
            f'[access_point]\nssid = "ZeroFi"\npassphrase_file = "{d / "pw"}"\n'
            '[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n')
        return d / "c.toml"

    def test_loading_without_secrets_skips_the_password_file(self):
        for exists in (True, False):
            cfg = ctl.load_config(self.conf(exists), secrets=False)
            self.assertEqual(cfg.ssid, "ZeroFi")
            self.assertEqual(cfg.passphrase, ctl._UNREAD_SECRET)

    def test_commands_that_build_the_ap_still_need_the_password(self):
        with self.assertRaises(ctl.GhostAPError) as cm:
            ctl.load_config(self.conf(pw_exists=False))
        self.assertIn("passphrase", str(cm.exception))

    def test_a_config_loaded_without_its_password_can_never_make_an_ap(self):
        cfg = ctl.load_config(self.conf(), secrets=False)
        with self.assertRaises(ctl.GhostAPError):
            ctl.render_hostapd(cfg)

    def test_only_looking_commands_are_read_only(self):
        self.assertEqual(set(ctl.READ_ONLY), {"status", "preflight", "gui"})
        for cmd in ("start", "stop", "recover", "test", "failover", "event"):
            self.assertNotIn(cmd, ctl.READ_ONLY)

    def test_status_works_end_to_end_without_the_password(self):
        path = self.conf()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.dict(ctl.COMMANDS, {"status": mock.Mock(return_value=0)}):
            m = ctl.COMMANDS["status"]
            rc = ctl.main(["status", "-c", str(path)])
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_args.args[0].passphrase, ctl._UNREAD_SECRET)


class StubShell(ctl.Shell):
    """Answers read-only commands from a table, so status can be checked in every mode."""

    def __init__(self, answers=None, **kw):
        super().__init__(dry_run=False, **kw)
        self.answers, self.seen = answers or {}, []

    def run(self, *cmd, check=True):
        self.seen.append(cmd)
        out = ""
        for prefix, text in self.answers.items():
            if cmd[:len(prefix)] == prefix:
                out = text
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def pid_alive(self, pid):
        return pid in self.alive

    alive = ()


class AdminPageControllerTests(Base):
    """The controller's side of the admin page: settings, status as data, the commands."""

    def write_config(self, body=""):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pw").write_text("testpass123\n")
        os.chmod(d / "pw", 0o600)
        (d / "c.toml").write_text(
            f'[access_point]\nssid = "ZeroFi"\npassphrase_file = "{d / "pw"}"\n'
            '[dhcp]\nstart = "192.168.4.10"\nend = "192.168.4.50"\n' + body)
        return d / "c.toml"

    # ---- settings ----
    def test_the_page_is_off_by_default_with_sensible_defaults(self):
        cfg = ctl.load_config(self.write_config())
        self.assertEqual((cfg.gui_enabled, cfg.gui_port, cfg.gui_bind), (False, 8480, "0.0.0.0"))

    def test_the_page_settings_load(self):
        cfg = ctl.load_config(self.write_config(
            '[gui]\nenabled = true\nport = 9000\nbind = "127.0.0.1"\n'))
        self.assertEqual((cfg.gui_enabled, cfg.gui_port, cfg.gui_bind), (True, 9000, "127.0.0.1"))

    def test_bad_port_and_bind_are_refused(self):
        for body, word in (("port = 80", "port"), ("port = 70000", "port"),
                           ("port = 1023", "port"), ('bind = "localhost"', "bind"),
                           ('bind = "256.1.1.1"', "bind"), ('bind = ""', "bind"),
                           ('bind = "all"', "bind")):
            with self.assertRaises(ctl.GhostAPError, msg=body) as cm:
                ctl.load_config(self.write_config(f"[gui]\n{body}\n"))
            self.assertIn(word, str(cm.exception))
        for ok in ('bind = "::1"', 'bind = "::"', "port = 1024", "port = 65535"):
            ctl.load_config(self.write_config(f"[gui]\n{ok}\n"))

    def test_the_settings_show_up_in_the_schema_and_example_config(self):
        keys = {(x["section"], x["key"]) for x in ctl.settings_schema()}
        self.assertTrue({("gui", "enabled"), ("gui", "port"), ("gui", "bind")} <= keys)
        self.assertIn("[gui]", ctl.render_example_config())

    # ---- status as data ----
    def status(self, *, alive=(), connected=False, state=None, rescue=False, failover=False,
               cfg=None, extra=None):
        answers = {("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"):
                   "wlan0:wifi:connected\n" if connected else "wlan0:wifi:disconnected\n",
                   ("systemctl", "is-active", f"{ctl.RESCUE_UNIT}.timer"):
                   "active\n" if rescue else "inactive\n",
                   ("systemctl", "is-active", f"{ctl.FAILOVER_UNIT}.timer"):
                   "active\n" if failover else "inactive\n"}
        answers.update(extra or {})
        sh = StubShell(answers)
        sh.alive = set(alive)
        for leftover in (ctl.HOSTAPD_PID, ctl.STATE_FILE):       # every call starts clean
            leftover.unlink(missing_ok=True)
        if alive:
            ctl.RUN_DIR.mkdir(parents=True, exist_ok=True)
            ctl.HOSTAPD_PID.write_text("4242\n")
        if state is not None:
            ctl.RUN_DIR.mkdir(parents=True, exist_ok=True)
            ctl.STATE_FILE.write_text(json.dumps(state))
        return sh, ctl.collect_status(cfg or self.cfg, sh)

    def test_status_data_in_each_mode(self):
        self.assertEqual(self.status(connected=True)[1]["mode"], "client")
        self.assertEqual(self.status()[1]["mode"], "disconnected")
        sh, st = self.status(alive={4242}, connected=True)
        self.assertEqual((st["mode"], st["ssid"], st["gateway"]), ("ap", "ZeroFi", "192.168.4.1"))

    def test_status_data_shows_a_live_test_and_the_rescue_timer(self):
        sh, st = self.status(connected=True, rescue=True, state={
            "purpose": "test", "committed": False, "deadline": sh_now() + 50})
        self.assertTrue(st["rescue_armed"])
        self.assertAlmostEqual(st["live_test"]["seconds_left"], 50, delta=3)
        self.assertIsNone(self.status(connected=True)[1]["live_test"])
        self.assertIsNone(self.status(connected=True, state={
            "purpose": "test", "committed": True, "deadline": 1})[1]["live_test"])

    def test_status_data_failover(self):
        import dataclasses
        cfg = dataclasses.replace(self.cfg, failover_grace=120)
        fo = self.status(cfg=cfg, failover=True)[1]["failover"]
        self.assertEqual(fo, {"enabled": True, "grace": 120, "countdown_running": True})
        fo = self.status(cfg=cfg)[1]["failover"]
        self.assertFalse(fo["countdown_running"])
        fo = self.status()[1]["failover"]
        self.assertEqual((fo["enabled"], fo["countdown_running"]), (False, False))

    def test_the_page_status_adds_address_wifi_name_and_hostname(self):
        answers = {("ip", "-4", "-o", "addr", "show", "dev", "wlan0"):
                   "3: wlan0    inet 192.168.1.50/24 brd 192.168.1.255 scope global wlan0\n"
                   "3: wlan0    inet 10.0.0.9/8 scope global secondary wlan0\n",
                   ("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", "wlan0"): "crashbandiclute\n"}
        sh, _ = self.status(connected=True, extra=answers)
        st = ctl.collect_gui_status(self.cfg, sh)
        self.assertEqual(st["addresses"], ["192.168.1.50", "10.0.0.9"])
        self.assertEqual(st["wifi"], "crashbandiclute")
        self.assertEqual((st["interface"], st["gui_port"]), ("wlan0", 8480))
        self.assertTrue(st["hostname"])
        json.dumps(st)                              # must be sendable as JSON

    def test_the_page_status_has_no_wifi_name_when_not_on_wifi(self):
        sh, _ = self.status()
        self.assertIsNone(ctl.collect_gui_status(self.cfg, sh)["wifi"])

    def test_the_page_status_says_whether_the_apply_helper_is_there(self):
        for active, expected in (("active\n", True), ("inactive\n", False), ("", False)):
            sh, _ = self.status(extra={("systemctl", "is-active", "ghostap-apply.path"): active})
            st = ctl.collect_gui_status(self.cfg, sh)
            self.assertIs(st["apply"]["available"], expected, active)

    def test_the_page_status_carries_a_pending_request_and_the_last_result(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "request").write_text("{}")
        (d / "result").write_text(json.dumps({"action": "test", "ok": False,
                                              "message": "nope", "at": 5, "extra": "x"}))
        sh, _ = self.status()
        with mock.patch.object(ctl, "APPLY_REQUEST", d / "request"), \
                mock.patch.object(ctl, "APPLY_RESULT", d / "result"):
            st = ctl.collect_gui_status(self.cfg, sh)
        self.assertTrue(st["apply"]["pending"])
        self.assertEqual(st["apply"]["last"],
                         {"action": "test", "ok": False, "message": "nope", "at": 5})
        json.dumps(st)

    def test_the_page_status_never_contains_the_password(self):
        sh, _ = self.status(connected=True)
        self.assertNotIn(self.cfg.passphrase, json.dumps(ctl.collect_gui_status(self.cfg, sh)))

    def test_status_text_mentions_the_page_only_when_it_is_on(self):
        import dataclasses
        for enabled in (False, True):
            cfg = dataclasses.replace(self.cfg, gui_enabled=enabled)
            sh, _ = self.status(connected=True, cfg=cfg)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ctl.cmd_status(cfg, sh)
            self.assertEqual("admin page:" in out.getvalue(), enabled)
            if enabled:
                self.assertIn(":8480", out.getvalue())

    def test_a_quiet_shell_does_not_log_each_command(self):
        with SettingsTests.logs_enabled():
            with self.assertLogs(ctl.log, "INFO") as normal:         # control: it DOES log
                ctl.Shell().run("true", check=False)
            self.assertTrue(normal.records)
            with self.assertNoLogs(ctl.log, "INFO"):
                ctl.Shell(quiet=True).run("true", check=False)

    # ---- the commands ----
    def gui_env(self, *, password=True, enabled=True):
        import dataclasses
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        gui = ctl._gui_module()
        if password:
            gui.write_password_file(d / "gui-password", "adminpass123")
        p = mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(d)})
        p.start()
        self.addCleanup(p.stop)
        return gui, dataclasses.replace(self.cfg, gui_enabled=enabled, gui_port=9123,
                                        gui_bind="127.0.0.1")

    def test_gui_command_starts_the_server_with_the_configured_address(self):
        gui, cfg = self.gui_env()
        seen = {}

        class FakeServer:
            def serve_forever(self):
                raise ctl.Interrupted("stop")        # what systemd's stop signal does

            def server_close(self):
                seen["closed"] = True

        def fake_make(provider, stored, host, port, settings=None, applier=None, logs=None):
            seen.update(provider=provider, stored=stored, host=host, port=port,
                        settings=settings, applier=applier, logs=logs)
            return FakeServer()
        with mock.patch.object(gui, "make_server", fake_make):
            self.assertEqual(ctl.cmd_gui(cfg, StubShell()), 0)
        self.assertEqual((seen["host"], seen["port"]), ("127.0.0.1", 9123))
        self.assertTrue(gui.verify_password("adminpass123", seen["stored"]))
        self.assertTrue(seen["closed"])
        self.assertIsInstance(seen["settings"], ctl.SettingsStore)
        self.assertEqual(seen["settings"].path, cfg.path)
        self.assertIsInstance(seen["applier"], ctl.Applier)
        self.assertIsInstance(seen["logs"], ctl.LogReader)
        info = seen["provider"]()
        self.assertEqual(info["mode"], "disconnected")
        self.assertNotIn("testpass123", json.dumps(info))
        self.assertIs(info["code_changed"], False)
        # `git pull` replaced the program while the page kept running the old one
        with mock.patch.object(ctl, "_code_fingerprint", return_value=("new",)):
            self.assertIs(seen["provider"]()["code_changed"], True)

    def test_the_code_fingerprint_changes_when_a_program_file_is_replaced(self):
        before = ctl._code_fingerprint()
        self.assertEqual(before, ctl._code_fingerprint())
        self.assertEqual(len(before), 2)
        self.assertNotIn(None, before)
        real = pathlib.Path.stat

        def newer(self_path, *a, **k):
            st = real(self_path, *a, **k)
            if self_path.name == "gui.py":
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid,
                                       st.st_gid, st.st_size + 1, st.st_atime, st.st_mtime,
                                       st.st_ctime))
            return st
        with mock.patch.object(pathlib.Path, "stat", newer):
            self.assertNotEqual(ctl._code_fingerprint(), before)

    def test_a_missing_program_file_does_not_crash_the_status(self):
        with mock.patch.object(pathlib.Path, "stat", side_effect=OSError):
            self.assertEqual(ctl._code_fingerprint(), (None, None))

    def test_gui_command_refuses_when_off_or_without_a_password(self):
        gui, cfg = self.gui_env(enabled=False)
        with mock.patch.object(gui, "make_server") as m:
            self.assertEqual(ctl.cmd_gui(cfg, StubShell()), 1)
            m.assert_not_called()
        gui, cfg = self.gui_env(password=False)
        with mock.patch.object(gui, "make_server") as m:
            self.assertEqual(ctl.cmd_gui(cfg, StubShell()), 1)
            m.assert_not_called()

    def test_gui_command_says_so_when_the_port_is_taken(self):
        gui, cfg = self.gui_env()
        with mock.patch.object(gui, "make_server", side_effect=OSError("Address in use")):
            with self.assertRaises(ctl.GhostAPError) as cm:
                ctl.cmd_gui(cfg, StubShell())
        self.assertIn("9123", str(cm.exception))

    def test_gui_password_command_saves_a_hash_and_checks_the_retype(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        target = d / "gui-password"
        with mock.patch.object(ctl, "GUI_PASSWORD_FILE", target), \
                mock.patch("getpass.getpass", side_effect=["adminpass123", "adminpass123"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ctl.main(["gui-password"]), 0)
        self.assertTrue(ctl._gui_module().verify_password("adminpass123", target.read_text()))
        self.assertNotIn("adminpass123", target.read_text())
        target.unlink()
        with mock.patch.object(ctl, "GUI_PASSWORD_FILE", target), \
                mock.patch("getpass.getpass", side_effect=["adminpass123", "different123"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ctl.main(["gui-password"]), 1)
        self.assertFalse(target.exists())
        with mock.patch.object(ctl, "GUI_PASSWORD_FILE", target), \
                mock.patch("getpass.getpass", side_effect=["short", "short"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ctl.main(["gui-password"]), 1)
        self.assertFalse(target.exists())

    def test_looking_commands_do_not_write_into_the_controller_log(self):
        path = self.write_config()
        # (command, is the file log skipped?). _setup_file_log's one argument means "skip it".
        for command, skipped in (("status", True), ("preflight", True), ("gui", True),
                                 ("stop", False)):
            with mock.patch.object(ctl, "_setup_file_log") as setup, \
                    mock.patch.dict(ctl.COMMANDS, {command: mock.Mock(return_value=0)}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ctl.main([command, "-c", str(path)]), 0, command)
            setup.assert_called_once_with(skipped)

    def test_the_gui_never_needs_the_ap_password_to_start(self):
        # the service user can't read /etc/ghostap/passphrase, and shouldn't need to
        path = self.write_config('[gui]\nenabled = true\n')
        (path.parent / "pw").unlink()
        cfg = ctl.load_config(path, secrets=False)
        self.assertTrue(cfg.gui_enabled)
        self.assertIn("gui", ctl.READ_ONLY)


def sh_now():
    return time.time()


if __name__ == "__main__":
    unittest.main()
