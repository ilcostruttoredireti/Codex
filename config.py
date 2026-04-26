import os
from dotenv import load_dotenv


class Config:
    def __init__(self):
        load_dotenv()
        self.gmail_credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
        self.gmail_token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
        self.hubspot_access_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
        self.poll_interval = int(os.getenv("POLL_INTERVAL", "60"))
        self.state_file = os.getenv("STATE_FILE", "sync_state.json")

        skip_str = os.getenv(
            "SKIP_DOMAINS",
            "gmail.com,googlemail.com,yahoo.com,ymail.com,hotmail.com,"
            "live.com,outlook.com,msn.com,icloud.com,me.com,mac.com,aol.com",
        )
        self.skip_domains = {d.strip().lower() for d in skip_str.split(",") if d.strip()}

        if not self.hubspot_access_token:
            raise ValueError(
                "HUBSPOT_ACCESS_TOKEN is required. Add it to .env or set the environment variable."
            )
