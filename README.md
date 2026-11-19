<p align="center">
  <img src="assets/wordmark-banner.png" alt="GhostAP" width="480">
</p>

A Raspberry Pi that always lets you in. At boot it joins a WiFi network it already knows
(your phone's hotspot, say). If none is around, it raises its own access point so you can
still connect and SSH in. Built for a Pi Zero W riding in a bag, and works on other Pis too.

- **Boot fallback:** known WiFi first, otherwise its own network (default name `ZeroFi`).
- **Failover:** optionally raises the access point later if WiFi drops, after a grace period.
  It stays up until the next reboot, so you can always get back in.
- **Self-reverting:** every network switch arms a timer that puts things back if it goes wrong.
- **Nothing hidden:** every value is a documented setting, plus raw `hostapd`/`dnsmasq` lines
  for anything else. `ghostap schema` lists them all.

## Quick start

```
git clone https://github.com/craykev13/ghost-ap.git ~/ghost-ap     # needs access if private
cd ~/ghost-ap
python3 install.py --dry-run     # preview: checks your system, changes nothing
sudo python3 install.py          # asks a few questions, shows the plan, then installs
```

The installer only adds what's missing, never touches the WiFi you're using right now, and
can be undone with `sudo python3 install.py --uninstall` (your config and password are kept
unless you add `--remove-config`). Keep the folder where it is: `ghostap` is a link into it,
so `git pull` updates it.

Then, on the Pi:

```
sudo ghostap preflight    # will this work here? lists anything in the way, with fixes
sudo ghostap test         # tries the AP and reverts by itself
sudo reboot               # try the real boot fallback
ghostap status            # what mode is it in? (no sudo needed)
```

Unattended: `sudo python3 install.py --yes --generate-password --ssid MyPi --country US`
(see `python3 install.py --help` for every flag).

## Admin page

A small web page, served by the Pi itself and protected by a password. It uses no internet and
loads nothing from outside. It has three tabs:

- **Status:** whether the Pi is joined to a WiFi network or running its own, and whether the
  router it joined is actually answering. Also its addresses, the safety timer and failover.
- **Settings:** edit the config (network name, channel, addresses, timers and so on) without SSH.
  Saving writes the config file (your comments stay, and the previous copy is kept as
  `ghostap.toml.bak`) but does not change the live network: changes apply at the next boot
  fallback, or can be tried safely right away (below).
- **Logs:** recent activity, the boot before this one, the last live test, and the radio
  snapshots. Passwords are hidden, and **Copy** puts the lines on the clipboard.

**Try the saved settings** (under the Settings form) runs the same self-reverting test as
`sudo ghostap test`: the Pi raises its own network with your settings, and unless you join it,
open the page there and tap **Keep**, it undoes itself. The admin password and the page's own
port and address are changed over SSH, not from the page.

The page itself has no network powers. It leaves a small request file, and a separate
root-owned helper (`ghostap-apply`, installed by `install.py`) carries out one of exactly three
things: try, keep or revert.

The installer offers the page: answer yes, pick (or generate) an admin password, and it installs
and enables the page, then prints the address when it finishes. Unattended, use
`--admin-password-file FILE` or `--generate-admin-password`, and `--no-admin-page` to skip it.
`ghostap status` shows the address too.

To set it up by hand instead:

```
sudo ghostap gui-password                                  # choose the admin password
sudo nano /etc/ghostap/ghostap.toml                        # add:  [gui]  then  enabled = true
sudo cp ~/ghost-ap/systemd/ghostap-gui.service /etc/systemd/system/
sudo cp ~/ghost-ap/systemd/ghostap-apply.service ~/ghost-ap/systemd/ghostap-apply.path /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ghostap-gui ghostap-apply.path
```

Open `http://<hostname>.local:8480` (for example `http://zerofi.local:8480`) or the Pi's
IP address with `:8480`. When the Pi is running its own network, that's `http://192.168.4.1:8480`.
The port and listen address are settings under `[gui]`. Sign-in is rate-limited, the page runs
sandboxed (almost every privilege removed, rest of the system read-only), and it is plain HTTP,
so keep it on networks you trust.

After `git pull`, restart the page to run the new code: `sudo systemctl restart ghostap-gui`.
The page shows a banner when it is running older code than what is on disk.

## What it needs

Raspberry Pi OS (Bookworm or newer) with **NetworkManager**, systemd, and a WiFi chip that
supports AP mode (the Pi Zero W / 3 / 4 / 5 built-ins do). Tested on a Pi Zero W (Raspberry Pi
OS 13, Trixie) and a Pi 3B+ (Raspberry Pi OS Lite, 64-bit); see
[`HARDWARE_TEST.md`](HARDWARE_TEST.md). Pis using dhcpcd or systemd-networkd aren't supported
yet; `ghostap preflight` says so instead of guessing.

## Docs

- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md): can't see the network, logs, `ghostap preflight`
- [`HARDWARE_TEST.md`](HARDWARE_TEST.md): hardware test checklist, and which boards have passed it
- [`ghostap.toml.example`](ghostap.toml.example): every setting, generated from the code

## Status

Works and is tested (an automated test suite, plus real-hardware runs on a Pi Zero W and a Pi 3B+).

Not done yet:
- Returning to the known WiFi by itself after a failover (today it takes a reboot).
- Failover when the router stops answering while WiFi still looks connected (the link check only reports it for now).
- Automatic channel selection.
- Pis on dhcpcd or systemd-networkd.
