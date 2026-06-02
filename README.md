# Gmail → HubSpot Contact Sync

Monitora continuamente la casella Gmail e sincronizza automaticamente i mittenti come contatti HubSpot.

## Funzionalità

- Polling continuo della casella in arrivo Gmail
- Estrazione: email, nome, cognome, dominio/azienda del mittente
- Creazione nuovo contatto HubSpot se non esiste
- Aggiornamento campi mancanti se il contatto esiste già
- Evita duplicati usando l'email come chiave unica
- Aggiunge attività email sulla timeline del contatto
- Applica etichetta Gmail `Synced/HubSpot` ai messaggi processati
- Salta automaticamente mittenti automatici (noreply, mailer-daemon, ecc.)
- Stato persistente: sopravvive ai riavvii senza riprocessare email già gestite

## Output per ogni email

| Campo | Valore |
|-------|--------|
| Stato | `Creato` / `Aggiornato` / `Ignorato` / `Errore` |
| Email contatto | mittente@dominio.com |
| ID contatto HubSpot | 12345678 |

## Setup

### 1. Dipendenze Python

```bash
pip install -r requirements.txt
```

### 2. Credenziali Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com)
2. Crea un progetto → Abilita **Gmail API**
3. Crea credenziali OAuth2 (tipo: Desktop App)
4. Scarica il file `credentials.json` nella cartella del progetto
5. Esegui il setup OAuth una volta sola:

```bash
python setup_gmail_oauth.py
```

Questo apre il browser per autorizzare l'accesso e salva `token.json`.

### 3. Token HubSpot

1. In HubSpot: Impostazioni → Integrazioni → App private
2. Crea una nuova app privata con scope:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.schemas.contacts.read`
3. Copia il token

### 4. Configurazione `.env`

```bash
cp .env.example .env
# Modifica .env con i tuoi valori
```

```env
GMAIL_CREDENTIALS_FILE=credentials.json
GMAIL_TOKEN_FILE=token.json
HUBSPOT_ACCESS_TOKEN=pat-eu1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
POLL_INTERVAL_SECONDS=60
SKIP_DOMAINS=noreply.com,no-reply.com,mailchimp.com
```

## Utilizzo

```bash
# Esecuzione continua (daemon)
python sync.py

# Singolo ciclo e poi esci
python sync.py --once
```

## Struttura file

```
├── sync.py                 # Entry point principale
├── gmail_client.py         # Client Gmail API
├── hubspot_client.py       # Client HubSpot API
├── state.py                # Persistenza stato sync
├── setup_gmail_oauth.py    # Setup OAuth2 una-tantum
├── requirements.txt
├── .env.example
├── credentials.json        # (non committare — da Google Cloud Console)
├── token.json              # (non committare — generato da setup_gmail_oauth.py)
└── sync_state.json         # (generato automaticamente)
```

## Campi HubSpot compilati

| Campo HubSpot | Sorgente |
|---------------|----------|
| `email` | Indirizzo mittente |
| `firstname` | Prima parola del display name |
| `lastname` | Resto del display name |
| `company` | Estratto dal dominio email |
| `hs_lead_source` | Fisso: `"Gmail"` |

## Note di sicurezza

Aggiungi al `.gitignore`:
```
credentials.json
token.json
.env
sync_state.json
```
