"""
Gmail → HubSpot Contact Sync
Monitors inbound Gmail threads and syncs sender contacts to HubSpot.
Runs as a scheduled routine (Claude Code on the web).

Logic:
  1. Fetch inbox threads newer than N days via Gmail MCP
  2. Filter real business senders (skip noreply/automated addresses)
  3. For each unique sender email:
     - If contact exists in HubSpot → update any missing fields
     - If contact does not exist  → create new contact
  4. Tag source as "Gmail Inbound"
"""

import re

# ---------------------------------------------------------------------------
# Sender filtering
# ---------------------------------------------------------------------------

SKIP_PREFIXES = (
    "noreply", "no-reply", "dont-reply", "notifications-noreply",
    "notify-noreply", "sc-noreply", "ads-noreply", "adsense-noreply",
    "updates-noreply", "mailer-daemon", "postmaster", "nobody",
    "system", "bounce", "confirm",
)

SKIP_DOMAINS = (
    "linkedin.com", "google.com", "skool.com", "feedspot.com",
    "notification.circle.so", "mailchimp.com",
)


def is_automated_sender(email: str) -> bool:
    """Return True if the sender address looks automated / system-generated."""
    email = email.lower()
    local, _, domain = email.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if local == prefix or local.startswith(prefix + "-") or local.startswith(prefix + "."):
            return True
    return False


# ---------------------------------------------------------------------------
# Contact parsing helpers
# ---------------------------------------------------------------------------

def extract_firstname(local: str) -> str | None:
    """
    Best-effort first name extraction from the local part of an email address.
    Returns None when the local part is a role address (info, support, hello, …).
    """
    role_words = {
        "info", "support", "hello", "general", "contact", "mail",
        "commerciale", "admin", "team", "news", "sales", "press",
        "media", "marketing", "service", "office", "mailer", "daily",
        "briefing", "mondostudi", "ufficiostampa",
    }
    name = local.split("@")[0].split(".")[0].split("+")[0]
    if name.lower() in role_words:
        return None
    return name.capitalize()


def extract_company_from_domain(domain: str) -> str:
    """
    Derive a human-readable company name from an email domain.
    Strips common subdomains and TLD suffixes.

    Examples:
      support.publer.com  → Publer
      ag.miraconsulting.it → Miraconsulting
      martes-ai.com       → Martes AI
    """
    parts = domain.split(".")
    # strip common subdomains
    while parts and parts[0] in ("support", "mail", "ag", "mnt", "newsletter", "notifications"):
        parts.pop(0)
    # strip TLD(s)
    company_slug = parts[0] if parts else domain
    # convert kebab-case to title
    return " ".join(w.capitalize() for w in re.split(r"[-_]", company_slug))


def build_contact_properties(sender_email: str) -> dict:
    """
    Build the HubSpot contact property dict for a given sender email.
    """
    local, _, domain = sender_email.partition("@")
    firstname = extract_firstname(local)
    company   = extract_company_from_domain(domain)

    props = {
        "email":           sender_email,
        "company":         company,
        "hs_lead_source":  "OTHER_CAMPAIGNS",   # closest standard value
    }
    if firstname:
        props["firstname"] = firstname

    return props


# ---------------------------------------------------------------------------
# Main sync routine  (pseudo-code — actual calls go through MCP tools)
# ---------------------------------------------------------------------------

def sync_gmail_to_hubspot(threads: list[dict]) -> list[dict]:
    """
    Process a list of Gmail thread objects (as returned by search_threads).
    Returns a result list with one entry per unique real sender.

    Each result dict:
      {
        "status":   "Creato" | "Aggiornato" | "Ignorato",
        "email":    str,
        "hubspot_id": int | None,
      }
    """
    seen_emails: set[str] = set()
    results: list[dict] = []

    for thread in threads:
        for msg in thread.get("messages", []):
            sender = msg.get("sender", "").lower().strip()
            if not sender or sender in seen_emails:
                continue
            seen_emails.add(sender)

            if is_automated_sender(sender):
                results.append({"status": "Ignorato", "email": sender, "hubspot_id": None})
                continue

            props = build_contact_properties(sender)

            # --- HubSpot lookup (via MCP tool search_crm_objects) ---
            # existing = hubspot_search_by_email(sender)
            #
            # if existing:
            #     update_missing_fields(existing["id"], props)
            #     results.append({"status": "Aggiornato", "email": sender, "hubspot_id": existing["id"]})
            # else:
            #     new_id = hubspot_create_contact(props)
            #     results.append({"status": "Creato", "email": sender, "hubspot_id": new_id})

    return results


# ---------------------------------------------------------------------------
# Deduplication key: email address (lower-cased)
# ---------------------------------------------------------------------------
DEDUP_KEY = "email"
