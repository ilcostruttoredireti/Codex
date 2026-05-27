import os
from dotenv import load_dotenv

load_dotenv()

# Gmail OAuth2
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# HubSpot
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Sync behaviour
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Domains to ignore (internal / no-reply senders)
IGNORED_DOMAINS = {
    "noreply.com", "no-reply.com", "mailchimp.com", "sendgrid.net",
    "bounce.com", "mailer-daemon", "googlemail.com",
}
IGNORED_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "bounce", "mailer-daemon"}
