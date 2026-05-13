# Gmail → HubSpot Contact Sync

Monitora continuamente la casella Gmail e sincronizza automaticamente i mittenti come contatti HubSpot, evitando duplicati e arricchendo i dati esistenti.

---

## Funzionalità

| Feature | Dettaglio |
|---|---|
| Monitoraggio Gmail | Polling configurabile (default 60 s) via query Gmail |
| Deduplicazione | Chiave unica = indirizzo email (case-insensitive) |
| Creazione contatto | Crea un nuovo contatto con Nome, Cognome, Azienda, Email |
| Aggiornamento contatto | Aggiorna solo i campi *vuoti* nel contatto esistente |
| Fonte contatto | Imposta `leadsource = "Gmail"` su ogni contatto |
| Timeline activity | Aggiunge una nota HubSpot con soggetto e data dell'email |
| Filtri anti-spam | Salta mittenti no-reply/bounce/postmaster |
| Stato per email | `Creato` / `Aggiornato` / `Ignorato` |
| Persistenza stato | Salva i messaggi già processati in `.gmail_sync_state.json` |

---

## Prerequisiti

- Python 3.11+
- Account Google con Gmail API abilitata
- HubSpot account con Private App token

---

## Setup

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Google Cloud — abilita Gmail API

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto (o usa uno esistente)
3. Abilita **Gmail API** in *APIs & Services → Library*
4. Crea credenziali **OAuth 2.0** → tipo *Desktop App*
5. Scarica il file JSON e salvalo come `credentials.json` nella root del progetto

### 3. HubSpot — crea Private App

1. HubSpot → *Settings → Integrations → Private Apps*
2. Crea nuova app con scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.schemas.contacts.read`
   - `engagements.read` + `engagements.write` (per timeline activity)
3. Copia il token generato

### 4. Configura `.env`

```bash
cp .env.example .env
```

Modifica `.env`:

```env
GOOGLE_CREDENTIALS_FILE=credentials.json
GOOGLE_TOKEN_FILE=token.json
HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
POLL_INTERVAL_SECONDS=60
GMAIL_QUERY=label:inbox is:unread
```

---

## Utilizzo

### Avvio continuo (loop infinito)

```bash
python main.py
```

Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail (OAuth2).  
Il token viene salvato in `token.json` per le esecuzioni successive.

### Esecuzione singola (cron / test)

```bash
python main.py --once
```

---

## Output esempio

```
======================================================================
  Gmail → HubSpot Contact Sync
  Query  : label:inbox is:unread
  Mode   : continuous
======================================================================
✅  [Creato    ]  mario.rossi@acme.com                      ID: 12345678
🔄  [Aggiornato]  giulia.bianchi@startup.it                 ID: 87654321
⏭️   [Ignorato  ]  newsletter@news.com                       ID: n/a  (All fields already populated)

--- Riepilogo ---
  Creati  : 1
  Aggiornati: 1
  Ignorati: 1
```

---

## Struttura file

```
.
├── main.py                   # Entry point + orchestratore
├── gmail_monitor.py          # Gmail polling + parsing mittenti
├── hubspot_sync.py           # HubSpot create/update/timeline
├── requirements.txt
├── .env.example
├── credentials.json          # (non committare - aggiunto a .gitignore)
├── token.json                # (generato automaticamente)
└── .gmail_sync_state.json    # (generato automaticamente - IDs già processati)
```

---

## Sicurezza

- `credentials.json` e `token.json` contengono dati sensibili — **non committarli mai**.
- Il file `.gitignore` li esclude automaticamente.
- Usa variabili d'ambiente o un secret manager in produzione.

---

## Cron (esempio Linux)

Per eseguire ogni 5 minuti con cron:

```cron
*/5 * * * * cd /path/to/project && python main.py --once >> sync.log 2>&1
```

---

## Troubleshooting

| Problema | Soluzione |
|---|---|
| `Token has been expired or revoked` | Elimina `token.json` e riavvia |
| `401 Unauthorized` HubSpot | Verifica che il Private App token sia valido e abbia gli scope corretti |
| Contatti non creati | Controlla il log con `LOG_LEVEL=DEBUG` |
| Nessun messaggio trovato | Verifica la `GMAIL_QUERY` con Gmail web (es. cerca `label:inbox is:unread`) |
