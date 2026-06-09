import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN: str = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE: str = os.getenv("STATE_FILE", "processed_messages.json")
MAX_MESSAGES_PER_POLL: int = int(os.getenv("MAX_MESSAGES_PER_POLL", "50"))
