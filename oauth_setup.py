#!/usr/bin/env python3
"""
One-time Gmail OAuth2 setup.
Run this once to generate gmail_token.json used by gmail_hubspot_sync.py.

Requirements:
  1. Create a Google Cloud project, enable Gmail API.
  2. Download OAuth2 credentials → save as client_secret.json.
  3. Run: python oauth_setup.py
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

if __name__ == "__main__":
    flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
    creds = flow.run_local_server(port=0)
    with open("gmail_token.json", "w") as f:
        f.write(creds.to_json())
    print("✅ gmail_token.json saved. You can now run gmail_hubspot_sync.py")
