# Roadmap

HID cold-boot is **gate zero**. Do not start UI or feature work until hardware reports:

1. `dtoverlay=dwc2` under `[all]` (not only `[cm5]`)
2. After reboot: `cat /sys/class/udc/*/state` → `configured`
3. Host enumerates keyboard + mouse
4. Console key press and mouse drag both work

Wi‑Fi note: the Zero W radio cannot cleanly run AP-for-GEEK *and* a heavy network scan at once. Scanner stays STA / opt-in (or a USB WiFi stick later). Serial on the third board: 3.3 V UART; level-shift if the module is not 3.3 V.

---

## Phase 0 — HID cold-boot (current)

- Install / reboot / UDC configured / host HID / web console key + drag
- Deferred from review: Origin Host pin to `192.168.4.1:8080`, incomplete gadget-dir reuse, KB/type vs mouse queue, udev + non-root, real auth

## Phase 1 — GEEK UI foundation

- Navigational menus on the GEEK LCD
- Button FSM: short / double / long hold
- Keeps HID path untouched; CVB reviews so HID does not regress

## Phase 2 — SSH stats dashboard

- On the GEEK display: connection / session stats from the Zero (or a small agent)
- Driven by Phase 1 menus + holds

## Phase 3 — Network scanner / analysis (opt-in)

- Runs on the Zero; **not** while serving the GEEK AP unless a second radio is added
- Prefer STA mode or USB WiFi stick for scan duty
- Heavy work stays behind an explicit opt-in flag

## Phase 4 — Serial bridge (third device)

- Third ESP32 / RP2350 ↔ Zero UART (GPIO 14/15) ↔ GEEK display
- Framing + baud documented; keep third board at 3.3 V or add a level shifter
- STL Master can cut a shell bay once the module footprint is locked

---

Out of scope until revisited: hardware KVM video / UVC (Zero W single OTG port), Comet Q / iOS touch parity.
