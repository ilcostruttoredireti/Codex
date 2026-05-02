from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# Gmail OAuth2
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

# HubSpot Private App token
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Sync settings
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
