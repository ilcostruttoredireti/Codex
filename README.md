# Gmail → HubSpot Contact Sync

Monitora la casella Gmail in entrata ed esegue l'upsert automatico dei mittenti
come contatti in HubSpot, aggiungendo una nota di timeline per ogni email ricevuta.

## Funzionamento

```
Gmail INBOX
    │
    ▼  (Gmail History API – polling)
 contact_extractor.py
    │  estrae: email, nome, cognome, azienda dal dominio
    ▼
 hubspot_client.py
    ├─ cerca per email
    ├─ se esiste  → aggiorna campi mancanti  [Aggiornato]
    ├─ se assente → crea nuovo contatto      [Creato]
    └─ nessuna modifica necessaria           [Ignorato]
    │
    └─ aggiunge nota timeline  ("Email in entrata da ...")
```

**Deduplicazione**: l'email del mittente è la chiave univoca.
**Fonte contatto**: il campo `lead_source` viene sempre impostato a `Gmail`.

## Prerequisiti

- Python 3.11+
- Account Google con Gmail API abilitata
- Account HubSpot con una Private App e scope:
  - `crm.objects.contacts.read`
  - `crm.objects.contacts.write`
  - `crm.objects.notes.write`

## Setup

### 1. Credenziali Google

1. Apri [Google Cloud Console](https://console.cloud.google.com/).
2. Crea un progetto → **API & Services → Enable APIs** → abilita **Gmail API**.
3. **Credentials → Create Credentials → OAuth 2.0 Client ID** (tipo: *Desktop app*).
4. Scarica il file JSON e salvalo come `credentials.json` nella root del progetto.

### 2. Token HubSpot

1. HubSpot → **Settings → Integrations → Private Apps → Create private app**.
2. Scope minimi: `crm.objects.contacts.read/write`, `crm.objects.notes.write`.
3. Copia il token generato.

### 3. Configura .env

```bash
cp .env.example .env
# Modifica .env e inserisci HUBSPOT_ACCESS_TOKEN
```

### 4. Installa dipendenze

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Prima esecuzione (autorizzazione OAuth)

```bash
python gmail_hubspot_sync.py
```

Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail.
Il token verrà salvato in `token.json` per le esecuzioni successive.

## Output

Per ogni email elaborata il log mostra:

```
2025-05-22 10:05:01 [INFO] [Creato    ] mario.rossi@acme.it              → HubSpot ID: 12345
2025-05-22 10:05:02 [INFO] [Ignorato  ] newsletter@mailchimp.com         → HubSpot ID: 67890
2025-05-22 10:05:03 [INFO] [Aggiornato] luca.bianchi@startup.io          → HubSpot ID: 11223
```

E a fine ciclo:

```
2025-05-22 10:05:03 [INFO] Riepilogo: 1 creati, 1 aggiornati, 1 ignorati
```

## Configurazione avanzata

| Variabile | Default | Descrizione |
|---|---|---|
| `HUBSPOT_ACCESS_TOKEN` | *(obbligatorio)* | Token della Private App HubSpot |
| `POLL_INTERVAL_SECONDS` | `60` | Secondi tra un polling e il successivo |
| `SKIP_DOMAINS` | *(vuoto)* | Domini da ignorare, separati da virgola |

## Struttura file

```
├── gmail_hubspot_sync.py   # entry-point, loop principale
├── gmail_client.py         # wrapper Gmail API
├── hubspot_client.py       # wrapper HubSpot REST API v3
├── contact_extractor.py    # parsing header → dati contatto
├── requirements.txt
├── .env.example
├── credentials.json        # ← DA CREARE (non committare)
├── token.json              # ← generato al primo login
└── .sync_state.json        # stato persistente del polling
```

> **Sicurezza**: aggiungi `credentials.json`, `token.json` e `.env` al tuo `.gitignore`.
