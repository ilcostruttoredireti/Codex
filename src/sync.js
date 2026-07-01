import { fetchRecentSenders } from './gmail.js';
import { findContactByEmail, createContact, updateContact } from './hubspot.js';

export async function runSync({ gmail, hubspot, lookbackDays = 1, dryRun = false }) {
  const senders = await fetchRecentSenders(gmail, { lookbackDays });
  console.log(`Found ${senders.length} unique senders in the last ${lookbackDays} day(s).`);

  const results = [];

  for (const sender of senders) {
    try {
      const result = await processSender(sender, hubspot, dryRun);
      results.push(result);
      logResult(result);
    } catch (err) {
      console.error(`Error processing ${sender.email}: ${err.message}`);
      results.push({ status: 'ERROR', email: sender.email, error: err.message });
    }
  }

  return results;
}

async function processSender(sender, hubspot, dryRun) {
  const existing = await findContactByEmail(hubspot, sender.email);

  if (existing) {
    const updateResult = await updateContact(hubspot, existing.id, sender, dryRun);
    return {
      status: updateResult.updated ? 'Aggiornato' : 'Ignorato',
      email: sender.email,
      contactId: existing.id,
      updates: updateResult.updates,
    };
  }

  const created = await createContact(hubspot, sender, dryRun);
  return {
    status: 'Creato',
    email: sender.email,
    contactId: created.id,
  };
}

function logResult({ status, email, contactId, updates }) {
  const icon = status === 'Creato' ? '✅' : status === 'Aggiornato' ? '🔄' : '⏭️';
  const detail = updates ? ` (aggiornato: ${Object.keys(updates).join(', ')})` : '';
  console.log(`${icon} [${status}] ${email} → ID: ${contactId}${detail}`);
}
