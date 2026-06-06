#!/usr/bin/env bash
# Setup rapido del progetto Gmail → HubSpot Sync
set -euo pipefail

echo "=== Gmail → HubSpot Sync — Setup ==="

# 1. Ambiente virtuale
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "Ambiente virtuale creato."
fi
source .venv/bin/activate

# 2. Dipendenze
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "Dipendenze installate."

# 3. File di configurazione
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "File .env creato da template. Completa le variabili prima di avviare."
fi

echo ""
echo "Passi successivi:"
echo "  1. Scarica credentials.json da Google Cloud Console e mettilo qui"
echo "  2. Imposta HUBSPOT_API_KEY in .env"
echo "  3. Avvia con: source .venv/bin/activate && python gmail_hubspot_sync.py"
