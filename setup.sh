#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 mangler. Installer 3.11+ og prøv igjen."
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Opprettet .env — fyll inn XAI_API_KEY, POLYMARKET_PRIVATE_KEY og POLYMARKET_FUNDER."
fi

echo
echo "Ferdig. Aktiver venv og kjør:"
echo "  source .venv/bin/activate"
echo "  python -m agent.bootstrap_creds"
echo "  python main.py once"
