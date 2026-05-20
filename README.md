# Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and automatically syncs sender contacts to HubSpot CRM. Avoids duplicates by using the email address as the unique key.

## What it does

For every new inbox message the script:

1. Extracts sender data — email, name, company domain
2. Looks up the contact in HubSpot by email
3. **Creates** the contact if it does not exist
4. **Updates** only blank fields if the contact already exists
5. Sets `Lead Source = Gmail` on every contact it touches
6. Prints a status line per message: **Creato / Aggiornato / Ignorato**

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Gmail OAuth2 credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → enable **Gmail API**
3. Create an **OAuth 2.0 Client ID** (type: *Desktop app*)
4. Download `credentials.json` and place it in this folder

### 3. HubSpot Private App token

1. In HubSpot go to **Settings → Integrations → Private Apps**
2. Create an app with scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
3. Copy the token

### 4. Environment variables

```bash
cp .env.example .env
# edit .env and fill in HUBSPOT_ACCESS_TOKEN
```

### 5. First run (OAuth consent)

```bash
python gmail_hubspot_sync.py
```

A browser window will open for Google sign-in. After authorising, a `token.json` is saved locally and reused on subsequent runs.

## Usage

```bash
# Single run (processes all inbox messages not yet seen)
python gmail_hubspot_sync.py

# Continuous daemon mode (polls every 5 minutes)
python gmail_hubspot_sync.py --daemon

# Custom polling interval (seconds)
python gmail_hubspot_sync.py --daemon --interval 120
```

## Output example

```
2025-05-20 10:00:01 INFO     Messaggi trovati: 12
2025-05-20 10:00:02 INFO     [Creato    ] mario.rossi@acme.com          ID HubSpot: 12301
2025-05-20 10:00:03 INFO     [Aggiornato] laura.bianchi@startup.io      ID HubSpot: 8872
2025-05-20 10:00:03 INFO     [Ignorato  ] info@gmail.com                  (nessun campo da aggiornare)
2025-05-20 10:00:04 INFO     Sync completato — Creati: 1, Aggiornati: 1, Ignorati: 10, Errori: 0

Stato        Email mittente                                ID HubSpot
───────────────────────────────────────────────────────────────────────────
Creato       mario.rossi@acme.com                         12301
Aggiornato   laura.bianchi@startup.io                     8872
```

## State file

`.sync_state.json` tracks the Unix timestamp of the last successful run and the set of already-processed Gmail message IDs. Delete it to reprocess all messages from scratch.

## Running as a system service (Linux)

Create `/etc/systemd/system/gmail-hubspot-sync.service`:

```ini
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/this/folder
EnvironmentFile=/path/to/this/folder/.env
ExecStart=/path/to/.venv/bin/python gmail_hubspot_sync.py --daemon --interval 300
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gmail-hubspot-sync
```
