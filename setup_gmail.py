#!/usr/bin/env python3
"""
One-time script to authorise Gmail access and save token.json.
Run this once on a machine with a browser, then copy token.json
to the server running sync.py.
"""

from dotenv import load_dotenv
import os
from google_auth_oauthlib.flow import InstalledAppFlow
from pathlib import Path

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")

if not Path(CREDENTIALS_FILE).exists():
    raise SystemExit(
        f"File non trovato: {CREDENTIALS_FILE}\n"
        "Scarica le credenziali OAuth 2.0 da Google Cloud Console e salvale come credentials.json"
    )

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
creds = flow.run_local_server(port=0)
Path(TOKEN_FILE).write_text(creds.to_json())
print(f"Token salvato in: {TOKEN_FILE}")
print("Ora puoi avviare sync.py")
