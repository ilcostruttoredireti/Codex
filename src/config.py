import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# HubSpot association type ID: Note → Contact (HUBSPOT_DEFINED = 202)
HUBSPOT_NOTE_TO_CONTACT_TYPE_ID = 202
