import os
from dotenv import load_dotenv

load_dotenv()

HUBSPOT_ACCESS_TOKEN: str = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE: str = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
POLLING_INTERVAL_SECONDS: int = int(os.environ.get("POLLING_INTERVAL_SECONDS", "60"))
MESSAGES_PER_RUN: int = int(os.environ.get("MESSAGES_PER_RUN", "50"))
CHECKPOINT_FILE: str = os.environ.get("CHECKPOINT_FILE", ".sync_checkpoint.json")
CREATE_TIMELINE_ACTIVITY: bool = os.environ.get("CREATE_TIMELINE_ACTIVITY", "true").lower() == "true"
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
