#!/usr/bin/env bash
# Run the Gmail→HubSpot sync.
# Requires HUBSPOT_TOKEN to be set in the environment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -z "${HUBSPOT_TOKEN:-}" ]]; then
  echo "ERROR: HUBSPOT_TOKEN is not set." >&2
  exit 1
fi

python gmail_hubspot_sync.py "$@"
