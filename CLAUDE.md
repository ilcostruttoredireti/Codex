# Gmail → HubSpot Contact Sync

Questo progetto monitora la casella Gmail in arrivo ed esegue la sincronizzazione automatica dei contatti mittenti verso HubSpot CRM.

## Funzionamento

Per ogni email in arrivo il sistema:
1. Estrae email, nome e dominio del mittente
2. Verifica se il contatto esiste già in HubSpot (chiave: email)
3. **Crea** il contatto se non esiste, impostando: email, nome, cognome, azienda (dal dominio), fonte = "Gmail"
4. **Aggiorna** i campi mancanti se il contatto esiste già
5. **Ignora** se il contatto è già completo

## Script principale

```
gmail_hubspot_sync.py
```

### Setup

```bash
pip install -r requirements.txt

# Prima autenticazione OAuth Gmail
python gmail_hubspot_sync.py --auth

# Esecuzione singola
HUBSPOT_ACCESS_TOKEN=pat-xxx python gmail_hubspot_sync.py --once

# Loop continuo ogni 5 minuti
HUBSPOT_ACCESS_TOKEN=pat-xxx python gmail_hubspot_sync.py --interval 300
```

### Variabili d'ambiente

| Variabile | Descrizione |
|-----------|-------------|
| `HUBSPOT_ACCESS_TOKEN` | Token Private App di HubSpot (obbligatorio) |

### File di stato

- `token.json` — credenziali OAuth Gmail (generato automaticamente)
- `last_sync.json` — ID dell'ultimo messaggio processato (evita riprocessamenti)
- `credentials.json` — Credenziali OAuth scaricate da Google Cloud Console

## Esecuzione come Claude Code session (modalità MCP)

Questo progetto può essere eseguito anche come sessione Claude Code schedulata,
usando i tool MCP `mcp__Gmail__search_threads` e `mcp__HubSpot__*`.

Prompt di scheduling:

```
Controlla le ultime email in Gmail (in:inbox -from:me newer_than:1d).
Per ogni mittente unico non in HubSpot, crea il contatto con email, nome, cognome,
azienda (dal dominio), fonte "Gmail". Per contatti già esistenti, aggiorna i campi
mancanti. Ignora: mailer-daemon, notifiche social, email proprie. Report finale:
Creati / Aggiornati / Ignorati con HubSpot ID.
```

## Logica anti-duplicati

- La chiave univoca è sempre l'indirizzo email
- Mittenti ignorati: `mailer-daemon`, `no-reply`, notifiche Facebook/LinkedIn,
  email proprie dell'account
- I contatti con tutti i campi già compilati vengono marcati "Ignorato (già completo)"

## Output per ogni email processata

```
[Stato]       Email mittente       HubSpot ID
Creato        nuovo@example.com    12345678
Aggiornato    esiste@example.com   87654321
Ignorato      completo@example.com 11223344
```
