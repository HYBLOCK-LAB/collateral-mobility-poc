# Usage (bash or zsh, from any directory):
#   source implementations/canton/scripts/env.sh
#
# Puts the pinned Canton toolchain and PostgreSQL 17 first on PATH for the
# current shell only. Shell rc files and the system default Java are untouched.
# Machine-specific paths come from implementations/canton/.env (gitignored,
# see env.example). Generated secrets live outside the repository under
# $CANTON_STATE_DIR/secrets and are loaded here when present.

if [ -n "${BASH_SOURCE:-}" ]; then
  _canton_src="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
  eval '_canton_src="${(%):-%x}"'
else
  _canton_src="$0"
fi
CANTON_IMPL_DIR="$(cd "$(dirname "$_canton_src")/.." && pwd)"
unset _canton_src
export CANTON_IMPL_DIR

if [ -f "$CANTON_IMPL_DIR/.env" ]; then
  set -a
  . "$CANTON_IMPL_DIR/.env"
  set +a
fi

# Pinned versions. Keep in sync with README §3 and daml/*/daml.yaml (sdk-version).
export CANTON_VERSION=3.7.3
export CANTON_DPM_VERSION=1.1.1
export CANTON_SDK_VERSION=3.7.1

_canton_data="${XDG_DATA_HOME:-$HOME/.local/share}/collateral-mobility-poc"
export CANTON_TOOLS_DIR="${CANTON_TOOLS_DIR:-$_canton_data/canton-tools}"
export CANTON_STATE_DIR="${CANTON_STATE_DIR:-$_canton_data/canton-state}"
unset _canton_data

# JDK 21 (Homebrew openjdk@21, keg-only). bin/canton resolves `java` from PATH
# on macOS, so JAVA_HOME/bin must come first.
export JAVA_HOME="${CANTON_JAVA_HOME:-/opt/homebrew/opt/openjdk@21}"
export JAVA_OPTS="${JAVA_OPTS:--Xmx3g}"
export DPM_HOME="$CANTON_TOOLS_DIR/dpm-home"
export CANTON_HOME="$CANTON_TOOLS_DIR/canton/canton-open-source-$CANTON_VERSION"

# Dedicated PostgreSQL 17 cluster (scripts/db.sh), not the Homebrew default one.
export CANTON_PG_BIN="${CANTON_PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
export CANTON_PG_HOST=127.0.0.1
export CANTON_PG_PORT="${CANTON_PG_PORT:-5433}"
export CANTON_PG_USER="${CANTON_PG_USER:-canton}"

export PATH="$JAVA_HOME/bin:$CANTON_TOOLS_DIR/dpm:$DPM_HOME/bin:$CANTON_HOME/bin:$CANTON_PG_BIN:$PATH"

for _canton_secret in "$CANTON_STATE_DIR/secrets/postgres.env" "$CANTON_STATE_DIR/secrets/tokens.env"; do
  if [ -f "$_canton_secret" ]; then
    set -a
    . "$_canton_secret"
    set +a
  fi
done
unset _canton_secret
