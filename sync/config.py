import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN: str = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
STATE_FILE: str = os.environ.get("STATE_FILE", ".sync_state.json")
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Email domains considered personal/free (no company extraction)
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr", "yahoo.es",
    "hotmail.com", "hotmail.it", "hotmail.co.uk", "hotmail.fr",
    "outlook.com", "outlook.it",
    "live.com", "live.it",
    "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "protonmail.ch", "pm.me",
    "tutanota.com", "tutamail.com",
    "zoho.com", "fastmail.com", "fastmail.fm",
    "yandex.com", "yandex.ru",
    "mail.com", "email.com",
    # Italian providers
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "tin.it", "email.it", "katamail.com", "iol.it",
}
