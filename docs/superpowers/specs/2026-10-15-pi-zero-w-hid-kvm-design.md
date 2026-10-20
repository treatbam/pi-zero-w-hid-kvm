# Pi Zero W HID KVM — phone console and GEEK panel

Date: 2026-10-15

This describes the next face of the existing `pi-zero-w-hid-kvm` bundle: the phone page served by the Pi Zero W, and the ESP32-S3-GEEK panel. It does not add a second product. Hardware stays a Zero W on one USB gadget cable (keyboard, mouse, and the serial port already there) plus the GEEK on Wi-Fi.

## Why

The phone keyboard wraps. Full words such as "backspace" and the function-key row are ordinary buttons with a 36px minimum, so on a phone the letters land in the gaps. The GEEK is a dense readout of temperatures, signal numbers, and link flags. It works, and it is dull to look at. The person using this wants the phone to feel like a keyboard with a trackpad under it, and the GEEK to be a small scoreboard they can read from the desk, including a glimpse of the target desktop.

## Goals

- The phone shows a keyboard whose rows never wrap, with the trackpad under the keys.
- The GEEK wakes on a scoreboard. One short press steps to a desktop still, then the terminal, then back to the scoreboard.
- The scoreboard’s big figures are the target PC when a live reading exists, and the Pi otherwise.
- The phone’s status line uses the same three words and colors as the GEEK.
- A stopped helper never leaves a frozen desktop looking live.
- The gadget remains a keyboard and mouse when the helper is absent.

## Non-goals

- HDMI capture, UVC, or smooth video. The Zero W’s only data port is already the gadget cable.
- A menu, a long-press mode, or a second gesture on the GEEK.
- Host-key macros on the GEEK. Ctrl+Alt+Del, Lock, and Alt+Tab stay on the phone.
- Showing the target PC’s own IP address. The USB cable does not reveal it.
- Readable body text in the desktop still. 240×135 can show windows and wallpaper, not sentences.
- Seeing the BIOS or the login screen before the helper is running.
- A new visual theme for the phone beyond the dark layout already agreed, plus the three status words.

## Anti-goals

- A green scoreboard with empty or invented PC numbers.
- A desktop page that keeps the last frame after the helper has gone quiet.
- Keys that wrap or collide because a label is a long word.
- Any GEEK control that the phone can already do by sending a key.

## What the person decided

### Phone

Straight rows. Every key has a fixed slot. Shift reveals symbols. The trackpad sits under the keys, the way a laptop is arranged. Left, middle, and right sit along the bottom edge of the pad. A status line stays on top. F-keys and power stay behind an Fn key so they cannot push a letter out of place. The dark key colors from the agreed screen stay.

The status line speaks in the GEEK’s three words, in the same colors:

- LINKED, green. A fresh PC reading is in hand.
- KEYS, blue. The USB host is attached, so the keyboard works, and no fresh PC reading is in hand.
- NO HOST, yellow. The USB host is not attached.

Both screens read that state from the Zero. They must not invent it separately.

### GEEK pages

One hardware button. A short press advances, instantly; two quick presses advance twice. A long press does nothing. There is no options list.

The cycle is:

1. Scoreboard, the page it wakes on.
2. Desktop still.
3. Terminal, the text console already carried on the USB serial port.
4. Back to the scoreboard.

### Scoreboard

Landscape, 240×135. The left block is a solid status color with the word written on it: LINKED, KEYS, or NO HOST. The right block is three rows. Each row is a right-aligned figure, a short label (temp, load, mem), and, for the PC, a sparkline. A small label says PC or PI so the figures cannot be misread.

- LINKED. The figures are the target PC, each with a trace of about the last two minutes. Temperature and load scale to the samples in that window. Memory is always 0–100%. The small print is the Pi: its temperature, memory percent, and address, then the GEEK’s address and signal in dBm.
- Helper just went quiet, cable still in. For one minute the word stays KEYS, the last PC figures and traces stay on screen dimmed, and the footer is an amber “reconnecting” toast. Green is off, because the reading is not live.
- After that minute, KEYS. The figures are the Pi. The small print says searching, and shows the GEEK’s address and signal.
- NO HOST. The cable is out, so there is no reconnecting toast. The figures are the Pi. The small print shows the Zero’s address and the GEEK’s address and signal.

Green is allowed only when a PC reading has actually arrived and is still fresh. A missing value is a dash on a blue or yellow board, never a green board with blanks. A frozen PC number during the reconnecting minute is dim and labeled PC, on the blue board.

The Pi’s temperature, load, and memory keep updating whenever the GEEK can reach the Zero. They move to the big figures in KEYS (once the reconnecting minute is over) and NO HOST. They move to the small print in LINKED and during the reconnecting minute.

### Desktop still

The still fills the panel. No title bar. It is a downscale of the target desktop to 240×135, refreshed on a short interval. Shapes survive. Sentences do not.

When the helper has missed a couple of updates, this page drops the picture and shows the same blue waiting state as KEYS. It does not hold the previous frame.

Until the helper has ever sent a frame, the page is that waiting state, including through BIOS and the time before login.

### Helper

One small program on the target PC sends two things over the USB serial port the Zero already exposes: the three PC figures, and the desktop still. The Zero stores the latest of each. The GEEK and the phone read them from the Zero.

The keyboard, mouse, scoreboard-of-the-Pi, and terminal all work with no helper installed.

The picture, the PC figures, and the terminal text share that serial port. Frames need an envelope the terminal page can tell apart from typed output. The envelope itself is left to the builder.

## Calls made here

These were not spelled out by the person. A different choice would not change the product they described.

- The status color block is 52px wide, narrower than a third of the panel, so each row fits a figure, a label, and a trace. The word is set vertically in it. A space in NO HOST is a short gap, not a full letter row.
- A new desktop frame is expected about every 2 seconds. Three missed intervals (about 6 seconds) counts as stale, and the desktop page switches to the blue waiting state.
- The scoreboard does not follow that 6-second rule. After the helper goes quiet it holds the last PC figures for one minute under a reconnecting toast, then shows the Pi and searching. Pulling the cable skips the toast.
- The waiting state on the desktop page reuses the blue field and the word KEYS, with no fake screenshot behind it.
- The phone keeps the dark keys from the agreed mock. Only the status words change.
- Screen-capture and the three PC stats are allowed to be OS-specific inside the helper. The bytes on the serial port are not. One frame format serves every helper.
- The first helper can target a single OS. Which OS is not chosen in this document.

## Left to the builder

- The serial envelope: how a stats record and a 240×135 frame sit beside terminal bytes without the terminal drawing junk.
- JPEG versus raw pixels between the Zero and the GEEK, as long as the panel shows a full-bleed still at the cadence above.
- Exact CSS for the phone grid, provided rows cannot wrap and the trackpad stays under the keys with the three buttons on its lower edge.
- How the helper reads temperature, load, memory, and the framebuffer on the chosen OS.
- What the blue waiting state looks like on the desktop page beyond the word KEYS and the blue field. Keep it as quiet as the scoreboard.

## Out of scope for this pass

Brightness, sound, extra GEEK pages, a pause control, a manual refresh gesture, and any capture dongle.
