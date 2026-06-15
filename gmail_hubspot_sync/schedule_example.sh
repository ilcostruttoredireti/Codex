#!/usr/bin/env bash
# Example cron entry — run the sync every hour
# Add to crontab with: crontab -e
#
#   0 * * * * /path/to/gmail_hubspot_sync/schedule_example.sh >> /var/log/gmail_hubspot_sync.log 2>&1

set -euo pipefail

export HUBSPOT_ACCESS_TOKEN="your_hubspot_private_app_token"
export GOOGLE_CREDENTIALS="/path/to/credentials.json"
export GOOGLE_TOKEN="/path/to/token.json"

cd "$(dirname "$0")"
python sync.py --days 1
