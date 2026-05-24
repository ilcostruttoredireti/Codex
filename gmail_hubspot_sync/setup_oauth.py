"""
setup_oauth.py — Script di setup guidato per il primo avvio.

Esegui questo script una sola volta per:
1. Verificare la configurazione (.env)
2. Completare il flusso OAuth Gmail (apre il browser)
3. Salvare il token per i run successivi

Utilizzo:
    python setup_oauth.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import config
from gmail_client import GmailClient
from logger import get_logger

log = get_logger("setup")


def main() -> None:
    log.info("=" * 60)
    log.info("  Gmail → HubSpot Sync  |  Setup OAuth")
    log.info("=" * 60)

    # Verifica .env
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        log.error("❌  File .env non trovato.")
        log.info("   Copia .env.example in .env e compila le variabili:")
        log.info("   cp .env.example .env")
        sys.exit(1)

    # Verifica token HubSpot
    if not config.hubspot_access_token:
        log.error("❌  HUBSPOT_ACCESS_TOKEN non configurato nel .env")
        sys.exit(1)
    log.info("✅  HubSpot token trovato.")

    # OAuth Gmail
    log.info("🔑  Avvio autenticazione Gmail (si aprirà il browser)...")
    client = GmailClient()
    client.authenticate()

    log.info("")
    log.info("✅  Setup completato! Ora puoi avviare il sync:")
    log.info("   python main.py          # loop continuo")
    log.info("   python main.py --once   # singolo ciclo")
    log.info("   python main.py --dry-run  # test senza modifiche")


if __name__ == "__main__":
    main()
