#!/usr/bin/env python3
"""
Decode sFlow v5 from a pcap and report whether MAP-E (IPv4-in-IPv6)
samples carry a decodable inner IPv4 header.

Used to answer: "can we decapsulate?" before writing any production code.

    sudo tcpdump -i eth0 -s 0 -w /tmp/sflow.pcap 'udp port 6343' -c 400
    ./decap_probe.py /tmp/sflow.pcap
"""
import struct
import sys
from collections import Counter


def read_pcap(path):
    """Yield UDP payloads from a classic pcap file (Ethernet linktype)."""
    with open(path, "rb") as fh:
        data = fh.read()

    magic = struct.unpack("<I", data[:4])[0]
    if magic == 0xA1B2C3D4:
        endian = "<"
    elif magic == 0xD4C3B2A1:
        endian = ">"
    elif magic in (0xA1B23C4D, 0x4D3CB2A1):
        endian = "<" if magic == 0xA1B23C4D else ">"
    else:
        raise SystemExit("not a pcap file")

    off = 24
    while off + 16 <= len(data):
        _ts, _tu, incl, _orig = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        frame = data[off:off + incl]
        off += incl
        # Ethernet -> IPv4 -> UDP
        if len(frame) < 42 or frame[12:14] != b"\x08\x00":
            continue
        ip = frame[14:]
        ihl = (ip[0] & 0x0F) * 4
        if ip[9] != 17:
            continue
        udp = ip[ihl:]
        yield udp[8:]


def parse_sflow(payload):
    """Yield (sampling_rate, header_bytes) for each flow sample."""
    if len(payload) < 28:
        return
    version, atype = struct.unpack("!II", payload[:8])
    if version != 5:
        return
    off = 8 + (4 if atype == 1 else 16)
    off += 4  # sub-agent id
    off += 4  # sequence
    off += 4  # uptime
    (nsamples,) = struct.unpack("!I", payload[off:off + 4])
    off += 4

    for _ in range(nsamples):
        if off + 8 > len(payload):
            return
        stype, slen = struct.unpack("!II", payload[off:off + 8])
        off += 8
        end = off + slen
        if (stype & 0x0FFF) != 1:          # not a flow sample
            off = end
            continue
        body = payload[off:end]
        if len(body) < 32:
            off = end
            continue
        seq, src_id, rate, pool, drops, inp, outp, nrec = struct.unpack("!IIIIIIII", body[:32])
        roff = 32
        for _r in range(nrec):
            if roff + 8 > len(body):
                break
            tag, rlen = struct.unpack("!II", body[roff:roff + 8])
            rend = roff + 8 + rlen
            if (tag & 0x0FFF) == 1:        # raw packet header
                hp, flen, stripped, hlen = struct.unpack("!IIII", body[roff + 8:roff + 24])
                hdr = body[roff + 24:roff + 24 + hlen]
                yield rate, hdr
            roff = rend
        off = end


def decode(header):
    """Return (kind, info) for a sampled Ethernet frame."""
    if len(header) < 14:
        return "short", None
    ethertype = struct.unpack("!H", header[12:14])[0]
    l3 = header[14:]

    if ethertype == 0x0800:                # plain IPv4
        return "ipv4", ipv4_tuple(l3)
    if ethertype == 0x86DD:                # IPv6
        if len(l3) < 40:
            return "v6-short", None
        next_hdr = l3[6]
        if next_hdr == 4:                  # IPv4-in-IPv6 (MAP-E / DS-Lite)
            inner = l3[40:]
            if len(inner) < 20:
                return "encap-short", None
            return "map-e", ipv4_tuple(inner)
        return "ipv6", (next_hdr, len(l3) - 40)
    return f"other(0x{ethertype:04x})", None


def ipv4_tuple(ip):
    if len(ip) < 20:
        return None
    ihl = (ip[0] & 0x0F) * 4
    proto = ip[9]
    src = ".".join(str(b) for b in ip[12:16])
    dst = ".".join(str(b) for b in ip[16:20])
    sport = dport = None
    if proto in (6, 17) and len(ip) >= ihl + 4:
        sport, dport = struct.unpack("!HH", ip[ihl:ihl + 4])
    return (src, dst, sport, dport, proto)


def main():
    path = sys.argv[1]
    kinds = Counter()
    examples = []
    rates = Counter()

    for payload in read_pcap(path):
        for rate, hdr in parse_sflow(payload):
            kind, info = decode(hdr)
            kinds[kind] += 1
            if kind == "map-e":
                rates[rate] += 1
                if len(examples) < 5 and info:
                    examples.append((info, len(hdr)))

    total = sum(kinds.values())
    print(f"samples decoded: {total}")
    for kind, n in kinds.most_common():
        print(f"  {kind:12s} {n:5d}  ({100.0*n/max(total,1):.1f}%)")

    print(f"\nMAP-E inner IPv4 decodable: {kinds.get('map-e', 0)} samples")
    if rates:
        print(f"reported sampling rate(s): {dict(rates)}")
    for (src, dst, sport, dport, proto), hlen in examples:
        print(f"  inner: {src}:{sport} -> {dst}:{dport} proto={proto} "
              f"(sampled header = {hlen} bytes)")


if __name__ == "__main__":
    main()
