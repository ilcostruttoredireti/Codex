# Gmail → HubSpot Contact Sync

Monitora la casella Gmail in arrivo e sincronizza automaticamente i mittenti come contatti HubSpot, evitando duplicati e aggiornando i campi mancanti.

## Funzionalità

- Polling continuo della casella Gmail (configurabile)
- Sincronizzazione incrementale tramite Gmail History API
- Ricerca contatto HubSpot per email (chiave univoca)
- **Creazione** nuovo contatto se non esiste
- **Aggiornamento** dei campi mancanti se il contatto esiste già
- **Ignora** contatti già completi
- Estrazione nome/cognome dal display name
- Estrazione azienda dal dominio email (esclude provider personali)
- Engagement timeline: email ricevuta associata al contatto
- Fonte contatto impostata a `"Gmail"` (`hs_lead_source`)
- Output per ogni email: `Stato | Email | ID HubSpot`

## Prerequisiti

- Python 3.10+
- Account Google Cloud con Gmail API abilitata
- HubSpot Private App con scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.emails.write`

## Setup

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Configura Google Cloud (Gmail API)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto e abilita la **Gmail API**
3. Crea credenziali **OAuth 2.0** (tipo: Desktop app)
4. Scarica il file JSON e rinominalo `credentials.json` nella directory del progetto

### 3. Configura HubSpot

1. Vai su **HubSpot → Impostazioni → Integrazioni → App private**
2. Crea una nuova Private App con gli scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.emails.write`
3. Copia il token generato

### 4. Crea il file `.env`

```bash
cp .env.example .env
```

Modifica `.env` e inserisci il tuo `HUBSPOT_ACCESS_TOKEN`.

## Utilizzo

### Avvio continuo (modalità daemon)

```bash
python main.py
```

Il primo avvio registra lo stato attuale della casella — le email **successive** verranno sincronizzate. Alla prima esecuzione verrà aperto il browser per autorizzare l'accesso Gmail.

### Singolo ciclo (test)

```bash
python main.py --once
```

Processa le email non lette correnti e stampa il report.

## Output di esempio

```
=== Gmail → HubSpot Sync avviato ===
Premi Ctrl+C per terminare.

Prima esecuzione: registro lo stato attuale della casella email.
historyId registrato: 123456
In attesa di nuove email (polling ogni 60s)...

  Stato: Creato        Email: mario.rossi@acme.com              ID HubSpot: 12345
  Stato: Aggiornato    Email: giulia.bianchi@example.org        ID HubSpot: 67890
  Stato: Ignorato      Email: info@company.it                   ID HubSpot: 11223
```

## Configurazione avanzata

Tutte le variabili sono in `.env`:

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `HUBSPOT_ACCESS_TOKEN` | — | Token HubSpot (obbligatorio) |
| `POLL_INTERVAL_SECONDS` | `60` | Frequenza polling in secondi |
| `GMAIL_QUERY` | `in:inbox is:unread` | Query Gmail per filtrare email |
| `SKIP_DOMAINS` | `gmail.com,...` | Domini da ignorare per l'azienda |
| `STATE_FILE` | `sync_state.json` | File di stato per sync incrementale |

## Struttura del progetto

```
.
├── main.py           # Entry point CLI
├── sync.py           # Loop di sincronizzazione
├── gmail_client.py   # Wrapper Gmail API
├── hubspot_client.py # Wrapper HubSpot API
├── config.py         # Configurazione da variabili d'ambiente
├── requirements.txt
├── .env.example
└── README.md
```
