"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts senders, syncs to HubSpot (no duplicates).
"""

import os
import re
import json
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE = "https://api.hubapi.com"

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default

# Own email addresses to skip (won't be added as contacts)
SKIP_EMAILS: set[str] = set(
    e.strip().lower()
    for e in os.getenv("SKIP_EMAILS", "").split(",")
    if e.strip()
)

# Forwarding relay: if sender matches, look inside the body for original sender
RELAY_SENDERS: set[str] = set(
    e.strip().lower()
    for e in os.getenv("RELAY_SENDERS", "redazione@latestata.it").split(",")
    if e.strip()
)


# ── Data model ─────────────────────────────────────────────────────────────────
@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    thread_id: str = ""
    subject: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    def as_hubspot_props(self) -> dict:
        props: dict = {"email": self.email, "leadsource": "Gmail"}
        if self.firstname:
            props["firstname"] = self.firstname
        if self.lastname:
            props["lastname"] = self.lastname
        if self.company:
            props["company"] = self.company
        elif self.domain and not self.domain.endswith(
            ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it",
             "tiscali.it", "virgilio.it", "alice.it", "tin.it")
        ):
            props["company"] = _domain_to_company(self.domain)
        return props


# ── Helpers ────────────────────────────────────────────────────────────────────

# Italian/English forwarded-message header patterns
_FW_PATTERNS = [
    # Da "Name" email@domain  or  Da Name <email@domain>
    re.compile(
        r'(?:^|\n)\s*Da[:\s]+"?([^"<\n]+?)"?\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
        re.MULTILINE,
    ),
    # From: "Name" <email>  or  From: email
    re.compile(
        r'(?:^|\n)\s*From[:\s]+"?([^"<\n]*?)"?\s*<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>',
        re.MULTILINE | re.IGNORECASE,
    ),
    # bare email fallback
    re.compile(r'Da[:\s]+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', re.MULTILINE),
]

_RFC_ADDR = re.compile(
    r'"?([^"<]*?)"?\s*<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>'
)


def _parse_display_name(raw: str) -> tuple[str, str, str]:
    """Return (email, firstname, lastname) from a raw sender string."""
    raw = raw.strip()
    m = _RFC_ADDR.match(raw)
    if m:
        display, email = m.group(1).strip(), m.group(2).strip().lower()
    elif "@" in raw:
        email = raw.lower()
        display = ""
    else:
        return "", "", ""

    firstname, lastname = _split_name(display)
    return email, firstname, lastname


def _split_name(display: str) -> tuple[str, str]:
    """Best-effort split of a display name into first / last."""
    display = display.strip().strip('"').strip("'")
    if not display:
        return "", ""
    # Strip trailing role labels like "- Alta Badia Brand"
    display = re.sub(r"\s*[-–]\s*.+$", "", display).strip()
    parts = display.split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0].capitalize(), ""
    return parts[0].capitalize(), " ".join(p.capitalize() for p in parts[1:])


def _domain_to_company(domain: str) -> str:
    """Convert a domain like 'wemakefuture.it' → 'Wemakefuture'."""
    name = domain.split(".")[0]
    # Insert spaces before uppercase letters in camelCase
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return name.replace("-", " ").title()


def _extract_from_body(text: str) -> tuple[str, str, str]:
    """Try to find original sender in a forwarded message body."""
    for pattern in _FW_PATTERNS:
        m = pattern.search(text)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                name_part, email_part = groups
                if "@" in email_part:
                    fn, ln = _split_name(name_part)
                    return email_part.strip().lower(), fn, ln
                if "@" in name_part:
                    return name_part.strip().lower(), "", ""
    return "", "", ""


# ── State manager ──────────────────────────────────────────────────────────────
class StateManager:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                pass
        return {"processed_threads": [], "last_run": None}

    def _save(self):
        self.path.write_text(json.dumps(self._data, indent=2))

    def is_processed(self, thread_id: str) -> bool:
        return thread_id in self._data["processed_threads"]

    def mark_processed(self, thread_id: str):
        if thread_id not in self._data["processed_threads"]:
            self._data["processed_threads"].append(thread_id)
            self._save()

    def update_last_run(self):
        self._data["last_run"] = datetime.now(timezone.utc).isoformat()
        self._save()


# ── Gmail client ───────────────────────────────────────────────────────────────
class GmailClient:
    def __init__(self):
        self._service = self._build_service()

    def _build_service(self):
        creds = None
        if Path(GMAIL_TOKEN_FILE).exists():
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def fetch_inbox_threads(self, max_results: int = 50) -> list[dict]:
        try:
            resp = (
                self._service.users()
                .threads()
                .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
                .execute()
            )
            return resp.get("threads", [])
        except HttpError as e:
            log.error("Gmail list error: %s", e)
            return []

    def get_thread_messages(self, thread_id: str) -> list[dict]:
        try:
            thread = (
                self._service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
            return thread.get("messages", [])
        except HttpError as e:
            log.error("Gmail get thread %s error: %s", thread_id, e)
            return []


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(msg: dict) -> str:
    """Recursively extract plain-text body from a Gmail message."""
    payload = msg.get("payload", {})
    return _extract_parts(payload)


def _extract_parts(part: dict) -> str:
    mime = part.get("mimeType", "")
    if mime == "text/plain":
        import base64
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if "parts" in part:
        return "\n".join(_extract_parts(p) for p in part["parts"])
    return ""


# ── Contact extractor ──────────────────────────────────────────────────────────
class ContactExtractor:
    def extract(self, messages: list[dict]) -> Optional[Contact]:
        """Return a Contact from the first message of a thread, or None."""
        if not messages:
            return None
        msg = messages[0]
        sender = _header(msg, "From")
        subject = _header(msg, "Subject")
        thread_id = msg.get("threadId", "")

        email, firstname, lastname = _parse_display_name(sender)
        if not email:
            return None

        # If this is a relay/forwarding address, dig into the body
        if email.lower() in RELAY_SENDERS:
            body = _body_text(msg)
            orig_email, orig_fn, orig_ln = _extract_from_body(body)
            if orig_email:
                email, firstname, lastname = orig_email, orig_fn, orig_ln

        email = email.lower().strip()
        if not email or email in SKIP_EMAILS:
            return None

        return Contact(
            email=email,
            firstname=firstname,
            lastname=lastname,
            thread_id=thread_id,
            subject=subject,
        )


# ── HubSpot client ─────────────────────────────────────────────────────────────
class HubSpotClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("HUBSPOT_API_KEY is not set")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        r = requests.get(f"{HUBSPOT_BASE}{path}", headers=self._headers, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(f"{HUBSPOT_BASE}{path}", headers=self._headers, json=body)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, body: dict) -> dict:
        r = requests.patch(f"{HUBSPOT_BASE}{path}", headers=self._headers, json=body)
        r.raise_for_status()
        return r.json()

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "leadsource"],
        }
        try:
            resp = self._post("/crm/v3/objects/contacts/search", body)
            results = resp.get("results", [])
            return results[0] if results else None
        except requests.HTTPError as e:
            log.error("HubSpot search error for %s: %s", email, e)
            return None

    def create_contact(self, props: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, props: dict) -> dict:
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})

    def add_note(self, contact_id: str, body: str):
        """Attach a note (activity) to the contact."""
        note_body = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                }
            ],
        }
        try:
            self._post("/crm/v3/objects/notes", note_body)
        except requests.HTTPError as e:
            log.warning("Could not create note for contact %s: %s", contact_id, e)


# ── Result type ────────────────────────────────────────────────────────────────
@dataclass
class SyncResult:
    status: str        # "CREATED" | "UPDATED" | "SKIPPED"
    email: str
    hubspot_id: str
    subject: str = ""
    reason: str = ""


# ── Sync engine ────────────────────────────────────────────────────────────────
class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient, state: StateManager):
        self.gmail = gmail
        self.hubspot = hubspot
        self.state = state
        self.extractor = ContactExtractor()

    def run_once(self) -> list[SyncResult]:
        threads = self.gmail.fetch_inbox_threads(max_results=50)
        results: list[SyncResult] = []

        for t in threads:
            tid = t["id"]
            if self.state.is_processed(tid):
                continue

            messages = self.gmail.get_thread_messages(tid)
            contact = self.extractor.extract(messages)

            if not contact:
                self.state.mark_processed(tid)
                continue

            result = self._sync_contact(contact)
            results.append(result)
            self.state.mark_processed(tid)
            log.info("[%s]  %-40s  id=%s", result.status, result.email, result.hubspot_id)

        self.state.update_last_run()
        return results

    def _sync_contact(self, contact: Contact) -> SyncResult:
        existing = self.hubspot.find_contact_by_email(contact.email)
        props = contact.as_hubspot_props()

        if existing:
            cid = existing["id"]
            current = existing.get("properties", {})
            # Only send fields that are missing in HubSpot
            updates = {
                k: v for k, v in props.items()
                if v and not current.get(k)
            }
            if updates:
                self.hubspot.update_contact(cid, updates)
                note = f"Email ricevuta via Gmail\nOggetto: {contact.subject}"
                self.hubspot.add_note(cid, note)
                return SyncResult("UPDATED", contact.email, cid, contact.subject)
            return SyncResult("SKIPPED", contact.email, cid, contact.subject,
                              "already complete")

        resp = self.hubspot.create_contact(props)
        cid = resp["id"]
        note = (
            f"Contatto creato automaticamente da Gmail Sync.\n"
            f"Fonte: Gmail Inbound\n"
            f"Tag: Inbound Gmail\n"
            f"Oggetto email: {contact.subject}"
        )
        self.hubspot.add_note(cid, note)
        return SyncResult("CREATED", contact.email, cid, contact.subject)


# ── CLI entry point ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single sync cycle and exit (default: run continuously)"
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL,
        help=f"Polling interval in seconds (default: {POLL_INTERVAL})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse emails and print contacts without touching HubSpot"
    )
    args = parser.parse_args()

    if args.dry_run:
        _dry_run()
        return

    gmail = GmailClient()
    hubspot = HubSpotClient(HUBSPOT_API_KEY)
    state = StateManager(STATE_FILE)
    engine = SyncEngine(gmail, hubspot, state)

    if args.once:
        results = engine.run_once()
        _print_summary(results)
        return

    log.info("Starting continuous sync (interval=%ds). Ctrl-C to stop.", args.interval)
    while True:
        try:
            results = engine.run_once()
            _print_summary(results)
        except Exception as e:
            log.error("Sync error: %s", e)
        time.sleep(args.interval)


def _dry_run():
    """Fetch emails and print parsed contacts without touching HubSpot."""
    log.info("DRY RUN – no HubSpot writes")
    gmail = GmailClient()
    extractor = ContactExtractor()
    threads = gmail.fetch_inbox_threads(max_results=30)
    seen: set[str] = set()
    for t in threads:
        messages = gmail.get_thread_messages(t["id"])
        contact = extractor.extract(messages)
        if contact and contact.email not in seen:
            seen.add(contact.email)
            print(
                f"  email={contact.email:<45}  "
                f"name={contact.firstname} {contact.lastname:<20}  "
                f"company={contact.company or contact.domain}"
            )


def _print_summary(results: list[SyncResult]):
    if not results:
        log.info("No new threads to process.")
        return
    created = sum(1 for r in results if r.status == "CREATED")
    updated = sum(1 for r in results if r.status == "UPDATED")
    skipped = sum(1 for r in results if r.status == "SKIPPED")
    log.info("Summary: %d created, %d updated, %d skipped", created, updated, skipped)
    print("\n{:<10} {:<45} {:<20}".format("STATUS", "EMAIL", "HUBSPOT ID"))
    print("-" * 80)
    for r in results:
        print("{:<10} {:<45} {:<20}".format(r.status, r.email, r.hubspot_id))


if __name__ == "__main__":
    main()
