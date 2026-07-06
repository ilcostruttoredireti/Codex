#!/usr/bin/env node
/**
 * Gmail → HubSpot Contact Sync
 *
 * Reads recent inbox threads, extracts sender data, and creates/updates
 * HubSpot contacts. Uses email address as unique key to avoid duplicates.
 *
 * Required env vars:
 *   GMAIL_CREDENTIALS_PATH   path to OAuth2 credentials JSON
 *   GMAIL_TOKEN_PATH         path to stored OAuth2 token JSON
 *   HUBSPOT_ACCESS_TOKEN     HubSpot private app access token
 *
 * Optional:
 *   SYNC_LOOKBACK_DAYS       how many days back to scan (default: 1)
 *   STATE_FILE               path to JSON file tracking processed thread IDs
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { google } = require('googleapis');
const hubspot = require('@hubspot/api-client');

// ── Configuration ─────────────────────────────────────────────────────────────

const LOOKBACK_DAYS = parseInt(process.env.SYNC_LOOKBACK_DAYS ?? '1', 10);
const STATE_FILE = process.env.STATE_FILE ?? path.join(__dirname, '.sync-state.json');

// Patterns that identify automated / system senders to skip
const SKIP_PATTERNS = [
  /^no[-_]?reply@/i,
  /^noreply@/i,
  /^donotreply@/i,
  /^nobody@/i,
  /^notifications?@/i,
  /^mailer-daemon@/i,
  /^bounce[s@]/i,
  /^postmaster@/i,
  /^(confirm|accountservices|payments?-update|conferma-[a-z]+)@/i,
  /@amazon\.(it|com|co\.uk|de|fr|es)/i,
  /@(google|youtube|googlemail)\.com/i,
  /@(adidas|newsletters?)[-./]/i,
  /@(mailchimp|mailerlite|brevo|sendgrid|klaviyo|constantcontact)\.com/i,
  /newsletter|e\.feedspot|academia-mail|serpapi|skool\.com|diib\.com|cloudflare\.com/i,
];

// ── State helpers ─────────────────────────────────────────────────────────────

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch {
    return { processedThreadIds: [] };
  }
}

function saveState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

// ── Sender parsing ────────────────────────────────────────────────────────────

/**
 * Returns { email, firstName, lastName, company } from a raw "From" header.
 * Examples handled:
 *   "John Doe <john@example.com>"
 *   "john@example.com"
 */
function parseSender(raw) {
  const emailMatch = raw.match(/<([^>]+)>/) ?? raw.match(/\S+@\S+/);
  if (!emailMatch) return null;

  const email = emailMatch[1] ?? emailMatch[0];
  const domain = email.split('@')[1] ?? '';

  // Try to extract display name
  const nameMatch = raw.match(/^"?([^<"]+?)"?\s*</);
  const displayName = nameMatch ? nameMatch[1].trim() : '';

  const nameParts = displayName ? displayName.split(/\s+/) : [];
  const firstName = nameParts[0] ?? localPartToFirstName(email);
  const lastName = nameParts.slice(1).join(' ') || undefined;

  const company = domainToCompany(domain);

  return { email: email.toLowerCase(), firstName, lastName, company, domain };
}

function localPartToFirstName(email) {
  const local = email.split('@')[0];
  // If local part looks like a real name (letters only), capitalise it
  if (/^[a-z]+$/i.test(local)) {
    return local.charAt(0).toUpperCase() + local.slice(1);
  }
  return local;
}

function domainToCompany(domain) {
  // Strip TLD and common subdomains to get a readable company name
  const base = domain
    .replace(/^(mail|news|m|smtp|send)\./i, '')
    .replace(/\.(com|it|io|net|org|co\.uk|es|de|fr|press|ai)$/i, '')
    .replace(/[-_]/g, ' ');
  return base
    .split(' ')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

function shouldSkip(email) {
  return SKIP_PATTERNS.some(p => p.test(email));
}

// ── Gmail helpers ─────────────────────────────────────────────────────────────

async function getGmailClient() {
  const credPath = process.env.GMAIL_CREDENTIALS_PATH;
  const tokPath = process.env.GMAIL_TOKEN_PATH;
  if (!credPath || !tokPath) {
    throw new Error('GMAIL_CREDENTIALS_PATH and GMAIL_TOKEN_PATH must be set');
  }

  const credentials = JSON.parse(fs.readFileSync(credPath, 'utf8'));
  const { client_secret, client_id, redirect_uris } = credentials.installed ?? credentials.web;
  const oAuth2 = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);
  oAuth2.setCredentials(JSON.parse(fs.readFileSync(tokPath, 'utf8')));
  return google.gmail({ version: 'v1', auth: oAuth2 });
}

async function fetchRecentThreads(gmail, lookbackDays, processedIds) {
  const since = new Date();
  since.setDate(since.getDate() - lookbackDays);
  const dateStr = `${since.getFullYear()}/${String(since.getMonth() + 1).padStart(2, '0')}/${String(since.getDate()).padStart(2, '0')}`;

  const threads = [];
  let pageToken;

  do {
    const res = await gmail.users.threads.list({
      userId: 'me',
      q: `in:inbox after:${dateStr} -from:me`,
      maxResults: 100,
      pageToken,
    });

    for (const t of res.data.threads ?? []) {
      if (!processedIds.has(t.id)) threads.push(t.id);
    }
    pageToken = res.data.nextPageToken;
  } while (pageToken);

  return threads;
}

async function getSenderFromThread(gmail, threadId) {
  const res = await gmail.users.threads.get({
    userId: 'me',
    id: threadId,
    format: 'metadata',
    metadataHeaders: ['From'],
  });

  const firstMsg = res.data.messages?.[0];
  const fromHeader = firstMsg?.payload?.headers?.find(h => h.name === 'From');
  return fromHeader?.value ?? null;
}

// ── HubSpot helpers ───────────────────────────────────────────────────────────

function getHubSpotClient() {
  const token = process.env.HUBSPOT_ACCESS_TOKEN;
  if (!token) throw new Error('HUBSPOT_ACCESS_TOKEN must be set');
  return new hubspot.Client({ accessToken: token });
}

async function findContact(hsClient, email) {
  const res = await hsClient.crm.contacts.searchApi.doSearch({
    filterGroups: [{
      filters: [{ propertyName: 'email', operator: 'EQ', value: email }],
    }],
    properties: ['email', 'firstname', 'lastname', 'company', 'hs_lead_source'],
    limit: 1,
  });
  return res.results?.[0] ?? null;
}

async function createContact(hsClient, contact) {
  const properties = {
    email: contact.email,
    firstname: contact.firstName,
    hs_lead_source: 'Gmail',
  };
  if (contact.lastName) properties.lastname = contact.lastName;
  if (contact.company) properties.company = contact.company;

  const res = await hsClient.crm.contacts.basicApi.create({ properties });
  return res.id;
}

async function updateContact(hsClient, id, contact, existing) {
  const properties = {};

  // Only fill in genuinely missing fields to avoid overwriting user data
  if (!existing.properties.lastname && contact.lastName) {
    properties.lastname = contact.lastName;
  }
  if (!existing.properties.company && contact.company) {
    properties.company = contact.company;
  }
  if (!existing.properties.hs_lead_source) {
    properties.hs_lead_source = 'Gmail';
  }

  if (Object.keys(properties).length === 0) return false;

  await hsClient.crm.contacts.basicApi.update(id, { properties });
  return true;
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function run() {
  const state = loadState();
  const processedIds = new Set(state.processedThreadIds);

  const gmail = await getGmailClient();
  const hsClient = getHubSpotClient();

  const threadIds = await fetchRecentThreads(gmail, LOOKBACK_DAYS, processedIds);
  console.log(`Found ${threadIds.length} new threads to process`);

  const results = [];

  for (const threadId of threadIds) {
    const rawFrom = await getSenderFromThread(gmail, threadId);
    if (!rawFrom) {
      processedIds.add(threadId);
      continue;
    }

    const sender = parseSender(rawFrom);
    if (!sender || shouldSkip(sender.email)) {
      processedIds.add(threadId);
      continue;
    }

    let status, contactId;

    try {
      const existing = await findContact(hsClient, sender.email);

      if (!existing) {
        contactId = await createContact(hsClient, sender);
        status = 'Creato';
      } else {
        contactId = existing.id;
        const updated = await updateContact(hsClient, existing.id, sender, existing);
        status = updated ? 'Aggiornato' : 'Ignorato';
      }
    } catch (err) {
      status = `Errore: ${err.message}`;
    }

    results.push({ status, email: sender.email, contactId: contactId ?? '-' });
    processedIds.add(threadId);
  }

  // Persist state – keep last 5 000 IDs to cap file size
  state.processedThreadIds = [...processedIds].slice(-5000);
  state.lastRun = new Date().toISOString();
  state.lastRunResults = results;
  saveState(state);

  // Print report
  console.log('\n── Risultati sincronizzazione ──────────────────────────────');
  console.log('Stato      | Email                              | ID HubSpot');
  console.log('-----------|------------------------------------|-----------');
  for (const r of results) {
    console.log(`${r.status.padEnd(10)} | ${r.email.padEnd(34)} | ${r.contactId}`);
  }
  console.log(`\nTotale: ${results.length} contatti processati`);
  console.log(`  Creati:     ${results.filter(r => r.status === 'Creato').length}`);
  console.log(`  Aggiornati: ${results.filter(r => r.status === 'Aggiornato').length}`);
  console.log(`  Ignorati:   ${results.filter(r => r.status === 'Ignorato').length}`);
  console.log(`  Errori:     ${results.filter(r => r.status.startsWith('Errore')).length}`);

  return results;
}

run().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
