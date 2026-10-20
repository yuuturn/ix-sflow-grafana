#!/bin/bash
# Creates the two least-privilege roles used by the stack.
#   pmacct  -> write-only-ish access to the flow tables
#   grafana -> read-only access to the reporting views
set -euo pipefail

echo "[init] creating roles pmacct / grafana"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pmacct') THEN
    CREATE ROLE pmacct LOGIN PASSWORD '${PMACCT_PASSWORD}';
  ELSE
    ALTER ROLE pmacct WITH LOGIN PASSWORD '${PMACCT_PASSWORD}';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana') THEN
    CREATE ROLE grafana LOGIN PASSWORD '${GRAFANA_PASSWORD}';
  ELSE
    ALTER ROLE grafana WITH LOGIN PASSWORD '${GRAFANA_PASSWORD}';
  END IF;
END
\$\$;
EOSQL
