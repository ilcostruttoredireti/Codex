import os
from dotenv import load_dotenv

load_dotenv()

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080")
GOOGLE_TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", ".google_token.json")

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE_PATH = os.getenv("STATE_FILE_PATH", ".sync_state.json")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Free email providers — company name won't be inferred from these domains
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "outlook.it", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "libero.it", "tiscali.it", "virgilio.it", "tin.it", "alice.it",
    "fastwebnet.it", "msn.com", "aol.com",
}
