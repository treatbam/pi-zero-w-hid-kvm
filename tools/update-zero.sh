#!/usr/bin/env bash
# Update the Pi Zero KVM over SSH (run on workbenchpc), and/or the local host helper.
#
#   tools/update-zero.sh [options]
#
# Zero steps (read-only root via raspi-config overlayfs, passwordless sudo):
#   preflight (ssh, sudo -n, git repo, ref exists on origin)  -> nothing changed yet
#   overlay on? -> disable_overlayfs, reboot, wait for SSH
#   git fetch, record previous commit, check out --ref, sudo bash zero/install.sh,
#   restart kvm-web, verify kvm-web + tailscaled active
#   enable_overlayfs, reboot, wait, verify root is overlay and services active
# Helper step (local): copy host/*.py to ~/.local/share/kvm-helper, restart the user unit.
#
# Options:
#   --host H         Zero host            (env ZERO_HOST, default geekpi-1)
#   --user U         SSH user             (env ZERO_USER, default pi)
#   --key PATH       SSH private key      (env ZERO_KEY,  default ~/.ssh/id_ed255)
#   --ref REF        branch/tag/commit to deploy (default: the Zero's current branch;
#                    main if it is on a detached HEAD)
#   --repo DIR       repo dir on the Zero, relative to its home or absolute (default pi-zero-w-hid-kvm)
#   --timeout S      seconds to wait for SSH after a reboot (default 300)
#   --no-reboot-ro   leave the Zero writable at the end (skip enable_overlayfs + reboot)
#   --ro-on-failure  re-enable the overlay even if the update failed (NOT default: a failed
#                    install would then be frozen read-only; default leaves it writable)
#   --skip-helper    only update the Zero
#   --helper-only    only update the local helper
#   --helper-src DIR source of host/*.py (default: host/ next to this script)
#   --dry-run        print what would run, change nothing
#   -h, --help
set -Eeuo pipefail   # -E: the ERR trap also fires inside functions

HOST="${ZERO_HOST:-geekpi-1}"
USER_="${ZERO_USER:-pi}"
KEY="${ZERO_KEY:-$HOME/.ssh/id_ed255}"
REF=""
RDIR=pi-zero-w-hid-kvm             # relative to the remote user's home
TIMEOUT=300
REBOOT_RO=1
RO_ON_FAIL=0
DO_ZERO=1
DO_HELPER=1
DRY=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_SRC="$SCRIPT_DIR/../host"
HELPER_DST="$HOME/.local/share/kvm-helper"
SSH_BIN="${KVM_SSH:-ssh}"          # override for tests (mock ssh)
SLEEP_BIN="${KVM_SLEEP:-sleep}"

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
die() { echo "update-zero: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --user) USER_="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --repo) RDIR="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --no-reboot-ro) REBOOT_RO=0; shift ;;
    --ro-on-failure) RO_ON_FAIL=1; shift ;;
    --skip-helper) DO_HELPER=0; shift ;;
    --helper-only) DO_ZERO=0; shift ;;
    --helper-src) HELPER_SRC="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done
[ "$DO_ZERO" = 1 ] || [ "$DO_HELPER" = 1 ] || die "--helper-only and --skip-helper together do nothing"
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || die "--timeout must be seconds"
if [ -n "$REF" ] && ! [[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]]; then die "--ref has odd characters: $REF"; fi
if ! [[ "$RDIR" =~ ^[A-Za-z0-9._/-]+$ ]]; then die "--repo has odd characters: $RDIR"; fi

TARGET="$USER_@$HOST"
SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10
          -o ServerAliveInterval=5 -o ServerAliveCountMax=3)

log() { printf '\n==> %s\n' "$*"; }
# rsh CMD: run a command on the Zero (printed instead under --dry-run)
rsh() {
  if [ "$DRY" = 1 ]; then printf '  [dry-run] ssh %s %q\n' "$TARGET" "$*"; return 0; fi
  "$SSH_BIN" "${SSH_OPTS[@]}" "$TARGET" "$@"
}
# rq CMD: read-only query, runs even in dry-run so the plan reflects reality when reachable
rq() { "$SSH_BIN" "${SSH_OPTS[@]}" "$TARGET" "$@"; }

STATE="nothing changed yet"
PREV=""
OVERLAY_WAS_ON=0
OVERLAY_DISABLED=0

on_fail() {
  local rc=$?
  trap - ERR
  echo >&2
  echo "!!! update-zero FAILED (exit $rc) during: $STATE" >&2
  if [ "$OVERLAY_DISABLED" = 1 ]; then
    echo "    The Zero's root is WRITABLE (overlay disabled) and stays that way, so you can fix it." >&2
    [ -n "$PREV" ] && echo "    Previous commit: $PREV (also saved in $RDIR/.kvm-update-prev on the Zero)" >&2
    echo "    Recovery:" >&2
    echo "      ssh -i $KEY -o IdentitiesOnly=yes $TARGET" >&2
    [ -n "$PREV" ] && echo "      cd $RDIR && git checkout $PREV && sudo bash zero/install.sh && sudo systemctl restart kvm-web" >&2
    echo "      systemctl is-active kvm-web tailscaled" >&2
    echo "      sudo raspi-config nonint enable_overlayfs && sudo reboot     # once it is healthy" >&2
    if [ "$RO_ON_FAIL" = 1 ] && [ "$OVERLAY_WAS_ON" = 1 ]; then
      echo "    --ro-on-failure given: re-enabling the overlay anyway and rebooting." >&2
      rsh "sudo raspi-config nonint enable_overlayfs && if grep -qs 'LABEL=kvmdata' /etc/fstab /media/root-ro/etc/fstab; then for c in /boot/firmware/cmdline.txt /boot/cmdline.txt; do [ -f \"\$c\" ] && sudo sed -i 's/overlayroot=tmpfs[^ ]*/overlayroot=tmpfs:recurse=0/' \"\$c\"; done; fi && (sleep 1; sudo systemctl reboot) >/dev/null 2>&1 &" || true
    fi
  else
    echo "    The overlay was not touched; the Zero is as it was." >&2
  fi
  exit "$rc"
}

boot_id() { rq cat /proc/sys/kernel/random/boot_id 2>/dev/null || true; }

reboot_and_wait() {
  local before after t=0
  before="$(boot_id)"
  STATE="rebooting the Zero"
  rsh "(sleep 1; sudo systemctl reboot) >/dev/null 2>&1 &" || true
  [ "$DRY" = 1 ] && { echo "  [dry-run] wait up to ${TIMEOUT}s for SSH with a new boot_id"; return 0; }
  echo "    waiting up to ${TIMEOUT}s for SSH..."
  "$SLEEP_BIN" 15
  while :; do
    after="$(boot_id)"
    if [ -n "$after" ] && [ "$after" != "$before" ]; then echo "    back up (${t}s)"; return 0; fi
    t=$((t + 5))
    [ "$t" -ge "$TIMEOUT" ] && { STATE="waiting for SSH after reboot (timed out after ${TIMEOUT}s)"; false; }
    "$SLEEP_BIN" 5
  done
}

overlay_on() { [ "$(rq findmnt -n -o FSTYPE / 2>/dev/null)" = overlay ]; }

verify_services() {
  STATE="checking kvm-web and tailscaled"
  local out
  if [ "$DRY" = 1 ]; then rsh systemctl is-active kvm-web tailscaled; return 0; fi
  out="$(rq systemctl is-active kvm-web tailscaled 2>&1 || true)"
  printf '%s\n' "$out" | while IFS= read -r l; do echo "    $l"; done
  [ "$(echo "$out" | grep -cx active)" = 2 ] || { echo "    kvm-web and tailscaled are not both active" >&2; false; }
}

update_zero() {
  [ -f "$KEY" ] || die "SSH key $KEY not found. Pass --key (or ZERO_KEY=...), e.g. --key ~/.ssh/id_ed25519_homelab"
  log "Preflight on $TARGET (read-only)"
  STATE="preflight"
  if [ "$DRY" = 1 ] && ! rq true 2>/dev/null; then
    echo "  (Zero not reachable; dry-run shows the plan only)"
    REF="${REF:-<current branch or main>}"
    rsh "sudo -n true; git -C $RDIR rev-parse HEAD; findmnt -n -o FSTYPE /"
  else
    rq true || die "cannot ssh to $TARGET with $KEY"
    rq sudo -n true || die "sudo on the Zero needs a password; this script expects passwordless sudo"
    rq "git -C $RDIR rev-parse --git-dir" >/dev/null || die "$RDIR on the Zero is not a git checkout (rsync'd copy?). Clone it there first."
    rq "git -C $RDIR remote get-url origin" >/dev/null || die "$RDIR on the Zero has no 'origin' remote"
    PREV="$(rq "git -C $RDIR rev-parse HEAD")"
    if [ -z "$REF" ]; then
      REF="$(rq "git -C $RDIR symbolic-ref -q --short HEAD" || true)"
      REF="${REF:-main}"
    fi
    rq "git -C $RDIR ls-remote --exit-code origin $REF" >/dev/null 2>&1 \
      || rq "git -C $RDIR cat-file -e $REF^{commit}" 2>/dev/null \
      || die "ref '$REF' not found on origin (or origin not reachable / no credentials on the Zero)"
    if [ -n "$(rq "git -C $RDIR status --porcelain --untracked-files=no")" ]; then
      die "$RDIR on the Zero has local changes; commit or stash them first"
    fi
    echo "    current commit $PREV, deploying ref '$REF'"
    if overlay_on; then OVERLAY_WAS_ON=1; fi
    echo "    overlay root: $([ "$OVERLAY_WAS_ON" = 1 ] && echo on || echo off)"
  fi
  trap on_fail ERR

  if [ "$OVERLAY_WAS_ON" = 1 ] || [ "$DRY" = 1 ]; then
    log "Disable overlayfs and reboot"
    STATE="disabling the overlay"
    # raspi-config's sed only strips plain 'overlayroot=tmpfs '; also strip 'overlayroot=tmpfs:recurse=0 ' used by kvmdata.
    rsh "sudo raspi-config nonint disable_overlayfs && for c in /boot/firmware/cmdline.txt /boot/cmdline.txt; do if [ -f \"\$c\" ] && grep -q 'overlayroot=tmpfs' \"\$c\"; then b=\$(dirname \"\$c\"); ro=0; findmnt -no OPTIONS \"\$b\" 2>/dev/null | grep -qw ro && { sudo mount -o remount,rw \"\$b\"; ro=1; }; sudo sed -i -E 's/overlayroot=tmpfs[^ ]* ?//g' \"\$c\"; [ \"\$ro\" = 1 ] && sudo mount -o remount,ro \"\$b\" || true; fi; done"
    OVERLAY_DISABLED=1
    reboot_and_wait
    if [ "$DRY" = 0 ] && overlay_on; then STATE="overlay still on after disable+reboot"; false; fi
  fi
  OVERLAY_DISABLED=1   # from here on the root is writable

  log "Update $RDIR to '$REF'"
  STATE="git fetch / checkout of '$REF'"
  rsh "echo ${PREV:-unknown} > $RDIR/.kvm-update-prev"
  rsh "cd $RDIR && git fetch --prune origin && git checkout $REF && { git symbolic-ref -q HEAD >/dev/null && git merge --ff-only @{u} || true; } && git log -1 --oneline"
  log "Install"
  STATE="sudo bash zero/install.sh"
  rsh "cd $RDIR && sudo bash zero/install.sh"
  STATE="restarting kvm-web"
  rsh sudo systemctl restart kvm-web
  [ "$DRY" = 1 ] || "$SLEEP_BIN" 3
  verify_services

  if [ "$REBOOT_RO" = 1 ]; then
    log "Re-enable overlayfs and reboot"
    STATE="enabling the overlay"
    # Preserve overlayroot=tmpfs:recurse=0 when kvmdata (/var/lib/kvm) is configured in fstab.
    rsh "sudo raspi-config nonint enable_overlayfs && if grep -qs 'LABEL=kvmdata' /etc/fstab /media/root-ro/etc/fstab; then for c in /boot/firmware/cmdline.txt /boot/cmdline.txt; do if [ -f \"\$c\" ]; then b=\$(dirname \"\$c\"); ro=0; findmnt -no OPTIONS \"\$b\" 2>/dev/null | grep -qw ro && { sudo mount -o remount,rw \"\$b\"; ro=1; }; sudo sed -i 's/overlayroot=tmpfs[^ ]*/overlayroot=tmpfs:recurse=0/' \"\$c\"; [ \"\$ro\" = 1 ] && sudo mount -o remount,ro \"\$b\" || true; fi; done; fi"
    reboot_and_wait
    STATE="checking root is overlay"
    if [ "$DRY" = 0 ]; then overlay_on || { echo "    root is not overlay after reboot" >&2; false; }; else rsh findmnt -n -o FSTYPE /; fi
    OVERLAY_DISABLED=0
    verify_services
    echo "    read-only root: on"
  else
    echo "    --no-reboot-ro: leaving the Zero writable (re-enable: sudo raspi-config nonint enable_overlayfs && sudo reboot)"
  fi
  trap - ERR
  log "Zero updated to '$REF' (previous: ${PREV:-unknown}; roll back with --ref ${PREV:-<commit>})"
}

update_helper() {
  log "Update local helper in $HELPER_DST"
  compgen -G "$HELPER_SRC/*.py" >/dev/null || die "no .py files in $HELPER_SRC"
  local f
  for f in "$HELPER_SRC"/*.py; do
    case "$(basename "$f")" in test_*) continue ;; esac
    if [ "$DRY" = 1 ]; then echo "  [dry-run] install -m 0644 $f $HELPER_DST/"; else
      mkdir -p "$HELPER_DST"; install -m 0644 "$f" "$HELPER_DST/"; fi
  done
  if [ "$DRY" = 1 ]; then echo "  [dry-run] systemctl --user restart kvm-helper"; return 0; fi
  local exe
  exe="$(systemctl --user show -p ExecStart --value kvm-helper 2>/dev/null || true)"
  case "$exe" in *"$HELPER_DST"*) ;; *)
    echo "    NOTE: the kvm-helper unit does not run from $HELPER_DST (ExecStart: ${exe:-unknown})."
    echo "          Point ExecStart at $HELPER_DST/kvm_helper.py so these copies are used." ;;
  esac
  systemctl --user restart kvm-helper
  "$SLEEP_BIN" 2
  systemctl --user is-active kvm-helper
}

[ "$DRY" = 1 ] && echo "update-zero: DRY RUN, nothing will change"
[ "$DO_ZERO" = 1 ] && update_zero
[ "$DO_HELPER" = 1 ] && update_helper
echo; echo "update-zero: done"
