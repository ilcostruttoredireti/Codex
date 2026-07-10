/**
 * Parses a raw "From" header like:
 *   "John Doe <john.doe@example.com>"
 *   "john.doe@example.com"
 *   "Example Support <support@example.com>"
 */
export function parseSender(rawFrom) {
  if (!rawFrom) return null;

  const angleMatch = rawFrom.match(/^(.*?)\s*<([^>]+)>$/);
  if (angleMatch) {
    const displayName = angleMatch[1].replace(/^["']|["']$/g, '').trim();
    const email = angleMatch[2].trim().toLowerCase();
    return { email, displayName };
  }

  const email = rawFrom.trim().toLowerCase();
  if (!email.includes('@')) return null;
  return { email, displayName: '' };
}

/**
 * Splits a display name into first/last name.
 * Handles:
 *   "John Doe"        → { first: "John", last: "Doe" }
 *   "Support"         → { first: "Support", last: "" }
 *   "SEOZoom Tool"    → { first: "SEOZoom", last: "Tool" }
 */
export function splitName(displayName, email) {
  if (displayName) {
    const parts = displayName.split(/\s+/);
    if (parts.length >= 2) {
      return { firstName: parts[0], lastName: parts.slice(1).join(' ') };
    }
    return { firstName: parts[0], lastName: '' };
  }

  // Derive a name from the local part of the email
  const local = email.split('@')[0];
  const cleaned = local.replace(/[._+-]/g, ' ').replace(/\b\w/g, c => c.toUpperCase()).trim();
  const parts = cleaned.split(/\s+/);
  return {
    firstName: parts[0] || '',
    lastName: parts.slice(1).join(' ') || '',
  };
}

/**
 * Extracts a company name from an email domain.
 *   "raffaprivatejet.com" → "Raffaprivatejet"
 *   "startup-italia.eu"   → "Startup Italia"
 */
export function companyFromDomain(domain) {
  const root = domain.split('.')[0]; // drop TLD
  return root
    .replace(/-/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

/**
 * Returns true if the email address looks like an automated sender.
 */
export function isAutomated(email, skipPatterns) {
  const lower = email.toLowerCase();
  return skipPatterns.some(pattern => lower.includes(pattern));
}

/**
 * Builds a structured contact object from a raw Gmail message.
 * Returns null if the sender should be skipped.
 */
export function extractContact(message, skipPatterns) {
  const from = message.sender || '';
  const parsed = parseSender(from);
  if (!parsed) return null;
  if (isAutomated(parsed.email, skipPatterns)) return null;

  const domain = parsed.email.split('@')[1] || '';
  const { firstName, lastName } = splitName(parsed.displayName, parsed.email);
  const company = companyFromDomain(domain);

  return {
    email: parsed.email,
    firstName,
    lastName,
    company,
    domain,
    leadSource: 'Gmail',
    rawFrom: from,
    messageId: message.id,
    receivedAt: message.date,
  };
}
