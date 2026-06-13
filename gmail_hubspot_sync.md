# Gmail → HubSpot Contact Sync

Routine automatica che monitora le email in arrivo su Gmail ed estrae i mittenti per sincronizzarli in HubSpot CRM.

## Come funziona

```
Gmail Inbox (ultimi 24h)
        │
        ▼
  Filtra mittenti
  (escludi: account owner, noreply, notifiche automatiche)
        │
        ▼
  HubSpot: cerca per email
   ┌─────┴──────┐
   │            │
EXISTS       NON EXISTS
   │            │
Aggiorna     Crea nuovo
campi        contatto
mancanti     │
   │         Campi:
   │         - email
   │         - firstname / lastname
   │         - company (da dominio)
   │         - hs_lead_status: OPEN
   │
   └────┬────┘
        │
   Aggiungi nota
   timeline con
   email ricevute
```

## Campi HubSpot popolati

| Campo HubSpot     | Fonte                                  |
|-------------------|----------------------------------------|
| `email`           | From header email                      |
| `firstname`       | Nome dal From header                   |
| `lastname`        | Cognome dal From header                |
| `company`         | Estratto dal dominio email             |
| `hs_lead_status`  | `OPEN` per nuovi contatti              |
| Nota timeline     | Elenco email ricevute + tag "Inbound Gmail" |

## Mittenti ignorati

- Account proprietario (`cristian.mameli.editore@gmail.com`)
- Domini automatici: `facebookmail.com`, `bounce.googlemail.com`, ecc.
- Prefissi: `noreply`, `no-reply`, `mailer-daemon`, `postmaster`, `bounce`

## Output per email processata

```
Stato: Creato | Aggiornato | Ignorato
Email contatto: mittente@dominio.it
ID contatto HubSpot: 123456789
```

## Schedulazione

Questa routine viene eseguita tramite Claude Code sul web con trigger schedulato.
Ogni esecuzione controlla le email delle ultime 24 ore (`newer_than:1d`).

## Dipendenze MCP

- `mcp__Gmail__search_threads` — lista thread inbox
- `mcp__HubSpot__search_crm_objects` — verifica esistenza contatto
- `mcp__HubSpot__manage_crm_objects` — crea / aggiorna contatti e note
