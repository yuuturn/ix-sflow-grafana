#!/usr/bin/env python3
"""
Create the traffic-viz monitoring items and triggers in Zabbix.

Idempotent: items are matched by key_, triggers by description. Existing
objects are updated in place, so the script can be re-run after a change.

    ZABBIX_TOKEN=... ./zabbix-setup.py
    ZABBIX_TOKEN=... ./zabbix-setup.py --host collector --dry-run

Environment:
    ZABBIX_TOKEN  API token (required)
    ZABBIX_API    default http://127.0.0.1:8081/api_jsonrpc.php
    ZABBIX_HOST   technical host name, default "collector"
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

METRIC_DIR = "/hostfs/var/lib/traffic-viz/metrics"
COMPONENT = "traffic-viz"

# (metric name, units, description)
ITEMS = [
    ("trafficviz_decapd_active", "", "decapd (sFlow collector) is active: 1/0"),
    ("trafficviz_sfacctd_active", "", "sfacctd (pmacct) is active: 1/0"),
    ("trafficviz_decapd_uptime", "uptime", "decapd process uptime in seconds"),
    ("trafficviz_decapd_flush_age", "s",
     "seconds since the last decapd collection cycle (heartbeat; advances even "
     "when there is no MAP-E traffic to write)"),
    ("trafficviz_decapd_write_age", "s",
     "informational: seconds since the last successful write to flows_decap "
     "(grows naturally when MAP-E traffic is sparse - do not alert on it)"),
    ("trafficviz_decapd_datagrams", "", "sFlow datagrams received (cumulative)"),
    ("trafficviz_decapd_flow_samples", "", "sFlow flow samples seen (cumulative)"),
    ("trafficviz_decapd_mape_samples", "", "MAP-E samples decapsulated (cumulative)"),
    ("trafficviz_decapd_decode_errors", "", "sFlow decode errors (cumulative)"),
    ("trafficviz_decapd_relay_errors", "", "datagrams that could not be relayed to pmacct"),
    ("trafficviz_decapd_flush_errors", "", "failed PostgreSQL flushes (cumulative)"),
    ("trafficviz_decapd_flush_rows", "", "rows written by the last flush"),
    ("trafficviz_decapd_pending_keys", "", "aggregated keys waiting for the next flush"),
    ("trafficviz_decapd_relay_enabled", "", "decapd relays to pmacct: 1/0"),
    ("trafficviz_stats_age", "s", "seconds since decapd refreshed its stats file"),
    ("trafficviz_sflow_ingest_age", "s", "seconds since pmacct last updated the flows table"),
    ("trafficviz_rollup_age", "s", "seconds since the newest 5-minute rollup bucket started"),
    ("trafficviz_db_ok", "", "PostgreSQL reachable from the health check: 1/0"),
    ("trafficviz_flows_1h", "", "raw flow rows in the last hour"),
    ("trafficviz_decap_rows", "", "total rows in flows_decap"),
    ("trafficviz_ip_asn_rows", "", "entries in the IP->ASN dictionary"),
    ("trafficviz_tv-postgres_running", "", "tv-postgres container running: 1/0"),
    ("trafficviz_tv-grafana_running", "", "tv-grafana container running: 1/0"),
    ("trafficviz_check_ok", "", "always 1; its mtime proves the check itself runs"),
]

# the marker item whose mtime is checked separately
MTIME_ITEM = {
    "name": "trafficviz check mtime",
    "key_": f"vfs.file.time[{METRIC_DIR}/trafficviz_check_ok,modify]",
    "description": "epoch of the last health check run (detects a stopped timer)",
}

def key_for(metric):
    """Item key for a metric file published by health.sh."""
    return f"vfs.file.contents[{METRIC_DIR}/{metric}]"


def last_expr(host, metric, op, value):
    # the trigger must reference the item KEY (the vfs.file.contents path),
    # not the human readable item name
    return f"last(/{host}/{key_for(metric)}){op}{value}"


# (description, builder(host) -> expression, priority, comments)
# priority: 2 Warning, 3 Average, 4 High, 5 Disaster
TRIGGERS = [
    ("traffic-viz: decapd stopped",
     lambda h: last_expr(h, "trafficviz_decapd_active", "<>", 1), 5,
     "sFlow の入口が停止。全可視化が止まる。systemctl status decapd を確認。"),
    ("traffic-viz: sfacctd stopped",
     lambda h: last_expr(h, "trafficviz_sfacctd_active", "<>", 1), 4,
     "pmacct が停止。decapd は受けているが DB に書かれない。"),
    ("traffic-viz: sFlow ingest stalled",
     lambda h: last_expr(h, "trafficviz_sflow_ingest_age", ">", 300), 4,
     "pmacct が5分以上 flows を更新していない。装置側の設定・経路・decapd を確認。"),
    ("traffic-viz: decapd flush stalled",
     lambda h: last_expr(h, "trafficviz_decapd_flush_age", ">", 180), 4,
     "decapd が3分以上 DB へ書き込んでいない。"),
    ("traffic-viz: database unreachable",
     lambda h: last_expr(h, "trafficviz_db_ok", "<>", 1), 4,
     "PostgreSQL に接続できない。tv-postgres コンテナを確認。"),
    ("traffic-viz: decapd flush errors",
     lambda h: last_expr(h, "trafficviz_decapd_flush_errors", ">", 0), 4,
     "DB 書き込みに失敗した。権限・容量・接続を確認。"),
    ("traffic-viz: rollup stalled",
     # rollup_age is measured from the START of the newest 5-minute bucket, so
     # a healthy system already swings 0-300 s. The rollup timer runs every
     # 2 min; 15 min leaves room for a delayed run without false positives
     # (observed max over 14 h was 474 s).
     lambda h: last_expr(h, "trafficviz_rollup_age", ">", 900), 2,
     "5分ロールアップが15分以上更新されていない。ダッシュボードが古いままになる。"),
    ("traffic-viz: sFlow decode errors",
     lambda h: last_expr(h, "trafficviz_decapd_decode_errors", ">", 0), 2,
     "サンプルのデコードに失敗。装置側のファーム更新などで形式が変わった可能性。"),
    ("traffic-viz: relay errors",
     lambda h: last_expr(h, "trafficviz_decapd_relay_errors", ">", 0), 2,
     "decapd から pmacct へ中継できていない。"),
    ("traffic-viz: health check stalled",
     lambda h: f"now()-last(/{h}/{MTIME_ITEM['key_']})>300", 4,
     "監視自体が止まっている。traffic-viz-health.timer を確認。"),
    ("traffic-viz: postgres container down",
     lambda h: last_expr(h, "trafficviz_tv-postgres_running", "<>", 1), 4,
     "tv-postgres コンテナが停止。"),
    ("traffic-viz: grafana container down",
     lambda h: last_expr(h, "trafficviz_tv-grafana_running", "<>", 1), 2,
     "tv-grafana コンテナが停止。"),
]


class Zabbix:
    def __init__(self, url, token, dry_run=False):
        self.url = url
        self.token = token
        self.dry_run = dry_run
        self.seq = 0

    def call(self, method, params):
        self.seq += 1
        body = json.dumps({"jsonrpc": "2.0", "method": method,
                           "params": params, "id": self.seq}).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json-rpc",
                     "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                out = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method}: HTTP {exc.code}: {exc.read()[:300]!r}")
        if "error" in out:
            raise SystemExit(f"{method} failed: {out['error']}")
        return out["result"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ZABBIX_HOST", "collector"))
    ap.add_argument("--api", default=os.environ.get("ZABBIX_API",
                                                    "http://127.0.0.1:8081/api_jsonrpc.php"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("ZABBIX_TOKEN")
    if not token:
        raise SystemExit("ZABBIX_TOKEN is not set")

    zbx = Zabbix(args.api, token, args.dry_run)

    hosts = zbx.call("host.get", {"filter": {"host": [args.host]},
                                  "output": ["hostid", "host"],
                                  "selectInterfaces": ["interfaceid", "ip", "port", "type", "available"]})
    if not hosts:
        raise SystemExit(f"host {args.host!r} not found")
    host = hosts[0]
    hostid = host["hostid"]
    agent_if = next((i for i in host["interfaces"] if i["type"] == "1"), None)
    if not agent_if:
        raise SystemExit("no Zabbix agent interface on the host")
    print(f"host {args.host} (id={hostid}) agent interface {agent_if['ip']}:{agent_if['port']} "
          f"available={agent_if['available']}")

    existing = {i["key_"]: i for i in zbx.call(
        "item.get", {"hostids": hostid, "output": ["itemid", "key_", "name", "delay"]})}

    created = updated = 0
    item_keys = []
    wanted = [(name, f"vfs.file.contents[{METRIC_DIR}/{name}]", desc) for name, _u, desc in ITEMS]
    wanted.append((MTIME_ITEM["name"], MTIME_ITEM["key_"], MTIME_ITEM["description"]))

    for name, key, desc in wanted:
        item_keys.append(key)
        params = {
            "name": name, "key_": key, "hostid": hostid, "type": 0, "value_type": 3,
            "delay": "60s", "interfaceid": agent_if["interfaceid"],
            "history": "30d", "trends": "365d", "description": desc,
            "tags": [{"tag": "component", "value": COMPONENT}],
        }
        if key in existing:
            if args.dry_run:
                print(f"  [dry-run] update item {name}")
            else:
                # item.update rejects create-only fields such as hostid
                upd = {k: v for k, v in params.items() if k != "hostid"}
                upd["itemid"] = existing[key]["itemid"]
                zbx.call("item.update", upd)
                updated += 1
        else:
            if args.dry_run:
                print(f"  [dry-run] create item {name}")
            else:
                zbx.call("item.create", params)
                created += 1
    print(f"items: {created} created, {updated} updated, {len(wanted)} total")

    trig_existing = {t["description"]: t for t in zbx.call(
        "trigger.get", {"hostids": hostid, "output": ["triggerid", "description", "expression"]})}

    t_created = t_updated = 0
    for desc, builder, prio, comment in TRIGGERS:
        expression = builder(args.host)
        params = {"description": desc, "expression": expression,
                  "priority": prio, "comments": comment,
                  "tags": [{"tag": "component", "value": COMPONENT}]}
        if desc in trig_existing:
            if args.dry_run:
                print(f"  [dry-run] update trigger {desc}")
            else:
                params["triggerid"] = trig_existing[desc]["triggerid"]
                zbx.call("trigger.update", params)
                t_updated += 1
        else:
            if args.dry_run:
                print(f"  [dry-run] create trigger {desc}")
            else:
                zbx.call("trigger.create", params)
                t_created += 1
    print(f"triggers: {t_created} created, {t_updated} updated, {len(TRIGGERS)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
