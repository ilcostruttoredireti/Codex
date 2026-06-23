import { google } from 'googleapis';
import { readFileSync, writeFileSync, existsSync } from 'fs';

const STATE_FILE = '.sync_state.json';

function loadState() {
  if (!existsSync(STATE_FILE)) return { processedIds: [] };
  return JSON.parse(readFileSync(STATE_FILE, 'utf8'));
}

function saveState(state) {
  writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

export function createGmailClient(credentials, token) {
  const auth = new google.auth.OAuth2(
    credentials.client_id,
    credentials.client_secret,
    credentials.redirect_uri
  );
  auth.setCredentials(token);
  return google.gmail({ version: 'v1', auth });
}

export async function fetchNewSenders(gmail, maxResults = 100) {
  const state = loadState();

  const listRes = await gmail.users.messages.list({
    userId: 'me',
    labelIds: ['INBOX'],
    maxResults,
    q: 'in:inbox -from:me -is:draft',
  });

  const messages = listRes.data.messages || [];
  const newMessages = messages.filter(m => !state.processedIds.includes(m.id));

  const senders = [];
  for (const msg of newMessages) {
    const full = await gmail.users.messages.get({
      userId: 'me',
      id: msg.id,
      format: 'metadata',
      metadataHeaders: ['From'],
    });
    const fromHeader = full.data.payload.headers.find(h => h.name === 'From')?.value;
    if (fromHeader) senders.push({ id: msg.id, from: fromHeader });
  }

  state.processedIds = [...new Set([...state.processedIds, ...newMessages.map(m => m.id)])].slice(-2000);
  saveState(state);
  return senders;
}
