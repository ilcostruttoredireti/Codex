"""Runtime configuration loaded from environment variables / .env file."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Gmail ─────────────────────────────────────────────────────────────────────
# Path to the OAuth 2.0 client-secrets JSON downloaded from Google Cloud Console
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")

# Cached OAuth token (auto-created on first successful authentication)
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")

# ── HubSpot ───────────────────────────────────────────────────────────────────
# Private App access token from HubSpot → Settings → Integrations → Private Apps
HUBSPOT_ACCESS_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# ── Sync behaviour ────────────────────────────────────────────────────────────
# Seconds between polling cycles (ignored when --once is passed)
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Local JSON file used to persist the Gmail history ID and processed message IDs
SYNC_STATE_FILE: str = os.getenv("SYNC_STATE_FILE", "sync_state.json")
