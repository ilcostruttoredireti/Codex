#!/usr/bin/env python3
"""
Interactive helper to walk through the Gmail OAuth2 setup.

Steps:
  1. Go to https://console.cloud.google.com and create a project.
  2. Enable the Gmail API.
  3. Create OAuth 2.0 credentials (Desktop app) and download credentials.json.
  4. Place credentials.json in this directory.
  5. Run this script — a browser window will open for authorization.
  6. A token.json file will be saved for future runs.

Usage:
    python setup_gmail.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")


def main() -> None:
    if not Path(CREDENTIALS_FILE).exists():
        print(f"ERROR: '{CREDENTIALS_FILE}' not found.")
        print(
            "Download your OAuth 2.0 credentials from "
            "https://console.cloud.google.com → APIs & Services → Credentials"
        )
        return

    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            print("Token refreshed successfully.")
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            print("Authorization successful.")
        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
        print(f"Token saved to '{TOKEN_FILE}'.")
    else:
        print("Token is already valid — no action needed.")


if __name__ == "__main__":
    main()
