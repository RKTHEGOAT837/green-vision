/* =====================================================================
   Green Vision account API
   =====================================================================
   The replacement for the Pipedream workflow, route for route, so the
   desktop app only has to change one URL.

   WHY IT MOVED. Pipedream's free tier meters every workflow run as a
   "credit" and gives about a hundred of them. This app spends one on each
   sign-in, project save, chat turn and history row, so the cap arrived
   quickly - and when it did, Pipedream DISABLED the workflow rather than
   erroring. Every route then returned Pipedream's own "Success!" HTML with
   HTTP 200. That is the worst possible failure: the client saw a 200,
   tried to read JSON, and silently behaved as though nothing had been
   saved. Sign-in stopped working, /me returned nothing, and the app looked
   like it had forgotten the user.

   Netlify gives 125,000 function invocations a month, includes Blobs for
   storage instead of selling it separately, and is already the account the
   studio is deployed from.

   STORAGE. Netlify Blobs, one store, with key prefixes standing in for
   tables:
     acct:<email>          the account and its profile
     tok:<token>           a pending sign-in link (15 min)
     sess:<session>        an active session (90 days)
     u:<email>:projects    that user's saved designs
     u:<email>:chats       their assistant transcript
     u:<email>:history     their search history
     lib:index             the shared, public Library
     idx:accounts          the list of account emails, for the admin page

   Blobs has no TTL, so expiry is a stored timestamp checked on read. That
   is the honest way round: a key that has outlived its window is treated
   as absent even if the bytes are still there.

   EMAIL. Whichever of Brevo or Resend has a key set. Both have a free tier
   that sends from a single verified address without owning a domain, which
   is what a Gmail-based sender needs. With neither configured the auth
   routes say so plainly rather than pretending a link was sent.
   ===================================================================== */

import { getStore } from "@netlify/blobs";
import crypto from "node:crypto";

const STORE = "greenvision";

/* Anything the app might be served from. The desktop app runs on file://,
   which sends `Origin: null`, so "*" is the only value that works for it -
   and every route that matters is already gated on a bearer session, which
   an attacker's page cannot read out of the app's storage. */
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization"
};

const LIMITS = {
  library: 60, projects: 60, chats: 400, history: 60,
  bodyBytes: 900000
};
const TOKEN_TTL_MS = 15 * 60 * 1000;
const INVITE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const SESSION_TTL_MS = 90 * 24 * 60 * 60 * 1000;

const json = (status, obj) =>
  new Response(JSON.stringify(obj), {
    status, headers: { ...CORS, "content-type": "application/json" }
  });
const html = (status, s) =>
  new Response(s, {
    status, headers: { ...CORS, "content-type": "text/html; charset=utf-8" }
  });

const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const isEmail = e => /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(e);

/* ---------- storage helpers ---------- */
function db() { return getStore({ name: STORE, consistency: "strong" }); }

async function get(key) {
  try { return await db().get(key, { type: "json" }); }
  catch { return null; }
}
async function set(key, value) { await db().setJSON(key, value); }
async function del(key) { try { await db().delete(key); } catch { /* gone */ } }

/* Expiry is ours to enforce: Blobs has no TTL. A record past its window is
   absent, and is deleted on the way out so the store does not grow
   forever. */
async function getFresh(key) {
  const rec = await get(key);
  if (!rec) return null;
  if (rec.expires && Date.now() > rec.expires) { await del(key); return null; }
  return rec;
}

/* ---------- email ---------- */
const SENDER = process.env.SENDER_EMAIL || "greenvision.support@gmail.com";
const SENDER_NAME = "Green Vision";

/* Gmail first, deliberately.

   The point of moving off Pipedream was to stop depending on somebody
   else's free tier, and signing up for Brevo or Resend just puts that
   dependency back in a different place. Gmail needs no new account at all:
   greenvision.support@gmail.com already exists, it already sends the
   branded mail this app used to send through Pipedream, and a free Google
   account will relay 500 messages a day - which for sign-in links and
   invitations is effectively unlimited.

   It needs an APP PASSWORD, not the account password. Google stopped
   accepting the real password for SMTP in 2022; an app password is a
   16-character credential minted per-application at
   myaccount.google.com/apppasswords (2-Step Verification must be on), and
   it can be revoked on its own without touching the account.

   The connection is SMTPS on 465 - implicit TLS, so the credential is
   never sent in the clear, and no STARTTLS upgrade to be stripped. */
async function sendMail({ to, subject, html: body, text }) {
  if (process.env.GMAIL_USER && process.env.GMAIL_APP_PASSWORD) {
    const nodemailer = (await import("nodemailer")).default;
    const tx = nodemailer.createTransport({
      host: "smtp.gmail.com", port: 465, secure: true,
      auth: { user: process.env.GMAIL_USER,
              // Google prints app passwords in groups of four. People paste
              // them exactly as shown, and SMTP then rejects a credential
              // that is correct apart from three spaces.
              pass: String(process.env.GMAIL_APP_PASSWORD).replace(/\s+/g, "") }
    });
    await tx.sendMail({
      from: '"' + SENDER_NAME + '" <' + process.env.GMAIL_USER + '>',
      to, subject, text, html: body
    });
    return "gmail";
  }
  if (process.env.BREVO_KEY) {
    const r = await fetch("https://api.brevo.com/v3/smtp/email", {
      method: "POST",
      headers: { "api-key": process.env.BREVO_KEY, "content-type": "application/json" },
      body: JSON.stringify({
        sender: { email: SENDER, name: SENDER_NAME },
        to: [{ email: to }], subject, htmlContent: body, textContent: text
      })
    });
    if (!r.ok) throw new Error("Brevo " + r.status + " " + (await r.text()).slice(0, 200));
    return "brevo";
  }
  if (process.env.RESEND_KEY) {
    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: "Bearer " + process.env.RESEND_KEY,
                 "content-type": "application/json" },
      body: JSON.stringify({ from: SENDER_NAME + " <" + SENDER + ">",
                             to: [to], subject, html: body, text })
    });
    if (!r.ok) throw new Error("Resend " + r.status + " " + (await r.text()).slice(0, 200));
    return "resend";
  }
  const e = new Error("no email provider configured");
  e.code = "NO_MAILER";
  throw e;
}

/* ---------- the branded emails ---------- */
const SITE = process.env.SITE_URL || "https://green-vision-india.netlify.app";
/* Two URLs, because they are two different things.

   DOWNLOAD is the page a person is sent to - branded, in plain English, with
   the SmartScreen warning explained before they meet it. It is what goes in
   emails and in the app's update prompt.

   DOWNLOAD_FILE is where the bytes actually are: a GitHub release asset.
   GitHub does not meter bandwidth on those, and the installer is 178 MB, so
   every colleague who downloaded it from the web host was spending that
   site's allowance. The page is a few kilobytes; the file is not on it. */
const DOWNLOAD = process.env.DOWNLOAD_URL || "https://green-vision-download.netlify.app/";
const DOWNLOAD_FILE = process.env.DOWNLOAD_FILE || "https://github.com/RKTHEGOAT837/green-vision-releases/releases/latest/download/GreenVision-1.0.0-x64.exe";

const SHELL = (inner, pre) => `<!doctype html><html><body style="margin:0;padding:0;background:#f2f5f3;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">${esc(pre)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f5f3;padding:32px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#fff;border-radius:16px;border:1px solid #dfe6e1;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
<tr><td style="padding:26px 28px 8px;">
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="width:44px;height:44px;vertical-align:middle;">
<img src="${SITE}/brand/green-vision-mark-128.png" width="44" height="44" alt="Green Vision" style="display:block;width:44px;height:44px;border:0;border-radius:12px;"/></td>
<td style="padding-left:11px;">
<div style="font-size:17px;font-weight:800;color:#0f2a22;letter-spacing:-.2px;">Green Vision</div>
<div style="font-size:10.5px;font-weight:700;color:#5c7168;letter-spacing:.12em;">ENVIRONMENTAL INTELLIGENCE</div>
</td></tr></table></td></tr>
${inner}
<tr><td style="padding:18px 28px 24px;">
<div style="border-top:1px solid #e6ece8;padding-top:14px;font-size:11px;line-height:1.6;color:#8a9a92;">
Green Vision — tree-planting prioritisation for Indian cities.</div>
</td></tr></table></td></tr></table></body></html>`;

const BUTTON = (href, label) => `<tr><td style="padding:0 28px 4px;">
<a href="${href}" style="display:block;background:#0e9f6e;color:#fff;text-decoration:none;text-align:center;padding:14px 18px;border-radius:12px;font-size:15px;font-weight:700;">${label}</a>
</td></tr>`;

const FALLBACK = link => `<tr><td style="padding:16px 28px 0;">
<p style="margin:0;font-size:12px;line-height:1.6;color:#5c7168;">This link works once and expires in 15 minutes.</p>
<p style="margin:10px 0 0;font-size:12px;line-height:1.6;color:#5c7168;">If you did not ask to sign in, ignore this email — nothing happens until the link is clicked.</p>
<p style="margin:10px 0 0;font-size:11px;line-height:1.6;color:#8a9a92;">Button not working? Paste this into your browser:<br>
<span style="word-break:break-all;color:#5c7168;">${link}</span></p></td></tr>`;

function mailFor(kind, { name, link, profile, note }) {
  const hi = name ? `Hello ${esc(name)},` : "Hello,";
  if (kind === "welcome" || kind === "signin") {
    const isNew = kind === "welcome";
    return {
      subject: isNew ? "Welcome to Green Vision — confirm your email"
                     : "Your Green Vision sign-in link",
      html: SHELL(`<tr><td style="padding:14px 28px 0;">
        <h1 style="margin:0 0 10px;font-size:20px;line-height:1.3;color:#0f2a22;font-weight:800;">
          ${isNew ? "Welcome to Green Vision" : "Your sign-in link"}</h1>
        <p style="margin:0 0 6px;font-size:14px;line-height:1.65;color:#33473f;">${hi}</p>
        <p style="margin:0 0 18px;font-size:14px;line-height:1.65;color:#33473f;">
          ${isNew ? "Your account is ready. Click below to confirm this address and open the app, signed in. There is no password to remember."
                  : "Click the button below and Green Vision will open, signed in."}</p>
        </td></tr>
        ${BUTTON(link, isNew ? "Confirm and open Green Vision" : "Sign in to Green Vision")}
        ${FALLBACK(link)}`, "Your Green Vision sign-in link"),
      text: [hi, "", "Your Green Vision sign-in link (works once, expires in 15 minutes):",
             "", link, "", "If you did not ask to sign in, ignore this email."].join("\n")
    };
  }
  if (kind === "invite") {
    const p = profile || {};
    const rows = [["Name", p.name], ["Organisation", p.org], ["Role", p.role],
                  ["City", p.city], ["State", p.state], ["Sign in with", p.email]]
      .filter(([, v]) => v)
      .map(([k, v]) => `<tr><td style="padding:3px 0;font-size:12px;color:#8a9a92;width:118px;">${k}</td>
        <td style="padding:3px 0;font-size:12px;color:#33473f;font-weight:600;">${esc(v)}</td></tr>`).join("");
    return {
      subject: "You have been given access to Green Vision",
      html: SHELL(`<tr><td style="padding:14px 28px 0;">
        <h1 style="margin:0 0 10px;font-size:20px;line-height:1.3;color:#0f2a22;font-weight:800;">
          You have been given access to Green Vision</h1>
        <p style="margin:0 0 14px;font-size:14px;line-height:1.65;color:#33473f;">${hi}</p>
        ${note ? `<p style="margin:0 0 14px;padding:12px 14px;background:#f7faf8;border-radius:10px;font-size:13.5px;line-height:1.6;color:#33473f;">${esc(note)}</p>` : ""}
        <p style="margin:0 0 16px;font-size:14px;line-height:1.65;color:#33473f;">
          Green Vision works out where a city should plant trees, and what it would cost.
          Air quality, canopy loss, traffic bottlenecks, soil and a 25-year projection, on one map.</p>
        </td></tr>
        ${BUTTON(DOWNLOAD, "Download for Windows &nbsp;·&nbsp; 177 MB")}
        <tr><td style="padding:14px 28px 0;">
          <p style="margin:0 0 8px;font-size:13px;font-weight:700;color:#0f2a22;">Your account</p>
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 14px;background:#f7faf8;border-radius:10px;padding:10px 12px;width:100%;">${rows}</table>
          <p style="margin:0 0 14px;font-size:13px;line-height:1.65;color:#33473f;">
            It is already set up — open the app, click <b>Sign in</b>, and enter
            <b>${esc((profile || {}).email || "")}</b>. A link arrives in seconds.</p>
          <p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#0f2a22;">Windows will warn you — this is expected</p>
          <p style="margin:0 0 14px;font-size:12.5px;line-height:1.6;color:#5c7168;">
            The installer is not code-signed, so SmartScreen shows “Windows protected your PC”.
            Click <b>More info</b>, then <b>Run anyway</b>. Nothing else needs installing — the
            analysis engine and a 2.6-million-feature map index are inside the download.</p>
        </td></tr>`, "Download Green Vision and sign in"),
      text: [hi, "", note || "You have been given access to Green Vision.", "",
             "Download for Windows: " + DOWNLOAD, "",
             "Open the app, click Sign in, and enter: " + ((profile || {}).email || ""), "",
             "Windows will warn about an unsigned installer — More info, then Run anyway."].join("\n")
    };
  }
  // notice
  return {
    subject: "You signed in to Green Vision",
    html: SHELL(`<tr><td style="padding:14px 28px 0;">
      <h1 style="margin:0 0 10px;font-size:19px;color:#0f2a22;font-weight:800;">You signed in to Green Vision</h1>
      <p style="margin:0 0 6px;font-size:14px;line-height:1.65;color:#33473f;">${hi}</p>
      <p style="margin:0 0 14px;font-size:14px;line-height:1.65;color:#33473f;">
        Your account was signed in just now.</p>
      <p style="margin:0;font-size:13px;line-height:1.65;color:#5c7168;">
        If that was you, nothing to do. If it was not, reply to this email — the link that
        did it has already been used and cannot be reused.</p></td></tr>`,
      "A sign-in to your Green Vision account"),
    text: [hi, "", "Your Green Vision account was signed in just now.",
           "", "If that was not you, reply to this email."].join("\n")
  };
}

/* The two pages a reader can land on after clicking a link in their mail. */
function page(title, body, deep, opts) {
  /* The label says what the button does AND why, because the page it sits on
     is the one moment a reader is most likely to be confused: they clicked a
     link in their email and landed in a browser, when what they wanted was to
     be signed in to an app. "Open Green Vision" does not tell them that
     pressing it is what completes the sign-in. */
  const btn = deep ? `<a class="b" href="${deep}">Click here to open Green Vision and sign in</a>` : "";
  /* Somebody clicking a sign-in link on a machine with no Green Vision on it
     sees a button that does nothing: the greenvision:// protocol is
     registered by the installer, so with no installer there is no handler and
     the browser silently drops it. That is the most likely first experience
     of an invited colleague, and the page said nothing about it.

     The download is offered as a quiet second line rather than a second
     button, because for everybody who DOES have the app it is noise. */
  const dl = (opts && opts.download === false) ? "" : `
    <p class="dl">Don't have Green Vision on this computer?
      <a href="${DOWNLOAD}">Download it for Windows</a> — then click your
      sign-in link again.</p>`;
  const go = deep ? `<script>setTimeout(function(){location.href=${JSON.stringify(deep)};},400);<\/script>` : "";
  return `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)} · Green Vision</title><style>
body{margin:0;background:#f2f5f3;font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#33473f;display:grid;place-items:center;min-height:100vh;padding:24px;}
.c{max-width:460px;background:#fff;border:1px solid #dfe6e1;border-radius:16px;padding:28px;text-align:center;}
.l{width:48px;height:48px;border-radius:14px;display:block;margin:0 auto 16px;}
h1{margin:0 0 10px;font-size:20px;color:#0f2a22;font-weight:800;}p{margin:0 0 18px;font-size:14px;}
.b{display:block;background:#0e9f6e;color:#fff;text-decoration:none;padding:13px;border-radius:12px;font-weight:700;}
.f{margin-top:18px;font-size:11.5px;color:#8a9a92;}
.dl{margin:16px 0 0;font-size:12.5px;color:#5c7168;}
.dl a{color:#0e9f6e;font-weight:700;}
</style></head><body><div class="c">
<img class="l" src="${SITE}/brand/green-vision-mark-128.png" alt="Green Vision" width="48" height="48">
<h1>${esc(title)}</h1><p>${esc(body)}</p>${btn}${dl}
<div class="f">Green Vision — tree-planting prioritisation for Indian cities.</div>
</div>${go}</body></html>`;
}

/* ---------- the handler ---------- */
export default async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  const url = new URL(req.url);
  // Strip the function prefix so routes read the same as they did on the
  // old host, whether Netlify called us through /api/* or directly.
  const path = url.pathname
    .replace(/^\/\.netlify\/functions\/api/, "")
    .replace(/^\/api/, "")
    .replace(/\/+$/, "") || "/";
  const method = req.method.toUpperCase();

  let body = {};
  if (method !== "GET") {
    try {
      const raw = await req.text();
      if (raw.length > LIMITS.bodyBytes) return json(413, { error: "too large" });
      body = raw ? JSON.parse(raw) : {};
    } catch { body = {}; }
    if (!body || typeof body !== "object") body = {};
  }

  try {
    /* ---------- health ---------- */
    if (path === "/health" || path === "/") {
      return json(200, { ok: true, service: "green-vision-api",
                         mailer: (process.env.GMAIL_USER && process.env.GMAIL_APP_PASSWORD)
                                   ? "gmail"
                               : process.env.BREVO_KEY ? "brevo"
                               : process.env.RESEND_KEY ? "resend" : null });
    }

    /* ---------- ask for a sign-in link ---------- */
    if (method === "POST" && path === "/auth/request") {
      const email = String(body.email || "").trim().toLowerCase();
      const state = String(body.state || "");
      if (!isEmail(email)) return json(400, { error: "invalid email" });
      if (!state || state.length < 16) return json(400, { error: "missing state" });

      /* Rate limit per address, or this is a free mail cannon pointed at
         anyone whose address you can type - and it is our sending
         reputation that pays for it. */
      const rlKey = "rl:" + email;
      const now = Date.now();
      const hits = ((await get(rlKey)) || []).filter(t => now - t < 60000);
      if (hits.length >= 3) return json(429, { error: "Too many sign-in emails. Wait a minute." });
      await set(rlKey, [...hits, now]);

      const profile = body.profile && typeof body.profile === "object" ? body.profile : {};
      const token = crypto.randomBytes(32).toString("base64url");
      await set("tok:" + token,
                { email, state, profile, created: now, used: false,
                  expires: now + TOKEN_TTL_MS });

      const existing = await get("acct:" + email);
      const isNew = !existing;
      const merged = { ...((existing && existing.profile) || {}) };
      for (const [k, v] of Object.entries(profile)) {
        if (v !== "" && v != null) merged[k] = typeof v === "string" ? v.slice(0, 200) : v;
      }
      await set("acct:" + email, {
        email, profile: merged,
        created: (existing && existing.created) || now,
        updated: now, sign_ins: (existing && existing.sign_ins) || 0
      });
      if (isNew) {
        const idx = (await get("idx:accounts")) || [];
        if (!idx.includes(email)) await set("idx:accounts", [...idx, email]);
      }

      const link = url.origin + "/auth/verify?token=" + encodeURIComponent(token);
      const m = mailFor(isNew ? "welcome" : "signin",
                        { name: merged.name ? String(merged.name).split(" ")[0] : null, link });
      try {
        await sendMail({ to: email, subject: m.subject, html: m.html, text: m.text });
      } catch (e) {
        if (e.code === "NO_MAILER") {
          return json(503, { error: "No email provider is configured on the server. " +
            "Set GMAIL_USER and GMAIL_APP_PASSWORD (an app password from " +
            "myaccount.google.com/apppasswords, with 2-Step Verification on), " +
            "or BREVO_KEY / RESEND_KEY." });
        }
        return json(502, { error: "Could not send the email: " + e.message });
      }
      return json(200, { ok: true, isNew });
    }

    /* ---------- the click in the mail ----------
       This must NOT spend the token. Mail scanners follow links before the
       reader ever sees them, and a token spent by a scanner is a sign-in
       the reader can never complete. The page hands the token to the app,
       and /auth/exchange is what consumes it. */
    if (method === "GET" && path === "/auth/verify") {
      const token = url.searchParams.get("token") || "";
      const rec = token ? await getFresh("tok:" + token) : null;
      if (!rec) return html(200, page("This link has expired",
        "Sign-in links last 15 minutes and work once. Open Green Vision and ask for a new one."));
      if (rec.used) return html(200, page("This link has already been used",
        "Open Green Vision and ask for a new one."));
      const deep = "greenvision://auth?token=" + encodeURIComponent(token) +
                   "&state=" + encodeURIComponent(rec.state || "");
      return html(200, page("Opening Green Vision…",
        "If nothing happens, use the button below.", deep));
    }

    /* ---------- the app exchanges the token for a session ---------- */
    if (method === "POST" && path === "/auth/exchange") {
      const token = String(body.token || "");
      const state = String(body.state || "");
      const rec = token ? await getFresh("tok:" + token) : null;
      if (!rec) return json(400, { error: "expired" });
      if (rec.used) return json(400, { error: "already used" });
      /* The state proves this callback belongs to a sign-in THIS copy of
         the app started. Without it, any page could fire a greenvision://
         link and sign the reader into somebody else's account. */
      if (!state || state !== rec.state) return json(400, { error: "state mismatch" });

      await set("tok:" + token, { ...rec, used: true });

      const now = Date.now();
      const acct = (await get("acct:" + rec.email)) || { email: rec.email, profile: {}, created: now };
      const isFirst = !acct.sign_ins;
      const updated = { ...acct, sign_ins: (acct.sign_ins || 0) + 1, last_seen: now };
      await set("acct:" + rec.email, updated);

      const session = crypto.randomBytes(32).toString("base64url");
      await set("sess:" + session,
                { email: rec.email, issued: now, expires: now + SESSION_TTL_MS });

      if (!isFirst) {
        const m = mailFor("notice", {
          name: (updated.profile || {}).name ? String(updated.profile.name).split(" ")[0] : null });
        try { await sendMail({ to: rec.email, subject: m.subject, html: m.html, text: m.text }); }
        catch { /* a sign-in must not fail because a notice did */ }
      }
      return json(200, { ok: true, email: rec.email, session,
                         profile: updated.profile || {}, sign_ins: updated.sign_ins,
                         isNew: isFirst });
    }

    /* ---------- the public Library ---------- */
    if (method === "GET" && path === "/library") {
      const index = (await get("lib:index")) || [];
      return json(200, { ok: true, designs: index.slice(0, LIMITS.library) });
    }

    /* ---------- admin: a username and password, not a magic link ----------
       Different audience, different mechanism. This is a list of everybody
       who has an account, so it is gated on a shared credential held in the
       environment rather than on an emailed link to one of them. */
    if (method === "POST" && path === "/admin/login") {
      const u = String(body.username || ""), p = String(body.password || "");
      const U = process.env.ADMIN_USER || "greenvision-admin";
      const P = process.env.ADMIN_PASS || "ChangeThisNow!2026";
      /* Constant-time-ish compare. Length is allowed to leak; the bytes are
         not, so a timing oracle cannot walk the password out character by
         character. */
      const eq = (a, b) => a.length === b.length &&
        crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));
      if (!eq(u, U) || !eq(p, P)) {
        await new Promise(r => setTimeout(r, 400));   // blunt the guessing rate
        return json(401, { error: "wrong username or password" });
      }
      const t = crypto.randomBytes(32).toString("base64url");
      await set("adm:" + t, { user: u, issued: Date.now(),
                              expires: Date.now() + 12 * 60 * 60 * 1000 });
      return json(200, { ok: true, token: t, user: u });
    }

    const bearer = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();

    /* Mint a sign-in link without sending mail. ADMIN ONLY.

       Two reasons it exists. It is the fallback when the mail provider is
       down or unconfigured - an administrator can still get somebody in
       rather than the whole product being blocked on a third party. And it
       is what makes the auth flow testable end to end without a mailbox,
       which is how the rest of this file was verified.

       It is exactly as dangerous as it sounds, so it is gated on the admin
       credential and nothing else: with it you can sign in AS anybody. The
       ordinary /auth/request route is the one users touch, and it only ever
       sends the link to the address that asked for it. */
    if (method === "POST" && path === "/admin/signin-link") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });
      const email = String(body.email || "").trim().toLowerCase();
      const state = String(body.state || "");
      if (!isEmail(email)) return json(400, { error: "invalid email" });
      if (!state || state.length < 16) return json(400, { error: "missing state" });

      const now = Date.now();
      const token = crypto.randomBytes(32).toString("base64url");
      await set("tok:" + token, { email, state, profile: {}, created: now,
                                  used: false, expires: now + INVITE_TTL_MS });
      const existing = await get("acct:" + email);
      if (!existing) {
        await set("acct:" + email, { email, profile: {}, created: now,
                                     updated: now, sign_ins: 0 });
        const idx = (await get("idx:accounts")) || [];
        if (!idx.includes(email)) await set("idx:accounts", [...idx, email]);
      }
      return json(200, { ok: true, email,
        verify: url.origin + "/auth/verify?token=" + encodeURIComponent(token),
        deep: "greenvision://auth?token=" + encodeURIComponent(token) +
              "&state=" + encodeURIComponent(state),
        token, expires_in_days: 7 });
    }

    /* ---------- what the app is told when it starts ----------

       Two things an administrator needs and had no way to do: tell everybody
       an update exists, and stop the app working if something is badly wrong
       with a build that is already on people's machines.

       PUBLIC on purpose. The app asks for this before anybody signs in - a
       broken build has to be stoppable for signed-out users too - and it
       carries nothing secret: a message, a version and a flag.

       FAIL-OPEN is the rule on the app side, not here. This endpoint being
       unreachable must never disable anybody: the app runs offline by
       design, and an aeroplane must not look like a revoked licence. */
    if (method === "GET" && path === "/app/notice") {
      const n = (await get("app:notice")) || {};
      return json(200, {
        ok: true,
        killed: !!n.killed,
        message: n.message || "",
        title: n.title || "",
        latest: n.latest || "",          // newest version available
        min: n.min || "",                // below this, the app should insist
        download: DOWNLOAD,              // the friendly page
        file: n.file || DOWNLOAD_FILE,   // the installer itself
        sha256: n.sha256 || "",
        size: n.size || "",
        // When set, only this signed-in address should act on the notice.
        // It exists so an administrator can rehearse an announcement without
        // interrupting everybody who has the app installed - the alternative
        // being to test on the whole userbase, which nobody sensible will do,
        // so the feature would go untested instead.
        only: n.only || "",
        updated: n.updated || 0
      });
    }

    if (method === "POST" && path === "/admin/notice") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });
      const rec = {
        killed: !!body.killed,
        title: String(body.title || "").slice(0, 120),
        message: String(body.message || "").slice(0, 2000),
        latest: String(body.latest || "").slice(0, 20),
        min: String(body.min || "").slice(0, 20),
        // What the download page shows about the current build. Kept here
        // rather than hard-coded into the page so a new release is one form
        // on the admin screen, not an edit and a redeploy.
        file: String(body.file || "").slice(0, 400),
        sha256: String(body.sha256 || "").replace(/[^0-9a-fA-F]/g, "").slice(0, 64),
        size: String(body.size || "").slice(0, 20),
        only: String(body.only || "").trim().toLowerCase().slice(0, 200),
        updated: Date.now(),
        by: adm.user
      };
      await set("app:notice", rec);
      return json(200, { ok: true, notice: rec });
    }

    if (method === "GET" && path === "/admin/users") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });
      const idx = (await get("idx:accounts")) || [];
      const rows = [];
      for (const email of idx) {
        const a = await get("acct:" + email);
        if (!a) continue;
        const [proj, chats, hist] = await Promise.all([
          get("u:" + email + ":projects"), get("u:" + email + ":chats"),
          get("u:" + email + ":history")
        ]);
        rows.push({
          email, profile: a.profile || {}, created: a.created,
          updated: a.updated, last_seen: a.last_seen, sign_ins: a.sign_ins || 0,
          counts: { projects: (proj || []).length, chats: (chats || []).length,
                    history: (hist || []).length }
        });
      }
      rows.sort((x, y) => (y.last_seen || y.created || 0) - (x.last_seen || x.created || 0));
      return json(200, { ok: true, users: rows, total: rows.length });
    }

    /* ---------- invite a colleague (key-gated) ---------- */
    if (method === "POST" && path === "/invite") {
      const KEY = process.env.INVITE_KEY || "SKKJPWnWu5reNl9UBfrVUUl_n4wQ9htJ";
      if (String(body.key || "") !== KEY) return json(403, { error: "bad key" });
      const to = String(body.email || "").trim().toLowerCase();
      if (!isEmail(to)) return json(400, { error: "invalid email" });

      const now = Date.now();
      const prof = {};
      for (const k of ["name", "org", "role", "city", "state"]) {
        const v = String(body[k] || "").trim();
        if (v) prof[k] = v.slice(0, 200);
      }
      const had = await get("acct:" + to);
      const merged = { ...((had && had.profile) || {}), ...prof };
      await set("acct:" + to, {
        email: to, profile: merged,
        created: (had && had.created) || now, updated: now,
        sign_ins: (had && had.sign_ins) || 0
      });
      if (!had) {
        const idx = (await get("idx:accounts")) || [];
        if (!idx.includes(to)) await set("idx:accounts", [...idx, to]);
      }

      const m = mailFor("invite", {
        name: merged.name ? String(merged.name).split(" ")[0] : null,
        profile: { ...merged, email: to },
        note: String(body.note || "").trim().slice(0, 600)
      });
      try { await sendMail({ to, subject: m.subject, html: m.html, text: m.text }); }
      catch (e) {
        if (e.code === "NO_MAILER") return json(503, {
          error: "No email provider configured. Set GMAIL_USER and " +
                 "GMAIL_APP_PASSWORD on the API site." });
        return json(502, { error: "Could not send: " + e.message });
      }
      return json(200, { ok: true, sent: to, isNew: !had });
    }

    /* ---------- everything below needs a user session ---------- */
    const sess = bearer ? await getFresh("sess:" + bearer) : null;
    if (!sess) return json(401, { error: "sign in first" });
    const email = sess.email;
    const key = suffix => "u:" + email + ":" + suffix;
    const acct = (await get("acct:" + email)) || { email, profile: {} };

    /* ---------- profile ---------- */
    if (path === "/me") {
      if (method === "GET") {
        const [projects, chats, history] = await Promise.all([
          get(key("projects")), get(key("chats")), get(key("history"))
        ]);
        return json(200, { ok: true, email, profile: acct.profile || {},
          created: acct.created, sign_ins: acct.sign_ins || 0,
          counts: { projects: (projects || []).length, chats: (chats || []).length,
                    history: (history || []).length } });
      }
      if (method === "PUT") {
        const p = { ...(acct.profile || {}) };
        for (const [k, v] of Object.entries(body.profile || {})) {
          if (v === null) delete p[k];
          else if (v !== "") p[k] = typeof v === "string" ? v.slice(0, 2000) : v;
        }
        await set("acct:" + email, { ...acct, profile: p, updated: Date.now() });
        return json(200, { ok: true, profile: p });
      }
    }

    /* ---------- projects ---------- */
    if (path === "/projects") {
      const list = (await get(key("projects"))) || [];
      if (method === "GET") return json(200, { ok: true, projects: list });
      if (method === "PUT") {
        const d = body.design;
        if (!d || !d.id) return json(400, { error: "no design" });
        const row = { id: d.id, name: d.name || "Untitled", place: d.place || "",
                      city: d.city || "", goal: d.goal || "park",
                      n_items: (d.items || []).length,
                      n_trees: (d.items || []).filter(i => i.k === "tree").length,
                      area_m2: d.plot ? Math.round(d.plot.area_m2 || 0) : 0,
                      updated: Date.now(), design: d };
        const next = [row, ...list.filter(x => x.id !== d.id)].slice(0, LIMITS.projects);
        await set(key("projects"), next);
        return json(200, { ok: true, saved: row.id, count: next.length });
      }
    }
    if (method === "POST" && path === "/projects/delete") {
      const list = (await get(key("projects"))) || [];
      const next = list.filter(x => x.id !== body.id);
      await set(key("projects"), next);
      return json(200, { ok: true, count: next.length });
    }

    /* ---------- publishing to the shared Library ---------- */
    if (method === "POST" && path === "/library") {
      const d = body.design;
      if (!d || !d.id) return json(400, { error: "no design" });
      const p = acct.profile || {};
      const entry = { id: d.id, name: d.name || "Untitled",
        by: p.name || email.split("@")[0], org: p.org || "", role: p.role || "",
        city: d.city || p.city || "", place: d.place || "", goal: d.goal || "park",
        n_trees: (d.items || []).filter(i => i.k === "tree").length,
        area_m2: d.plot ? Math.round(d.plot.area_m2 || 0) : 0,
        published: Date.now(), design: d };
      const index = (await get("lib:index")) || [];
      const next = [entry, ...index.filter(x => x.id !== d.id)].slice(0, LIMITS.library);
      await set("lib:index", next);
      return json(200, { ok: true, published: entry.id, count: next.length });
    }
    if (method === "POST" && path === "/library/delete") {
      const index = (await get("lib:index")) || [];
      const mine = index.find(x => x.id === body.id);
      // Only the person who published it, or an admin, may withdraw it.
      const admin = bearer ? await getFresh("adm:" + bearer) : null;
      if (mine && !admin && mine.by !== ((acct.profile || {}).name || email.split("@")[0])) {
        return json(403, { error: "not yours to remove" });
      }
      await set("lib:index", index.filter(x => x.id !== body.id));
      return json(200, { ok: true });
    }

    /* ---------- the assistant's memory ---------- */
    if (path === "/chats") {
      const log = (await get(key("chats"))) || [];
      if (method === "GET") return json(200, { ok: true, chats: log });
      if (method === "POST") {
        const turn = { role: body.role === "bot" ? "bot" : "me",
                       text: String(body.text || "").slice(0, 4000),
                       place: body.place || "", intent: body.intent || "",
                       at: Date.now() };
        const next = [...log, turn].slice(-LIMITS.chats);
        await set(key("chats"), next);
        return json(200, { ok: true, count: next.length });
      }
    }
    if (method === "POST" && path === "/chats/clear") {
      await set(key("chats"), []);
      return json(200, { ok: true, count: 0 });
    }

    /* ---------- search history ---------- */
    if (path === "/history") {
      const list = (await get(key("history"))) || [];
      if (method === "GET") return json(200, { ok: true, history: list });
      if (method === "POST") {
        const row = { place: String(body.place || "").slice(0, 200),
                      lat: body.lat, lon: body.lon, at: Date.now() };
        if (!row.place) return json(400, { error: "no place" });
        const next = [row, ...list.filter(x => x.place !== row.place)].slice(0, LIMITS.history);
        await set(key("history"), next);
        return json(200, { ok: true, count: next.length });
      }
    }

    return json(404, { error: "not found", path });
  } catch (e) {
    // Never return Netlify's own error page: the client reads JSON, and an
    // HTML body at any status is the failure mode that made Pipedream's
    // outage invisible.
    return json(500, { error: String((e && e.message) || e) });
  }
};
