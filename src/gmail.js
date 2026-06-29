const { google } = require('googleapis');

function buildGmailClient() {
  const auth = new google.auth.OAuth2(
    process.env.GMAIL_CLIENT_ID,
    process.env.GMAIL_CLIENT_SECRET
  );
  auth.setCredentials({ refresh_token: process.env.GMAIL_REFRESH_TOKEN });
  return google.gmail({ version: 'v1', auth });
}

// Parse "Name <email>" or "email" format
function parseSender(from) {
  if (!from) return null;
  const match = from.match(/^"?([^"<]+?)"?\s*<([^>]+)>$/);
  if (match) {
    const fullName = match[1].trim();
    const email = match[2].trim().toLowerCase();
    const parts = fullName.split(/\s+/);
    return {
      email,
      firstName: parts[0] || null,
      lastName: parts.length > 1 ? parts.slice(1).join(' ') : null,
      fullName,
    };
  }
  const emailOnly = from.trim().toLowerCase();
  if (emailOnly.includes('@')) {
    return { email: emailOnly, firstName: null, lastName: null, fullName: null };
  }
  return null;
}

// Extract "Da: Name <email>" lines from Italian forwarded email bodies
function extractForwardedSender(body) {
  if (!body) return null;
  // Match "Da: ..." or "From: ..." in forwarded Italian/English emails
  const match = body.match(/(?:^|\n)Da:\s*(?:"?([^"\n<]+?)"?\s*)?[<]?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})[>]?/m);
  if (!match) {
    const enMatch = body.match(/(?:^|\n)From:\s*(?:"?([^"\n<]+?)"?\s*)?[<]?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})[>]?/m);
    if (!enMatch) return null;
    return parseSenderFromParts(enMatch[1], enMatch[2]);
  }
  return parseSenderFromParts(match[1], match[2]);
}

function parseSenderFromParts(namePart, email) {
  if (!email) return null;
  const cleanEmail = email.trim().toLowerCase();
  if (!namePart) return { email: cleanEmail, firstName: null, lastName: null, fullName: null };
  const fullName = namePart.trim();
  const parts = fullName.split(/\s+/);
  return {
    email: cleanEmail,
    firstName: parts[0] || null,
    lastName: parts.length > 1 ? parts.slice(1).join(' ') : null,
    fullName,
  };
}

// Extract company name from email domain (skip personal/generic providers)
const GENERIC_DOMAINS = new Set([
  'gmail.com', 'yahoo.com', 'yahoo.it', 'libero.it', 'virgilio.it',
  'hotmail.com', 'outlook.com', 'icloud.com', 'me.com', 'live.it',
  'tiscali.it', 'alice.it', 'tin.it', 'fastwebnet.it',
]);

function companyFromDomain(email) {
  if (!email) return null;
  const domain = email.split('@')[1];
  if (!domain || GENERIC_DOMAINS.has(domain)) return null;
  // Strip common subdomains (ufficio.stampa.xxx → xxx)
  const parts = domain.split('.');
  const tldCount = parts[parts.length - 2] === 'mc' || parts[parts.length - 2] === 'co' ? 3 : 2;
  const meaningful = parts.slice(0, parts.length - tldCount + 1);
  return meaningful.join('.').replace(/[-_]/g, ' ');
}

const SKIP_SENDERS = new Set([
  'noreply', 'no-reply', 'notification', 'notifications',
  'mailer-daemon', 'postmaster', 'bounce', 'donotreply',
]);

function shouldSkipEmail(email) {
  if (!email) return true;
  const local = email.split('@')[0].toLowerCase();
  return SKIP_SENDERS.has(local) || local.startsWith('no-reply') || local.includes('noreply');
}

async function fetchRecentThreads(gmail, lookbackHours = 24) {
  const after = Math.floor((Date.now() - lookbackHours * 3600 * 1000) / 1000);
  const res = await gmail.users.threads.list({
    userId: 'me',
    q: `in:inbox after:${after}`,
    maxResults: 100,
  });

  const threads = res.data.threads || [];
  const results = [];

  for (const thread of threads) {
    const threadData = await gmail.users.threads.get({
      userId: 'me',
      id: thread.id,
      format: 'full',
    });

    for (const msg of threadData.data.messages || []) {
      const headers = msg.payload?.headers || [];
      const fromHeader = headers.find(h => h.name.toLowerCase() === 'from')?.value;

      // Get plain text body for forwarded sender extraction
      let body = '';
      const parts = msg.payload?.parts || [];
      const textPart = parts.find(p => p.mimeType === 'text/plain');
      if (textPart?.body?.data) {
        body = Buffer.from(textPart.body.data, 'base64').toString('utf8');
      } else if (msg.payload?.body?.data) {
        body = Buffer.from(msg.payload.body.data, 'base64').toString('utf8');
      }

      // Primary sender from From header
      const directSender = parseSender(fromHeader);
      if (directSender && !shouldSkipEmail(directSender.email)) {
        results.push({ ...directSender, source: 'direct' });
      }

      // Also extract original sender from forwarded emails
      const fwdSender = extractForwardedSender(body);
      if (fwdSender && !shouldSkipEmail(fwdSender.email)) {
        results.push({ ...fwdSender, source: 'forwarded' });
      }
    }
  }

  return results;
}

module.exports = { buildGmailClient, fetchRecentThreads, companyFromDomain, shouldSkipEmail };
