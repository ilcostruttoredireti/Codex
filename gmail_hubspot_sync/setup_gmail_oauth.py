#!/usr/bin/env python3
"""
One-time OAuth setup for Gmail.
Run this interactively before scheduling sync.py.

Steps:
  1. Go to Google Cloud Console → APIs & Services → Credentials
  2. Create OAuth 2.0 Client ID (Desktop app)
  3. Download JSON and save as credentials.json in this directory
  4. Run: python setup_gmail_oauth.py
  5. Authorize in the browser — token.json is saved for future runs
"""

from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path
import os

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

if not Path(CREDS_FILE).exists():
    raise SystemExit(f"credentials.json not found at {CREDS_FILE}.\n"
                     "Download it from Google Cloud Console → APIs → Credentials.")

flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
creds = flow.run_local_server(port=0)
Path(TOKEN_FILE).write_text(creds.to_json())
print(f"✓ Token saved to {TOKEN_FILE}. You can now run sync.py.")
