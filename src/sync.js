'use strict';

const { extractContactFields } = require('./utils');
const { createGmailClient, fetchInboxSenders } = require('./gmail');
const { createHubspotClient, findContactByEmail, createContact, updateContact } = require('./hubspot');

async function runSync({ lookbackMinutes = 60, dryRun = false } = {}) {
  const gmail = createGmailClient();
  const hubspotClient = createHubspotClient();

  const sinceDate = new Date(Date.now() - lookbackMinutes * 60 * 1000);
  console.log(`[sync] Scanning Gmail inbox since ${sinceDate.toISOString()} (lookback: ${lookbackMinutes}m)`);

  const senderHeaders = await fetchInboxSenders(gmail, sinceDate);
  console.log(`[sync] Found ${senderHeaders.length} unique senders`);

  const results = [];

  for (const header of senderHeaders) {
    const fields = extractContactFields(header);
    if (!fields) {
      console.log(`[sync] Skipped (automated): ${header}`);
      continue;
    }

    const existing = await findContactByEmail(hubspotClient, fields.email);

    let result;
    if (existing) {
      result = await updateContact(hubspotClient, existing.id, existing, fields, dryRun);
    } else {
      result = await createContact(hubspotClient, fields, dryRun);
    }

    results.push({ email: fields.email, hubspotId: result.id, status: result.status });
    console.log(`[sync] ${result.status.padEnd(10)} ${fields.email}  (HubSpot ID: ${result.id})`);
  }

  const summary = results.reduce((acc, r) => {
    acc[r.status] = (acc[r.status] || 0) + 1;
    return acc;
  }, {});

  console.log('\n--- Sync summary ---');
  console.log(`Processati : ${results.length}`);
  console.log(`Creati     : ${summary['Creato'] || 0}`);
  console.log(`Aggiornati : ${summary['Aggiornato'] || 0}`);
  console.log(`Ignorati   : ${summary['Ignorato'] || 0}`);

  return results;
}

module.exports = { runSync };
