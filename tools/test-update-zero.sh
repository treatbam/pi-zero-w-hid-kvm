#!/usr/bin/env bash
# Offline test of tools/update-zero.sh against a mocked ssh (tools/mock/ssh). No network.
# shellcheck disable=SC2016,SC2034  # check() evals its single-quoted condition later
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
printf '#!/bin/sh\nexit 0\n' > "$T/sleep"; chmod +x "$T/sleep"; touch "$T/key"
export MOCK_DIR="$T/st" KVM_SSH="$HERE/mock/ssh" KVM_SLEEP="$T/sleep" ZERO_KEY="$T/key"
fails=0
reset() { rm -rf "$MOCK_DIR"; mkdir "$MOCK_DIR"; echo on > "$MOCK_DIR/overlay"; echo 1 > "$MOCK_DIR/boot"
          for f in "$@"; do touch "$MOCK_DIR/$f"; done; }
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }
reset; "$HERE/update-zero.sh" --skip-helper >"$T/out" 2>&1; rc=$?
check "happy path ends read-only" '[ $rc = 0 ] && [ "$(cat $MOCK_DIR/overlay)" = on ]'
reset failinstall; "$HERE/update-zero.sh" --skip-helper >"$T/out" 2>&1; rc=$?
check "install failure stops writable with recovery" '[ $rc != 0 ] && [ "$(cat $MOCK_DIR/overlay)" = off ] && grep -q "git checkout abc1234" "$T/out"'
reset tsdown; "$HERE/update-zero.sh" --skip-helper >"$T/out" 2>&1; rc=$?
check "inactive tailscaled is a failure" '[ $rc != 0 ] && [ "$(cat $MOCK_DIR/overlay)" = off ]'
reset; "$HERE/update-zero.sh" --skip-helper --no-reboot-ro >"$T/out" 2>&1; rc=$?
check "--no-reboot-ro leaves it writable" '[ $rc = 0 ] && [ "$(cat $MOCK_DIR/overlay)" = off ]'
reset down; "$HERE/update-zero.sh" --dry-run --skip-helper >"$T/out" 2>&1; rc=$?
check "dry-run with Zero down prints the plan" '[ $rc = 0 ] && grep -q enable_overlayfs "$T/out" && [ ! -s $MOCK_DIR/pending ]'
ZERO_KEY=/nonexistent "$HERE/update-zero.sh" --skip-helper >"$T/out" 2>&1; rc=$?
check "missing key fails early" '[ $rc != 0 ] && grep -q "not found" "$T/out"'
exit $fails
