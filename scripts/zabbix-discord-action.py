#!/usr/bin/env python3
"""
Create a Zabbix action that sends traffic-viz problems to Discord.

Scoped by the event tag `component=traffic-viz`, so the existing (disabled)
catch-all Discord action stays untouched and unrelated alerts are unaffected.

    ZABBIX_TOKEN=... ./zabbix-discord-action.py
    ZABBIX_TOKEN=... ./zabbix-discord-action.py --dry-run

Idempotent: an action with the same name is updated in place.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

COMPONENT = "traffic-viz"
DEFAULT_NAME = "traffic-viz → Discord 通知"

PROBLEM_MSG = """{EVENT.SEVERITY}: {EVENT.NAME}

ホスト: {HOST.NAME}
時刻: {EVENT.DATE} {EVENT.TIME}
内容: {EVENT.OPDATA}

詳細: {EVENT.ID}"""

RECOVERY_MSG = """復旧: {EVENT.NAME}

ホスト: {HOST.NAME}
時刻: {EVENT.DATE} {EVENT.TIME}
継続時間: {EVENT.DURATION}"""


class Zabbix:
    def __init__(self, url, token, dry_run=False):
        self.url, self.token, self.dry_run = url, token, dry_run
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
    ap.add_argument("--api", default=os.environ.get("ZABBIX_API",
                                                    "http://127.0.0.1:8081/api_jsonrpc.php"))
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--mediatype", default="Discord", help="media type name")
    ap.add_argument("--user", default="discord-notify", help="Zabbix user that owns the webhook")
    ap.add_argument("--min-severity", type=int, default=2,
                    help="0=not classified .. 5=disaster (default 2 = Warning)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("ZABBIX_TOKEN")
    if not token:
        raise SystemExit("ZABBIX_TOKEN is not set")
    zbx = Zabbix(args.api, token, args.dry_run)

    mt = zbx.call("mediatype.get", {"filter": {"name": [args.mediatype]},
                                    "output": ["mediatypeid", "name", "status", "type"]})
    if not mt:
        raise SystemExit(f"media type {args.mediatype!r} not found")
    mt = mt[0]
    if mt["status"] != "0":
        raise SystemExit(f"media type {args.mediatype!r} is disabled")

    usr = zbx.call("user.get", {"filter": {"username": [args.user]},
                                "output": ["userid", "username"],
                                "selectMedias": ["mediatypeid", "sendto", "active"]})
    if not usr:
        raise SystemExit(f"user {args.user!r} not found")
    usr = usr[0]
    media = next((m for m in (usr.get("medias") or [])
                  if m["mediatypeid"] == mt["mediatypeid"]), None)
    if not media:
        raise SystemExit(f"user {args.user!r} has no {args.mediatype!r} media configured")
    if media["active"] != "0":
        raise SystemExit(f"the {args.mediatype!r} media of {args.user!r} is disabled")
    print(f"media type {mt['name']} (id={mt['mediatypeid']}) -> user {usr['username']} "
          f"(id={usr['userid']}) webhook {media['sendto'][:60]}...")

    # 3 = trigger name, operator 2 = "like"; every traffic-viz trigger is
    # named "traffic-viz: ..." so this matches exactly that set. (Tag
    # conditions exist too - conditiontype 26 - but their parameter layout
    # differs between Zabbix versions, so the name filter is the safe choice.)
    conditions = [
        {"conditiontype": 3, "operator": 2, "value": "traffic-viz"},
    ]
    if args.min_severity > 0:
        # 4 = trigger severity; operator 5 = ">="
        conditions.append({"conditiontype": 4, "operator": 5, "value": str(args.min_severity)})

    params = {
        "name": args.name,
        "eventsource": 0,          # trigger events
        "status": 0,               # enabled
        "esc_period": "1h",
        # Zabbix 7+ keeps action conditions inside a filter object
        "filter": {"evaltype": 0, "conditions": conditions},
        "operations": [{
            "operationtype": 0,    # send message
            "esc_step_from": 1,
            "esc_step_to": 1,
            "opmessage": {"default_msg": 0, "subject": "{EVENT.SEVERITY}: {EVENT.NAME}",
                          "message": PROBLEM_MSG, "mediatypeid": mt["mediatypeid"]},
            "opmessage_usr": [{"userid": usr["userid"]}],
        }],
        "recovery_operations": [{
            "operationtype": 0,
            "opmessage": {"default_msg": 0, "subject": "RESOLVED: {EVENT.NAME}",
                          "message": RECOVERY_MSG, "mediatypeid": mt["mediatypeid"]},
            "opmessage_usr": [{"userid": usr["userid"]}],
        }],
    }

    existing = zbx.call("action.get", {"filter": {"name": [args.name]},
                                       "output": ["actionid", "name", "status"]})
    if args.dry_run:
        print("  [dry-run] would", "update" if existing else "create", args.name)
        print(json.dumps(params, ensure_ascii=False, indent=2)[:800])
        return 0

    if existing:
        params["actionid"] = existing[0]["actionid"]
        zbx.call("action.update", params)
        print(f"action updated: {args.name} (id={existing[0]['actionid']})")
    else:
        res = zbx.call("action.create", params)
        print(f"action created: {args.name} (id={res['actionids'][0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
