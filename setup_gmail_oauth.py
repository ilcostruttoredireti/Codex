#!/usr/bin/env python3
"""
First-run helper: opens a browser to authorise Gmail OAuth2 access
and saves the token file so the main script can run headlessly.

Run this once on a machine with a browser before deploying to a server.
"""

from pathlib import Path
import os
import sys

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

if not Path(CREDENTIALS_FILE).exists():
    print(f"ERROR: {CREDENTIALS_FILE} not found.")
    print("Download it from: Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs")
    sys.exit(1)

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
creds = flow.run_local_server(port=0)
Path(TOKEN_FILE).write_text(creds.to_json())
print(f"Token saved to {TOKEN_FILE}. You can now run gmail_hubspot_sync.py.")
