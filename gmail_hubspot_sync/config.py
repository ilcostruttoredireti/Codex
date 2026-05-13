"""Load configuration from environment variables / .env file."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    gmail_credentials_file: str
    gmail_token_file: str
    hubspot_api_key: str
    poll_interval: int
    state_file: str

    @classmethod
    def from_env(cls) -> "Config":
        missing = []
        hubspot_api_key = os.getenv("HUBSPOT_API_KEY", "")
        if not hubspot_api_key:
            missing.append("HUBSPOT_API_KEY")

        credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
        if not os.path.exists(credentials_file) and not os.path.exists(
            os.getenv("GMAIL_TOKEN_FILE", "token.json")
        ):
            missing.append("GMAIL_CREDENTIALS_FILE (file not found)")

        if missing:
            raise EnvironmentError(
                "Variabili di ambiente obbligatorie mancanti: "
                + ", ".join(missing)
                + "\nCopia .env.example in .env e compila i valori."
            )

        return cls(
            gmail_credentials_file=credentials_file,
            gmail_token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
            hubspot_api_key=hubspot_api_key,
            poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            state_file=os.getenv("STATE_FILE", "sync_state.json"),
        )
