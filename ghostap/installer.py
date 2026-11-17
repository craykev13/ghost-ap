#!/usr/bin/env python3
"""GhostAP installer. Standard library only, so it runs on a bare Pi Zero W.

    sudo python3 install.py              # ask a few questions, show the plan, then install
    sudo python3 install.py --dry-run    # show exactly what it WOULD do, change nothing
    sudo python3 install.py --uninstall  # undo what the installer did (keeps your config)

What it deliberately does NOT do: touch your live network. It installs files, enables the
boot service and leaves the WiFi connection you are using right now alone. GhostAP takes
over at the next boot (or when you run `sudo ghostap test`, which reverts itself).

How it is built, so it can be tested without a Pi:
  * build_plan() turns your answers plus what is already on the machine into a list of
    Steps. It only LOOKS at the machine. --dry-run stops here.
  * apply_plan() performs the steps and writes a manifest as it goes, so a half-finished
    install can still be rolled back.
  * uninstall() reads the manifest and reverses it.
All file and command access goes through Host, which tests point at a temp folder.
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _controller():
    """The controller module. Reuses an already-loaded copy so both agree on classes."""
    mod = sys.modules.get("ghostap_controller")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        "ghostap_controller", Path(__file__).resolve().parent / "controller.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod            # dataclasses needs this
    spec.loader.exec_module(mod)
    return mod


# Where things go on the Pi (all absolute; Host maps them under a test root).
CONF_DIR = "/etc/ghostap"
CONF = f"{CONF_DIR}/ghostap.toml"
PASSPHRASE = f"{CONF_DIR}/passphrase"
LINK = "/usr/local/bin/ghostap"
SERVICE = "/etc/systemd/system/ghostap.service"
DISPATCHER = "/etc/NetworkManager/dispatcher.d/90-ghostap"
GUI_PASSWORD = f"{CONF_DIR}/gui-password"
GUI_SERVICE = "/etc/systemd/system/ghostap-gui.service"
GUI_UNIT = "ghostap-gui.service"
DEFAULT_GUI_PORT = 8480
JOURNAL = "/etc/systemd/journald.conf.d/99-persistent.conf"
STATE_DIR = "/var/lib/ghostap"
MANIFEST = f"{STATE_DIR}/install-manifest.json"
BACKUPS = f"{STATE_DIR}/backups"
LEGACY_SERVICE = "wifi-or-ap.service"
PACKAGES = {"hostapd": "hostapd", "dnsmasq": "dnsmasq-base", "iw": "iw"}
JOURNAL_TEXT = "[Journal]\nStorage=persistent\n"
MANIFEST_VERSION = 1


class InstallError(Exception):
    pass


# ---- talking to the machine ---------------------------------------------------------

def _default_runner(cmd, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


class Host:
    """Files and commands, in one place. `root` lets tests install into a temp folder;
    `runner` lets them record commands instead of running them."""

    def __init__(self, root: str = "/", runner=None):
        self.root = Path(root)
        self.runner = runner or _default_runner

    def p(self, path: str) -> Path:
        return self.root / str(path).lstrip("/")

    def exists(self, path):
        return self.p(path).exists() or self.p(path).is_symlink()

    def is_symlink(self, path):
        return self.p(path).is_symlink()

    def readlink(self, path):
        return os.readlink(self.p(path))

    def read(self, path) -> bytes:
        return self.p(path).read_bytes()

    def mode(self, path) -> int:
        return self.p(path).stat().st_mode & 0o7777

    def sha(self, path):
        h = hashlib.sha256()
        h.update(self.read(path))
        return h.hexdigest()

    def mkdir(self, path):
        """Create a folder and any missing parents. Returns the ones that were new."""
        made, cur = [], self.p(path)
        while not cur.exists() and cur != cur.parent:
            made.append(cur)
            cur = cur.parent
        self.p(path).mkdir(parents=True, exist_ok=True)
        return [self._unp(m) for m in reversed(made)]

    def _unp(self, real: Path) -> str:
        return "/" + str(real.relative_to(self.root)) if self.root != Path("/") else str(real)

    def write(self, path, data: bytes, mode: int):
        """Atomic: a crash leaves the old file or the new one, never half of one."""
        dst = self.p(path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".ghostap-tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()

    def symlink(self, path, target):
        dst = self.p(path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".ghostap-tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, dst)

    def remove(self, path):
        p = self.p(path)
        if p.is_symlink() or p.is_file():
            p.unlink()

    def rmtree(self, path):
        shutil.rmtree(self.p(path), ignore_errors=True)

    def rmdir_if_empty(self, path):
        try:
            self.p(path).rmdir()
        except OSError:
            pass

    def make_executable(self, real_path: Path):
        try:
            real_path.chmod(real_path.stat().st_mode | 0o111)
        except OSError:
            pass

    def run(self, cmd, check=True, env=None):
        rc, out = self.runner(tuple(cmd), env) if env else self.runner(tuple(cmd))
        if check and rc != 0:
            raise InstallError(f"{' '.join(cmd)} failed ({rc}): {out}")
        return rc, out


# ---- answers and plan -----------------------------------------------------------------

@dataclass
class Choices:
    interface: str = "wlan0"
    ssid: str = "ZeroFi"
    passphrase: str | None = None        # None = keep the passphrase file that is there
    generated: bool = False              # True when WE made the password up (so we show it once)
    country: str = "US"
    keep_config: bool = False            # True = leave an existing config untouched
    failover: bool = True                # also raise the AP when WiFi drops after boot
    failover_grace: int = 120
    boot_service: bool = True
    persistent_journal: bool = False
    hostapd_debug: bool = False
    disable_legacy: bool = True          # turn off the old wifi-or-ap.service if enabled
    disable_system_daemons: bool = True  # turn off a stray system hostapd/dnsmasq service
    admin_page: bool = False             # install the web admin page (the questions turn it on)
    admin_password: str | None = None    # None = keep the admin password that is there
    admin_generated: bool = False        # True when WE made the admin password up
    gui_port: int = DEFAULT_GUI_PORT


@dataclass
class Step:
    kind: str                 # packages | mkdir | file | link | cmd
    target: str               # what it touches (a path, a service name, ...)
    summary: str              # one line for the plan screen
    status: str = "new"       # new | replace | same
    data: bytes = b""
    mode: int = 0o644
    link_to: str = ""
    cmd: tuple = ()
    undo: tuple = ()
    check: bool = True
    pkgs: tuple = ()
    keep_on_uninstall: bool = False
    secret: bool = False


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    findings: list = field(default_factory=list)

    def changes(self):
        return [s for s in self.steps if s.status != "same"]


def _toml_str(value: str) -> str:
    return json.dumps(value)        # a JSON string is a valid TOML basic string


def render_config(c: Choices) -> str:
    """A compact config with just the answers; every other setting keeps its default."""
    ctl = _controller()
    stamp = datetime.date.today().isoformat()
    lines = [
        f"# Written by the GhostAP installer on {stamp}.",
        "# Anything not listed here uses its default. Every available setting is in",
        "# ghostap.toml.example in the repo, and `ghostap schema` describes each one.",
        "",
        "[network]",
        f"interface = {_toml_str(c.interface)}",
        "",
        "[access_point]",
        f"ssid = {_toml_str(c.ssid)}",
        f"country = {_toml_str(c.country)}",
        f"passphrase_file = {_toml_str(PASSPHRASE)}",
    ]
    if c.hostapd_debug:
        lines.append("hostapd_debug = true")
    default_addr = next(s.default for s in ctl.SETTINGS if s.key == "address")
    net = ctl.ipaddress.IPv4Interface(default_addr).network
    first = net.network_address + 10
    last = net.network_address + 50
    lines += ["", "[dhcp]", f"start = {_toml_str(str(first))}", f"end = {_toml_str(str(last))}"]
    if c.failover:
        lines += ["", "[recovery]", f"failover_grace = {c.failover_grace}"]
    if c.admin_page:
        lines += ["", "[gui]", "enabled = true"]
        if c.gui_port != DEFAULT_GUI_PORT:
            lines.append(f"port = {c.gui_port}")
    return "\n".join(lines) + "\n"


def check_config_text(text: str) -> None:
    """Run the controller's own validation on the config we're about to write."""
    ctl = _controller()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ghostap.toml"
        path.write_text(text)
        try:
            ctl.load_config(path, secrets=False)
        except ctl.GhostAPError as e:
            raise InstallError(f"those answers make an invalid config: {e}")


def check_passphrase(p: str) -> str | None:
    if not (8 <= len(p) <= 63 and p.isascii() and p.isprintable()):
        return "the password must be 8 to 63 plain characters (letters, numbers, symbols)"
    return None


def generate_passphrase() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"        # no 0/O/1/l/i lookalikes
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


def _same_file(host: Host, path: str, data: bytes, mode: int) -> bool:
    if not host.exists(path) or host.is_symlink(path):
        return False
    return host.read(path) == data and host.mode(path) == mode


def build_plan(host: Host, c: Choices, facts, findings, repo: Path = REPO) -> Plan:
    """Turn answers + the machine's current state into steps. Looks, never changes."""
    plan = Plan(findings=findings)
    add = plan.steps.append

    # 1. folders first: the install record lives in one of them
    for d in (CONF_DIR, STATE_DIR):
        if not host.exists(d):
            add(Step("mkdir", d, f"Create folder {d}", mode=0o755))

    # 2. packages: only what is actually missing
    missing = [t for t in PACKAGES if not facts.tools.get(t, True)]
    if missing:
        pkgs = tuple(sorted({PACKAGES[t] for t in missing}))
        add(Step("packages", "apt", f"Install {', '.join(pkgs)} (needs internet)", pkgs=pkgs,
                 cmd=("apt-get", "install", "-y", "--no-install-recommends", *pkgs)))
        if "hostapd" in missing:
            # the package may switch its own service on; GhostAP runs its own hostapd
            add(Step("cmd", "hostapd.service", "Make sure the system hostapd service stays off",
                     cmd=("systemctl", "disable", "hostapd.service"), check=False))

    # 3. config and password
    conf_exists = host.exists(CONF)
    if c.keep_config and conf_exists:
        add(Step("file", CONF, f"Keep your existing config {CONF}", status="same",
                 keep_on_uninstall=True))
        plan.notes.append("Your existing config is untouched, so the SSID / country / failover "
                          "answers above don't apply to it. Edit it yourself if you want them.")
        if c.failover and not _config_has_failover(host):
            plan.notes.append("Failover is off in your config. To turn it on, set "
                              f"failover_grace = {c.failover_grace} under [recovery] "
                              f"in {CONF}.")
    else:
        text = render_config(c)
        check_config_text(text)
        data = text.encode()
        same = _same_file(host, CONF, data, 0o644)
        status = "same" if same else "replace" if conf_exists else "new"
        add(Step("file", CONF, f"Write config {CONF}", status=status, data=data, mode=0o644,
                 keep_on_uninstall=True))
    if c.passphrase is not None:
        err = check_passphrase(c.passphrase)
        if err:
            raise InstallError(err)
        data = (c.passphrase + "\n").encode()
        same = _same_file(host, PASSPHRASE, data, 0o600)
        status = "same" if same else "replace" if host.exists(PASSPHRASE) else "new"
        add(Step("file", PASSPHRASE, f"Save the AP password in {PASSPHRASE} (private, mode 600)",
                 status=status, data=data, mode=0o600, keep_on_uninstall=True, secret=True))
    elif not host.exists(PASSPHRASE):
        raise InstallError(f"no AP password given and {PASSPHRASE} doesn't exist yet")
    else:
        add(Step("file", PASSPHRASE, f"Keep the existing AP password in {PASSPHRASE}",
                 status="same", keep_on_uninstall=True))

    # 4. the command
    ctl_path = str(repo / "ghostap" / "controller.py")
    linked = host.is_symlink(LINK) and host.readlink(LINK) == ctl_path
    add(Step("link", LINK, f"Link {LINK} -> {ctl_path}", link_to=ctl_path,
             status="same" if linked else "replace" if host.exists(LINK) else "new"))
    plan.notes.append("The command is a link into this folder, so keep it where it is. "
                      "`git pull` here updates GhostAP.")

    # 5. boot service + failover hook
    if c.boot_service:
        unit = (repo / "systemd" / "ghostap.service").read_bytes()
        same = _same_file(host, SERVICE, unit, 0o644)
        unit_changed = not same
        add(Step("file", SERVICE, f"Install the boot service {SERVICE}", data=unit, mode=0o644,
                 status="same" if same else "replace" if host.exists(SERVICE) else "new"))
        if c.admin_page:
            unit_changed = _plan_admin_unit(host, c, plan, repo) or unit_changed
        if unit_changed:
            add(Step("cmd", "systemd", "Reload systemd", cmd=("systemctl", "daemon-reload")))
        enabled = host.run(("systemctl", "is-enabled", "ghostap.service"), check=False)[1]
        add(Step("cmd", "ghostap.service", "Enable GhostAP at boot (it does not start now)",
                 cmd=("systemctl", "enable", "ghostap.service"),
                 undo=("systemctl", "disable", "ghostap.service"),
                 status="same" if enabled.startswith("enabled") else "new"))
        if c.admin_page:
            _plan_admin_enable(host, c, plan)
        legacy_on = host.run(("systemctl", "is-enabled", LEGACY_SERVICE),
                             check=False)[1].startswith("enabled")
        if legacy_on and c.disable_legacy:
            add(Step("cmd", LEGACY_SERVICE,
                     "Disable the old wifi-or-ap.service (two boot services must not both run; "
                     "takes effect next boot)",
                     cmd=("systemctl", "disable", LEGACY_SERVICE),
                     undo=("systemctl", "enable", LEGACY_SERVICE), check=False))
    if c.admin_page and not c.boot_service:
        if _plan_admin_unit(host, c, plan, repo):
            add(Step("cmd", "systemd", "Reload systemd", cmd=("systemctl", "daemon-reload")))
        _plan_admin_enable(host, c, plan)
    if c.failover:
        hook = (repo / "nm-dispatcher" / "90-ghostap").read_bytes()
        same = _same_file(host, DISPATCHER, hook, 0o755)
        add(Step("file", DISPATCHER, "Install the NetworkManager hook that triggers failover",
                 data=hook, mode=0o755,
                 status="same" if same else "replace" if host.exists(DISPATCHER) else "new"))

    # 6. optional extras
    if c.persistent_journal:
        same = _same_file(host, JOURNAL, JOURNAL_TEXT.encode(), 0o644)
        add(Step("file", JOURNAL, "Keep system logs across reboots (journal on disk, not RAM)",
                 data=JOURNAL_TEXT.encode(), mode=0o644,
                 status="same" if same else "replace" if host.exists(JOURNAL) else "new"))
        if not same:
            add(Step("cmd", "journald", "Restart the log service",
                     cmd=("systemctl", "restart", "systemd-journald"), check=False))
    if c.disable_system_daemons:
        for svc in ("hostapd", "dnsmasq"):
            if facts.enabled.get(svc) == "enabled":
                add(Step("cmd", f"{svc}.service",
                         f"Disable the system {svc} service (GhostAP runs its own; "
                         "takes effect next boot)",
                         cmd=("systemctl", "disable", f"{svc}.service"),
                         undo=("systemctl", "enable", f"{svc}.service"), check=False))
    if facts.ap_profiles:
        plan.notes.append("A leftover NetworkManager hotspot profile exists "
                          f"({', '.join(facts.ap_profiles)}). The installer leaves it alone "
                          "because deleting it can't be undone. If you don't use it: "
                          "sudo nmcli connection delete '<name>'")
    return plan


def _plan_admin_unit(host: Host, c: Choices, plan: Plan, repo: Path) -> bool:
    """The service file and the admin password. (Enabling/starting comes after the reload.)
    Returns True when the service file itself is new or changed, i.e. systemd must reload."""
    ctl = _controller()
    gui = ctl._gui_module()
    unit = (repo / "systemd" / "ghostap-gui.service").read_bytes()
    unit_same = _same_file(host, GUI_SERVICE, unit, 0o644)
    plan.steps.append(Step(
        "file", GUI_SERVICE, f"Install the admin page service {GUI_SERVICE}", data=unit,
        mode=0o644, status="same" if unit_same
        else "replace" if host.exists(GUI_SERVICE) else "new"))
    exists = host.exists(GUI_PASSWORD)
    if c.admin_password is not None:
        err = check_admin_password(c.admin_password)
        if err:
            raise InstallError(err)
        stored = host.read(GUI_PASSWORD).decode().strip() if exists else ""
        if exists and gui.verify_password(c.admin_password, stored) \
                and host.mode(GUI_PASSWORD) == 0o600:
            plan.steps.append(Step("file", GUI_PASSWORD, f"Keep the admin password in "
                                   f"{GUI_PASSWORD}", status="same", keep_on_uninstall=True))
        else:
            data = (gui.hash_password(c.admin_password) + "\n").encode()
            plan.steps.append(Step(
                "file", GUI_PASSWORD, f"Save the admin page password in {GUI_PASSWORD} "
                "(stored scrambled, mode 600)", data=data, mode=0o600, secret=True,
                keep_on_uninstall=True, status="replace" if exists else "new"))
    elif not exists:
        raise InstallError(f"no admin password given and {GUI_PASSWORD} doesn't exist yet")
    else:
        plan.steps.append(Step("file", GUI_PASSWORD, f"Keep the existing admin password in "
                               f"{GUI_PASSWORD}", status="same", keep_on_uninstall=True))
    if c.keep_config and host.exists(CONF) and not _config_gui_enabled(host):
        plan.notes.append("The admin page is off in your existing config. To turn it on, add "
                          f"`enabled = true` under [gui] in {CONF}.")
    return not unit_same


def _plan_admin_enable(host: Host, c: Choices, plan: Plan) -> None:
    changed = any(s.status != "same" for s in plan.steps
                  if s.target in (GUI_SERVICE, GUI_PASSWORD, CONF))
    enabled = host.run(("systemctl", "is-enabled", GUI_UNIT), check=False)[1]
    plan.steps.append(Step(
        "cmd", GUI_UNIT, "Enable the admin page at boot",
        cmd=("systemctl", "enable", GUI_UNIT), undo=("systemctl", "disable", "--now", GUI_UNIT),
        status="same" if enabled.startswith("enabled") else "new"))
    if changed:
        plan.steps.append(Step("cmd", GUI_UNIT, "(Re)start the admin page so it picks this up",
                               cmd=("systemctl", "restart", GUI_UNIT), check=False))


def check_admin_password(p: str) -> str | None:
    gui = _controller()._gui_module()
    if not (gui.MIN_PASSWORD <= len(p) <= gui.MAX_PASSWORD) or not p.isprintable():
        return f"the admin password must be {gui.MIN_PASSWORD}-{gui.MAX_PASSWORD} characters"
    return None


def generate_admin_password() -> str:
    return generate_passphrase()


def _config_gui_enabled(host: Host) -> bool:
    try:
        raw = tomllib.loads(host.read(CONF).decode())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    return raw.get("gui", {}).get("enabled") is True


def _config_has_failover(host: Host) -> bool:
    try:
        raw = tomllib.loads(host.read(CONF).decode())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    return bool(raw.get("recovery", {}).get("failover_grace", 0))


# ---- showing the plan -------------------------------------------------------------------

_LABEL = {"new": "add    ", "replace": "replace", "same": "ok     "}


def render_plan(plan: Plan, *, dry_run: bool) -> str:
    out = []
    todo = plan.changes()
    out.append("Here's what will happen:" if todo else "Everything is already in place.")
    out.append("")
    for s in plan.steps:
        if s.kind == "mkdir" and s.status == "same":
            continue
        out.append(f"  [{_LABEL[s.status]}] {s.summary}")
    for n in plan.notes:
        out.append("")
        out.append(f"  Note: {n}")
    out.append("")
    out.append("Your WiFi connection right now is not touched. GhostAP takes over at the next "
               "boot, or when you run `sudo ghostap test` (it undoes itself).")
    if dry_run:
        out.append("")
        out.append("(dry run: nothing was changed)")
    return "\n".join(out)


# ---- doing it ---------------------------------------------------------------------------

def _save_manifest(host: Host, manifest: dict) -> None:
    host.write(MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)


def _load_manifest(host: Host) -> dict | None:
    if not host.exists(MANIFEST):
        return None
    try:
        data = json.loads(host.read(MANIFEST).decode())
    except (ValueError, UnicodeDecodeError):
        raise InstallError(f"{MANIFEST} is damaged, so I can't tell what was installed")
    if data.get("version") != MANIFEST_VERSION:
        raise InstallError(f"{MANIFEST} was written by a different installer version")
    return data


def apply_plan(host: Host, plan: Plan, choices: Choices, repo: Path = REPO,
               say=lambda m: None) -> dict:
    """Perform the steps. The manifest is rewritten after every step, so if anything fails
    (or you press Ctrl-C) `--uninstall` can still undo what was done so far."""
    prior = _load_manifest(host)
    manifest = prior or {
        "version": MANIFEST_VERSION, "repo": str(repo), "entries": [],
        "choices": {k: v for k, v in vars(choices).items() if k != "passphrase"},
    }
    if prior:
        manifest["repo"] = str(repo)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    entries = manifest["entries"]

    def record(**e):
        old = next((x for x in entries if x["kind"] == e["kind"]
                    and x["target"] == e["target"]), None)
        if old and e["kind"] in ("file", "link") and old["action"] in ("created", "replaced") \
                and e["action"] != "unchanged":
            # a second run changes the file again, but "what it was before we ever touched
            # it" is still the first run's answer: keep that so uninstall goes all the way back
            for k in ("action", "backup", "old_link"):
                if k in old:
                    e[k] = old[k]
                else:
                    e.pop(k, None)
        entries[:] = [x for x in entries if x is not old]
        entries.append(e)
        _save_manifest(host, manifest)

    def backup(path) -> str:
        name = path.strip("/").replace("/", "_")
        dst = f"{BACKUPS}/{stamp}/{name}"
        host.write(dst, host.read(path), host.mode(path) if not host.is_symlink(path) else 0o644)
        return dst

    for step in plan.steps:
        say(step.summary)
        if step.kind == "packages":
            host.run(step.cmd, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
            record(kind="packages", target="apt", action="created", pkgs=list(step.pkgs))
        elif step.kind == "mkdir":
            made = host.mkdir(step.target)
            if made:
                record(kind="mkdir", target=step.target, action="created", made=made,
                       keep_on_uninstall=step.keep_on_uninstall)
        elif step.kind == "file":
            if step.status == "same":
                if not any(e["kind"] == "file" and e["target"] == step.target for e in entries):
                    record(kind="file", target=step.target, action="unchanged",
                           keep_on_uninstall=step.keep_on_uninstall)
                continue
            bak = backup(step.target) if host.exists(step.target) else None
            host.write(step.target, step.data, step.mode)
            record(kind="file", target=step.target,
                   action="replaced" if bak else "created", backup=bak,
                   sha256=host.sha(step.target), mode=step.mode,
                   keep_on_uninstall=step.keep_on_uninstall)
        elif step.kind == "link":
            if step.status == "same":
                if not any(e["kind"] == "link" and e["target"] == step.target for e in entries):
                    record(kind="link", target=step.target, action="unchanged")
                host.make_executable(Path(step.link_to))
                continue
            bak = backup(step.target) if host.exists(step.target) and \
                not host.is_symlink(step.target) else None
            old = host.readlink(step.target) if host.is_symlink(step.target) else None
            host.symlink(step.target, step.link_to)
            host.make_executable(Path(step.link_to))
            record(kind="link", target=step.target,
                   action="replaced" if (bak or old) else "created",
                   backup=bak, old_link=old, link_to=step.link_to)
        elif step.kind == "cmd":
            if step.status == "same":
                continue
            rc, out = host.run(step.cmd, check=step.check)
            if rc != 0:
                say(f"  (continuing: {' '.join(step.cmd)} returned {rc})")
            if step.undo:
                record(kind="cmd", target=step.target, action="created",
                       cmd=list(step.cmd), undo=list(step.undo))
        else:
            raise InstallError(f"unknown step kind {step.kind}")
    return manifest


def uninstall(host: Host, *, remove_config: bool = False, force: bool = False,
              say=print) -> int:
    manifest = _load_manifest(host)
    if manifest is None:
        say("Nothing to uninstall: no install record found "
            f"({MANIFEST}). If you installed by hand, see HARDWARE_TEST.md.")
        return 1
    kept = []
    for e in reversed(manifest["entries"]):
        kind, target, action = e["kind"], e["target"], e["action"]
        if kind == "packages":
            kept.append("packages left installed (other things may use them): "
                        + ", ".join(e.get("pkgs", [])))
        elif kind == "cmd":
            if e.get("undo"):
                host.run(tuple(e["undo"]), check=False)
                say(f"undid: {' '.join(e['cmd'])}")
        elif kind in ("file", "link") and action == "unchanged" \
                and not (e.get("keep_on_uninstall") and remove_config):
            # (your own settings are the exception: --remove-config means exactly that)
            kept.append(f"{target} (was already there before the installer)")
        elif kind in ("file", "link"):
            if e.get("keep_on_uninstall") and not remove_config:
                kept.append(f"{target} (your settings; use --remove-config to delete)")
                continue
            if kind == "file" and host.exists(target) and not host.is_symlink(target):
                if e.get("sha256") and host.sha(target) != e["sha256"] and not force:
                    kept.append(f"{target} (you changed it since install; use --force)")
                    continue
            if kind == "link" and host.exists(target) and \
                    (not host.is_symlink(target) or host.readlink(target) != e.get("link_to")) \
                    and not force:
                kept.append(f"{target} (no longer the link we made; use --force)")
                continue
            host.remove(target)
            if e.get("backup") and host.exists(e["backup"]):
                host.write(target, host.read(e["backup"]), host.mode(e["backup"]))
                say(f"restored your previous {target}")
            elif e.get("old_link"):
                host.symlink(target, e["old_link"])
                say(f"restored your previous link {target}")
            else:
                say(f"removed {target}")
        elif kind == "mkdir":
            for d in reversed(e.get("made", [])):
                host.rmdir_if_empty(d)
    host.run(("systemctl", "daemon-reload"), check=False)
    host.remove(MANIFEST)
    if remove_config:
        host.rmtree(BACKUPS)
    elif host.exists(BACKUPS) and any(host.p(BACKUPS).iterdir()):
        kept.append(f"{BACKUPS} (copies of files the installer replaced)")
    host.rmdir_if_empty(BACKUPS)
    host.rmdir_if_empty(STATE_DIR)
    host.rmdir_if_empty(CONF_DIR)
    if kept:
        say("")
        say("Left in place:")
        for k in kept:
            say(f"  - {k}")
    say("")
    say("Uninstalled. Your running network was not touched; the change takes full effect "
        "at the next reboot.")
    return 0


# ---- the interactive part --------------------------------------------------------------

class IO:
    """All terminal input/output, swappable so tests can type the answers."""

    def __init__(self, input_fn=input, secret_fn=getpass.getpass, out=print):
        self.input, self.secret_fn, self.out = input_fn, secret_fn, out

    def say(self, text=""):
        self.out(text)

    def ask(self, prompt, default=None, check=None):
        while True:
            shown = f"{prompt} [{default}]: " if default not in (None, "") else f"{prompt}: "
            ans = self.input(shown).strip() or (default or "")
            err = check(ans) if check else (None if ans else "please type something")
            if not err:
                return ans
            self.say(f"  {err}")

    def yes_no(self, prompt, default=True):
        hint = "Y/n" if default else "y/N"
        while True:
            ans = self.input(f"{prompt} [{hint}]: ").strip().lower()
            if not ans:
                return default
            if ans in ("y", "yes"):
                return True
            if ans in ("n", "no"):
                return False
            self.say("  please answer y or n")

    def choose(self, prompt, options, default=1):
        """Numbered list; Enter takes the default. (Numbers, not arrow keys, because arrow
        menus break over phone SSH apps.) options is a list of (value, label)."""
        self.say(prompt)
        for i, (_, label) in enumerate(options, 1):
            self.say(f"  {i}) {label}")
        while True:
            ans = self.input(f"Choose 1-{len(options)} [{default}]: ").strip() or str(default)
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return options[int(ans) - 1][0]
            self.say(f"  type a number from 1 to {len(options)}")

    def secret(self, prompt):
        return self.secret_fn(prompt)


def _check_ssid(s):
    if not s or len(s.encode()) > 32:
        return "the network name must be 1 to 32 characters"
    return None


def _check_country(s):
    return None if re.fullmatch(r"[A-Za-z]{2}", s) else "two letters, like US or GB"


def _check_grace(s):
    if not s.isdigit() or int(s) < 15:
        return "a whole number of seconds, at least 15"
    return None


def interview(io: IO, host: Host, facts, defaults: Choices | None = None,
              overrides: dict | None = None, assume_yes: bool = False) -> Choices:
    """Ask only what isn't already decided by a command-line flag."""
    c = defaults or Choices(interface=facts.iface)
    ov = overrides or {}
    has_conf = host.exists(CONF)
    has_pass = host.exists(PASSPHRASE)
    has_gui_pw = host.exists(GUI_PASSWORD)

    def given(k):
        return k in ov and ov[k] is not None

    for k in ("ssid", "country", "failover", "failover_grace", "boot_service",
              "persistent_journal", "hostapd_debug", "keep_config", "passphrase", "generated",
              "admin_page", "admin_password", "admin_generated"):
        if given(k):
            setattr(c, k, ov[k])

    if assume_yes:
        if not given("keep_config"):
            wants_new = any(given(k) for k in ("ssid", "country", "failover",
                                               "failover_grace", "passphrase"))
            c.keep_config = has_conf and not wants_new
        if c.passphrase is None and not (has_pass or c.keep_config):
            raise InstallError("--yes needs a password: add --generate-password or "
                               "--passphrase-file FILE")
        if not given("country") and facts.reg_country not in (None, "00"):
            c.country = facts.reg_country
        if not given("admin_page"):
            # unattended: only switch the page on if a password is available for it
            c.admin_page = c.admin_password is not None or has_gui_pw
        return c

    io.say()
    if has_conf and not given("keep_config"):
        c.keep_config = io.choose(
            f"Found an existing GhostAP config ({CONF}).",
            [(True, "Keep it, and only install what's missing"),
             (False, "Replace it with new answers (the old one is backed up)")], 1)
    if not c.keep_config:
        if not given("ssid"):
            c.ssid = io.ask("Name of the fallback WiFi network (SSID)", c.ssid, _check_ssid)
        if not given("passphrase"):
            opts = [("type", "Type a password"), ("gen", "Make a random one for me")]
            if has_pass:
                opts.append(("keep", f"Keep the existing one in {PASSPHRASE}"))
            how = io.choose("Password for that network?", opts, 3 if has_pass else 1)
            if how == "type":
                while True:
                    p1 = io.secret("  Password (8-63 characters, hidden): ")
                    err = check_passphrase(p1)
                    if err:
                        io.say(f"  {err}")
                    elif p1 != io.secret("  Type it again: "):
                        io.say("  those didn't match")
                    else:
                        c.passphrase = p1
                        break
            elif how == "gen":
                c.passphrase, c.generated = generate_passphrase(), True
        if not given("country"):
            guess = facts.reg_country if facts.reg_country not in (None, "00") else c.country
            c.country = io.ask("WiFi country code (sets which channels are legal)",
                               guess, _check_country).upper()
        if not given("failover"):
            c.failover = io.choose(
                "When should the fallback network appear?",
                [(False, "Only at boot, when no known WiFi is found"),
                 (True, "At boot, and also later if the WiFi drops (failover)")], 2)
        if c.failover and not given("failover_grace"):
            c.failover_grace = int(io.ask(
                "  Seconds to wait for WiFi to come back before switching", str(c.failover_grace),
                _check_grace))
    elif not has_pass and not given("passphrase"):
        raise InstallError(f"you chose to keep the config but {PASSPHRASE} doesn't exist; "
                           "run again and set a password")
    if not given("boot_service"):
        c.boot_service = io.yes_no("Start GhostAP automatically at boot?", True)
    if not given("persistent_journal"):
        c.persistent_journal = io.yes_no(
            "Keep system logs across reboots? (helps troubleshooting; Pi OS keeps them in "
            "RAM by default)", False)
    if not given("admin_page"):
        c.admin_page = io.yes_no("Install the admin web page? (a status page you open in a "
                                 "browser; password protected, works offline)", True)
    if c.admin_page and c.admin_password is None:
        opts = [("type", "Type a password"), ("gen", "Make a random one for me")]
        if has_gui_pw:
            opts.append(("keep", f"Keep the existing one in {GUI_PASSWORD}"))
        how = io.choose("Password for the admin page?", opts, 3 if has_gui_pw else 1)
        if how == "type":
            while True:
                p1 = io.secret("  Admin page password (8+ characters, hidden): ")
                err = check_admin_password(p1)
                if err:
                    io.say(f"  {err}")
                elif p1 != io.secret("  Type it again: "):
                    io.say("  those didn't match")
                else:
                    c.admin_password = p1
                    break
        elif how == "gen":
            c.admin_password, c.admin_generated = generate_admin_password(), True
    return c


def admin_page_lines(host: Host) -> list[str]:
    """Where to open the admin page, read from the config that is actually installed."""
    try:
        raw = tomllib.loads(host.read(CONF).decode())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    gui = raw.get("gui", {})
    if gui.get("enabled") is not True or not host.exists(GUI_SERVICE):
        return []
    port = gui.get("port", DEFAULT_GUI_PORT)
    name = host.run(("hostname",), check=False)[1].strip().lower()
    ips = [x for x in host.run(("hostname", "-I"), check=False)[1].split()
           if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", x)]
    gateway = str(raw.get("access_point", {}).get("address", "192.168.4.1/24")).split("/")[0]
    urls = ([f"http://{name}.local:{port}"] if name else []) + [f"http://{ip}:{port}" for ip in ips]
    out = ["", "Admin page:  " + (urls[0] if urls else f"http://<this Pi's address>:{port}")]
    out += [f"             {u}" for u in urls[1:]]
    out.append(f"  (when the Pi is running its own network: http://{gateway}:{port})")
    out.append("  Sign in with the admin password you set.")
    return out


def explain_findings(io: IO, findings) -> None:
    ctl = _controller()
    order = {ctl.BLOCK: 0, ctl.WARN: 1, ctl.INFO: 2}
    mark = {ctl.BLOCK: "[X]", ctl.WARN: "[!]", ctl.INFO: "[i]"}
    for f in sorted(findings, key=lambda x: order[x.level]):
        io.say(f"{mark[f.level]} {f.title}")
        if f.detail:
            io.say(f"      {f.detail}")
        if f.fix:
            io.say(f"      Fix: {f.fix}")


def system_check(shell, iface: str):
    """Preflight, but missing tools the installer can install are not blockers here."""
    ctl = _controller()
    facts = ctl.gather_facts(shell, iface)
    findings = []
    for f in ctl.judge(facts):
        if f.code.startswith("missing:") and f.code.split(":", 1)[1] in PACKAGES:
            tool = f.code.split(":", 1)[1]
            findings.append(ctl.Finding(ctl.INFO, f"{tool} isn't installed yet",
                                        "The installer will add it.", code=f.code))
        else:
            findings.append(f)
    return facts, findings


def run(argv=None, *, io: IO | None = None, host: Host | None = None, shell=None,
        is_root: bool | None = None) -> int:
    ctl = _controller()
    ap = argparse.ArgumentParser(prog="install.py", description="GhostAP installer")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="don't ask: use defaults and the flags below")
    ap.add_argument("--uninstall", action="store_true", help="undo what the installer did")
    ap.add_argument("--remove-config", action="store_true",
                    help="(uninstall) also delete your config and password")
    ap.add_argument("--force", action="store_true",
                    help="(uninstall) remove files even if you changed them")
    ap.add_argument("--ssid")
    ap.add_argument("--country")
    ap.add_argument("--passphrase-file", type=Path, help="read the AP password from this file")
    ap.add_argument("--generate-password", action="store_true")
    ap.add_argument("--keep-config", action="store_true", default=None)
    ap.add_argument("--failover", dest="failover", action="store_true", default=None)
    ap.add_argument("--no-failover", dest="failover", action="store_false")
    ap.add_argument("--failover-grace", type=int)
    ap.add_argument("--no-boot-service", dest="boot_service", action="store_false", default=None)
    ap.add_argument("--persistent-journal", action="store_true", default=None)
    ap.add_argument("--no-admin-page", dest="admin_page", action="store_false", default=None,
                    help="don't install the admin web page")
    ap.add_argument("--admin-password-file", type=Path,
                    help="read the admin page password from this file")
    ap.add_argument("--generate-admin-password", action="store_true")
    ap.add_argument("--interface", default="wlan0")
    args = ap.parse_args(argv)

    io = io or IO()
    host = host or Host()
    shell = shell or ctl.Shell()
    root_ok = (os.geteuid() == 0) if is_root is None else is_root
    try:
        if args.uninstall:
            if not root_ok and not args.dry_run:
                raise InstallError("run it with sudo: sudo python3 install.py --uninstall")
            return uninstall(host, remove_config=args.remove_config, force=args.force, say=io.say)

        io.say("GhostAP installer" + ("  (dry run: nothing will be changed)" if args.dry_run else ""))
        io.say()
        facts, findings = system_check(shell, args.interface)
        explain_findings(io, findings)
        if any(f.level == ctl.BLOCK for f in findings):
            io.say()
            io.say("GhostAP can't be installed on this machine as it is. Fix the [X] items "
                   "above and run this again.")
            return 1
        if not args.dry_run and not root_ok:
            raise InstallError("run it with sudo: sudo python3 install.py")

        admin_pw, admin_generated = None, False
        if args.admin_password_file:
            try:
                admin_pw = args.admin_password_file.read_text().strip()
            except OSError as e:
                raise InstallError(f"can't read {args.admin_password_file}: {e}")
        elif args.generate_admin_password:
            admin_pw, admin_generated = generate_admin_password(), True
        passphrase, generated = None, False
        if args.passphrase_file:
            try:
                passphrase = args.passphrase_file.read_text().strip()
            except OSError as e:
                raise InstallError(f"can't read {args.passphrase_file}: {e}")
        elif args.generate_password:
            passphrase = generate_passphrase()
            generated = True
        overrides = {"ssid": args.ssid, "country": args.country.upper() if args.country else None,
                     "failover": args.failover, "failover_grace": args.failover_grace,
                     "boot_service": args.boot_service,
                     "persistent_journal": args.persistent_journal,
                     "keep_config": args.keep_config, "passphrase": passphrase,
                     "generated": generated or None,
                     "admin_page": args.admin_page, "admin_password": admin_pw,
                     "admin_generated": admin_generated or None}
        choices = interview(io, host, facts, Choices(interface=args.interface), overrides,
                            assume_yes=args.yes)
        plan = build_plan(host, choices, facts, findings)
        io.say()
        io.say(render_plan(plan, dry_run=args.dry_run))
        if args.dry_run or not plan.changes():
            return 0
        io.say()
        if not args.yes and not io.yes_no("Go ahead?", False):
            io.say("Cancelled. Nothing was changed.")
            return 0
        io.say()
        try:
            apply_plan(host, plan, choices, say=lambda m: io.say(f"  - {m}"))
        except (InstallError, KeyboardInterrupt) as e:
            io.say()
            io.say(f"Stopped part-way: {e or 'interrupted'}")
            io.say("What was done is recorded. Undo it with: sudo python3 install.py --uninstall")
            return 1
        io.say()
        io.say("Done.")
        if choices.generated and choices.passphrase:
            io.say(f"The fallback network is {choices.ssid!r}, password: {choices.passphrase}")
            io.say(f"(Write it down. It's saved in {PASSPHRASE}.)")
        if choices.admin_generated and choices.admin_password:
            io.say(f"The admin page password is: {choices.admin_password}")
            io.say(f"(Write it down. Only a scrambled copy is saved, in {GUI_PASSWORD}.)")
        for line in admin_page_lines(host):
            io.say(line)
        io.say("")
        io.say("Next steps (nothing has touched your live WiFi yet):")
        io.say("  1. sudo ghostap preflight        # re-check the system")
        io.say("  2. sudo ghostap test             # tries the AP and reverts by itself")
        io.say("  3. sudo reboot                   # try the real boot fallback")
        return 0
    except InstallError as e:
        io.say(f"Error: {e}")
        return 1
    except (KeyboardInterrupt, EOFError):
        io.say()
        io.say("Cancelled. Nothing was changed.")
        return 130


if __name__ == "__main__":
    sys.exit(run())
