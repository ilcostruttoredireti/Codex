"""
One-time helper: run this script to complete Gmail OAuth2 and generate token.json.
Usage:  python setup_gmail_oauth.py
"""

import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")


def main():
    if not Path(CREDENTIALS_FILE).exists():
        print(
            f"ERROR: {CREDENTIALS_FILE} not found.\n"
            "Download it from Google Cloud Console:\n"
            "  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON"
        )
        return

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    Path(TOKEN_FILE).write_text(creds.to_json())
    print(f"Token saved to {TOKEN_FILE}. You can now run gmail_hubspot_sync.py.")


if __name__ == "__main__":
    main()
