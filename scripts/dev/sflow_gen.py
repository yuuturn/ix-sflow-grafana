#!/usr/bin/env python3
"""
Minimal sFlow v5 exporter used to prove the pipeline works before the
IX2207 is configured.

It sends `--count` flow samples (default: a burst), each one an Ethernet
frame carrying IPv4 + UDP so that sfacctd can decode 5-tuple + port.

    ./sflow_gen.py --host 127.0.0.1 --count 500 --rate 1000

Private source addresses are drawn from the sample set (they show up in the
Sankey as hosts); public destinations are well-known networks so the ASN
lookup returns something recognisable.
"""
import argparse
import random
import socket
import struct
import time

# 内部ホスト（RFC1918）。is_internal() が「内部」と判定できるレンジを使う。
# 192.0.2.0/24 などの TEST-NET を使うと transit（外部→外部）扱いになるので注意。
SRC_HOSTS = [
    "192.168.1.10", "192.168.1.11", "192.168.1.12",
    "192.168.1.20", "192.168.1.21", "192.168.1.22",
    "192.168.1.50", "192.168.1.51",
]

# public destinations, all resolvable to a real ASN
DST_ASN = [
    ("142.250.196.110", 443, "Google"),
    ("142.250.207.78",  443, "Google/YouTube"),
    ("104.244.42.1",    443, "X/Twitter"),
    ("157.240.25.35",   443, "Meta"),
    ("203.104.209.7",   443, "DMM"),
    ("133.242.0.1",     443, "Sakura"),
    ("8.8.8.8",          53, "Google DNS"),
    ("1.1.1.1",          53, "Cloudflare DNS"),
    ("13.107.42.14",    443, "Microsoft"),
    ("52.84.150.10",    443, "AWS/CloudFront"),
    ("210.140.92.181",   80, "NTT docomo"),
]

ETH_HDR_LEN = 14
IP_HDR_LEN = 20
UDP_HDR_LEN = 8


def ip_checksum(packet: bytes) -> int:
    if len(packet) % 2:
        packet += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(packet) // 2), packet))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_frame(src_ip: str, dst_ip: str, sport: int, dport: int,
                payload_len: int = 100) -> bytes:
    payload = bytes(payload_len)
    udp = struct.pack("!HHHH", sport, dport, UDP_HDR_LEN + len(payload), 0) + payload

    src = socket.inet_aton(src_ip)
    dst = socket.inet_aton(dst_ip)
    total_len = IP_HDR_LEN + len(udp)
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0, total_len,
        random.randint(0, 0xFFFF),
        0x4000, 64, 17, 0,
        src, dst,
    )
    ip = ip[:10] + struct.pack("!H", ip_checksum(ip)) + ip[12:]

    eth = (b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02"
           + b"\x08\x00")
    return eth + ip + udp


def build_mape_frame(src_ip: str, dst_ip: str, sport: int, dport: int,
                     payload_len: int = 40) -> bytes:
    """IPv4-in-IPv6 (MAP-E): Ethernet + IPv6(next-header 4) + inner IPv4."""
    inner = build_frame(src_ip, dst_ip, sport, dport, payload_len)[ETH_HDR_LEN:]

    # outer addresses mimic a MAP-E BR -> subscriber tunnel
    outer_src = socket.inet_pton(socket.AF_INET6, "2404:9200:225:100::64")
    outer_dst = socket.inet_pton(socket.AF_INET6, "2001:db8:aaaa:bbbb:6a:484d:a000:5500")
    v6 = struct.pack(
        "!IHBB16s16s",
        0x60000000,      # version 6, traffic class 0, flow label 0
        len(inner),      # payload length
        4,               # next header = IPv4-in-IP
        64,              # hop limit
        outer_src,
        outer_dst,
    )

    eth = (b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02"
           + b"\x86\xdd")
    return eth + v6 + inner


def flow_sample(seq: int, frame: bytes, rate: int, in_if: int, out_if: int) -> bytes:
    header = frame
    pad = (-len(header)) % 4

    # raw packet header record (enterprise 0, format 1)
    rec = struct.pack(
        "!IIIIII",
        1,                       # flow format = raw packet header
        16 + len(header) + pad,  # record length = 4 x uint32 + header
        1,                       # header protocol = ethernet
        1518,                    # original frame length
        0,                       # stripped
        len(header),             # header length (sampled bytes)
    ) + header + b"\x00" * pad

    body = struct.pack(
        "!IIIIIIII",
        seq,          # sequence number
        0,            # source id (ifIndex space), 0
        rate,         # sampling rate
        0,            # sample pool
        0,            # drops
        in_if,        # input
        out_if,       # output
        1,            # number of flow records
    ) + rec

    # sample type = flow sample (enterprise 0, format 1), then length, body
    return struct.pack("!II", 1, len(body)) + body


def build_datagram(agent_ip: str, seq: int, samples: list) -> bytes:
    now = int(time.monotonic() * 1000) & 0xFFFFFFFF
    hdr = struct.pack(
        "!II4sIIII",
        5,                       # sFlow version
        1,                       # agent address type = IPv4
        socket.inet_aton(agent_ip),
        0,                       # sub-agent id
        seq,                     # sequence number
        now,                     # uptime (ms)
        len(samples),
    )
    return hdr + b"".join(samples)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6343)
    ap.add_argument("--agent", default="192.0.2.1",
                    help="agent address carried in the datagram (the IX2207)")
    ap.add_argument("--count", type=int, default=300,
                    help="number of flow samples to send")
    ap.add_argument("--rate", type=int, default=1000, help="sampling rate 1:N")
    ap.add_argument("--batch", type=int, default=20, help="samples per datagram")
    ap.add_argument("--interval", type=float, default=0.2,
                    help="seconds between datagrams")
    ap.add_argument("--mape", action="store_true",
                    help="wrap samples as MAP-E (IPv4-in-IPv6) to exercise decapd")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    seq = 0

    while sent < args.count:
        batch = []
        for i in range(min(args.batch, args.count - sent)):
            src = random.choice(SRC_HOSTS)
            dst, dport, _ = random.choice(DST_ASN)
            sport = random.randint(32768, 60999)
            if args.mape:
                frame = build_mape_frame(dst, "203.0.113.45", dport,
                                         random.randint(10000, 60000))
            else:
                frame = build_frame(src, dst, sport, dport)
            batch.append(flow_sample(seq * 100 + i, frame, args.rate, 2, 1))
        seq += 1
        sock.sendto(build_datagram(args.agent, seq, batch), (args.host, args.port))
        sent += len(batch)
        time.sleep(args.interval)

    print(f"sent {sent} flow samples to {args.host}:{args.port} "
          f"(sampling rate 1:{args.rate}, agent {args.agent})")


if __name__ == "__main__":
    main()
