# pi-zero-w-hid-kvm

HID-only DIY "KVM cousin": a Raspberry Pi Zero W acts as a USB keyboard+mouse for a host and
exposes a browser console; an ESP32-S3-GEEK is a WiFi status panel. **No video.**

## Architecture
```
 phone/laptop browser ──WiFi──┐
                              ▼
 ESP32-S3-GEEK ──WiFi──▶ Pi Zero W (AP 192.168.4.1, web :8080) ──USB OTG (HID gadget)──▶ Host PC
 (polls /api/status,          /dev/hidg0 keyboard, /dev/hidg1 mouse
  BOOT btn → Ctrl+Alt+Del)
```

**WiFi topology (chosen): Zero W is the access point** (SSID `pizero-kvm`, 192.168.4.1).
GEEK and your browser join it. No home network needed. (Alternative: both join home WiFi —
then set `KVM_HOST` to the Zero's LAN IP; not covered here.)

## Wiring
- Zero W **USB data port** (the one labeled "USB", *not* "PWR") → host USB-A/C (micro-USB cable/adapter).
  This port powers the Zero from the host and carries HID. Leave PWR unplugged (or power via PWR
  and accept that the Zero stays up when the host is off).
- GEEK: powered by its own USB-A plug (any USB power). No wires to the Zero.

## Zero W setup (Raspberry Pi OS Lite)
```bash
# copy the project tree to the Zero (no git remote yet), then run from the project directory:
# Lite may need: sudo apt install -y rsync
#   rsync -a pi-zero-w-hid-kvm/ pi@<zero>:~/pi-zero-w-hid-kvm/ && ssh pi@<zero>
cd ~/pi-zero-w-hid-kvm
sudo bash zero/install.sh    # ensures [all] dtoverlay=dwc2, dwc2+libcomposite modules, installs services
sudo reboot
```
Access point (Bookworm / NetworkManager) — note this takes over wlan0, do it from console/serial:
```bash
sudo nmcli con add type wifi ifname wlan0 con-name kvm-ap autoconnect yes ssid pizero-kvm \
  802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 192.168.4.1/24 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk 'change-me-please'
sudo nmcli con up kvm-ap
```
Then browse to `http://192.168.4.1:8080`. Manual run: `sudo bash zero/gadget/hid-gadget.sh && sudo python3 zero/web/server.py`.

### API (port 8080, bound to 0.0.0.0)
| Method | Path | Body |
|---|---|---|
| GET | /api/status | → `{"hid_ready","host_connected","udc_state","ip"}` |
| POST | /api/key | `{"keys":["a"],"mods":["ctrl"]}` |
| POST | /api/type | `{"text":"hello"}` (US layout) |
| POST | /api/mouse | `{"dx":0,"dy":0,"wheel":0,"buttons":0}` |
| POST | /api/macro | `{"name":"ctrl_alt_del"|"alt_tab"|"win_l"}` |

`host_connected` = `/sys/class/udc/*/state == configured` (host enumerated the gadget). Approximate:
it does not mean the host has focus/is unlocked. POSTs return 409 when not configured,
400 on bad JSON/types, 415 without `Content-Type: application/json`, 403 on cross-origin `Origin`,
and 503 if the host stops reading HID reports for >0.5 s (write timeout; lock is released).

## GEEK setup (PlatformIO)
```bash
cd geek && cp src/secrets.example.h src/secrets.h   # edit SSID/pass to match the AP
pio run -t upload && pio device monitor
```

## Out of scope
- Any video (HDMI capture, UVC, CSI). Zero W has one OTG port, already used for HID.
- Absolute-pointer/touch emulation, auth/TLS, non-US keyboard layouts.

## Known TODOs / risks
- **No auth** on the API: anyone on the AP can type into the host. Use a strong AP password.
- GEEK LCD pins/backlight polarity and PlatformIO board def are unverified (marked TODO in code).
- Uses Linux Foundation test VID/PID 1d6b:0104 — fine for hobby use.
- Services run as root; better: dedicated user + udev rule for /dev/hidg*.

## Known issues / deferred
- udev rule for `/dev/hidg*` (group-owned 0660 + non-root service) deferred; still chmod 666 + root services, now after a retry loop waiting for the nodes.
- Mouse ordering uses a sequential client-side fetch queue, not WebSocket (stdlib server has no WS; queue fixes ordering).
- Origin/Content-Type check reduces cross-page typing; it is not auth. Token auth deferred.
- `type_text` holds the lock for the whole string (≤1000 chars, each write bounded by 0.5 s timeout).
- GEEK LCD pins still unverified TODOs (not guessed).
