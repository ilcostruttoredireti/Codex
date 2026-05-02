import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox is:unread")

SKIP_DOMAINS = set(
    d.strip().lower()
    for d in os.getenv(
        "SKIP_DOMAINS",
        "gmail.com,yahoo.com,hotmail.com,outlook.com,icloud.com,"
        "protonmail.com,live.com,aol.com,mail.com",
    ).split(",")
)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
