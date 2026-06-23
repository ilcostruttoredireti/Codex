const GENERIC_DOMAINS = new Set([
  'gmail.com', 'yahoo.com', 'yahoo.it', 'hotmail.com', 'hotmail.it',
  'outlook.com', 'outlook.it', 'libero.it', 'virgilio.it', 'tiscali.it',
  'icloud.com', 'me.com', 'mac.com', 'live.com', 'msn.com',
]);

const SKIP_PATTERNS = [
  'facebookmail.com', 'linkedin.com', 'twitter.com', 'instagram.com',
  'mailchimp.com', 'sendgrid.net', 'amazonses.com', 'mailgun.org',
  'bounce', 'mailer-daemon',
];

const SKIP_PREFIXES = [
  'noreply@', 'no-reply@', 'donotreply@', 'notification@',
  'mailer@', 'bounce@', 'postmaster@', 'daemon@', 'alert@',
  'pageupdates@', 'page@',
];

export function extractCompanyFromDomain(email) {
  const domain = email.split('@')[1];
  if (!domain || GENERIC_DOMAINS.has(domain)) return '';
  const parts = domain.split('.');
  return parts[0]
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

export function parseSenderName(displayName) {
  if (!displayName) return { firstname: '', lastname: '' };
  const clean = displayName.trim().replace(/^"|"$/g, '');
  const parts = clean.split(/\s+/);
  if (parts.length === 1) return { firstname: parts[0], lastname: '' };
  const lastname = parts.pop();
  return { firstname: parts.join(' '), lastname };
}

export function parseFromHeader(fromHeader) {
  if (!fromHeader) return null;
  const angleMatch = fromHeader.match(/^([^<]*)<([^>]+)>/);
  if (angleMatch) {
    const { firstname, lastname } = parseSenderName(angleMatch[1]);
    return { email: angleMatch[2].trim().toLowerCase(), firstname, lastname };
  }
  const emailMatch = fromHeader.match(/([^\s]+@[^\s]+)/);
  if (emailMatch) {
    return { email: emailMatch[1].toLowerCase(), firstname: '', lastname: '' };
  }
  return null;
}

export function shouldSkipEmail(email) {
  const lower = email.toLowerCase();
  if (SKIP_PREFIXES.some(p => lower.startsWith(p))) return true;
  if (SKIP_PATTERNS.some(p => lower.includes(p))) return true;
  return false;
}

export function buildContactData(parsed) {
  const company = parsed.company || extractCompanyFromDomain(parsed.email);
  return {
    email: parsed.email,
    firstname: parsed.firstname || '',
    lastname: parsed.lastname || '',
    company,
  };
}
