# Gmail → HubSpot Contact Sync

Monitors the Gmail inbox continuously. For every new inbound email it:

1. Extracts the sender's **email, name, and company** (inferred from domain).
2. Searches HubSpot for an existing contact with that email address.
3. **Creates** the contact if it doesn't exist, or **updates** only empty fields if it does.
4. Adds an *Inbound Gmail* note to the contact's timeline.
5. Logs the result — `Created`, `Updated`, or `Ignored` — with the HubSpot contact ID.

---

## Quick start

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `HUBSPOT_TOKEN` | HubSpot Private App token (requires `crm.objects.contacts.read/write` and `crm.objects.notes.write` scopes) |
| `CREDENTIALS_FILE` | Path to `credentials.json` from Google Cloud Console |
| `TOKEN_FILE` | Where the OAuth token is cached (created automatically) |
| `POLL_INTERVAL` | Seconds between Gmail polls (default `60`) |
| `STATE_FILE` | Persistence file for Gmail historyId (default `sync_state.json`) |

### 3. Set up Google credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Enable **Gmail API**.
2. Create an **OAuth 2.0 Client ID** (Application type: *Desktop app*).
3. Download `credentials.json` and place it next to `main.py`.

### 4. Set up HubSpot

1. Go to HubSpot → Settings → Integrations → **Private Apps** → Create a private app.
2. Grant scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.notes.write`.
3. Copy the token into `HUBSPOT_TOKEN`.

### 5. Run

```bash
source .env   # or: export $(cat .env | xargs)
python main.py
```

On first launch a browser window opens for Gmail OAuth consent. After authorising,
`token.json` is saved and the sync loop starts.

---

## Output example

```
2026-05-11 10:00:00  INFO     Gmail → HubSpot contact sync starting (poll interval: 60s)
2026-05-11 10:00:01  INFO     First run – anchored at historyId 12345. Monitoring for new mail…
2026-05-11 10:01:00  INFO     Found 2 new inbox message(s).
2026-05-11 10:01:01  INFO     [Created ]  mario.rossi@acme.com                      HubSpot ID: 12301
2026-05-11 10:01:02  INFO     [Ignored ]  newsletter@gmail.com                      HubSpot ID: 11900
2026-05-11 10:02:00  INFO     Found 1 new inbox message(s).
2026-05-11 10:02:01  INFO     [Updated ]  mario.rossi@acme.com                      HubSpot ID: 12301
```

---

## How it works

| Step | Detail |
|---|---|
| **Polling** | Uses the Gmail [History API](https://developers.google.com/gmail/api/reference/rest/v1/users.history/list) to receive only deltas since the last run. No messages are re-processed. |
| **First run** | Anchors to the current `historyId`; historical emails are **not** imported. |
| **Deduplication** | Email address is the unique key. A processed-message cache (capped at 2 000 entries) prevents double-processing within a session. |
| **Contact fields** | `email`, `firstname`, `lastname`, `company` (from domain, skipping free providers), `leadsource = Gmail`. |
| **Update strategy** | Only **empty** fields are overwritten — existing data is never clobbered. |
| **Timeline note** | An `Inbound Gmail` note is attached to every contact after create/update. |
