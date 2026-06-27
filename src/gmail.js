import { google } from 'googleapis';

export function createGmailClient({ clientId, clientSecret, refreshToken, userEmail }) {
  const auth = new google.auth.OAuth2(clientId, clientSecret);
  auth.setCredentials({ refresh_token: refreshToken });

  const gmail = google.gmail({ version: 'v1', auth });

  return {
    async getRecentInboxThreads(hoursBack = 24) {
      const since = Math.floor((Date.now() - hoursBack * 60 * 60 * 1000) / 1000);
      const query = `in:inbox after:${since} -from:${userEmail} -from:me`;

      const threads = [];
      let pageToken;

      do {
        const res = await gmail.users.threads.list({
          userId: 'me',
          q: query,
          maxResults: 100,
          pageToken,
        });
        if (res.data.threads) threads.push(...res.data.threads);
        pageToken = res.data.nextPageToken;
      } while (pageToken);

      return threads;
    },

    async getThreadSender(threadId) {
      const res = await gmail.users.threads.get({
        userId: 'me',
        id: threadId,
        format: 'metadata',
        metadataHeaders: ['From', 'Subject', 'Date'],
      });

      const firstMessage = res.data.messages?.[0];
      if (!firstMessage) return null;

      const headers = firstMessage.payload?.headers ?? [];
      const from = headers.find(h => h.name === 'From')?.value ?? '';
      const subject = headers.find(h => h.name === 'Subject')?.value ?? '';
      const date = headers.find(h => h.name === 'Date')?.value ?? '';

      return parseSenderInfo(from, subject, date, threadId);
    },
  };
}

function parseSenderInfo(fromHeader, subject, date, threadId) {
  // "Nome Cognome <email@domain.com>" oppure "email@domain.com"
  const nameEmailPattern = /^(?:"?([^"<]+)"?\s*)?<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?$/;
  const match = fromHeader.trim().match(nameEmailPattern);

  if (!match) return null;

  const rawName = (match[1] ?? '').trim();
  const email = (match[2] ?? '').toLowerCase().trim();

  if (!email) return null;

  const nameParts = rawName.split(/\s+/).filter(Boolean);
  const firstName = nameParts[0] ?? '';
  const lastName = nameParts.slice(1).join(' ');

  const domain = email.split('@')[1] ?? '';
  const company = deriveCompany(domain, rawName);

  return { email, firstName, lastName, company, domain, subject, date, threadId };
}

function deriveCompany(domain, displayName) {
  const personalDomains = new Set([
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
    'icloud.com', 'libero.it', 'virgilio.it', 'tiscali.it',
    'alice.it', 'tin.it', 'live.com', 'live.it',
  ]);

  if (personalDomains.has(domain)) return '';

  // Rimuovi TLD comune e converti in nome leggibile
  const parts = domain.split('.');
  const relevant = parts.length >= 3 ? parts.slice(0, -2).join(' ') : parts[0];

  // Se il nome visualizzato sembra un nome azienda (es. "Ufficio Stampa X"), preferisci quello
  if (displayName && displayName.length > 3 && !/^[A-Z]\.[A-Z]/.test(displayName)) {
    const wordCount = displayName.split(' ').length;
    if (wordCount >= 2) return displayName;
  }

  return relevant.charAt(0).toUpperCase() + relevant.slice(1);
}
