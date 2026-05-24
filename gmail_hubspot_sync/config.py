"""
config.py — Configurazione centralizzata del progetto.

Carica le variabili d'ambiente da .env e le espone come oggetto Config
immutabile usato dagli altri moduli.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet

from dotenv import load_dotenv

# Carica .env dalla directory in cui risiede questo file
_BASE_DIR = Path(__file__).parent
load_dotenv(_BASE_DIR / ".env")


@dataclass(frozen=True)
class Config:
    # ── Gmail ──────────────────────────────────────────────────────────────
    gmail_client_id: str = field(default_factory=lambda: os.getenv("GMAIL_CLIENT_ID", ""))
    gmail_client_secret: str = field(default_factory=lambda: os.getenv("GMAIL_CLIENT_SECRET", ""))
    gmail_token_file: Path = field(
        default_factory=lambda: Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
    )
    gmail_credentials_file: Path = field(
        default_factory=lambda: Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
    )

    # ── HubSpot ────────────────────────────────────────────────────────────
    hubspot_access_token: str = field(
        default_factory=lambda: os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    )

    # ── Sync ───────────────────────────────────────────────────────────────
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    )
    processed_label: str = field(
        default_factory=lambda: os.getenv("PROCESSED_LABEL", "HubSpot-Synced")
    )
    ignore_domains: FrozenSet[str] = field(
        default_factory=lambda: frozenset(
            d.strip().lower()
            for d in os.getenv(
                "IGNORE_DOMAINS",
                "noreply.com,no-reply.com,mailer.com,mailchimp.com,sendgrid.net",
            ).split(",")
            if d.strip()
        )
    )
    contact_source: str = field(
        default_factory=lambda: os.getenv("CONTACT_SOURCE", "Gmail")
    )
    contact_tag: str = field(
        default_factory=lambda: os.getenv("CONTACT_TAG", "Inbound Gmail")
    )
    log_file: Path = field(
        default_factory=lambda: Path(os.getenv("LOG_FILE", "sync.log"))
    )

    def validate(self) -> None:
        """Lancia ValueError se i parametri obbligatori mancano."""
        missing: list[str] = []
        if not self.hubspot_access_token:
            missing.append("HUBSPOT_ACCESS_TOKEN")
        # credentials.json o GMAIL_CLIENT_ID sono alternativi
        if not self.gmail_credentials_file.exists() and not self.gmail_client_id:
            missing.append("GMAIL_CLIENT_ID / credentials.json")
        if missing:
            raise ValueError(
                f"Variabili d'ambiente mancanti: {', '.join(missing)}. "
                "Copia .env.example in .env e compilale."
            )


# Istanza globale
config = Config()
