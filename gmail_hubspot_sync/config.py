import os
from dataclasses import dataclass, field


@dataclass
class Config:
    hubspot_api_key: str
    gmail_credentials_file: str
    gmail_token_file: str
    poll_interval: int
    state_file: str
    ignored_domains: set = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "Config":
        ignored_raw = os.environ.get(
            "IGNORED_DOMAINS",
            "gmail.com,yahoo.com,hotmail.com,outlook.com,live.com,icloud.com,protonmail.com",
        )
        return cls(
            hubspot_api_key=os.environ["HUBSPOT_API_KEY"],
            gmail_credentials_file=os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"),
            gmail_token_file=os.environ.get("GMAIL_TOKEN_FILE", "token.json"),
            poll_interval=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
            state_file=os.environ.get("STATE_FILE", "state.json"),
            ignored_domains={d.strip().lower() for d in ignored_raw.split(",")},
        )
