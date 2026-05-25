"""Configurazione centralizzata tramite variabili d'ambiente (.env)."""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Carica .env dalla root del progetto (o dalla directory corrente)
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")


def _get_required(key: str) -> str:
    """Legge una variabile obbligatoria; lancia ValueError se mancante."""
    value = os.getenv(key)
    if not value:
        raise ValueError(
            f"Variabile d'ambiente obbligatoria mancante: '{key}'\n"
            f"Copia .env.example in .env e compila i valori."
        )
    return value


# ─── Gmail ────────────────────────────────────────────────────────────────────
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_USER_ID: str = os.getenv("GMAIL_USER_ID", "me")

# Scopes Gmail richiesti (sola lettura)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ─── HubSpot ──────────────────────────────────────────────────────────────────
HUBSPOT_ACCESS_TOKEN: str = _get_required("HUBSPOT_ACCESS_TOKEN")

# ─── Comportamento sync ───────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE: str = os.getenv("STATE_FILE", ".gmail_sync_state.json")

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ─── Filtri ───────────────────────────────────────────────────────────────────
_raw_ignore = os.getenv("IGNORE_DOMAINS", "")
IGNORE_DOMAINS: set[str] = (
    {d.strip().lower() for d in _raw_ignore.split(",") if d.strip()}
    if _raw_ignore
    else set()
)
IGNORE_SELF: bool = os.getenv("IGNORE_SELF", "true").lower() == "true"

# Domini di servizio noti da ignorare sempre
SYSTEM_DOMAINS: set[str] = {
    "noreply.github.com",
    "mailer-daemon.google.com",
    "bounce.gmail.com",
    "postmaster.google.com",
    "accounts.google.com",
    "notifications.google.com",
}

ALL_IGNORE_DOMAINS = IGNORE_DOMAINS | SYSTEM_DOMAINS

# ─── HubSpot tag e sorgente ───────────────────────────────────────────────────
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"


def setup_logging() -> logging.Logger:
    """Configura il logging e restituisce il logger root."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("gmail_hubspot_sync")
