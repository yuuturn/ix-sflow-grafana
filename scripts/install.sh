#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install / update the stack. Run as root from /opt/traffic-viz:
#
#   sudo ./scripts/install.sh                 # default: router -> sfacctd:6343
#   sudo ./scripts/install.sh --with-decapd   # fallback for firmware without `out`
#
# The default deployment samples the LAN port in both directions
# (`sflow sampling-rate <rate> in` / `out`), so sfacctd receives plain IPv4 and
# listens on udp/6343 directly.
#
# --with-decapd is only for devices that cannot sample egress: the WAN ingress
# side then carries MAP-E (IPv4-in-IPv6) samples, decapd takes udp/6343, pulls
# the inner IPv4 out of the sampled header and relays the rest to sfacctd on
# udp/6345.
#
# Idempotent: safe to re-run after editing any config file.
# ---------------------------------------------------------------------------
set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/traffic-viz}"
ENV_FILE="$STACK_DIR/.env"
WITH_DECAPD=0

while [ $# -gt 0 ]; do
    case "$1" in
        --with-decapd) WITH_DECAPD=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown option '$1'" >&2; exit 1 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo)" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE missing" >&2; exit 1; }

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

echo "==> docker compose up"
cd "$STACK_DIR"
# docker-entrypoint-initdb.d scripts are sourced by the entrypoint and must be
# readable/executable for the postgres user inside the container
chmod 0755 "$STACK_DIR"/sql/init/*.sh
chmod 0644 "$STACK_DIR"/sql/init/*.sql
docker compose up -d

echo "==> container service user"
if ! getent passwd pmacct >/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin pmacct
    echo "    created system user 'pmacct'"
fi
install -d -o pmacct -g pmacct -m 0750 /var/log/pmacct /run/pmacct
# metrics are read by the Zabbix agent container as an unprivileged user
install -d -o pmacct -g pmacct -m 0755 /var/lib/traffic-viz /var/lib/traffic-viz/metrics

if [ "$WITH_DECAPD" -eq 1 ]; then
    echo "==> python dependency for decapd"
    python3 -c "import psycopg2" 2>/dev/null || \
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3-psycopg2 >/dev/null
fi

echo "==> /etc/pmacct/sfacctd.conf"
if [ "$WITH_DECAPD" -eq 1 ]; then
    SFACCTD_PORT=6345   # decapd relays to us
else
    SFACCTD_PORT=6343   # the router sends straight to us
fi
install -d -m 0755 /etc/pmacct
sed -e "s|__PMACCT_PASSWORD__|${PG_PMACCT_PASSWORD}|" \
    -e "s|^sfacctd_port: 6343$|sfacctd_port: ${SFACCTD_PORT}|" \
    "$STACK_DIR/pmacct/sfacctd.conf" > /etc/pmacct/sfacctd.conf
chown root:pmacct /etc/pmacct/sfacctd.conf
chmod 0640 /etc/pmacct/sfacctd.conf

if [ "$WITH_DECAPD" -eq 1 ]; then
    echo "==> decapd environment"
    install -d -m 0755 /etc/traffic-viz
    cat > /etc/traffic-viz/decapd.env <<EOF
DECAPD_DSN=host=127.0.0.1 port=5432 dbname=traffic user=pmacct password=${PG_PMACCT_PASSWORD}
DECAPD_ARGS=--port 6343 --forward-to 127.0.0.1:6345
EOF
    chmod 0640 /etc/traffic-viz/decapd.env
fi

echo "==> systemd units"
install -m 0644 "$STACK_DIR"/systemd/*.service "$STACK_DIR"/systemd/*.timer /etc/systemd/system/
if [ "$WITH_DECAPD" -eq 1 ]; then
    install -m 0644 "$STACK_DIR"/optional/decapd/decapd.service /etc/systemd/system/
fi
chmod 0755 "$STACK_DIR"/scripts/*.sh
systemctl daemon-reload

echo "==> enable timers"
systemctl enable --now traffic-viz-rollup.timer
systemctl enable --now traffic-viz-prune.timer
systemctl enable --now traffic-viz-asn.timer
systemctl enable --now traffic-viz-health.timer

if [ "$WITH_DECAPD" -eq 1 ]; then
    echo "==> enable decapd (sFlow entry point) + sfacctd"
    systemctl enable --now decapd.service
else
    echo "==> disable decapd (not used) + enable sfacctd"
    systemctl disable --now decapd.service 2>/dev/null || true
fi
systemctl enable sfacctd.service
# restart so config edits are picked up on re-run
systemctl restart sfacctd.service
sleep 3
systemctl --no-pager --lines=5 status sfacctd.service || true

echo
if [ "$WITH_DECAPD" -eq 1 ]; then
    echo "mode     : WAN ingress + decapd (fallback for firmware without 'out')"
    echo "sFlow in : decapd udp/6343 -> sfacctd udp/6345"
else
    echo "mode     : LAN ingress + egress (default)"
    echo "sFlow in : sfacctd udp/6343"
fi
echo "Grafana  : http://192.0.2.10:3000/  (admin / see \$GRAFANA_ADMIN_PASSWORD in $ENV_FILE)"
echo "listening: $(ss -lnuH 'sport = :6343' | awk '{print $4}' | paste -sd, -)"
