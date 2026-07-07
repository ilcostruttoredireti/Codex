require('dotenv').config();
const { google } = require('googleapis');
const hubspot = require('@hubspot/api-client');

// ── Config ────────────────────────────────────────────────────────────────────

const LOOKBACK_DAYS = parseInt(process.env.LOOKBACK_DAYS || '1', 10);

// Prefixes and domains to skip — automated / no-reply senders
const SKIP_PREFIXES = new Set([
  'no-reply', 'noreply', 'dont-reply', 'do-not-reply', 'donotreply',
  'system', 'confirm', 'mailer', 'nobody', 'notify-noreply',
  'ads-noreply', 'adsense-noreply', 'admanager-noreply', 'updates-noreply',
  'googlebase-noreply', 'sc-noreply', 'sellersupport', 'accountservices',
  'payments-update', 'conferma-ordine', 'conferma-spedizione', 'premium',
]);

const SKIP_DOMAINS = new Set([
  'google.com', 'gmail.com', 'linkedin.com', 'amazon.com', 'amazon.it',
  'facebook.com', 'instagram.com', 'twitter.com', 'youtube.com',
  'mailchimp.com', 'revolut.com', 'cloudflare.com', 'serpapi.com',
  'tiktok.com', 'freemius.com', 'feedspot.com', 'academia-mail.com',
  'hubspot.com', 'notifications.hubspot.com',
]);

// ── Gmail ─────────────────────────────────────────────────────────────────────

function buildGmailClient() {
  const auth = new google.auth.OAuth2(
    process.env.GMAIL_CLIENT_ID,
    process.env.GMAIL_CLIENT_SECRET,
  );
  auth.setCredentials({ refresh_token: process.env.GMAIL_REFRESH_TOKEN });
  return google.gmail({ version: 'v1', auth });
}

async function fetchInboxSenders(gmail) {
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - LOOKBACK_DAYS);
  const after = Math.floor(cutoff.getTime() / 1000);

  const senders = new Map(); // email → { email, name, domain }
  let pageToken;

  do {
    const res = await gmail.users.threads.list({
      userId: 'me',
      q: `in:inbox -in:sent -in:draft after:${after}`,
      maxResults: 100,
      pageToken,
      fields: 'nextPageToken,threads(id)',
    });

    const threads = res.data.threads || [];

    await Promise.all(
      threads.map(async (t) => {
        const thread = await gmail.users.threads.get({
          userId: 'me',
          id: t.id,
          format: 'METADATA',
          metadataHeaders: ['From'],
        });
        for (const msg of thread.data.messages || []) {
          const fromHeader = (msg.payload.headers || []).find(
            (h) => h.name === 'From',
          );
          if (!fromHeader) continue;
          const parsed = parseSender(fromHeader.value);
          if (!parsed || shouldSkip(parsed.email)) continue;
          if (!senders.has(parsed.email)) senders.set(parsed.email, parsed);
        }
      }),
    );

    pageToken = res.data.nextPageToken;
  } while (pageToken);

  return [...senders.values()];
}

// Parse "Name <email>" or bare "email"
function parseSender(raw) {
  const match = raw.match(/^(?:"?([^"<]+)"?\s+)?<([^>]+)>/);
  if (match) {
    return buildContact(match[2].trim().toLowerCase(), match[1]?.trim());
  }
  const bare = raw.trim().toLowerCase();
  if (bare.includes('@')) return buildContact(bare, undefined);
  return null;
}

function buildContact(email, rawName) {
  const [localPart, domain] = email.split('@');
  const company = domainToCompany(domain);

  // Try to extract first/last from the display name
  let firstname, lastname;
  if (rawName && rawName.length > 0) {
    const parts = rawName.split(/\s+/);
    firstname = parts[0] || undefined;
    lastname = parts.slice(1).join(' ') || undefined;
  }

  // Fallback: capitalise the local part when it looks like a real name
  if (!firstname && /^[a-z]+$/.test(localPart)) {
    firstname = localPart.charAt(0).toUpperCase() + localPart.slice(1);
  }

  return { email, firstname, lastname, domain, company };
}

function domainToCompany(domain) {
  // Strip subdomains (e.g. ag.miraconsulting.it → miraconsulting.it)
  const parts = domain.split('.');
  const base = parts.length > 2 ? parts.slice(-2).join('.') : domain;
  // Remove TLD and capitalise
  const name = base.split('.')[0];
  return name.charAt(0).toUpperCase() + name.slice(1);
}

function shouldSkip(email) {
  const [local, domain] = email.split('@');
  if (!domain) return true;
  if (SKIP_DOMAINS.has(domain)) return true;
  if (SKIP_PREFIXES.has(local)) return true;
  // Skip if local part starts with a skip prefix followed by - or +
  for (const prefix of SKIP_PREFIXES) {
    if (local.startsWith(prefix + '-') || local.startsWith(prefix + '+')) return true;
  }
  return false;
}

// ── HubSpot ───────────────────────────────────────────────────────────────────

function buildHubSpotClient() {
  return new hubspot.Client({ accessToken: process.env.HUBSPOT_ACCESS_TOKEN });
}

async function findExistingContacts(hs, emails) {
  if (emails.length === 0) return new Map();

  const results = await hs.crm.contacts.searchApi.doSearch({
    filterGroups: [
      {
        filters: [
          { propertyName: 'email', operator: 'IN', values: emails },
        ],
      },
    ],
    properties: ['email', 'firstname', 'lastname', 'company', 'hs_lead_source'],
    limit: 200,
  });

  const map = new Map();
  for (const contact of results.results || []) {
    map.set(contact.properties.email, contact);
  }
  return map;
}

async function upsertContact(hs, sender, existing) {
  const props = {
    hs_lead_source: 'Gmail',
    // Only set these when creating or if empty in HubSpot
    ...(sender.company && { company: sender.company }),
  };

  if (!existing) {
    // Create new contact
    if (sender.firstname) props.firstname = sender.firstname;
    if (sender.lastname) props.lastname = sender.lastname;
    props.email = sender.email;

    const created = await hs.crm.contacts.basicApi.create({
      properties: props,
      associations: [],
    });
    return { status: 'Creato', id: created.id };
  }

  // Update: only fill missing fields
  const updates = {};
  const ep = existing.properties;

  if (!ep.firstname && sender.firstname) updates.firstname = sender.firstname;
  if (!ep.lastname && sender.lastname) updates.lastname = sender.lastname;
  if (!ep.company && sender.company) updates.company = sender.company;
  if (!ep.hs_lead_source) updates.hs_lead_source = 'Gmail';

  if (Object.keys(updates).length === 0) {
    return { status: 'Ignorato', id: existing.id };
  }

  await hs.crm.contacts.basicApi.update(existing.id, { properties: updates });
  return { status: 'Aggiornato', id: existing.id };
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  console.log(`[Gmail→HubSpot Sync] Scanning last ${LOOKBACK_DAYS} day(s)…\n`);

  const gmail = buildGmailClient();
  const hs = buildHubSpotClient();

  const senders = await fetchInboxSenders(gmail);
  console.log(`Found ${senders.length} unique meaningful senders.\n`);

  const emails = senders.map((s) => s.email);
  const existing = await findExistingContacts(hs, emails);

  const results = [];

  for (const sender of senders) {
    try {
      const result = await upsertContact(hs, sender, existing.get(sender.email));
      results.push({ ...result, email: sender.email });
      console.log(`[${result.status}] ${sender.email} → ID ${result.id}`);
    } catch (err) {
      console.error(`[Errore] ${sender.email}: ${err.message}`);
      results.push({ status: 'Errore', email: sender.email, id: null });
    }
  }

  // Summary
  const counts = { Creato: 0, Aggiornato: 0, Ignorato: 0, Errore: 0 };
  for (const r of results) counts[r.status] = (counts[r.status] || 0) + 1;

  console.log('\n── Riepilogo ────────────────────────────────────────────');
  console.log(`  Creati:     ${counts.Creato}`);
  console.log(`  Aggiornati: ${counts.Aggiornato}`);
  console.log(`  Ignorati:   ${counts.Ignorato}`);
  if (counts.Errore) console.log(`  Errori:     ${counts.Errore}`);
  console.log('─────────────────────────────────────────────────────────\n');

  return results;
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
