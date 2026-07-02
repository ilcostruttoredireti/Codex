/**
 * Gmail → HubSpot Contact Sync
 *
 * Reads recent inbound Gmail messages, extracts unique human senders,
 * then creates or updates the corresponding contacts in HubSpot.
 * Runs as a Claude Code scheduled routine; requires:
 *   GMAIL_CREDENTIALS_PATH  – path to OAuth2 token JSON (gmail scope)
 *   HUBSPOT_ACCESS_TOKEN    – private-app or OAuth access token
 *
 * Output per email processed:
 *   { status: 'Created' | 'Updated' | 'Skipped', email, hubspotId }
 */

const { google } = require('googleapis');
const https = require('https');
const fs = require('fs');

// ── helpers ────────────────────────────────────────────────────────────────

const SKIP_PATTERNS = [
  /^no.?reply@/i,
  /^noreply@/i,
  /^notifications?@/i,
  /^notifica@/i,
  /^invoicing@/i,
  /^billing@/i,
  /^payments?.noreply@/i,
  /^system@/i,
  /^premium@/i,
  /^close_friend_updates@/i,
  /^hi@news\./i,
  /@facebookmail\.com$/i,
  /@discord\.com$/i,
  /@notification\./i,
  /@newsletter\./i,
  /@academia-mail\.com$/i,
];

function isAutomated(email) {
  return SKIP_PATTERNS.some((re) => re.test(email));
}

function parseSenderName(header) {
  // header may be "First Last <email>" or just "email"
  const match = header.match(/^(.+?)\s*<[^>]+>$/);
  return match ? match[1].replace(/"/g, '').trim() : '';
}

function domainToCompany(domain) {
  // strip TLD and capitalise
  return domain
    .replace(/\.(it|com|eu|org|net|io|ai)$/, '')
    .replace(/-/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── Gmail ──────────────────────────────────────────────────────────────────

async function getGmailClient() {
  const credPath = process.env.GMAIL_CREDENTIALS_PATH || './gmail-token.json';
  const credentials = JSON.parse(fs.readFileSync(credPath, 'utf8'));
  const { client_secret, client_id, redirect_uris, token } = credentials;
  const oAuth2Client = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);
  oAuth2Client.setCredentials(token);
  return google.gmail({ version: 'v1', auth: oAuth2Client });
}

async function fetchInboundSenders(daysBack = 7) {
  const gmail = await getGmailClient();
  const after = Math.floor(Date.now() / 1000) - daysBack * 86400;
  const res = await gmail.users.messages.list({
    userId: 'me',
    q: `in:inbox -from:me after:${after}`,
    maxResults: 100,
  });

  const messages = res.data.messages || [];
  const senders = new Map(); // email → { name, domain }

  for (const msg of messages) {
    const full = await gmail.users.messages.get({
      userId: 'me',
      id: msg.id,
      format: 'metadata',
      metadataHeaders: ['From'],
    });
    const fromHeader = full.data.payload.headers.find((h) => h.name === 'From')?.value || '';
    const emailMatch = fromHeader.match(/<([^>]+)>/) || fromHeader.match(/([^\s]+@[^\s]+)/);
    if (!emailMatch) continue;

    const email = emailMatch[1].toLowerCase().trim();
    if (isAutomated(email) || senders.has(email)) continue;

    const name = parseSenderName(fromHeader);
    const domain = email.split('@')[1];
    senders.set(email, { name, domain });
  }

  return senders;
}

// ── HubSpot ────────────────────────────────────────────────────────────────

function hubspotRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : undefined;
    const req = https.request(
      {
        hostname: 'api.hubapi.com',
        path,
        method,
        headers: {
          Authorization: `Bearer ${process.env.HUBSPOT_ACCESS_TOKEN}`,
          'Content-Type': 'application/json',
          ...(data ? { 'Content-Length': Buffer.byteLength(data) } : {}),
        },
      },
      (res) => {
        let raw = '';
        res.on('data', (c) => (raw += c));
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, body: JSON.parse(raw) });
          } catch {
            resolve({ status: res.statusCode, body: raw });
          }
        });
      }
    );
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

async function findContact(email) {
  const res = await hubspotRequest('POST', '/crm/v3/objects/contacts/search', {
    filterGroups: [{ filters: [{ propertyName: 'email', operator: 'EQ', value: email }] }],
    properties: ['email', 'firstname', 'lastname', 'company', 'hs_lead_source'],
    limit: 1,
  });
  if (res.status === 200 && res.body.total > 0) return res.body.results[0];
  return null;
}

async function createContact(email, name, domain) {
  const [firstname, ...rest] = name ? name.split(' ') : [email.split('@')[0]];
  const lastname = rest.join(' ');
  const company = domainToCompany(domain);

  const props = {
    email,
    firstname,
    ...(lastname ? { lastname } : {}),
    company,
    hs_analytics_source: 'EMAIL_MARKETING',
    hs_analytics_source_data_1: 'Gmail',
  };

  const res = await hubspotRequest('POST', '/crm/v3/objects/contacts', { properties: props });
  return res.body;
}

async function updateContact(id, name, domain, existing) {
  const updates = {};
  if (!existing.properties.company) updates.company = domainToCompany(domain);
  if (!existing.properties.hs_lead_source) updates.hs_lead_source = 'Gmail';
  if (name && !existing.properties.firstname) {
    const [firstname, ...rest] = name.split(' ');
    updates.firstname = firstname;
    if (rest.length) updates.lastname = rest.join(' ');
  }
  if (!Object.keys(updates).length) return existing;

  const res = await hubspotRequest('PATCH', `/crm/v3/objects/contacts/${id}`, {
    properties: updates,
  });
  return res.body;
}

async function addGmailTag(contactId) {
  await hubspotRequest('POST', `/crm/v3/objects/contacts/${contactId}/notes`, {
    properties: {
      hs_note_body: 'Email inbound ricevuta tramite Gmail',
      hs_timestamp: new Date().toISOString(),
    },
    associations: [
      {
        to: { id: contactId },
        types: [{ associationCategory: 'HUBSPOT_DEFINED', associationTypeId: 202 }],
      },
    ],
  });
}

// ── main ───────────────────────────────────────────────────────────────────

async function run() {
  console.log('Fetching inbound Gmail senders (last 7 days)…');
  const senders = await fetchInboundSenders(7);
  console.log(`Found ${senders.size} unique human senders.\n`);

  const results = [];

  for (const [email, { name, domain }] of senders) {
    const existing = await findContact(email);

    if (existing) {
      await updateContact(existing.id, name, domain, existing);
      results.push({ status: 'Updated', email, hubspotId: existing.id });
      console.log(`UPDATED  ${email} → #${existing.id}`);
    } else {
      const created = await createContact(email, name, domain);
      if (created.id) {
        await addGmailTag(created.id);
        results.push({ status: 'Created', email, hubspotId: created.id });
        console.log(`CREATED  ${email} → #${created.id}`);
      } else {
        results.push({ status: 'Error', email, hubspotId: null, detail: created });
        console.error(`ERROR    ${email}`, created);
      }
    }
  }

  console.log('\n── Summary ──────────────────────────────────────────');
  const created = results.filter((r) => r.status === 'Created').length;
  const updated = results.filter((r) => r.status === 'Updated').length;
  const errors = results.filter((r) => r.status === 'Error').length;
  console.log(`Created: ${created}  Updated: ${updated}  Errors: ${errors}`);
  return results;
}

run().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});
