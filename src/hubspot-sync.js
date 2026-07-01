/**
 * HubSpot Contact Sync
 *
 * Gestisce la creazione e l'aggiornamento dei contatti HubSpot
 * a partire dai dati estratti dalle email Gmail.
 *
 * Logica:
 *  1. Cerca contatto per email (chiave univoca)
 *  2. Se non esiste → crea con tutti i campi
 *  3. Se esiste → aggiorna i campi vuoti (non sovrascrive dati esistenti)
 *  4. Aggiunge nota attività "Email ricevuta da Gmail"
 *  5. Restituisce { status, email, hubspotId }
 */

/**
 * Determina quali proprietà aggiornare senza sovrascrivere valori esistenti.
 *
 * @param {object} existing  - Proprietà già presenti nel contatto HubSpot
 * @param {object} incoming  - Proprietà estratte dall'email
 * @returns {object}         - Solo le proprietà da aggiornare
 */
function diffProperties(existing, incoming) {
  const updates = {};
  const OVERWRITE_NEVER = ['email', 'hs_object_id'];

  for (const [key, value] of Object.entries(incoming)) {
    if (OVERWRITE_NEVER.includes(key)) continue;
    if (!value || String(value).trim() === '') continue;
    const current = existing[key];
    if (!current || String(current).trim() === '') {
      updates[key] = value;
    }
  }

  return updates;
}

/**
 * Mappa le proprietà del contatto ai nomi di campo HubSpot.
 */
function toHubSpotProperties(props) {
  return {
    email: props.email,
    firstname: props.firstname,
    lastname: props.lastname,
    company: props.company,
    hs_lead_status: 'NEW',
  };
}

/**
 * Costruisce il corpo della nota da associare al contatto.
 */
function buildActivityNote(props, direction = 'inbound') {
  const date = new Date().toISOString().split('T')[0];
  return [
    `📧 Email ${direction === 'inbound' ? 'ricevuta' : 'inviata'} via Gmail`,
    `Data: ${date}`,
    `Mittente: ${props.email}`,
    props.subject ? `Oggetto: ${props.subject}` : '',
    `Tag: Inbound Gmail`,
  ].filter(Boolean).join('\n');
}

export { diffProperties, toHubSpotProperties, buildActivityNote };
