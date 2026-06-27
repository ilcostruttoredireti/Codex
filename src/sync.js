/**
 * Core sync logic: per ogni thread Gmail recente, verifica e aggiorna HubSpot.
 *
 * Regole:
 *  - Usa l'email come chiave unica per evitare duplicati
 *  - Se il contatto non esiste → crea con fonte "Gmail"
 *  - Se esiste → aggiorna solo i campi vuoti
 *  - Ignora mittenti da dominio uguale all'utente Gmail (email interne)
 */

const GMAIL_TAG = 'Inbound Gmail';
const LEAD_SOURCE = 'Gmail';

export async function runSync({ gmail, hubspot, lookbackHours = 24, dryRun = false }) {
  const results = [];
  const seenEmails = new Set();

  console.log(`\n[sync] Cerco email degli ultimi ${lookbackHours}h in Gmail...`);
  const threads = await gmail.getRecentInboxThreads(lookbackHours);
  console.log(`[sync] Trovati ${threads.length} thread`);

  for (const thread of threads) {
    const sender = await gmail.getThreadSender(thread.id);
    if (!sender) continue;

    // Dedup nella stessa run
    if (seenEmails.has(sender.email)) continue;
    seenEmails.add(sender.email);

    const result = await processSender({ sender, hubspot, dryRun });
    results.push(result);

    console.log(`[sync] ${result.status.padEnd(10)} ${result.email}  (HubSpot ID: ${result.hubspotId ?? '—'})`);
  }

  return results;
}

async function processSender({ sender, hubspot, dryRun }) {
  const { email, firstName, lastName, company, subject } = sender;

  const existing = await hubspot.findContactByEmail(email);

  if (!existing) {
    // --- CREA nuovo contatto ---
    if (!dryRun) {
      const props = buildCreateProps(sender);
      const created = await hubspot.createContact(props);
      await hubspot.createNote(
        created.id,
        `${GMAIL_TAG} — Email ricevuta: "${subject}"`,
      );
      return { status: 'Creato', email, hubspotId: created.id };
    }
    return { status: 'Creato (dry-run)', email, hubspotId: null };
  }

  // --- AGGIORNA campi mancanti ---
  const updates = buildUpdateProps(existing.properties, sender);

  if (Object.keys(updates).length === 0) {
    return { status: 'Ignorato', email, hubspotId: existing.id };
  }

  if (!dryRun) {
    await hubspot.updateContact(existing.id, updates);
  }

  return { status: dryRun ? 'Aggiornato (dry-run)' : 'Aggiornato', email, hubspotId: existing.id };
}

function buildCreateProps({ email, firstName, lastName, company }) {
  return {
    email,
    firstname: firstName || email.split('@')[0],
    ...(lastName && { lastname: lastName }),
    ...(company && { company }),
    hs_lead_status: LEAD_SOURCE,
    lead_source: LEAD_SOURCE,
  };
}

function buildUpdateProps(existing, { firstName, lastName, company }) {
  const updates = {};

  if (!existing.firstname && firstName) updates.firstname = firstName;
  if (!existing.lastname && lastName) updates.lastname = lastName;
  if (!existing.company && company) updates.company = company;
  if (!existing.hs_lead_status) updates.hs_lead_status = LEAD_SOURCE;

  return updates;
}
