# Gmail → HubSpot Contact Sync — Istruzioni Agente

## Scopo
Routine schedulata che monitora Gmail per nuove email in arrivo ed esegue
l'upsert automatico dei mittenti in HubSpot, evitando duplicati.

## Come funziona ogni run

### 1. Carica stato
- Leggi `state/last_sync.json` per recuperare i thread già processati.

### 2. Cerca email recenti
```
mcp__Gmail__search_threads(
  query = "in:inbox newer_than:1d -from:me",
  pageSize = 50
)
```

### 3. Per ogni thread NON in `processed_thread_ids`

**Estrai mittente** dal campo `sender` del primo messaggio.

**Salta** i mittenti da ignorare (domini automatici):
- facebookmail.com, twitter.com, notifications.google.com,
  accounts.google.com, linkedinmail.com, mail.instagram.com

**Salta** l'email dell'utente stesso (`cristian.mameli.editore@gmail.com`).

**Cerca su HubSpot**:
```
mcp__HubSpot__search_crm_objects(
  objectType = "contacts",
  filterGroups = [{ filters: [{ propertyName: "email", operator: "EQ", value: <email> }] }],
  properties = ["email", "firstname", "lastname", "company"]
)
```

**Se NON trovato** → Crea contatto:
```
mcp__HubSpot__manage_crm_objects(
  confirmationStatus = "CONFIRMATION_WAIVED_FOR_SESSION",
  createRequest = { objects: [{
    objectType: "contacts",
    properties: {
      email:     <email>,
      firstname: <firstname>,       # dal nome mittente
      lastname:  <lastname>,        # dal nome mittente
      company:   <domain_company>,  # dal dominio email
    }
  }]}
)
```
Stato output: **Creato**

**Se trovato** → Aggiorna eventuali campi vuoti (firstname, lastname, company).
Se non ci sono campi da aggiornare, salta l'update CRM ma crea comunque la nota.
Stato output: **Aggiornato**

**Crea nota attività** su ogni contatto processato:
```
mcp__HubSpot__manage_crm_objects(
  createRequest = { objects: [{
    objectType: "notes",
    properties: {
      hs_note_body: "📧 Email ricevuta via Gmail il <data>. Tag: Inbound Gmail.",
      hs_timestamp: "<ISO8601>"
    },
    associations: [{ targetObjectId: <contact_id>, targetObjectType: "contacts" }]
  }]}
)
```

### 4. Aggiorna stato
Salva `state/last_sync.json`:
```json
{
  "processed_thread_ids": ["id1", "id2", ...],
  "last_sync": "2026-06-22T15:05:00Z"
}
```
Committa e pusha il file di stato aggiornato.

### 5. Output report
Stampa la tabella:
```
| Stato     | Email                              | ID HubSpot     |
|-----------|------------------------------------|----------------|
| Aggiornato| redazione@latestata.it             | 395702512840   |
| Aggiornato| galleria@gallerianazionalemarche.it| 759075626223   |
| Aggiornato| mayaamenduni@gmail.com             | 769170699507   |
| Ignorato  | pageupdates@facebookmail.com       | —              |
| Ignorato  | cristian.mameli.editore@gmail.com  | — (utente)     |
```

### 6. Notifica push
Invia sempre una PushNotification con il riassunto del run.

## Regole anti-duplicato
- Usare `email` come chiave univoca su HubSpot.
- Mantenere `state/processed_thread_ids` per non riprocessare thread già visti.
- La lista può essere trimmata ai soli ultimi 500 ID per mantenere il file leggero.
