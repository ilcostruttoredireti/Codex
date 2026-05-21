# Gmail → HubSpot Contact Sync

Monitora la casella Gmail e sincronizza automaticamente i mittenti su HubSpot CRM.

## Funzionalità

- Polling continuo delle email in arrivo (configurabile)
- Estrazione automatica di email, nome, cognome, azienda (dal dominio)
- Creazione nuovo contatto HubSpot se non esiste
- Aggiornamento campi mancanti se il contatto esiste già
- Nessun duplicato: l'email è la chiave univoca
- Sorgente contatto impostata a `"Gmail"`
- Stato persistente: le email già processate non vengono rilette

## Setup

### 1. Credenziali Gmail (OAuth2)

1. Vai su [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto → Abilita **Gmail API**
3. Credenziali → Crea **OAuth 2.0 Client ID** (tipo: Desktop App)
4. Scarica il JSON e salvalo come `credentials.json` nella root del progetto

### 2. Token HubSpot

1. HubSpot → Impostazioni → Integrazioni → **App Private**
2. Crea una nuova app con scope: `crm.objects.contacts.read` e `crm.objects.contacts.write`
3. Copia il token

### 3. Configurazione

```bash
cp .env.example .env
# Modifica .env con i tuoi valori
```

### 4. Installazione dipendenze

```bash
pip install -r requirements.txt
```

### 5. Avvio

```bash
# Loop continuo (ogni 60 secondi)
python main.py

# Singolo ciclo
python main.py --once

# Dry-run: mostra cosa farebbe senza toccare HubSpot
python main.py --dry-run --once
```

## Output per ogni email processata

```
STATO        EMAIL                                    ID HUBSPOT      NOTE
------------------------------------------------------------------------
Creato       mario.rossi@acme.com                     12345678        
Aggiornato   giulia.bianchi@example.it                87654321        Campi aggiornati: company
Ignorato     noreply@newsletter.com                   -               Nessun campo nuovo da aggiornare
```

## Struttura file

```
main.py             # Entry point, loop principale
gmail_auth.py       # Autenticazione OAuth2 Gmail
gmail_reader.py     # Lettura email e parsing mittenti
hubspot_sync.py     # Creazione/aggiornamento contatti HubSpot
.env.example        # Template variabili d'ambiente
requirements.txt    # Dipendenze Python
processed_ids.json  # (generato) Stato email già processate
token.json          # (generato) Token OAuth Gmail
```

## Campi HubSpot compilati

| Campo HubSpot | Sorgente |
|---------------|---------|
| `email` | Indirizzo mittente |
| `firstname` | Prima parte del display name |
| `lastname` | Seconda parte del display name |
| `company` | Dominio email (es. `acme.com` → `Acme`) |
| `leadsource` | Fisso: `"Gmail"` |
| `hs_lead_status` | Fisso: `"NEW"` (solo alla creazione) |
