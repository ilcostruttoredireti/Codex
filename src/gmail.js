import { google } from 'googleapis';

const SKIP_PATTERNS = [
  /^no-?reply@/i,
  /^noreply@/i,
  /^mailer@/i,
  /^postmaster@/i,
  /^bounce@/i,
  /^notifications?@/i,
  /^alerts?@/i,
  /^security@/i,
  /^newsletter@/i,
  /^unsubscribe@/i,
  /^autorespond@/i,
  /^donotreply@/i,
  /^do-not-reply@/i,
  /@(facebookmail|googlemail|accounts\.google|noreply\.|notifications?\.)/.test,
  /^(cloudplatform|googleplay|sc|admanager)-noreply@/i,
];

const SKIP_DOMAINS = new Set([
  'facebookmail.com',
  'accounts.google.com',
  'googlemail.com',
]);

export function createGmailClient({ clientId, clientSecret, refreshToken }) {
  const auth = new google.auth.OAuth2(clientId, clientSecret);
  auth.setCredentials({ refresh_token: refreshToken });
  return google.gmail({ version: 'v1', auth });
}

export async function fetchRecentSenders(gmail, { lookbackDays = 1 } = {}) {
  const after = new Date();
  after.setDate(after.getDate() - lookbackDays);
  const afterUnix = Math.floor(after.getTime() / 1000);

  const senders = new Map();
  let pageToken;

  do {
    const res = await gmail.users.messages.list({
      userId: 'me',
      q: `in:inbox -from:me after:${afterUnix}`,
      maxResults: 500,
      pageToken,
    });

    const messages = res.data.messages || [];
    pageToken = res.data.nextPageToken;

    await Promise.all(
      messages.map(async ({ id }) => {
        try {
          const msg = await gmail.users.messages.get({
            userId: 'me',
            id,
            format: 'metadata',
            metadataHeaders: ['From', 'Date'],
          });

          const fromHeader = msg.data.payload.headers.find(h => h.name === 'From')?.value;
          if (!fromHeader) return;

          const parsed = parseSender(fromHeader);
          if (!parsed) return;

          if (!senders.has(parsed.email)) {
            senders.set(parsed.email, { ...parsed, messageId: id });
          }
        } catch {
          // skip inaccessible messages
        }
      })
    );
  } while (pageToken);

  return [...senders.values()];
}

export function parseSender(fromHeader) {
  // "Name Surname <email@domain.com>" or "email@domain.com"
  const angleMatch = fromHeader.match(/^(.*?)\s*<([^>]+)>/);
  let displayName = '';
  let email = '';

  if (angleMatch) {
    displayName = angleMatch[1].trim().replace(/^["']|["']$/g, '');
    email = angleMatch[2].trim().toLowerCase();
  } else {
    email = fromHeader.trim().toLowerCase();
  }

  if (!isValidEmail(email) || shouldSkip(email)) return null;

  const [localPart, domain] = email.split('@');
  const { firstname, lastname } = extractName(displayName, localPart);
  const company = inferCompany(domain, displayName);

  return { email, firstname, lastname, company, domain };
}

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

function shouldSkip(email) {
  const [, domain] = email.split('@');
  if (SKIP_DOMAINS.has(domain)) return true;
  return SKIP_PATTERNS.some(p =>
    typeof p === 'function' ? p(email) : p.test(email)
  );
}

function extractName(displayName, localPart) {
  if (displayName && displayName.length > 1 && !/^(info|support|contact|press|redazione|newsletter|sales|admin|hello|team)$/i.test(displayName)) {
    const parts = displayName.split(/\s+/);
    if (parts.length >= 2) {
      return { firstname: parts[0], lastname: parts.slice(1).join(' ') };
    }
    return { firstname: displayName, lastname: '' };
  }

  // fall back to parsing local part: "chelsea.c" → firstname=Chelsea, lastname=C
  const nameParts = localPart.split(/[._+-]/).filter(p => p.length > 1);
  if (nameParts.length >= 2) {
    return {
      firstname: capitalize(nameParts[0]),
      lastname: capitalize(nameParts.slice(1).join(' ')),
    };
  }
  if (nameParts.length === 1) {
    return { firstname: capitalize(nameParts[0]), lastname: '' };
  }
  return { firstname: '', lastname: '' };
}

function inferCompany(domain, displayName) {
  // strip common TLDs and format
  const knownGeneric = new Set(['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com', 'libero.it', 'virgilio.it']);
  if (knownGeneric.has(domain)) return '';

  // if display name looks like a company name (multiple words, not a person), use it
  if (displayName && /\s/.test(displayName) && !/^[A-Z][a-z]+ [A-Z][a-z]+$/.test(displayName)) {
    return displayName;
  }

  // derive from domain: "martes-ai.com" → "Martes AI", "rec-media.it" → "Rec Media"
  const base = domain.split('.').slice(0, -1).join('.');
  return base
    .split(/[-_]/)
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}
