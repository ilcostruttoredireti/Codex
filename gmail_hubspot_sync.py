#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for incoming emails, extracts sender data, and syncs
contacts to HubSpot. Uses email address as the deduplication key.

Standalone mode requires:
  - Google Gmail API credentials (OAuth2)
  - HubSpot Private App token

Claude Code routine mode: uses MCP tools directly (no credentials needed).
"""

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Configuration ────────────────────────────────────────────────────────────

SYNC_STATE_FILE = Path(__file__).parent / "sync_state.json"
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Senders to skip (system, internal, automated)
SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "google.com",
    "mailer-daemon.google.com",
}
SKIP_EMAILS = {
    "pubblica.latestata@gmail.com",
    "redazione@latestata.it",
    "cristian.mameli.editore@gmail.com",
    "mailer-daemon@googlemail.com",
    "analytics-noreply@google.com",
}
SKIP_PREFIXES = ("noreply", "no-reply", "notification", "mailer-daemon", "postmaster")


# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    source_message_id: str = ""

    def __post_init__(self):
        if "@" in self.email:
            self.domain = self.email.split("@")[1]

    @property
    def is_personal_gmail(self) -> bool:
        return self.domain in ("gmail.com", "libero.it", "hotmail.com",
                               "yahoo.com", "outlook.com", "alice.it")

    def infer_company_from_domain(self):
        if self.company or self.is_personal_gmail:
            return
        # Strip TLD and clean domain for company name
        parts = self.domain.split(".")
        if len(parts) >= 2:
            raw = parts[-2] if parts[-2] not in ("co", "org", "edu", "gov") else parts[-3]
            self.company = raw.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[int]
    status: str  # "CREATO" | "AGGIORNATO" | "IGNORATO" | "ERRORE"
    detail: str = ""


@dataclass
class SyncState:
    last_run: str = ""
    processed_thread_ids: list = field(default_factory=list)
    total_created: int = 0
    total_updated: int = 0
    total_skipped: int = 0

    @classmethod
    def load(cls) -> "SyncState":
        if SYNC_STATE_FILE.exists():
            data = json.loads(SYNC_STATE_FILE.read_text())
            return cls(**data)
        return cls()

    def save(self):
        SYNC_STATE_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))

    def mark_processed(self, thread_id: str):
        if thread_id not in self.processed_thread_ids:
            self.processed_thread_ids.append(thread_id)
        # Keep only last 5000 IDs to prevent unbounded growth
        if len(self.processed_thread_ids) > 5000:
            self.processed_thread_ids = self.processed_thread_ids[-5000:]


# ── Email parsing helpers ─────────────────────────────────────────────────────

def should_skip(email: str) -> bool:
    email_lower = email.lower()
    if email_lower in SKIP_EMAILS:
        return True
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    if domain in SKIP_DOMAINS:
        return True
    local = email_lower.split("@")[0]
    return any(local.startswith(p) for p in SKIP_PREFIXES)


def parse_sender_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' → (firstname, lastname). Returns ('', '') on failure."""
    name = display_name.strip().strip('"').strip("'")
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_forwarded_sender(snippet: str) -> Optional[str]:
    """Extract real sender email from 'Da: Name <email>' in forwarded snippets."""
    match = re.search(r"Da\s*[:\"]?\s*[^<]*<([^>]+)>", snippet, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"From\s*[:\"]?\s*[^<]*<([^>]+)>", snippet, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def extract_forwarded_name(snippet: str) -> str:
    """Extract sender display name from forwarded snippet."""
    match = re.search(r'Da\s*[:\"]?\s*"?([^"<\n]+)"?\s*<', snippet, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r'From\s*[:\"]?\s*"?([^"<\n]+)"?\s*<', snippet, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def build_contact_from_thread(thread: dict) -> list[SenderContact]:
    """Extract one or more contacts from a Gmail thread dict."""
    contacts = []
    seen_emails = set()

    for msg in thread.get("messages", []):
        sender = msg.get("sender", "")
        snippet = msg.get("snippet", "")
        msg_id = msg.get("id", "")

        # Parse the direct sender
        if "<" in sender:
            match = re.match(r'^(.*?)\s*<([^>]+)>', sender)
            if match:
                name_raw, email = match.group(1).strip(), match.group(2).strip()
            else:
                email = sender.strip("<>")
                name_raw = ""
        else:
            email = sender.strip()
            name_raw = ""

        email = email.lower()

        if email and email not in seen_emails and not should_skip(email):
            firstname, lastname = parse_sender_name(name_raw)
            c = SenderContact(
                email=email,
                firstname=firstname,
                lastname=lastname,
                source_message_id=msg_id,
            )
            c.infer_company_from_domain()
            seen_emails.add(email)
            contacts.append(c)

        # For forwarded/relay emails (e.g. redazione@latestata.it), also extract
        # the original sender from the snippet
        if snippet and ("Da:" in snippet or "From:" in snippet):
            fwd_email = extract_forwarded_sender(snippet)
            if fwd_email:
                fwd_email = fwd_email.lower()
                if fwd_email not in seen_emails and not should_skip(fwd_email):
                    fwd_name = extract_forwarded_name(snippet)
                    firstname, lastname = parse_sender_name(fwd_name)
                    c = SenderContact(
                        email=fwd_email,
                        firstname=firstname,
                        lastname=lastname,
                        source_message_id=msg_id,
                    )
                    c.infer_company_from_domain()
                    seen_emails.add(fwd_email)
                    contacts.append(c)

    return contacts


# ── HubSpot helpers (standalone API mode) ────────────────────────────────────

def hubspot_search_contact(email: str, token: str) -> Optional[dict]:
    """Search for a contact by email. Returns the contact dict or None."""
    import urllib.request
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    payload = json.dumps({
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        "properties": ["email", "firstname", "lastname", "company", "leadsource"],
        "limit": 1,
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    results = data.get("results", [])
    return results[0] if results else None


def hubspot_create_contact(contact: SenderContact, token: str) -> dict:
    """Create a new HubSpot contact. Returns the created object."""
    import urllib.request
    url = "https://api.hubapi.com/crm/v3/objects/contacts"
    props = {
        "email": contact.email,
        "leadsource": CONTACT_SOURCE,
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company
    payload = json.dumps({"properties": props}).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def hubspot_update_contact(contact_id: str, updates: dict, token: str) -> dict:
    """Patch an existing HubSpot contact with the given property updates."""
    import urllib.request
    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
    payload = json.dumps({"properties": updates}).encode()
    req = urllib.request.Request(url, data=payload, method="PATCH", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ── Gmail helpers (standalone API mode) ──────────────────────────────────────

def gmail_list_inbox_threads(creds, since_history_id: Optional[str] = None,
                              max_results: int = 50) -> list[dict]:
    """
    List inbox threads using the Gmail API.
    `creds` is a google.oauth2.credentials.Credentials object.
    """
    from googleapiclient.discovery import build  # type: ignore

    service = build("gmail", "v1", credentials=creds)
    query = "in:inbox -from:me"
    kwargs = {"userId": "me", "q": query, "maxResults": max_results}
    result = service.users().threads().list(**kwargs).execute()

    threads = []
    for t in result.get("threads", []):
        thread_data = service.users().threads().get(
            userId="me", id=t["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        msgs = []
        for m in thread_data.get("messages", []):
            headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
            msgs.append({
                "id": m["id"],
                "sender": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": m.get("snippet", ""),
                "labelIds": m.get("labelIds", []),
            })
        threads.append({"id": t["id"], "messages": msgs})

    return threads


# ── Main sync logic ──────────────────────────────────────────────────────────

def sync_contacts(threads: list[dict], hubspot_token: str) -> list[SyncResult]:
    """
    Core sync logic. Works in standalone mode with a HubSpot token.
    For Claude Code / MCP mode, use sync_contacts_mcp() instead.
    """
    results = []
    processed_contacts: dict[str, SyncResult] = {}

    for thread in threads:
        contacts = build_contact_from_thread(thread)
        for contact in contacts:
            if contact.email in processed_contacts:
                continue  # already handled this email in this run

            existing = hubspot_search_contact(contact.email, hubspot_token)

            if existing is None:
                # CREATE
                try:
                    created = hubspot_create_contact(contact, hubspot_token)
                    r = SyncResult(
                        email=contact.email,
                        hubspot_id=int(created["id"]),
                        status="CREATO",
                    )
                except Exception as e:
                    r = SyncResult(email=contact.email, hubspot_id=None,
                                   status="ERRORE", detail=str(e))
            else:
                # UPDATE missing fields
                existing_props = existing.get("properties", {})
                updates = {}
                if not existing_props.get("firstname") and contact.firstname:
                    updates["firstname"] = contact.firstname
                if not existing_props.get("lastname") and contact.lastname:
                    updates["lastname"] = contact.lastname
                if not existing_props.get("company") and contact.company:
                    updates["company"] = contact.company
                if not existing_props.get("leadsource"):
                    updates["leadsource"] = CONTACT_SOURCE

                if updates:
                    try:
                        hubspot_update_contact(existing["id"], updates, hubspot_token)
                        r = SyncResult(
                            email=contact.email,
                            hubspot_id=int(existing["id"]),
                            status="AGGIORNATO",
                            detail=f"Aggiornati: {', '.join(updates.keys())}",
                        )
                    except Exception as e:
                        r = SyncResult(email=contact.email,
                                       hubspot_id=int(existing["id"]),
                                       status="ERRORE", detail=str(e))
                else:
                    r = SyncResult(
                        email=contact.email,
                        hubspot_id=int(existing["id"]),
                        status="IGNORATO",
                        detail="Tutti i campi già presenti",
                    )

            processed_contacts[contact.email] = r
            results.append(r)
            time.sleep(0.1)  # gentle rate limiting

    return results


def print_report(results: list[SyncResult]):
    width = 76
    print("=" * width)
    print(f"{'GMAIL → HUBSPOT SYNC REPORT':^{width}}")
    print(f"{'Eseguito: ' + datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'):^{width}}")
    print("=" * width)

    counts = {"CREATO": 0, "AGGIORNATO": 0, "IGNORATO": 0, "ERRORE": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        icon = {"CREATO": "✚", "AGGIORNATO": "↻", "IGNORATO": "–", "ERRORE": "✗"}.get(r.status, "?")
        hs_id = str(r.hubspot_id) if r.hubspot_id else "—"
        detail = f"  [{r.detail}]" if r.detail else ""
        print(f"  {icon} {r.status:<12} {r.email:<45} ID: {hs_id}{detail}")

    print("-" * width)
    print(f"  Totale: {len(results)} contatti  |  "
          f"Creati: {counts['CREATO']}  "
          f"Aggiornati: {counts['AGGIORNATO']}  "
          f"Ignorati: {counts['IGNORATO']}  "
          f"Errori: {counts['ERRORE']}")
    print("=" * width)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--hubspot-token", envvar="HUBSPOT_TOKEN",
                        default=os.getenv("HUBSPOT_TOKEN"),
                        help="HubSpot Private App token")
    parser.add_argument("--gmail-credentials", default="credentials.json",
                        help="Path to Google OAuth2 credentials JSON")
    parser.add_argument("--days", type=int, default=1,
                        help="Look back N days for emails (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse emails but do not write to HubSpot")
    args = parser.parse_args()

    if not args.hubspot_token and not args.dry_run:
        raise SystemExit("Set HUBSPOT_TOKEN or pass --hubspot-token")

    # Load Google credentials
    from google.oauth2.credentials import Credentials  # type: ignore
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    token_file = Path("token.json")
    creds = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(args.gmail_credentials, SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())

    state = SyncState.load()
    threads = gmail_list_inbox_threads(creds, max_results=100)

    # Filter already-processed threads
    new_threads = [t for t in threads if t["id"] not in state.processed_thread_ids]

    if not new_threads:
        print("Nessuna nuova email da processare.")
    else:
        if args.dry_run:
            print(f"[DRY RUN] {len(new_threads)} thread da processare:")
            for t in new_threads:
                contacts = build_contact_from_thread(t)
                for c in contacts:
                    print(f"  → {c.email}  ({c.firstname} {c.lastname}) [{c.company}]")
        else:
            results = sync_contacts(new_threads, args.hubspot_token)
            print_report(results)

            state.last_run = datetime.now(timezone.utc).isoformat()
            for t in new_threads:
                state.mark_processed(t["id"])
            state.total_created += sum(1 for r in results if r.status == "CREATO")
            state.total_updated += sum(1 for r in results if r.status == "AGGIORNATO")
            state.total_skipped += sum(1 for r in results if r.status == "IGNORATO")
            state.save()
