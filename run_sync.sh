#!/usr/bin/env bash
# Wrapper script for cron / systemd timer scheduling.
#
# Example crontab entry to run every 15 minutes:
#   */15 * * * * /path/to/Codex/run_sync.sh >> /var/log/gmail_hubspot_sync.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtualenv if present
if [ -f ".venv/bin/activate" ]; then
    source ".venv/bin/activate"
fi

exec python gmail_hubspot_sync.py "$@"
