"""
One-time OAuth flow to generate token.json for Gmail access.
Run once interactively:  python setup_gmail_oauth.py

Prerequisites:
  1. Enable Gmail API in Google Cloud Console
  2. Create OAuth 2.0 Client ID → Desktop App
  3. Download credentials.json and place it in this directory
"""

import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

def main():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)

        with open("token.json", "w") as fh:
            fh.write(creds.to_json())

    print("✓ token.json creato con successo.")
    print("  Puoi ora avviare la sync con:  python gmail_hubspot_sync.py")

if __name__ == "__main__":
    main()
