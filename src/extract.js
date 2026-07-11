'use strict';

/**
 * Parses a raw "From" header value like:
 *   "John Smith <john@example.com>"  →  { name: "John Smith", email: "john@example.com" }
 *   "john@example.com"               →  { name: null, email: "john@example.com" }
 */
function parseFromHeader(raw) {
  if (!raw) return { name: null, email: null };

  const angleMatch = raw.match(/^(.*?)\s*<([^>]+)>/);
  if (angleMatch) {
    const name = angleMatch[1].trim().replace(/^["']|["']$/g, '') || null;
    const email = angleMatch[2].trim().toLowerCase();
    return { name: name || null, email };
  }

  const email = raw.trim().toLowerCase();
  return { name: null, email };
}

/**
 * Splits a display name into first/last name parts.
 * Falls back to using the email local part when no name is available.
 */
function splitName(name, email) {
  if (name) {
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
      return { firstName: parts[0], lastName: parts.slice(1).join(' ') };
    }
    return { firstName: parts[0], lastName: '' };
  }

  // Derive a readable name from the email local part
  const local = email.split('@')[0].replace(/[._-]/g, ' ');
  const capitalized = local.replace(/\b\w/g, c => c.toUpperCase());
  return { firstName: capitalized, lastName: '' };
}

/**
 * Derives a company name from the email domain.
 * e.g. "raffaprivatejet.com" → "Raffaprivatejet"
 *      "startupitalia.eu"    → "Startupitalia"
 */
function companyFromDomain(domain) {
  const parts = domain.split('.');
  // Drop TLD and common subdomains like 'mail', 'email', 'info'
  const filtered = parts.filter(p => !['mail', 'email', 'info', 'marketing', 'send', 'news'].includes(p));
  const base = filtered[0] || parts[0];
  return base.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

/**
 * Extracts a structured contact record from a Gmail message.
 * @param {{ from: string, subject: string, messageId: string, date: string }} msg
 * @returns {{ email, firstName, lastName, company, domain, subject, messageId, date } | null}
 */
function extractContact(msg) {
  const { name, email } = parseFromHeader(msg.from);
  if (!email) return null;

  const domain = email.split('@')[1];
  const { firstName, lastName } = splitName(name, email);
  const company = companyFromDomain(domain);

  return {
    email,
    firstName,
    lastName,
    company,
    domain,
    subject: msg.subject || '',
    messageId: msg.messageId,
    date: msg.date,
  };
}

module.exports = { extractContact, parseFromHeader, splitName, companyFromDomain };
