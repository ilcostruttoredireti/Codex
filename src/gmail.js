'use strict';

const { google } = require('googleapis');

function buildOAuth2Client() {
  const client = new google.auth.OAuth2(
    process.env.GMAIL_CLIENT_ID,
    process.env.GMAIL_CLIENT_SECRET
  );
  client.setCredentials({ refresh_token: process.env.GMAIL_REFRESH_TOKEN });
  return client;
}

function buildGmailClient() {
  return google.gmail({ version: 'v1', auth: buildOAuth2Client() });
}

/**
 * List recent inbox messages, skipping sent/drafts.
 * @param {number} maxResults
 * @param {string|null} pageToken
 */
async function listInboxMessages(maxResults = 100, pageToken = null) {
  const gmail = buildGmailClient();
  const params = {
    userId: 'me',
    q: 'in:inbox -in:sent -in:draft',
    maxResults,
  };
  if (pageToken) params.pageToken = pageToken;

  const res = await gmail.users.messages.list(params);
  return res.data;
}

/**
 * Get minimal sender headers for a message ID.
 */
async function getMessageSender(messageId) {
  const gmail = buildGmailClient();
  const res = await gmail.users.messages.get({
    userId: 'me',
    id: messageId,
    format: 'metadata',
    metadataHeaders: ['From', 'Subject', 'Date'],
  });

  const headers = res.data.payload.headers;
  const getHeader = (name) =>
    (headers.find((h) => h.name.toLowerCase() === name.toLowerCase()) || {}).value || '';

  const from = getHeader('From');
  return {
    messageId,
    raw: from,
    subject: getHeader('Subject'),
    date: getHeader('Date'),
    ...parseFrom(from),
  };
}

/**
 * Parse "Firstname Lastname <email@domain.com>" or plain "email@domain.com".
 */
function parseFrom(from) {
  const match = from.match(/^"?([^"<]+?)"?\s*<([^>]+)>$/);
  if (match) {
    const fullName = match[1].trim();
    const email = match[2].trim().toLowerCase();
    const parts = fullName.split(/\s+/);
    return {
      email,
      firstName: parts[0] || '',
      lastName: parts.slice(1).join(' ') || '',
      fullName,
    };
  }
  const email = from.replace(/[<>]/g, '').trim().toLowerCase();
  return { email, firstName: '', lastName: '', fullName: '' };
}

/**
 * Extract company name from email domain.
 * Strips "mail.", "news.", "info.", etc. prefixes.
 */
function domainToCompany(email) {
  const domain = email.split('@')[1] || '';
  const stripped = domain
    .replace(/^(mail|news|no-reply|noreply|notify|marketing|support|info|hello|hi|accounts|account|ads|cdn|e)\./i, '')
    .replace(/\.(com|it|eu|io|ai|org|net|co|press|es)$/, '');
  return stripped
    .split(/[-.]/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

const NO_REPLY_PATTERNS = [
  /^no[-.]?reply/i,
  /^noreply/i,
  /^notify[-.]?noreply/i,
  /^postmaster/i,
  /^mailer-daemon/i,
  /^nobody@/i,
  /^bounce/i,
  /^donotreply/i,
  /^do[-.]not[-.]reply/i,
  /^auto[-.]?reply/i,
  /^daemon/i,
];

const SYSTEM_DOMAINS = new Set([
  'google.com',
  'googleapis.com',
  'accounts.google.com',
  'googlemail.com',
  'youtube.com',
  'discord.com',
  'skool.com',
  'academia-mail.com',
  'ebay.com',
  'moneya.es',
  'pinterest.com',
]);

/**
 * Returns true if the email is clearly automated and should be skipped.
 */
function isAutomatedSender(email) {
  const local = email.split('@')[0];
  const domain = email.split('@')[1] || '';

  if (SYSTEM_DOMAINS.has(domain)) return true;
  if (NO_REPLY_PATTERNS.some((re) => re.test(local))) return true;

  // Skip @google.com subdomains used for system mail
  if (domain.endsWith('.google.com') || domain === 'google.com') return true;
  if (domain.endsWith('.youtube.com') || domain === 'youtube.com') return true;

  return false;
}

module.exports = { listInboxMessages, getMessageSender, parseFrom, domainToCompany, isAutomatedSender };
