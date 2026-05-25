# Gmail → HubSpot Contact Sync

Monitora in modo continuo tutte le email in arrivo su **Gmail** e sincronizza automaticamente i mittenti come contatti nel **HubSpot CRM**, evitando duplicati e aggiornando i dati esistenti.

---

## 🗺️ Architettura

```
Gmail API  ──►  GmailClient       ──►  SyncEngine  ──►  HubSpotClient  ──►  HubSpot CRM
                (estrae contatti)       (orchestrazione)  (crea/aggiorna)
                                              │
                                    ProcessedMessageTracker
                                    (JSON state — evita ri-processamento)
```

### Moduli

| File | Responsabilità |
|------|----------------|
| `gmail_hubspot_sync/gmail_client.py` | Autenticazione OAuth2 Gmail, fetch messaggi, parsing header `From` |
| `gmail_hubspot_sync/hubspot_client.py` | Ricerca contatti HubSpot, creazione, aggiornamento, note timeline |
| `gmail_hubspot_sync/sync_engine.py` | Loop principale: coordina Gmail ↔ HubSpot, filtra indirizzi di sistema |
| `gmail_hubspot_sync/state.py` | Tracker persistente (JSON) dei messaggi già processati |
| `gmail_hubspot_sync/models.py` | `ContactInfo`, `SyncResult`, `SyncStatus` |
| `gmail_hubspot_sync/config.py` | Configurazione via env/`.env` |
| `main.py` | Entry point |

---

## ⚡ Funzionalità

- **Monitoraggio continuo** Gmail (polling configurabile, default 60s)
- **Parsing intelligente** header `From`: nome + email anche con encoding MIME
- **Deriving automatico** dell'azienda dal dominio (esclusi provider free)
- **Anti-duplicati**: usa l'email come chiave univoca in HubSpot
- **Aggiornamento selettivo**: sovrascrive solo i campi vuoti in HubSpot
- **Filtro indirizzi di sistema**: `noreply@`, `mailer-daemon@`, ecc. vengono ignorati
- **Timeline activity**: nota HubSpot "Inbound Gmail" associata ad ogni contatto creato
- **State persistente**: `processed_messages.json` evita ri-processamento al riavvio
- **Output chiaro** per ogni email processata:
  ```
  ✅ Stato: Creato     | Email: mario@acme.com | ID HubSpot: 123456 | Oggetto: «Preventivo»
  🔄 Stato: Aggiornato | Email: luca@corp.it   | ID HubSpot: 789012
  ⏭️ Stato: Ignorato   | Email: noreply@shop.com
  ```

---

## 🚀 Setup

### 1. Dipendenze Python

```bash
pip install -r requirements.txt
```

### 2. Credenziali Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto → abilita **Gmail API**
3. Credenziali → **OAuth 2.0 Client ID** (tipo: Desktop App)
4. Scarica il JSON e salvalo come `credentials.json` nella root del progetto
5. Al primo avvio si aprirà il browser per autorizzare l'app → verrà creato automaticamente `token.json`

### 3. HubSpot Private App Token

1. Vai su [HubSpot → Impostazioni → Integrazioni → App private](https://app.hubspot.com/private-apps)
2. Crea una nuova app con gli scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia il token generato

### 4. Configurazione `.env`

```bash
cp .env.example .env
# Modifica .env con i tuoi valori
```

```env
HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
GMAIL_CREDENTIALS_FILE=credentials.json
GMAIL_POLL_INTERVAL=60
GMAIL_LABEL=INBOX
LOG_LEVEL=INFO
```

---

## ▶️ Esecuzione

```bash
# Loop continuo (default)
python main.py

# Esecuzione singola (utile con cron)
RUN_ONCE=true python main.py
```

### Esempio output

```
2026-05-25 10:00:01 [INFO] __main__ — Avvio Gmail → HubSpot Sync
2026-05-25 10:00:02 [INFO] gmail_hubspot_sync.sync_engine — Gmail→HubSpot Sync avviato | intervallo: 60s | label: INBOX
2026-05-25 10:00:03 [INFO] gmail_hubspot_sync.sync_engine — === Ciclo sync: 3 nuovi messaggi ===
✅ Stato: Creato     | Email: mario@acme.com    | ID HubSpot: 123456 | Oggetto: «Richiesta info»
🔄 Stato: Aggiornato | Email: giulia@studio.it  | ID HubSpot: 234567 | Oggetto: «Re: Proposta»
⏭️ Stato: Ignorato   | Email: noreply@amazon.it
```

---

## 🔧 Variabili d'ambiente

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `HUBSPOT_ACCESS_TOKEN` | — | **Obbligatorio.** Token HubSpot Private App |
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | File OAuth2 Google Cloud |
| `GMAIL_TOKEN_FILE` | `token.json` | Token salvato dopo il primo login |
| `GMAIL_LABEL` | `INBOX` | Label Gmail da monitorare |
| `GMAIL_POLL_INTERVAL` | `60` | Secondi tra un ciclo e l'altro |
| `GMAIL_MAX_RESULTS` | `50` | Max email per ciclo |
| `STATE_FILE` | `processed_messages.json` | File di stato locale |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `RUN_ONCE` | `false` | `true` = esegui un ciclo ed esci |

---

## 🧪 Test

```bash
pytest tests/ -v
# 31 passed
```

---

## 📋 Campi HubSpot compilati

| Campo HubSpot | Fonte |
|--------------|-------|
| `email` | Header `From` |
| `firstname` | Nome dal display name |
| `lastname` | Cognome dal display name |
| `company` | Dominio email (se non provider free) |
| `leadsource` | Fisso: `"Gmail"` |
| Nota timeline | Tag `"Inbound Gmail"` + testo email |

> I campi vengono aggiornati **solo se vuoti** nel contatto HubSpot esistente.

---

## 🚦 Logica anti-duplicati

```
Email in arrivo
     │
     ▼
Già in processed_messages.json? ──YES──► skip
     │NO
     ▼
find_contact_by_email(email) in HubSpot
     │
     ├─ Non trovato ──► CREATE contact
     │
     └─ Trovato ──► PATCH solo i campi vuoti
                         │
                         └─ Nessun campo da aggiornare ──► IGNORED
```
