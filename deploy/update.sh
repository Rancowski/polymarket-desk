#!/bin/bash
# Never touch .env or data/. Never overwrite the running copy of this script.
# Phase 1: download tarball, copy THIS file to /tmp, exec the staged copy.
# Phase 2 (staged): copy code, pip, patch_env, write status, restart.
set -euo pipefail
ROOT="${ROOT:-/opt/polymarket-desk}"
if [ ! -f "$ROOT/main.py" ]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi

write_status() {
  ok="$1"
  reason="$2"
  export DESK_UPDATE_OK="$ok"
  export DESK_UPDATE_REASON="$reason"
  python3 - <<'PY'
import json, os
from datetime import datetime, timezone
path = os.path.join(os.environ.get("ROOT", "/opt/polymarket-desk"), "data", "update.status")
os.makedirs(os.path.dirname(path), exist_ok=True)
payload = {
    "ok": os.environ.get("DESK_UPDATE_OK") == "1",
    "running": False,
    "ts": datetime.now(timezone.utc).isoformat(),
    "reason": os.environ.get("DESK_UPDATE_REASON", ""),
}
open(path, "w", encoding="utf-8").write(json.dumps(payload) + "\n")
PY
}

if [ "${UPDATE_STAGED:-0}" != "1" ]; then
  if [ ! -f "$ROOT/.env" ]; then
    echo "MISSING .env at $ROOT — abort"
    exit 1
  fi
  mkdir -p "$ROOT/data"
  TMP=$(mktemp -d /tmp/desk-src.XXXXXX)
  echo "Downloading latest code..."
  curl -fsSL "https://github.com/Rancowski/polymarket-desk/archive/refs/heads/main.tar.gz" -o "$TMP/desk.tgz"
  tar -xzf "$TMP/desk.tgz" -C "$TMP"
  SRC=$(find "$TMP" -maxdepth 1 -type d -name "polymarket-desk-*" | head -n1)
  if [ -z "$SRC" ] || [ ! -f "$SRC/deploy/update.sh" ]; then
    echo "tarball missing deploy/update.sh"
    exit 1
  fi
  STAGE=$(mktemp /tmp/desk-update.XXXXXX.sh)
  cp "$SRC/deploy/update.sh" "$STAGE"
  chmod +x "$STAGE"
  export UPDATE_STAGED=1 ROOT TMP SRC
  echo "Exec staged updater $STAGE"
  exec /bin/bash "$STAGE"
fi

# Running from /tmp. Safe to overwrite $ROOT/deploy/update.sh.
cd "$ROOT"
mkdir -p "$ROOT/data"
LOG="$ROOT/data/update.log"
exec >>"$LOG" 2>&1
echo "==== staged update $(date -u +%Y-%m-%dT%H:%M:%SZ) from $SRC ===="

on_err() {
  write_status 0 "update failed (see data/update.log)"
  echo "FAILED"
  exit 1
}
trap on_err ERR

echo "Copying code (not .env, not data/)..."
cp -a "$SRC/agent" "$ROOT/"
cp -a "$SRC/main.py" "$ROOT/main.py"
cp -a "$SRC/requirements.txt" "$ROOT/requirements.txt"
mkdir -p "$ROOT/deploy"
cp -a "$SRC/deploy/." "$ROOT/deploy/"
chmod +x "$ROOT/deploy/update.sh"
if [ -x "$ROOT/.venv/bin/pip" ]; then
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
fi
python3 "$ROOT/deploy/patch_env.py" "$ROOT/.env"
write_status 1 "restarting"
echo "Restarting service..."
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl restart polymarket-desk
fi
echo "OK. New code running."
rm -rf "${TMP:-}"
