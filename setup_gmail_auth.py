#!/usr/bin/env python3
"""
First-time Gmail OAuth2 setup.
Run once to generate credentials/gmail_token.json.
"""

import os
import sys

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.environ.get(
    "GMAIL_CREDENTIALS_FILE", "credentials/gmail_credentials.json"
)
TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "credentials/gmail_token.json")


def main() -> None:
    if not os.path.exists(CREDENTIALS_FILE):
        print(
            f"[ERRORE] File credenziali non trovato: {CREDENTIALS_FILE}\n\n"
            "Passaggi:\n"
            "  1. Vai su https://console.cloud.google.com/\n"
            "  2. Abilita la Gmail API\n"
            "  3. Crea credenziali OAuth2 (Desktop App)\n"
            "  4. Scarica il file JSON e salvalo come:\n"
            f"     {CREDENTIALS_FILE}\n"
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(TOKEN_FILE) or ".", exist_ok=True)
    with open(TOKEN_FILE, "w") as fh:
        fh.write(creds.to_json())

    print(f"[OK] Token salvato in: {TOKEN_FILE}")
    print("Puoi ora avviare il sync con:  python main.py")


if __name__ == "__main__":
    main()
