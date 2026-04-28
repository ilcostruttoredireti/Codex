import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_LABEL_NAME = "HubSpot-Synced"
STATE_FILE = os.getenv("STATE_FILE", "state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Domains treated as personal — company name will not be inferred from them
PERSONAL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.co.uk", "yahoo.it",
    "hotmail.com", "hotmail.it", "hotmail.co.uk",
    "outlook.com", "live.com", "live.it", "live.co.uk",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "tutanota.com",
    "aol.com", "msn.com", "libero.it", "virgilio.it", "tin.it",
})
