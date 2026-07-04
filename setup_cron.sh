#!/usr/bin/env bash
# Installa dipendenze e aggiunge cron job per esecuzione ogni ora

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
LOG_FILE="$SCRIPT_DIR/sync.log"

# Crea virtual environment e installa dipendenze
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

# Aggiungi cron job (ogni ora)
CRON_CMD="0 * * * * HUBSPOT_ACCESS_TOKEN=\$HUBSPOT_ACCESS_TOKEN GOOGLE_CREDENTIALS_FILE=\$GOOGLE_CREDENTIALS_FILE $PYTHON $SCRIPT_DIR/gmail_hubspot_sync.py >> $LOG_FILE 2>&1"

# Aggiungi solo se non esiste già
( crontab -l 2>/dev/null | grep -qF "gmail_hubspot_sync.py" ) \
    || ( crontab -l 2>/dev/null; echo "$CRON_CMD" ) | crontab -

echo "Cron job configurato:"
crontab -l | grep "gmail_hubspot_sync"
echo ""
echo "Prossime esecuzioni: ogni ora al minuto 0"
echo "Log: $LOG_FILE"
echo ""
echo "Prima esecuzione manuale:"
echo "  HUBSPOT_ACCESS_TOKEN=<token> $PYTHON $SCRIPT_DIR/gmail_hubspot_sync.py --since-days 7"
