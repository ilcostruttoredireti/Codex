/**
 * Gmail → HubSpot Contact Sync
 *
 * Monitors Gmail inbox for incoming emails, extracts sender info,
 * and creates or updates contacts in HubSpot — deduplicating by email.
 *
 * Usage (Node.js):
 *   node gmail_hubspot_sync.js
 *
 * Environment variables required:
 *   HUBSPOT_ACCESS_TOKEN   — HubSpot private app token (contacts read/write)
 *   GOOGLE_OAUTH_TOKEN     — Gmail OAuth2 access token
 *
 * Run continuously (e.g. via cron every 15 min) or in a loop with --watch.
 */

const https = require("https");
const { execSync } = require("child_process");

// ─── Configuration ────────────────────────────────────────────────────────────

const HUBSPOT_TOKEN = process.env.HUBSPOT_ACCESS_TOKEN;
const GMAIL_TOKEN = process.env.GOOGLE_OAUTH_TOKEN;

// How far back to look for new messages on each run (seconds)
const LOOKBACK_SECONDS = parseInt(process.env.LOOKBACK_SECONDS || "900", 10);

// Patterns that identify automated / no-reply senders to skip
const SKIP_PATTERNS = [
  /^no-?reply/i,
  /^noreply/i,
  /^notify/i,
  /^notifications?/i,
  /^mailer-daemon/i,
  /^postmaster/i,
  /^bounce/i,
  /^do-not-reply/i,
  /^auto-?reply/i,
  /^pinbot/i,
  /^ads-/i,
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

function httpsRequest(options, body) {
  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(data) });
        } catch {
          resolve({ status: res.statusCode, body: data });
        }
      });
    });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

function gmailRequest(path) {
  return httpsRequest({
    hostname: "gmail.googleapis.com",
    path,
    method: "GET",
    headers: { Authorization: `Bearer ${GMAIL_TOKEN}` },
  });
}

function hubspotRequest(method, path, body) {
  const options = {
    hostname: "api.hubapi.com",
    path,
    method,
    headers: {
      Authorization: `Bearer ${HUBSPOT_TOKEN}`,
      "Content-Type": "application/json",
    },
  };
  return httpsRequest(options, body);
}

// ─── Sender extraction ────────────────────────────────────────────────────────

function parseSender(raw) {
  // Handles: "First Last <email@domain.com>" or just "email@domain.com"
  const match = raw.match(/^(?:"?(.+?)"?\s+)?<?([^\s<>]+@[^\s<>]+)>?$/);
  if (!match) return null;
  const fullName = (match[1] || "").trim();
  const email = match[2].trim().toLowerCase();
  const [localPart, domain] = email.split("@");
  const parts = fullName.split(/\s+/);
  const firstname = parts[0] || localPart;
  const lastname = parts.length > 1 ? parts.slice(1).join(" ") : "";
  const company = domainToCompany(domain);
  return { email, firstname, lastname, domain, company };
}

function domainToCompany(domain) {
  // Strip common TLDs and subdomains to guess a company name
  const root = domain.replace(/^(mail|smtp|mg|email|send|info)\./i, "");
  const name = root
    .replace(/\.(com|it|eu|net|org|io|co\.uk|de|fr|es)$/, "")
    .replace(/[-_.]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return name;
}

function isAutomated(email) {
  const local = email.split("@")[0];
  return SKIP_PATTERNS.some((p) => p.test(local));
}

// ─── Gmail ───────────────────────────────────────────────────────────────────

async function fetchRecentInboxMessages() {
  const after = Math.floor(Date.now() / 1000) - LOOKBACK_SECONDS;
  const query = encodeURIComponent(`in:inbox -from:me after:${after}`);
  const res = await gmailRequest(
    `/gmail/v1/users/me/messages?q=${query}&maxResults=50`
  );
  if (res.status !== 200) throw new Error(`Gmail list error: ${res.status}`);
  return res.body.messages || [];
}

async function getMessageSender(messageId) {
  const res = await gmailRequest(
    `/gmail/v1/users/me/messages/${messageId}?format=metadata&metadataHeaders=From`
  );
  if (res.status !== 200) return null;
  const headers = res.body.payload?.headers || [];
  const fromHeader = headers.find((h) => h.name === "From");
  return fromHeader ? parseSender(fromHeader.value) : null;
}

// ─── HubSpot ─────────────────────────────────────────────────────────────────

async function findContact(email) {
  const res = await hubspotRequest(
    "POST",
    "/crm/v3/objects/contacts/search",
    {
      filterGroups: [
        { filters: [{ propertyName: "email", operator: "EQ", value: email }] },
      ],
      properties: ["email", "firstname", "lastname", "company", "hs_lead_source"],
      limit: 1,
    }
  );
  if (res.status !== 200) return null;
  return res.body.results?.[0] || null;
}

async function createContact(sender) {
  const props = {
    email: sender.email,
    firstname: sender.firstname,
    lastname: sender.lastname,
    company: sender.company,
    hs_lead_source: "OFFLINE",   // maps to "Other" — tag set separately
    website: `https://${sender.domain}`,
  };
  const res = await hubspotRequest("POST", "/crm/v3/objects/contacts", {
    properties: props,
  });
  return { status: res.status, body: res.body };
}

async function updateContact(contactId, sender, existing) {
  const props = {};
  const ep = existing.properties;

  // Only fill in missing fields
  if (!ep.firstname || ep.firstname === ep.email?.split("@")[0])
    props.firstname = sender.firstname;
  if (!ep.lastname) props.lastname = sender.lastname;
  if (!ep.company)  props.company  = sender.company;
  if (!ep.hs_lead_source) props.hs_lead_source = "OFFLINE";

  if (Object.keys(props).length === 0) return null;

  const res = await hubspotRequest(
    "PATCH",
    `/crm/v3/objects/contacts/${contactId}`,
    { properties: props }
  );
  return { status: res.status, body: res.body };
}

// ─── Main loop ────────────────────────────────────────────────────────────────

async function run() {
  const results = [];

  console.log(`[gmail-hubspot-sync] Fetching messages from last ${LOOKBACK_SECONDS}s…`);
  const messages = await fetchRecentInboxMessages();
  console.log(`[gmail-hubspot-sync] Found ${messages.length} message(s) to process`);

  const seen = new Set();

  for (const msg of messages) {
    const sender = await getMessageSender(msg.id);
    if (!sender) continue;
    if (seen.has(sender.email)) continue;
    if (isAutomated(sender.email)) {
      results.push({ status: "Ignorato", email: sender.email, reason: "automated sender", hubspotId: null });
      continue;
    }

    seen.add(sender.email);

    const existing = await findContact(sender.email);

    if (existing) {
      const updated = await updateContact(existing.id, sender, existing);
      results.push({
        status: updated ? "Aggiornato" : "Ignorato",
        email: sender.email,
        hubspotId: existing.id,
      });
    } else {
      const created = await createContact(sender);
      results.push({
        status: created.status === 201 ? "Creato" : "Errore",
        email: sender.email,
        hubspotId: created.body?.id || null,
        error: created.status !== 201 ? created.body : undefined,
      });
    }
  }

  console.log("\n── Risultati ──────────────────────────────────────────────");
  console.log("Stato        | Email                              | HubSpot ID");
  console.log("─────────────|────────────────────────────────────|───────────");
  for (const r of results) {
    const stato = r.status.padEnd(12);
    const email = r.email.padEnd(35);
    const id    = r.hubspotId || "-";
    console.log(`${stato} | ${email}| ${id}`);
  }
  console.log(`\nTotale: ${results.length} | Creati: ${results.filter(r=>r.status==="Creato").length} | Aggiornati: ${results.filter(r=>r.status==="Aggiornato").length} | Ignorati: ${results.filter(r=>r.status==="Ignorato").length}`);

  return results;
}

run().catch((err) => {
  console.error("[gmail-hubspot-sync] Fatal error:", err.message);
  process.exit(1);
});
