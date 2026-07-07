"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs sender contacts to HubSpot.
"""

import re
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── skip patterns ────────────────────────────────────────────────────────────
SKIP_PREFIXES = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "dont-reply", "donot-reply", "notify-noreply", "notifications-noreply",
    "updates-noreply", "sc-noreply", "ads-noreply", "bounce", "mailer",
    "nobody", "system", "daemon", "postmaster", "abuse", "root",
    "mail-noreply", "reply-noreply",
}
SKIP_DOMAINS = {
    "google.com", "googlemail.com", "gmail.com", "linkedin.com",
    "facebook.com", "twitter.com", "instagram.com", "tiktok.com",
    "shop.tiktok.com", "skool.com", "circle.so",
}
# Senders whose full address should be ignored (wildcards via prefix check above)
SKIP_FULL = {"nobody@e.feedspot.com"}

LEAD_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

STATE_FILE = Path(__file__).parent / ".gmail_sync_state.json"


# ─── data model ───────────────────────────────────────────────────────────────
@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    detail: str = ""


# ─── helpers ──────────────────────────────────────────────────────────────────
def _domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD + common words)."""
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


def _parse_sender(raw_sender: str) -> tuple[str, str, str]:
    """Return (email, first_name, last_name) from 'Name <email>' or 'email'."""
    m = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', raw_sender)
    if m:
        display, email = m.group(1).strip(), m.group(2).strip().lower()
        parts = display.split(None, 1)
        return email, parts[0], parts[1] if len(parts) > 1 else ""
    email = raw_sender.strip().lower()
    local = email.split("@")[0]
    parts = re.split(r"[._\-]+", local)
    if len(parts) >= 2:
        return email, parts[0].title(), parts[1].title()
    return email, local.title(), ""


def should_skip(email: str) -> bool:
    """Return True for automated / uninteresting senders."""
    email = email.lower()
    if email in SKIP_FULL:
        return True
    local = email.split("@")[0]
    domain = _domain(email)
    if any(local.startswith(pfx) for pfx in SKIP_PREFIXES):
        return True
    if domain in SKIP_DOMAINS:
        return True
    return False


def extract_contact(raw_sender: str) -> Optional[SenderContact]:
    email, first, last = _parse_sender(raw_sender)
    if not email or should_skip(email):
        return None
    domain = _domain(email)
    company = _company_from_domain(domain)
    return SenderContact(
        email=email,
        first_name=first,
        last_name=last,
        company=company,
        domain=domain,
    )


# ─── state persistence ────────────────────────────────────────────────────────
def load_processed_ids() -> set[str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("processed_thread_ids", []))
        except Exception:
            pass
    return set()


def save_processed_ids(ids: set[str]) -> None:
    existing: dict = {}
    if STATE_FILE.exists():
        try:
            existing = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    existing["processed_thread_ids"] = list(ids)
    existing["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(existing, indent=2))


# ─── HubSpot helpers (called externally via MCP) ──────────────────────────────
def build_hubspot_properties(contact: SenderContact, existing: Optional[dict] = None) -> dict:
    """Build the properties dict to send to HubSpot."""
    props: dict[str, str] = {}
    ex = existing or {}

    if not ex.get("email"):
        props["email"] = contact.email
    if not ex.get("firstname") and contact.first_name:
        props["firstname"] = contact.first_name
    if not ex.get("lastname") and contact.last_name:
        props["lastname"] = contact.last_name
    if not ex.get("company") and contact.company:
        props["company"] = contact.company
    # Always stamp / overwrite lead source so it reflects Gmail origin
    if not ex.get("hs_lead_source"):
        props["hs_lead_source"] = LEAD_SOURCE

    return props


def determine_status(existing_id: Optional[str], updated_fields: dict) -> str:
    if existing_id is None:
        return "Creato"
    if updated_fields:
        return "Aggiornato"
    return "Ignorato"


# ─── main orchestration logic ─────────────────────────────────────────────────
def process_threads(threads: list[dict]) -> list[dict]:
    """
    Extract unique real contacts from a list of Gmail thread objects.
    Returns list of contact dicts ready for HubSpot lookup/create.
    """
    seen: dict[str, SenderContact] = {}
    for thread in threads:
        for msg in thread.get("messages", []):
            sender_raw = msg.get("sender", "")
            contact = extract_contact(sender_raw)
            if contact and contact.email not in seen:
                seen[contact.email] = contact
    return [
        {
            "email": c.email,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "company": c.company,
            "domain": c.domain,
        }
        for c in seen.values()
    ]


def format_report(results: list[SyncResult]) -> str:
    lines = ["Gmail → HubSpot Sync Report", "=" * 40]
    created = [r for r in results if r.status == "Creato"]
    updated = [r for r in results if r.status == "Aggiornato"]
    ignored = [r for r in results if r.status == "Ignorato"]

    for label, group in [("CREATI", created), ("AGGIORNATI", updated), ("IGNORATI", ignored)]:
        if group:
            lines.append(f"\n{label} ({len(group)}):")
            for r in group:
                hs = f"[HS:{r.hubspot_id}]" if r.hubspot_id else ""
                lines.append(f"  • {r.email} {hs}")

    lines.append(
        f"\nTotale: {len(created)} creati, {len(updated)} aggiornati, {len(ignored)} ignorati"
    )
    return "\n".join(lines)
