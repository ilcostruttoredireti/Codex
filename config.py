import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")
PROCESS_INITIAL_EMAILS = os.getenv("PROCESS_INITIAL_EMAILS", "false").lower() == "true"
INITIAL_EMAILS_MAX = int(os.getenv("INITIAL_EMAILS_MAX", "50"))
