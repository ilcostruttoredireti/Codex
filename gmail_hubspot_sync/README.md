# Gmail → HubSpot Contact Sync

Monitora la casella Gmail in arrivo, estrae i mittenti e li sincronizza automaticamente in HubSpot evitando duplicati.

## Funzionamento

Per ogni email in arrivo:
1. Estrae email, nome e dominio del mittente
2. Verifica se il contatto esiste già in HubSpot (chiave: email)
3. **Se esiste** → aggiorna i campi mancanti
4. **Se non esiste** → crea nuovo contatto
5. Compila: Email, Nome, Cognome, Azienda (dal dominio), Fonte = "Gmail"

## Setup

### 1. Google Cloud (Gmail API)

1. Vai su [console.cloud.google.com](https://console.cloud.google.com)
2. Crea un progetto → abilita **Gmail API**
3. Crea credenziali OAuth 2.0 (tipo: Desktop app)
4. Scarica `credentials.json` e mettilo in questa cartella

### 2. HubSpot Private App

1. HubSpot → Impostazioni → Integrazioni → App private
2. Crea app con scope: `crm.objects.contacts.read`, `crm.objects.contacts.write`
3. Copia il token nel file `.env`

### 3. Installazione

```bash
pip install -r requirements.txt
cp .env.example .env
# Modifica .env con i tuoi dati
```

### 4. Prima esecuzione (autenticazione Gmail)

```bash
python sync.py
# Si aprirà il browser per autorizzare l'accesso Gmail
# Il token viene salvato in token.json per le esecuzioni future
```

## Utilizzo

```bash
# Analizza ultimi 7 giorni (default)
python sync.py

# Analizza ultimi 30 giorni
python sync.py --days 30
```

## Output

```
============================================================
SYNC GMAIL → HUBSPOT  |  2026-06-13 18:00 UTC
============================================================
Creati: 3  |  Aggiornati: 5  |  Ignorati: 2
============================================================
[CREATO]    mario.rossi@example.com | ID:12345678
[AGGIORNATO] info@azienda.it        | ID:87654321
[IGNORATO]  noreply@facebook.com    | (nessun campo mancante)
```

## Automazione (cron)

```bash
# Esegui ogni giorno alle 08:00
0 8 * * * cd /path/to/gmail_hubspot_sync && python sync.py --days 1 >> /var/log/sync.log 2>&1
```

## Mittenti ignorati automaticamente

- Mailer daemon / bounce
- Notifiche automatiche (Facebook, ecc.)
- I tuoi stessi indirizzi email (configurati in `OWN_EMAILS`)
