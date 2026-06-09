# Setup Gmail OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable **Gmail API**
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download `credentials.json` and place it next to `sync.py`
5. First run will open a browser for authorization → saves `token.json`

## Scopes required
- `https://www.googleapis.com/auth/gmail.readonly`

## HubSpot Private App
1. HubSpot → Settings → Integrations → Private Apps
2. Create app with scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.notes.write`
3. Copy the token to `HUBSPOT_API_KEY` in your `.env`
