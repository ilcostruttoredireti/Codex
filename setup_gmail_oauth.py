"""
One-time script: generates token.json for Gmail OAuth.
Run this once on a machine with a browser before deploying the sync daemon.
"""

from google_auth_oauthlib.flow import InstalledAppFlow
import os

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
creds = flow.run_local_server(port=0)

with open(TOKEN_FILE, "w") as fh:
    fh.write(creds.to_json())

print(f"✅ Token salvato in {TOKEN_FILE}")
