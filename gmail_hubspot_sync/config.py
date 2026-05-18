import os
from dotenv import load_dotenv

load_dotenv()

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "sync_state.db")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "50"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
