"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.
Avoids duplicates using email as unique key; updates existing records with missing fields.
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from email.utils import parseaddr


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ContactInfo:
    email: str
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    company: Optional[str] = None
    jobtitle: Optional[str] = None
    phone: Optional[str] = None
    domain: Optional[str] = None
    source: str = "Gmail"


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    changes: dict = field(default_factory=dict)


# ── Email parsing ──────────────────────────────────────────────────────────────

# Domains to skip (own accounts, automated senders, generic providers)
SKIP_DOMAINS = {
    "latestata.it",
    "facebookmail.com",
    "googlemail.com",
}

SKIP_EMAILS = {
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
    "redazione@latestata.it",
}

GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "libero.it", "hotmail.com", "outlook.com"}


def extract_domain(email: str) -> str:
    return email.split("@")[1].lower() if "@" in email else ""


def should_skip(email: str) -> bool:
    email = email.lower().strip()
    if email in SKIP_EMAILS:
        return True
    domain = extract_domain(email)
    return domain in SKIP_DOMAINS


def company_from_domain(domain: str) -> Optional[str]:
    """Derive a company name from a non-generic domain."""
    if domain in GENERIC_DOMAINS or not domain:
        return None
    # Strip TLD and capitalize: "theatrenvol.org" → "Theatrenvol"
    name = domain.split(".")[0].replace("-", " ").title()
    return name


def parse_sender(raw_from: str) -> ContactInfo:
    """Parse a raw From header into a ContactInfo."""
    display_name, email = parseaddr(raw_from)
    email = email.lower().strip()
    domain = extract_domain(email)

    firstname = lastname = None
    if display_name:
        parts = display_name.strip().split(None, 1)
        firstname = parts[0] if parts else None
        lastname = parts[1] if len(parts) > 1 else None

    company = company_from_domain(domain) if domain not in GENERIC_DOMAINS else None

    return ContactInfo(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
    )


# ── HubSpot logic (uses MCP tools in runtime) ─────────────────────────────────

def build_hubspot_properties(contact: ContactInfo, existing: Optional[dict] = None) -> dict:
    """Return only the HubSpot properties that are new or improved."""
    existing = existing or {}
    props = {}

    def needs_update(key: str, new_val) -> bool:
        return new_val and not existing.get(key)

    if needs_update("firstname", contact.firstname):
        props["firstname"] = contact.firstname
    if needs_update("lastname", contact.lastname):
        props["lastname"] = contact.lastname
    if needs_update("company", contact.company):
        props["company"] = contact.company
    if needs_update("jobtitle", contact.jobtitle):
        props["jobtitle"] = contact.jobtitle
    if needs_update("phone", contact.phone):
        props["phone"] = contact.phone

    # Always set lead source to Gmail on first sync
    if not existing.get("leadsource"):
        props["leadsource"] = "EMAIL_MARKETING"

    return props


# ── Main sync logic ────────────────────────────────────────────────────────────

def process_email_thread(thread: dict, mcp_gmail, mcp_hubspot) -> list[SyncResult]:
    """
    Process a single Gmail thread: extract senders, sync to HubSpot.
    In production this function is called by the MCP runtime loop.
    """
    results = []
    seen_emails: set[str] = set()

    for message in thread.get("messages", []):
        sender_raw = message.get("sender", "")
        _, email = parseaddr(sender_raw)
        email = email.lower().strip()

        if not email or email in seen_emails or should_skip(email):
            continue
        seen_emails.add(email)

        contact = parse_sender(sender_raw)

        # Search HubSpot for existing contact
        existing_records = mcp_hubspot.search_crm_objects(
            objectType="contacts",
            query=email,
            properties=["email", "firstname", "lastname", "company", "jobtitle",
                        "phone", "leadsource"],
        )

        if existing_records["total"] == 0:
            # Create new contact
            props = {
                "email": email,
                "leadsource": "EMAIL_MARKETING",
            }
            if contact.firstname:
                props["firstname"] = contact.firstname
            if contact.lastname:
                props["lastname"] = contact.lastname
            if contact.company:
                props["company"] = contact.company

            result = mcp_hubspot.manage_crm_objects(
                confirmationStatus="CONFIRMED",
                createRequest={"objects": [{"objectType": "contacts", "properties": props}]},
            )
            new_id = result["createResults"]["results"][0]["objectId"]
            results.append(SyncResult(email=email, hubspot_id=str(new_id),
                                      status="Creato", changes=props))

        else:
            # Update existing contact with missing fields
            existing = existing_records["results"][0]
            existing_props = existing["properties"]
            updates = build_hubspot_properties(contact, existing_props)

            if updates:
                mcp_hubspot.manage_crm_objects(
                    confirmationStatus="CONFIRMED",
                    updateRequest={"objects": [{
                        "objectType": "contacts",
                        "objectId": int(existing["id"]),
                        "properties": updates,
                    }]},
                )
                results.append(SyncResult(email=email, hubspot_id=existing["id"],
                                          status="Aggiornato", changes=updates))
            else:
                results.append(SyncResult(email=email, hubspot_id=existing["id"],
                                          status="Ignorato"))

    return results


def run_sync(mcp_gmail, mcp_hubspot, lookback: str = "1d") -> list[SyncResult]:
    """
    Entry point for the scheduled routine.
    Fetches recent inbox threads and syncs all senders to HubSpot.
    """
    threads = mcp_gmail.search_threads(
        query=f"in:inbox newer_than:{lookback} -from:me",
        pageSize=50,
    )
    all_results: list[SyncResult] = []
    for thread in threads.get("threads", []):
        all_results.extend(process_email_thread(thread, mcp_gmail, mcp_hubspot))
    return all_results
