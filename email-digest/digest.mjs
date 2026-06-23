#!/usr/bin/env node
// Daily inbox digest: reads recent Gmail inbox mail, groups it, flags anything
// that looks like it needs a personal reply, and emails the summary to you.
//
// Runs unattended on a schedule (see .github/workflows/email-digest.yml).
// Auth uses an OAuth2 refresh token so it can act on your personal Gmail
// without a server. See email-digest/README.md for setup.

import { google } from "googleapis";

// ---------------------------------------------------------------------------
// Config (all via env so nothing secret lives in the repo)
// ---------------------------------------------------------------------------
const {
  GOOGLE_CLIENT_ID,
  GOOGLE_CLIENT_SECRET,
  GOOGLE_REFRESH_TOKEN,
  ANTHROPIC_API_KEY,                       // optional: adds an AI TL;DR
  ANTHROPIC_MODEL = "claude-sonnet-4-6",   // optional override
  DIGEST_TO,                               // recipient; defaults to the mailbox owner
  LOOKBACK_HOURS = "24",
  MAX_EMAILS = "60",
  DRY_RUN,                                 // set to "1" to print instead of sending
} = process.env;

function requireEnv(name, val) {
  if (!val) {
    console.error(`Missing required env var: ${name}`);
    process.exit(1);
  }
  return val;
}
const lookbackHours = Number(LOOKBACK_HOURS) || 24;
const maxEmails = Number(MAX_EMAILS) || 60;

// ---------------------------------------------------------------------------
// Gmail client
// ---------------------------------------------------------------------------
// Current hour (0-23) in US Central time, DST-aware via the IANA database.
function centralHour() {
  const h = Number(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Chicago",
      hour: "numeric",
      hour12: false,
    }).format(new Date())
  );
  return h === 24 ? 0 : h; // some platforms render midnight as 24
}

// On scheduled runs we fire at several UTC hours to cover both CST and CDT;
// skip any run whose real Central hour isn't one we want. Manual runs always go.
function shouldRunNow() {
  const guard = (process.env.GUARD_HOURS_CENTRAL || "")
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isInteger(n));
  if (process.env.GITHUB_EVENT_NAME !== "schedule" || !guard.length) return true;
  const h = centralHour();
  if (guard.includes(h)) return true;
  console.log(
    `Central time is ${h}:00 — not a scheduled digest hour (${guard.join(", ")}). Skipping.`
  );
  return false;
}

function gmailClient() {
  const oauth2 = new google.auth.OAuth2(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET);
  oauth2.setCredentials({ refresh_token: GOOGLE_REFRESH_TOKEN });
  return google.gmail({ version: "v1", auth: oauth2 });
}

function header(headers, name) {
  const h = headers.find((x) => x.name.toLowerCase() === name.toLowerCase());
  return h ? h.value : "";
}

function parseFrom(from) {
  // "Jane Doe <jane@example.com>" -> { name, email }
  const m = from.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
  if (m) return { name: m[1].trim(), email: m[2].trim().toLowerCase() };
  return { name: "", email: from.trim().toLowerCase() };
}

// ---------------------------------------------------------------------------
// Classification heuristics
// ---------------------------------------------------------------------------
const NO_REPLY_RE =
  /(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?@|notify@|mailer|bounce|newsletter|updates?@|info@|hello@|team@|support@|alerts?@|automated)/i;

const JOB_SENDER_RE =
  /(linkedin|indeed|glassdoor|ziprecruiter|lever\.co|greenhouse|wellfound|angellist|dice\.com|monster\.com|hired\.com|builtin)/i;
const JOB_SUBJECT_RE =
  /\b(job|jobs|hiring|opening|opportunit(y|ies)|recruit|apply now|new roles?|positions?)\b/i;

const ACTION_RE =
  /\b(invoice|bill|billing|payment|past due|overdue|receipt|order|shipped|shipment|delivery|tracking|statement|balance|due|renew|subscription|expir|verify|verification|confirm|security alert|sign[- ]?in|password|account|refund|deposit|withdrawal|transaction|tax|appointment|reservation|rsvp)\b/i;

const MARKETING_SUBJECT_RE =
  /(% off|sale|deal|save|discount|coupon|promo|newsletter|webinar|unsubscribe|limited time|free shipping|black friday|cyber monday|new arrival|don'?t miss|last chance|exclusive offer)/i;

export function classify(msg) {
  const { from, subject, snippet, labelIds, listUnsub } = msg;
  const { email } = parseFrom(from);
  const labels = new Set(labelIds || []);

  const hasUnsub = Boolean(listUnsub);
  const isPromoLabel = labels.has("CATEGORY_PROMOTIONS");
  const isSocialLabel = labels.has("CATEGORY_SOCIAL");
  const isPersonalLabel = labels.has("CATEGORY_PERSONAL");
  const looksAutomated = NO_REPLY_RE.test(email);

  const text = `${subject} ${snippet}`;

  // Job alerts first — they're a distinct bucket the user called out.
  if (JOB_SENDER_RE.test(email) || JOB_SUBJECT_RE.test(subject)) {
    return "jobs";
  }

  // Marketing / promotional: unsubscribe link + promo language, or Gmail's
  // own promotions/social categorization, and not obviously actionable.
  if (
    !ACTION_RE.test(text) &&
    (isPromoLabel || isSocialLabel || (hasUnsub && MARKETING_SUBJECT_RE.test(text)))
  ) {
    return "marketing";
  }

  // Worth acting on: bills, orders, accounts, security, etc.
  if (ACTION_RE.test(text)) return "action";

  // Needs a personal reply: looks like it's from a real human.
  // (Not automated sender, not bulk/promo, ideally Gmail's "personal" bucket.)
  if (!looksAutomated && !hasUnsub && (isPersonalLabel || (!isPromoLabel && !isSocialLabel))) {
    return "reply";
  }

  // Everything else is low-priority noise.
  return "marketing";
}

const GROUPS = {
  reply: { title: "✋ Needs a reply", blurb: "Looks like a real person waiting on you" },
  action: { title: "📌 Worth acting on", blurb: "Bills, orders, accounts, security" },
  jobs: { title: "💼 Job alerts", blurb: "" },
  marketing: { title: "🗑️ Ignorable / marketing", blurb: "" },
};

// ---------------------------------------------------------------------------
// Optional AI TL;DR (graceful no-op if no key)
// ---------------------------------------------------------------------------
async function aiSummary(buckets) {
  if (!ANTHROPIC_API_KEY) return "";
  const lines = [];
  for (const key of ["reply", "action", "jobs"]) {
    for (const m of buckets[key]) {
      lines.push(`[${key}] ${parseFrom(m.from).name || parseFrom(m.from).email} — ${m.subject}`);
    }
  }
  if (!lines.length) return "";
  try {
    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: ANTHROPIC_MODEL,
        max_tokens: 300,
        messages: [
          {
            role: "user",
            content:
              "Here are subjects from my inbox grouped by category. Write a 2-3 sentence " +
              "plain-language TL;DR of what needs my attention today. Be concise, no preamble.\n\n" +
              lines.join("\n"),
          },
        ],
      }),
    });
    if (!res.ok) {
      console.warn(`AI summary skipped (HTTP ${res.status})`);
      return "";
    }
    const data = await res.json();
    return (data.content?.[0]?.text || "").trim();
  } catch (err) {
    console.warn(`AI summary skipped: ${err.message}`);
    return "";
  }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function esc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function renderHtml(buckets, tldr, meta) {
  const section = (key) => {
    const items = buckets[key];
    if (!items.length) return "";
    const g = GROUPS[key];
    const rows = items
      .map((m) => {
        const f = parseFrom(m.from);
        const who = esc(f.name || f.email);
        return `<li style="margin:0 0 10px;line-height:1.4">
          <span style="font-weight:600">${who}</span>
          <span style="color:#888"> — ${esc(m.subject || "(no subject)")}</span>
          <br><span style="color:#aaa;font-size:13px">${esc(m.snippet || "")}</span>
        </li>`;
      })
      .join("");
    return `<h2 style="font-size:16px;margin:22px 0 8px">${g.title}
      <span style="color:#999;font-weight:400;font-size:13px">(${items.length})${
        g.blurb ? " · " + g.blurb : ""
      }</span></h2>
      <ul style="padding-left:18px;margin:0">${rows}</ul>`;
  };

  const tldrBlock = tldr
    ? `<div style="background:#f4f1ff;border-left:3px solid #7C5CFC;padding:12px 14px;border-radius:6px;margin:0 0 18px">
         <strong>TL;DR</strong><br>${esc(tldr)}</div>`
    : "";

  return `<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:0 auto;color:#1a1a1a">
    <p style="color:#777;font-size:13px;margin:0 0 4px">Inbox digest · last ${meta.hours}h · ${meta.total} messages</p>
    ${tldrBlock}
    ${section("reply")}
    ${section("action")}
    ${section("jobs")}
    ${section("marketing")}
    <p style="color:#bbb;font-size:12px;margin-top:24px">Generated automatically by your Capital email-digest workflow.</p>
  </div>`;
}

function renderText(buckets, tldr, meta) {
  const out = [`Inbox digest · last ${meta.hours}h · ${meta.total} messages`, ""];
  if (tldr) out.push("TL;DR: " + tldr, "");
  for (const key of ["reply", "action", "jobs", "marketing"]) {
    const items = buckets[key];
    if (!items.length) continue;
    out.push(`== ${GROUPS[key].title} (${items.length}) ==`);
    for (const m of items) {
      const f = parseFrom(m.from);
      out.push(`- ${f.name || f.email} — ${m.subject || "(no subject)"}`);
    }
    out.push("");
  }
  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Send
// ---------------------------------------------------------------------------
function buildRaw({ to, from, subject, html, text }) {
  const boundary = "b_" + Math.random().toString(36).slice(2);
  const mime = [
    `From: ${from}`,
    `To: ${to}`,
    `Subject: ${subject}`,
    "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`,
    "",
    `--${boundary}`,
    "Content-Type: text/plain; charset=UTF-8",
    "",
    text,
    "",
    `--${boundary}`,
    "Content-Type: text/html; charset=UTF-8",
    "",
    html,
    "",
    `--${boundary}--`,
  ].join("\r\n");
  return Buffer.from(mime).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  if (!shouldRunNow()) return;

  requireEnv("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID);
  requireEnv("GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET);
  requireEnv("GOOGLE_REFRESH_TOKEN", GOOGLE_REFRESH_TOKEN);

  const gmail = gmailClient();

  const profile = await gmail.users.getProfile({ userId: "me" });
  const owner = profile.data.emailAddress;
  const to = DIGEST_TO || owner;

  const list = await gmail.users.messages.list({
    userId: "me",
    q: `in:inbox newer_than:${Math.ceil(lookbackHours / 24) || 1}d`,
    maxResults: maxEmails,
  });
  const ids = (list.data.messages || []).map((m) => m.id);

  if (!ids.length) {
    console.log("No new inbox mail in the lookback window — skipping digest.");
    return;
  }

  const cutoff = Date.now() - lookbackHours * 3600 * 1000;
  const messages = [];
  for (const id of ids) {
    const res = await gmail.users.messages.get({
      userId: "me",
      id,
      format: "metadata",
      metadataHeaders: ["From", "Subject", "Date", "List-Unsubscribe"],
    });
    const d = res.data;
    const headers = d.payload?.headers || [];
    const internal = Number(d.internalDate || 0);
    if (internal && internal < cutoff) continue; // tighter than Gmail's day granularity
    messages.push({
      from: header(headers, "From"),
      subject: header(headers, "Subject"),
      listUnsub: header(headers, "List-Unsubscribe"),
      snippet: (d.snippet || "").trim(),
      labelIds: d.labelIds || [],
    });
  }

  const buckets = { reply: [], action: [], jobs: [], marketing: [] };
  for (const m of messages) buckets[classify(m)].push(m);

  const meta = { hours: lookbackHours, total: messages.length };
  const tldr = await aiSummary(buckets);
  const html = renderHtml(buckets, tldr, meta);
  const text = renderText(buckets, tldr, meta);

  const subject = `📬 Inbox digest — ${buckets.reply.length} need a reply, ${buckets.action.length} to act on`;

  if (DRY_RUN === "1") {
    console.log(text);
    console.log("\n--- (DRY_RUN: not sending) ---");
    return;
  }

  await gmail.users.messages.send({
    userId: "me",
    requestBody: { raw: buildRaw({ to, from: owner, subject, html, text }) },
  });
  console.log(`Digest sent to ${to}: ${messages.length} messages summarized.`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error("Digest failed:", err?.message || err);
    process.exit(1);
  });
}
