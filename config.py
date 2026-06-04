import os
from dotenv import load_dotenv

load_dotenv()

# Gmail OAuth2
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# HubSpot Private App access token
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

# Sync behaviour
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "1"))

# Personal / free-mail domains — company name won't be inferred from these
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr",
    "hotmail.com", "hotmail.it", "hotmail.co.uk",
    "outlook.com", "outlook.it",
    "live.com", "live.it",
    "icloud.com", "me.com", "mac.com",
    "msn.com", "aol.com",
    "protonmail.com", "proton.me",
    "tutanota.com", "tutamail.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
}
