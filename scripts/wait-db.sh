#!/usr/bin/env bash
# Block until the PostgreSQL container accepts connections.
# Used as ExecStartPre for sfacctd so a reboot does not leave the collector
# dead if Docker comes up slowly on the Pi.
set -euo pipefail

HOST="${DB_HOST:-127.0.0.1}"
PORT="${DB_PORT:-5432}"
DEADLINE=$(( $(date +%s) + 90 ))

while :; do
  if timeout 2 bash -c "</dev/tcp/${HOST}/${PORT}" 2>/dev/null; then
    exit 0
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "wait-db: ${HOST}:${PORT} not reachable after 90s" >&2
    exit 1
  fi
  sleep 2
done
