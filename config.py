import os
from dotenv import load_dotenv

load_dotenv()

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "INBOX")
STATE_FILE = os.getenv("STATE_FILE", ".gmail_state.json")

# Gmail OAuth scopes (read-only è sufficiente)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domini da ignorare (noreply, notifiche automatiche, ecc.)
IGNORED_DOMAINS = {
    "noreply.github.com",
    "notifications.github.com",
    "mail.gmail.com",
    "accounts.google.com",
    "bounce.em.hubspot.com",
    "mailer-daemon.googlemail.com",
}

# Indirizzi da ignorare esplicitamente
IGNORED_EMAILS = {
    "noreply@gmail.com",
    "no-reply@accounts.google.com",
    "mailer-daemon@googlemail.com",
}
