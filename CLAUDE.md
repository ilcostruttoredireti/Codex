# Gmail → HubSpot Contact Sync

## Panoramica
Applicazione Python che monitora la casella Gmail in tempo reale e sincronizza automaticamente i mittenti come contatti in HubSpot CRM.

## Struttura
```
Codex/
├── main.py                         # Entrypoint CLI
├── requirements.txt
├── .env.example                    # Template configurazione
├── gmail_hubspot_sync/
│   ├── __init__.py
│   ├── config.py                   # Lettura variabili d'ambiente
│   ├── gmail_client.py             # OAuth2 + Gmail History API
│   ├── hubspot_client.py           # HubSpot CRM API v3
│   ├── sync.py                     # Orchestratore principale
│   └── models.py                   # Dataclass condivisi
└── tests/
    ├── test_gmail_client.py
    └── test_hubspot_client.py
```

## Setup rapido

```bash
pip install -r requirements.txt
cp .env.example .env
# Compila .env con i tuoi token
python main.py
```

## Comandi principali

```bash
python main.py              # loop continuo (Ctrl+C per fermare)
python main.py --once       # singola iterazione
python main.py --dry-run    # simula senza scrivere su HubSpot
python main.py --version
pytest tests/               # esegui i test
```

## Variabili obbligatorie in .env
- `GMAIL_CREDENTIALS_FILE` – path al file OAuth client secret di Google
- `HUBSPOT_ACCESS_TOKEN` – token Private App di HubSpot

## Note architetturali
- Gmail: usa la **History API** (incrementale) per evitare di ri-scansionare tutta la casella ad ogni iterazione.
- HubSpot: la **email** è la chiave unica per la deduplicazione.
- Lo stato (ultimo `historyId`) è salvato in `.gmail_sync_state.json`.
- I campi HubSpot vengono aggiornati **solo se vuoti** (non si sovrascrivono dati esistenti).
