#!/bin/bash
# Aldri rør data/ (PnL, xAI-kost, innskutt, posisjoner) eller hemmelige nøkler.
# patch_env.py endrer kun kjente handelsparametre.
set -euo pipefail
ROOT="${ROOT:-/opt/polymarket-desk}"
cd "$ROOT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Henter siste kode…"
curl -fsSL "https://github.com/Rancowski/polymarket-desk/archive/refs/heads/main.tar.gz" -o "$TMP/desk.tgz"
tar -xzf "$TMP/desk.tgz" -C "$TMP"
SRC=$(find "$TMP" -maxdepth 1 -type d -name 'polymarket-desk-*' | head -n1)
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
systemctl restart polymarket-desk
echo "OK. Ny kode kjører."
