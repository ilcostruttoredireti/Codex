# Gmail → HubSpot Contact Sync

Workflow automatico che monitora le email in arrivo su Gmail e sincronizza i mittenti come contatti in HubSpot.

## Funzionamento

```
Gmail (email in arrivo)
        │
        ▼
  Estrai mittente
  (email, nome, dominio)
        │
        ▼
  HubSpot: contatto esiste?
    ├─ NO  → Crea contatto
    └─ SÌ  → Aggiorna campi mancanti
        │
        ▼
  Applica label Gmail "HubSpot-Synced"
        │
        ▼
  Log: CREATO / AGGIORNATO / IGNORATO
```

## Campi HubSpot compilati

| Campo HubSpot   | Fonte                              |
|-----------------|------------------------------------|
| `email`         | Indirizzo email mittente           |
| `firstname`     | Prima parola del display name      |
| `lastname`      | Resto del display name             |
| `company`       | Dominio email (es. `acme.com` → `Acme`) |
| `leadsource`    | Fisso: `"Gmail"`                   |
| Note/tag        | `"Inbound Gmail"`                  |

## Esecuzione

Il workflow viene eseguito automaticamente ogni **10 minuti** tramite CronCreate.

Per ogni email elaborata viene prodotto un log:

```
[CREATO]     mario.rossi@acme.com  → HubSpot ID: 12345
[AGGIORNATO] luca.bianchi@corp.it  → HubSpot ID: 67890
[IGNORATO]   noreply@service.com   → Mittente di sistema
```

## Anti-duplicati

- L'email del mittente è usata come **chiave unica** in HubSpot
- Le email già elaborate ricevono il label Gmail `HubSpot-Synced` e vengono saltate nelle run successive
- I mittenti di sistema (noreply, mailer-daemon, ecc.) vengono ignorati automaticamente

## Struttura file

```
gmail_hubspot_sync/
├── README.md          # Questa documentazione
├── sync.py            # Logica di parsing e utilità
└── state/
    └── last_run.txt   # Timestamp dell'ultima esecuzione
```
