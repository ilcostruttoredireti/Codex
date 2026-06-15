# Gmail → HubSpot Contact Sync

Monitora la posta in arrivo su Gmail e sincronizza automaticamente i contatti in HubSpot.

## Funzionamento

1. Legge le email nella **Inbox Gmail** (max 50 alla volta)
2. **Filtra** automaticamente mittenti no-reply, newsletter e domini noti (Google, LinkedIn, ecc.)
3. Per ogni mittente reale:
   - **Crea** un nuovo contatto in HubSpot se non esiste
   - **Aggiorna** i campi mancanti se il contatto esiste già
4. Aggiunge una **nota attività** HubSpot taggata `Inbound Gmail`
5. Salva gli ID già processati in `processed_ids.json` per evitare duplicati

## Campi HubSpot compilati

| Campo HubSpot          | Fonte                         |
|------------------------|-------------------------------|
| `email`                | Indirizzo mittente            |
| `firstname`            | Parte sinistra del display name |
| `lastname`             | Resto del display name        |
| `company`              | Derivato dal dominio email    |
| `hs_analytics_source`  | `EMAIL_MARKETING`             |
| Nota attività          | Oggetto email + tag `Inbound Gmail` |

## Setup

### 1. Credenziali Gmail (OAuth 2.0)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto e abilita **Gmail API**
3. Crea credenziali OAuth 2.0 → Desktop app
4. Scarica il file JSON e salvalo come `gmail_hubspot_sync/credentials.json`

### 2. API Key HubSpot

```bash
export HUBSPOT_API_KEY="il-tuo-token-privato-hubspot"
```

Oppure crea un file `.env`:
```
HUBSPOT_API_KEY=il-tuo-token-privato-hubspot
```

### 3. Installazione dipendenze

```bash
pip install -r gmail_hubspot_sync/requirements.txt
```

### 4. Prima esecuzione (autenticazione Gmail)

```bash
python gmail_hubspot_sync/sync.py
```

Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail.  
Il token viene salvato in `token.json` per le esecuzioni successive.

## Output per ogni email processata

```
── Risultati sincronizzazione Gmail → HubSpot ─────────────────────
Stato                               Email                                    ID HubSpot
──────────────────────────────────────────────────────────────────────────────────────────
Creato                              mario@esempio.it                         12345678
Aggiornato                          info@azienda.com                         87654321
Ignorato (dati già completi)        redazione@latestata.it                   395702512840

Totale elaborati: 3
```

## Esecuzione schedulata

### Linux/macOS (crontab)
```cron
*/15 * * * * /usr/bin/python3 /path/to/Codex/gmail_hubspot_sync/sync.py >> /var/log/gmail_hubspot_sync.log 2>&1
```

### Claude Code on the Web
Il sync può essere configurato come routine schedulata nel progetto Codex usando  
Claude Code Scheduled Sessions.
