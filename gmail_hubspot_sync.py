#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and automatically creates or updates
HubSpot contacts from sender data. Uses email as the unique dedup key.

Setup:
    pip install -r requirements.txt
    cp .env.example .env
    # Fill in HUBSPOT_ACCESS_TOKEN and configure Gmail OAuth2 credentials
    python gmail_hubspot_sync.py
"""

import json
import os
import time
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from hubspot.crm.contacts import SimplePublicObjectInput

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json")

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
CONTACT_SOURCE = "EMAIL_MARKETING"   # hs_analytics_source enum value
CONTACT_SOURCE_LABEL = "Gmail"       # used in note body

STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))

# Local-part prefixes that identify automated/system senders – skip these
SKIP_PREFIXES = {
    "no-reply", "noreply", "do-not-reply", "donotreply",
    "notifications", "notification", "mailer", "bounce",
    "postmaster", "daemon", "auto-reply", "autoreply",
    "newsletter", "unsubscribe", "admin", "webmaster",
    "info",          # generic info@ addresses
    "support",       # support@ addresses (often automated)
    "contact",       # contact@ (often forms / marketing)
    "help",
    "hello",
    "team",
    "ship",
    "news",
    "updates",
    "alert",
    "alerts",
    "billing",
    "invoice",
    "invoices",
    "receipts",
    "receipt",
    "orders",
    "order",
    "security",
    "privacy",
    "legal",
    "abuse",
    "spam",
    "sales",         # generic sales@ addresses
    "marketing",
    "redazione",     # editorial address (Italian)
    "sellersupport",
}

# Domain → company pretty name
DOMAIN_COMPANY_MAP: dict[str, str] = {
    "smashballoon.com": "Smash Balloon",
    "themeisle.com": "ThemeIsle",
    "startupitalia.eu": "Startup Italia",
    "martes-ai.com": "Martes AI",
    "mircogasparotto.com": "Mirco Gasparotto",
    "vercel.com": "Vercel",
    "tiktok.com": "TikTok",
    "kaggle.com": "Kaggle",
    "youtube.com": "YouTube",
    "circle.so": "Circle",
    "github.com": "GitHub",
    "stripe.com": "Stripe",
    "notion.so": "Notion",
    "hubspot.com": "HubSpot",
    "google.com": "Google",
    "microsoft.com": "Microsoft",
    "amazon.com": "Amazon",
    "linkedin.com": "LinkedIn",
    "twitter.com": "Twitter",
    "facebook.com": "Meta",
    "apple.com": "Apple",
}

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": [], "last_run": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
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


def fetch_inbox_threads(service, max_results: int = 50, page_token: str | None = None):
    kwargs = {"userId": "me", "labelIds": ["INBOX"], "maxResults": max_results}
    if page_token:
        kwargs["pageToken"] = page_token
    return service.users().threads().list(**kwargs).execute()


def get_thread_sender(service, thread_id: str) -> dict | None:
    """Return sender metadata from the first message in a thread."""
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="metadata",
             metadataHeaders=["From", "Subject", "Date"])
        .execute()
    )
    messages = thread.get("messages", [])
    if not messages:
        return None
    headers = {h["name"]: h["value"]
               for h in messages[0].get("payload", {}).get("headers", [])}
    return {
        "thread_id": thread_id,
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
    }


# ── Contact parsing ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> dict | None:
    """
    Parse a 'From' header into contact fields.
    Returns None for automated/system senders.
    """
    display_name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None

    email = email.strip().lower()
    local = email.split("@")[0]
    domain = email.split("@")[1]

    # Skip automated senders
    clean_local = local.split("+")[0]  # strip Gmail-style tags
    if clean_local in SKIP_PREFIXES or any(
        clean_local.startswith(p + "-") for p in SKIP_PREFIXES
    ):
        return None

    root = _root_domain(domain)
    company = DOMAIN_COMPANY_MAP.get(root) or DOMAIN_COMPANY_MAP.get(domain) or _prettify_domain(root)
    firstname, lastname = _split_name(display_name, local)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def _root_domain(domain: str) -> str:
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _prettify_domain(domain: str) -> str:
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def _split_name(display: str, local: str) -> tuple[str, str]:
    display = display.strip()
    if not display or display == local or "@" in display:
        return local.title(), ""
    parts = display.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client, email: str) -> dict | None:
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company",
                    "hs_analytics_source", "hs_lead_status"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0].to_dict() if resp.results else None


def _missing_props(existing: dict, candidate: dict) -> dict:
    """Return only fields from candidate that are absent or empty in existing."""
    props = existing.get("properties", {})
    return {k: v for k, v in candidate.items() if v and not props.get(k)}


def _add_note(client, contact_id: str, body: str) -> None:
    """Create a NOTE engagement on a contact."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    note_props = {"hs_note_body": body, "hs_timestamp": now_ms}
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
    note_obj = NoteInput(
        properties=note_props,
        associations=[{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED",
                       "associationTypeId": 202}],  # Note → Contact
        }],
    )
    client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=note_obj
    )


def create_contact(client, contact: dict) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status, contact_id)."""
    props = {k: v for k, v in {
        "email": contact["email"],
        "firstname": contact.get("firstname", ""),
        "lastname": contact.get("lastname", ""),
        "company": contact.get("company", ""),
        "hs_analytics_source": CONTACT_SOURCE,
        "hs_lead_status": "NEW",
    }.items() if v}

    obj = SimplePublicObjectInputForCreate(properties=props)
    result = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=obj
    )
    contact_id = result.id

    try:
        _add_note(client, contact_id,
                  f"Contatto creato automaticamente da Gmail.\nFonte: {CONTACT_SOURCE_LABEL}\nTag: Inbound Gmail")
    except Exception as exc:
        log.warning("Nota non creata per %s: %s", contact["email"], exc)

    return "CREATO", contact_id


def update_contact(client, contact_id: str, updates: dict) -> tuple[str, str]:
    """Update missing fields on an existing contact."""
    obj = SimplePublicObjectInput(properties=updates)
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=obj,
    )
    return "AGGIORNATO", contact_id


# ── Main sync loop ────────────────────────────────────────────────────────────

def sync_once(gmail_service, hs_client, state: dict) -> list[dict]:
    processed_ids: set = set(state.get("processed_ids", []))
    results = []

    resp = fetch_inbox_threads(gmail_service, max_results=50)
    threads = resp.get("threads", [])

    for thread in threads:
        thread_id = thread["id"]
        if thread_id in processed_ids:
            continue

        meta = get_thread_sender(gmail_service, thread_id)
        if not meta:
            processed_ids.add(thread_id)
            continue

        contact = parse_sender(meta["from"])
        if not contact:
            log.info("IGNORATO  %s (mittente automatico)", meta["from"])
            results.append({"stato": "IGNORATO", "email": meta["from"], "id": None})
            processed_ids.add(thread_id)
            continue

        email = contact["email"]
        try:
            existing = find_contact_by_email(hs_client, email)
            if existing:
                contact_id = existing["id"]
                missing = _missing_props(existing, {
                    "firstname": contact.get("firstname"),
                    "lastname": contact.get("lastname"),
                    "company": contact.get("company"),
                    "hs_lead_status": "NEW",
                })
                if missing:
                    stato, cid = update_contact(hs_client, contact_id, missing)
                else:
                    stato, cid = "IGNORATO (già completo)", contact_id
            else:
                stato, cid = create_contact(hs_client, contact)

            log.info("%-35s  %-40s  %s", stato, email, cid)
            results.append({"stato": stato, "email": email, "id": cid})

        except ApiException as exc:
            log.error("Errore HubSpot per %s: %s", email, exc)
            results.append({"stato": "ERRORE", "email": email, "id": None})

        processed_ids.add(thread_id)
        time.sleep(0.2)  # respect rate limits

    state["processed_ids"] = list(processed_ids)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    return results


def main() -> None:
    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN non configurato. Vedi .env.example")

    state = load_state()
    gmail_service = get_gmail_service()
    hs_client = get_hubspot_client()

    log.info("Avvio sync Gmail → HubSpot")
    results = sync_once(gmail_service, hs_client, state)
    save_state(state)

    print("\n── Riepilogo ─────────────────────────────────────────────────────────────")
    print(f"{'Stato':<35}  {'Email contatto':<40}  {'ID HubSpot'}")
    print("─" * 90)
    for r in results:
        print(f"{r['stato']:<35}  {r['email']:<40}  {r['id'] or '—'}")
    print(f"\nTotale: {len(results)} email elaborate")


if __name__ == "__main__":
    main()
