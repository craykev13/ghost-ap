#!/usr/bin/env python3
"""Installer tests. No Pi, no root: the installer is pointed at a temp folder that stands
in for the Pi's filesystem, and every command it would run is recorded instead of run.

    python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import tomllib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Private copies, so these tests don't depend on (or disturb) the other test file's.
ctl = _load("ghostap_controller_inst", ROOT / "ghostap" / "controller.py")
inst = _load("ghostap_installer_test", ROOT / "ghostap" / "installer.py")
inst._controller = lambda: ctl


class Runner:
    """Stands in for running commands. Records them; answers is-enabled from a table."""

    def __init__(self, enabled=None, fail=()):
        self.calls, self.enabled, self.fail = [], dict(enabled or {}), list(fail)

    def __call__(self, cmd, env=None):
        self.calls.append(tuple(cmd))
        if cmd[:2] == ("systemctl", "is-enabled"):
            state = self.enabled.get(cmd[2], "disabled")
            return (0 if state == "enabled" else 1), state
        for prefix in self.fail:
            if cmd[:len(prefix)] == tuple(prefix):
                return 1, "simulated failure"
        return 0, ""

    def ran(self, *prefix):
        return any(c[:len(prefix)] == prefix for c in self.calls)


def facts(**kw):
    f = ctl.HostFacts(
        model="Raspberry Pi Zero W Rev 1.1", os_name="Raspbian GNU/Linux 13 (trixie)",
        tools={t: True for t in ("hostapd", "dnsmasq", "iw", "nmcli", "systemd-run", "ip")},
        active={"NetworkManager": "active", "wpa_supplicant": "active"},
        enabled={"hostapd": "masked", "dnsmasq": "disabled"},
        ap_mode=True, reg_country="US", wifi_profiles=1)
    for k, v in kw.items():
        setattr(f, k, v)
    return f


class FakeShell:
    """What the preflight reads, for a healthy Pi (or one with a different network stack)."""

    def __init__(self, stack="NetworkManager", tools=("hostapd", "dnsmasq", "iw", "nmcli",
                                                       "systemd-run", "ip")):
        self.stack, self.tools = stack, set(tools)

    def probe(self, *cmd):
        if cmd[:2] == ("systemctl", "is-active"):
            on = cmd[2] in (self.stack, "wpa_supplicant")
            return (0, "active") if on else (3, "inactive")
        if cmd[:2] == ("systemctl", "is-enabled"):
            return 0, "disabled"
        if cmd == ("test", "-d", "/run/systemd/system") or cmd[:2] == ("ip", "link"):
            return 0, ""
        if cmd == ("iw", "list"):
            return 0, "\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n"
        if cmd == ("iw", "reg", "get"):
            return 0, "country US: DFS-FCC\n"
        if cmd[:2] == ("nmcli", "-t"):
            return 0, "Home:802-11-wireless"
        if cmd[:2] == ("nmcli", "-g"):
            return 0, "infrastructure"
        return 127, ""

    def read_text(self, path):
        return {"/proc/device-tree/model": "Raspberry Pi Zero W Rev 1.1\0",
                "/etc/os-release": 'PRETTY_NAME="Raspbian GNU/Linux 13 (trixie)"\n'}.get(path)

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None


class Rig(unittest.TestCase):
    """A fake Pi filesystem plus helpers."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runner = Runner()
        self.host = inst.Host(str(self.tmp), self.runner)
        self.said = []

    def mk_host(self, **runner_kw):
        self.runner = Runner(**runner_kw)
        self.host = inst.Host(str(self.tmp), self.runner)
        return self.host

    def choices(self, **kw):
        base = dict(passphrase="correct horse")
        base.update(kw)
        return inst.Choices(**base)

    def plan(self, c=None, f=None):
        c = c or self.choices()
        return inst.build_plan(self.host, c, f or facts(), []), c

    def install(self, c=None, f=None):
        plan, c = self.plan(c, f)
        inst.apply_plan(self.host, plan, c)
        return plan

    def path(self, p):
        return self.host.p(p)

    def tree(self):
        """Every file and link (not the ordinary system folders a real Pi already has)."""
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                      if p.is_file() or p.is_symlink())

    def run_cli(self, argv, answers=(), secrets=(), shell=None, root=True):
        answers, secrets = list(answers), list(secrets)

        def ask(prompt=""):
            if not answers:
                raise EOFError
            return answers.pop(0)

        def secret(prompt=""):
            if not secrets:
                raise EOFError
            return secrets.pop(0)
        io = inst.IO(ask, secret, self.said.append)
        rc = inst.run(argv, io=io, host=self.host, shell=shell or FakeShell(), is_root=root)
        return rc, "\n".join(self.said)


class ConfigTests(Rig):
    def load(self, text):
        p = self.tmp / "c.toml"
        p.write_text(text)
        return ctl.load_config(p, secrets=False)

    def test_defaults_make_a_config_the_controller_accepts(self):
        cfg = self.load(inst.render_config(inst.Choices()))
        self.assertEqual((cfg.interface, cfg.ssid, cfg.country), ("wlan0", "ZeroFi", "US"))
        self.assertEqual((cfg.dhcp_start, cfg.dhcp_end), ("192.168.4.10", "192.168.4.50"))
        self.assertEqual(cfg.failover_grace, 120)
        self.assertFalse(cfg.hostapd_debug)

    def test_answers_land_in_the_config(self):
        cfg = self.load(inst.render_config(inst.Choices(
            interface="wlan1", ssid="My Pi", country="GB", failover_grace=45,
            hostapd_debug=True)))
        self.assertEqual((cfg.interface, cfg.ssid, cfg.country, cfg.failover_grace,
                          cfg.hostapd_debug), ("wlan1", "My Pi", "GB", 45, True))

    def test_failover_off_means_no_failover_setting(self):
        text = inst.render_config(inst.Choices(failover=False))
        self.assertNotIn("failover_grace", text)
        self.assertEqual(self.load(text).failover_grace, 0)

    def test_awkward_ssids_survive_quoting(self):
        for ssid in ('He said "hi"', "back\\slash", "café ☕", "a#b=c"):
            self.assertEqual(self.load(inst.render_config(inst.Choices(ssid=ssid))).ssid, ssid)

    def test_bad_answers_are_refused_before_anything_is_written(self):
        for kw in ({"country": "USA"}, {"ssid": "x" * 33}, {"failover_grace": 5}):
            with self.assertRaises(inst.InstallError, msg=str(kw)):
                inst.check_config_text(inst.render_config(inst.Choices(**kw)))

    def test_passphrase_rules(self):
        self.assertIsNone(inst.check_passphrase("longenough"))
        for bad in ("short", "x" * 64, "cafécafécafé", "has\nnewline1"):
            self.assertIsNotNone(inst.check_passphrase(bad), bad)

    def test_generated_passwords_are_valid_readable_and_different(self):
        seen = {inst.generate_passphrase() for _ in range(20)}
        self.assertEqual(len(seen), 20)
        for p in seen:
            self.assertRegex(p, r"^[a-hjkmnp-z2-9]{4}(-[a-hjkmnp-z2-9]{4}){3}$")
            self.assertIsNone(inst.check_passphrase(p))


class PlanTests(Rig):
    def test_fresh_pi_plan_covers_everything_and_needs_no_packages(self):
        plan, _ = self.plan()
        targets = {s.target: s for s in plan.steps}
        for t in (inst.CONF, inst.PASSPHRASE, inst.LINK, inst.SERVICE, inst.DISPATCHER):
            self.assertEqual(targets[t].status, "new", t)
        self.assertEqual(targets[inst.PASSPHRASE].mode, 0o600)
        self.assertEqual(targets[inst.DISPATCHER].mode, 0o755)
        self.assertFalse([s for s in plan.steps if s.kind == "packages"])
        self.assertTrue(targets["ghostap.service"].cmd == ("systemctl", "enable", "ghostap.service"))

    def test_only_missing_packages_are_installed(self):
        f = facts()
        f.tools["dnsmasq"] = False
        f.tools["hostapd"] = False
        plan, _ = self.plan(f=f)
        pk = next(s for s in plan.steps if s.kind == "packages")
        self.assertEqual(pk.pkgs, ("dnsmasq-base", "hostapd"))
        # the hostapd package may enable its own service: we switch that off
        self.assertTrue(any(s.cmd == ("systemctl", "disable", "hostapd.service")
                            for s in plan.steps))
        f2 = facts()
        f2.tools["iw"] = False
        pk = next(s for s in self.plan(f=f2)[0].steps if s.kind == "packages")
        self.assertEqual(pk.pkgs, ("iw",))
        self.assertFalse(any(s.cmd == ("systemctl", "disable", "hostapd.service")
                             for s in self.plan(f=f2)[0].steps))

    def test_choices_switch_pieces_off(self):
        plan, _ = self.plan(self.choices(failover=False, boot_service=False))
        targets = {s.target for s in plan.steps}
        self.assertNotIn(inst.DISPATCHER, targets)
        self.assertNotIn(inst.SERVICE, targets)
        self.assertNotIn("ghostap.service", targets)

    def test_persistent_journal_is_optional(self):
        self.assertNotIn(inst.JOURNAL, {s.target for s in self.plan()[0].steps})
        plan, _ = self.plan(self.choices(persistent_journal=True))
        self.assertIn(inst.JOURNAL, {s.target for s in plan.steps})

    def test_no_password_anywhere_is_an_error(self):
        with self.assertRaises(inst.InstallError):
            inst.build_plan(self.host, inst.Choices(), facts(), [])

    def test_weak_password_is_refused(self):
        with self.assertRaises(inst.InstallError):
            self.plan(self.choices(passphrase="short"))

    def test_legacy_service_is_disabled_only_when_enabled(self):
        plan, _ = self.plan()
        self.assertNotIn(inst.LEGACY_SERVICE, {s.target for s in plan.steps})
        self.mk_host(enabled={inst.LEGACY_SERVICE: "enabled"})
        plan, _ = self.plan()
        step = next(s for s in plan.steps if s.target == inst.LEGACY_SERVICE)
        self.assertEqual(step.cmd, ("systemctl", "disable", inst.LEGACY_SERVICE))
        self.assertEqual(step.undo, ("systemctl", "enable", inst.LEGACY_SERVICE))
        # and it never STOPS it: that could pull the radio out from under a live session
        self.assertNotIn("--now", step.cmd)
        plan, _ = self.plan(self.choices(disable_legacy=False))
        self.assertNotIn(inst.LEGACY_SERVICE, {s.target for s in plan.steps})

    def test_stray_system_daemons_are_disabled_not_stopped(self):
        f = facts(enabled={"hostapd": "enabled", "dnsmasq": "enabled"})
        plan, _ = self.plan(f=f)
        for svc in ("hostapd", "dnsmasq"):
            step = next(s for s in plan.steps if s.target == f"{svc}.service"
                        and s.undo)
            self.assertEqual(step.cmd, ("systemctl", "disable", f"{svc}.service"))

    def test_leftover_hotspot_profile_is_reported_not_deleted(self):
        plan, _ = self.plan(f=facts(ap_profiles=("ZeroFi-Hotspot",)))
        self.assertIn("ZeroFi-Hotspot", " ".join(plan.notes))
        self.assertFalse(any("nmcli" in " ".join(s.cmd) for s in plan.steps))


class ApplyTests(Rig):
    def test_fresh_install_writes_everything_correctly(self):
        self.install()
        h = self.host
        self.assertEqual(h.read(inst.SERVICE), (ROOT / "systemd/ghostap.service").read_bytes())
        self.assertEqual(h.read(inst.DISPATCHER),
                         (ROOT / "nm-dispatcher/90-ghostap").read_bytes())
        self.assertEqual(h.mode(inst.DISPATCHER), 0o755)
        self.assertEqual(h.mode(inst.PASSPHRASE), 0o600)
        self.assertEqual(h.read(inst.PASSPHRASE), b"correct horse\n")
        self.assertEqual(h.readlink(inst.LINK), str(ROOT / "ghostap" / "controller.py"))
        ctl.load_config(self.path(inst.CONF), secrets=False)
        self.assertTrue(self.runner.ran("systemctl", "daemon-reload"))
        self.assertTrue(self.runner.ran("systemctl", "enable", "ghostap.service"))

    def test_install_never_starts_anything_or_touches_the_network(self):
        self.install()
        for c in self.runner.calls:
            joined = " ".join(c)
            self.assertNotIn("--now", joined)
            self.assertFalse({"start", "restart", "stop"} & set(c) - {"systemd-journald"}, c)
            self.assertNotIn("nmcli", joined)
            self.assertNotIn("hostapd", c[0])

    def test_apt_runs_noninteractively_and_only_when_needed(self):
        seen = {}
        orig = self.host.runner

        def spy(cmd, env=None):
            if cmd[0] == "apt-get":
                seen["env"] = env
            return orig(cmd, env)
        self.host.runner = spy
        f = facts()
        f.tools["hostapd"] = False
        self.install(f=f)
        self.assertEqual(seen["env"]["DEBIAN_FRONTEND"], "noninteractive")
        self.assertTrue(self.runner.ran("apt-get", "install", "-y", "--no-install-recommends",
                                        "hostapd"))
        self.assertFalse(self.runner.ran("apt-get", "update"))

    def test_second_run_finds_nothing_to_do(self):
        self.install()
        self.runner.enabled["ghostap.service"] = "enabled"     # what systemd would now say
        self.runner.calls.clear()
        plan, _ = self.plan()
        self.assertEqual(plan.changes(), [])
        self.assertIn("already in place", inst.render_plan(plan, dry_run=False))

    def test_partial_changes_only_redo_what_differs(self):
        self.install()
        self.path(inst.SERVICE).write_text("# someone edited this\n")
        plan, _ = self.plan()
        self.assertEqual([s.target for s in plan.changes() if s.kind == "file"], [inst.SERVICE])
        self.assertEqual(plan.changes()[0].status, "replace")

    def test_right_content_with_wrong_permissions_is_not_called_fine(self):
        self.install()
        os.chmod(self.path(inst.DISPATCHER), 0o644)      # NetworkManager ignores non-executable hooks
        os.chmod(self.path(inst.PASSPHRASE), 0o644)      # and a world-readable password is bad
        self.runner.enabled["ghostap.service"] = "enabled"
        plan, c = self.plan()
        self.assertEqual({s.target for s in plan.changes()}, {inst.DISPATCHER, inst.PASSPHRASE})
        inst.apply_plan(self.host, plan, c)
        self.assertEqual(self.host.mode(inst.DISPATCHER), 0o755)
        self.assertEqual(self.host.mode(inst.PASSPHRASE), 0o600)

    def test_replaced_files_are_backed_up(self):
        self.install()
        self.path(inst.SERVICE).write_text("# my own version\n")
        self.install()
        backups = [p for p in (self.tmp / "var/lib/ghostap/backups").rglob("*") if p.is_file()]
        self.assertTrue(any(p.read_text() == "# my own version\n" for p in backups))

    def test_manifest_is_written_and_holds_no_password(self):
        self.install()
        text = self.host.read(inst.MANIFEST).decode()
        data = json.loads(text)
        self.assertEqual(data["version"], inst.MANIFEST_VERSION)
        self.assertNotIn("correct horse", text)
        self.assertEqual(self.host.mode(inst.MANIFEST), 0o600)
        kinds = {e["kind"] for e in data["entries"]}
        self.assertTrue({"file", "link", "cmd", "mkdir"} <= kinds)

    def test_failure_part_way_leaves_a_usable_record(self):
        self.mk_host(fail=[("systemctl", "enable", "ghostap.service")])
        plan, c = self.plan()
        with self.assertRaises(inst.InstallError):
            inst.apply_plan(self.host, plan, c)
        data = json.loads(self.host.read(inst.MANIFEST).decode())
        self.assertIn(inst.SERVICE, {e["target"] for e in data["entries"]})
        # and uninstall can still clean up what got done
        self.runner.fail.clear()
        self.assertEqual(inst.uninstall(self.host, remove_config=True, say=lambda m: None), 0)
        self.assertFalse(self.host.exists(inst.SERVICE))
        self.assertFalse(self.host.exists(inst.LINK))

    def test_best_effort_commands_do_not_abort_the_install(self):
        self.mk_host(enabled={inst.LEGACY_SERVICE: "enabled"},
                     fail=[("systemctl", "disable", inst.LEGACY_SERVICE)])
        self.install()
        self.assertTrue(self.host.exists(inst.SERVICE))

    def test_keeping_an_existing_config_leaves_it_byte_for_byte(self):
        self.host.write(inst.CONF, b"# mine\n[access_point]\nssid = \"Mine\"\n", 0o644)
        self.host.write(inst.PASSPHRASE, b"already-set\n", 0o600)
        plan, c = self.plan(inst.Choices(keep_config=True))
        self.assertIn("failover_grace", " ".join(plan.notes))
        inst.apply_plan(self.host, plan, c)
        self.assertEqual(self.host.read(inst.CONF), b"# mine\n[access_point]\nssid = \"Mine\"\n")
        self.assertEqual(self.host.read(inst.PASSPHRASE), b"already-set\n")

    def test_rewriting_a_config_backs_up_the_old_one(self):
        self.host.write(inst.CONF, b"# old\n", 0o644)
        self.host.write(inst.PASSPHRASE, b"already-set\n", 0o600)
        plan, c = self.plan(self.choices(passphrase=None, ssid="NewName"))
        self.assertEqual(next(s for s in plan.steps if s.target == inst.CONF).status, "replace")
        inst.apply_plan(self.host, plan, c)
        self.assertIn(b"NewName", self.host.read(inst.CONF))
        self.assertTrue(any(p.read_bytes() == b"# old\n"
                            for p in (self.tmp / "var/lib/ghostap/backups").rglob("*")
                            if p.is_file()))


class UninstallTests(Rig):
    def say(self, m):
        self.said.append(m)

    def test_uninstall_removes_what_it_added_and_keeps_your_settings(self):
        self.install()
        self.assertEqual(inst.uninstall(self.host, say=self.say), 0)
        for t in (inst.SERVICE, inst.DISPATCHER, inst.LINK, inst.MANIFEST):
            self.assertFalse(self.host.exists(t), t)
        self.assertTrue(self.host.exists(inst.CONF))
        self.assertTrue(self.host.exists(inst.PASSPHRASE))
        self.assertTrue(self.runner.ran("systemctl", "disable", "ghostap.service"))
        self.assertIn("--remove-config", "\n".join(self.said))

    def test_remove_config_leaves_nothing_behind(self):
        before = self.tree()
        self.install()
        inst.uninstall(self.host, remove_config=True, say=self.say)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.host.exists(inst.CONF_DIR))
        self.assertFalse(self.host.exists(inst.STATE_DIR))

    def test_files_you_changed_are_not_deleted_without_force(self):
        self.install()
        self.path(inst.DISPATCHER).write_text("#!/bin/sh\n# my hook\n")
        inst.uninstall(self.host, say=self.say)
        self.assertTrue(self.host.exists(inst.DISPATCHER))
        self.assertIn("you changed it", "\n".join(self.said))

    def test_force_removes_files_you_changed(self):
        self.install()
        self.path(inst.DISPATCHER).write_text("#!/bin/sh\n# my hook\n")
        inst.uninstall(self.host, force=True, say=self.say)
        self.assertFalse(self.host.exists(inst.DISPATCHER))

    def test_a_link_someone_repointed_is_left_alone_without_force(self):
        self.install()
        self.host.symlink(inst.LINK, "/somewhere/else")
        inst.uninstall(self.host, say=self.say)
        self.assertEqual(self.host.readlink(inst.LINK), "/somewhere/else")

    def test_previous_files_are_restored(self):
        self.host.write(inst.SERVICE, b"# the one I had before\n", 0o644)
        self.install()
        self.assertNotEqual(self.host.read(inst.SERVICE), b"# the one I had before\n")
        inst.uninstall(self.host, say=self.say)
        self.assertEqual(self.host.read(inst.SERVICE), b"# the one I had before\n")

    def test_a_previous_link_is_restored(self):
        self.host.symlink(inst.LINK, "/old/place/ghostap")
        self.install()
        inst.uninstall(self.host, say=self.say)
        self.assertEqual(self.host.readlink(inst.LINK), "/old/place/ghostap")

    def test_things_that_were_already_there_are_not_ours_to_delete(self):
        # hand-installed before the installer existed
        self.host.write(inst.SERVICE, (ROOT / "systemd/ghostap.service").read_bytes(), 0o644)
        self.host.write(inst.DISPATCHER, (ROOT / "nm-dispatcher/90-ghostap").read_bytes(), 0o755)
        self.host.symlink(inst.LINK, str(ROOT / "ghostap" / "controller.py"))
        self.install()
        inst.uninstall(self.host, say=self.say)
        for t in (inst.SERVICE, inst.DISPATCHER, inst.LINK):
            self.assertTrue(self.host.exists(t), t)
        self.assertIn("already there", "\n".join(self.said))

    def test_undo_commands_run_in_reverse_and_legacy_service_comes_back(self):
        self.mk_host(enabled={inst.LEGACY_SERVICE: "enabled"})
        self.install()
        self.runner.calls.clear()
        inst.uninstall(self.host, say=self.say)
        self.assertTrue(self.runner.ran("systemctl", "enable", inst.LEGACY_SERVICE))
        self.assertTrue(self.runner.ran("systemctl", "disable", "ghostap.service"))

    def test_packages_are_never_removed(self):
        f = facts()
        f.tools["hostapd"] = False
        self.install(f=f)
        self.runner.calls.clear()
        inst.uninstall(self.host, say=self.say)
        self.assertFalse(self.runner.ran("apt-get"))
        self.assertIn("hostapd", "\n".join(self.said))

    def test_rerun_with_new_answers_still_uninstalls_back_to_the_start(self):
        before = self.tree()
        self.install(self.choices(ssid="First"))
        self.install(self.choices(ssid="Second", passphrase="another pass"))
        self.install(self.choices(ssid="Third", passphrase="third passw"))
        inst.uninstall(self.host, remove_config=True, say=self.say)
        self.assertEqual(self.tree(), before)

    def test_nothing_to_uninstall_is_said_plainly(self):
        self.assertEqual(inst.uninstall(self.host, say=self.say), 1)
        self.assertIn("Nothing to uninstall", "\n".join(self.said))

    def test_a_damaged_record_is_refused_cleanly(self):
        self.host.write(inst.MANIFEST, b"{not json", 0o600)
        with self.assertRaises(inst.InstallError):
            inst.uninstall(self.host, say=self.say)


class CommandLineTests(Rig):
    def test_dry_run_changes_nothing_and_says_so(self):
        before = self.tree()
        rc, out = self.run_cli(["--dry-run", "--yes", "--generate-password"], root=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.tree(), before)
        self.assertIn("dry run", out)
        self.assertIn("Here's what will happen", out)
        self.assertFalse([c for c in self.runner.calls if c[0] != "systemctl"])
        self.assertFalse(self.runner.ran("systemctl", "enable"))

    def test_the_plan_screen_never_shows_the_password(self):
        rc, out = self.run_cli(["--dry-run", "--yes", "--generate-password"], root=False)
        self.assertNotRegex(out, r"[a-hjkmnp-z2-9]{4}-[a-hjkmnp-z2-9]{4}-[a-hjkmnp-z2-9]{4}")
        rc, out = self.run_cli(["--dry-run", "--yes"], root=False)   # no password given
        self.assertEqual(rc, 1)

    def test_real_run_needs_root(self):
        rc, out = self.run_cli(["--yes", "--generate-password"], root=False)
        self.assertEqual(rc, 1)
        self.assertIn("sudo", out)
        self.assertFalse(self.host.exists(inst.CONF))

    def test_unattended_install_works_and_shows_a_generated_password_once(self):
        rc, out = self.run_cli(["--yes", "--generate-password", "--ssid", "Lab Pi",
                                "--country", "gb", "--failover-grace", "90"])
        self.assertEqual(rc, 0, out)
        cfg = ctl.load_config(self.path(inst.CONF), secrets=False)
        self.assertEqual((cfg.ssid, cfg.country, cfg.failover_grace), ("Lab Pi", "GB", 90))
        saved = self.host.read(inst.PASSPHRASE).decode().strip()
        self.assertEqual(out.count(saved), 1)
        self.assertIn("sudo ghostap test", out)

    def test_answers_that_make_a_bad_config_stop_everything_before_any_change(self):
        for flags in (["--ssid", "x" * 40], ["--country", "USA"], ["--failover-grace", "3"]):
            self.said.clear()
            before = self.tree()
            rc, out = self.run_cli(["--yes", "--generate-password", *flags])
            self.assertEqual(rc, 1, flags)
            self.assertIn("Error:", out)
            self.assertEqual(self.tree(), before)
            self.assertFalse(self.runner.ran("systemctl", "enable"))

    def test_a_typed_password_is_never_echoed_back(self):
        pw = self.tmp / "pw.txt"
        pw.write_text("my secret pass\n")
        rc, out = self.run_cli(["--yes", "--passphrase-file", str(pw)])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("my secret pass", out)
        self.assertEqual(self.host.read(inst.PASSPHRASE), b"my secret pass\n")

    def test_yes_with_an_existing_config_keeps_it_unless_told_otherwise(self):
        self.host.write(inst.CONF, b"# mine\n", 0o644)
        self.host.write(inst.PASSPHRASE, b"already-set\n", 0o600)
        rc, out = self.run_cli(["--yes"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.host.read(inst.CONF), b"# mine\n")
        rc, out = self.run_cli(["--yes", "--ssid", "Changed"])
        self.assertIn(b"Changed", self.host.read(inst.CONF))

    def test_blockers_stop_the_install_but_missing_packages_do_not(self):
        rc, out = self.run_cli(["--yes", "--generate-password"], shell=FakeShell(stack="dhcpcd"))
        self.assertEqual(rc, 1)
        self.assertIn("can't be installed", out)
        self.assertFalse(self.host.exists(inst.CONF))
        self.said.clear()
        rc, out = self.run_cli(["--yes", "--generate-password"],
                               shell=FakeShell(tools=("iw", "nmcli", "systemd-run", "ip")))
        self.assertEqual(rc, 0, out)
        self.assertIn("hostapd isn't installed yet", out)
        self.assertTrue(self.runner.ran("apt-get", "install"))

    def test_uninstall_through_the_command_line(self):
        self.run_cli(["--yes", "--generate-password"])
        self.said.clear()
        rc, out = self.run_cli(["--uninstall"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.host.exists(inst.SERVICE))
        rc, out = self.run_cli(["--uninstall"], root=False)
        self.assertEqual(rc, 1)

    def test_a_failing_install_says_how_to_roll_back(self):
        self.mk_host(fail=[("systemctl", "enable")])
        rc, out = self.run_cli(["--yes", "--generate-password"])
        self.assertEqual(rc, 1)
        self.assertIn("--uninstall", out)


class InteractiveTests(Rig):
    def test_the_questions_and_the_summary_before_anything_changes(self):
        rc, out = self.run_cli(
            [], answers=["MyZero", "1", "gb", "2", "60", "y", "n", "y"],
            secrets=["hunter22hunter", "hunter22hunter"])
        self.assertEqual(rc, 0, out)
        cfg = ctl.load_config(self.path(inst.CONF), secrets=False)
        self.assertEqual((cfg.ssid, cfg.country, cfg.failover_grace), ("MyZero", "GB", 60))
        self.assertEqual(self.host.read(inst.PASSPHRASE), b"hunter22hunter\n")
        self.assertLess(out.index("Here's what will happen"), out.index("Go ahead?")
                        if "Go ahead?" in out else len(out))
        self.assertNotIn("hunter22hunter", out)

    def test_bad_answers_are_asked_again(self):
        rc, out = self.run_cli(
            [], answers=["x" * 40, "ZeroFi", "1", "USA", "us", "2", "5", "30", "y", "n", "y"],
            secrets=["short", "longenough1", "mismatch22", "longenough1", "longenough1"])
        self.assertEqual(rc, 0, out)
        self.assertIn("1 to 32 characters", out)
        self.assertIn("8 to 63", out)
        self.assertIn("didn't match", out)
        self.assertIn("two letters", out)
        self.assertIn("at least 15", out)
        self.assertEqual(self.host.read(inst.PASSPHRASE), b"longenough1\n")

    def test_making_up_a_password_shows_it_once_at_the_end(self):
        rc, out = self.run_cli([], answers=["", "2", "", "2", "", "y", "n", "y"])
        self.assertEqual(rc, 0, out)
        saved = self.host.read(inst.PASSPHRASE).decode().strip()
        self.assertEqual(out.count(saved), 1)
        self.assertLess(out.index("Done."), out.index(saved))

    def test_saying_no_at_the_summary_changes_nothing(self):
        before = self.tree()
        rc, out = self.run_cli([], answers=["", "2", "", "2", "", "y", "n", "n"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.tree(), before)
        self.assertIn("Cancelled. Nothing was changed.", out)

    def test_ctrl_c_or_ctrl_d_mid_questions_changes_nothing(self):
        before = self.tree()
        rc, out = self.run_cli([], answers=["MyZero"])      # runs out of answers: EOFError
        self.assertEqual(rc, 130)
        self.assertEqual(self.tree(), before)
        self.assertIn("Nothing was changed", out)

    def test_existing_config_offers_to_keep_it_and_skips_the_rest(self):
        self.host.write(inst.CONF, b"# mine\n", 0o644)
        self.host.write(inst.PASSPHRASE, b"already-set\n", 0o600)
        rc, out = self.run_cli([], answers=["1", "y", "n", "y"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.host.read(inst.CONF), b"# mine\n")
        self.assertNotIn("SSID", out.split("Found an existing")[1].split("Here's")[0])

    def test_an_existing_password_can_be_kept_when_rewriting_the_config(self):
        self.host.write(inst.CONF, b"# old\n", 0o644)
        self.host.write(inst.PASSPHRASE, b"already-set\n", 0o600)
        rc, out = self.run_cli([], answers=["2", "NewOne", "3", "", "2", "", "y", "n", "y"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.host.read(inst.PASSPHRASE), b"already-set\n")
        self.assertIn(b"NewOne", self.host.read(inst.CONF))

    def test_choose_ignores_nonsense_and_takes_the_default_on_enter(self):
        answers = iter(["9", "abc", ""])
        io = inst.IO(lambda prompt: next(answers), None, self.said.append)
        self.assertEqual(io.choose("Pick", [("a", "A"), ("b", "B")], default=2), "b")
        self.assertEqual(sum("type a number" in s for s in self.said), 2)


class PreflightLinkTests(unittest.TestCase):
    def test_missing_tools_carry_a_machine_readable_code(self):
        f = facts()
        f.tools["hostapd"] = False
        codes = [x.code for x in ctl.judge(f)]
        self.assertIn("missing:hostapd", codes)
        self.assertEqual([c for c in codes if c.startswith("missing:")], ["missing:hostapd"])

    def test_every_installable_package_maps_to_a_real_tool_check(self):
        self.assertEqual(set(inst.PACKAGES), {"hostapd", "dnsmasq", "iw"})
        for tool in inst.PACKAGES:
            self.assertIn(tool, ctl._TOOLS)


class RepoFilesTests(unittest.TestCase):
    def test_the_files_the_installer_ships_exist_and_the_hook_is_what_we_think(self):
        for rel in ("systemd/ghostap.service", "nm-dispatcher/90-ghostap",
                    "ghostap/controller.py", "ghostap.toml.example"):
            self.assertTrue((ROOT / rel).exists(), rel)
        self.assertIn("/usr/local/bin/ghostap", (ROOT / "systemd/ghostap.service").read_text())
        self.assertIn(inst.LINK, (ROOT / "nm-dispatcher/90-ghostap").read_text())


if __name__ == "__main__":
    unittest.main()
