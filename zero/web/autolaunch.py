"""Zero-boot automatic helper detection, stream-on-boot, and supervised 1-tap host pairing.

On Zero boot (or target USB plug-in):
  1. Passively watches /dev/ttyGS0 for 3.5 s while UDC == 'configured'.
  2. If silent, sends \\x1e\\x1fKVMCTL:probe\\n.
  3. An already-running helper replies with TYPE_MODE(helper) + stats -> LINKED.
     A paired host running kvm_stub.py / kvm_stub.ps1 replies with TYPE_HELLO (type 10,
     announcing os='linux'|'windows'|'darwin' and want='stream'|'install'), and Zero
     streams the compressed + SHA-256-verified helper bundle (\\x1e\\x1fKVMBOOT:...) directly
     into host RAM/tmpfs — zero keystrokes injected, works behind lock screens, and
     automatically updates every paired PC whenever the Zero is updated.
  4. If probe goes unanswered (~7.5 s total), marks helper_state='missing' so the web UI
     offers 1-tap supervised pairing (Pair Linux Host / Pair Windows Host) alongside the
     read-only KVM-SETUP USB drive and /setup/* downloads.
"""
import base64
import gzip
import hashlib
import os
import threading
import time

SETUP_GADGET_LINK = "/sys/kernel/config/usb_gadget/kvm/configs/c.1/mass_storage.usb0"


def _default_host_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "host"))


class PayloadBuilder:
    """Builds and caches gzipped + SHA-256-tagged OS-specific helper & installer bundles."""

    SETUP_FILES = {
        "install-linux.sh": ("install-linux.sh", "text/x-shellscript; charset=utf-8"),
        "install-windows.ps1": ("install-windows.ps1", "text/plain; charset=utf-8"),
        "kvm_stub.py": ("kvm_stub.py", "text/x-python; charset=utf-8"),
    }

    def __init__(self, host_dir=None):
        self.host_dir = host_dir or _default_host_dir()
        self._cache = {}
        self._lock = threading.Lock()

    def _p(self, name):
        return os.path.join(self.host_dir, name)

    def _mtimes(self, names):
        out = []
        for n in names:
            try:
                out.append(os.path.getmtime(self._p(n)))
            except OSError:
                out.append(0.0)
        return tuple(out)

    @staticmethod
    def _encode_bundle(raw_bytes):
        sha = hashlib.sha256(raw_bytes).hexdigest()
        gz = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
        b64 = base64.b64encode(gz).decode("ascii")
        return sha, b64

    def _read_text(self, name):
        with open(self._p(name), "r", encoding="utf-8") as f:
            return f.read()

    def _read_bytes(self, name):
        with open(self._p(name), "rb") as f:
            return f.read()

    def build_python_helper_source(self):
        stats_src = self._read_text("kvm_stats.py")
        prev_src = self._read_text("kvm_preview.py")
        help_src = self._read_text("kvm_helper.py")
        prelude = (
            "#!/usr/bin/env python3\n"
            "# Auto-streamed single-file bundle from Pi Zero W HID KVM\n"
            "import sys as _kvm_sys, types as _kvm_types\n"
            "def _kvm_load_mod(_name, _src):\n"
            "    _m = _kvm_types.ModuleType(_name)\n"
            "    _m.__file__ = f'<{_name}>'\n"
            "    exec(compile(_src, _m.__file__, 'exec'), _m.__dict__)\n"
            "    _kvm_sys.modules[_name] = _m\n"
            f"_kvm_load_mod('kvm_stats', {stats_src!r})\n"
            f"_kvm_load_mod('kvm_preview', {prev_src!r})\n"
        )
        return (prelude + "\n" + help_src).encode("utf-8")

    def build_python_installer_source(self):
        stub_src = self._read_text("kvm_stub.py")
        script = (
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            f"STUB = {stub_src!r}\n"
            "dst_dir = os.path.expanduser('~/.local/share/kvm-helper')\n"
            "os.makedirs(dst_dir, exist_ok=True)\n"
            "stub_p = os.path.join(dst_dir, 'kvm_stub.py')\n"
            "with open(stub_p, 'w', encoding='utf-8') as f: f.write(STUB)\n"
            "os.chmod(stub_p, 0o755)\n"
            "if sys.platform == 'darwin':\n"
            "    pl = os.path.expanduser('~/Library/LaunchAgents/io.diy.kvm-helper.plist')\n"
            "    os.makedirs(os.path.dirname(pl), exist_ok=True)\n"
            "    open(pl, 'w').write("
            "'<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict>'"
            "'<key>Label</key><string>io.diy.kvm-helper</string>'"
            "'<key>ProgramArguments</key><array><string>' + sys.executable + '</string>'"
            "'<string>' + stub_p + '</string></array>'"
            "'<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>')\n"
            "    subprocess.run(['launchctl', 'unload', pl], stderr=subprocess.DEVNULL)\n"
            "    subprocess.run(['launchctl', 'load', pl])\n"
            "else:\n"
            "    ud = os.path.expanduser('~/.config/systemd/user')\n"
            "    os.makedirs(ud, exist_ok=True)\n"
            "    disp = os.environ.get('DISPLAY', ':0')\n"
            "    open(os.path.join(ud, 'kvm-helper.service'), 'w').write("
            "'[Unit]\\nDescription=Pi Zero W HID KVM watcher stub\\n\\n'"
            "f'[Service]\\nEnvironment=DISPLAY={disp}\\nExecStart={sys.executable} {stub_p}\\nRestart=always\\nRestartSec=2\\n\\n'"
            "'[Install]\\nWantedBy=default.target\\n')\n"
            "    r = subprocess.run(['systemctl', '--user', 'daemon-reload'], stderr=subprocess.DEVNULL)\n"
            "    if r.returncode == 0:\n"
            "        subprocess.run(['systemctl', '--user', 'enable', '--now', 'kvm-helper.service'])\n"
            "        subprocess.run(['systemctl', '--user', 'restart', 'kvm-helper.service'])\n"
            "    else:\n"
            "        subprocess.Popen([sys.executable, stub_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        )
        return script.encode("utf-8")

    def stream_payload(self, os_name):
        os_key = "windows" if os_name == "windows" else ("darwin" if os_name == "darwin" else "linux")
        with self._lock:
            if os_key == "windows":
                mt = self._mtimes(("kvm_helper.ps1",))
                ck = ("stream", os_key, mt)
                if ck not in self._cache:
                    raw = self._read_bytes("kvm_helper.ps1")
                    self._cache[ck] = self._encode_bundle(raw)
                return os_key, self._cache[ck][0], self._cache[ck][1]
            mt = self._mtimes(("kvm_stats.py", "kvm_preview.py", "kvm_helper.py"))
            ck = ("stream", "posix", mt)
            if ck not in self._cache:
                raw = self.build_python_helper_source()
                self._cache[ck] = self._encode_bundle(raw)
            return os_key, self._cache[ck][0], self._cache[ck][1]

    def install_payload(self, os_name):
        os_key = "windows" if os_name == "windows" else ("darwin" if os_name == "darwin" else "linux")
        with self._lock:
            if os_key == "windows":
                mt = self._mtimes(("install-windows.ps1",))
                ck = ("install", os_key, mt)
                if ck not in self._cache:
                    raw = self._read_bytes("install-windows.ps1")
                    self._cache[ck] = self._encode_bundle(raw)
                return os_key, self._cache[ck][0], self._cache[ck][1]
            mt = self._mtimes(("kvm_stub.py",))
            ck = ("install", "posix", mt)
            if ck not in self._cache:
                raw = self.build_python_installer_source()
                self._cache[ck] = self._encode_bundle(raw)
            return os_key, self._cache[ck][0], self._cache[ck][1]

    def setup_file(self, name):
        meta = self.SETUP_FILES.get(name)
        if not meta:
            return None, None
        fname, ctype = meta
        try:
            return self._read_bytes(fname), ctype
        except OSError:
            return None, None


_LINUX_SERIAL_BOOTSTRAP_PY = (
    "import base64,glob,gzip,hashlib,os,select,struct,termios,time\n"
    "d=(sorted(glob.glob('/dev/serial/by-id/usb-DIY_PiZeroW_HID_KVM_*'))+['/dev/ttyACM0'])[0]\n"
    "fd=os.open(d,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK);t=termios.tcgetattr(fd)\n"
    "t[0]=t[1]=t[3]=0;t[2]=(t[2]&~(termios.CSIZE|termios.PARENB|termios.CSTOPB))|termios.CS8|termios.CREAD|termios.CLOCAL\n"
    "termios.tcsetattr(fd,termios.TCSANOW,t)\n"
    "b=b'{\"v\":1,\"os\":\"linux\",\"want\":\"install\"}'\n"
    "os.write(fd,b'\\xfe\\xfdKV\\x0a'+struct.pack('<I',len(b))+b);buf=b'';end=time.time()+10\n"
    "while time.time()<end:\n"
    " if select.select([fd],[],[],0.5)[0]:buf+=os.read(fd,16384)\n"
    " if b'\\x1e\\x1fKVMINST:linux:' in buf:\n"
    "  r=buf.split(b'\\x1e\\x1fKVMINST:linux:',1)[1]\n"
    "  if b'\\n' in r:\n"
    "   s,p=r.split(b'\\n',1)[0].strip().split(b':',1);c=gzip.decompress(base64.b64decode(p));os.close(fd)\n"
    "   if hashlib.sha256(c).hexdigest().encode()==s:exec(c.decode())\n"
    "   break\n"
)
_LINUX_PY_B64 = base64.b64encode(_LINUX_SERIAL_BOOTSTRAP_PY.encode("utf-8")).decode("ascii")

LINUX_HID_ONELINER = (
    "f=$(ls /media/$USER/KVM-SETUP/install-linux.sh /run/media/$USER/KVM-SETUP/install-linux.sh 2>/dev/null|head -n1);"
    f"if [ -n \"$f\" ];then bash \"$f\";else python3 -c 'import base64;exec(base64.b64decode(\"{_LINUX_PY_B64}\"))';fi;exit\n"
)

WINDOWS_HID_ONELINER = (
    "$v=(Get-Volume -FileSystemLabel KVM-SETUP -EA 0).DriveLetter;"
    "if($v){&($v+':\\install-windows.ps1');exit}"
    "$c=(gcim Win32_PnPEntity|?{$_.PNPDeviceID -match 'VID_1D6B&PID_0104' -and $_.Name -match '\\((COM\\d+)\\)'}|select -f 1);"
    "if($c -and $c.Name -match '\\((COM\\d+)\\)'){$p=New-Object IO.Ports.SerialPort($Matches[1],115200);$p.DtrEnable=$true;$p.Open();"
    "$j=[Text.Encoding]::UTF8.GetBytes('{\"v\":1,\"os\":\"windows\",\"want\":\"install\"}');"
    "$h=[byte[]](0xFE,0xFD,0x4B,0x56,10)+[BitConverter]::GetBytes([uint32]$j.Length)+$j;$p.Write($h,0,$h.Length);"
    "$e=[Text.Encoding]::GetEncoding(28591);$b='';$t=[DateTime]::UtcNow.AddSeconds(10);"
    "while([DateTime]::UtcNow -lt $t){if($p.BytesToRead -gt 0){$x=New-Object byte[] $p.BytesToRead;$r=$p.Read($x,0,$x.Length);$b+=$e.GetString($x,0,$r);"
    "$m=([string][char]0x1E)+([string][char]0x1F)+'KVMINST:windows:';"
    "if($b.Contains($m)){$s=$b.Substring($b.IndexOf($m)+$m.Length);if($s.Contains(\"`n\")){$l=$s.Split(\"`n\")[0].Trim();$p.Close();"
    "$k=$l.Split(':');$gz=[Convert]::FromBase64String($k[1]);$ms=New-Object IO.MemoryStream(,$gz);"
    "$gs=New-Object IO.Compression.GZipStream($ms,[IO.Compression.CompressionMode]::Decompress);$mo=New-Object IO.MemoryStream;$gs.CopyTo($mo);"
    "$raw=$mo.ToArray();$sha=([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($raw)).Replace('-','').ToLower());"
    "if($sha -eq $k[0]){iex([Text.Encoding]::UTF8.GetString($raw))};break}}}Start-Sleep -Milliseconds 80}"
    "if($p.IsOpen){$p.Close()}};exit\n"
)


class AutoLauncher:
    """Watches host link + TYPE_HELLO and orchestrates probe, stream-on-boot, and 1-tap pairing."""

    WATCH_S = 3.5
    PROBE_WAIT_S = 4.0
    STREAM_WAIT_S = 8.0

    def __init__(self, hv, send_ctl, send_boot_frame, builder=None, now=None, setup_link=SETUP_GADGET_LINK):
        self.hv = hv
        self._send_ctl = send_ctl
        self._send_boot = send_boot_frame
        self.builder = builder or PayloadBuilder()
        self._now = now or time.monotonic
        self._setup_link = setup_link
        self._lock = threading.Lock()
        self._state = "no_host"
        self._state_at = self._now()
        self._last_hello_seq = hv.hello()[2]
        self._stream_tries = 0
        self._detected_os = None
        self._detected_arch = None
        self._detected_host = None
        self._last_stream_at = None

    def status_fields(self):
        with self._lock:
            return {
                "helper_state": self._state,
                "helper_os": self._detected_os,
                "helper_arch": self._detected_arch,
                "setup_drive": os.path.exists(self._setup_link),
            }

    def step(self, host_connected):
        """Advance state machine once. Safe to call every ~0.25 s from background loop or tests."""
        now = self._now()
        action = None
        with self._lock:
            if not host_connected:
                self._state = "no_host"
                self._state_at = now
                self._stream_tries = 0
                self._last_hello_seq = self.hv.hello()[2]
                return None

            sf = self.hv.status_fields(True)
            mode, _ = self.hv.mode()
            if sf["link"] == "LINKED" or mode == "shell" or self.hv.requested_mode == "shell":
                self._state = "linked"
                self._stream_tries = 0
                self._last_hello_seq = self.hv.hello()[2]
                return None

            h_info, h_age, h_seq = self.hv.hello()
            if h_info is not None and h_seq != self._last_hello_seq and (h_age is not None and h_age <= 5.0):
                self._last_hello_seq = h_seq
                os_name = h_info.get("os", "linux")
                want = h_info.get("want", "stream")
                self._detected_os = os_name
                self._detected_arch = h_info.get("arch") or None
                self._detected_host = h_info.get("host") or None
                self._state = "streaming"
                self._state_at = now
                self._last_stream_at = now
                self._stream_tries += 1
                action = ("boot", want, os_name)
            elif self._state in ("no_host", "linked"):
                self._state = "watching"
                self._state_at = now
                self._stream_tries = 0
            elif self._state == "watching" and (now - self._state_at) >= self.WATCH_S:
                self._state = "probing"
                self._state_at = now
                action = ("probe",)
            elif self._state == "probing" and (now - self._state_at) >= self.PROBE_WAIT_S:
                self._state = "missing"
                self._state_at = now
            elif self._state == "streaming" and (now - self._state_at) >= self.STREAM_WAIT_S:
                if self._stream_tries < 2:
                    self._state = "probing"
                    self._state_at = now
                    action = ("probe",)
                else:
                    self._state = "missing"
                    self._state_at = now
            elif self._state == "pairing" and (now - self._state_at) >= 15.0:
                self._state = "probing"
                self._state_at = now
                action = ("probe",)

        if action is None:
            return None
        if action[0] == "probe":
            self._send_ctl("probe")
            return "probe"
        if action[0] == "boot":
            _, want, os_name = action
            if want == "install":
                os_tag, sha, b64 = self.builder.install_payload(os_name)
                ok = self._send_boot("KVMINST", os_tag, sha, b64)
                return "install:" + os_tag if ok else "install_err"
            os_tag, sha, b64 = self.builder.stream_payload(os_name)
            ok = self._send_boot("KVMBOOT", os_tag, sha, b64)
            return "stream:" + os_tag if ok else "stream_err"
        return None

    def trigger_probe(self):
        with self._lock:
            self._state = "probing"
            self._state_at = self._now()
            self._stream_tries = 0
        ok = self._send_ctl("probe")
        return {"ok": True, "sent": bool(ok), "helper_state": "probing"}

    def trigger_hid_pair(self, os_name, hid_mod, hid_lock, run_async=True):
        if os_name not in ("linux", "windows"):
            raise ValueError("os must be 'linux' or 'windows'")
        with self._lock:
            if self._state == "pairing":
                return {"ok": False, "error": "pairing already in progress"}
            self._state = "pairing"
            self._state_at = self._now()
            self._detected_os = os_name

        def _worker():
            try:
                if os_name == "linux":
                    with hid_lock:
                        hid_mod.key_combo(["t"], ["ctrl", "alt"])
                    time.sleep(0.85)
                    with hid_lock:
                        hid_mod.type_text(LINUX_HID_ONELINER)
                else:
                    with hid_lock:
                        hid_mod.key_combo(["r"], ["gui"])
                    time.sleep(0.75)
                    with hid_lock:
                        hid_mod.type_text("powershell -NoP -Ep Bypass\n")
                    time.sleep(0.85)
                    with hid_lock:
                        hid_mod.type_text(WINDOWS_HID_ONELINER)
            except Exception as exc:
                print(f"autolaunch hid pair ({os_name}) failed: {exc}", flush=True)
            finally:
                with self._lock:
                    if self._state == "pairing":
                        self._state = "watching"
                        self._state_at = self._now()

        if run_async:
            threading.Thread(target=_worker, daemon=True).start()
        else:
            _worker()
        return {"ok": True, "helper_state": "pairing", "os": os_name}
