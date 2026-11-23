#!/usr/bin/env bash
# Platform adapters of B1: X (P_X), Y (P_Y) and Z (P_Z) on 127.0.0.1; the
# ports are set in adapter/server.py (PLATFORMS).
#
#   adapter.sh setup     create .venv (Python 3.12) and install adapter/requirements.txt
#   adapter.sh tokens    generate the four local bearer tokens once (secrets/tokens.env)
#   adapter.sh start     build the DAR if stale, start X, Y and Z, wait for their warm-up
#   adapter.sh stop      stop the adapters (SIGTERM, then wait)
#   adapter.sh status    process and readiness state of X, Y and Z
#
# Requires a bootstrapped Canton (scripts/canton.sh start). Adapter output goes
# to $CANTON_STATE_DIR/logs/adapter-<P>.out, events to $CANTON_EVIDENCE_DIR.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/env.sh"

PY="$CANTON_IMPL_DIR/.venv/bin/python"
SERVER="$CANTON_IMPL_DIR/adapter/server.py"
PKG="$CANTON_IMPL_DIR/daml/collateral"
DAR="$PKG/.daml/dist/collateral-0.4.0.dar"
TOKENS="$CANTON_STATE_DIR/secrets/tokens.env"
LOGDIR="$CANTON_STATE_DIR/logs"
PLATFORMS="X Y Z"
START_TIMEOUT="${CANTON_ADAPTER_START_TIMEOUT:-120}"

die() { echo "adapter.sh: $*" >&2; exit 1; }

port_of() { "$PY" -I "$SERVER" --platform "$1" --print-port; }

# The process command line is the source of truth, as in canton.sh.
running_pid() {
  local pid
  pid="$(pgrep -f -- "$SERVER --platform $1" | head -n 1)"
  [ -n "$pid" ] || return 1
  echo "$pid"
}

cmd_setup() {
  local python="${CANTON_PYTHON:-/opt/homebrew/bin/python3.12}"
  [ -x "$python" ] || die "Python 3.12 not found at $python (brew install python@3.12, or set CANTON_PYTHON)"
  "$python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' || die "$python is not Python 3.12"
  [ -x "$PY" ] || "$python" -m venv "$CANTON_IMPL_DIR/.venv"
  "$PY" -m pip install --disable-pip-version-check -q -r "$CANTON_IMPL_DIR/adapter/requirements.txt"
  echo "adapter.sh: $("$PY" --version) ready in $CANTON_IMPL_DIR/.venv"
}

cmd_tokens() {
  if [ -f "$TOKENS" ]; then echo "adapter.sh: $TOKENS already exists (not overwritten)"; return; fi
  umask 077
  mkdir -p "$(dirname "$TOKENS")"
  local tmp name
  tmp="$(mktemp "$TOKENS.XXXXXX")"
  for name in POC_BANK_A_TOKEN POC_BANK_B_TOKEN POC_BANK_C_TOKEN POC_OBSERVER_TOKEN; do
    printf '%s=%s\n' "$name" "$(openssl rand -hex 32)" >>"$tmp"
  done
  mv "$tmp" "$TOKENS"
  echo "adapter.sh: wrote $TOKENS (mode 600; values are not printed)"
}

# Rebuild when the DAR is missing or older than any source of the package.
build_dar() {
  if [ -f "$DAR" ] && [ -z "$(find "$PKG/daml.yaml" "$PKG/daml" -newer "$DAR" -print -quit)" ]; then
    return
  fi
  echo "adapter.sh: building $(basename "$DAR")"
  (cd "$PKG" && dpm build >"$LOGDIR/dpm-build-collateral.log" 2>&1) ||
    { tail -n 20 "$LOGDIR/dpm-build-collateral.log" >&2; die "dpm build failed"; }
}

cmd_start() {
  [ -x "$PY" ] || die "no .venv (scripts/adapter.sh setup)"
  [ -f "$TOKENS" ] || die "no tokens (scripts/adapter.sh tokens)"
  "$CANTON_IMPL_DIR/scripts/canton.sh" ready || die "start Canton first"
  mkdir -p "$LOGDIR"
  build_dar
  local p pid
  for p in $PLATFORMS; do
    if pid="$(running_pid "$p")"; then echo "adapter.sh: $p already running (pid $pid)"; continue; fi
    if lsof -nP -iTCP:"$(port_of "$p")" -sTCP:LISTEN >/dev/null 2>&1; then
      die "port $(port_of "$p") is used by another process"
    fi
    rm -f "$CANTON_STATE_DIR/adapter-$p.ready"
    nohup "$PY" -I "$SERVER" --platform "$p" >"$LOGDIR/adapter-$p.out" 2>&1 &
  done
  local waited=0 missing
  while :; do
    missing=""
    for p in $PLATFORMS; do
      [ -f "$CANTON_STATE_DIR/adapter-$p.ready" ] && continue
      if ! running_pid "$p" >/dev/null; then
        tail -n 20 "$LOGDIR/adapter-$p.out" >&2
        die "adapter $p exited during start-up (see $LOGDIR/adapter-$p.out)"
      fi
      missing="$missing $p"
    done
    [ -n "$missing" ] || break
    [ "$waited" -lt "$START_TIMEOUT" ] || die "not ready after ${START_TIMEOUT}s:$missing"
    sleep 1
    waited=$((waited + 1))
  done
  for p in $PLATFORMS; do echo "adapter.sh: $p ready $(cat "$CANTON_STATE_DIR/adapter-$p.ready")"; done
}

cmd_stop() {
  local p pid
  for p in $PLATFORMS; do
    if ! pid="$(running_pid "$p")"; then
      echo "adapter.sh: $p not running"; rm -f "$CANTON_STATE_DIR/adapter-$p.ready"; continue
    fi
    kill -TERM "$pid"
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && die "$p (pid $pid) still running after 30s"
    rm -f "$CANTON_STATE_DIR/adapter-$p.ready"
    echo "adapter.sh: $p stopped"
  done
}

cmd_status() {
  local p pid
  for p in $PLATFORMS; do
    if pid="$(running_pid "$p")"; then
      if [ -f "$CANTON_STATE_DIR/adapter-$p.ready" ]; then
        echo "adapter.sh: $p ready (pid $pid) $(cat "$CANTON_STATE_DIR/adapter-$p.ready")"
      else echo "adapter.sh: $p running, warm-up not finished (pid $pid)"; fi
    else
      echo "adapter.sh: $p not running"
    fi
  done
}

case "${1:-}" in
  setup) cmd_setup ;;
  tokens) cmd_tokens ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
