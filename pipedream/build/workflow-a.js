/* =====================================================================
   Pipedream workflow 1 of 2 — POST /auth/request
   =====================================================================
   Trigger:  HTTP / Webhook, "Return a custom response from your workflow"
   Props:    a Data Store named `db`, and your Gmail account named `gmail`
   Env:      GV_FROM_EMAIL   the Green Vision Gmail address
             GV_VERIFY_URL   workflow 2's trigger URL

   Takes an email address and the onboarding answers from the desktop app,
   mints a single-use token, stores it for fifteen minutes, and sends the
   sign-in email.

   The token is 32 random bytes, not a JWT. Nothing here needs to be
   verified offline, and a JWT would put the profile into a string the
   reader can paste anywhere. An opaque handle to a row leaks nothing if
   the email is forwarded.
   ===================================================================== */

import crypto from "crypto";


/* ---- email helpers (kept in pipedream/_email.js) ---- */
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const SHELL = (inner, preheader) => `<!doctype html>
<html><body style="margin:0;padding:0;background:#f2f5f3;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">${escapeHtml(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#f2f5f3;padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:520px;background:#ffffff;border-radius:16px;
                    border:1px solid #dfe6e1;overflow:hidden;
                    font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
        <tr><td style="padding:26px 28px 8px;">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="width:38px;height:38px;background:#0e9f6e;border-radius:11px;
                       text-align:center;vertical-align:middle;font-size:20px;">&#127793;</td>
            <td style="padding-left:11px;">
              <div style="font-size:17px;font-weight:800;color:#0f2a22;letter-spacing:-.2px;">Green Vision</div>
              <div style="font-size:10.5px;font-weight:700;color:#5c7168;letter-spacing:.12em;">ENVIRONMENTAL INTELLIGENCE</div>
            </td>
          </tr></table>
        </td></tr>
        ${inner}
        <tr><td style="padding:18px 28px 24px;">
          <div style="border-top:1px solid #e6ece8;padding-top:14px;
                      font-size:11px;line-height:1.6;color:#8a9a92;">
            Green Vision — tree-planting prioritisation for Indian cities.
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>`;

const BUTTON = (href, label) => `
  <tr><td style="padding:0 28px 4px;">
    <a href="${href}" style="display:block;background:#0e9f6e;color:#ffffff;text-decoration:none;
       text-align:center;padding:14px 18px;border-radius:12px;font-size:15px;font-weight:700;">${label}</a>
  </td></tr>`;

const FALLBACK = link => `
  <tr><td style="padding:16px 28px 0;">
    <p style="margin:0;font-size:12px;line-height:1.6;color:#5c7168;">
      This link works once and expires in 15 minutes.</p>
    <p style="margin:10px 0 0;font-size:12px;line-height:1.6;color:#5c7168;">
      If you did not ask to sign in, ignore this email — nothing happens until the
      link is clicked, and it expires on its own.</p>
    <p style="margin:10px 0 0;font-size:11px;line-height:1.6;color:#8a9a92;">
      Button not working? Paste this into your browser:<br>
      <span style="word-break:break-all;color:#5c7168;">${link}</span></p>
  </td></tr>`;

function brandedEmail({ kind, name, link, profile, when, device }) {
  const hi = name ? `Hello ${escapeHtml(name)},` : "Hello,";

  if (kind === "welcome") {
    const p = profile || {};
    const rows = [
      ["Organisation", p.org], ["Role", p.role],
      ["City", p.city], ["State", p.state]
    ].filter(([, v]) => v).map(([k, v]) =>
      `<tr><td style="padding:3px 0;font-size:12px;color:#8a9a92;width:110px;">${k}</td>
           <td style="padding:3px 0;font-size:12px;color:#33473f;font-weight:600;">${escapeHtml(v)}</td></tr>`
    ).join("");

    return {
      html: SHELL(`
        <tr><td style="padding:14px 28px 0;">
          <h1 style="margin:0 0 10px;font-size:20px;line-height:1.3;color:#0f2a22;font-weight:800;">
            Welcome to Green Vision</h1>
          <p style="margin:0 0 6px;font-size:14px;line-height:1.65;color:#33473f;">${hi}</p>
          <p style="margin:0 0 16px;font-size:14px;line-height:1.65;color:#33473f;">
            Your account is ready. Click below to confirm this address and open the app,
            signed in. There is no password to remember.</p>
          ${rows ? `<table role="presentation" cellpadding="0" cellspacing="0"
             style="margin:0 0 18px;background:#f7faf8;border-radius:10px;padding:10px 12px;width:100%;">
             ${rows}</table>` : ""}
        </td></tr>
        ${BUTTON(link, "Confirm and open Green Vision")}
        <tr><td style="padding:16px 28px 0;">
          <p style="margin:0 0 8px;font-size:13px;font-weight:700;color:#0f2a22;">What you get</p>
          <ul style="margin:0;padding-left:18px;font-size:12.5px;line-height:1.7;color:#33473f;">
            <li>Your projects, chats and search history saved to your account</li>
            <li>The assistant remembers what you have been working on</li>
            <li>Your name and organisation on designs and on the exported BOQ</li>
            <li>A shared Library of what other planners have published</li>
          </ul>
        </td></tr>
        ${FALLBACK(link)}`,
        "Confirm your email and open Green Vision"),
      text: [hi, "", "Welcome to Green Vision. Confirm this address and open the app:",
             "", link, "", "The link works once and expires in 15 minutes.",
             "If you did not sign up, ignore this email."].join("\n")
    };
  }

  if (kind === "notice") {
    return {
      html: SHELL(`
        <tr><td style="padding:14px 28px 0;">
          <h1 style="margin:0 0 10px;font-size:19px;line-height:1.3;color:#0f2a22;font-weight:800;">
            You signed in to Green Vision</h1>
          <p style="margin:0 0 6px;font-size:14px;line-height:1.65;color:#33473f;">${hi}</p>
          <p style="margin:0 0 14px;font-size:14px;line-height:1.65;color:#33473f;">
            Your account was signed in ${when ? "on " + escapeHtml(when) : "just now"}${device ? " from " + escapeHtml(device) : ""}.</p>
          <p style="margin:0 0 6px;font-size:13px;line-height:1.65;color:#5c7168;">
            If that was you, nothing to do. If it was not, reply to this email and we
            will disable the account — the sign-in link that did it has already been
            used and cannot be reused.</p>
        </td></tr>`,
        "A sign-in to your Green Vision account"),
      text: [hi, "", `Your Green Vision account was signed in ${when ? "on " + when : "just now"}${device ? " from " + device : ""}.`,
             "", "If that was not you, reply to this email."].join("\n")
    };
  }

  // kind === "signin"
  return {
    html: SHELL(`
      <tr><td style="padding:14px 28px 0;">
        <h1 style="margin:0 0 10px;font-size:20px;line-height:1.3;color:#0f2a22;font-weight:800;">
          Your sign-in link</h1>
        <p style="margin:0 0 6px;font-size:14px;line-height:1.65;color:#33473f;">${hi}</p>
        <p style="margin:0 0 18px;font-size:14px;line-height:1.65;color:#33473f;">
          Click the button below and Green Vision will open, signed in.</p>
      </td></tr>
      ${BUTTON(link, "Sign in to Green Vision")}
      ${FALLBACK(link)}`,
      "Your Green Vision sign-in link"),
    text: [hi, "", "Here is your Green Vision sign-in link. It works once and expires in 15 minutes:",
           "", link, "", "If you did not ask to sign in, ignore this email."].join("\n")
  };
}

/* Gmail's API wants a raw RFC 2822 message; multipart/alternative so the
   client picks HTML or text for itself. */
async function sendMail({ gmail, from, to, subject, html, text }) {
  const boundary = "gv_" + Math.random().toString(36).slice(2);
  const raw = [
    `From: Green Vision <${from}>`,
    `To: ${to}`,
    `Subject: ${subject}`,
    "MIME-Version: 1.0",
    `Content-Type: multipart/alternative; boundary="${boundary}"`,
    "",
    `--${boundary}`, 'Content-Type: text/plain; charset="UTF-8"', "", text, "",
    `--${boundary}`, 'Content-Type: text/html; charset="UTF-8"', "", html, "",
    `--${boundary}--`
  ].join("\r\n");

  const encoded = Buffer.from(raw).toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  const r = await fetch("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", {
    method: "POST",
    headers: { Authorization: `Bearer ${gmail.$auth.oauth_access_token}`,
               "Content-Type": "application/json" },
    body: JSON.stringify({ raw: encoded })
  });
  if (!r.ok) {
    const t = await r.text().catch(() => "");
    throw new Error("Gmail send failed: " + r.status + " " + t.slice(0, 300));
  }
}

/* ---- the workflow ---- */
export default defineComponent({
  props: {
    db: { type: "data_store" },
    gmail: { type: "app", app: "gmail" }
  },

  async run({ steps, $ }) {
    const body = steps.trigger.event.body || {};
    const email = String(body.email || "").trim().toLowerCase();
    const state = String(body.state || "");
    const profile = body.profile && typeof body.profile === "object" ? body.profile : {};

    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) {
      return $.respond({ status: 400, body: { error: "invalid email" } });
    }
    if (!state || state.length < 16) {
      return $.respond({ status: 400, body: { error: "missing state" } });
    }

    /* Rate limit per address. Without it this endpoint is a free mail
       cannon pointed at anyone whose address you can type, and it is our
       Gmail account that gets suspended for sending it. */
    const now = Date.now();
    const rlKey = "rl:" + email;
    const hits = ((await this.db.get(rlKey)) || []).filter(t => now - t < 60_000);
    if (hits.length >= 3) {
      return $.respond({ status: 429, body: { error: "Too many sign-in emails. Wait a minute." } });
    }
    await this.db.set(rlKey, [...hits, now], { ttl: 120 });

    const token = crypto.randomBytes(32).toString("base64url");
    await this.db.set("tok:" + token, { email, state, profile, created: now, used: false },
                      { ttl: 15 * 60 });

    /* The account row. Upserted here rather than after verification so a
       returning planner keeps what they gave last time; blank answers this
       time do not wipe answers from before. `isNew` decides which email
       they get — a welcome, or a sign-in notice. */
    const acctKey = "acct:" + email;
    const existing = await this.db.get(acctKey);
    const isNew = !existing;
    const merged = { ...((existing && existing.profile) || {}) };
    for (const [k, v] of Object.entries(profile)) {
      if (v !== "" && v != null) merged[k] = typeof v === "string" ? v.slice(0, 200) : v;
    }
    await this.db.set(acctKey, {
      email,
      profile: merged,
      created: (existing && existing.created) || now,
      updated: now,
      sign_ins: (existing && existing.sign_ins) || 0
    });

    const verify = process.env.GV_VERIFY_URL;
    if (!verify) return $.respond({ status: 500, body: { error: "GV_VERIFY_URL is not set" } });
    const link = verify.replace(/\/+$/, "") + "/auth/verify?token=" + encodeURIComponent(token);

    const first = merged.name ? String(merged.name).split(" ")[0] : null;
    await sendMail({
      gmail: this.gmail,
      from: process.env.GV_FROM_EMAIL,
      to: email,
      subject: isNew ? "Welcome to Green Vision — confirm your email"
                     : "Your Green Vision sign-in link",
      ...brandedEmail({ kind: isNew ? "welcome" : "signin", name: first, link, profile: merged })
    });

    return $.respond({ status: 200, body: { ok: true, isNew } });
  }
});
