"""
One-time helper: run this script locally to complete the Gmail OAuth2 flow
and generate token.json, then copy that file to your server.

Usage:
    python setup_gmail_oauth.py
"""
import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow
import config


def main() -> None:
    if not os.path.exists(config.GMAIL_CREDENTIALS_FILE):
        print(
            f"ERROR: '{config.GMAIL_CREDENTIALS_FILE}' not found.\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials\n"
            "and place it next to this script."
        )
        return

    flow = InstalledAppFlow.from_client_secrets_file(
        config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
    )
    creds = flow.run_local_server(port=0)

    with open(config.GMAIL_TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"✓ Token saved to '{config.GMAIL_TOKEN_FILE}'")
    print("  Copy this file to your server alongside the sync script.")


if __name__ == "__main__":
    main()
