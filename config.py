import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_API_KEY: str = os.environ.get("HUBSPOT_API_KEY", "")
GMAIL_CREDENTIALS_FILE: str = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL_SECONDS: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
STATE_DB_PATH: str = os.environ.get("STATE_DB_PATH", "sync_state.db")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Free personal email domains — company name will NOT be derived from these
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "live.com", "live.it", "icloud.com",
    "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "gmx.com", "gmx.net", "mail.com", "ymail.com", "msn.com",
    "inbox.com", "libero.it", "virgilio.it", "alice.it", "tiscali.it",
}
