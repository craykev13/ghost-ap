# GhostAP: first hardware test on ZeroFi

Goal: confirm on real systemd, NetworkManager and hostapd the things the fake system can only
assume. This installs no boot service. Keep your working `wifi-or-ap.service` until GhostAP
replaces it, and never enable both.

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
Shorthand below: `G="sudo python3 $HOME/ghost-ap/ghostap/controller.py"` and `-c /etc/ghostap/fast.toml`.

## A. No network risk. Do these first
1. `$G test --dry-run -c /etc/ghostap/fast.toml`: read it; nothing should say ERROR.
2. `sudo python3 ~/ghost-ap/tools/check_rescue_timer.py --config /etc/ghostap/fast.toml`
   Every line must PASS. **Stop at the first FAIL and send me the whole output.**

## B. Revert path (do not commit)
1. Hotspot ON, SSH in over it. Run `$G test -c /etc/ghostap/fast.toml`. SSH freezes: expected.
2. Phone: hotspot OFF, join ZeroFi, SSH to `192.168.4.1` (you have about 60 s).
3. `$G status -c /etc/ghostap/fast.toml` should show AP mode, `UNCOMMITTED`, seconds left,
   `rescue timer: armed`. Also `cat /run/ghostap/ghostap.log`.
4. Do nothing. ZeroFi disappears at about 60 s. Turn the hotspot back ON.
5. **Write down how long the Pi takes to rejoin the hotspot on its own.** This is the one thing I
   can't predict (does NetworkManager keep retrying?). If it hasn't rejoined after about 1 minute, tell me.
6. Back in over the hotspot:
   `journalctl -u ghostap-rescue -o cat --since -10min`  (expect "never committed" and, because the
   hotspot was off at the deadline, "did NOT reconnect", exit status 3: correct here);
   `systemctl list-timers | grep ghostap` (nothing); `pgrep -a hostapd` (nothing);
   `ip -4 addr show wlan0` (no 192.168.4.1).

## C. Commit path
Same as B, but before the deadline run `$G test --commit -c /etc/ghostap/fast.toml`.
Expect: "committed", `rescue timer: not armed`, and ZeroFi still up well past the old deadline.
Then `$G stop -c /etc/ghostap/fast.toml`, hotspot ON, and confirm it rejoins.

## D. Controller killed mid-switch (the scenario you flagged)
Hotspot ON, SSH in, then:
```
sudo bash -c "python3 $HOME/ghost-ap/ghostap/controller.py test -c /etc/ghostap/fast.toml & sleep 1.5; kill -9 \$!"
```
Repeat with `sleep 0.5` and `sleep 3`. Expect: a stray hostapd may be up for a moment, then at
about 60 s the rescue timer fires and the Pi returns to client mode. Turn the hotspot ON and
check as in B.6. `cat /run/ghostap/ghostap.log` shows the last thing the controller did before it died.

## If anything looks wrong, send me
`cat /run/ghostap/ghostap.log`, `journalctl -u ghostap-rescue -o cat --since -15min`,
`systemctl list-timers --all | grep ghostap`, `nmcli device status`, `pgrep -a 'hostapd|dnsmasq'`

## Not covered yet
Boot-time behaviour, the installer, and the `KillMode=process` service file (all arrive with the installer).
