# Gmail → HubSpot Contact Sync

Monitora le email in arrivo su Gmail ed esegue automaticamente l'upsert dei mittenti come contatti in HubSpot CRM.

## Funzionamento

```
Gmail INBOX ──► estrai mittente ──► cerca in HubSpot ──► crea / aggiorna / ignora
                                          │
                                    (chiave: email)
```

Per ogni email elaborata viene stampato:
```
[CREATO    ]  mario.rossi@acme.com (Mario Rossi) | Acme  →  ID: 12345678
[AGGIORNATO]  info@example.com                   →  ID: 87654321
[IGNORATO  ]  newsletter@news.com                →  ID: 11223344
```

## Setup

### 1. Dipendenze Python

```bash
pip install -r requirements.txt
```

### 2. Google Cloud — Gmail API

1. Vai su [Google Cloud Console](https://console.cloud.google.com/) → crea un progetto (o usa uno esistente)
2. **APIs & Services → Enable APIs** → abilita **Gmail API**
3. **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop App**
4. Scarica il file JSON e rinominalo `credentials.json` nella directory del progetto

### 3. HubSpot — Private App

1. Vai su **HubSpot → Settings → Integrations → Private Apps → Create a private app**
2. Scopes minimi richiesti:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
3. Copia il token generato

### 4. Configurazione `.env`

```bash
cp .env.example .env
# modifica .env con il tuo token HubSpot
```

### 5. Primo avvio — autorizzazione Gmail

Al primo avvio si apre automaticamente il browser per il consenso OAuth:

```bash
python main.py
```

Il token viene salvato in `token.json` e riutilizzato nei run successivi.

## Utilizzo

```bash
# Singola esecuzione (processa le email nuove e termina)
python main.py

# Modalità continua — scansione ogni 60 secondi
python main.py --loop

# Modalità continua con intervallo personalizzato (120 s)
python main.py --loop --interval 120
```

## Campi HubSpot popolati

| Proprietà HubSpot | Fonte                                  |
|-------------------|----------------------------------------|
| `email`           | Indirizzo del mittente                 |
| `firstname`       | Nome estratto dall'header `From:`      |
| `lastname`        | Cognome estratto dall'header `From:`   |
| `company`         | Derivato dal dominio email             |
| `leadsource`      | Fisso: `"Gmail"`                       |
| `hs_lead_status`  | `"NEW"` (solo alla creazione)          |

## Logica duplicati

L'email è usata come chiave unica. Comportamento:
- **Contatto non trovato** → creazione
- **Contatto esistente, campi vuoti** → aggiornamento solo dei campi mancanti
- **Contatto esistente, tutto compilato** → ignorato (nessuna modifica)

## Domini da escludere

Imposta `SKIP_DOMAINS` in `.env` per ignorare domini specifici:

```
SKIP_DOMAINS=miaazienda.com,noreply.github.com,notifications.example.com
```
