#!/usr/bin/env python3
"""
Run this script ONCE to complete the Gmail OAuth2 flow and save token.json.
After this, main.py will refresh the token automatically.

Prerequisite:
  1. Go to https://console.cloud.google.com
  2. Enable the Gmail API for your project
  3. Create an OAuth2 Desktop credential and download credentials.json
  4. Place credentials.json in this directory (or set GMAIL_CREDENTIALS_FILE)
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def main() -> None:
    from config import GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE

    logger.info(f"Credenziali: {GMAIL_CREDENTIALS_FILE}")
    logger.info(f"Token output: {GMAIL_TOKEN_FILE}")

    import os
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        logger.error(
            f"File '{GMAIL_CREDENTIALS_FILE}' non trovato.\n"
            "Scarica le credenziali OAuth2 dalla Google Cloud Console e\n"
            "rinominale in credentials.json (oppure imposta GMAIL_CREDENTIALS_FILE)."
        )
        sys.exit(1)

    try:
        from gmail_client import GmailClient
        client = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)
        msgs = client.list_inbox_messages(max_results=1)
        logger.info(f"Autenticazione completata. Token salvato in: {GMAIL_TOKEN_FILE}")
        logger.info(f"Test di connessione OK (trovato {len(msgs)} messaggio/i in inbox).")
    except Exception as exc:
        logger.error(f"Autenticazione fallita: {exc}")
        sys.exit(1)


if __name__ == '__main__':
    main()
