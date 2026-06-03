#!/usr/bin/env python3
"""
One-time helper to generate gmail_token.json via the OAuth2 browser flow.
Run this script once on a machine with a browser, then copy gmail_token.json
to your server / container.

    python setup_gmail_oauth.py
"""
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path("gmail_credentials.json")
TOKEN_FILE = Path("gmail_token.json")

if not CREDENTIALS_FILE.exists():
    print(
        f"ERROR: {CREDENTIALS_FILE} not found.\n"
        "Download OAuth 2.0 credentials from Google Cloud Console:\n"
        "  APIs & Services → Credentials → Create Credentials → OAuth client ID\n"
        "  Application type: Desktop app\n"
        "  Download JSON and save as gmail_credentials.json"
    )
    raise SystemExit(1)

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
creds = flow.run_local_server(port=0)
TOKEN_FILE.write_text(creds.to_json())
print(f"✓ Token saved to {TOKEN_FILE}")
