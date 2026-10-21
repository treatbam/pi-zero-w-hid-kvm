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
| POST | /api/stats | `{"on":true\|false}` → type 8 extended stats on/off (PIN; 409 in shell mode) |
| POST | /api/quality | `{"quality":"low"\|"med"\|"high"\|"off"}` → preview quality (409 in shell mode) |
| GET | /api/preview?since=N | changed JPEG tiles since N (PIN; 204 when no fresh preview) |
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

A short press on BOOT (or any wired button) advances **scoreboard (target PC) → KVM → desktop still → terminal**.
A long press does nothing, and two quick presses are just two page steps (there is no double-press delay). Hold BOOT while powering on to reopen the setup portal.

The scoreboard’s left block is the status word from `link`, with `N C S` keyboard lock LED indicators along the bottom edge when a host is attached. Green **LINKED** means the big figures are the PC, each with a dual-layer `82×22 px` readout (7px horizontal plasma gradient bar with warning notch and 60s peak-hold pip on top, 13px shaded Tufte horizon 60-sample sparkline with warning ember tint below), a 30s trend delta badge under each `temp`/`load`/`mem` label, headless bench triage (`/:disk%` and the full top process name with its CPU %) across the top header, a glance line (green/red dots: Pi up + uptime, GEEK Wi-Fi + RSSI), and a mirrored duplex network waterfall (`dn` emerald upward / `up` amber downward) in the footer. When the helper goes quiet the rail stays blue **KEYS**, the last PC figures and traces stay up dimmed, and an amber **reconnecting** toast sits in the footer for one minute. After that minute the big figures are the Pi and the footer says **searching**. Yellow **NO HOST** means the cable is out: Pi figures immediately, no toast. If the Zero cannot be reached the word is **ZERO** and the figures are dashes. The desktop page stays on that blue KEYS waiting field until a fresh still arrives, and drops the picture when `/api/frame` is not 200. The phone follows `link` and does not hold the extra minute.

The **KVM** page is about the box itself: Zero temp/load/mem, Zero Wi-Fi RSSI, Tailscale up/down and Zero uptime; GEEK RSSI and IP; the link word (LINKED / NO HOST / waiting) with the PC's LAN IP; per-core bars (every core from the type 8 extended stats, else main's top-8 hottest) and CPU/GPU/NVMe temperatures and GPU % when the PC reports them. Anything the PC doesn't report is simply left out. `zero_wifi_rssi` and `tailscale` come from `/api/status` only with the PIN; the GEEK sends its `KVM_PIN` on every status poll. Extended stats (type 8) are switched separately from the preview with `KVMCTL:stats:on|off`: kvm-web sends `stats:on` itself on the first helper ack and again after a helper restart (an ack after more than 5 s of silence, at most every 10 s), so page 2 has them with no browser open. The helper then sends type 8 every 2 s in helper mode (paused in shell mode). `POST /api/stats {"on":false}` (PIN; 409 in shell mode) turns them off and stops the automatic re-requests until `{"on":true}`; a browser preview level (`q:`) still brings them while it is set.

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

`host/kvm_helper.py` runs on the target PC. Every 2 seconds it writes package temperature (a CPU thermal zone or `/sys/class/hwmon` chip such as `coretemp` / `k10temp`; a host with no temperature sensor at all cannot reach LINKED), 1-minute load, and memory percent (type 1), default-route RX/TX rates, CPU count and top-8 per-core busy % (type 4; the CPU count is sent even with no default route), and LAN IP, `/` disk usage %, and top CPU process (type 5). When `DISPLAY` is set and a client is viewing the desktop still (`P_DESK` or the phone trackpad preview/loupe), it grabs X11 via `ffmpeg` (`full` downscale or `loupe` 1:1 cursor-centered 240×135 crop), stamps a high-contrast 5×5 cursor reticle and 4px cyan delta brackets around screen changes, and sends the 240×135 RGB565 still (type 2). The Zero removes those packets before
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

### Desktop preview (quality: low / med / high) and extra PC stats

Open **Desktop preview & PC stats** on the console. The helper streams only the 64×64
tiles that changed, as JPEGs; the Zero stores and serves them untouched (no re-encoding)
and the browser draws them onto a canvas. Pick a quality in the dropdown (PIN-protected,
sent to the helper as `1E 1F "KVMCTL:q:" low|med|high 0A`, the same framing and `/api/term`
filter as the mode toggle). The preview pauses in shell mode and resumes afterwards.

| quality | size | rate | JPEG q | max per frame | idle | typical (15% of screen changing) | ceiling |
|---|---|---|---|---|---|---|---|
| low | 640×360 | 1 fps | 45 | 48 KB | ~18 B/s | ~5 KB/s | 48 KB/s |
| med | 960×540 | 2 fps | 60 | 96 KB | ~36 B/s | ~35 KB/s | 192 KB/s |
| high | 1280×720 | 4 fps | 75 | 160 KB | ~72 B/s | ~90 KB/s | 640 KB/s |

A full refresh (on start, quality change, leaving shell mode, and every 60 s) of a busy desktop
is about 55 / 140 / 270 KB, so it is spread over one or two frames by the per-frame budget.
The 240×135 GEEK still keeps main's on-demand lease (`frame`/`loupe`). Serial writes are queued in a non-blocking outbox, so a large preview frame waits instead of timing out; a preview frame is skipped while more than 64 KB is queued.

The helper needs **only the Python 3 standard library** (fresh, minimal SBC installs are the
main use). Everything else is optional and detected once at startup, logged as one
`optional: missing ...` line:

- `ffmpeg` + `xrandr` + an X display: the GEEK still (the same one-shot grab as before).
- `python3-pil` as well: the browser preview. `numpy` makes the still conversion faster.
- `psutil`, `nvidia-smi`: extra temperature and GPU sources. `/proc` and `/sys` are used without them.

With none of them, or on a headless box with no X, the helper sends the basic stats and the
mode acks and still does shell mode; the preview answers `off`.

The helper sends **no** preview, extended stats or quality ack (types 7–9) until the Zero asks
with `KVMCTL:q:<level>`. The console sends that when the preview panel opens, and `q:off`
when it closes or the page is left (this stops the preview and the extended stats; with
several browsers, the last one to close wins). `/api/quality` returns 409 in shell mode, and
the console reports "no ack from the helper" if nothing confirms the level within 5 s.
The Zero parses types 7–9 only while it holds a helper-mode ack less than 5 s old and the console hasn't asked for shell mode, and releases
any header whose payload hasn't arrived within 3 s as terminal text, so shell output can't fake
a preview or stall the terminal. Preview frames are bounded: 64-aligned tiles up to 64×64,
1280×720 at most, and 2 MiB of stored JPEG in total (the grid resets past that).

Extra stats (console panel, and `/api/status`): `pc_cores_all` (every core, %; `pc_cores` stays main's top-8), `pc_gpu_pct`,
`pc_cpu_temp_c`, `pc_gpu_temp_c`, `pc_nvme_temp_c`. Each is `null` when the PC can't report it.
Sources: `/proc/stat`, `psutil` if installed, `/sys/class/hwmon` (coretemp/k10temp, nvme,
amdgpu/nouveau), AMD `gpu_busy_percent`, `nvidia-smi`. Intel iGPU usage is not reported.

Wire additions (envelope `FE FD 4B 56`, type, u32 length): type 7 preview frame, type 8
extended stats JSON `{"v":1,...}`, type 9 quality ack (types 4/5/6 stay net, triage, clipboard). Type 1 stats are still sent, so a new Zero
works with an old helper. An old Zero with a new helper also works: the helper sends types 7–9 only
after the Zero asks with `KVMCTL:q:` or `KVMCTL:stats:on`, which an old Zero never does.
Quality ack values are 0 low, 1 med, 2 high, 3 off. `GET /api/preview?since=N` (PIN) returns the tiles changed after N; `preview_quality` and
`preview_quality_requested` appear in `/api/status` only with the PIN.

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

## Updating remotely (from workbenchpc)

### Zero: `tools/update-zero.sh`
Updates the Zero over SSH, handling the read-only (overlayfs) root:
preflight (nothing changes) → disable overlay + reboot → `git fetch` + checkout `--ref` →
`sudo bash zero/install.sh` → restart `kvm-web` → check `kvm-web` + `tailscaled` →
re-enable overlay + reboot → check root is `overlay` and both services are active.
Then it copies `host/*.py` (not tests) to `~/.local/share/kvm-helper` and runs
`systemctl --user restart kvm-helper` on workbenchpc.

```bash
tools/update-zero.sh --dry-run                       # print the plan
tools/update-zero.sh --key ~/.ssh/id_ed25519_homelab # Zero + local helper
tools/update-zero.sh --ref geek-ui-polish --skip-helper
tools/update-zero.sh --helper-only
tools/update-zero.sh --ref <previous-commit>         # roll back
```
Defaults: `--host geekpi-1` (`ZERO_HOST`), `--user pi` (`ZERO_USER`), `--key ~/.ssh/id_ed255`
(`ZERO_KEY`; check this name, the homelab key is `~/.ssh/id_ed25519_homelab`), `--ref` = the
Zero's current branch (`main` if detached), `--timeout 300` s per reboot.
`--no-reboot-ro` leaves the Zero writable at the end.

Safety: the Zero must be a git clone with an `origin` it can fetch (a private repo needs a deploy
key or token on the Zero), with no local changes and passwordless sudo. These are checked before
anything changes. The previous commit is printed and saved in
`~/pi-zero-w-hid-kvm/.kvm-update-prev` on the Zero. If any step fails after the overlay
is disabled, the script stops, **leaves the Zero writable**, and prints recovery and rollback
commands. It doesn't re-enable read-only over a broken install unless you pass `--ro-on-failure`.
The helper copy only helps if the user unit's `ExecStart` points at
`~/.local/share/kvm-helper/kvm_helper.py`; the script warns when it doesn't (the shipped
`host/kvm-helper.service` runs from `~/pi-zero-w-hid-kvm/host`).

### GEEK: OTA over Wi-Fi (`tools/flash-geek.sh`)
OTA (ArduinoOTA / espota, port 3232, mDNS name `kvm-geek.local`) is **off until you set an OTA
password** in the setup portal (hold BOOT at power-on → join `KVM-GEEK-setup` →
http://192.168.4.1 → "OTA update password", 8+ characters; tick "turn OTA off" to clear it).
OTA uses its own password rather than the console PIN. The PIN is short and digits-only, the GEEK
already sends it to the Zero on every poll, and OTA auth has no lockout, so a PIN could be
brute-forced.

```bash
tools/flash-geek.sh                 # auto: USB if plugged in, else direct OTA, else relay via the Zero
tools/flash-geek.sh usb             # fixed by-id port only (never ttyACM*)
tools/flash-geek.sh ota [--host kvm-geek.local|IP]
tools/flash-geek.sh relay [--via geekpi-1] [--user pi] [--key ~/.ssh/id_ed255] [--geek-ip IP]
tools/flash-geek.sh relay --dry-run
```
`auto` checks the by-id port, then resolves `--host` (`GEEK_HOST`, default `kvm-geek.local`)
and pings it for about 2 s. If neither works, it relays. `ota` and `relay` first build `geek_ota` if
`firmware.bin` is missing or older than `geek/src` / `platformio.ini`.

**Relay** (workbenchpc can't reach the GEEK, but the Zero can): over SSH with `-o IdentitiesOnly=yes`
(`ZERO_HOST` / `ZERO_USER` / `ZERO_KEY`) it makes a private `mktemp -d /tmp/geek-ota.XXXXXX` on
the Zero (`/tmp` only, since the root is read-only) and copies `espota.py`, a small runner and
`firmware.bin` into it. The password goes in over SSH stdin to a 0600 file. It finds the GEEK
in order: `--geek-ip`, `ip neigh` for MAC `84:fc:e6:51:45:f8`, `kvm-geek.local`, then a /24 ping
sweep followed by `ip neigh` again. espota then runs there, and the dir is removed on exit,
failure or Ctrl-C.

**Password**: `GEEK_OTA_PASS`, else a hidden prompt. It is never on a command line. Direct OTA
gives it to espota through the runner's environment. On the Zero, the runner reads the 0600
file and calls `espota.py` in the same process. espota has no password-file option, so this is
how both stay out of `ps`. Calling `pio run -e geek_ota -t upload` directly still passes
`--auth=` on the command line; the script avoids that path.
Partitions: `default_8MB.csv` (two 3.25 MB OTA slots, firmware ~1 MB). It's the same layout the
board already used, so no repartition is needed. Firmware older than this has no OTA listener,
so the first flash of this version must be over USB. The screen shows an OTA progress panel while
an upload runs.
