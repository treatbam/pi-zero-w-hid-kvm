#!/usr/bin/env bash
# Offline test of tools/flash-geek.sh with fake ssh/scp/pio/python3/ip/getent/ping (tools/mock/flash).
# shellcheck disable=SC2016,SC2317,SC2034  # check() evals its single-quoted conditions later
set -uo pipefail
unset KVM_SSH KVM_SCP PIO PYTHON GEEK_HOST ZERO_HOST ZERO_USER GEEK_OTA_PASS
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export FAKE_PW='s3cret-Pa55' FAKE_LOG="$T/log" FAKE_STATE="$T/st" PATH="$HERE/mock/flash:$PATH"
# Work on a scratch copy so the fake build never touches the real geek/.pio.
mkdir -p "$T/repo/tools" "$T/repo/geek/src"; cp "$HERE/flash-geek.sh" "$T/repo/tools/"
touch "$T/repo/geek/platformio.ini" "$T/repo/geek/src/main.cpp"
export FAKE_FWDIR="$T/repo/geek/.pio/build/geek_ota"
touch "$T/espota.py" "$T/key"; export ESPOTA="$T/espota.py" ZERO_KEY="$T/key" GEEK_USB_PORT=/dev/serial/by-id/nonexistent-geek-if00
F="$T/repo/tools/flash-geek.sh"
fails=0
reset() { rm -rf "$FAKE_STATE" "$FAKE_LOG"; mkdir "$FAKE_STATE"; : > "$FAKE_LOG"; for f in "$@"; do touch "$FAKE_STATE/$f"; done; }
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails + 1)); sed 's/^/    /' "$T/out"; fi; }
nopw() { ! grep -qF "$FAKE_PW" "$FAKE_LOG"; }
noleft() { [ -z "$(find /tmp -maxdepth 1 -name 'geek-ota.*' -newer "$T/key" 2>/dev/null)" ]; }
run() { GEEK_OTA_PASS="$FAKE_PW" "$F" "$@" >"$T/out" 2>&1; }

reset; GEEK_USB_PORT="$T/notbyid" run usb; rc=$?
check "usb refuses non-by-id port" '[ $rc != 0 ] && grep -q refusing "$T/out"'

reset resolve neigh_hit; mkdir -p "$T/byid"; touch "$T/byid/geek-if00"
GEEK_BYID_DIR="$T/byid" GEEK_USB_PORT="$T/byid/geek-if00" run auto; rc=$?
check "auto picks USB when the by-id port exists" '[ $rc = 0 ] && grep -q "GEEK on USB" "$T/out" && grep -q "upload --upload-port $T/byid/geek-if00" "$FAKE_LOG" && ! grep -q "^ssh" "$FAKE_LOG"'
reset; GEEK_USB_PORT=/dev/serial/by-id/fake-geek-if00 run usb --dry-run; rc=$?
check "usb dry-run uses by-id upload-port" '[ $rc = 0 ] && grep -q "upload-port /dev/serial/by-id/fake-geek-if00" "$T/out"'

reset resolve; run auto; rc=$?
check "auto picks direct OTA when host answers" '[ $rc = 0 ] && grep -q "direct OTA" "$T/out" && grep -q "espota ip=192.168.4.50 pw_ok=yes" "$FAKE_STATE/result"'
check "direct OTA: password not in any argv" 'nopw'
check "direct OTA built first" 'grep -q "pio run -d .* -e geek_ota" "$FAKE_LOG"'

reset neigh_hit; run auto; rc=$?
check "auto falls back to relay" '[ $rc = 0 ] && grep -q relaying "$T/out"'
check "relay: ip neigh hit (uppercase MAC)" 'grep -q "espota ip=192.168.4.23 pw_ok=yes" "$FAKE_STATE/result"'
check "relay: pw file is 0600" 'grep -q "pwfile mode=600" "$FAKE_STATE/result"'
check "relay: password not in any argv" 'nopw'
check "relay: remote dir removed" 'grep -q "removed" "$T/out" && noleft'
check "relay: firmware up to date skips rebuild" '! grep -q "^pio " "$FAKE_LOG"'

reset; run relay; rc=$?
check "relay: neigh miss + no mDNS -> sweep finds it" '[ $rc = 0 ] && grep -q "espota ip=192.168.4.31" "$FAKE_STATE/result"'

reset; run relay --geek-ip 10.0.0.7; rc=$?
check "relay: --geek-ip override" '[ $rc = 0 ] && grep -q "espota ip=10.0.0.7" "$FAKE_STATE/result" && ! grep -q "ip neigh" "$FAKE_LOG"'

reset neigh_hit espota_fail; run relay; rc=$?
check "relay failure still cleans up" '[ $rc != 0 ] && grep -q removed "$T/out" && noleft && nopw'

touch "$T/repo/geek/src/main.cpp"; sleep 1; touch "$T/repo/geek/src/main.cpp"
reset neigh_hit; run relay; rc=$?
check "stale firmware triggers rebuild" '[ $rc = 0 ] && grep -q "^pio run" "$FAKE_LOG"'

reset neigh_hit pio_fail; rm -f "$FAKE_FWDIR/firmware.bin"; run relay; rc=$?
check "build failure aborts before touching the Zero" '[ $rc != 0 ] && ! grep -q "^ssh" "$FAKE_LOG"'

reset; run relay --dry-run; rc=$?
check "relay dry-run" '[ $rc = 0 ] && grep -q "dry-run" "$T/out" && ! grep -q "^ssh" "$FAKE_LOG"'
exit $fails
