"""
Configuration loaded from environment variables / .env file.
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    def __init__(self) -> None:
        self.hubspot_token: str = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
        self.gmail_credentials_path: str = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")
        self.gmail_token_path: str = os.environ.get("GMAIL_TOKEN_PATH", "token.pickle")
        self.max_emails: int = int(os.environ.get("MAX_EMAILS", "200"))
        self.gmail_query: str = os.environ.get(
            "GMAIL_QUERY", "in:inbox is:unread -in:draft"
        )
        self.contact_source_label: str = os.environ.get("CONTACT_SOURCE_LABEL", "Gmail")
        self.contact_tag: str = os.environ.get("CONTACT_TAG", "Inbound Gmail")

        if not self.hubspot_token:
            raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is required")
