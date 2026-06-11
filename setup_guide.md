# Setup: Gmail → HubSpot Contact Sync

## Requisiti

- Python 3.11+
- Account Google con Gmail
- Account HubSpot con una Private App

---

## 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

---

## 2. Crea il file `.env`

```bash
cp .env.example .env
```

Poi modifica `.env` con i tuoi valori.

---

## 3. Configura HubSpot

1. Vai su **HubSpot → Settings → Integrations → Private Apps**
2. Crea una nuova Private App
3. Permessi minimi richiesti:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
4. Copia il token generato e incollalo in `.env` come `HUBSPOT_ACCESS_TOKEN`

---

## 4. Configura Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto (o usa uno esistente)
3. Abilita l'API **Gmail API**
4. Crea credenziali **OAuth 2.0 Client ID** (tipo: Desktop app)
5. Scarica il JSON e salvalo come `credentials.json` nella stessa cartella dello script
6. Al primo avvio si aprirà il browser per autorizzare l'accesso

---

## 5. Avvio

```bash
# Modalità continua (polling ogni 60 secondi)
python gmail_hubspot_sync.py

# Singola passata
python gmail_hubspot_sync.py --once

# Processa solo le email degli ultimi 7 giorni
python gmail_hubspot_sync.py --since 7d --once

# Combinazioni
python gmail_hubspot_sync.py --since 24h
```

---

## Output per ogni email processata

```
✚ [Creato]      mario.rossi@acme.com    (HubSpot ID: 12345678)
↺ [Aggiornato]  luca.bianchi@corp.it    (HubSpot ID: 87654321)
– [Ignorato]    noreply@github.com      (HubSpot ID: N/A)
```

| Stato      | Significato                                         |
|------------|-----------------------------------------------------|
| Creato     | Nuovo contatto aggiunto in HubSpot                  |
| Aggiornato | Contatto esistente arricchito con campi mancanti    |
| Ignorato   | Contatto già completo oppure email non valida       |
| Errore     | Problema durante la chiamata API HubSpot            |

---

## Campi compilati in HubSpot

| Campo HubSpot  | Fonte                                      |
|----------------|--------------------------------------------|
| `email`        | Mittente email                             |
| `firstname`    | Nome dall'header From o dalla parte locale |
| `lastname`     | Cognome dall'header From                   |
| `company`      | Dominio email (es. acme.com → "Acme")      |
| `leadsource`   | Sempre impostato a "Gmail"                 |

> **Nota**: i domini pubblici (gmail, yahoo, hotmail, ecc.) non generano un nome azienda.

---

## Esecuzione automatica (cron)

```bash
# Ogni 5 minuti
*/5 * * * * /usr/bin/python3 /path/to/gmail_hubspot_sync.py --once >> /var/log/gmail_hubspot.log 2>&1
```
