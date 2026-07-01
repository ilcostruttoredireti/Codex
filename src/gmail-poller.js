/**
 * Gmail Poller
 *
 * Recupera le email in arrivo nelle ultime N ore e restituisce
 * i mittenti da processare.
 *
 * Usa l'API Gmail via MCP (mcp__Gmail__search_threads + mcp__Gmail__get_thread).
 */

import { isAutomatedSender, buildContactProperties } from './sync.js';

/**
 * Raccoglie i mittenti unici dalle email recenti in inbox.
 * @param {number} lookbackHours  - Quante ore indietro guardare
 * @returns {Map<string, object>} - email → contactProperties
 */
async function collectSenders(lookbackHours = 24) {
  const days = Math.ceil(lookbackHours / 24);
  const query = `in:inbox newer_than:${days}d -in:draft`;

  // Chiama mcp__Gmail__search_threads (eseguito via MCP in ambiente Claude)
  const threads = await searchGmailThreads(query);

  const senders = new Map();

  for (const thread of threads) {
    for (const msg of thread.messages || []) {
      const senderEmail = extractEmail(msg.sender || '');
      if (!senderEmail) continue;
      if (isAutomatedSender(senderEmail)) continue;
      if (senders.has(senderEmail)) continue;

      const props = buildContactProperties(senderEmail, msg.sender, msg.subject);
      senders.set(senderEmail, props);
    }
  }

  return senders;
}

/** Estrae l'indirizzo email da stringhe tipo "Nome <email@example.com>" */
function extractEmail(sender) {
  const angleMatch = sender.match(/<([^>]+)>/);
  if (angleMatch) return angleMatch[1].toLowerCase().trim();
  const plain = sender.match(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/i);
  return plain ? plain[0].toLowerCase() : null;
}

/**
 * Stub: in produzione questa funzione chiama mcp__Gmail__search_threads.
 * Nell'ambiente Claude Code viene sostituita dall'orchestratore MCP.
 */
async function searchGmailThreads(query) {
  throw new Error(
    'searchGmailThreads deve essere implementata con il tool MCP mcp__Gmail__search_threads. ' +
    `Query: ${query}`
  );
}

export { collectSenders, extractEmail };
