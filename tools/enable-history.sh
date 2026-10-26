#!/usr/bin/env bash
# One-shot: turn on the per-PC network history. Run on workbenchpc from the repo:
#   bash tools/enable-history.sh [--host geekpi-1] [--user pi] [--key ~/.ssh/KEY]
# 1. pulls geek-display-v2
# 2. updates the Zero and the local helper, leaving the Zero writable (overlay off)
# 3. creates/mounts the kvmdata partition on the Zero
# 4. turns the read-only overlay back on, reboots, and checks history is live
# If step 3 fails (usually "no free space at end of card"), the overlay is turned back on
# and nothing else is changed.
set -Eeuo pipefail
HOST="${ZERO_HOST:-geekpi-1}"; USER_="${ZERO_USER:-pi}"; KEY="${ZERO_KEY:-$HOME/.ssh/id_ed255}"
while [ $# -gt 0 ]; do case "$1" in
  --host) HOST=$2; shift 2;; --user) USER_=$2; shift 2;; --key) KEY=$2; shift 2;;
  *) echo "usage: $0 [--host H] [--user U] [--key PATH]" >&2; exit 2;; esac; done
cd "$(dirname "${BASH_SOURCE[0]}")/.."
T="$USER_@$HOST"; SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o ConnectTimeout=10 "$T")
say() { printf '\n==> %s\n' "$*"; }
wait_up() { say "waiting for $HOST to come back"; sleep 15
  for _ in $(seq 60); do "${SSH[@]}" true 2>/dev/null && return 0; sleep 5; done
  echo "Zero did not come back over SSH" >&2; exit 1; }
ro_on() { "${SSH[@]}" "sudo raspi-config nonint enable_overlayfs && (sleep 1; sudo systemctl reboot) >/dev/null 2>&1 &" || true; wait_up; }

say "pulling geek-display-v2"
git checkout -q geek-display-v2 && git pull -q --ff-only

say "updating the Zero and helper (Zero left writable)"
bash tools/update-zero.sh --host "$HOST" --user "$USER_" --key "$KEY" --no-reboot-ro

say "creating/mounting the kvmdata partition"
if ! "${SSH[@]}" "sudo bash ~/pi-zero-w-hid-kvm/tools/setup-kvmdata.sh"; then
  say "partition setup failed; turning the read-only overlay back on, nothing else changed"
  ro_on
  echo "Paste the error above into the chat. If it says 'MiB free', the root partition needs shrinking." >&2
  exit 1
fi

say "re-enabling the read-only overlay and rebooting"
ro_on

say "checking"
"${SSH[@]}" 'findmnt -no FSTYPE /; findmnt -no SOURCE,TARGET /var/lib/kvm || echo "kvmdata NOT mounted";
  systemctl is-active kvm-web; journalctl -u kvm-web -b --no-pager | grep net-history | tail -3'
curl -s "http://$HOST:8080/api/status" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("pc_host:",d.get("pc_host"),"| since:",d.get("net_hist_since"),"| procs:",len(d.get("net_hist") or []))' || true
say "done. Flash the GEEK if you haven't: cd geek && pio run -t upload"
