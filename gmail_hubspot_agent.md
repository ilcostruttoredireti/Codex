# Gmail → HubSpot Contact Sync — Agent Runbook

## Cosa fa
Monitora tutte le email in arrivo su Gmail. Estrae i mittenti e li
sincronizza automaticamente in HubSpot come contatti, evitando
duplicati e aggiornando i campi mancanti.

## Logica di sync

```
Gmail (inbox, ultimi 7 giorni)
        │
        ▼
  parse_gmail_threads()
  Filtra: skip noreply, notifiche, proprie email, mailer-daemon
        │
        ▼
  Per ogni mittente unico:
  ┌─────────────────────────────────────────────────────────┐
  │  HubSpot search_crm_objects (filtro: email = chiave)    │
  │                                                         │
  │  NON trovato  →  CREA  (manage_crm_objects createRequest)│
  │  Trovato      →  AGGIORNA campi vuoti                   │
  │                  oppure IGNORA se già completo          │
  └─────────────────────────────────────────────────────────┘
        │
        ▼
  Stampa report + PushNotification se ci sono novità
```

## Campi HubSpot compilati

| Campo HubSpot    | Fonte                          |
|------------------|--------------------------------|
| `email`          | Mittente Gmail (chiave unica)  |
| `firstname`      | Display name (se persona)      |
| `lastname`       | Display name (se persona)      |
| `company`        | Dominio email (euristica)      |
| `hs_lead_source` | `OTHER` (più vicino a Gmail)   |

## Domini ignorati
- `facebookmail.com`, `googlemail.com`, `accounts.google.com`
- `bounce.amazon.com`, `mailer-daemon.*`
- Email proprie del proprietario dell'account

## Prefissi locali ignorati
- `noreply`, `no-reply`, `notification`, `notifications`, `mailer-daemon`

## Come schedulare (Claude Code on the web)
1. Crea un'**ambiente** su https://code.claude.com con trigger `schedule`
2. Imposta la frequenza desiderata (es. ogni ora o ogni giorno)
3. Il prompt del task può essere:
   ```
   Esegui il sync Gmail → HubSpot come descritto in gmail_hubspot_agent.md
   ```
4. L'agente chiamerà i tool MCP Gmail e HubSpot e invierà
   una PushNotification con il riepilogo.

## Output per ogni email processata
- **Stato**: `Creato` / `Aggiornato` / `Ignorato`
- **Email contatto**
- **ID contatto HubSpot**
