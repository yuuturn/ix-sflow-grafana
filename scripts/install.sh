#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install / update the stack. Run as root from /opt/traffic-viz:
#
#   sudo ./scripts/install.sh
#
# Idempotent: safe to re-run after editing any config file.
# ---------------------------------------------------------------------------
set -euo pipefail

STACK_DIR="${STACK_DIR:-/opt/traffic-viz}"
ENV_FILE="$STACK_DIR/.env"

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo)" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE missing" >&2; exit 1; }

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

echo "==> 1/6 docker compose up"
cd "$STACK_DIR"
# docker-entrypoint-initdb.d scripts are sourced by the entrypoint and must be
# readable/executable for the postgres user inside the container
chmod 0755 "$STACK_DIR"/sql/init/*.sh
chmod 0644 "$STACK_DIR"/sql/init/*.sql
docker compose up -d

echo "==> 2/6 container service user"
if ! getent passwd pmacct >/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin pmacct
    echo "    created system user 'pmacct'"
fi
install -d -o pmacct -g pmacct -m 0750 /var/log/pmacct /run/pmacct
# metrics are read by the Zabbix agent container as an unprivileged user
install -d -o pmacct -g pmacct -m 0755 /var/lib/traffic-viz /var/lib/traffic-viz/metrics

echo "==> 2b/6 python dependency for decapd"
python3 -c "import psycopg2" 2>/dev/null || \
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-psycopg2 >/dev/null

echo "==> 3/6 /etc/pmacct/sfacctd.conf"
install -d -m 0755 /etc/pmacct
sed "s|__PMACCT_PASSWORD__|${PG_PMACCT_PASSWORD}|" \
    "$STACK_DIR/pmacct/sfacctd.conf" > /etc/pmacct/sfacctd.conf
chown root:pmacct /etc/pmacct/sfacctd.conf
chmod 0640 /etc/pmacct/sfacctd.conf

echo "==> 3b/6 decapd environment"
install -d -m 0755 /etc/traffic-viz
cat > /etc/traffic-viz/decapd.env <<EOF
DECAPD_DSN=host=127.0.0.1 port=5432 dbname=traffic user=pmacct password=${PG_PMACCT_PASSWORD}
DECAPD_ARGS=--port 6343 --forward-to 127.0.0.1:6345
EOF
chmod 0640 /etc/traffic-viz/decapd.env

echo "==> 4/6 systemd units"
install -m 0644 "$STACK_DIR"/systemd/*.service "$STACK_DIR"/systemd/*.timer /etc/systemd/system/
chmod 0755 "$STACK_DIR"/scripts/*.sh
systemctl daemon-reload

echo "==> 5/6 enable timers"
systemctl enable --now traffic-viz-rollup.timer
systemctl enable --now traffic-viz-prune.timer
systemctl enable --now traffic-viz-asn.timer
systemctl enable --now traffic-viz-health.timer

echo "==> 6/6 enable decapd (sFlow entry point) + sfacctd"
systemctl enable --now decapd.service
systemctl enable sfacctd.service
# restart so config edits are picked up on re-run
systemctl restart sfacctd.service
sleep 3
systemctl --no-pager --lines=5 status sfacctd.service || true

echo
echo "Grafana : http://192.0.2.10:3000/  (admin / see \$GRAFANA_ADMIN_PASSWORD in $ENV_FILE)"
echo "sFlow in : $(ss -lnuH 'sport = :6343' | awk '{print $4}' | paste -sd, -  )"
echo "pmacct in: $(ss -lnuH 'sport = :6345' | awk '{print $4}' | paste -sd, -  )"
