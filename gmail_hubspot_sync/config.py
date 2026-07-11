import os
from dataclasses import dataclass


@dataclass
class Config:
    # Gmail OAuth credentials (Service Account or OAuth2)
    gmail_credentials_file: str = ""
    gmail_token_file: str = ""
    gmail_scopes: list = None

    # HubSpot private app token
    hubspot_access_token: str = ""

    # Sync settings
    poll_interval_seconds: int = 300        # 5 minutes between checks
    lookback_days: int = 1                  # how far back to look on first run
    state_file: str = ".sync_state.json"    # persists last-seen message ID/timestamp

    # Filter: skip purely automated senders
    skip_automated: bool = True

    def __post_init__(self):
        if self.gmail_scopes is None:
            self.gmail_scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        self.gmail_credentials_file = os.getenv(
            "GMAIL_CREDENTIALS_FILE", self.gmail_credentials_file
        )
        self.gmail_token_file = os.getenv(
            "GMAIL_TOKEN_FILE", self.gmail_token_file or "token.json"
        )
        self.hubspot_access_token = os.getenv(
            "HUBSPOT_ACCESS_TOKEN", self.hubspot_access_token
        )
        self.poll_interval_seconds = int(
            os.getenv("POLL_INTERVAL_SECONDS", str(self.poll_interval_seconds))
        )
        self.lookback_days = int(
            os.getenv("LOOKBACK_DAYS", str(self.lookback_days))
        )
        self.state_file = os.getenv("STATE_FILE", self.state_file)
