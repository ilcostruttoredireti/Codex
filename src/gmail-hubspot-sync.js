/**
 * Gmail → HubSpot Contact Sync
 *
 * Monitors the Gmail inbox for new emails and syncs sender data
 * into HubSpot contacts (create if new, update if existing).
 *
 * State is tracked in ./state.json to avoid reprocessing threads.
 *
 * Required env vars:
 *   GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
 *   HUBSPOT_ACCESS_TOKEN
 */

const fs = require("fs");
const path = require("path");
const https = require("https");

const STATE_FILE = path.join(__dirname, "..", "state.json");
const LOOKBACK_DAYS = 7;

// ---------------------------------------------------------------------------
// Automated-sender filter
// ---------------------------------------------------------------------------

const SKIP_LOCAL_PARTS = new Set([
  "noreply",
  "no-reply",
  "donotreply",
  "do-not-reply",
  "notifications",
  "notification",
  "notify",
  "mailer",
  "newsletter",
  "bounce",
  "bounces",
  "nobody",
  "daemon",
  "postmaster",
  "automailer",
  "auto",
  "automated",
  "system",
  "noreplies",
]);

const SKIP_DOMAIN_PATTERNS = [
  /\.amazonses\.com$/,
  /\.sendgrid\.net$/,
  /^notification\./,
  /^notifications\./,
  /^mail\d+\./,
  /^mailer\./,
  /^email\./,
  /^engage\./,
  /^marketing\./,
  /^news\./,
  /^conferma-/,
  /^noreply\./,
];

const SKIP_KNOWN_DOMAINS = new Set([
  "amazon.it",
  "amazon.com",
  "ebay.com",
  "ebay.it",
  "paypal.com",
  "revolut.com",
  "coinranking.com",
  "feedspot.com",
  "academia-mail.com",
  "thomsonreuters.com",
  "semalt.org",
]);

function isAutomatedSender(email) {
  if (!email || !email.includes("@")) return true;
  const [local, domain] = email.toLowerCase().split("@");

  if (SKIP_LOCAL_PARTS.has(local)) return true;
  if (local.startsWith("noreply") || local.startsWith("no-reply")) return true;
  if (local.includes("confirm") || local.includes("conferma")) return true;
  if (SKIP_KNOWN_DOMAINS.has(domain)) return true;
  if (SKIP_DOMAIN_PATTERNS.some((r) => r.test(domain))) return true;

  return false;
}

// ---------------------------------------------------------------------------
// Name / company extraction helpers
// ---------------------------------------------------------------------------

function parseFromHeader(fromStr) {
  // Handles "John Doe <john@example.com>" or "john@example.com"
  const match = fromStr.match(/^"?([^"<]+)"?\s*<([^>]+)>/);
  if (match) {
    return { displayName: match[1].trim(), email: match[2].trim().toLowerCase() };
  }
  return { displayName: null, email: fromStr.trim().toLowerCase() };
}

function splitName(displayName) {
  if (!displayName) return { firstname: null, lastname: null };
  const parts = displayName.split(/\s+/);
  if (parts.length === 1) return { firstname: parts[0], lastname: null };
  const lastname = parts.pop();
  return { firstname: parts.join(" "), lastname };
}

function companyFromDomain(domain) {
  const labels = domain.replace(/\.(com|it|eu|org|net|io|co|uk|de|fr|es)$/, "").split(".");
  const name = labels[labels.length - 1];
  return name.charAt(0).toUpperCase() + name.slice(1);
}

function isGenericLocalPart(local) {
  return ["info", "contact", "hello", "support", "team", "staff", "admin", "sales", "help"].includes(local);
}

// ---------------------------------------------------------------------------
// State helpers
// ---------------------------------------------------------------------------

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
  } catch {
    return { processedThreadIds: [], lastRun: null };
  }
}

function saveState(state) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

// ---------------------------------------------------------------------------
// Gmail API (OAuth2 + REST)
// ---------------------------------------------------------------------------

async function refreshAccessToken(clientId, clientSecret, refreshToken) {
  const body = new URLSearchParams({
    client_id: clientId,
    client_secret: clientSecret,
    refresh_token: refreshToken,
    grant_type: "refresh_token",
  }).toString();

  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        hostname: "oauth2.googleapis.com",
        path: "/token",
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve(JSON.parse(data)));
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

async function gmailGet(accessToken, path) {
  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        hostname: "gmail.googleapis.com",
        path,
        headers: { Authorization: `Bearer ${accessToken}` },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve(JSON.parse(data)));
      }
    );
    req.on("error", reject);
    req.end();
  });
}

async function fetchInboxThreadIds(accessToken, afterEpochSec) {
  const allIds = [];
  let pageToken = "";
  do {
    const q = `in:inbox after:${afterEpochSec}${pageToken ? `&pageToken=${pageToken}` : ""}`;
    const result = await gmailGet(
      accessToken,
      `/gmail/v1/users/me/threads?q=${encodeURIComponent(`in:inbox after:${afterEpochSec}`)}&maxResults=500${pageToken ? `&pageToken=${pageToken}` : ""}`
    );
    if (result.threads) allIds.push(...result.threads.map((t) => t.id));
    pageToken = result.nextPageToken || "";
  } while (pageToken);
  return allIds;
}

async function fetchThreadSender(accessToken, threadId) {
  const thread = await gmailGet(accessToken, `/gmail/v1/users/me/threads/${threadId}?format=metadata&metadataHeaders=From`);
  const messages = thread.messages || [];
  if (!messages.length) return null;
  const firstMsg = messages[0];
  const fromHeader = (firstMsg.payload?.headers || []).find((h) => h.name === "From");
  return fromHeader?.value || null;
}

// ---------------------------------------------------------------------------
// HubSpot API
// ---------------------------------------------------------------------------

async function hubspotRequest(token, method, path, body = null) {
  return new Promise((resolve, reject) => {
    const bodyStr = body ? JSON.stringify(body) : null;
    const req = https.request(
      {
        hostname: "api.hubapi.com",
        path,
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          ...(bodyStr ? { "Content-Length": Buffer.byteLength(bodyStr) } : {}),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            resolve({ status: res.statusCode, body: JSON.parse(data) });
          } catch {
            resolve({ status: res.statusCode, body: data });
          }
        });
      }
    );
    req.on("error", reject);
    if (bodyStr) req.write(bodyStr);
    req.end();
  });
}

async function findContactByEmail(token, email) {
  const res = await hubspotRequest(token, "POST", "/crm/v3/objects/contacts/search", {
    filterGroups: [{ filters: [{ propertyName: "email", operator: "EQ", value: email }] }],
    properties: ["email", "firstname", "lastname", "company", "hs_lead_status"],
    limit: 1,
  });
  if (res.status === 200 && res.body.results?.length) return res.body.results[0];
  return null;
}

async function createContact(token, props) {
  const res = await hubspotRequest(token, "POST", "/crm/v3/objects/contacts", { properties: props });
  return res;
}

async function updateContact(token, id, props) {
  const res = await hubspotRequest(token, "PATCH", `/crm/v3/objects/contacts/${id}`, { properties: props });
  return res;
}

// ---------------------------------------------------------------------------
// Main sync logic
// ---------------------------------------------------------------------------

async function run() {
  const { GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, HUBSPOT_ACCESS_TOKEN } = process.env;
  if (!GMAIL_CLIENT_ID || !GMAIL_CLIENT_SECRET || !GMAIL_REFRESH_TOKEN || !HUBSPOT_ACCESS_TOKEN) {
    throw new Error("Missing required environment variables. See README.");
  }

  const state = loadState();
  const processedSet = new Set(state.processedThreadIds);
  const results = [];

  // Gmail access token
  const tokenData = await refreshAccessToken(GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN);
  const gmailToken = tokenData.access_token;
  if (!gmailToken) throw new Error("Failed to obtain Gmail access token");

  // Fetch threads from last N days
  const afterSec = Math.floor(Date.now() / 1000) - LOOKBACK_DAYS * 86400;
  const threadIds = await fetchInboxThreadIds(gmailToken, afterSec);
  const newThreadIds = threadIds.filter((id) => !processedSet.has(id));

  console.log(`Threads found: ${threadIds.length}, new to process: ${newThreadIds.length}`);

  for (const threadId of newThreadIds) {
    processedSet.add(threadId);

    const fromRaw = await fetchThreadSender(gmailToken, threadId);
    if (!fromRaw) continue;

    const { displayName, email } = parseFromHeader(fromRaw);
    if (!email || isAutomatedSender(email)) {
      results.push({ threadId, email, status: "IGNORATO", reason: "automated sender" });
      continue;
    }

    const [local, domain] = email.split("@");
    const isGeneric = isGenericLocalPart(local);

    // Build contact properties
    let { firstname, lastname } = splitName(displayName);

    // If firstname came from a generic local part (e.g. "info@"), clear it
    if (isGeneric || !firstname) {
      firstname = null;
      lastname = null;
    }

    const company = companyFromDomain(domain);
    const props = {
      email,
      lead_source: "Gmail",
      hs_lead_status: "NEW",
    };
    if (firstname) props.firstname = firstname;
    if (lastname) props.lastname = lastname;
    if (company) props.company = company;

    // Check for existing contact
    const existing = await findContactByEmail(HUBSPOT_ACCESS_TOKEN, email);

    if (existing) {
      // Update only missing fields
      const updates = {};
      if (!existing.properties.company && company) updates.company = company;
      if (!existing.properties.firstname && firstname) updates.firstname = firstname;
      if (!existing.properties.lastname && lastname) updates.lastname = lastname;

      if (Object.keys(updates).length) {
        await updateContact(HUBSPOT_ACCESS_TOKEN, existing.id, updates);
        results.push({ threadId, email, status: "AGGIORNATO", hubspotId: existing.id });
      } else {
        results.push({ threadId, email, status: "IGNORATO", reason: "already up to date", hubspotId: existing.id });
      }
    } else {
      const res = await createContact(HUBSPOT_ACCESS_TOKEN, props);
      const hubspotId = res.body?.id || null;
      results.push({ threadId, email, status: "CREATO", hubspotId });
    }
  }

  // Persist state (keep last 5000 thread IDs to avoid unbounded growth)
  const allIds = Array.from(processedSet);
  state.processedThreadIds = allIds.slice(-5000);
  state.lastRun = new Date().toISOString();
  saveState(state);

  // Print summary
  console.log("\n=== RISULTATI SINCRONIZZAZIONE ===");
  for (const r of results.filter((r) => r.status !== "IGNORATO")) {
    console.log(`[${r.status}] ${r.email} → HubSpot ID: ${r.hubspotId || "n/a"}`);
  }
  const created = results.filter((r) => r.status === "CREATO").length;
  const updated = results.filter((r) => r.status === "AGGIORNATO").length;
  const ignored = results.filter((r) => r.status === "IGNORATO").length;
  console.log(`\nCreati: ${created} | Aggiornati: ${updated} | Ignorati: ${ignored}`);

  return results;
}

module.exports = { run, isAutomatedSender, parseFromHeader, splitName };

if (require.main === module) {
  run().catch((err) => {
    console.error("Sync failed:", err);
    process.exit(1);
  });
}
