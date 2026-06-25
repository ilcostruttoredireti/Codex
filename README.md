# Gmail → HubSpot Contact Sync

Monitora la casella Gmail e sincronizza automaticamente i mittenti come contatti HubSpot, evitando duplicati e aggiornando i campi mancanti.

## Funzionamento

Per ogni email in arrivo:
1. Estrae i dati del mittente (email, nome, dominio/azienda)
2. Gestisce le email inoltrate estraendo il mittente originale
3. Verifica in HubSpot se il contatto esiste già
   - **Esiste** → aggiorna i campi vuoti (nome, cognome, azienda)
   - **Non esiste** → crea il contatto con nota "Inbound Gmail"
4. Usa l'email come chiave univoca per evitare duplicati

## Output per email processata

| Campo      | Valori possibili                    |
|------------|-------------------------------------|
| Stato      | `Creato` / `Aggiornato` / `Ignorato` |
| Email      | indirizzo del mittente              |
| HubSpot ID | ID numerico del contatto            |

## Setup

### 1. Prerequisiti

```bash
pip install -r requirements.txt
```

### 2. Gmail API

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto e abilita la **Gmail API**
3. Crea credenziali OAuth 2.0 (tipo: Desktop App)
4. Scarica `credentials.json` nella cartella del progetto

### 3. HubSpot

1. Vai su **Impostazioni → Integrazioni → App private**
2. Crea una nuova app privata con gli scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
3. Copia il token generato

### 4. Variabili d'ambiente

```bash
cp .env.example .env
# Modifica .env con i tuoi valori reali
```

### 5. Prima esecuzione (autenticazione Gmail)

Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail:

```bash
python gmail_hubspot_sync.py
```

Il token viene salvato in `token.json` per le esecuzioni successive.

## Esecuzione automatica (cron)

```bash
# Ogni 15 minuti
*/15 * * * * cd /path/to/project && python gmail_hubspot_sync.py >> sync.log 2>&1
```

## Stato di avanzamento

Il file `.sync_state.json` (ignorato da git) memorizza:
- `last_history_id`: punto di ripresa per l'API Gmail History
- `processed_message_ids`: IDs già elaborati (ultimi 500)

## Note tecniche

- Le email inoltrate (Fw:/Fwd:) vengono analizzate per estrarre il mittente originale
- I domini istituzionali (es. `comune.roma.it`) generano automaticamente il nome azienda
- I domini generici (gmail, yahoo, ecc.) non producono un'azienda
- La nota "Inbound Gmail" viene aggiunta solo ai contatti nuovi
