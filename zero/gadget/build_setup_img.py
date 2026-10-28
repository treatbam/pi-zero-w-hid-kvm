#!/usr/bin/env python3
"""Pure-stdlib FAT12 read-only image builder for the KVM-SETUP USB drive.

Creates a 512 KB FAT12 disk image (volume label 'KVM-SETUP') with VFAT long
filenames containing:
  - install-linux.sh
  - install-windows.ps1
  - kvm_stub.py
  - README.txt
"""
import math
import os
import struct
import sys

SECTOR = 512
TOTAL_SECTORS = 1024          # 512 KiB
RESERVED = 1
NUM_FATS = 2
SPF = 3                       # 3 * 512 = 1536 B -> 1024 12-bit entries
ROOT_ENTRIES = 112            # 112 * 32 = 3584 B -> 7 sectors
ROOT_SECTORS = (ROOT_ENTRIES * 32) // SECTOR
DATA_START = RESERVED + NUM_FATS * SPF + ROOT_SECTORS   # sector 14 (cluster 2)

README_TEXT = """Pi Zero W HID KVM - One-Time Target Host Pairing (KVM-SETUP)
============================================================

Pair this computer once so every future Pi Zero boot or USB plug-in
automatically streams the latest SHA-256-verified KVM helper over USB serial:

  Linux / macOS (run as your normal user, never root):
    bash ./install-linux.sh

  Windows 10 / 11 (x64 & ARM64, PowerShell, no admin needed):
    powershell -ExecutionPolicy Bypass -File .\\install-windows.ps1

Keyboard and trackpad work immediately with zero software installed.
"""


def _lfn_checksum(short11):
    s = 0
    for b in short11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def _lfn_entries(long_name, short11):
    chk = _lfn_checksum(short11)
    u16 = [ord(c) for c in long_name] + [0x0000]
    n_slots = max(1, math.ceil(len(u16) / 13))
    while len(u16) < n_slots * 13:
        u16.append(0xFFFF)
    entries = []
    for idx in range(n_slots, 0, -1):
        chunk = u16[(idx - 1) * 13: idx * 13]
        seq = idx | (0x40 if idx == n_slots else 0)
        p1 = struct.pack("<5H", *chunk[0:5])
        p2 = struct.pack("<6H", *chunk[5:11])
        p3 = struct.pack("<2H", *chunk[11:13])
        ent = struct.pack("<B10sBBB12sH4s", seq, p1, 0x0F, 0x00, chk, p2, 0, p3)
        entries.append(ent)
    return entries


def _set_fat12(fat, cluster, val):
    val &= 0xFFF
    off = cluster + (cluster // 2)
    if cluster & 1:
        fat[off] = (fat[off] & 0x0F) | ((val << 4) & 0xF0)
        fat[off + 1] = (val >> 4) & 0xFF
    else:
        fat[off] = val & 0xFF
        fat[off + 1] = (fat[off + 1] & 0xF0) | ((val >> 8) & 0x0F)


def _get_fat12(fat, cluster):
    off = cluster + (cluster // 2)
    w = fat[off] | (fat[off + 1] << 8)
    return (w >> 4) & 0xFFF if (cluster & 1) else (w & 0xFFF)


def _read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def build_image_bytes(host_dir=None):
    here = os.path.dirname(os.path.abspath(__file__))
    if host_dir is None:
        host_dir = os.path.normpath(os.path.join(here, "..", "..", "host"))

    files = [
        ("install-linux.sh",   b"INSTLNX SH ", _read_bytes(os.path.join(host_dir, "install-linux.sh"))),
        ("install-windows.ps1", b"INSTWIN PS1", _read_bytes(os.path.join(host_dir, "install-windows.ps1"))),
        ("kvm_stub.py",        b"KVM_STUBPY ", _read_bytes(os.path.join(host_dir, "kvm_stub.py"))),
        ("README.txt",         b"README  TXT", README_TEXT.encode("utf-8")),
    ]

    img = bytearray(TOTAL_SECTORS * SECTOR)

    # Boot Sector (BPB + EBPB for FAT12)
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", boot, 11,
                     SECTOR,          # BytesPerSector
                     1,               # SectorsPerCluster
                     RESERVED,        # ReservedSectors
                     NUM_FATS,        # NumFATs
                     ROOT_ENTRIES,    # RootDirEntries
                     TOTAL_SECTORS,   # TotalSectors16
                     0xF8,            # MediaDescriptor
                     SPF,             # SectorsPerFAT
                     32,              # SectorsPerTrack
                     64,              # NumHeads
                     0,               # HiddenSectors
                     0)               # TotalSectors32
    boot[36] = 0x80
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, 0x4B564D32)   # Volume serial "KVM2"
    boot[43:54] = b"KVM-SETUP  "
    boot[54:62] = b"FAT12   "
    boot[510:512] = b"\x55\xaa"
    img[0:SECTOR] = boot

    fat = bytearray(SPF * SECTOR)
    fat[0:3] = b"\xf8\xff\xff"

    root = bytearray(ROOT_SECTORS * SECTOR)
    # Volume label entry in root dir
    vol_ent = struct.pack("<11sBBBHHHHHHHI", b"KVM-SETUP  ", 0x08, 0, 0, 0, 0x559C, 0x559C, 0, 0, 0x559C, 0, 0)
    root[0:32] = vol_ent
    dir_slot = 1

    next_cluster = 2
    # DOS date/time: 2026-10-28 00:00:00 -> date=((46<<9)|(10<<5)|28)=0x5D5C, time=0
    dos_date, dos_time = 0x5D5C, 0x0000

    for long_name, short11, content in files:
        n_clust = max(1, math.ceil(len(content) / SECTOR)) if content else 0
        first_clust = next_cluster if n_clust > 0 else 0
        for i in range(n_clust):
            c = next_cluster + i
            nxt = (c + 1) if (i + 1 < n_clust) else 0xFFF
            _set_fat12(fat, c, nxt)
            sec_off = (DATA_START + (c - 2)) * SECTOR
            chunk = content[i * SECTOR: (i + 1) * SECTOR]
            img[sec_off: sec_off + len(chunk)] = chunk
        next_cluster += n_clust

        for lfn in _lfn_entries(long_name, short11):
            root[dir_slot * 32: (dir_slot + 1) * 32] = lfn
            dir_slot += 1
        s_ent = struct.pack("<11sBBBHHHHHHHI",
                            short11, 0x21, 0, 0, dos_time, dos_date, dos_date,
                            0, dos_time, dos_date, first_clust, len(content))
        root[dir_slot * 32: (dir_slot + 1) * 32] = s_ent
        dir_slot += 1

    for f_idx in range(NUM_FATS):
        f_off = (RESERVED + f_idx * SPF) * SECTOR
        img[f_off: f_off + len(fat)] = fat
    r_off = (RESERVED + NUM_FATS * SPF) * SECTOR
    img[r_off: r_off + len(root)] = root
    return bytes(img)


def list_fat12_files(img_bytes):
    """Read back {long_or_short_name: bytes} from a FAT12 image (for verification/tests)."""
    fat_off = RESERVED * SECTOR
    fat = img_bytes[fat_off: fat_off + SPF * SECTOR]
    r_off = (RESERVED + NUM_FATS * SPF) * SECTOR
    root = img_bytes[r_off: r_off + ROOT_SECTORS * SECTOR]
    out = {}
    lfn_parts = []
    for slot in range(ROOT_ENTRIES):
        ent = root[slot * 32: (slot + 1) * 32]
        if ent[0] == 0x00:
            break
        if ent[0] == 0xE5:
            lfn_parts.clear()
            continue
        attr = ent[11]
        if attr == 0x0F:
            u16 = list(struct.unpack_from("<5H", ent, 1)) + list(struct.unpack_from("<6H", ent, 14)) + list(struct.unpack_from("<2H", ent, 28))
            chars = []
            for ch in u16:
                if ch in (0x0000, 0xFFFF):
                    break
                chars.append(chr(ch))
            lfn_parts.insert(0, "".join(chars))
            continue
        if attr & 0x08:
            lfn_parts.clear()
            continue
        name = "".join(lfn_parts) if lfn_parts else (
            ent[0:8].decode("ascii").rstrip() + ("." + ent[8:11].decode("ascii").rstrip() if ent[8:11].strip() else "")
        )
        lfn_parts.clear()
        first_c, size = struct.unpack_from("<HI", ent, 26)
        buf = bytearray()
        c = first_c
        while c >= 2 and c < 0xFF8 and len(buf) < size:
            sec_off = (DATA_START + (c - 2)) * SECTOR
            buf += img_bytes[sec_off: sec_off + SECTOR]
            c = _get_fat12(fat, c)
        out[name] = bytes(buf[:size])
    return out


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "kvm-setup.img")
    data = build_image_bytes()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, out_path)
    print(f"built {out_path} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
