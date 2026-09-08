#!/bin/bash
# Aldri rør data/ (PnL, xAI-kost, innskutt, posisjoner) eller hemmelige nøkler.
# patch_env.py endrer kun kjente handelsparametre — ikke .env-nøkler.
set -euo pipefail
ROOT="${ROOT:-/opt/polymarket-desk}"
if [ ! -f "$ROOT/main.py" ]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
cd "$ROOT"
if [ ! -f "$ROOT/.env" ]; then
  echo "ADVARSEL: $ROOT/.env mangler — avbryter før restart."
  exit 1
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Henter siste kode…"
curl -fsSL "https://github.com/Rancowski/polymarket-desk/archive/refs/heads/main.tar.gz" -o "$TMP/desk.tgz"
tar -xzf "$TMP/desk.tgz" -C "$TMP"
SRC=$(find "$TMP" -maxdepth 1 -type d -name 'polymarket-desk-*' | head -n1)
# Kode kun. .env og data/ kopieres aldri.
cp -a "$SRC/agent" "$ROOT/"
cp -a "$SRC/main.py" "$ROOT/main.py"
cp -a "$SRC/requirements.txt" "$ROOT/requirements.txt"
mkdir -p "$ROOT/deploy"
cp -a "$SRC/deploy/." "$ROOT/deploy/"
if [ -x "$ROOT/.venv/bin/pip" ]; then
  "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"
fi
python3 "$ROOT/deploy/patch_env.py" "$ROOT/.env"
chmod +x "$ROOT/deploy/update.sh"
echo "Restarter tjenesten…"
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl restart polymarket-desk
fi
echo "OK. Ny kode kjører."
