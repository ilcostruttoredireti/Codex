/**
 * Gmail → HubSpot Contact Sync
 *
 * Per ogni email in arrivo:
 *  - estrae email, nome, dominio del mittente
 *  - cerca il contatto in HubSpot per email (chiave unica)
 *  - crea il contatto se non esiste, aggiorna i campi mancanti se esiste
 *
 * Uso:
 *   node src/sync.js              # esegue il sync
 *   node src/sync.js --dry-run   # simula senza scrivere su HubSpot
 */

import { google } from 'googleapis';
import hubspot from '@hubspot/api-client';
import 'dotenv/config';

const DRY_RUN = process.argv.includes('--dry-run');
const LOOKBACK_HOURS = parseInt(process.env.LOOKBACK_HOURS ?? '24', 10);

// Mittenti automatici da ignorare (noreply, notifiche, newsletter di sistema)
const SKIP_PATTERNS = [
  /^no-?reply@/i,
  /^noreply@/i,
  /^notifications?@/i,
  /^notifications?-/i,
  /^do-?not-?reply@/i,
  /^mailer-daemon@/i,
  /^postmaster@/i,
  /^bounce@/i,
  /^support@(?:google|youtube|facebook|twitter|instagram|linkedin|microsoft|apple)\.com$/i,
  /@(?:youtube|facebook|twitter|instagram|linkedin|microsoft|apple|amazon|tiktok|skool|academia-mail)\.com$/i,
  /^(?:notify|admanager|googlebase|sc)-noreply@google\.com$/i,
  /^accountservices@mailchimp\.com$/i,
  /^premium@academia-mail\.com$/i,
];

function shouldSkip(email) {
  return SKIP_PATTERNS.some((re) => re.test(email));
}

/**
 * Estrae nome, cognome e azienda dall'indirizzo mittente.
 * Il campo "From" può avere la forma:
 *   "Nome Cognome <email@dominio.com>"  oppure semplicemente  "email@dominio.com"
 */
function parseSender(rawFrom, emailAddress) {
  const nameMatch = rawFrom.match(/^"?([^"<]+)"?\s*</);
  const displayName = nameMatch ? nameMatch[1].trim() : '';

  const [localPart, domain] = emailAddress.split('@');
  const domainRoot = domain.split('.').slice(0, -1).join(' ');

  // Se il displayName è uguale all'email o vuoto, proviamo a ricavarlo dalla local-part
  let firstName = '';
  let lastName = '';

  if (displayName && displayName !== emailAddress) {
    const parts = displayName.split(/\s+/);
    firstName = parts[0] ?? '';
    lastName = parts.slice(1).join(' ');
  } else {
    // Nessun display name: ricaviamo il nome dalla local-part
    const cleaned = localPart.replace(/[._-]/g, ' ').replace(/\d+/g, '').trim();
    const parts = cleaned.split(/\s+/).filter(Boolean);
    firstName = parts[0] ?? '';
    lastName = parts.slice(1).join(' ');
  }

  // Azienda: usiamo la parte principale del dominio come fallback
  const company = domainRoot
    .split(' ')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');

  return { firstName, lastName, company, domain };
}

// ── Gmail ──────────────────────────────────────────────────────────────────

async function getGmailClient() {
  const auth = new google.auth.OAuth2(
    process.env.GMAIL_CLIENT_ID,
    process.env.GMAIL_CLIENT_SECRET,
  );
  auth.setCredentials({ refresh_token: process.env.GMAIL_REFRESH_TOKEN });
  return google.gmail({ version: 'v1', auth });
}

/**
 * Recupera i messaggi in arrivo nelle ultime LOOKBACK_HOURS ore.
 * Restituisce un array di oggetti { emailAddress, rawFrom }.
 */
async function fetchInboundSenders(gmail) {
  const afterDate = Math.floor((Date.now() - LOOKBACK_HOURS * 3600 * 1000) / 1000);
  const res = await gmail.users.messages.list({
    userId: 'me',
    q: `in:inbox -from:me after:${afterDate}`,
    maxResults: 200,
  });

  const messages = res.data.messages ?? [];
  const senderMap = new Map(); // email → rawFrom (dedup per email)

  for (const msg of messages) {
    const detail = await gmail.users.messages.get({
      userId: 'me',
      id: msg.id,
      format: 'metadata',
      metadataHeaders: ['From'],
    });

    const fromHeader = detail.data.payload?.headers?.find((h) => h.name === 'From')?.value ?? '';
    const emailMatch = fromHeader.match(/<([^>]+)>/) ?? fromHeader.match(/(\S+@\S+)/);
    if (!emailMatch) continue;

    const emailAddress = emailMatch[1].toLowerCase().trim();
    if (!senderMap.has(emailAddress)) {
      senderMap.set(emailAddress, fromHeader);
    }
  }

  return Array.from(senderMap.entries()).map(([emailAddress, rawFrom]) => ({
    emailAddress,
    rawFrom,
  }));
}

// ── HubSpot ────────────────────────────────────────────────────────────────

function getHubSpotClient() {
  return new hubspot.Client({ accessToken: process.env.HUBSPOT_ACCESS_TOKEN });
}

async function findContact(client, email) {
  try {
    const res = await client.crm.contacts.searchApi.doSearch({
      filterGroups: [
        {
          filters: [{ propertyName: 'email', operator: 'EQ', value: email }],
        },
      ],
      properties: ['email', 'firstname', 'lastname', 'company', 'hs_analytics_source'],
      limit: 1,
    });
    return res.results[0] ?? null;
  } catch {
    return null;
  }
}

async function createContact(client, { emailAddress, firstName, lastName, company }) {
  const props = {
    email: emailAddress,
    lead_source: 'Gmail',
    hs_analytics_source: 'OTHER_CAMPAIGNS',
  };
  if (firstName) props.firstname = firstName;
  if (lastName) props.lastname = lastName;
  if (company) props.company = company;

  const res = await client.crm.contacts.basicApi.create({ properties: props });
  return res.id;
}

async function updateContact(client, contactId, existing, { firstName, lastName, company }) {
  const updates = {};

  if (firstName && !existing.properties.firstname) updates.firstname = firstName;
  if (lastName && !existing.properties.lastname) updates.lastname = lastName;
  if (company && !existing.properties.company) updates.company = company;
  if (!existing.properties.lead_source) updates.lead_source = 'Gmail';

  if (Object.keys(updates).length === 0) return false;

  await client.crm.contacts.basicApi.update(contactId, { properties: updates });
  return true;
}

// ── Main ───────────────────────────────────────────────────────────────────

async function run() {
  console.log(`\n🔄  Gmail → HubSpot Sync  [${new Date().toISOString()}]`);
  console.log(`    Lookback: ${LOOKBACK_HOURS}h | Dry-run: ${DRY_RUN}\n`);

  const gmail = await getGmailClient();
  const hubClient = getHubSpotClient();

  const senders = await fetchInboundSenders(gmail);
  console.log(`📬  Mittenti unici trovati: ${senders.length}`);

  const results = [];

  for (const { emailAddress, rawFrom } of senders) {
    if (shouldSkip(emailAddress)) {
      results.push({ email: emailAddress, status: 'Ignorato', hubspotId: null });
      continue;
    }

    const { firstName, lastName, company } = parseSender(rawFrom, emailAddress);
    const existing = await findContact(hubClient, emailAddress);

    if (existing) {
      const updated = DRY_RUN
        ? true
        : await updateContact(hubClient, existing.id, existing, { firstName, lastName, company });
      results.push({
        email: emailAddress,
        status: updated ? 'Aggiornato' : 'Ignorato',
        hubspotId: existing.id,
      });
    } else {
      let newId = null;
      if (!DRY_RUN) {
        newId = await createContact(hubClient, { emailAddress, firstName, lastName, company });
      }
      results.push({ email: emailAddress, status: 'Creato', hubspotId: newId ?? 'DRY_RUN' });
    }
  }

  // Stampa report
  console.log('\n📊  Risultati:\n');
  console.log('  Stato      │ Email                              │ HubSpot ID');
  console.log('  ───────────┼────────────────────────────────────┼─────────────────');
  for (const r of results) {
    const s = r.status.padEnd(10);
    const e = r.email.padEnd(35);
    const id = r.hubspotId ?? '—';
    console.log(`  ${s} │ ${e} │ ${id}`);
  }

  const counts = results.reduce((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});
  console.log(
    `\n  Creati: ${counts.Creato ?? 0}  |  Aggiornati: ${counts.Aggiornato ?? 0}  |  Ignorati: ${counts.Ignorato ?? 0}\n`,
  );

  return results;
}

run().catch((err) => {
  console.error('Errore durante il sync:', err.message);
  process.exit(1);
});
