#!/usr/bin/env bash
# Dedicated PostgreSQL 17 cluster for the Canton nodes.
#
#   db.sh init                 create the cluster, role and node databases (once)
#   db.sh start | stop | status
#   db.sh wipe --yes           drop and recreate the node databases (re-bootstrap)
#                              and remove the adapters' rejection records
#   db.sh psql [args...]       psql as the Canton role
#
# The cluster lives in $CANTON_STATE_DIR/postgres, listens on 127.0.0.1 only,
# and keeps PostgreSQL's default durability (fsync, synchronous_commit,
# full_page_writes on). It is started and stopped by hand; no login service.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/env.sh"

PGDATA="$CANTON_STATE_DIR/postgres"
SECRETS="$CANTON_STATE_DIR/secrets"
LOGDIR="$CANTON_STATE_DIR/logs"
# One database per node (sequencer driver separate, as in Canton's HA example).
NODE_DBS=(canton_s1_sequencer canton_s1_sequencer_driver canton_s1_mediator canton_px canton_py canton_pz)

die() { echo "db.sh: $*" >&2; exit 1; }

pg() { "$CANTON_PG_BIN/$1" "${@:2}"; }

psql_admin() {
  [ -n "${CANTON_PG_PASSWORD:-}" ] || die "CANTON_PG_PASSWORD not set (run db.sh init first)"
  PGPASSWORD="$CANTON_PG_PASSWORD" pg psql -X -q -v ON_ERROR_STOP=1 \
    -h "$CANTON_PG_HOST" -p "$CANTON_PG_PORT" -U "$CANTON_PG_USER" "$@"
}

is_running() { pg pg_ctl -D "$PGDATA" status >/dev/null 2>&1; }

create_dbs() {
  for db in "${NODE_DBS[@]}"; do
    psql_admin -d postgres -c "CREATE DATABASE $db OWNER $CANTON_PG_USER ENCODING 'UTF8'"
  done
}

cmd_init() {
  [ -x "$CANTON_PG_BIN/initdb" ] || die "PostgreSQL 17 not found at $CANTON_PG_BIN (brew install postgresql@17)"
  [ ! -e "$PGDATA" ] || die "$PGDATA already exists"
  if lsof -nP -iTCP:"$CANTON_PG_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $CANTON_PG_PORT is already in use (set CANTON_PG_PORT in .env)"
  fi
  umask 077
  mkdir -p "$SECRETS" "$LOGDIR"
  local pw pwfile
  pw="$(openssl rand -hex 24)"
  pwfile="$(mktemp "$SECRETS/pwfile.XXXXXX")"
  printf '%s\n' "$pw" >"$pwfile"
  pg initdb -D "$PGDATA" -U "$CANTON_PG_USER" --pwfile="$pwfile" \
    --auth=scram-sha-256 --encoding=UTF8 --locale=C >"$LOGDIR/initdb.log"
  rm -f "$pwfile"
  printf 'CANTON_PG_PASSWORD=%s\n' "$pw" >"$SECRETS/postgres.env"
  export CANTON_PG_PASSWORD="$pw"
  cat >>"$PGDATA/postgresql.conf" <<EOF

# collateral-mobility-poc (implementations/canton/scripts/db.sh)
listen_addresses = '127.0.0.1'
port = $CANTON_PG_PORT
unix_socket_directories = ''
max_connections = 200
EOF
  cmd_start
  create_dbs
  echo "db.sh: cluster ready at $PGDATA (port $CANTON_PG_PORT), databases: ${NODE_DBS[*]}"
}

cmd_start() {
  is_running && { echo "db.sh: already running"; return; }
  mkdir -p "$LOGDIR"
  pg pg_ctl -D "$PGDATA" -l "$LOGDIR/postgres.log" -w -t 60 start >/dev/null
  echo "db.sh: started (port $CANTON_PG_PORT)"
}

cmd_stop() {
  is_running || { echo "db.sh: not running"; return; }
  pg pg_ctl -D "$PGDATA" -m fast -w -t 60 stop >/dev/null
  echo "db.sh: stopped"
}

cmd_status() {
  if ! is_running; then echo "db.sh: not running ($PGDATA)"; exit 3; fi
  psql_admin -d postgres -At -F ' = ' -c "
    SELECT name, setting FROM pg_settings
    WHERE name IN ('server_version','port','listen_addresses','max_connections',
                   'fsync','synchronous_commit','full_page_writes','wal_level')
    ORDER BY name"
  psql_admin -d postgres -At -c "
    SELECT 'database = ' || datname FROM pg_database
    WHERE datname LIKE 'canton_%' ORDER BY datname"
}

cmd_wipe() {
  [ "${1:-}" = "--yes" ] || die "wipe drops all Canton node data; rerun as: db.sh wipe --yes"
  is_running || die "cluster not running"
  if pgrep -f "canton.*$CANTON_IMPL_DIR/canton/" >/dev/null; then
    die "Canton is still running (scripts/canton.sh stop)"
  fi
  if pgrep -f -- "$CANTON_IMPL_DIR/adapter/server.py" >/dev/null; then
    die "an adapter is still running (scripts/adapter.sh stop)"
  fi
  for db in "${NODE_DBS[@]}"; do
    psql_admin -d postgres -c "DROP DATABASE IF EXISTS $db WITH (FORCE)"
  done
  create_dbs
  rm -f "$CANTON_STATE_DIR/ids.json"
  # Rejection records belong to the dropped ledger (interface §6.3).
  rm -rf "$CANTON_STATE_DIR/requests"
  echo "db.sh: recreated ${NODE_DBS[*]}; removed the adapters' rejection records"
}

case "${1:-}" in
  init) cmd_init ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  wipe) shift; cmd_wipe "$@" ;;
  psql) shift; psql_admin "$@" ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
