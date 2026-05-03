import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json"))
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", str(BASE_DIR / "token.json"))

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
STATE_FILE = os.getenv("STATE_FILE", str(BASE_DIR / "state.json"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
