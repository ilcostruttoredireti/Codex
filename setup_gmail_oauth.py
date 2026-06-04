#!/usr/bin/env python3
"""
Utility per completare il flusso OAuth2 Gmail una tantum.
Eseguire questo script sul tuo computer locale (non in produzione)
per generare il file token.json, poi copiarlo nel server.

Prerequisiti:
  1. Vai su https://console.cloud.google.com
  2. Crea un progetto e abilita Gmail API
  3. Crea credenziali OAuth2 "Desktop App" e scarica il JSON
  4. Rinomina il file scaricato in credentials.json
  5. Esegui: python setup_gmail_oauth.py
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def main():
    if not os.path.exists(CREDENTIALS_FILE):
        raise SystemExit(
            f"File '{CREDENTIALS_FILE}' non trovato.\n"
            "Scarica le credenziali OAuth2 da Google Cloud Console e rinominale in credentials.json"
        )

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"✓ Token salvato in '{TOKEN_FILE}'")
    print("  Copia questo file sul server insieme a gmail_hubspot_sync.py")


if __name__ == "__main__":
    main()
