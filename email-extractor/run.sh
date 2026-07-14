#!/usr/bin/env bash
# Add-on entrypoint.
#
# Two database modes, decided by the add-on options:
#   - pg_dsn set      -> external Postgres, this script only starts the app.
#   - pg_dsn empty    -> BUNDLED Postgres: a local cluster lives on the add-on
#     + pg_password      /data volume (so HA backups snapshot DB + file store
#                        together) and is reachable for n8n on port 5432 under
#                        the add-on hostname. Role/db "email" is ensured on
#                        every start; the app connects via 127.0.0.1.
set -euo pipefail

OPTS=/data/options.json
PGDATA=/data/postgres
PGBIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
PGLOG=/data/postgres.log

opt() {
  python3 - "$1" <<'PY'
import json, sys
try:
    print(json.load(open("/data/options.json")).get(sys.argv[1]) or "")
except Exception:
    print("")
PY
}

PG_DSN="$(opt pg_dsn)"
PG_PASSWORD="$(opt pg_password)"

start_bundled_pg() {
  if [ -z "$PG_PASSWORD" ]; then
    echo "FATAL: option pg_password is required when pg_dsn is empty (bundled Postgres mode)." >&2
    exit 1
  fi
  mkdir -p "$PGDATA"
  chown -R postgres:postgres "$PGDATA"
  chmod 700 "$PGDATA"
  if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "Initializing bundled Postgres cluster in $PGDATA"
    su -s /bin/bash postgres -c "$PGBIN/initdb -D '$PGDATA' --encoding=UTF8 --auth-local=peer --auth-host=scram-sha-256"
  fi
  # Idempotent network config: listen on all interfaces, allow the hassio
  # docker subnet (n8n add-on) with password auth.
  grep -q "^listen_addresses" "$PGDATA/postgresql.conf" \
    || echo "listen_addresses = '*'" >> "$PGDATA/postgresql.conf"
  grep -q "172.30.32.0/23" "$PGDATA/pg_hba.conf" \
    || echo "host all all 172.30.32.0/23 scram-sha-256" >> "$PGDATA/pg_hba.conf"
  grep -q "127.0.0.1/32" "$PGDATA/pg_hba.conf" \
    || echo "host all all 127.0.0.1/32 scram-sha-256" >> "$PGDATA/pg_hba.conf"
  su -s /bin/bash postgres -c "$PGBIN/pg_ctl -D '$PGDATA' -w -t 60 -l '$PGLOG' start"
  # Ensure role + database exist and the password matches the option.
  su -s /bin/bash postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='email'\"" | grep -q 1 \
    || su -s /bin/bash postgres -c "psql -c \"CREATE ROLE email LOGIN\""
  PGPASS_SQL=$(printf "%s" "$PG_PASSWORD" | sed "s/'/''/g")
  su -s /bin/bash postgres -c "psql -c \"ALTER ROLE email LOGIN PASSWORD '$PGPASS_SQL'\""
  su -s /bin/bash postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='email'\"" | grep -q 1 \
    || su -s /bin/bash postgres -c "createdb -O email email"
  echo "Bundled Postgres ready (cluster $PGDATA, db email)."
}

stop_bundled_pg() {
  su -s /bin/bash postgres -c "$PGBIN/pg_ctl -D '$PGDATA' -m fast stop" || true
}

BUNDLED=0
if [ -z "$PG_DSN" ]; then
  BUNDLED=1
  start_bundled_pg
fi

shutdown() {
  echo "Shutting down..."
  [ -n "${APP_PID:-}" ] && kill -TERM "$APP_PID" 2>/dev/null || true
  wait "${APP_PID:-}" 2>/dev/null || true
  [ "$BUNDLED" = "1" ] && stop_bundled_pg
  exit 0
}
trap shutdown TERM INT

python -m app.main &
APP_PID=$!
wait "$APP_PID"
RC=$?
[ "$BUNDLED" = "1" ] && stop_bundled_pg
exit $RC
