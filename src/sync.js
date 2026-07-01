/**
 * Gmail → HubSpot Contact Sync
 *
 * Monitora le email in arrivo su Gmail ed estrae i mittenti per
 * crearli/aggiornarli come contatti in HubSpot.
 *
 * Utilizzo (tramite MCP tools o integrazione diretta):
 *   node src/sync.js
 *
 * Questo file documenta la logica implementata anche tramite gli MCP tools
 * Gmail e HubSpot di Claude Code.
 */

// ─── Configurazione ──────────────────────────────────────────────────────────

/** Prefissi email da ignorare (mittenti automatici) */
const AUTOMATED_PREFIXES = [
  'noreply', 'no-reply', 'no_reply', 'donotreply', 'do-not-reply',
  'mailer-daemon', 'postmaster', 'bounce', 'bounces', 'notifications',
  'notification', 'alert', 'alerts', 'automated', 'robot', 'system',
  'newsletter', 'newsletters', 'unsubscribe', 'billing', 'invoice',
  'receipts', 'receipt', 'order', 'orders', 'shipping', 'ship',
  'cloudbilling', 'cloudplatform', 'sc-noreply', 'googleplay-noreply',
];

/** Domini di piattaforme automatiche da ignorare */
const AUTOMATED_DOMAINS = [
  'google.com', 'googlemail.com', 'gmail.com',
  'facebookmail.com', 'facebook.com',
  'youtube.com', 'twitter.com', 'linkedin.com',
  'notification.circle.so', 'skool.com',
  'kaggle.com', 'claude.com', 'serpapi.com',
];

/** Finestra temporale di ricerca email (giorni) */
const LOOKBACK_DAYS = 1;

// ─── Logica principale ───────────────────────────────────────────────────────

/**
 * Estrae nome e cognome dal campo "From" dell'email.
 * Esempi: "Riccardo Belli" → { first: "Riccardo", last: "Belli" }
 *         "SEOZoom Info"   → { first: "SEOZoom",  last: "Info" }
 */
function parseSenderName(senderString) {
  if (!senderString) return { first: '', last: '' };

  // Formato "Nome Cognome <email@domain.com>"
  const nameMatch = senderString.match(/^([^<]+)<[^>]+>/);
  const rawName = nameMatch ? nameMatch[1].trim() : '';

  if (!rawName) return { first: '', last: '' };

  const parts = rawName.split(/\s+/).filter(Boolean);
  return {
    first: parts[0] || '',
    last: parts.slice(1).join(' ') || '',
  };
}

/**
 * Ricava il nome azienda dal dominio email.
 * Esempi: "rec-media.it" → "Rec Media", "seozoom.it" → "Seozoom"
 */
function companyFromDomain(domain) {
  if (!domain) return '';
  const base = domain.split('.')[0];
  return base
    .split('-')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/**
 * Verifica se il mittente è automatico/bot e va ignorato.
 */
function isAutomatedSender(email) {
  if (!email) return true;
  const [prefix, domain] = email.toLowerCase().split('@');
  if (!prefix || !domain) return true;

  if (AUTOMATED_DOMAINS.some(d => domain === d || domain.endsWith('.' + d))) {
    return true;
  }

  if (AUTOMATED_PREFIXES.some(p => prefix === p || prefix.startsWith(p + '-') || prefix.startsWith(p + '.'))) {
    return true;
  }

  return false;
}

/**
 * Normalizza i dati del mittente per HubSpot.
 */
function buildContactProperties(email, senderHeader, subject) {
  const [, domain] = email.split('@');
  const { first, last } = parseSenderName(senderHeader);

  return {
    email,
    firstname: first || capitalizeFirst(email.split('@')[0]),
    lastname: last || '',
    company: companyFromDomain(domain),
    domain,
    subject,
    source_label: 'Gmail',
  };
}

function capitalizeFirst(str) {
  if (!str) return '';
  return str.charAt(0).toUpperCase() + str.slice(1);
}

// ─── Risultati sincronizzazione ──────────────────────────────────────────────

/**
 * Formatta il report finale di sincronizzazione.
 * @param {Array<{status, email, hubspotId, action}>} results
 */
function formatReport(results) {
  const lines = [
    '═══════════════════════════════════════════════',
    '  Gmail → HubSpot Sync — Report',
    '═══════════════════════════════════════════════',
  ];

  const created = results.filter(r => r.status === 'Creato');
  const updated = results.filter(r => r.status === 'Aggiornato');
  const skipped = results.filter(r => r.status === 'Ignorato');

  for (const r of results) {
    const icon = r.status === 'Creato' ? '✅' : r.status === 'Aggiornato' ? '🔄' : '⏭️';
    lines.push(`${icon} [${r.status}] ${r.email} — ID: ${r.hubspotId || '-'}`);
    if (r.note) lines.push(`   ↳ ${r.note}`);
  }

  lines.push('───────────────────────────────────────────────');
  lines.push(`Creati: ${created.length} | Aggiornati: ${updated.length} | Ignorati: ${skipped.length}`);
  lines.push('═══════════════════════════════════════════════');
  return lines.join('\n');
}

export {
  isAutomatedSender,
  buildContactProperties,
  companyFromDomain,
  parseSenderName,
  formatReport,
};
