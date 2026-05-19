#!/usr/bin/env python3
"""
One-time Gmail OAuth setup.
Run this interactively ONCE to generate token.json, then start gmail_hubspot_sync.py.

    python setup_gmail_oauth.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    creds_file = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
    token_path = Path("token.json")

    if not creds_file.exists():
        raise SystemExit(
            f"File non trovato: {creds_file}\n\n"
            "1. Vai su https://console.cloud.google.com\n"
            "2. APIs & Services → Credentials → Create Credentials → OAuth client ID\n"
            "3. Tipo applicazione: Desktop app\n"
            "4. Scarica il JSON e salvalo come 'credentials.json' in questa directory."
        )

    print("Avvio flusso OAuth Gmail...")
    print("Si aprirà il browser per autorizzare l'accesso.\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json())

    # Quick sanity check
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "sconosciuta")

    print(f"\n✅  Autenticazione completata.")
    print(f"   Account Gmail: {email}")
    print(f"   Token salvato: {token_path}\n")
    print("Ora puoi avviare il servizio con:\n    python gmail_hubspot_sync.py")


if __name__ == "__main__":
    main()
