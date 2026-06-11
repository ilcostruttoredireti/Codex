# Gmail OAuth2 Setup

## 1. Google Cloud Console

1. Go to https://console.cloud.google.com/
2. Create a project (or select an existing one)
3. Enable the **Gmail API**: APIs & Services → Library → Gmail API → Enable
4. Create OAuth credentials: APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Download the JSON → save as `credentials.json` in the project root
5. Add your Gmail address to **Test users** (OAuth consent screen)

## 2. First run (token generation)

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in HUBSPOT_ACCESS_TOKEN
python gmail_hubspot_sync.py --once
```

A browser window will open for OAuth consent. After approval `token.json` is saved automatically.

## 3. HubSpot Private App token

1. HubSpot → Settings → Integrations → Private Apps → Create a private app
2. Scopes needed: `crm.objects.contacts.read`, `crm.objects.contacts.write`
3. Copy the access token into `.env` as `HUBSPOT_ACCESS_TOKEN`

## 4. Running continuously

```bash
# Single pass
python gmail_hubspot_sync.py --once

# Continuous polling every 2 minutes
python gmail_hubspot_sync.py --interval 120

# As a background service (Linux/Mac)
nohup python gmail_hubspot_sync.py --interval 60 > /dev/null 2>&1 &
```

## Output format

```
[CREATO]    new@domain.com      → ID: 12345678
[AGGIORNATO] known@domain.com   → ID: 87654321
[IGNORATO]  noreply@example.com → ID: None
```

Logs are also written to `gmail_hubspot_sync.log`.
