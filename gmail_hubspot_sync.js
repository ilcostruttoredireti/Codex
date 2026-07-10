/**
 * Gmail → HubSpot Contact Sync
 *
 * Scans Gmail inbox for recent emails, extracts sender data,
 * and creates/updates contacts in HubSpot avoiding duplicates.
 *
 * Run via: node gmail_hubspot_sync.js
 * Or schedule with Claude Code: /loop 30m
 *
 * Requirements:
 *   GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
 *   HUBSPOT_API_KEY
 */

const https = require("https");

// ─── Configuration ────────────────────────────────────────────────────────────

const CONFIG = {
  // Look back window for Gmail (default: last 24 hours)
  lookbackHours: 24,
  // Senders to skip (no-reply, notifications, system)
  skipPrefixes: [
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "notifications",
    "notify",
    "bounces",
    "auto-reply",
  ],
  skipDomains: [
    "accounts.google.com",
    "google.com",
    "googlecloud.com",
    "gmail.com",
    "facebookmail.com",
    "twitter.com",
    "linkedin.com",
  ],
  hubspotSource: "Gmail",
  hubspotTag: "Inbound Gmail",
};

// ─── Gmail API helpers ────────────────────────────────────────────────────────

async function gmailRequest(token, path) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: "gmail.googleapis.com",
      path: `/gmail/v1/users/me/${path}`,
      headers: { Authorization: `Bearer ${token}` },
    };
    https
      .get(options, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve(JSON.parse(data)));
      })
      .on("error", reject);
  });
}

async function getAccessToken() {
  const params = new URLSearchParams({
    client_id: process.env.GMAIL_CLIENT_ID,
    client_secret: process.env.GMAIL_CLIENT_SECRET,
    refresh_token: process.env.GMAIL_REFRESH_TOKEN,
    grant_type: "refresh_token",
  });
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
        res.on("end", () => resolve(JSON.parse(data).access_token));
      }
    );
    req.on("error", reject);
    req.write(params.toString());
    req.end();
  });
}

/**
 * Fetch new inbox messages from the last N hours.
 */
async function fetchInboxSenders(token, lookbackHours = 24) {
  const after = Math.floor(Date.now() / 1000) - lookbackHours * 3600;
  const q = encodeURIComponent(`in:inbox after:${after}`);
  const list = await gmailRequest(token, `messages?q=${q}&maxResults=100`);

  if (!list.messages) return [];

  const senders = new Map();
  for (const msg of list.messages) {
    const detail = await gmailRequest(
      token,
      `messages/${msg.id}?format=METADATA&metadataHeaders=From&metadataHeaders=Date`
    );
    const fromHeader = detail.payload?.headers?.find((h) => h.name === "From");
    if (!fromHeader) continue;

    const parsed = parseFromHeader(fromHeader.value);
    if (!parsed) continue;
    if (shouldSkip(parsed.email)) continue;

    // Deduplicate: keep first occurrence per email
    if (!senders.has(parsed.email)) {
      senders.set(parsed.email, parsed);
    }
  }
  return Array.from(senders.values());
}

/**
 * Parse "Name <email>" or plain "email" header.
 */
function parseFromHeader(from) {
  const match = from.match(/^(.*?)\s*<([^>]+)>/) || from.match(/^([^\s]+)$/);
  if (!match) return null;

  const email = (match[2] || match[1]).toLowerCase().trim();
  const fullName = (match[1] || "").trim().replace(/^["']|["']$/g, "");
  const domain = email.split("@")[1] || "";
  const nameParts = fullName.split(/\s+/);

  return {
    email,
    firstName: nameParts[0] || "",
    lastName: nameParts.slice(1).join(" ") || "",
    fullName,
    domain,
    company: domainToCompany(domain),
  };
}

function domainToCompany(domain) {
  // Strip common prefixes and TLDs for a clean company name
  return domain
    .replace(/^(mail|email|info|newsletter|news|send|engage)\./i, "")
    .replace(/\.(com|it|io|ai|net|org|co|eu)$/i, "")
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function shouldSkip(email) {
  const [prefix, domain] = email.split("@");
  if (!prefix || !domain) return true;
  if (CONFIG.skipDomains.includes(domain)) return true;
  return CONFIG.skipPrefixes.some((p) => prefix.toLowerCase().startsWith(p));
}

// ─── HubSpot API helpers ──────────────────────────────────────────────────────

function hubspotRequest(method, path, body) {
  const apiKey = process.env.HUBSPOT_API_KEY;
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : "";
    const options = {
      hostname: "api.hubapi.com",
      path,
      method,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        ...(data && { "Content-Length": Buffer.byteLength(data) }),
      },
    };
    const req = https.request(options, (res) => {
      let resp = "";
      res.on("data", (c) => (resp += c));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(resp) });
        } catch {
          resolve({ status: res.statusCode, body: resp });
        }
      });
    });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

async function findContact(email) {
  const res = await hubspotRequest("POST", "/crm/v3/objects/contacts/search", {
    filterGroups: [
      { filters: [{ propertyName: "email", operator: "EQ", value: email }] },
    ],
    properties: ["email", "firstname", "lastname", "company", "hs_analytics_source"],
    limit: 1,
  });
  return res.body.results?.[0] || null;
}

async function createContact(sender) {
  const res = await hubspotRequest("POST", "/crm/v3/objects/contacts", {
    properties: {
      email: sender.email,
      firstname: sender.firstName || "Contact",
      lastname: sender.lastName || "",
      company: sender.company || sender.domain,
      hs_analytics_source: "OTHER",
    },
  });

  if (res.status === 201) {
    // Add a note tagging the source
    await addNote(res.body.id, `Fonte: ${CONFIG.hubspotSource}. Tag: ${CONFIG.hubspotTag}`);
    return { action: "CREATO", contactId: res.body.id };
  }
  return { action: "ERRORE", error: res.body.message };
}

async function updateContact(contactId, sender, existing) {
  const updates = {};

  if (!existing.properties.lastname && sender.lastName)
    updates.lastname = sender.lastName;
  if (!existing.properties.company && sender.company)
    updates.company = sender.company;

  if (Object.keys(updates).length === 0) {
    return { action: "IGNORATO" };
  }

  await hubspotRequest("PATCH", `/crm/v3/objects/contacts/${contactId}`, {
    properties: updates,
  });
  return { action: "AGGIORNATO" };
}

async function addNote(contactId, body) {
  const note = await hubspotRequest("POST", "/crm/v3/objects/notes", {
    properties: { hs_note_body: body, hs_timestamp: new Date().toISOString() },
  });
  if (note.status === 201) {
    await hubspotRequest(
      "PUT",
      `/crm/v3/objects/notes/${note.body.id}/associations/contacts/${contactId}/note_to_contact`,
      {}
    );
  }
}

// ─── Main sync loop ───────────────────────────────────────────────────────────

async function sync() {
  console.log(`\n[${new Date().toISOString()}] Gmail → HubSpot Sync Started`);
  console.log("─".repeat(60));

  const results = [];

  let token;
  try {
    token = await getAccessToken();
  } catch (e) {
    console.error("❌ Gmail auth failed:", e.message);
    process.exit(1);
  }

  const senders = await fetchInboxSenders(token, CONFIG.lookbackHours);
  console.log(`📬 ${senders.length} unique senders found in inbox\n`);

  for (const sender of senders) {
    const existing = await findContact(sender.email);
    let result;

    if (existing) {
      result = await updateContact(existing.id, sender, existing);
      result.contactId = existing.id;
    } else {
      result = await createContact(sender);
    }

    results.push({ ...result, email: sender.email });
    const icon =
      result.action === "CREATO"
        ? "✅"
        : result.action === "AGGIORNATO"
        ? "🔄"
        : "⏭️";
    console.log(
      `${icon} ${result.action.padEnd(12)} ${sender.email.padEnd(40)} ID: ${result.contactId || "—"}`
    );
  }

  // Summary
  const created = results.filter((r) => r.action === "CREATO").length;
  const updated = results.filter((r) => r.action === "AGGIORNATO").length;
  const skipped = results.filter((r) => r.action === "IGNORATO").length;

  console.log("\n" + "─".repeat(60));
  console.log(`✅ Creati: ${created}  🔄 Aggiornati: ${updated}  ⏭️ Ignorati: ${skipped}`);
  console.log("─".repeat(60) + "\n");

  return { created, updated, skipped, total: results.length };
}

sync().catch(console.error);
