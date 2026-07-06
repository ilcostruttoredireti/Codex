'use strict';

// Email prefixes that indicate automated/system senders with no real person behind them
const AUTOMATED_PREFIXES = [
  'noreply', 'no-reply', 'do-not-reply', 'donotreply',
  'nobody', 'mailer', 'daemon', 'bounce', 'postmaster',
  'automailer', 'automated', 'notifications',
  'confirm', 'confirmation', 'alerts', 'alert',
  'newsletter', 'news', 'updates', 'system',
  'conferma-', 'conferma_', 'payments-', 'payments_',
  'adsense-', 'ads-', 'invoice', 'billing',
];

// Domains of large platforms where sender addresses carry no CRM value
const PLATFORM_DOMAINS = [
  'amazon.it', 'amazon.com', 'amazon.co.uk', 'amazon.de',
  'google.com', 'googlemail.com', 'gmail.com',
  'tiktok.com', 'instagram.com', 'facebook.com', 'twitter.com', 'x.com',
  'linkedin.com', 'youtube.com',
  'mailchimp.com', 'sendgrid.net', 'klaviyo.com', 'brevo.com',
  'hubspot.com', 'salesforce.com',
  'stripe.com', 'paypal.com',
  'cloudflare.com', 'algolia.com',
  'circle.so', 'skool.com', 'academia.edu',
  'serpapi.com', 'feedspot.com',
  'notification.circle.so',
];

function isAutomatedSender(email) {
  if (!email) return true;
  const [prefix, domain] = email.toLowerCase().split('@');
  if (!domain) return true;

  if (PLATFORM_DOMAINS.some(d => domain === d || domain.endsWith('.' + d))) {
    return true;
  }

  if (AUTOMATED_PREFIXES.some(p => prefix.startsWith(p))) {
    return true;
  }

  return false;
}

// Parse "First Last <email@domain.com>" or plain "email@domain.com"
function parseSender(senderHeader) {
  if (!senderHeader) return { email: null, displayName: null };

  const angleMatch = senderHeader.match(/^(.+?)\s*<([^>]+)>\s*$/);
  if (angleMatch) {
    return {
      email: angleMatch[2].trim().toLowerCase(),
      displayName: angleMatch[1].trim().replace(/^["']|["']$/g, ''),
    };
  }

  const emailMatch = senderHeader.match(/[^\s]+@[^\s]+/);
  if (emailMatch) {
    return { email: emailMatch[0].toLowerCase(), displayName: null };
  }

  return { email: null, displayName: null };
}

// Split a display name into first/last. Falls back to email prefix.
function splitName(displayName, emailPrefix) {
  if (displayName) {
    const parts = displayName.trim().split(/\s+/);
    if (parts.length >= 2) {
      return { firstname: parts[0], lastname: parts.slice(1).join(' ') };
    }
    return { firstname: displayName.trim(), lastname: null };
  }

  // Use email prefix as first name if no display name
  const name = emailPrefix.replace(/[._-]/g, ' ').trim();
  const parts = name.split(/\s+/);
  if (parts.length >= 2) {
    return {
      firstname: parts[0].charAt(0).toUpperCase() + parts[0].slice(1),
      lastname: parts.slice(1).map(p => p.charAt(0).toUpperCase() + p.slice(1)).join(' '),
    };
  }
  return {
    firstname: name.charAt(0).toUpperCase() + name.slice(1),
    lastname: null,
  };
}

// Derive a human-readable company name from a domain
function companyFromDomain(domain) {
  // Strip www. and common TLD patterns
  const base = domain.replace(/^www\./, '').replace(/\.(it|com|org|net|io|co|eu|uk|de|fr|es)(\..+)?$/, '');
  // Convert hyphens/dots to spaces and title-case
  return base
    .split(/[-.]/)
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

// Extract all fields from a sender email address
function extractContactFields(senderHeader) {
  const { email, displayName } = parseSender(senderHeader);
  if (!email || isAutomatedSender(email)) return null;

  const [prefix, domain] = email.split('@');
  const { firstname, lastname } = splitName(displayName, prefix);

  return {
    email,
    firstname,
    lastname,
    company: companyFromDomain(domain),
    domain,
  };
}

module.exports = { extractContactFields, isAutomatedSender };
