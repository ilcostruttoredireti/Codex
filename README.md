# Gmail → HubSpot Contact Sync

Monitora la casella Gmail in arrivo e sincronizza automaticamente i mittenti come contatti HubSpot.

## Cosa fa

Per ogni email ricevuta nella inbox:

| Caso | Azione |
|------|--------|
| Contatto **non esiste** in HubSpot | Crea nuovo contatto con fonte "Gmail" |
| Contatto **già esistente** | Aggiorna i campi vuoti (nome, cognome, azienda) |
| Mittente automatico/noreply | Salta senza elaborare |

Campi compilati in HubSpot:
- **Email** — chiave univoca
- **Nome / Cognome** — dall'header `From`
- **Azienda** — ricavata dal dominio email (es. `acme.com` → "Acme")
- **Fonte contatto** — impostata a "Gmail"
- **Lifecycle stage** — "lead"
- **Nota nella timeline** — oggetto email, data, tag `Inbound Gmail`

---

## Prerequisiti

- Python 3.11+
- Account Google con Gmail
- Account HubSpot con permessi CRM

---

## Setup rapido

### 1 — Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2 — Configura HubSpot

1. Vai in **HubSpot → Impostazioni → Integrazioni → App Private**
2. Crea una nuova app privata con questi scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia il token generato

```bash
cp .env.example .env
# Modifica .env e incolla il token:
# HUBSPOT_ACCESS_TOKEN=pat-eu1-...
```

### 3 — Configura Gmail OAuth

1. Vai su [Google Cloud Console](https://console.cloud.google.com)
2. Crea/seleziona un progetto
3. Abilita **Gmail API**: API & Services → Library → Gmail API → Enable
4. Crea credenziali: Credentials → Create → OAuth 2.0 Client ID → **Desktop App**
5. Scarica il JSON e rinominalo `credentials.json` nella stessa directory

```bash
python setup_gmail_oauth.py
# → Si apre il browser → autorizza l'accesso → token salvato in gmail_token.json
```

---

## Utilizzo

```bash
# Elaborazione singola (controlla le ultime 100 email)
python gmail_hubspot_sync.py

# Monitoraggio continuo (ogni 60 secondi)
python gmail_hubspot_sync.py --watch

# Monitoraggio ogni 5 minuti, controlla le ultime 200 email
python gmail_hubspot_sync.py --watch --interval 300 --max-emails 200

# Resetta lo stato (rielabora tutto da capo)
python gmail_hubspot_sync.py --reset-state
```

---

## Output di esempio

```
2026-06-05 10:23:01 [INFO] Gmail pronto.
2026-06-05 10:23:01 [INFO] Elaborazione di 5 nuovi messaggi...
✅  mario.rossi@acme.com          status=Creato                   hs_id=12345678
🔄  giulia.bianchi@example.it     status=Aggiornato               hs_id=87654321
⏭️  noreply@mailchimp.com         status=Ignorato (automatico)    hs_id=-

══════════════════════════════════════════════════════════════════
  RIEPILOGO SINCRONIZZAZIONE  —  2026-06-05 10:23:05
  ─────────────────────────────────────────────────────────────
  Email elaborate : 5
  ✅ Creati       : 1
  🔄 Aggiornati   : 1
  ⏭️  Ignorati    : 3
  ❌ Errori       : 0
══════════════════════════════════════════════════════════════════

  STATUS       EMAIL                                  HUBSPOT ID
  ───────────────────────────────────────────────────────────────
  Creato       mario.rossi@acme.com                   12345678
  Aggiornato   giulia.bianchi@example.it              87654321
```

---

## File generati automaticamente

| File | Descrizione |
|------|-------------|
| `gmail_token.json` | Token OAuth Gmail (non committare) |
| `.sync_state.json` | IDs messaggi già elaborati |
| `.env` | Variabili d'ambiente (non committare) |

Aggiungi `.env`, `gmail_token.json` e `.sync_state.json` al tuo `.gitignore`.

---

## Architettura

```
gmail_hubspot_sync.py
├── get_gmail_service()          # OAuth2 Gmail
├── fetch_inbox_message_ids()    # Lista ID messaggi inbox
├── get_message_metadata()       # Header From/Subject/Date
├── parse_sender()               # Estrae nome + email dal From
├── company_from_domain()        # Dominio → nome azienda
├── is_skippable()               # Filtra noreply/automatici
├── hs_search_contact()          # Cerca contatto per email
├── hs_create_contact()          # Crea nuovo contatto
├── hs_update_contact()          # Aggiorna campi mancanti
├── hs_create_note()             # Aggiunge nota timeline
├── process_message()            # Logica per singolo messaggio
└── sync_once()                  # Elabora tutti i nuovi messaggi
```
