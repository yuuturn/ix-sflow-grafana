#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Publish one small file per metric under /var/lib/traffic-viz/metrics/.
#
# Zabbix (running on another host) reads them through the collector agent, which
# has the host root mounted at /hostfs:
#
#   vfs.file.contents[/hostfs/var/lib/traffic-viz/metrics/<name>]
#
# One file per metric means new metrics need no agent-side change at all.
# Every file is (re)written on every run, so a stale file always means the
# check itself stopped -- use vfs.file.time[...,mtime] to detect that.
# ---------------------------------------------------------------------------
set -uo pipefail

STACK_DIR="${STACK_DIR:-/opt/traffic-viz}"
CONTAINER="${CONTAINER:-tv-postgres}"
DB_USER="${DB_USER:-tvadmin}"
DB_NAME="${DB_NAME:-traffic}"
STATS_FILE="${STATS_FILE:-/var/lib/traffic-viz/decapd-stats.json}"
OUT_DIR="${OUT_DIR:-/var/lib/traffic-viz/metrics}"

mkdir -p "$OUT_DIR"
chmod 755 /var/lib/traffic-viz "$OUT_DIR" 2>/dev/null || true

put() {  # put <name> <value>
  printf '%s\n' "$2" > "$OUT_DIR/$1.tmp" 2>/dev/null && mv "$OUT_DIR/$1.tmp" "$OUT_DIR/$1"
  chmod 644 "$OUT_DIR/$1" 2>/dev/null || true
}

now=$(date +%s)
ok=0

# --- systemd units -------------------------------------------------------
for unit in decapd sfacctd; do
  state=$(systemctl is-active "$unit" 2>/dev/null)
  [ "$state" = "active" ] && put "trafficviz_${unit}_active" 1 || put "trafficviz_${unit}_active" 0
done

# --- decapd internal counters -------------------------------------------
if [ -r "$STATS_FILE" ]; then
  eval "$(python3 - "$STATS_FILE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for k in ("datagrams", "flow_samples", "map_e_samples", "decode_errors",
          "relay_errors", "last_flush_rows", "flush_errors", "pending_keys"):
    print(f'v_{k}="{int(d.get(k) or 0)}"')
print(f'v_last_flush="{int(d.get("last_flush_at") or 0)}"')
print(f'v_last_cycle="{int(d.get("last_cycle_at") or 0)}"')
print(f'v_started="{int(d.get("started_at") or 0)}"')
print(f'v_relay="{1 if d.get("relay_enabled") else 0}"')
PY
)"
  put trafficviz_decapd_datagrams     "${v_datagrams:-0}"
  put trafficviz_decapd_flow_samples  "${v_flow_samples:-0}"
  put trafficviz_decapd_mape_samples  "${v_map_e_samples:-0}"
  put trafficviz_decapd_decode_errors "${v_decode_errors:-0}"
  put trafficviz_decapd_relay_errors  "${v_relay_errors:-0}"
  put trafficviz_decapd_flush_errors  "${v_flush_errors:-0}"
  put trafficviz_decapd_flush_rows    "${v_last_flush_rows:-0}"
  put trafficviz_decapd_pending_keys  "${v_pending_keys:-0}"
  put trafficviz_decapd_relay_enabled "${v_relay:-0}"

  # flush_age = age of the collection loop heartbeat. It must NOT be based on
  # the last DB write: decapd only writes when there is MAP-E traffic, so a
  # quiet hour would look like a stall (this caused two false alerts).
  if [ "${v_last_cycle:-0}" -gt 0 ]; then
    put trafficviz_decapd_flush_age $(( now - v_last_cycle ))
  elif [ "${v_last_flush:-0}" -gt 0 ]; then
    put trafficviz_decapd_flush_age $(( now - v_last_flush ))
  else
    # never flushed yet: fall back to process age so the item is not "unknown"
    if [ "${v_started:-0}" -gt 0 ]; then
      put trafficviz_decapd_flush_age $(( now - v_started ))
    else
      put trafficviz_decapd_flush_age 999999
    fi
  fi
  [ "${v_started:-0}" -gt 0 ] && put trafficviz_decapd_uptime $(( now - v_started )) \
                             || put trafficviz_decapd_uptime 0
  # informational only: grows naturally when MAP-E traffic is sparse
  if [ "${v_last_flush:-0}" -gt 0 ]; then
    put trafficviz_decapd_write_age $(( now - v_last_flush ))
  else
    put trafficviz_decapd_write_age 0
  fi
  put trafficviz_stats_age $(( now - $(stat -c %Y "$STATS_FILE" 2>/dev/null || echo 0) ))
else
  for m in datagrams flow_samples mape_samples decode_errors relay_errors \
           flush_errors flush_rows pending_keys; do
    put "trafficviz_decapd_${m}" 0
  done
  put trafficviz_decapd_flush_age 999999
  put trafficviz_decapd_uptime 0
  put trafficviz_stats_age 999999
fi

# --- end-to-end freshness from the database ------------------------------
db_out=$(docker exec -i "$CONTAINER" psql -q -U "$DB_USER" -d "$DB_NAME" -tAF'|' -c "
SELECT
  COALESCE(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'utc')
             - (SELECT max(stamp_updated) FROM flows)))::bigint, -1),
  COALESCE(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'utc')
             - (SELECT max(bucket) FROM flow_5m)))::bigint, -1),
  (SELECT count(*) FROM flows WHERE stamp_inserted >= (now() AT TIME ZONE 'utc') - interval '1 hour'),
  (SELECT count(*) FROM flows_decap),
  (SELECT count(*) FROM ip_asn);
" 2>/dev/null)

if [ -n "$db_out" ]; then
  IFS='|' read -r ingest_age rollup_age flows_1h decap_rows ip_asn_rows <<< "$db_out"
  put trafficviz_sflow_ingest_age "$ingest_age"
  put trafficviz_rollup_age      "$rollup_age"
  put trafficviz_flows_1h        "$flows_1h"
  put trafficviz_decap_rows      "$decap_rows"
  put trafficviz_ip_asn_rows     "$ip_asn_rows"
  put trafficviz_db_ok 1
else
  put trafficviz_sflow_ingest_age 999999
  put trafficviz_rollup_age 999999
  put trafficviz_flows_1h 0
  put trafficviz_decap_rows 0
  put trafficviz_ip_asn_rows 0
  put trafficviz_db_ok 0
fi

# --- container state (read through the docker socket in the agent image) --
for c in tv-postgres tv-grafana; do
  state=$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
  [ "$state" = "true" ] && put "trafficviz_${c}_running" 1 || put "trafficviz_${c}_running" 0
done

# --- one file that always exists: proves the check itself is alive --------
put trafficviz_check_ok 1
exit 0
