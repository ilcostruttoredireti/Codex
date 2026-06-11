import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
DAYS_LOOKBACK = int(os.getenv("DAYS_LOOKBACK", "7"))

SKIP_SENDERS = set(
    e.strip().lower()
    for e in os.getenv(
        "SKIP_SENDERS",
        "mailer-daemon@googlemail.com,no-reply@accounts.google.com,"
        "analytics-noreply@google.com,notification@priority.facebookmail.com,"
        "posta-certificata@legalmail.it,google-noreply@google.com",
    ).split(",")
    if e.strip()
)

OWN_EMAILS = set(
    e.strip().lower()
    for e in os.getenv(
        "OWN_EMAILS",
        "pubblica.latestata@gmail.com,cristian.mameli.editore@gmail.com,"
        "cristian.mameli@gmail.com,redazione@latestata.it",
    ).split(",")
    if e.strip()
)
