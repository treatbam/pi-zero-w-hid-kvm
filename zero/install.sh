#!/bin/bash
# Run on the Zero W as root from the project directory (must contain geek/ and zero/).
# Copies the tree to /opt and enables services.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -d "$ROOT/zero/systemd" ] || ! ls "$ROOT/zero/systemd/"*.service >/dev/null 2>&1; then
  echo "error: zero/systemd/*.service not found." >&2
  echo "Run this from the project root, e.g.:" >&2
  echo "  cd ~/pi-zero-w-hid-kvm && sudo bash zero/install.sh" >&2
  echo "Not from inside zero/ — that directory must contain geek/ and zero/." >&2
  exit 1
fi
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt   # Bookworm vs older
[ -f "$CFG" ] || { echo "config.txt not found in /boot/firmware or /boot" >&2; exit 1; }

# Ensure an un-optioned "dtoverlay=dwc2" under [all].
# WHY: Bookworm images often ship "dtoverlay=dwc2,dr_mode=host" under [cm5] (or other model
# filters). A plain grep for ^dtoverlay=dwc2 matches that line and wrongly skips, but it never
# applies to a Zero W and forces host mode anyway -> no UDC, no gadget. Only an exact
# "dtoverlay=dwc2" (optionally with options, but never dr_mode=host) in the [all] section
# (or before any section header) counts. New installs use dr_mode=peripheral so OTG
# auto-detect can't leave the Zero in host mode.
has_all_dwc2() {
  awk 'BEGIN{s="[all]"; f=0}
       { l=$0; sub(/#.*/,"",l); gsub(/^[ \t]+|[ \t\r]+$/,"",l) }
       l ~ /^\[.*\]$/ { s=l; next }
       s=="[all]" && l ~ /^dtoverlay=dwc2(,.*)?$/ && l !~ /dr_mode=host/ { f=1 }
       END{ exit !f }' "$1"
}
if has_all_dwc2 "$CFG"; then
  echo "dwc2 overlay already present under [all] in $CFG"
else
  cp -a "$CFG" "$CFG.bak.$(date +%s)"
  printf '\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CFG"
  echo "Appended [all] dtoverlay=dwc2,dr_mode=peripheral to $CFG (backup saved)"
fi
grep -qx 'dwc2' /etc/modules || echo dwc2 >> /etc/modules
grep -qx 'libcomposite' /etc/modules || echo libcomposite >> /etc/modules
rsync -a --delete --exclude .git ./ /opt/pi-zero-w-hid-kvm/
cp zero/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable hid-gadget.service kvm-web.service
echo "Done. Reboot required for dwc2. AP setup: see README."
