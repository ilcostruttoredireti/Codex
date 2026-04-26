#!/usr/bin/env python3
"""
Run this script once to complete the Gmail OAuth2 flow and save the token.
After this, sync.py will authenticate automatically without a browser.

Usage:
    python setup_gmail_auth.py
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from config import Config

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    try:
        config = Config()
    except ValueError as exc:
        print(f"Config error: {exc}")
        sys.exit(1)

    creds_path = Path(config.gmail_credentials_file)
    token_path = Path(config.gmail_token_file)

    if not creds_path.exists():
        print(f"Error: credentials file not found at '{creds_path}'.")
        print(
            "Download credentials.json from:\n"
            "  Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs"
        )
        sys.exit(1)

    if token_path.exists():
        print(f"Token already exists at '{token_path}'. Delete it to re-authenticate.")
        sys.exit(0)

    print("Opening browser for Gmail authorization...")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_path, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to '{token_path}'.")
    print("You can now run:  python sync.py")


if __name__ == "__main__":
    main()
