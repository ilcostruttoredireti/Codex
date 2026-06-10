#!/usr/bin/env python3
"""
Script di setup OAuth2 per Gmail.
Esegui questo script UNA VOLTA per generare token.json prima di avviare il sync.

Prerequisiti:
  1. Vai su https://console.cloud.google.com/
  2. Crea un progetto → Abilita l'API Gmail
  3. Crea credenziali OAuth 2.0 (tipo: Desktop App)
  4. Scarica il JSON e salvalo come 'credentials.json' nella cartella del progetto
  5. Esegui: python setup_oauth.py
"""

import sys
from gmail_hubspot_sync.config import load_config
from gmail_hubspot_sync.gmail_client import GmailClient


def main():
    try:
        config = load_config()
    except (ValueError, KeyError) as e:
        print(f"Errore configurazione: {e}")
        print("Crea il file .env con almeno HUBSPOT_ACCESS_TOKEN.")
        sys.exit(1)

    gmail = GmailClient(
        credentials_file=config.gmail_credentials_file,
        token_file=config.gmail_token_file,
        scopes=config.gmail_scopes,
    )

    print("Apertura browser per autorizzazione Gmail OAuth2...")
    gmail.authenticate()
    print(f"\n✓ Token salvato in '{config.gmail_token_file}'")
    print("Ora puoi avviare il sync con: python main.py")


if __name__ == "__main__":
    main()
