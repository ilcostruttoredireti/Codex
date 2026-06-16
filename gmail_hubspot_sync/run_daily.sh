#!/usr/bin/env bash
# Convenience wrapper — run once per day via cron:
#   0 8 * * * /path/to/gmail_hubspot_sync/run_daily.sh >> /var/log/gmail_hubspot_sync.log 2>&1

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

python sync.py --days 1
