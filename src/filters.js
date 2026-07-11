'use strict';

// Local parts that indicate automated/system senders
const AUTOMATED_LOCAL_PARTS = new Set([
  'no-reply', 'noreply', 'donotreply', 'do-not-reply',
  'notify-noreply', 'noreply-accounts', 'mailer-daemon',
  'postmaster', 'daemon', 'bounce', 'bounces', 'auto-reply',
  'autoresponder', 'notifications', 'alerts', 'system',
  'confirm', 'support', 'help', 'info-noreply', 'pinbot',
  'messaging-digest-noreply',
]);

// Domains that are always automated (platforms, CDNs, transactional services)
const AUTOMATED_DOMAINS = new Set([
  'youtube.com', 'google.com', 'linkedin.com', 'discord.com',
  'mailchimp.com', 'canva.com', 'bybit.com', 'openai.com',
  'vercel.com', 'facebook.com', 'twitter.com', 'instagram.com',
  'pinterest.com', 'tiktok.com', 'slack.com', 'notion.so',
  'github.com', 'gitlab.com', 'atlassian.com', 'jira.com',
  'stripe.com', 'paypal.com', 'apple.com', 'microsoft.com',
]);

/**
 * Returns true if the sender email should be skipped (automated/system).
 * @param {string} email
 * @returns {boolean}
 */
function isAutomated(email) {
  if (!email || !email.includes('@')) return true;

  const [local, domain] = email.toLowerCase().split('@');

  // Skip if domain is in the automated list or is a subdomain of one
  if (AUTOMATED_DOMAINS.has(domain)) return true;
  for (const d of AUTOMATED_DOMAINS) {
    if (domain.endsWith('.' + d)) return true;
  }

  // Skip if the local part matches known automated patterns
  if (AUTOMATED_LOCAL_PARTS.has(local)) return true;
  if (local.startsWith('no-reply') || local.startsWith('noreply')) return true;
  if (local.startsWith('bounce') || local.startsWith('mailer-daemon')) return true;

  return false;
}

module.exports = { isAutomated };
