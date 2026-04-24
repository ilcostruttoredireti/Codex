"""
Script di utilità per configurare le credenziali Gmail la prima volta.

Uso:
    python setup_gmail.py

Genera il file token.json necessario per il funzionamento del sync.
"""

import os
from dotenv import load_dotenv
from gmail_reader import GmailReader

load_dotenv()

credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")

print("=== Setup Gmail OAuth2 ===")
print(f"Credenziali: {credentials_file}")
print(f"Token output: {token_file}")
print()

if not os.path.exists(credentials_file):
    print(f"ERRORE: file {credentials_file!r} non trovato.")
    print("Scaricalo da: https://console.cloud.google.com/apis/credentials")
    print("  1. Crea un progetto Google Cloud")
    print("  2. Abilita l'API Gmail")
    print("  3. Crea credenziali OAuth2 Desktop")
    print("  4. Scarica il JSON e rinominalo 'credentials.json'")
    raise SystemExit(1)

reader = GmailReader(credentials_file, token_file)
reader.authenticate()
print(f"Token salvato in {token_file!r}. Setup completato!")
print("Ora puoi avviare il sync con: python main.py")
