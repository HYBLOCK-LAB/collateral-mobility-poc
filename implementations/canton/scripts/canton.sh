#!/usr/bin/env bash
# Canton process for B1 (S1, P_X, P_Y, P_Z; topology b1-xyz) or, with
# CANTON_TOPOLOGY=b2 in .env, for B2 (S1, S2, P_X, P_Y, P_Z; topology b2).
#
#   canton.sh start     start the daemon and wait until bootstrap finished
#   canton.sh stop      stop the daemon (SIGTERM, then wait)
#   canton.sh status    process state and the identifiers in ids.json
#   canton.sh ready     exit 0 only if the daemon runs and its bootstrap finished
#
# Requires the PostgreSQL cluster (scripts/db.sh start). Node logs go to
# $CANTON_STATE_DIR/logs/canton.log, stdout/stderr to canton.out. A state
# directory bootstrapped with another topology (ids.json, e.g. the stage-2
# "b1-xy" ledger) is refused: start it from the commit that created it.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/env.sh"

die() { echo "canton.sh: $*" >&2; exit 1; }

# The topology is chosen once per state directory; ids.json records it and the
# start check below refuses a state directory bootstrapped with another one.
TOPOLOGY="${CANTON_TOPOLOGY:-b1-xyz}"
case "$TOPOLOGY" in
  b1-xyz)
    CONF="$CANTON_IMPL_DIR/canton/b1-xy.conf"
    BOOT="$CANTON_IMPL_DIR/canton/bootstrap-b1-xy.canton"
    ;;
  b2)
    CONF="$CANTON_IMPL_DIR/canton/b2.conf"
    BOOT="$CANTON_IMPL_DIR/canton/bootstrap-b2.canton"
    ;;
  *) die "unknown CANTON_TOPOLOGY '$TOPOLOGY' (b1-xyz or b2)" ;;
esac
PIDFILE="$CANTON_STATE_DIR/canton.pid"
READY="$CANTON_STATE_DIR/canton.ready"
LOGDIR="$CANTON_STATE_DIR/logs"
START_TIMEOUT="${CANTON_START_TIMEOUT:-300}"

# The java process itself is the source of truth (bin/canton execs java), so a
# stale or wrong pid file cannot hide a running daemon. Any topology's daemon
# of this checkout counts: b1-xyz and b2 use the same S1 and participant ports,
# and stop must find one started with the other topology.
running_pid() {
  local pid
  pid="$(pgrep -f -- "daemon -c $CANTON_IMPL_DIR/canton/[^ ]*\.conf" | head -n 1)"
  [ -n "$pid" ] || return 1
  echo "$pid"
}

cmd_start() {
  if pid="$(running_pid)"; then
    pgrep -f -- "daemon -c $CONF" | grep -qx "$pid" ||
      die "a Canton daemon of this checkout runs with another topology than $TOPOLOGY (pid $pid); stop it first"
    echo "canton.sh: already running (pid $pid)"
    return
  fi
  [ -x "$CANTON_HOME/bin/canton" ] || die "Canton $CANTON_VERSION not found at $CANTON_HOME (scripts/install-toolchain.sh)"
  [ -n "${CANTON_PG_PASSWORD:-}" ] || die "CANTON_PG_PASSWORD not set (scripts/db.sh init)"
  if [ -f "$CANTON_STATE_DIR/ids.json" ] &&
    ! grep -q "\"topology\": \"$TOPOLOGY\"" "$CANTON_STATE_DIR/ids.json"; then
    die "$CANTON_STATE_DIR was bootstrapped with another topology than $TOPOLOGY (ids.json); use a new state directory (README §4)"
  fi
  "$CANTON_PG_BIN/pg_isready" -q -h "$CANTON_PG_HOST" -p "$CANTON_PG_PORT" || die "PostgreSQL not running (scripts/db.sh start)"
  mkdir -p "$LOGDIR"
  rm -f "$READY"
  (
    cd "$CANTON_STATE_DIR"
    # A simple background command, so $! is the pid that nohup, bin/canton and
    # finally java run under.
    nohup canton daemon -c "$CONF" --bootstrap "$BOOT" \
      --log-file-name "$LOGDIR/canton.log" >"$LOGDIR/canton.out" 2>&1 &
    echo $! >"$PIDFILE"
  )
  local pid waited=0
  pid="$(cat "$PIDFILE")"
  sleep 1
  [ "$(running_pid || true)" = "$pid" ] || die "started pid $pid is not the Canton java process"
  while [ ! -f "$READY" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      tail -n 40 "$LOGDIR/canton.out" >&2
      rm -f "$PIDFILE"
      die "Canton exited during bootstrap (see $LOGDIR/canton.log)"
    fi
    [ "$waited" -lt "$START_TIMEOUT" ] || die "bootstrap not finished after ${START_TIMEOUT}s (pid $pid still running)"
    sleep 2
    waited=$((waited + 2))
  done
  echo "canton.sh: ready after ~${waited}s (pid $pid)"
}

cmd_stop() {
  local pid
  if ! pid="$(running_pid)"; then echo "canton.sh: not running"; rm -f "$PIDFILE" "$READY"; return; fi
  kill -TERM "$pid"
  for _ in $(seq 1 60); do
    kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE" "$READY"; echo "canton.sh: stopped"; return; }
    sleep 1
  done
  die "pid $pid still running after 60s"
}

# canton.ready alone is not enough: a crashed or killed daemon leaves it behind,
# and db.sh wipe removes ids.json but not this file.
cmd_ready() {
  local pid
  pid="$(running_pid)" || die "Canton not running (scripts/canton.sh start)"
  [ -f "$READY" ] || die "Canton (pid $pid) has not finished its bootstrap"
  [ -f "$CANTON_STATE_DIR/ids.json" ] || die "no ids.json; restart Canton to bootstrap again"
}

cmd_status() {
  local pid
  if pid="$(running_pid)"; then
    if [ -f "$READY" ]; then echo "canton.sh: running and bootstrapped (pid $pid)"
    else echo "canton.sh: running, bootstrap not finished (pid $pid)"; fi
  else
    echo "canton.sh: not running"
  fi
  [ -f "$CANTON_STATE_DIR/ids.json" ] && cat "$CANTON_STATE_DIR/ids.json"
  return 0
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  ready) cmd_ready ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
