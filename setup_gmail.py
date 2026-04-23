#!/usr/bin/env python3
"""
One-time Gmail OAuth setup.
Run this script once to generate token.json before starting the sync.

Prerequisites:
1. Go to https://console.cloud.google.com/
2. Create a project → Enable Gmail API
3. Create OAuth 2.0 credentials (Desktop app) → Download credentials.json
4. Place credentials.json in this directory
5. Run: python setup_gmail.py
"""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def main():
    creds_file = Path("credentials.json")
    token_file = Path("token.json")

    if not creds_file.exists():
        raise SystemExit(
            "credentials.json not found.\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json())
    print(f"✓ token.json saved. You can now run: python gmail_hubspot_sync.py")

if __name__ == "__main__":
    main()
