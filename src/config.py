import os
from dataclasses import dataclass, field


@dataclass
class Config:
    gmail_credentials_file: str
    gmail_token_file: str
    hubspot_api_key: str
    poll_interval: int = 60
    state_file: str = "data/sync_state.json"
    exclude_domains: list = field(default_factory=list)
    exclude_no_reply: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        hubspot_key = os.environ.get("HUBSPOT_API_KEY", "")
        if not hubspot_key:
            raise EnvironmentError("HUBSPOT_API_KEY environment variable is required")

        raw_excluded = os.environ.get("EXCLUDE_DOMAINS", "")
        excluded = [d.strip() for d in raw_excluded.split(",") if d.strip()] if raw_excluded else []

        return cls(
            gmail_credentials_file=os.environ.get(
                "GMAIL_CREDENTIALS_FILE", "credentials/gmail_credentials.json"
            ),
            gmail_token_file=os.environ.get(
                "GMAIL_TOKEN_FILE", "credentials/gmail_token.json"
            ),
            hubspot_api_key=hubspot_key,
            poll_interval=int(os.environ.get("POLL_INTERVAL", "60")),
            state_file=os.environ.get("STATE_FILE", "data/sync_state.json"),
            exclude_domains=excluded,
            exclude_no_reply=os.environ.get("EXCLUDE_NO_REPLY", "true").lower() == "true",
        )
