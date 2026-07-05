# Gmail → HubSpot Contact Sync

Monitora le email in arrivo su Gmail ed esegue la sincronizzazione automatica dei mittenti come contatti in HubSpot.

## Come funziona

1. Recupera le email in arrivo nelle ultime `LOOKBACK_HOURS` ore (default 24h)
2. Filtra i mittenti automatici (no-reply, notifiche di sistema, ecc.)
3. Per ogni mittente reale:
   - se il contatto **esiste** in HubSpot → aggiorna i campi mancanti
   - se il contatto **non esiste** → crea un nuovo contatto
4. Imposta `lead_source = "Gmail"` su ogni contatto creato/aggiornato

## Campi HubSpot compilati

| Campo HubSpot       | Fonte                              |
|---------------------|------------------------------------|
| `email`             | indirizzo mittente                 |
| `firstname`         | display name o local-part email    |
| `lastname`          | display name (se presente)         |
| `company`           | dominio email (capitalizzato)      |
| `lead_source`       | "Gmail" (fisso)                    |

## Setup

### 1. Credenziali Gmail (OAuth2)

1. Vai a [Google Cloud Console](https://console.cloud.google.com/)
2. Crea un progetto e abilita l'API Gmail
3. Crea credenziali OAuth2 (tipo "Web application")
4. Genera il refresh token con scope `https://www.googleapis.com/auth/gmail.readonly`
5. Copia i valori in `.env`

### 2. Credenziali HubSpot

1. In HubSpot → Impostazioni → Integrazioni → App private
2. Crea una nuova app privata con scope `crm.objects.contacts.read` e `crm.objects.contacts.write`
3. Copia l'access token in `.env`

### 3. File `.env`

```
cp .env.example .env
# modifica .env con le tue credenziali
```

## Esecuzione

```bash
npm install

# Sync effettivo
npm run sync

# Simulazione (no scritture su HubSpot)
npm run sync:dry
```

## Esecuzione schedulata (cron)

```cron
# Ogni ora
0 * * * * cd /path/to/codex && node src/sync.js >> sync.log 2>&1
```

## Esecuzione con Claude Code (MCP)

Questo repository è ottimizzato per essere eseguito come automazione via Claude Code
con i tool MCP Gmail e HubSpot. In quel caso le credenziali non sono necessarie
perché l'autenticazione avviene tramite gli account collegati a Claude.
