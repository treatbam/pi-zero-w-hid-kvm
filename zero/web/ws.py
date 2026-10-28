"""Minimal RFC 6455 WebSocket upgrade + low-latency HID frame loop (Python 3 stdlib only)."""
import base64
import hashlib
import json
import socket
import struct

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_PAYLOAD = 2048


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    """Read one client->server WebSocket frame. Enforces client masking and payload cap."""
    b0, b1 = _recv_exact(sock, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if not masked:
        raise ValueError("unmasked client frame")
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > MAX_WS_PAYLOAD:
        raise ValueError("frame too large")
    mask = _recv_exact(sock, 4)
    raw = bytearray(_recv_exact(sock, length))
    for i in range(length):
        raw[i] ^= mask[i & 3]
    return opcode, bytes(raw)


def send_frame(sock, payload, opcode=0x1):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    n = len(payload)
    hdr = bytearray([0x80 | (opcode & 0x0F)])
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126)
        hdr += struct.pack("!H", n)
    else:
        hdr.append(127)
        hdr += struct.pack("!Q", n)
    sock.sendall(bytes(hdr) + payload)


def accept_key(sec_key):
    digest = hashlib.sha1((sec_key.strip() + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def serve_ws(handler, check_pin_fn, hid_mod, hid_lock):
    """Upgrade HTTP GET /api/ws to WebSocket and process HID mouse/key/ping frames."""
    if not handler._origin_ok():
        return handler._json(403, {"error": "cross-origin request refused"})
    upgrade = (handler.headers.get("Upgrade") or "").lower()
    sec_key = handler.headers.get("Sec-WebSocket-Key") or ""
    if upgrade != "websocket" or not sec_key:
        return handler._json(400, {"error": "expected WebSocket upgrade"})

    # Optional header auth (CLI/tests); browsers authenticate on the first frame instead
    # so the PIN never appears in URL query strings.
    authed = False
    hdr_pin = handler.headers.get("X-KVM-PIN")
    if hdr_pin is not None:
        ok = check_pin_fn(hdr_pin)
        if ok is None:
            return handler._json(429, {"error": "too many wrong PINs, wait a minute"})
        if not ok:
            return handler._json(401, {"error": "wrong or missing PIN"})
        authed = True

    acc = accept_key(sec_key)
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", acc)
    handler.end_headers()
    handler.close_connection = True

    sock = handler.connection
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

    try:
        if not authed:
            sock.settimeout(4.0)
            op, raw = read_frame(sock)
            if op != 0x1:
                send_frame(sock, b"", opcode=0x8)
                return
            msg = json.loads(raw.decode("utf-8"))
            if not isinstance(msg, dict) or msg.get("op") != "auth":
                send_frame(sock, json.dumps({"ok": False, "error": "auth required"}))
                send_frame(sock, b"", opcode=0x8)
                return
            ok = check_pin_fn(str(msg.get("pin") or ""))
            if not ok:
                err = "locked_out" if ok is None else "bad_pin"
                send_frame(sock, json.dumps({"ok": False, "error": err}))
                send_frame(sock, b"", opcode=0x8)
                return
            authed = True

        sock.settimeout(45.0)
        send_frame(sock, json.dumps({"ok": True, "op": "hello"}))

        max_m = hid_mod.MAX_MOVE
        while True:
            op, raw = read_frame(sock)
            if op == 0x8:  # Close
                send_frame(sock, b"", opcode=0x8)
                break
            if op == 0x9:  # Ping
                send_frame(sock, raw[:125], opcode=0xA)
                continue
            if op != 0x1:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("op")
            if kind == "m":
                dx = int(msg.get("dx", 0))
                dy = int(msg.get("dy", 0))
                w = int(msg.get("w", 0))
                b = int(msg.get("b", 0))
                if not (-max_m <= dx <= max_m and -max_m <= dy <= max_m and -127 <= w <= 127 and 0 <= b <= 7):
                    continue
                if hid_mod.status()["host_connected"]:
                    try:
                        with hid_lock:
                            hid_mod.mouse(dx, dy, w, b)
                    except Exception:
                        pass
            elif kind == "k":
                keys = msg.get("keys") or []
                mods = msg.get("mods") or []
                if (isinstance(keys, list) and len(keys) <= 6 and all(isinstance(k, str) and k in hid_mod.KEYS for k in keys)
                        and isinstance(mods, list) and len(mods) <= 8 and all(isinstance(m, str) and m in hid_mod.MOD for m in mods)):
                    if hid_mod.status()["host_connected"]:
                        try:
                            with hid_lock:
                                hid_mod.key_combo(keys, mods)
                        except Exception:
                            pass
            elif kind == "p":
                send_frame(sock, json.dumps({"op": "p", "t": msg.get("t", 0)}))
    except (EOFError, OSError, ValueError, socket.timeout):
        pass
