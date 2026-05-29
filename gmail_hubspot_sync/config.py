"""Load configuration from environment variables."""
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    gmail_credentials_file: str = ""
    gmail_token_file: str = ""
    hubspot_access_token: str = ""
    poll_interval_seconds: int = 300
    state_file: str = "state.json"
    log_level: str = "INFO"
    label_processed: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
            gmail_token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
            hubspot_access_token=os.getenv("HUBSPOT_ACCESS_TOKEN", ""),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
            state_file=os.getenv("STATE_FILE", "state.json"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            label_processed=os.getenv("LABEL_PROCESSED", "false").lower() == "true",
        )

    def validate(self) -> None:
        missing = []
        if not Path(self.gmail_credentials_file).exists():
            missing.append(f"GMAIL_CREDENTIALS_FILE ({self.gmail_credentials_file})")
        if not self.hubspot_access_token:
            missing.append("HUBSPOT_ACCESS_TOKEN")
        if missing:
            raise EnvironmentError(
                "Missing required configuration:\n" + "\n".join(f"  - {m}" for m in missing)
            )
