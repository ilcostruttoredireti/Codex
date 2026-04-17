# Gmail → HubSpot Contact Sync – Setup Guide

## Prerequisites

- Python 3.11+
- A Google Cloud project with the Gmail API enabled
- A HubSpot Private App token

---

## 1 · Install dependencies

```bash
pip install -r requirements.txt
```

---

## 2 · Google OAuth2 credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Credentials**.
2. Create an **OAuth 2.0 Client ID** (Desktop app type).
3. Download the JSON file and save it as `credentials.json` in this directory.
4. Enable the **Gmail API** for the project.

---

## 3 · HubSpot Private App token

1. In HubSpot go to **Settings → Integrations → Private Apps** → Create app.
2. Grant the following scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copy the token.

---

## 4 · Environment variables

```bash
cp .env.example .env
# Edit .env and fill in HUBSPOT_ACCESS_TOKEN
```

---

## 5 · Run

```bash
# Load env vars, then start the sync loop
export $(grep -v '^#' .env | xargs)
python gmail_hubspot_sync.py
```

On **first run** a browser window opens for Gmail OAuth consent.  
The token is saved to `token.json` for subsequent runs.

---

## Output format

```
[Created ]  alice@acme.com                             HubSpot ID: 12345
[Updated ]  bob@corp.io                                HubSpot ID: 67890
[Ignored ]  charlie@example.com                        HubSpot ID: 11111
```

| Status    | Meaning                                                       |
|-----------|---------------------------------------------------------------|
| Created   | New contact created in HubSpot                                |
| Updated   | Existing contact updated with missing fields                  |
| Ignored   | Contact already complete; only timeline note added            |

---

## How it works

1. Polls Gmail INBOX every `POLL_INTERVAL_SECONDS` seconds.
2. For each new message, extracts sender `name`, `email`, and `domain`.
3. Searches HubSpot for a contact with the same email.
   - **Not found** → creates contact with `hs_lead_source = "Gmail"`.
   - **Found, incomplete** → fills in missing `firstname`, `lastname`, or `company`.
   - **Found, complete** → no property changes.
4. Adds a timeline note ("Inbound email received … Tag: Inbound Gmail") to the contact.
5. Saves processed message IDs to `.processed_messages.json` to avoid reprocessing.
