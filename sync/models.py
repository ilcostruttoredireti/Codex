from dataclasses import dataclass
from typing import Optional


@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""


@dataclass
class SyncResult:
    """Result of processing a single email message."""
    status: str  # "created" | "updated" | "ignored"
    contact_email: str
    hubspot_id: Optional[str] = None
    reason: Optional[str] = None

    def log_line(self) -> str:
        icon = {"created": "✅", "updated": "🔄", "ignored": "⏭"}.get(self.status, "?")
        parts = [
            f"{icon} {self.status.upper():8s}",
            f"email={self.contact_email}",
            f"hs_id={self.hubspot_id or 'n/a'}",
        ]
        if self.reason:
            parts.append(f"reason={self.reason}")
        return " | ".join(parts)
