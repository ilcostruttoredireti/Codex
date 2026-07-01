# Gmail → HubSpot Contact Sync

Monitora le email in arrivo su Gmail ed estrae i mittenti per crearli o aggiornarli come contatti in HubSpot, evitando duplicati.

## Flusso

```
Gmail Inbox
    ↓
Filtra mittenti automatici (noreply, bot, piattaforme)
    ↓
Estrai: email, nome, cognome, dominio aziendale
    ↓
Cerca contatto in HubSpot (chiave: email)
    ├─ NON ESISTE → Crea contatto con tutti i campi
    └─ ESISTE → Aggiorna solo i campi vuoti
    ↓
Crea nota attività "Inbound Gmail"
    ↓
Report: Creato / Aggiornato / Ignorato
```

## Campi HubSpot compilati

| Campo HubSpot   | Sorgente                          |
|-----------------|-----------------------------------|
| `email`         | Indirizzo mittente                |
| `firstname`     | Parte nome dal campo "From"       |
| `lastname`      | Parte cognome dal campo "From"    |
| `company`       | Dominio email (es. seozoom → SEOZoom) |
| `hs_lead_status`| `"NEW"`                           |

## Logica anti-duplicati

- Chiave univoca: indirizzo email
- I campi già valorizzati in HubSpot **non vengono sovrascritti**
- Mittenti automatici ignorati: `noreply`, `no-reply`, Google, Facebook, YouTube, ecc.

## Struttura file

```
src/
  sync.js          # Funzioni core (filtri, parsing, report)
  gmail-poller.js  # Recupera thread Gmail via MCP
  hubspot-sync.js  # Crea/aggiorna contatti HubSpot via MCP
```

## Esecuzione (via Claude Code / MCP)

Questo modulo viene eseguito come **routine automatica** tramite gli MCP tool
`mcp__Gmail__search_threads` e `mcp__HubSpot__*`. Non richiede credenziali
aggiuntive se eseguito in una sessione Claude Code con Gmail e HubSpot connessi.
