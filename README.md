# Gmail → HubSpot Contact Sync

Monitora automaticamente la casella Gmail e sincronizza i contatti dei mittenti in HubSpot, evitando duplicati.

## Funzionamento

```
Email in arrivo → Estrazione mittente → Verifica HubSpot → Crea / Aggiorna contatto
```

Per ogni nuova email:
1. **Estrae** email, nome, cognome, dominio aziendale (anche da email inoltrate)
2. **Cerca** il contatto in HubSpot via email (chiave unica)
3. **Crea** il contatto se non esiste, con `Fonte: Gmail` e `lifecyclestage: lead`
4. **Aggiorna** i campi vuoti se il contatto esiste già
5. **Registra** l'attività email sulla timeline HubSpot

### Output per ogni email

| Stato | Significato |
|-------|-------------|
| ✅ CREATO | Nuovo contatto creato in HubSpot |
| 🔄 AGGIORNATO | Contatto esistente aggiornato con dati mancanti |
| ⏭️ IGNORATO | Contatto già completo, nessuna modifica necessaria |
| ❌ ERRORE | Errore durante l'elaborazione |

## Setup

### 1. Prerequisiti

```bash
pip install -r requirements.txt
```

### 2. Google Cloud Console

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto e abilita **Gmail API**
3. Crea credenziali **OAuth 2.0 Desktop**
4. Scarica `credentials.json` nella cartella del progetto

### 3. HubSpot Private App Token

1. In HubSpot: Impostazioni → Integrazioni → App Private
2. Crea app con scope: `crm.objects.contacts.read`, `crm.objects.contacts.write`
3. Copia il token

### 4. Configurazione

```bash
cp .env.example .env
# Modifica .env con i tuoi valori
```

### 5. Prima esecuzione (OAuth Gmail)

```bash
python gmail_hubspot_sync.py
```

Al primo avvio si aprirà il browser per autorizzare l'accesso Gmail. Il token viene salvato in `token.pickle` per le esecuzioni successive.

## Esecuzione continua

### Manuale
```bash
python gmail_hubspot_sync.py
```

### Come servizio systemd (Linux)

```ini
[Unit]
Description=Gmail HubSpot Sync
After=network.target

[Service]
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 gmail_hubspot_sync.py
Restart=always
EnvironmentFile=/path/to/project/.env

[Install]
WantedBy=multi-user.target
```

### Docker

```bash
docker build -t gmail-hubspot-sync .
docker run -d \
  -e HUBSPOT_API_KEY=your_token \
  -v $(pwd)/credentials.json:/app/credentials.json \
  -v $(pwd)/token.pickle:/app/token.pickle \
  gmail-hubspot-sync
```

## Logica anti-duplicati

- L'**email** è usata come chiave univoca
- Vengono saltati: `no-reply@`, `mailer-daemon@`, email proprie
- I domini personali (gmail, yahoo, ecc.) non generano nome azienda

## Campi HubSpot compilati

| Campo HubSpot | Fonte |
|---------------|-------|
| `email` | Header From: |
| `firstname` | Display name mittente |
| `lastname` | Display name mittente |
| `company` | Estratto dal dominio email |
| `hs_analytics_source` | "Gmail" (fisso) |
| `lifecyclestage` | "lead" (solo nuovi) |

## File di stato

`sync_state.json` traccia i thread già processati per evitare elaborazioni doppie tra un ciclo e l'altro.
