# kvmctl

Command-line control for the pi-zero-w-hid-kvm web server. It calls the same
HTTP API the web UI uses, so the PIN, the Origin/same-origin check and the
server-side media lock all apply. It never drives the USB gadget or media
directly, so the web UI and the CLI can't race each other.

Stdlib only (argparse + urllib), so it runs on the Zero with no extra packages.

## Config

Flag beats env beats default.

| flag | env | default |
|------|-----|---------|
| `--url` | `KVM_URL` | `http://127.0.0.1:8080` |
| `--pin` | `KVM_PIN` | (none) |

Reads (`status`, `iso list`) work without a PIN the same way the web UI's status
does. Every write needs the PIN; without one the CLI stops before sending.

## Commands

```
kvmctl status
kvmctl iso list
kvmctl iso netboot                 # download the pinned, checksum-verified netboot.xyz ISO
kvmctl iso mount netboot.xyz.iso   # read-only CD by default
kvmctl iso mount recovery.img --rw # writable mounts need --rw (mirrors the web UI's KVM_ALLOW_RW guard)
kvmctl iso mount debian.iso --disk # present as a plain disk instead of a CD-ROM
kvmctl iso eject
kvmctl iso delete old.iso
kvmctl type "console=ttyACM0,115200"
kvmctl key F12
kvmctl boot-menu F12               # alias for a single boot-menu keypress
kvmctl power host wake
kvmctl macro ctrl_alt_del
```

Add `--json` to any command for raw JSON (handy for scripts, routines, or Home
Assistant `command_line` sensors).

## Install on the Zero

```
sudo ln -s /opt/pi-zero-w-hid-kvm/zero/cli/kvmctl.py /usr/local/bin/kvmctl
export KVM_PIN=... KVM_URL=http://127.0.0.1:8080
```

## Tests

```
python3 -m unittest zero.cli.test_kvmctl
```
