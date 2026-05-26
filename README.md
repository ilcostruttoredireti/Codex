# Gmail → HubSpot Contact Sync

Monitora le email in arrivo su Gmail, estrae i mittenti e li sincronizza automaticamente come contatti in HubSpot, evitando duplicati e aggiornando i dati esistenti.

## Funzionalità

| Funzione | Dettaglio |
|---|---|
| 📥 Monitoraggio Gmail | Polling continuo della casella di posta |
| 🔍 Estrazione mittenti | Gestisce mittenti diretti e email inoltrate (Fw:) |
| 🔄 Sync HubSpot | Crea nuovi contatti o aggiorna quelli esistenti |
| 🚫 Deduplicazione | Usa l'email come chiave univoca |
| 🏷️ Tagging | Imposta `leadsource = Gmail` e `hs_lead_status = Inbound Gmail` |
| 📊 Report | Stampa stato per ogni email processata |

## Requisiti

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client hubspot-api-client
```

## Configurazione

### 1. Google / Gmail
Abilita la **Gmail API** nel [Google Cloud Console](https://console.cloud.google.com/):
1. Crea un progetto → API & Services → Abilita `Gmail API`
2. Configura credenziali OAuth 2.0 (Desktop app) oppure Service Account con **Domain-Wide Delegation**
3. Autentica:
   ```bash
   gcloud auth application-default login
   # oppure usa un file service account:
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
   ```

### 2. HubSpot
Crea un **Private App Token** in HubSpot:
1. Settings → Integrations → Private Apps → Crea app
2. Scopes necessari: `crm.objects.contacts.read`, `crm.objects.contacts.write`
3. Copia il token generato

## Utilizzo

```bash
# Modalità loop continuo (ogni 60 secondi)
export HUBSPOT_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
python gmail_hubspot_sync.py

# Intervallo personalizzato (es. ogni 5 minuti)
python gmail_hubspot_sync.py --interval 300

# Esecuzione singola (test / cron job)
python gmail_hubspot_sync.py --once

# Token passato come argomento
python gmail_hubspot_sync.py --token "pat-na1-..." --once
```

## Output di esempio

```
══════════════════════════════════════════════════════════════════════
  REPORT SINCRONIZZAZIONE — 2026-05-26 17:30:00
══════════════════════════════════════════════════════════════════════
  STATO                EMAIL                                    ID HUBSPOT
──────────────────────────────────────────────────────────────────────
  ✅ Creato            a.pappalardo@cidim.it                    123456789
  🔄 Aggiornato        l.bramanti@nextpress.it                  779740647624
  ⏭  Ignorato         noreply@example.com                      —
══════════════════════════════════════════════════════════════════════
```

## Campi HubSpot sincronizzati

| Campo HubSpot | Fonte |
|---|---|
| `email` | Mittente email |
| `firstname` | Nome dal display name |
| `lastname` | Cognome dal display name |
| `company` | Dal display name o dominio email |
| `leadsource` | `"Gmail"` (fisso) |
| `hs_lead_status` | `"Inbound Gmail"` (fisso) |

## Architettura

```
Gmail Inbox
    │
    ▼ (Gmail API - History / Search)
[fetch_new_messages()]
    │
    ▼
[extract_contacts_from_message()]
    │  • mittente diretto
    │  • parsing email inoltrate (pattern "Da ...")
    ▼
[should_skip()] ── ignora no-reply, domini blacklist
    │
    ▼
[search_contact()] ─── HubSpot CRM Search API
    │
    ├── esistente? ──► [update_contact()] → aggiorna campi mancanti
    │
    └── nuovo?     ──► [create_contact()] → crea con tutti i campi
```

## Esecuzione automatica (systemd / cron)

### cron (ogni 5 minuti)
```cron
*/5 * * * * HUBSPOT_TOKEN=pat-... /usr/bin/python3 /path/to/gmail_hubspot_sync.py --once >> /var/log/gmail_hs_sync.log 2>&1
```

### systemd service
```ini
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/gmail_hubspot_sync.py
Environment=HUBSPOT_TOKEN=pat-na1-...
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
