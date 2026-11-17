<p align="center">
  <img src="assets/wordmark-banner.png" alt="GhostAP" width="480">
</p>

A Raspberry Pi that always lets you in. At boot it joins a WiFi network it already knows
(your phone's hotspot, say). If none is around, it raises its own access point so you can
still connect and SSH in. Built for a Pi Zero W riding in a bag, and works on other Pis too.

- **Boot fallback:** known WiFi first, otherwise its own network (default name `ZeroFi`).
- **Failover:** optionally raises the access point later if WiFi drops, after a grace period.
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

## Admin page (preview)

A small web page, served by the Pi itself, that shows what it's doing: connected or running
its own network, addresses, the safety timer and failover, and a **Logs** tab (recent activity, the last boot, the last live test, radio snapshots; passwords are
hidden, and Copy puts the lines on your clipboard to paste), and a **Settings** tab where you
can edit the config (network name, channel, addresses, timers and so on) without SSH. It uses
no internet and loads nothing from outside. Saving writes the config file (your comments stay,
and the previous copy is kept as `ghostap.toml.bak`) but doesn't change the live network:
changes apply at the next boot fallback, or try them safely now with `sudo ghostap test`.
The admin password, and the admin page's own port and address, are still changed over SSH.

Under the form, **Try the saved settings** runs the same self-reverting test as
`sudo ghostap test`: the Pi raises its own network with your settings, and unless you join it,
open the page there and tap **Keep**, it undoes itself. The page itself has no network powers.
It leaves a small request file, and a separate root-owned helper (`ghostap-apply`, installed
by `install.py`) carries out one of exactly three things: try, keep or revert. The installer offers it: say yes, pick (or generate) an admin password, and it
installs and enables the page, then prints the address when it finishes. Unattended, use
`--admin-password-file FILE` or `--generate-admin-password`, and `--no-admin-page` to skip it.
`ghostap status` shows the address too.

To set it up by hand instead:

```
sudo ghostap gui-password                                  # choose the admin password
sudo nano /etc/ghostap/ghostap.toml                        # add:  [gui]  then  enabled = true
sudo cp ~/ghost-ap/systemd/ghostap-gui.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ghostap-gui
```

Open `http://<hostname>.local:8480` (for example `http://zerofi.local:8480`) or the Pi's
IP address with `:8480`. When the Pi is running its own network, that's `http://192.168.4.1:8480`.
The port and listen address are settings under `[gui]`. Sign-in is rate-limited, the page runs
sandboxed (almost every privilege removed, rest of the system read-only), and it is plain HTTP,
so keep it on networks you trust.

## What it needs

Raspberry Pi OS (Bookworm or newer) with **NetworkManager**, systemd, and a WiFi chip that
supports AP mode (the Pi Zero W / 3 / 4 / 5 built-ins do). Checked so far on a Pi Zero W
running Raspberry Pi OS 13 (Trixie). Pis using dhcpcd or systemd-networkd aren't supported
yet; `ghostap preflight` says so instead of guessing.

## Docs

- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md): can't see the network, logs, `ghostap preflight`
- [`HARDWARE_TEST.md`](HARDWARE_TEST.md): step-by-step checks on real hardware
- [`ghostap.toml.example`](ghostap.toml.example): every setting, generated from the code

## Status

Works and is tested (an automated test suite, plus real-hardware runs). Planned: a logs page, link-health checks after the AP-to-client switch, other network setups.
