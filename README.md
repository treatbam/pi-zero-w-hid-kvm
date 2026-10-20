# pi-zero-w-hid-kvm

HID-only DIY "KVM cousin": a Raspberry Pi Zero W acts as a USB keyboard+mouse for a host and
exposes a browser console; an ESP32-S3-GEEK is a WiFi scoreboard. There is no HDMI capture.
A small Linux helper can send PC stats and a 240×135 desktop still over the USB serial port.

## Architecture
```
 phone/laptop browser ──WiFi──┐
                              ▼
 ESP32-S3-GEEK ──WiFi──▶ Pi Zero W (AP 192.168.4.1, web :8080) ──USB OTG (HID gadget)──▶ Host PC
 (short press cycles pages)   /dev/hidg0 keyboard, /dev/hidg1 mouse, /dev/ttyGS0 serial
                              Host helper (optional) writes stats + a still on ttyACM0
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
sudo bash zero/install.sh    # ensures [all] dtoverlay=dwc2 (accepts options; new installs use dr_mode=peripheral), dwc2+libcomposite modules, installs services
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
| GET | /api/status | → `hid_ready`, `host_connected`, `udc_state`, `leds` (`{num,caps,scroll}`), `ip`, `temp_c`, `load1`, `mem_pct`, `link` (`LINKED` / `KEYS` / `NO HOST`), `pc_temp_c`, `pc_load1`, `pc_mem_pct`, `pc_rx_bps`, `pc_tx_bps`, `pc_ncpu`, `pc_cores` (top 8 core busy % sorted hottest-to-coolest), `pc_ip`, `pc_disk_pct`, `pc_top_proc`, `pc_top_cpu_pct`, `pc_age_s`, `frame_fresh`. PC fields are null unless `link` is `LINKED`. |
| GET | /api/frame[?crop=full\|loupe] | 240×135 RGB565 little-endian still (`full` downscale or `loupe` 1:1 cursor-centered crop), or 204 when stale. Requests on-demand capture from `kvm_helper.py`. Needs `X-KVM-PIN`. |
| GET | /api/clipboard | → `{"ok":true,"text":"...","age_s":...}` pulls UTF-8 clipboard from host helper (needs `X-KVM-PIN`). |
| POST | /api/clipboard | `{"text":"..."}` pushes layout-agnostic UTF-8 text to host clipboard (`wl-copy`/`xclip`/`xsel`). |
| POST | /api/ping | `{}` bounces `CapsLock` twice and measures host USB HID LED output report echo `latency_ms` (`alive` bool). |
| POST | /api/key | `{"keys":["a"],"mods":["ctrl"]}` |
| POST | /api/type | `{"text":"hello"}` (US layout) |
| POST | /api/mouse | `{"dx":0,"dy":0,"wheel":0,"buttons":0}` |
| POST | /api/macro | `{"name":"ctrl_alt_del"|"alt_tab"|"win_l"}` |
| GET | /api/term?n=40 | → `{"seq","lines":[...],"partial","open","error"}` (needs `X-KVM-PIN`) |
| POST | /api/mode | `{"mode":"shell"\|"helper"}` → switch the host serial port (see Serial mode toggle) |
| POST | /api/term | `{"text":"ls","newline":true}` → keystrokes to the host serial console |

`host_connected` = `/sys/class/udc/*/state == configured` (host enumerated the gadget). Approximate:
it does not mean the host has focus/is unlocked; use **Ping** (`POST /api/ping`) to verify the host OS is responsive via USB HID LED readback. POSTs return 409 when not configured,
400 on bad JSON/types, 415 without `Content-Type: application/json`, 403 on cross-origin `Origin`,
and 503 if the host stops reading HID reports for >0.5 s (write timeout; lock is released).

## GEEK setup (PlatformIO)
```bash
cd geek && cp src/secrets.example.h src/secrets.h   # edit SSID/pass to match the AP
pio run -t upload && pio device monitor
```

A short press on BOOT (or any wired button) advances **scoreboard → desktop still → terminal**.
A long press and a double press do nothing. Hold BOOT while powering on to reopen the setup portal.

The scoreboard’s left block is the status word from `link`, with `N C S` keyboard lock LED indicators along the bottom edge when a host is attached. Green **LINKED** means the big figures are the PC, each with a dual-layer `82×22 px` readout (7px horizontal plasma gradient bar with warning notch and 60s peak-hold pip on top, 13px shaded Tufte horizon 60-sample sparkline with warning ember tint below), a 30s trend delta badge under each `temp`/`load`/`mem` label, an 8-core micro-chiclet utilization heatmap under `load`, headless bench triage (`pc_ip`, `/:disk%`, `top_proc:cpu%`) across the top header, and a mirrored duplex network waterfall (`dn` emerald upward / `up` amber downward) in the footer. When the helper goes quiet the rail stays blue **KEYS**, the last PC figures and traces stay up dimmed, and an amber **reconnecting** toast sits in the footer for one minute. After that minute the big figures are the Pi and the footer says **searching**. Yellow **NO HOST** means the cable is out: Pi figures immediately, no toast. If the Zero cannot be reached the word is **ZERO** and the figures are dashes. The desktop page stays on that blue KEYS waiting field until a fresh still arrives, and drops the picture when `/api/frame` is not 200. The phone follows `link` and does not hold the extra minute.

## Host terminal (USB serial)

The gadget also exposes a CDC ACM serial port (`acm.usb0`): `/dev/ttyGS0` on the Zero,
`/dev/ttyACM0` on a Linux host, a `COMx` port on Windows. `kvm-web` reads it into a ring
buffer of the last 200 lines (ANSI colours stripped). View it at `http://<zero>:8080/term.html`
(send box, Ctrl+C/Ctrl+D) or on the GEEK's **TERMINAL** page (last 16 lines, 39 cols).
This is a text console, not video: it shows only what the host writes to the serial port.

**Linux host** — login shell on the port:
```bash
sudo systemctl enable --now serial-getty@ttyACM0.service
```
Log in from the web terminal's send box. Optional: add `console=ttyACM0` only if your
distro supports it this late in boot; kernel boot messages generally won't appear because
the port only exists once the USB driver has loaded.

Do not run that getty at the same time as the helper below. Both write the same port. The helper has its own shell mode instead (see Serial mode toggle).

### PC stats and desktop still (Linux helper)

`host/kvm_helper.py` runs on the target PC. Every 2 seconds it writes package temperature, 1-minute load, and memory percent (type 1), default-route RX/TX rates and top-8 per-core busy % (type 4), and LAN IP, `/` disk usage %, and top CPU process (type 5). When `DISPLAY` is set and a client is viewing the desktop still (`P_DESK` or the phone trackpad preview/loupe), it grabs X11 via `ffmpeg` (`full` downscale or `loupe` 1:1 cursor-centered 240×135 crop), stamps a high-contrast 5×5 cursor reticle and 4px cyan delta brackets around screen changes, and sends the 240×135 RGB565 still (type 2). The Zero removes those packets before
the terminal buffer, and serves them as `/api/status` and `/api/frame`. Three missed intervals
(6 seconds) drop LINKED back to KEYS and drop the still. The scoreboard then shows
**reconnecting** for one minute before it falls back to the Pi and **searching**.
Until the helper has sent something, the panel stays on KEYS and the desktop page
stays blank, including through BIOS and the login screen.

```bash
sudo systemctl disable --now serial-getty@ttyACM0.service   # this port only
python3 host/kvm_helper.py                                  # KVM_HOST_DEV=/dev/ttyACM0
```

`ffmpeg` and `xrandr` are needed for the picture. Without them, or with `KVM_DISPLAY`
empty, the helper still sends the stats packets. The keyboard and mouse do not need it.

### Serial mode toggle (helper ⇄ login shell)

Only one program can own the host's serial port, so the helper owns it and switches
between two modes. The **serial:** button on the console (and on `term.html`) flips it:

- **helper** (default): stats + on-demand still + clipboard sync, as above.
- **shell**: stats and the still pause (the panel drops to KEYS), and the helper starts
  `bash -l` on a pty **as the user running the helper** and bridges it to the port.
  Use it from `term.html`. `exit`, or pressing the button again, kills and reaps bash and
  goes back to helper mode.

The Zero never runs arbitrary commands on the host; it only sends control packets the helper
chooses to act on. Wire format:

- Zero → host: `1E 1F "KVMCTL:" <cmd> 0A` (`shell`, `helper`, `frame`, `loupe`, `cget`, `cset:<base64>`). `/api/term` refuses any text containing a `1E` or
  `1F` byte (even alone, so a prefix can't be split across requests), so typed input can't forge it.
- Host → Zero: envelope `FE FD 4B 56` with type `1` (stats, 12B), `2` (frame, 64800B), `3` (mode ack, 1B: `0` helper / `1` shell), `4` (net + top-8 cores, 20B), `5` (triage, 40B), or `6` (clipboard UTF-8, ≤1024B). `/api/status` shows the mode ack as
  `serial_mode` (null = no helper answered), plus `serial_mode_requested` and `serial_mode_age_s` when the request carries a valid `X-KVM-PIN`.
- Latency: in helper mode a switch waits for the current still grab, so allow up to ~1.5 s
  (plus the 2 s status poll) before the button updates.

Run it as a systemd **user** service on the host (workbenchpc), never as root. The helper
refuses shell mode when its euid is 0:

```bash
mkdir -p ~/.config/systemd/user && cp host/kvm-helper.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now kvm-helper
loginctl enable-linger $USER
```

The unit uses `/dev/serial/by-id/usb-DIY_PiZeroW_HID_KVM_0001-if03`; your user needs to be
in the `dialout` group. Don't run a getty on the same port.

Security: shell mode means **the console PIN = a shell as that user**. Keep the PIN private,
keep port 8080 on your LAN or Tailscale (never port-forward it), and don't run the helper as
an account you wouldn't hand to someone holding the PIN. The console is plain HTTP, so the PIN
and terminal text are unencrypted on the LAN (Tailscale encrypts them). Anything that writes to the port
from the host, including output printed inside the shell (e.g. `printf`), can forge
envelopes: fake PC stats, a fake desktop still, or a fake mode ack. That only changes what
the console *displays*; it can't start a shell or send keys.

**Windows host** — no getty; find the port in Device Manager → Ports (COM & LPT)
("USB Serial Device", driver `usbser.sys` is automatic on Windows 10/11), then stream a
script into it, e.g. PowerShell:
```powershell
$p = New-Object System.IO.Ports.SerialPort COM5,115200; $p.Open()
while ($true) { $p.WriteLine("$(Get-Date -f HH:mm:ss) CPU $((Get-CimInstance Win32_Processor).LoadPercentage)%"); Start-Sleep 2 }
```
(Interactive shells over COM need a third-party tool; status/log streaming is the practical use.)

**Limitations**
- Only works **after the host OS has booted** and loaded its USB serial driver. No BIOS/UEFI,
  bootloader, or early-boot output, and nothing if the host hangs before login.
- The baud rate is ignored (it's USB); any value works.
- Re-running `hid-gadget.sh` on a Zero with an older gadget (no `acm.usb0`) rebuilds it, so
  the host re-enumerates once. If the gadget fails to bind after this change, the dwc2
  endpoint budget is the likely cause; see `journalctl -u hid-gadget`.
- A login over this port is a real shell on the host; it's behind the console PIN, but
  anyone with the PIN gets it.

## Out of scope
- HDMI capture, UVC, or CSI. The Zero W has one OTG port, already used for HID.
  The desktop page is a still from the helper, not live video.
- Absolute-pointer/touch emulation, auth/TLS, non-US keyboard layouts.

## Known TODOs / risks
- GET `/api/status` is open on the AP. Everything else needs the console PIN. Use a strong AP password.
- GEEK LCD pins/backlight polarity and PlatformIO board def are unverified (marked TODO in code).
- Uses Linux Foundation test VID/PID 1d6b:0104 — fine for hobby use.
- Services run as root; better: dedicated user + udev rule for /dev/hidg*.

## Known issues / deferred
- udev rule for `/dev/hidg*` (group-owned 0660 + non-root service) deferred; still chmod 666 + root services, now after a retry loop waiting for the nodes.
- Mouse ordering uses a sequential client-side fetch queue, not WebSocket (stdlib server has no WS; queue fixes ordering).
- Origin/Content-Type check reduces cross-page typing; it is not auth. Token auth deferred.
- `type_text` holds the lock for the whole string (≤1000 chars, each write bounded by 0.5 s timeout).
- GEEK LCD pins still unverified TODOs (not guessed).

## Console PIN and power controls

`install.sh` creates an 8-digit console PIN in `/etc/pi-zero-w-hid-kvm/pin` (root-only) and prints it at the end. The web console asks for it once and remembers it in that browser. Every POST needs it in the `X-KVM-PIN` header, and five wrong tries from one address locks that address out for 60 s. The server refuses to start without a PIN. Put the same PIN in `KVM_PIN` in `geek/src/secrets.h` (or the setup portal) so the GEEK can read the terminal and the desktop still.

Power buttons (all ask for confirmation first):

- **Host, soft:** Shut down, Sleep and Wake are sent as USB System Control keys from a third HID device (`/dev/hidg2`). The host's OS decides what "power" does, usually its power-button action. After updating, the host sees the device re-connect once.
- **Host, hard:** Hard reset and Hard power need optocouplers wired to the motherboard headers. Until then they return "not installed".
- **Zero:** Reboot and Shut down need you to type the action name. The service runs as root today. If it moves to its own user, allow just these two commands:
  `kvm ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot` in `/etc/sudoers.d/kvm-power`.

To shut the Zero down by hand: `sudo poweroff`, then wait for the green LED to stop blinking before unplugging.

## Flashing the GEEK without a PC toolchain

Firmware built **without** `secrets.h` (placeholders from `secrets.example.h`), so no Wi-Fi password or PIN is baked into the bin. On first boot it opens a setup portal:

1. Join Wi-Fi **`KVM-GEEK-setup`**, open **http://192.168.4.1**.
2. Enter the Zero's SSID/password, address (default `192.168.4.1`), port `8080`, and the console PIN that `install.sh` printed (`/etc/pi-zero-w-hid-kvm/pin`).
3. Save; it reboots and connects. Values live in NVS (`Preferences`, namespace `kvm`).

Hold **BOOT** while powering on to reopen the portal anytime. (Builds made locally *with* `secrets.h` use those values as defaults and skip the portal unless BOOT is held.)

### From Home Assistant (Terminal & SSH add-on) or a NAS
Plug the GEEK into the HA box/NAS USB port, copy `kvm-geek.bin` over, then:

```sh
pip install esptool            # HA add-on: apk add py3-pip first if needed
esptool.py --chip esp32s3 --port /dev/ttyACM0 --baud 460800 write_flash 0x0 kvm-geek.bin
```

Port may be `/dev/ttyUSB0`; check `ls /dev/tty{ACM,USB}*`. Hold BOOT while plugging in if it won't connect. On HA OS the add-on needs Protection mode off / the device mapped to see the port.

### Building the merged bin yourself
`tools/merge-geek-bin.sh` (needs `pio`) builds with placeholders and writes `build/kvm-geek.bin` (bootloader 0x0, partitions 0x8000, boot_app0 0xe000, app 0x10000 merged at 0x0, 8MB DIO).
