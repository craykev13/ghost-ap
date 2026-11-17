# GhostAP

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

Works and is tested (225 tests, plus real-hardware runs). Planned: a local web page for
changing settings, link-health checks after the AP-to-client switch, other network setups.
