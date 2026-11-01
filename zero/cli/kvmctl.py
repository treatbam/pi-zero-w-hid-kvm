#!/usr/bin/env python3
"""kvmctl - command-line control for the pi-zero-w-hid-kvm web server.

Talks to the same HTTP API the web UI uses, so the PIN, the Origin check and the
server-side media lock all apply. Nothing here touches the gadget or media
directly; every action goes through the running server.

Config (flag > env > default):
  --url   / KVM_URL   base URL of the server   (default http://127.0.0.1:8080)
  --pin   / KVM_PIN   PIN for write actions    (GET /status and /iso list work without it)

Examples:
  kvmctl status
  kvmctl iso list
  kvmctl iso netboot
  kvmctl iso mount netboot.xyz.iso
  kvmctl iso mount recovery.img --rw        # writable mounts need the explicit flag
  kvmctl iso eject
  kvmctl type "console=ttyACM0,115200"
  kvmctl key F12
  kvmctl boot-menu F12                       # alias for a single boot-menu keypress
  kvmctl power host wake
  kvmctl macro ctrl_alt_del
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Client:
    def __init__(self, base_url, pin=None, timeout=30):
        self.base = base_url.rstrip("/")
        self.pin = pin
        self.timeout = timeout

    def _request(self, method, path, body=None, need_pin=False):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.pin:
            headers["X-KVM-PIN"] = self.pin
        elif need_pin:
            raise ApiError(0, "this action needs a PIN; pass --pin or set KVM_PIN")
        # No Origin header on purpose: the server treats a missing Origin as same-origin
        # (curl-style), which is exactly what a CLI is.
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"error": raw.decode("utf-8", "replace")[:200]}
            raise ApiError(e.code, payload.get("error", f"HTTP {e.code}"))
        except urllib.error.URLError as e:
            raise ApiError(0, f"cannot reach {self.base}: {e.reason}")

    # --- reads ---
    def status(self):
        return self._request("GET", "/api/status")[1]

    def media(self):
        return self._request("GET", "/api/media")[1]

    # --- media writes ---
    def mount(self, image, cdrom=None, ro=True):
        body = {"image": image, "ro": ro}
        if cdrom is not None:
            body["cdrom"] = cdrom
        return self._request("POST", "/api/media/mount", body, need_pin=True)[1]

    def eject(self):
        return self._request("POST", "/api/media/eject", {}, need_pin=True)[1]

    def netboot(self):
        return self._request("POST", "/api/media/netboot", {}, need_pin=True)[1]

    def delete(self, image):
        return self._request("POST", "/api/media/delete", {"image": image}, need_pin=True)[1]

    # --- hid ---
    def type_text(self, text):
        return self._request("POST", "/api/type", {"text": text}, need_pin=True)[1]

    def key(self, keys, mods=None):
        body = {"keys": keys}
        if mods:
            body["mods"] = mods
        return self._request("POST", "/api/key", body, need_pin=True)[1]

    def power(self, target, action):
        body = {"target": target, "action": action, "confirm": True}
        return self._request("POST", "/api/power", body, need_pin=True)[1]

    def macro(self, name):
        return self._request("POST", "/api/macro", {"name": name}, need_pin=True)[1]


def _emit(obj, as_json):
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
        return
    if isinstance(obj, dict):
        for k in sorted(obj):
            print(f"{k}: {obj[k]}")
    else:
        print(obj)


def _print_iso_list(media, as_json):
    if as_json:
        print(json.dumps(media, indent=2, sort_keys=True))
        return
    images = media.get("images", [])
    mounted = media.get("mounted") or media.get("current")
    if not images:
        print("(no images)")
    for img in images:
        name = img if isinstance(img, str) else img.get("name", str(img))
        mark = " *" if name == mounted else ""
        if isinstance(img, dict):
            size = img.get("size")
            extra = f"  ({size} bytes)" if size else ""
        else:
            extra = ""
        print(f"{name}{mark}{extra}")
    if mounted:
        print(f"\nmounted: {mounted}")


def build_parser():
    p = argparse.ArgumentParser(prog="kvmctl", description="Control the pi-zero-w-hid-kvm from the command line.")
    p.add_argument("--url", default=os.environ.get("KVM_URL", DEFAULT_URL), help="server base URL")
    p.add_argument("--pin", default=os.environ.get("KVM_PIN"), help="PIN for write actions")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    p.add_argument("--timeout", type=float, default=30, help="request timeout seconds")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show server/host status")

    iso = sub.add_parser("iso", help="virtual media control")
    isosub = iso.add_subparsers(dest="iso_cmd", required=True)
    isosub.add_parser("list", help="list images and what's mounted")
    m = isosub.add_parser("mount", help="mount an image as a CD (read-only by default)")
    m.add_argument("image")
    m.add_argument("--rw", action="store_true", help="mount writable (disk image); refused without this flag")
    m.add_argument("--disk", action="store_true", help="present as a plain disk instead of a CD-ROM")
    isosub.add_parser("eject", help="eject and forget the current image")
    isosub.add_parser("netboot", help="download the pinned netboot.xyz ISO (checksum-verified)")
    d = isosub.add_parser("delete", help="delete an image from the Zero")
    d.add_argument("image")

    t = sub.add_parser("type", help="type text on the host via HID")
    t.add_argument("text")

    k = sub.add_parser("key", help="send a key combo via HID")
    k.add_argument("keys", nargs="+", help="one or more key names, e.g. F12")
    k.add_argument("--mods", nargs="*", default=[], help="modifiers, e.g. ctrl alt")

    bm = sub.add_parser("boot-menu", help="press a single boot-menu key (alias for key)")
    bm.add_argument("key", help="boot-menu key, e.g. F12")

    pw = sub.add_parser("power", help="power/reset control")
    pw.add_argument("target", choices=["host", "zero"])
    pw.add_argument("action")

    mc = sub.add_parser("macro", help="run a named macro")
    mc.add_argument("name")
    return p


def run(args, client):
    if args.cmd == "status":
        _emit(client.status(), args.json)
    elif args.cmd == "iso":
        if args.iso_cmd == "list":
            _print_iso_list(client.media(), args.json)
        elif args.iso_cmd == "mount":
            ro = not args.rw
            cdrom = False if args.disk else None
            _emit(client.mount(args.image, cdrom=cdrom, ro=ro), args.json)
        elif args.iso_cmd == "eject":
            _emit(client.eject(), args.json)
        elif args.iso_cmd == "netboot":
            _emit(client.netboot(), args.json)
        elif args.iso_cmd == "delete":
            _emit(client.delete(args.image), args.json)
    elif args.cmd == "type":
        _emit(client.type_text(args.text), args.json)
    elif args.cmd == "key":
        _emit(client.key(args.keys, args.mods), args.json)
    elif args.cmd == "boot-menu":
        _emit(client.key([args.key]), args.json)
    elif args.cmd == "power":
        _emit(client.power(args.target, args.action), args.json)
    elif args.cmd == "macro":
        _emit(client.macro(args.name), args.json)
    else:  # pragma: no cover
        raise ApiError(0, f"unknown command {args.cmd}")


def main(argv=None):
    args = build_parser().parse_args(argv)
    client = Client(args.url, pin=args.pin, timeout=args.timeout)
    try:
        run(args, client)
    except ApiError as e:
        sys.stderr.write(f"error: {e.message}\n")
        return 1 if e.status == 0 else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
