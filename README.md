# Gmail → HubSpot Contact Sync

Routine automatica che monitora le email in arrivo su Gmail e sincronizza i mittenti come contatti in HubSpot.

## Come funziona

```
Gmail Inbox (ultime 24h)
        │
        ▼
 Filtra mittenti automatici
 (noreply, notifiche, bot)
        │
        ▼
 Per ogni mittente umano unico:
        │
        ├─ Esiste in HubSpot? ──Sì──▶ Aggiorna campi mancanti
        │                              + Aggiunge nota "Inbound Gmail"
        │
        └───────────────────No──▶ Crea nuovo contatto
                                   + Aggiunge nota "Inbound Gmail"
        │
        ▼
 Report: Creato / Aggiornato / Ignorato
        │
        ▼
 Push Notification → telefono
```

## Campi HubSpot compilati

| Campo HubSpot | Fonte |
|---------------|-------|
| Email | Indirizzo mittente |
| Nome | Display name email (se disponibile) |
| Cognome | Display name email (se disponibile) |
| Azienda | Dominio email (es. `latestata.it` → `Latestata`) |
| Nota attività | Testo con tag "Inbound Gmail", data, conteggio email |

## Regole di deduplicazione

- Chiave univoca: **indirizzo email**
- Se il contatto esiste: aggiorna solo i campi **vuoti** (non sovrascrive dati esistenti)
- Se non esiste: crea nuovo contatto con tutti i campi disponibili

## Mittenti ignorati automaticamente

- `notification@*`, `noreply@*`, `no-reply@*`
- `*@facebookmail.com`, `*@amazonses.com`, `*@sendgrid.net`
- `postmaster@*`, `mailer-daemon@*`, `donotreply@*`

## File

| File | Descrizione |
|------|-------------|
| `gmail_to_hubspot_sync.py` | Logica di sync (classi, regole, utility) |
| `SYNC_LOG.md` | Log dell'ultima esecuzione |

## Esecuzione

Questa routine viene eseguita come **sessione schedulata Claude Code** con accesso ai tool MCP:
- **Gmail MCP** — lettura email inbox
- **HubSpot MCP** — ricerca, creazione e aggiornamento contatti
