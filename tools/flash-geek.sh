#!/usr/bin/env bash
# Build and flash the ESP32-S3-GEEK firmware: USB, direct Wi-Fi OTA, or OTA relayed via the Zero.
#
#   tools/flash-geek.sh [auto|usb|ota|relay] [options]      (default: auto)
#
#   auto   USB if the GEEK's by-id port exists, else direct OTA if --host answers,
#          else relay through the Zero.
#   usb    pio upload over /dev/serial/by-id/... (never ttyACM*; GEEK_USB_PORT overrides)
#   ota    espota from this machine to --host (default kvm-geek.local, env GEEK_HOST)
#   relay  copy espota.py + firmware.bin to a private /tmp dir on the Zero and run espota
#          there (the Zero shares the GEEK's Wi-Fi). Cleans up on exit, even on failure.
#
# Options:
#   --host H      GEEK host/IP for direct OTA (GEEK_HOST, default kvm-geek.local)
#   --via H       Zero for relay (ZERO_HOST, default geekpi-1)
#   --user U      Zero SSH user (ZERO_USER, default pi)
#   --key PATH    Zero SSH key (ZERO_KEY, default ~/.ssh/id_ed255)
#   --geek-ip IP  skip the GEEK lookup on the Zero
#   --dry-run     print the plan
# Password: GEEK_OTA_PASS, else prompted (not echoed). It is never put on a command line:
# it reaches espota through a tiny runner (env var locally, 0600 file on the Zero).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIO="${PIO:-pio}"
SSH_BIN="${KVM_SSH:-ssh}"
SCP_BIN="${KVM_SCP:-scp}"
PY="${PYTHON:-python3}"
BYID="${GEEK_BYID_DIR:-/dev/serial/by-id}"   # override only for tests
USB_PORT="${GEEK_USB_PORT:-$BYID/usb-Espressif_USB_JTAG_serial_debug_unit_84:FC:E6:51:45:F8-if00}"
GEEK_MAC=84:fc:e6:51:45:f8
HOST="${GEEK_HOST:-kvm-geek.local}"
VIA="${ZERO_HOST:-geekpi-1}"
ZUSER="${ZERO_USER:-pi}"
KEY="${ZERO_KEY:-$HOME/.ssh/id_ed255}"
GEEK_IP=""
DRY=0
FW="$ROOT/geek/.pio/build/geek_ota/firmware.bin"

MODE=auto
case "${1:-}" in auto|usb|ota|relay) MODE="$1"; shift ;; esac
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --via) VIA="$2"; shift 2 ;;
    --user) ZUSER="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --geek-ip) GEEK_IP="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "flash-geek: unknown option $1 (try --help)" >&2; exit 2 ;;
  esac
done
for v in "$HOST" "$VIA" "$ZUSER" "$GEEK_IP"; do
  [[ "$v" =~ ^[A-Za-z0-9._:-]*$ ]] || { echo "flash-geek: odd characters in '$v'" >&2; exit 2; }
done

say() { echo "flash-geek: $*"; }
die() { echo "flash-geek: $*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run]'; printf ' %q' "$@"; echo; else "$@"; fi; }

LTMP=""; RDIR=""; ZT=""
SSH_OPTS=()
cleanup() {
  local rc=$?
  set +e
  if [ -n "$RDIR" ]; then "$SSH_BIN" "${SSH_OPTS[@]}" "$ZT" "rm -rf '$RDIR'" >/dev/null 2>&1 \
    && echo "flash-geek: removed $ZT:$RDIR" || echo "flash-geek: could not remove $ZT:$RDIR (it is in /tmp; gone at reboot)" >&2; fi
  [ -n "$LTMP" ] && rm -rf "$LTMP"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

get_pass() {
  if [ -z "${GEEK_OTA_PASS:-}" ]; then
    if [ "$DRY" = 1 ]; then GEEK_OTA_PASS='<prompted>'; else
      [ -t 0 ] || die "set GEEK_OTA_PASS or run interactively to be prompted"
      read -r -s -p "GEEK OTA password: " GEEK_OTA_PASS; echo; fi
  fi
  [ -n "$GEEK_OTA_PASS" ] || die "empty OTA password"
}

espota_path() {
  local p="${ESPOTA:-}"
  [ -n "$p" ] || p="${PLATFORMIO_CORE_DIR:-$HOME/.platformio}/packages/framework-arduinoespressif32/tools/espota.py"
  [ -f "$p" ] || die "espota.py not found at $p (build once with pio, or set ESPOTA=)"
  echo "$p"
}

# Build geek_ota if firmware.bin is missing or older than any source / platformio.ini.
build_if_needed() {
  if [ -f "$FW" ] && [ -z "$(find "$ROOT/geek/src" "$ROOT/geek/platformio.ini" -newer "$FW" -print -quit)" ]; then
    say "firmware.bin is up to date"
  else
    say "building (pio run -e geek_ota)"
    run "$PIO" run -d "$ROOT/geek" -e geek_ota
  fi
  [ "$DRY" = 1 ] || [ -f "$FW" ] || die "build produced no $FW"
}

# Runner: password from a file (argv[4]) or $GEEK_OTA_PASS, so it never sits in an argv.
write_runner() {
  LTMP="$(mktemp -d)"
  cat > "$LTMP/ota_run.py" <<'PYEOF'
import os, runpy, sys
espota, ip, fw = sys.argv[1:4]
pw = open(sys.argv[4]).read().rstrip("\n") if len(sys.argv) > 4 else os.environ["GEEK_OTA_PASS"]
sys.argv = [espota, "-i", ip, "-p", "3232", "-a", pw, "-f", fw, "-r"]
runpy.run_path(espota, run_name="__main__")
PYEOF
}

# Resolve + ping, ~2 s. Prints the IP on success.
reachable() {
  local ip
  ip="$(getent hosts "$1" 2>/dev/null | awk '{print $1; exit}')" || true
  [ -n "$ip" ] || return 1
  ping -c 1 -W 2 "$ip" >/dev/null 2>&1 || return 1
  echo "$ip"
}

do_usb() {
  case "$USB_PORT" in "$BYID"/*) ;; *) die "refusing $USB_PORT; use a $BYID/ path (ttyACM numbers move)" ;; esac
  [ "$DRY" = 1 ] || [ -e "$USB_PORT" ] || die "$USB_PORT not present (GEEK plugged in?)"
  say "USB via $USB_PORT"
  run "$PIO" run -d "$ROOT/geek" -e esp32s3geek -t upload --upload-port "$USB_PORT"
}

do_ota() {
  local ip="${1:-}" ep
  get_pass
  [ -n "$ip" ] || ip="$(reachable "$HOST")" || { [ "$DRY" = 1 ] && ip="$HOST"; } || die "$HOST does not resolve/answer"
  build_if_needed
  ep="$(espota_path)"; write_runner
  say "OTA to $HOST ($ip)"
  # The password is passed in the environment of this one child, not on its command line.
  if [ "$DRY" = 1 ]; then echo "  [dry-run] GEEK_OTA_PASS=*** $PY ota_run.py $ep $ip $FW"
  else GEEK_OTA_PASS="$GEEK_OTA_PASS" "$PY" "$LTMP/ota_run.py" "$ep" "$ip" "$FW"; fi
}

do_relay() {
  local ep ip
  get_pass
  [ "$DRY" = 1 ] || [ -f "$KEY" ] || die "SSH key $KEY not found (--key / ZERO_KEY)"
  ZT="$ZUSER@$VIA"
  SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10)
  build_if_needed
  ep="$(espota_path)"; write_runner
  say "relay via $ZT"
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] ssh $ZT mktemp -d /tmp/geek-ota.XXXXXX"
    echo "  [dry-run] scp espota.py ota_run.py firmware.bin -> $ZT:/tmp/geek-ota.*/"
    echo "  [dry-run] password -> ssh stdin -> /tmp/geek-ota.*/pw (0600)"
    echo "  [dry-run] find GEEK: ${GEEK_IP:-ip neigh $GEEK_MAC, then kvm-geek.local, then ping sweep}"
    echo "  [dry-run] ssh $ZT python3 ota_run.py espota.py <ip> firmware.bin pw; rm -rf the dir"
    return 0
  fi
  RDIR="$("$SSH_BIN" "${SSH_OPTS[@]}" "$ZT" 'umask 077; mktemp -d /tmp/geek-ota.XXXXXX')"
  [[ "$RDIR" =~ ^/tmp/geek-ota\.[A-Za-z0-9]+$ ]] || { RDIR=""; die "unexpected remote temp dir"; }
  "$SCP_BIN" -q "${SSH_OPTS[@]}" "$ep" "$LTMP/ota_run.py" "$FW" "$ZT:$RDIR/"
  printf '%s\n' "$GEEK_OTA_PASS" | "$SSH_BIN" "${SSH_OPTS[@]}" "$ZT" "umask 077; cat > '$RDIR/pw'"
  ip="$GEEK_IP"
  if [ -z "$ip" ]; then
    # shellcheck disable=SC2016  # expanded on the Zero
    ip="$("$SSH_BIN" "${SSH_OPTS[@]}" "$ZT" '
      find_mac() { ip neigh show | awk -v m="'"$GEEK_MAC"'" '"'"'tolower($0) ~ m && $1 ~ /^[0-9.]+$/ {print $1; exit}'"'"'; }
      ip=$(find_mac)
      [ -n "$ip" ] || ip=$(getent hosts kvm-geek.local 2>/dev/null | awk "{print \$1; exit}")
      if [ -z "$ip" ]; then
        for net in $(ip -4 -o addr show scope global | awk "{print \$4}" | grep "/24$" | sed "s#\.[0-9]*/24##"); do
          for i in $(seq 1 254); do ping -c 1 -W 1 "$net.$i" >/dev/null 2>&1 & done; wait
        done
        ip=$(find_mac)
      fi
      echo "$ip"')"
  fi
  [ -n "$ip" ] || die "GEEK ($GEEK_MAC) not found from the Zero; is it on Wi-Fi? try --geek-ip"
  say "GEEK at $ip (seen from the Zero)"
  "$SSH_BIN" "${SSH_OPTS[@]}" "$ZT" "cd '$RDIR' && python3 ota_run.py espota.py '$ip' firmware.bin pw"
}

case "$MODE" in
  usb) do_usb ;;
  ota) do_ota ;;
  relay) do_relay ;;
  auto)
    if [ -e "$USB_PORT" ]; then say "auto: GEEK on USB"; do_usb
    elif ip="$(reachable "$HOST")"; then say "auto: $HOST answers ($ip), direct OTA"; do_ota "$ip"
    else say "auto: no USB, $HOST not reachable; relaying via the Zero"; do_relay; fi ;;
esac
