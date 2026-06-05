#!/usr/bin/env python3
"""
Setup OAuth Gmail — esegui questo script una volta sola per autorizzare l'accesso.

Prerequisiti:
  1. Accedi a https://console.cloud.google.com
  2. Crea (o seleziona) un progetto
  3. Abilita l'API Gmail: API & Services → Library → cerca "Gmail API" → Enable
  4. Crea credenziali OAuth 2.0:
       API & Services → Credentials → Create Credentials
       → OAuth 2.0 Client ID → Desktop App → Scarica JSON
  5. Rinomina il file scaricato in 'credentials.json' e posizionalo qui
  6. Esegui questo script:  python setup_gmail_oauth.py
     → Si aprirà il browser per autorizzare l'accesso
     → Il token verrà salvato in 'gmail_token.json'

Dopo il setup, lo script principale (gmail_hubspot_sync.py) usa il token
senza ulteriori interazioni browser.
"""

import sys
from pathlib import Path

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "gmail_token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> None:
    if not Path(CREDENTIALS_FILE).exists():
        print(f"❌  File '{CREDENTIALS_FILE}' non trovato.")
        print("   Scaricalo da Google Cloud Console come descritto nell'intestazione di questo script.")
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("📋  Avvio flusso OAuth Gmail...")
    print("    Si aprirà il browser — accedi con l'account Google da monitorare.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    Path(TOKEN_FILE).write_text(creds.to_json())
    print(f"\n✅  Token salvato in '{TOKEN_FILE}'.")
    print("   Puoi ora avviare la sincronizzazione con:\n")
    print("   python gmail_hubspot_sync.py --watch")


if __name__ == "__main__":
    main()
