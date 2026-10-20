#!/usr/bin/env bash
# Every 2 minutes: resolve newly seen IPs to ASNs, then rebuild the rollup for
# the recent window (the window covers the in-flight 5-minute bucket that
# pmacct keeps updating, plus a little slack for late flushes).
set -euo pipefail

CONTAINER="${CONTAINER:-tv-postgres}"
DB_USER="${DB_USER:-tvadmin}"
DB_NAME="${DB_NAME:-traffic}"

out=$(docker exec -i "$CONTAINER" psql -q -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
      -tAc "SELECT 'resolved_ips=' || refresh_ip_asn()
                 || ' rollup_rows=' || refresh_flow_5m();")

echo "[$(date -u +%FT%TZ)] $out"
