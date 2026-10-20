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
OUT_DIR="${OUT_DIR:-/var/lib/traffic-viz/metrics}"

mkdir -p "$OUT_DIR"
chmod 755 /var/lib/traffic-viz "$OUT_DIR" 2>/dev/null || true

put() {  # put <name> <value>
  printf '%s\n' "$2" > "$OUT_DIR/$1.tmp" 2>/dev/null && mv "$OUT_DIR/$1.tmp" "$OUT_DIR/$1"
  chmod 644 "$OUT_DIR/$1" 2>/dev/null || true
}

# --- systemd units -------------------------------------------------------
for unit in sfacctd; do
  state=$(systemctl is-active "$unit" 2>/dev/null)
  [ "$state" = "active" ] && put "trafficviz_${unit}_active" 1 || put "trafficviz_${unit}_active" 0
done

# --- end-to-end freshness from the database ------------------------------
db_out=$(docker exec -i "$CONTAINER" psql -q -U "$DB_USER" -d "$DB_NAME" -tAF'|' -c "
SELECT
  COALESCE(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'utc')
             - (SELECT max(stamp_updated) FROM flows)))::bigint, -1),
  COALESCE(EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'utc')
             - (SELECT max(bucket) FROM flow_5m)))::bigint, -1),
  (SELECT count(*) FROM flows WHERE stamp_inserted >= (now() AT TIME ZONE 'utc') - interval '1 hour'),
  (SELECT count(*) FROM ip_asn);
" 2>/dev/null)

if [ -n "$db_out" ]; then
  IFS='|' read -r ingest_age rollup_age flows_1h ip_asn_rows <<< "$db_out"
  put trafficviz_sflow_ingest_age "$ingest_age"
  put trafficviz_rollup_age      "$rollup_age"
  put trafficviz_flows_1h        "$flows_1h"
  put trafficviz_ip_asn_rows     "$ip_asn_rows"
  put trafficviz_db_ok 1
else
  put trafficviz_sflow_ingest_age 999999
  put trafficviz_rollup_age 999999
  put trafficviz_flows_1h 0
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
