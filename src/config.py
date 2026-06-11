import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it",
    "icloud.com", "live.com", "live.it", "me.com",
    "protonmail.com", "proton.me", "aol.com", "msn.com",
    "libero.it", "tiscali.it", "alice.it", "virgilio.it",
    "tin.it", "fastwebnet.it", "email.it",
}


@dataclass
class Config:
    hubspot_api_token: str
    gmail_credentials_file: str = "credentials.json"
    gmail_token_file: str = "token.json"
    poll_interval_seconds: int = 60
    state_file: str = "sync_state.json"
    skip_personal_domains: bool = False
    personal_domains: set = field(default_factory=lambda: PERSONAL_DOMAINS)


def load_config() -> Config:
    load_dotenv()

    token = os.environ.get("HUBSPOT_API_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "HUBSPOT_API_TOKEN is required. "
            "Create a Private App in HubSpot Settings → Integrations → Private Apps."
        )

    skip = os.getenv("SKIP_PERSONAL_DOMAINS", "false").lower() in ("true", "1", "yes")

    return Config(
        hubspot_api_token=token,
        gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        gmail_token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        state_file=os.getenv("STATE_FILE", "sync_state.json"),
        skip_personal_domains=skip,
    )
