#!/usr/bin/env python3
"""
decapd - sFlow v5 collector that decodes MAP-E (IPv4-in-IPv6) samples.

pmacct's sfacctd cannot decapsulate: its tunnel decoder is compiled out for
sFlow (CONFIG-KEYS marks tunnel_0 as NO_SFACCTD). The sampled header, however,
carries the inner IPv4 packet: with `sflow max-header-size 128` we get
14 (Ethernet) + 40 (IPv6) + 20 (IPv4) + transport header, which is enough for
the inner 5-tuple.

This daemon only cares about flow samples whose L3 is IPv6 with next-header 4
(IP-in-IP). Everything else belongs to pmacct, which keeps doing the bulk of
the work.

Two deployment modes:
  * the IX2207 sends to two collectors:
        decapd --port 6344
  * the IX2207 has a single collector:
        # pmacct moves to 6345, decapd takes 6343 and relays everything
        decapd --port 6343 --forward-to 127.0.0.1:6345

Rows are written as deltas and added into flows_decap with ON CONFLICT, so a
restart never double counts and repeated flushes stay consistent.
"""
import argparse
import asyncio
import json
import os
import collections
import logging
import signal
import socket
import struct
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger("decapd")

BUCKET_SECONDS = 300  # 5-minute buckets, aligned with pmacct's sql_history


# --------------------------------------------------------------------------
# sFlow v5 decoding
# --------------------------------------------------------------------------
def iter_flow_samples(payload):
    """Yield (sampling_rate, advertised_packet_len, header_bytes)."""
    if len(payload) < 28:
        return
    version, atype = struct.unpack_from("!II", payload, 0)
    if version != 5:
        return
    off = 8 + (4 if atype == 1 else 16) + 4 + 4 + 4  # agent addr + subagent + seq + uptime
    if off + 4 > len(payload):
        return
    (nsamples,) = struct.unpack_from("!I", payload, off)
    off += 4

    for _ in range(nsamples):
        if off + 8 > len(payload):
            return
        stype, slen = struct.unpack_from("!II", payload, off)
        off += 8
        end = min(off + slen, len(payload))
        if (stype & 0x0FFF) != 1:  # not a flow sample
            off = end
            continue

        body = payload[off:end]
        off = end
        if len(body) < 32:
            continue

        _seq, _src_id, rate = struct.unpack_from("!III", body, 0)
        (nrec,) = struct.unpack_from("!I", body, 28)
        roff = 32
        for _r in range(nrec):
            if roff + 8 > len(body):
                break
            tag, rlen = struct.unpack_from("!II", body, roff)
            rend = roff + 8 + rlen
            if (tag & 0x0FFF) == 1:  # raw packet header
                if roff + 24 > len(body):
                    break
                _hp, frame_len, _stripped, hlen = struct.unpack_from("!IIII", body, roff + 8)
                hdr = body[roff + 24:roff + 24 + hlen]
                yield rate, frame_len, hdr
            roff = rend


def decode_inner_ipv4(header):
    """Return (src, dst, sport, dport, proto) for IPv4-in-IPv6, else None."""
    if len(header) < 14 + 40 + 20:
        return None
    if header[12:14] != b"\x86\xdd":       # not IPv6
        return None
    v6 = header[14:]
    if (v6[0] >> 4) != 6:
        return None

    next_hdr = v6[6]
    offset = 40
    # walk the common extension headers (0 hop-by-hop, 43 routing, 44 fragment,
    # 60 destination options); MAP-E uses plain IP-in-IP so this is rarely hit
    while next_hdr in (0, 43, 44, 60):
        if offset + 8 > len(v6):
            return None
        next_hdr = v6[offset]
        offset += 8 * (v6[offset + 1] + 1)

    if next_hdr != 4:                      # 4 = IPv4-in-IP
        return None

    ip = v6[offset:]
    if len(ip) < 20 or (ip[0] >> 4) != 4:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if ihl < 20 or len(ip) < ihl:
        return None

    proto = ip[9]
    src = socket.inet_ntoa(ip[12:16])
    dst = socket.inet_ntoa(ip[16:20])
    sport = dport = 0
    if proto in (6, 17) and len(ip) >= ihl + 4:
        sport, dport = struct.unpack_from("!HH", ip, ihl)
    return src, dst, sport, dport, proto


# --------------------------------------------------------------------------
# Aggregation + PostgreSQL flush
# --------------------------------------------------------------------------
class Aggregator:
    """Sum (bucket, 5-tuple) -> packets/bytes since the last flush."""

    def __init__(self):
        self.pending = collections.defaultdict(lambda: [0, 0, 0])  # [packets, bytes, samples]
        self.seen = 0

    @staticmethod
    def bucket_of(when=None):
        ts = int(time.time() if when is None else when)
        return datetime.fromtimestamp(ts - ts % BUCKET_SECONDS, tz=timezone.utc)

    def add(self, key, frame_len, rate):
        # sFlow counts the sampled packets; scale back to real traffic
        self.pending[(self.bucket_of(),) + key][0] += rate
        self.pending[(self.bucket_of(),) + key][1] += frame_len * rate
        self.pending[(self.bucket_of(),) + key][2] += 1
        self.seen += 1

    def take(self):
        rows, self.pending = self.pending, collections.defaultdict(lambda: [0, 0, 0])
        return rows


class Writer:
    def __init__(self, dsn):
        import psycopg2  # imported lazily so --replay works without it
        self.psycopg2 = psycopg2
        self.dsn = dsn
        self.conn = None

    def _connect(self):
        if self.conn is None or self.conn.closed:
            self.conn = self.psycopg2.connect(self.dsn)
            self.conn.autocommit = False
            with self.conn.cursor() as cur:
                cur.execute("SET TIME ZONE 'UTC'")
            self.conn.commit()
        return self.conn

    def flush(self, rows):
        if not rows:
            return 0
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO flows_decap
                        (bucket, ip_src, ip_dst, port_src, port_dst, ip_proto,
                         packets, bytes, samples, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (bucket, ip_src, ip_dst, port_src, port_dst, ip_proto)
                    DO UPDATE SET
                        packets    = flows_decap.packets + EXCLUDED.packets,
                        bytes      = flows_decap.bytes   + EXCLUDED.bytes,
                        samples    = flows_decap.samples + EXCLUDED.samples,
                        updated_at = now()
                    """,
                    rows,
                )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------
def write_stats(path, payload):
    """Publish counters for the Zabbix health check (atomic replace)."""
    if not path:
        return
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("cannot write stats file %s: %s", path, exc)


class Collector(asyncio.DatagramProtocol):
    def __init__(self, agg, forward_to=None):
        self.agg = agg
        self.forward_to = forward_to
        self.transport = None
        self.stats = collections.Counter()
        self.started_at = time.time()
        self.last_flush_at = 0.0
        self.last_cycle_at = time.time()

    def connection_made(self, transport):
        self.transport = transport

    def snapshot(self, agg, flush_rows=0, flush_errors=0):
        return {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "datagrams": self.stats["datagrams"],
            "flow_samples": self.stats["flow_samples"],
            "map_e_samples": self.stats["map_e_samples"],
            "decode_errors": self.stats["decode_errors"],
            "relay_enabled": bool(self.forward_to),
            "relay_errors": self.stats["relay_errors"],
            # heartbeat of the collection loop: advances every cycle even when
            # there is nothing to write (MAP-E traffic is sparse at times)
            "last_cycle_at": self.last_cycle_at,
            # time of the last successful DB write; only advances when rows exist
            "last_flush_at": self.last_flush_at,
            "last_flush_rows": flush_rows,
            "flush_errors": flush_errors,
            "pending_keys": len(agg.pending),
        }

    def datagram_received(self, data, addr):
        if self.forward_to:
            try:
                self.transport.sendto(data, self.forward_to)
            except Exception as exc:  # never let relay failure stop decoding
                self.stats["relay_errors"] += 1
                log.warning("relay failed: %s", exc)

        self.stats["datagrams"] += 1
        try:
            for rate, frame_len, hdr in iter_flow_samples(data):
                self.stats["flow_samples"] += 1
                inner = decode_inner_ipv4(hdr)
                if inner is None:
                    continue
                self.stats["map_e_samples"] += 1
                self.agg.add(inner, frame_len or 128, rate or 1)
        except Exception as exc:
            self.stats["decode_errors"] += 1
            log.debug("decode error from %s: %s", addr, exc)


async def run(args):
    agg = Aggregator()
    writer = Writer(args.dsn) if not args.replay else None
    forward_to = None
    if args.forward_to:
        host, port = args.forward_to.rsplit(":", 1)
        forward_to = (host, int(port))

    loop = asyncio.get_running_loop()

    if args.replay:
        import pcap_replay  # local helper, only used offline
        for payload in pcap_replay.udp_payloads(args.replay):
            for rate, frame_len, hdr in iter_flow_samples(payload):
                agg.stats = None
                inner = decode_inner_ipv4(hdr)
                if inner:
                    agg.add(inner, frame_len or 128, rate or 1)
        rows = agg.take()
        print(f"decoded {agg.seen} MAP-E samples into {len(rows)} rows")
        for key, (pkts, byts, _) in list(rows.items())[:10]:
            print(f"  {key[0]}  {key[1]}:{key[3]} -> {key[2]}:{key[4]} proto={key[5]}  "
                  f"{pkts} pkts / {byts} bytes")
        return

    transport, protocol = await loop.create_datagram_endpoint(
        lambda: Collector(agg, forward_to),
        local_addr=(args.bind, args.port),
    )
    log.info("listening on %s:%d%s", args.bind, args.port,
             f", relaying to {forward_to}" if forward_to else "")
    write_stats(args.stats_file, protocol.snapshot(agg))

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    flush_errors = 0
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.flush)
            except asyncio.TimeoutError:
                pass
            rows = agg.take()
            if rows:
                payload = [
                    (k[0], k[1], k[2], k[3], k[4], k[5], v[0], v[1], v[2])
                    for k, v in rows.items()
                ]
                try:
                    writer.flush(payload)
                    protocol.last_flush_at = time.time()
                    log.info("flushed %d rows (%d MAP-E samples so far, %s)",
                             len(payload), protocol.stats["map_e_samples"],
                             dict(protocol.stats))
                except Exception as exc:
                    flush_errors += 1
                    log.error("flush failed, re-queueing: %s", exc)
                    for k, v in rows.items():
                        cur = agg.pending[k]
                        cur[0] += v[0]; cur[1] += v[1]; cur[2] += v[2]
            protocol.last_cycle_at = time.time()
            write_stats(args.stats_file,
                        protocol.snapshot(agg, len(rows), flush_errors))
    finally:
        transport.close()
        rows = agg.take()
        if rows:
            payload = [(k[0], k[1], k[2], k[3], k[4], k[5], v[0], v[1], v[2])
                       for k, v in rows.items()]
            try:
                writer.flush(payload)
                protocol.last_flush_at = time.time()
                log.info("final flush: %d rows", len(payload))
            except Exception as exc:
                flush_errors += 1
                log.error("final flush failed: %s", exc)
        write_stats(args.stats_file,
                    protocol.snapshot(agg, 0, flush_errors))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=6344)
    ap.add_argument("--forward-to", help="HOST:PORT to relay every datagram to (single-collector mode)")
    ap.add_argument("--dsn", default=os.environ.get(
        "DECAPD_DSN", "host=127.0.0.1 port=5432 dbname=traffic user=pmacct"))
    ap.add_argument("--flush", type=float, default=60.0, help="seconds between DB flushes")
    ap.add_argument("--replay", help="decode a pcap offline instead of listening")
    ap.add_argument("--stats-file",
                    default=os.environ.get("DECAPD_STATS_FILE",
                                           "/var/lib/traffic-viz/decapd-stats.json"),
                    help="JSON counters consumed by the Zabbix health check")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
