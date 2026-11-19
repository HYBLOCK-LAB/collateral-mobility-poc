#!/usr/bin/env bash
# Install the pinned Canton toolchain into $CANTON_TOOLS_DIR (outside the repo):
#   Canton open source 3.7.3, dpm 1.1.1 and, through dpm, Daml SDK 3.7.1.
# JDK 21 and PostgreSQL 17 come from Homebrew; this script only checks them.
# Safe to rerun: finished steps are skipped, downloads are checked by SHA-256.
# Disk: Canton ~330M, dpm-home ~2.7G. The SDK install took ~10 min on an M-series Mac.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/env.sh"

# SHA-256 of the release files, compared with the published GitHub values on 2026-11-17.
CANTON_SHA256=366277df57096edc690ebe4bae11c66ff2d9a9d7ad8660493b6da2f62cfd622e
DPM_SHA256_DARWIN_ARM64=3774f7a979067c31648ddf8fc250b1316d780f82c2d6cba7fc1ba8ce21082129

die() { echo "install-toolchain.sh: $*" >&2; exit 1; }

[ "$(uname -s)-$(uname -m)" = "Darwin-arm64" ] ||
  die "only macOS arm64 is pinned (add the dpm SHA-256 for $(uname -s)-$(uname -m) first)"
[ -x "$JAVA_HOME/bin/java" ] || die "JDK 21 not found at $JAVA_HOME (brew install openjdk@21)"
# Version output is captured first: under pipefail, `cmd | grep -q` fails when
# grep exits before cmd has written everything (SIGPIPE).
java_version="$("$JAVA_HOME/bin/java" -version 2>&1)"
case "$java_version" in *'version "21.'*) ;; *) die "$JAVA_HOME is not JDK 21" ;; esac
[ -x "$CANTON_PG_BIN/postgres" ] || die "PostgreSQL 17 not found at $CANTON_PG_BIN (brew install postgresql@17)"

DL="$CANTON_TOOLS_DIR/downloads"
mkdir -p "$DL" "$CANTON_TOOLS_DIR/canton" "$CANTON_TOOLS_DIR/dpm"

fetch() { # url file sha256
  local url="$1" file="$DL/$2" sha="$3"
  if [ ! -f "$file" ]; then
    curl -fL --retry 3 -o "$file.part" "$url"
    mv "$file.part" "$file"
  fi
  echo "$sha  $file" | shasum -a 256 -c - >/dev/null || die "SHA-256 mismatch for $file (delete it and rerun)"
}

canton_tgz="canton-open-source-$CANTON_VERSION.tar.gz"
dpm_tgz="dpm-$CANTON_DPM_VERSION-darwin-arm64.tar.gz"
fetch "https://github.com/digital-asset/canton/releases/download/v$CANTON_VERSION/$canton_tgz" "$canton_tgz" "$CANTON_SHA256"
fetch "https://github.com/digital-asset/dpm/releases/download/$CANTON_DPM_VERSION/$dpm_tgz" "$dpm_tgz" "$DPM_SHA256_DARWIN_ARM64"

[ -x "$CANTON_HOME/bin/canton" ] || tar -xzf "$DL/$canton_tgz" -C "$CANTON_TOOLS_DIR/canton"
[ -x "$CANTON_TOOLS_DIR/dpm/dpm" ] || tar -xzf "$DL/$dpm_tgz" -C "$CANTON_TOOLS_DIR/dpm"

# Exact version match (a substring or regex test would accept 3.7.10 for 3.7.1).
if ! dpm version 2>/dev/null | awk -v v="$CANTON_SDK_VERSION" '$1 == v || $2 == v { found = 1 } END { exit !found }'; then
  dpm install "$CANTON_SDK_VERSION"
fi

echo "== installed"
canton_version="$(canton --version)"; echo "${canton_version%%$'\n'*}"
dpm version
echo "${java_version%%$'\n'*}"
"$CANTON_PG_BIN/postgres" --version
