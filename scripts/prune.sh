#!/usr/bin/env bash
# Daily retention job. Raw flow rows keep 7 days (forensics), the rollup keeps
# 45 days (that is what the dashboards read).
set -euo pipefail

CONTAINER="${CONTAINER:-tv-postgres}"
DB_USER="${DB_USER:-tvadmin}"
DB_NAME="${DB_NAME:-traffic}"
KEEP_FLOWS="${KEEP_FLOWS:-7 days}"
KEEP_ROLLUP="${KEEP_ROLLUP:-45 days}"

out=$(docker exec -i "$CONTAINER" psql -q -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
      -tAc "SELECT 'deleted_flows=' || d.deleted_flows || ' deleted_rollup=' || d.deleted_rollup
            FROM prune_traffic('${KEEP_FLOWS}', '${KEEP_ROLLUP}') d;")

echo "[$(date -u +%FT%TZ)] $out"
