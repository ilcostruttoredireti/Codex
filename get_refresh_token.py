#!/usr/bin/env python3
"""
One-time helper script to obtain a Google OAuth2 refresh token.

Run this locally (requires a browser):
    python get_refresh_token.py

Then copy the printed refresh_token into your .env file.

Prerequisites:
  1. Create a project in Google Cloud Console
  2. Enable the Gmail API
  3. Create OAuth 2.0 credentials (Desktop App type)
  4. Download the JSON and set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in your .env
"""

import os
import json
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    print("ERROR: Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in your .env first.")
    raise SystemExit(1)

client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(
    client_config,
    scopes=["https://www.googleapis.com/auth/gmail.readonly"],
)

creds = flow.run_local_server(port=0)

print("\n✅  Authentication successful!\n")
print("Add these values to your .env file:")
print(f"  GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
print(f"  GOOGLE_CLIENT_ID={creds.client_id}")
print(f"  GOOGLE_CLIENT_SECRET={creds.client_secret}")
