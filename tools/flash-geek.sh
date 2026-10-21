#!/usr/bin/env bash
# Build and flash the ESP32-S3-GEEK firmware, over Wi-Fi (OTA) or USB.
#
#   tools/flash-geek.sh ota [--host H] [--dry-run]   # Wi-Fi, needs an OTA password set in the portal
#   tools/flash-geek.sh usb [--dry-run]              # USB, by-id port only (never ttyACM*)
#
# OTA: host from --host or GEEK_HOST (default kvm-geek.local), password from GEEK_OTA_PASS
#      (prompted, not echoed, when unset). Uses PlatformIO env geek_ota (espota, port 3232).
# USB: env esp32s3geek, port GEEK_USB_PORT or the GEEK's fixed by-id path below.
# Needs `pio` on PATH (or PIO=/path/to/pio).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIO="${PIO:-pio}"
USB_PORT_DEFAULT=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_84:FC:E6:51:45:F8-if00
MODE="${1:-}"; [ $# -gt 0 ] && shift
HOST="${GEEK_HOST:-kvm-geek.local}"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "flash-geek: unknown option $1" >&2; exit 2 ;;
  esac
done
run() { if [ "$DRY" = 1 ]; then printf '[dry-run]'; printf ' %q' "$@"; echo; else "$@"; fi; }

case "$MODE" in
  ota)
    if [ -z "${GEEK_OTA_PASS:-}" ]; then
      if [ "$DRY" = 1 ]; then GEEK_OTA_PASS='<prompted>'
      else read -r -s -p "GEEK OTA password: " GEEK_OTA_PASS; echo; fi
    fi
    [ -n "$GEEK_OTA_PASS" ] || { echo "flash-geek: no OTA password" >&2; exit 1; }
    export GEEK_HOST="$HOST" GEEK_OTA_PASS
    echo "flash-geek: OTA to $HOST"
    run "$PIO" run -d "$ROOT/geek" -e geek_ota -t upload
    ;;
  usb)
    PORT="${GEEK_USB_PORT:-$USB_PORT_DEFAULT}"
    case "$PORT" in /dev/serial/by-id/*) ;; *)
      echo "flash-geek: refusing $PORT; use a /dev/serial/by-id/ path (ttyACM numbers move)" >&2; exit 1 ;;
    esac
    [ "$DRY" = 1 ] || [ -e "$PORT" ] || { echo "flash-geek: $PORT not present (GEEK plugged in?)" >&2; exit 1; }
    echo "flash-geek: USB via $PORT"
    run "$PIO" run -d "$ROOT/geek" -e esp32s3geek -t upload --upload-port "$PORT"
    ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
