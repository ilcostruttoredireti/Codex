"""
One-time script to complete the Gmail OAuth2 flow and cache the token.
Run this before starting the sync daemon for the first time:

    python -m gmail_hubspot_sync.setup_gmail
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")

if not os.path.exists(credentials_file):
    print(
        f"ERROR: credentials file not found at '{credentials_file}'.\n"
        "Download it from Google Cloud Console → APIs & Services → Credentials\n"
        "and set GMAIL_CREDENTIALS_FILE in your .env file.",
        file=sys.stderr,
    )
    sys.exit(1)

from .gmail_client import GmailClient

client = GmailClient(credentials_file, token_file)
client.authenticate()
print(f"OAuth token saved to '{token_file}'. You can now run the sync daemon.")
