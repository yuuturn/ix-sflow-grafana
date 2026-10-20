#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Refresh the IP -> ASN table from a keyless public source (iptoasn.com).
#
#   https://iptoasn.com/data/ip2asn-v4.tsv.gz
#   https://iptoasn.com/data/ip2asn-v6.tsv.gz
#
# iptoasn.com data is public domain and needs no account or license key, which
# makes it a drop-in replacement for MaxMind GeoLite2-ASN. If you later get a
# MaxMind licence key, swap download_data() for the GeoLite2-ASN-CSV downloads
# and keep the rest of this script unchanged.
#
# Loading is atomic: a TEMP staging table is filled, then asn_range is
# truncated and refilled in one transaction, so dashboards never see a
# half-loaded table.
# ---------------------------------------------------------------------------
set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/traffic-viz}"
CONTAINER="${CONTAINER:-tv-postgres}"
DB_USER="${DB_USER:-tvadmin}"
DB_NAME="${DB_NAME:-traffic}"
WORK_DIR="${WORK_DIR:-/var/tmp/traffic-viz-asn}"

mkdir -p "$WORK_DIR"
TSV="$WORK_DIR/asn.tsv"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

download_data() {
  local url base
  : > "$TSV"
  for f in ip2asn-v4 ip2asn-v6; do
    url="https://iptoasn.com/data/${f}.tsv.gz"
    base="$WORK_DIR/${f}.tsv.gz"
    log "downloading $url"
    if ! curl -fsSL --retry 3 --retry-delay 5 -m 300 -o "$base" "$url"; then
      echo "ERROR: download failed: $url" >&2
      return 1
    fi
    if ! gzip -t "$base" 2>/dev/null; then
      echo "ERROR: downloaded file is not valid gzip: $base" >&2
      return 1
    fi
  done

  log "converting to integer ranges"
  python3 - "$WORK_DIR" "$TSV" <<'PY'
import gzip
import sys
from ipaddress import ip_address

workdir, out_path = sys.argv[1], sys.argv[2]
rows = 0

with open(out_path, "w", encoding="utf-8") as out:
    for name in ("ip2asn-v4", "ip2asn-v6"):
        path = f"{workdir}/{name}.tsv.gz"
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                # iptoasn column order is: start, end, AS, COUNTRY, DESCRIPTION
                start, end, asn, country = parts[0], parts[1], parts[2], parts[3]
                desc = parts[4] if len(parts) > 4 else ""
                try:
                    start_n = int(ip_address(start))
                    end_n = int(ip_address(end))
                except ValueError:
                    continue
                if end_n < start_n:
                    continue
                # tab-safe: drop any tab/newline inside the description
                desc = desc.replace("\t", " ").replace("\n", " ").strip()
                country = country.replace("\t", " ").strip()
                if not desc:
                    desc = f"AS{asn}"
                out.write(f"{start_n}\t{end_n}\t{asn}\t{desc}\t{country}\n")
                rows += 1

if rows < 1000:
    raise SystemExit(f"ERROR: only {rows} rows parsed - refusing to load")
print(f"parsed {rows} ranges", file=sys.stderr)
PY

  log "parsed $(wc -l < "$TSV") rows"
}

load_into_db() {
  log "loading into $DB_NAME.asn_range"
  {
    echo "BEGIN;"
    echo "SET TIME ZONE 'UTC';"
    echo "CREATE TEMP TABLE stg_asn (LIKE asn_range) ON COMMIT DROP;"
    echo "COPY stg_asn (ip_start, ip_end, asn, org, country) FROM STDIN;"
    cat "$TSV"
    echo "\\."
    echo "TRUNCATE asn_range;"
    echo "INSERT INTO asn_range (ip_start, ip_end, asn, org, country)"
    echo "  SELECT ip_start, ip_end, asn, org, country FROM stg_asn;"
    # resolved IPs may now point at a different ASN: drop the cache so that
    # refresh_ip_asn() rebuilds it against the new data
    echo "TRUNCATE ip_asn;"
    echo "INSERT INTO asn_meta (key, value, updated_at)"
    echo "  VALUES ('source', 'iptoasn.com/ip2asn-v4.tsv.gz + ip2asn-v6.tsv.gz', now())"
    echo "  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();"
    echo "COMMIT;"
  } | docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U "$DB_USER" -d "$DB_NAME"

  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -q -U "$DB_USER" -d "$DB_NAME" \
    -c "ANALYZE asn_range;" \
    -c "SELECT count(*) AS ranges, pg_size_pretty(pg_total_relation_size('asn_range')) AS size FROM asn_range;"
}

main() {
  download_data
  load_into_db
  log "done"
}

main "$@"
