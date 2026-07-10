import fs from 'fs';
import path from 'path';

/**
 * Persists sync state to disk so each run only processes new emails.
 * State shape:
 *   {
 *     lastProcessedDate: "2026-07-10T20:00:00Z",
 *     processedMessageIds: ["id1", "id2", ...]
 *   }
 */
export class SyncState {
  constructor(filePath) {
    this.filePath = filePath;
    this._state = this._load();
  }

  _load() {
    try {
      const raw = fs.readFileSync(this.filePath, 'utf8');
      return JSON.parse(raw);
    } catch {
      return { lastProcessedDate: null, processedMessageIds: [] };
    }
  }

  save() {
    const dir = path.dirname(this.filePath);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(this.filePath, JSON.stringify(this._state, null, 2));
  }

  get lastProcessedDate() {
    return this._state.lastProcessedDate;
  }

  set lastProcessedDate(value) {
    this._state.lastProcessedDate = value;
  }

  isProcessed(messageId) {
    return this._state.processedMessageIds.includes(messageId);
  }

  markProcessed(messageId) {
    if (!this.isProcessed(messageId)) {
      this._state.processedMessageIds.push(messageId);
      // Cap the list at 10 000 entries to avoid unbounded growth
      if (this._state.processedMessageIds.length > 10_000) {
        this._state.processedMessageIds = this._state.processedMessageIds.slice(-10_000);
      }
    }
  }
}
