import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    GMAIL_CREDENTIALS_PATH: str = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    GMAIL_TOKEN_PATH: str = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    HUBSPOT_API_KEY: str = os.getenv("HUBSPOT_API_KEY", "")
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))
    STATE_FILE: str = os.getenv("STATE_FILE", ".sync_state.json")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    BACKFILL_DAYS: int = int(os.getenv("BACKFILL_DAYS", "0"))

    def validate(self) -> None:
        if not self.HUBSPOT_API_KEY:
            raise ValueError("HUBSPOT_API_KEY is required. Set it in .env or environment.")
        if not os.path.exists(self.GMAIL_CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"Gmail credentials not found at '{self.GMAIL_CREDENTIALS_PATH}'. "
                "Download it from Google Cloud Console → APIs & Services → Credentials."
            )
