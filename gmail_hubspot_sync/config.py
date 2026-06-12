import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_IDS_FILE = os.getenv("PROCESSED_IDS_FILE", ".processed_emails.json")

# Domains to skip (transactional / no-reply senders)
IGNORED_DOMAINS = {
    "noreply.github.com", "notifications.github.com",
    "mailer.notion.so", "mail.notion.so",
    "bounce.em.hubspot.com",
}

IGNORED_EMAIL_PREFIXES = {"noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster"}

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"
