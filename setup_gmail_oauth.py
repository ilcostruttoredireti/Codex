#!/usr/bin/env python3
"""
Helper one-shot per completare il flusso OAuth Gmail.
Esegui questo script UNA VOLTA per generare il token.json.

    python setup_gmail_oauth.py

Dopo l'autenticazione, main.py non richiederà più il browser.
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv

load_dotenv()

CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

if __name__ == "__main__":
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"✅ Token salvato in: {TOKEN_FILE}")
    print("   Ora puoi avviare main.py")
