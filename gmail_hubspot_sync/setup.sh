#!/usr/bin/env bash
# Setup script for Gmail → HubSpot Contact Sync
set -e

echo "=== Gmail → HubSpot Sync Setup ==="

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy env template if .env doesn't exist
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "✓ .env created from template. Edit it with your credentials before running."
fi

echo ""
echo "=== Next steps ==="
echo "1. Edit .env with your HUBSPOT_API_KEY and SKIP_ADDRESSES"
echo "2. Download credentials.json from Google Cloud Console:"
echo "   - Enable Gmail API at https://console.cloud.google.com/apis/library/gmail.googleapis.com"
echo "   - Create OAuth 2.0 Client ID (Desktop app)"
echo "   - Download and save as credentials.json in this directory"
echo "3. Run: source .venv/bin/activate && python sync.py --once"
echo "   (A browser window will open for Gmail OAuth on first run)"
echo "4. For continuous monitoring: python sync.py"
echo "   For a one-shot run:        python sync.py --once"
echo "   Skip HubSpot notes:        python sync.py --no-notes"
echo ""
