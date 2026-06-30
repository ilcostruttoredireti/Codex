"""
One-time Gmail OAuth setup.
Run this interactively to generate gmail_token.json.
Requires gmail_credentials.json from Google Cloud Console
(OAuth 2.0 Desktop app credentials).
"""
from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

flow = InstalledAppFlow.from_client_secrets_file("gmail_credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

with open("gmail_token.json", "w") as f:
    f.write(creds.to_json())

print("gmail_token.json saved successfully.")
