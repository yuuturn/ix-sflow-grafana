#!/usr/bin/env python3
"""Minimal classic-pcap reader used by `decapd.py --replay` for offline tests."""
import struct


def udp_payloads(path):
    with open(path, "rb") as fh:
        data = fh.read()

    magic = struct.unpack("<I", data[:4])[0]
    if magic in (0xA1B2C3D4, 0xA1B23C4D):
        endian = "<"
    elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
        endian = ">"
    else:
        raise SystemExit("not a classic pcap file")

    off = 24
    while off + 16 <= len(data):
        _ts, _tu, incl, _orig = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        frame = data[off:off + incl]
        off += incl
        if len(frame) < 42 or frame[12:14] != b"\x08\x00":
            continue
        ip = frame[14:]
        ihl = (ip[0] & 0x0F) * 4
        if ip[9] != 17:          # not UDP
            continue
        yield ip[ihl:][8:]
