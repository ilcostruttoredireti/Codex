#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitors Gmail inbox and syncs senders as HubSpot CRM contacts.

Usage:
    python gmail_hubspot_sync.py [--continuous] [--timeline] [--verbose]

Authentication:
    Gmail  → OAuth2 via credentials.json / token.json (Google Cloud Console)
    HubSpot → Private App token via --hubspot-token or env HUBSPOT_TOKEN
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Constants ─────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("processed_messages.json")

# Free/personal email domains — not used as company name
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "tiscali.it", "libero.it", "virgilio.it", "alice.it",
    "tin.it", "fastwebnet.it",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")

# ── Gmail ─────────────────────────────────────────────────────────────────────

def build_gmail_service(credentials_file: str, token_file: str):
    creds = None
    tp = Path(token_file)
    if tp.exists():
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        tp.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def list_inbox_messages(service, max_results: int = 50, page_token: str | None = None):
    """Return (messages, nextPageToken)."""
    kwargs: dict = {
        "userId": "me",
        "labelIds": ["INBOX"],
        "maxResults": max_results,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    result = service.users().messages().list(**kwargs).execute()
    return result.get("messages", []), result.get("nextPageToken")


def get_message_metadata(service, msg_id: str) -> dict:
    """Fetch only From / Date / Subject headers (faster than full fetch)."""
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Date", "Subject"],
        )
        .execute()
    )
    return {h["name"]: h["value"] for h in msg["payload"]["headers"]}


# ── Sender parsing ────────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From: header."""
    name, addr = parseaddr(from_header)
    return name.strip(), addr.strip().lower()


def split_display_name(display_name: str) -> tuple[str, str]:
    """Best-effort (firstname, lastname) from display name."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def company_from_domain(domain: str) -> str:
    """
    Derive a company name from an email domain.
    Returns empty string for personal/free providers.
    Example: cristian@mycompany.it  → Mycompany
    """
    if not domain or domain in PERSONAL_DOMAINS:
        return ""
    parts = domain.split(".")
    # e.g. mail.mycompany.com → mycompany
    raw = parts[-2] if len(parts) >= 2 else domain
    return raw.capitalize()


# ── HubSpot API client ────────────────────────────────────────────────────────

class HubSpotClient:
    _BASE = "https://api.hubapi.com"

    def __init__(self, token: str):
        self._s = requests.Session()
        self._s.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    # --- contacts ---

    def find_contact_by_email(self, email: str) -> dict | None:
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": [
                "email", "firstname", "lastname", "company",
                "hs_lead_source", "hs_tag",
            ],
            "limit": 1,
        }
        r = self._s.post(f"{self._BASE}/crm/v3/objects/contacts/search", json=payload)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        r = self._s.post(
            f"{self._BASE}/crm/v3/objects/contacts", json={"properties": props}
        )
        r.raise_for_status()
        return r.json()

    def update_contact(self, contact_id: str, props: dict) -> dict:
        r = self._s.patch(
            f"{self._BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": props},
        )
        r.raise_for_status()
        return r.json()

    # --- timeline engagement (email received) ---

    def log_email_activity(
        self,
        contact_id: str,
        subject: str,
        sender_email: str,
        timestamp_ms: int,
    ) -> bool:
        """
        Create an inbound email activity on the contact's timeline.
        Uses HubSpot CRM v3 emails endpoint.
        """
        payload = {
            "properties": {
                "hs_timestamp": str(timestamp_ms),
                "hs_email_subject": subject or "(no subject)",
                "hs_email_direction": "INBOUND",
                "hs_email_status": "RECEIVED",
                "hs_email_sender_email": sender_email,
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 198,  # email → contact
                        }
                    ],
                }
            ],
        }
        r = self._s.post(f"{self._BASE}/crm/v3/objects/emails", json=payload)
        if not r.ok:
            log.warning(
                "Timeline activity failed for contact %s: %s — %s",
                contact_id,
                r.status_code,
                r.text[:300],
            )
        return r.ok


# ── State file ────────────────────────────────────────────────────────────────

def load_processed_ids() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_processed_ids(ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ── Message processor ─────────────────────────────────────────────────────────

def process_message(
    service,
    hs: HubSpotClient,
    msg_id: str,
    add_timeline: bool,
    stats: dict,
) -> tuple[str, str, str | None]:
    """
    Process one Gmail message.

    Returns:
        (status, sender_email, hubspot_contact_id)
        status ∈ {"CREATO", "AGGIORNATO", "IGNORATO"}
    """
    try:
        headers = get_message_metadata(service, msg_id)
    except HttpError as exc:
        log.warning("Cannot fetch message %s: %s", msg_id, exc)
        stats["ignored"] += 1
        return "IGNORATO", "—", None

    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(no subject)")
    date_str = headers.get("Date", "")

    display_name, sender_email = parse_sender(from_header)

    if not sender_email or "@" not in sender_email:
        log.debug("Skipping %s — invalid sender '%s'", msg_id, from_header)
        stats["ignored"] += 1
        return "IGNORATO", from_header or "—", None

    domain = sender_email.split("@")[1]
    firstname, lastname = split_display_name(display_name)
    company = company_from_domain(domain)

    # ── HubSpot lookup ────────────────────────────────────────────────────────
    existing = hs.find_contact_by_email(sender_email)

    if existing:
        contact_id = existing["id"]
        cur = existing.get("properties", {})

        updates: dict = {}
        if not cur.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not cur.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not cur.get("company") and company:
            updates["company"] = company
        if not cur.get("hs_lead_source"):
            updates["hs_lead_source"] = "Gmail"

        if updates:
            hs.update_contact(contact_id, updates)
            status = "AGGIORNATO"
            stats["updated"] += 1
        else:
            status = "IGNORATO"
            stats["ignored"] += 1
    else:
        new_props: dict = {
            "email": sender_email,
            "hs_lead_source": "Gmail",
        }
        if firstname:
            new_props["firstname"] = firstname
        if lastname:
            new_props["lastname"] = lastname
        if company:
            new_props["company"] = company

        created = hs.create_contact(new_props)
        contact_id = created["id"]
        status = "CREATO"
        stats["created"] += 1

    # ── Optional timeline activity ────────────────────────────────────────────
    if add_timeline and status != "IGNORATO":
        try:
            ts_ms = int(
                parsedate_to_datetime(date_str)
                .astimezone(timezone.utc)
                .timestamp()
                * 1000
            )
        except Exception:
            ts_ms = int(time.time() * 1000)
        hs.log_email_activity(contact_id, subject, sender_email, ts_ms)

    return status, sender_email, contact_id


# ── Sync pass ─────────────────────────────────────────────────────────────────

_COL_W = 45  # column width for email address in output table


def one_pass(
    service,
    hs: HubSpotClient,
    processed: set[str],
    batch_size: int,
    add_timeline: bool,
) -> dict:
    stats: dict = {"created": 0, "updated": 0, "ignored": 0, "new_msgs": 0}
    page_token: str | None = None

    while True:
        messages, page_token = list_inbox_messages(service, batch_size, page_token)

        for m in messages:
            mid = m["id"]
            if mid in processed:
                continue

            stats["new_msgs"] += 1
            status, email, cid = process_message(service, hs, mid, add_timeline, stats)

            status_label = {"CREATO": "CREATO   ", "AGGIORNATO": "AGGIORNATO", "IGNORATO": "IGNORATO "}.get(
                status, status
            )
            print(
                f"  [{status_label}]  {email:<{_COL_W}}  HubSpot ID: {cid or '—'}"
            )
            processed.add(mid)

        if not page_token:
            break

    save_processed_ids(processed)
    return stats


# ── Entry point ───────────────────────────────────────────────────────────────

def run(
    credentials_file: str,
    token_file: str,
    hubspot_token: str,
    batch_size: int,
    add_timeline: bool,
    continuous: bool,
    interval: int,
) -> None:
    log.info("Connessione a Gmail…")
    service = build_gmail_service(credentials_file, token_file)
    hs = HubSpotClient(hubspot_token)
    processed = load_processed_ids()
    log.info("ID già processati in cache: %d", len(processed))

    total: dict = {"created": 0, "updated": 0, "ignored": 0}

    def _pass(label: str = "") -> None:
        if label:
            log.info("── Ciclo %s ──────────────────────────────────────────", label)
        stats = one_pass(service, hs, processed, batch_size, add_timeline)
        for k in ("created", "updated", "ignored"):
            total[k] += stats[k]
        log.info(
            "Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d"
            " (nuovi msg: %d)",
            stats["created"], stats["updated"], stats["ignored"], stats["new_msgs"],
        )

    if continuous:
        log.info(
            "Modalità continua attiva — intervallo %ds. Premi Ctrl+C per fermare.",
            interval,
        )
        cycle = 1
        while True:
            _pass(str(cycle))
            cycle += 1
            time.sleep(interval)
    else:
        _pass()
        print(
            f"\n{'─'*65}\n"
            f"  Riepilogo finale\n"
            f"  Creati:     {total['created']}\n"
            f"  Aggiornati: {total['updated']}\n"
            f"  Ignorati:   {total['ignored']}\n"
            f"{'─'*65}"
        )


def main() -> None:
    p = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail come contatti HubSpot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--credentials",
        default="credentials.json",
        help="File OAuth2 Gmail scaricato dalla Google Cloud Console (default: credentials.json)",
    )
    p.add_argument(
        "--token",
        default="token.json",
        help="File token Gmail (viene creato automaticamente al primo avvio)",
    )
    p.add_argument(
        "--hubspot-token",
        default=os.environ.get("HUBSPOT_TOKEN"),
        metavar="TOKEN",
        help="Token HubSpot Private App — o usa la variabile d'ambiente HUBSPOT_TOKEN",
    )
    p.add_argument(
        "--batch",
        type=int,
        default=50,
        metavar="N",
        help="Messaggi da leggere per ciclo (default: 50, max 500)",
    )
    p.add_argument(
        "--timeline",
        action="store_true",
        help="Aggiungi un'attività 'email ricevuta' sulla timeline HubSpot del contatto",
    )
    p.add_argument(
        "--continuous",
        action="store_true",
        help="Esegui in loop continuo anziché un singolo ciclo",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=300,
        metavar="SECONDI",
        help="Pausa tra un ciclo e l'altro in modalità continua (default: 300 s = 5 min)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Abilita log di debug",
    )
    args = p.parse_args()

    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if not args.hubspot_token:
        p.error(
            "Token HubSpot mancante.\n"
            "Usa --hubspot-token TOKEN  oppure  export HUBSPOT_TOKEN=pat-xx-..."
        )

    run(
        credentials_file=args.credentials,
        token_file=args.token,
        hubspot_token=args.hubspot_token,
        batch_size=min(args.batch, 500),
        add_timeline=args.timeline,
        continuous=args.continuous,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
