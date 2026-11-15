# GhostAP troubleshooting

Things that actually went wrong on a Pi Zero W (brcmfmac WiFi), and what fixed them.

## iPhone says "Incorrect password" but other devices join fine
iOS shows that message for **any** failed WPA handshake, not only a wrong password. Look at what
hostapd saw: set `hostapd_debug = true` in `/etc/ghostap/ghostap.toml`, reboot or re-raise the AP,
try to join, then run `sudo journalctl -t hostapd --no-pager | tail -40`. A good join shows
`pairwise key handshake completed (RSN)`.

The most common real cause was **a leftover NetworkManager hotspot profile**. NetworkManager's own
hotspot mode fights hostapd and iPhones refuse to join it. Check:
```
nmcli -t -f NAME,TYPE,DEVICE connection show --active
pgrep -a hostapd
```
If the active profile on wlan0 is a hotspot (for example `ZeroFi-Hotspot`) and there is no hostapd
process, delete it: `sudo nmcli connection delete ZeroFi-Hotspot`. Log lines mentioning
`nm-dnsmasq-wlan0` also mean NetworkManager, not GhostAP, is running the AP.

## ZeroFi shows up in a WiFi analyzer app, but phones and laptops can't see or join it
Beacons from a dead AP: the firmware keeps announcing it for a while after hostapd is gone.
Check `pgrep -a hostapd`. Fixed in the current version: the failover job is started with
`KillMode=process`, so systemd no longer kills hostapd and dnsmasq when that job finishes.
`sudo ghostap snapshot` records the radio state so you can see which case you are in.

## Logs disappear after a reboot
Raspberry Pi OS ships `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`, which keeps
the journal in RAM and overrides `/etc/systemd/journald.conf`. Override it with a later-sorting file:
```
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\n' | sudo tee /etc/systemd/journald.conf.d/99-persistent.conf
sudo systemctl restart systemd-journald
sudo journalctl --flush
ls /var/log/journal        # a long hex folder name should appear
```

## `sudo ghostap` says "command not found"
Copying `controller.py` strips its executable bit. Run `chmod +x ~/ghost-ap/ghostap/controller.py`.
The boot service and the NetworkManager hook call it through `python3`, so boot is not affected.

## The link is flaky after leaving AP mode
Seen on the Pi Zero W: after the AP-to-client switch the link is sometimes slow or dead until a
reboot. GhostAP signals hostapd **once** and waits (a second signal during teardown wedges the
firmware: `-512` in `dmesg`). Do not unbind or reload the brcmfmac driver: `wlan0` can disappear
until the Pi is power-cycled. Rebooting is the reliable way back to client mode.

## Where to look
| What | Where |
| --- | --- |
| Radio snapshots (AP up, +90 s, manual) | `/var/lib/ghostap/ap-snapshots.log` |
| Controller log for the current boot | `/run/ghostap/ghostap.log` |
| hostapd handshake steps | `journalctl -t hostapd` (needs `hostapd_debug = true`) |
| Failover job | `journalctl -u ghostap-failover` |
| Current mode and timers | `sudo ghostap status` |
