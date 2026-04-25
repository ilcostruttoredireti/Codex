#!/usr/bin/env python3
"""
One-time Gmail OAuth2 setup.

Run this ONCE before starting sync.py to generate token.json.

Steps:
  1. Go to https://console.cloud.google.com/
  2. Create a project → Enable the Gmail API
  3. Create OAuth 2.0 credentials (Desktop App) → download as credentials.json
  4. Place credentials.json in this directory
  5. Run: python setup_gmail.py
  6. A browser window opens → sign in → grant access
  7. token.json is saved automatically — sync.py uses it from now on
"""

import os
import sys

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES      = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDS_FILE  = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE  = os.getenv("GMAIL_TOKEN_FILE", "token.json")


def main() -> None:
    if not os.path.exists(CREDS_FILE):
        print(f"[ERRORE] File non trovato: {CREDS_FILE}")
        print("Scarica le credenziali OAuth da Google Cloud Console e salvale come credentials.json")
        sys.exit(1)

    if os.path.exists(TOKEN_FILE):
        print(f"token.json già presente. Eliminalo per re-autenticarti.")
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.valid:
            print("✅ Token valido — puoi avviare sync.py")
            return

    print("Avvio flusso OAuth2 Gmail …")
    flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as fh:
        fh.write(creds.to_json())

    print(f"✅ Autenticazione completata. Token salvato in {TOKEN_FILE}")
    print("Puoi ora avviare:  python sync.py")


if __name__ == "__main__":
    main()
