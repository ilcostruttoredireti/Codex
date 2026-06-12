import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    gmail_credentials_file: str
    gmail_token_file: str
    hubspot_access_token: str
    polling_interval: int
    state_file: str
    log_level: str
    initial_messages: int


def load_config() -> Config:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN mancante. "
            "Impostalo nel file .env o come variabile d'ambiente."
        )

    return Config(
        gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        gmail_token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
        hubspot_access_token=token,
        polling_interval=int(os.getenv("POLLING_INTERVAL", "60")),
        state_file=os.getenv("STATE_FILE", ".sync_state.json"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        initial_messages=int(os.getenv("INITIAL_MESSAGES", "50")),
    )
