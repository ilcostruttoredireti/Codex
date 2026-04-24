# Gmail → HubSpot Contact Sync

Monitors your Gmail inbox and automatically syncs sender contacts into HubSpot, avoiding duplicates and filling in missing fields on existing records.

## How it works

```
Gmail inbox
    │
    ▼  (Google Gmail API – historyId polling)
gmail_monitor.py  ──►  new raw messages
    │
    ▼
contact_extractor.py  ──►  ContactInfo(email, first/last name, company, domain)
    │
    ▼
hubspot_sync.py
    ├── search contact by email
    ├── if NOT found  → create contact  (status: Creato)
    └── if found      → update blank fields  (status: Aggiornato)
         │
         └──► log Engagement note  (tag: "Inbound Gmail")
```

For each processed email the script prints:

| Column | Values |
|---|---|
| Stato | `Creato` / `Aggiornato` / `Ignorato` |
| Email | sender address |
| ID HubSpot | numeric contact ID |

## Setup

### 1. Google Cloud – Gmail API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services** → **Library**.
2. Enable **Gmail API**.
3. Go to **Credentials** → **Create Credentials** → **OAuth 2.0 Client ID** → **Desktop app**.
4. Download the JSON file and save it as `credentials.json` in the project root.

### 2. HubSpot – Private App token

1. HubSpot → **Settings** → **Integrations** → **Private Apps** → **Create a private app**.
2. Required scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.contacts.delete`
   - `timeline` (for activity notes – optional)
3. Copy the generated access token.

### 3. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in HUBSPOT_ACCESS_TOKEN
```

### 5. Run

```bash
python main.py
```

On the **first run** a browser window opens for Google OAuth consent. After approval a `token.json` file is saved locally – subsequent runs are headless.

#### Options

```
python main.py --interval 30        # poll every 30 s instead of 60
python main.py --no-activity        # skip HubSpot timeline notes
```

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point; CLI args; output loop |
| `gmail_monitor.py` | Gmail API auth + historyId-based polling |
| `contact_extractor.py` | Parse sender info from raw message |
| `hubspot_sync.py` | HubSpot search / create / update / note |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |
| `gmail_state.json` | *(auto-generated)* last historyId checkpoint |
| `token.json` | *(auto-generated)* Google OAuth token |

## Deduplication strategy

The sender's **email address** is used as the unique key when searching HubSpot (`/crm/v3/objects/contacts/search`).  
On updates, only **blank** fields are overwritten – existing richer data is never erased.
