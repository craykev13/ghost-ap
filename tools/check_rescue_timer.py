#!/usr/bin/env python3
"""Verify the REAL systemd behaviour GhostAP's rescue timer depends on.

Touches NO networking. It drives the controller's own arm_rescue()/disarm_rescue()
(so the real flags are tested) but with harmless commands that append a line to a
marker file. Run it on the Pi, as root, BEFORE any live AP test:

    sudo python3 tools/check_rescue_timer.py
    sudo python3 tools/check_rescue_timer.py --config /etc/ghostap/ghostap.toml   # + end-to-end stage

It checks: creation, activation, expiry, cleanup, name reuse after a failed run,
arming while a recover is running, the timer surviving the death of the process that
armed it, and (with --config) that systemd can actually run the real recover command
line. Exits 0 only if every stage passes.
"""
import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent


def load_controller():
    for candidate in (HERE.parent / "ghostap" / "controller.py", HERE / "controller.py",
                      HERE.parent / "controller.py"):
        if candidate.exists():
            spec = importlib.util.spec_from_file_location("ghostap_controller", candidate)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod
    sys.exit("can't find controller.py next to or above this script")


ctl = load_controller()
UNIT = ctl.RESCUE_UNIT
TIMER, SERVICE = f"{UNIT}.timer", f"{UNIT}.service"
MARK = pathlib.Path("/run/ghostap-timer-check.log")
sh = ctl.Shell()
results = []


def systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def load_state(unit):
    return systemctl("show", "-p", "LoadState", "--value", unit).stdout.strip()


def marks():
    return MARK.read_text().split() if MARK.exists() else []


def echo(tag):
    return f"echo {tag} >> {MARK}"


def arm(seconds, script):
    ctl.arm_rescue(sh, seconds, ["/bin/sh", "-c", script])


def wait_until(pred, timeout, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def gone():
    return load_state(TIMER) == "not-found" and load_state(SERVICE) == "not-found"


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ""))


def info(msg):
    print(f"  [info] {msg}")


def stage(title):
    print(f"\n{title}")


def child_arm_then_die():
    """Arm a timer, then SIGKILL ourselves: no cleanup code can possibly run."""
    arm(4, echo("S5"))
    os.kill(os.getpid(), signal.SIGKILL)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--child-arm":
        child_arm_then_die()
    config = None
    if "--config" in sys.argv:
        config = pathlib.Path(sys.argv[sys.argv.index("--config") + 1])

    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 tools/check_rescue_timer.py")
    if not pathlib.Path("/run/systemd/system").exists():
        sys.exit("systemd is not running as PID 1 here, so there is nothing to check")
    if load_state(TIMER) != "not-found" or load_state(SERVICE) != "not-found":
        sys.exit(f"{UNIT} exists right now (a real rescue timer may be armed). "
                 "Refusing to touch it. Wait for it, or run: ghostap test --abort")
    MARK.unlink(missing_ok=True)
    print(subprocess.run(["systemctl", "--version"], capture_output=True, text=True).stdout.splitlines()[0])

    try:
        stage("1. Arm, see it, disarm: a disarmed timer must never fire")
        arm(8, echo("S1"))
        check("timer is active after arming", systemctl("is-active", TIMER).stdout.strip() == "active")
        check("timer is listed by systemctl list-timers",
              UNIT in systemctl("list-timers", "--all", "--no-pager").stdout)
        ctl.disarm_rescue(sh)
        check("disarm unloads the timer", wait_until(lambda: load_state(TIMER) == "not-found", 3),
              f"LoadState={load_state(TIMER)}")
        check("disarm also unloads the service half (else re-arming would hit a name clash)",
              wait_until(lambda: load_state(SERVICE) == "not-found", 5),
              f"LoadState={load_state(SERVICE)}")
        try:
            arm(60, "true")
            check("re-arming straight after a disarm works", True)
        except ctl.GhostAPError as e:
            check("re-arming straight after a disarm works", False, str(e)[:140])
        ctl.disarm_rescue(sh)
        wait_until(gone, 5)
        time.sleep(10)
        check("a disarmed timer does not fire", "S1" not in marks(), f"marks={marks()}")

        stage("2. Expiry: fires on time (AccuracySec) and the units clean themselves up")
        t0 = time.monotonic()
        arm(4, echo("S2"))
        fired = wait_until(lambda: "S2" in marks(), 20)
        elapsed = time.monotonic() - t0
        check("timer fires and runs its command", fired)
        check("fires close to the requested 4s (default accuracy could be ~60s late)",
              fired and 3.0 <= elapsed <= 7.0, f"{elapsed:.1f}s")
        check("both units are gone afterwards", wait_until(gone, 6),
              f"timer={load_state(TIMER)} service={load_state(SERVICE)}")

        stage("3. A FAILED run must not hold the unit name hostage")
        arm(2, "exit 3")
        time.sleep(6)
        check("failed service is unloaded (--collect)", load_state(SERVICE) == "not-found",
              f"LoadState={load_state(SERVICE)}")
        try:
            arm(60, "true")
            check("re-arming straight after a failure works", True)
        except ctl.GhostAPError as e:
            check("re-arming straight after a failure works", False, str(e))
        ctl.disarm_rescue(sh)

        stage("4. Arming while a recover is still running")
        arm(2, f"{echo('S4a')}; sleep 8; {echo('S4b')}")
        wait_until(lambda: "S4a" in marks(), 10)
        try:
            arm(60, "true")
            info("a second arming SUCCEEDED while the first run was still going")
        except ctl.GhostAPError as e:
            info(f"a second arming was refused cleanly: {str(e)[:110]}")
        check("arming never kills the recover that is already running",
              wait_until(lambda: "S4b" in marks(), 14), f"marks={marks()}")
        ctl.disarm_rescue(sh)
        wait_until(gone, 5)

        stage("5. The timer outlives the process that armed it (SIGKILL, no cleanup runs)")
        child = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve()), "--child-arm"])
        check("arming process was killed by SIGKILL", child.returncode == -signal.SIGKILL,
              f"returncode={child.returncode}")
        check("timer still fired afterwards", wait_until(lambda: "S5" in marks(), 12), f"marks={marks()}")
        wait_until(gone, 5)

        stage("6. End to end: can systemd run the REAL recover command line? (needs --config)")
        if config is None:
            info("skipped. Re-run with --config /etc/ghostap/ghostap.toml to include it")
        else:
            try:
                cfg = ctl.load_config(config)
            except ctl.GhostAPError as e:
                check("config loads", False, str(e))
                cfg = None
            if cfg and (not ctl.wifi_connected(sh, cfg.interface) or ctl._daemons_present(sh)
                        or ctl.read_state() is not None):
                info("skipped: this stage is only harmless when connected to WiFi with no AP and "
                     "no active test. Run it that way.")
            elif cfg:
                profile = ctl.current_connection(sh, cfg.interface) or "My Phone"
                # `recover` on a healthy client is a no-op by design ("nothing to undo").
                ctl.arm_rescue(sh, 3, ctl.recover_command(cfg, expect="test", original=profile))
                wait_until(gone, 15)
                time.sleep(1)
                log = subprocess.run(["journalctl", "-u", SERVICE, "--since", "-40s", "-o", "cat",
                                      "--no-pager"], capture_output=True, text=True).stdout
                check("systemd ran recover with our arguments and it exited cleanly",
                      "nothing to undo" in log, "see journal below if this failed")
                if "nothing to undo" not in log:
                    print("      journal:\n      " + (log.strip().replace("\n", "\n      ") or "(empty)"))
                check("network was not disturbed", ctl.wifi_connected(sh, cfg.interface))
    finally:
        ctl.disarm_rescue(sh)
        MARK.unlink(missing_ok=True)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    if all(results):
        print("The rescue timer behaves as GhostAP assumes. Safe to move on to the live test.")
        return 0
    print("Do NOT run the live AP test until the failures above are understood.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
