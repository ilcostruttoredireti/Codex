# Gmail → HubSpot Contact Sync

Continuously monitors Gmail inbox and upserts every new sender as a HubSpot contact, avoiding duplicates.

## What it does

For every inbound email:

1. **Extracts** the sender's email, name, and company domain.
2. **Checks HubSpot** for an existing contact with that email.
   - If it **exists** → fills in any missing fields (name, company, source).
   - If it **doesn't exist** → creates a new contact.
3. **Sets HubSpot fields**: Email, First name, Last name, Company (from domain), Lead Source = "Gmail", Lead Status = "NEW".
4. **Attaches a CRM note** tagging the contact as "Inbound Gmail".
5. **Skips** automated senders (`no-reply`, `noreply`, …) and configurable domains.

Output per email:

| Status | Email | HubSpot ID |
|--------|-------|-----------|
| CREATED | mario@example.com | 123456 |
| UPDATED | sara@acme.it | 789012 |
| IGNORED | newsletter@co.com | 345678 |

---

## Setup

### 1 — Gmail OAuth2 credentials

1. Open [Google Cloud Console](https://console.cloud.google.com).
2. Create a project, enable the **Gmail API**.
3. Create **OAuth 2.0 Desktop** credentials and download `credentials.json`.
4. Place `credentials.json` in this directory.

### 2 — HubSpot private app token

1. In HubSpot: **Settings → Integrations → Private Apps → Create**.
2. Grant scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.notes.write`.
3. Copy the token into `.env`.

### 3 — Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4 — Configure

```bash
cp .env.example .env
# Edit .env with your paths and tokens
```

---

## Run

### One-shot (sync last 24 h of inbox, then exit)

```bash
python gmail_hubspot_sync.py --once
```

### Continuous (poll every N seconds)

```bash
python gmail_hubspot_sync.py
```

### Custom Gmail query

```bash
python gmail_hubspot_sync.py --once --query "in:inbox newer_than:7d"
```

---

## Behaviour details

| Scenario | Action |
|----------|--------|
| New sender, never in HubSpot | Create contact with all fields + note |
| Sender already in HubSpot, complete | Ignored (no duplicate) |
| Sender already in HubSpot, missing fields | Update only the missing fields |
| Automated sender (`no-reply`, `noreply`, …) | Skipped silently |
| Sender domain in `SKIP_DOMAINS` | Skipped silently |

### Name inference

If the `From:` header has no display name, the name is inferred from the local part of the address:

- `mario.rossi@azienda.it` → First: **Mario**, Last: **Rossi**
- `info@azienda.it` → First: **Info**, Last: *(blank)*

### Company inference

The company name is derived from the sender's domain, stripping generic TLDs:

- `marcheteatro.it` → **Marcheteatro**
- `comune.ancona.it` → **Comune Ancona**
- `gmail.com` → *(blank — personal domain)*
