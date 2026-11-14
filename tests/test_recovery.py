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
import logging
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
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
        self.effect_count = 0
        self.crash_at = None             # raise SimulatedCrash after this many effects
        self.interrupt_at = None         # raise ctl.Interrupted after this many effects
        # failure switches
        self.fail_hostapd = False
        self.hostapd_broken = False      # starts, but never reaches ENABLED
        self.hostapd_no_pidfile = False
        self.fail_systemd_run = False
        self.systemd_run_half_creates = False   # timer gets created, THEN the call errors

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
        return True


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = pathlib.Path(tmp.name) / "run"
        for name, value in dict(
            RUN_DIR=run, HOSTAPD_CONF=run / "hostapd.conf", HOSTAPD_PID=run / "hostapd.pid",
            DNSMASQ_CONF=run / "dnsmasq.conf", DNSMASQ_PID=run / "dnsmasq.pid",
            STATE_FILE=run / "state.json",
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


if __name__ == "__main__":
    unittest.main()
