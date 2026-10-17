#!/usr/bin/env bash
# Build the GEEK firmware WITHOUT secrets.h (placeholders + setup portal) and merge into one bin at 0x0.
# Usage: tools/merge-geek-bin.sh [out.bin]     (needs `pio` on PATH)
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd); OUT=${1:-$ROOT/build/kvm-geek.bin}
TMP=$(mktemp -d); cp -r "$ROOT/geek/." "$TMP"; rm -f "$TMP/src/secrets.h" ; rm -rf "$TMP/.pio"
(cd "$TMP" && pio run -e esp32s3geek)
B=$TMP/.pio/build/esp32s3geek; PK=${PLATFORMIO_CORE_DIR:-$HOME/.platformio}/packages
BA=$(find "$PK/framework-arduinoespressif32" -name boot_app0.bin | head -1)
ET=$(find "$PK/tool-esptoolpy" -name esptool.py | head -1)
python3 "$ET" --chip esp32s3 merge_bin -o "$OUT" --flash_mode dio --flash_size 8MB \
  0x0 "$B/bootloader.bin" 0x8000 "$B/partitions.bin" 0xe000 "$BA" 0x10000 "$B/firmware.bin"
echo "wrote $OUT"
