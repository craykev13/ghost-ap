# GhostAP: hardware test checklist

Confirms, on real systemd, NetworkManager and hostapd, the things the automated tests (which
use a simulated system) can only assume. Run it on each new board or OS release.

Throughout, the **fallback network** is the access point GhostAP raises (`ssid` in
`/etc/ghostap/ghostap.toml`, `ZeroFi` by default) and the **known WiFi** is the network the Pi
normally joins (a phone hotspot works well, because you can switch it off on demand).

Stages A to D install no boot service. They are for testing a checkout before installing it.
If an older custom WiFi-or-AP script is enabled on the Pi, disable it first: never run two
of these at once. Stage E and later use the installed setup (`sudo python3 install.py`).

## Verified so far

| Board | OS | Result |
| --- | --- | --- |
| Pi Zero W | Raspberry Pi OS 13 (Trixie) | A to E, F1 to F7; failover AP re-checked after a full `apt upgrade` (kernel 6.18.60, new WiFi firmware) |
| Pi 3B+ | Raspberry Pi OS Lite, 64-bit | Fresh install from the installer; E1 and E2 (boot fallback, failover, rejoin); the admin page, F6 (logs) and F7 (link check) |

Pi 4 and Pi 5 are expected to work but have not been run through this list yet.

## Setup (once)

```
cd ~/ghost-ap && git pull
sudo mkdir -p /etc/ghostap
sudo cp ghostap.toml.example /etc/ghostap/ghostap.toml
echo 'YOUR_AP_PASSWORD' | sudo tee /etc/ghostap/passphrase >/dev/null
sudo chmod 600 /etc/ghostap/passphrase
# short timers so each run takes about a minute:
sed 's/^live_timeout = .*/live_timeout = 30/' /etc/ghostap/ghostap.toml | sudo tee /etc/ghostap/fast.toml >/dev/null
```

Shorthand below: `G="sudo python3 $HOME/ghost-ap/ghostap/controller.py"`, with
`-c /etc/ghostap/fast.toml` for the short-timer config.

## A. No network risk (do these first)

1. `$G test --dry-run -c /etc/ghostap/fast.toml`. Read the output; nothing should say ERROR.
2. `sudo python3 ~/ghost-ap/tools/check_rescue_timer.py --config /etc/ghostap/fast.toml`.
   Every line must PASS. Stop at the first FAIL and capture the whole output (see
   [Reporting a failure](#reporting-a-failure)).

## B. Revert path (do not commit)

1. Known WiFi on, and SSH in over it. Run `$G test -c /etc/ghostap/fast.toml`. The SSH session
   freezes: expected.
2. Switch the known WiFi off (for a phone hotspot, turn it off), join the fallback network, and
   SSH to `192.168.4.1`. You have about 60 s.
3. `$G status -c /etc/ghostap/fast.toml` should show AP mode, `UNCOMMITTED`, seconds left, and
   `rescue timer: armed`. Also check `cat /run/ghostap/ghostap.log`.
4. Do nothing. The fallback network disappears at about 60 s. Turn the known WiFi back on.
5. Note how long the Pi takes to rejoin the known WiFi by itself. It depends on NetworkManager
   retrying, so it is worth recording per board. If it has not rejoined after about 1 minute,
   treat that as a failure.
6. Once back in over the known WiFi, check:
   - `journalctl -u ghostap-rescue -o cat --since -10min` shows "never committed" and, because the
     known WiFi was off at the deadline, "did NOT reconnect", exit status 3. That is correct here.
   - `systemctl list-timers | grep ghostap` shows nothing.
   - `pgrep -a hostapd` shows nothing.
   - `ip -4 addr show wlan0` has no `192.168.4.1`.

## C. Commit path

Same as B, but before the deadline run `$G test --commit -c /etc/ghostap/fast.toml`.
Expect "committed", `rescue timer: not armed`, and the fallback network still up well past the
old deadline. Then run `$G stop -c /etc/ghostap/fast.toml`, switch the known WiFi on, and confirm
the Pi rejoins it.

## D. Controller killed mid-switch

With the known WiFi on and an SSH session open:

```
sudo bash -c "python3 $HOME/ghost-ap/ghostap/controller.py test -c /etc/ghostap/fast.toml & sleep 1.5; kill -9 \$!"
```

Repeat with `sleep 0.5` and `sleep 3`. Expect a stray hostapd to be up for a moment, then, at
about 60 s, the rescue timer fires and the Pi returns to client mode. Switch the known WiFi on
and check as in B.6. `cat /run/ghostap/ghostap.log` shows the last thing the controller did
before it died.

## Reporting a failure

When a stage fails, capture:

```
cat /run/ghostap/ghostap.log
journalctl -u ghostap-rescue -o cat --since -15min
systemctl list-timers --all | grep ghostap
nmcli device status
pgrep -a 'hostapd|dnsmasq'
```

and include it, with the board and OS, in the issue (there are templates for bug reports and
hardware compatibility reports). The admin page's **Logs** tab shows most of
this from a browser, and **Copy** puts it on the clipboard.

## E. Boot fallback and failover

The installer (`sudo python3 install.py`) does all of the following. By hand, it is:

Install the boot service and the NetworkManager hook, and turn failover on:

```
sudo ln -sf ~/ghost-ap/ghostap/controller.py /usr/local/bin/ghostap
sudo chmod +x ~/ghost-ap/ghostap/controller.py
sudo cp ~/ghost-ap/systemd/ghostap.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ghostap.service
sudo install -m 755 -o root -g root ~/ghost-ap/nm-dispatcher/90-ghostap /etc/NetworkManager/dispatcher.d/90-ghostap
sudo sed -i 's/^failover_grace = .*/failover_grace = 120/' /etc/ghostap/ghostap.toml
```

Make logs survive reboots first (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md); Raspberry Pi OS
keeps them in RAM by default). The installer offers this.

**E1. Boot fallback.** Known WiFi off, reboot the Pi, and wait 3 to 4 minutes. The fallback network
should appear. Join it from a phone and a laptop and SSH to `192.168.4.1`. `sudo ghostap status`
says `mode: AP`. Then switch the known WiFi on and reboot: the Pi should join it by itself (about
8 s after the radio is ready).

**E2. Failover after boot.** With the Pi on the known WiFi, switch it off. After about 2.5 minutes
(120 s grace plus a 30 s last look) the fallback network appears. Check that it is still up
5 minutes later, join it from every device you have, and run `pgrep -a hostapd` (a process must be
listed). Every AP start also writes radio snapshots to `/var/lib/ghostap/ap-snapshots.log`, one
when the AP comes up and one 90 s later. `sudo ghostap snapshot` takes one at any time.

```
sudo tail -60 /var/lib/ghostap/ap-snapshots.log
```

To go back to the known WiFi, switch it on and reboot. The AP never switches back by itself,
on purpose.

**E3. If the fallback network doesn't show up or doesn't last.** Reboot with the known WiFi on, then:

```
sudo tail -120 /var/lib/ghostap/ap-snapshots.log
sudo journalctl -b -1 --no-pager | grep -iE 'ghostap|hostapd|dnsmasq|Killing process'
```

(`-b -1` is the previous boot, so it needs the persistent journal.)

## F. Admin page

Needs the page installed (`sudo python3 install.py`, answer yes to the admin page).

**F1. Helper is in place.** `systemctl is-active ghostap-apply.path` says `active`, and
`ls -ld /run/ghostap-gui` shows the page's folder. The Settings tab's "Try" card must not say the
helper is missing.

**F2. Save a harmless change** (for example `boot_timeout`). `sudo grep boot_timeout /etc/ghostap/ghostap.toml`
shows it, the comments in the file are still there, and `ghostap.toml.bak` holds the old file.

**F3. Try the saved settings** (with the Pi within reach). On the known WiFi, open Settings, press
"Try the saved settings", and confirm. The page stops responding. Join the fallback network, open
`http://192.168.4.1:8480`, sign in, and tap **Keep this**. `sudo ghostap status` should show
`mode: AP`. Revert with `sudo ghostap stop`, or reboot with the known WiFi on.

**F4. Walk away.** Repeat F3 but do nothing. After about `live_timeout` plus 30 s the Pi should
rejoin the known WiFi on its own. If it doesn't, run `sudo ghostap recover`.

**F5. If "Try" does nothing:** `journalctl -u ghostap-apply.service -n 40 --no-pager`.

**F6. Logs tab.**
- "Recent activity" shows the current boot and recent tests (`ghostap`, `hostapd`, `dnsmasq` lines).
- "Before the last reboot" shows the boot before this one. Right after a first install it says
  that boot wasn't saved, and it works from the next reboot on. It needs the persistent journal.
- "Last live test" and "Radio snapshots" read the files under `/run/ghostap` and `/var/lib/ghostap`.

If a view says "Couldn't read the system log", run `journalctl -u ghostap-gui -n 20 --no-pager`.
The page reads the journal from inside its sandbox, which is the one part the automated tests
cannot cover.

**F7. Link check.** On the Status tab, while joined to the known WiFi, the Connection card should say
"Router <address> is answering." `sudo ghostap status` prints a `link:` line too, and after a
reboot Recent activity has a `link check:` line. If the card says "couldn't check if it answers",
the page's sandbox is not allowed to run `ping`; the command-line and boot-log checks still work.
Switch the known WiFi off and on again to see the line change (it can lag by about 15 s).

**F8. Danger zone.** Needs the apply helper (F1). At the bottom of the Settings tab:
- Press **Restart...**: the button for the confirmation stays greyed out until the Pi's name is typed
  exactly (capitals count; a wrong or partial name does nothing). **Cancel** closes it.
- Type the name, confirm: the page says it is restarting, the Pi goes quiet, then comes back
  and the page works again after a minute or two (`journalctl -b -1 | grep "asked to reboot"`).
- With a test waiting (F3, before Keep or Revert), both buttons are replaced by a note.
- **Shut down...** works the same way but leaves the Pi off until power is cycled, so try it only
  with the Pi within reach.

**After a kernel or firmware update.** `sudo apt full-upgrade` can change the WiFi firmware. After
one, repeat E2 (switch the known WiFi off and confirm the fallback network appears) before relying
on the Pi unattended.

## Not covered yet

- Pis using dhcpcd or systemd-networkd (unsupported; `ghostap preflight` says so).
- Returning to the known WiFi by itself after a failover (today it takes a reboot).
- Pi 4 and Pi 5.
