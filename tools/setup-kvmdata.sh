#!/usr/bin/env bash
# Create/mount the small writable ext4 partition (label kvmdata) at /var/lib/kvm, where
# kvm-web keeps the per-PC network history. It sits outside the read-only overlay.
#
# RUN ON THE ZERO WITH THE OVERLAY OFF (sudo raspi-config nonint disable_overlayfs; reboot),
# otherwise the fstab edit lands in the overlay and vanishes on reboot. Idempotent: an
# existing kvmdata partition is only mounted/registered, never reformatted.
#
#   sudo bash tools/setup-kvmdata.sh [--size-mb 64] [--disk /dev/mmcblk0]
# Making a new partition needs free, unpartitioned space at the end of the card (shrink
# root first, or flash with room left). The script refuses rather than resize anything.
set -euo pipefail
SIZE_MB=64; DISK=/dev/mmcblk0; MNT=/var/lib/kvm; LABEL=kvmdata
while [ $# -gt 0 ]; do case "$1" in
  --size-mb) SIZE_MB=$2; shift 2;; --disk) DISK=$2; shift 2;;
  *) echo "usage: $0 [--size-mb N] [--disk DEV]" >&2; exit 2;; esac; done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
if grep -q ' / overlay ' /proc/mounts; then
  echo "root is overlayfs: disable the overlay and reboot first" >&2; exit 1
fi
if ! blkid -L "$LABEL" >/dev/null 2>&1; then
  command -v parted >/dev/null || { echo "need parted (apt install parted)" >&2; exit 1; }
  free=$(parted -sm "$DISK" unit MiB print free | awk -F: '$5=="free;"{gsub("MiB","",$4); s=$4} END{print int(s)}')
  [ "${free:-0}" -ge "$SIZE_MB" ] || { echo "only ${free:-0} MiB free at end of $DISK; need $SIZE_MB" >&2; exit 1; }
  start=$(parted -sm "$DISK" unit MiB print free | awk -F: '$5=="free;"{s=$2} END{print s}')
  parted -s "$DISK" unit MiB mkpart primary ext4 "$start" "$(( ${start%MiB} + SIZE_MB ))MiB"
  partprobe "$DISK"; udevadm settle
  part=$(lsblk -lnpo NAME "$DISK" | tail -1)
  mkfs.ext4 -q -L "$LABEL" "$part"
  echo "created $part ($SIZE_MB MiB, label $LABEL)"
fi
mkdir -p "$MNT"
if ! grep -q "^LABEL=$LABEL[[:space:]]" /etc/fstab; then
  echo "LABEL=$LABEL $MNT ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2" >> /etc/fstab
  echo "added $MNT to /etc/fstab"
fi
mountpoint -q "$MNT" || mount "$MNT"
echo "ok: $(findmnt -no SOURCE,TARGET,FSTYPE "$MNT")"
# With overlayroot=tmpfs:recurse=0 (set by enable-history.sh) overlayroot covers only /; a separate ext4 mount from fstab stays writable when the
# overlay is back on. Re-enable it with: raspi-config nonint enable_overlayfs; reboot
