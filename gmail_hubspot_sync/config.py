import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

CONTACT_SOURCE = "Gmail"
SKIP_SENDERS = {
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "mailer", "support",
}
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "msn.com", "aol.com",
    "protonmail.com", "mail.com", "libero.it", "tiscali.it",
}
