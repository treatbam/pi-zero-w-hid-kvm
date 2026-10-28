#!/usr/bin/env bash
# One-time pairing installer for Linux & macOS target hosts (runs as normal user, never root).
# Installs the ~3 KB stdlib watcher stub (kvm_stub.py) so every future Zero boot or USB
# plug-in automatically streams the latest SHA-256-verified helper directly over USB serial.
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
  echo "install-linux.sh: run as your normal desktop user, not root." >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || pwd)"
DST_DIR="$HOME/.local/share/kvm-helper"
mkdir -p "$DST_DIR"

if [ -f "$SRC_DIR/kvm_stub.py" ]; then
  install -m 0755 "$SRC_DIR/kvm_stub.py" "$DST_DIR/kvm_stub.py"
  for f in kvm_helper.py kvm_preview.py kvm_stats.py; do
    [ -f "$SRC_DIR/$f" ] && install -m 0644 "$SRC_DIR/$f" "$DST_DIR/$f" || true
  done
else
  URL="${KVM_URL:-http://192.168.4.1}/setup/kvm_stub.py"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$DST_DIR/kvm_stub.py"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$DST_DIR/kvm_stub.py" "$URL"
  else
    echo "install-linux.sh: kvm_stub.py not found locally and neither curl nor wget is installed." >&2
    exit 1
  fi
  chmod 0755 "$DST_DIR/kvm_stub.py"
fi

OS_NAME="$(uname -s)"
if [ "$OS_NAME" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/io.diy.kvm-helper.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.diy.kvm-helper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DST_DIR/kvm_stub.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  launchctl load "$PLIST"
  echo "Paired macOS host: LaunchAgent io.diy.kvm-helper active."
else
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  if [ -x /usr/bin/python3 ]; then
    PY_BIN=/usr/bin/python3
  else
    PY_BIN="$(command -v python3 || echo /usr/bin/python3)"
  fi
  XAUTH_VAL="${XAUTHORITY:-%h/.Xauthority}"
  cat > "$UNIT_DIR/kvm-helper.service" <<EOF
[Unit]
Description=Pi Zero W HID KVM auto-stream watcher stub (stats, preview, serial shell)

[Service]
Environment=DISPLAY=${DISPLAY:-:0}
Environment=KVM_DISPLAY=${KVM_DISPLAY:-${DISPLAY:-:0}}
Environment=XAUTHORITY=$XAUTH_VAL
ExecStart=$PY_BIN %h/.local/share/kvm-helper/kvm_stub.py
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
  if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload >/dev/null 2>&1; then
    systemctl --user enable --now kvm-helper.service
    systemctl --user restart kvm-helper.service
    echo "Paired Linux host: systemd --user unit kvm-helper.service active."
  else
    nohup "$PY_BIN" "$DST_DIR/kvm_stub.py" >"$DST_DIR/stub.log" 2>&1 &
    echo "Paired Linux host: started kvm_stub.py in background (systemd --user unavailable)."
  fi
fi
