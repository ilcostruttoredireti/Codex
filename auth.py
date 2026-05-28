"""
One-time OAuth2 setup for Gmail.

Run this script once on a machine with a browser to authorise access:

    python auth.py

It will open a browser window, ask you to log in to Google and approve
the requested scopes, then save a `token.json` file that `main.py`
reuses for all subsequent runs (refreshing automatically).
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")

flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
creds = flow.run_local_server(port=0)

with open(token_file, "w") as fh:
    fh.write(creds.to_json())

print(f"Autorizzazione completata. Token salvato in '{token_file}'.")
