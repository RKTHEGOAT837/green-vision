/* =====================================================================
   The Green Vision emails
   =====================================================================
   Three of them: welcome (first sign-up), sign-in link (every time after),
   and a sign-in notice (sent after a successful sign-in, so an
   unrecognised one is visible).

   Plain HTML with inlined styles and no external images. Every mail client
   worth supporting renders it, and none of it depends on a CDN that could
   be blocked on a municipal network. A text/plain part goes alongside,
   because a link nobody can click is not a sign-in.

   Pipedream code steps cannot import a sibling file, so paste the contents
   of this file at the TOP of each workflow's code step, above the
   `export default`. It is kept as its own file here so the three workflows
   cannot drift into three different-looking emails.
   ===================================================================== */

export function escapeHtml(s) {
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
            <td style="width:44px;height:44px;vertical-align:middle;">
              <img src="https://green-vision-india.netlify.app/brand/green-vision-mark-128.png"
                   width="44" height="44" alt="Green Vision"
                   style="display:block;width:44px;height:44px;border:0;outline:none;
                          border-radius:12px;" /></td>
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

export function brandedEmail({ kind, name, link, profile, when, device }) {
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
export async function sendMail({ gmail, from, to, subject, html, text }) {
  const boundary = "gv_" + Math.random().toString(36).slice(2);

  /* RFC 2822 headers are ASCII. A raw UTF-8 em-dash in the Subject came
     out of Gmail as "Green Vision Ã¢Â€Â” confirm your email" — the body was
     fine, because its charset is declared, but the header has no such
     declaration and the bytes were read as Latin-1. RFC 2047 is the
     encoding that fixes it, and it is only applied when it is needed so
     an all-ASCII subject stays human-readable in the raw message. */
  const encodeHeader = v =>
    /^[ -~]*$/.test(v)
      ? v
      : "=?UTF-8?B?" + Buffer.from(v, "utf8").toString("base64") + "?=";

  const raw = [
    `From: =?UTF-8?B?${Buffer.from("Green Vision", "utf8").toString("base64")}?= <${from}>`,
    `To: ${to}`,
    `Subject: ${encodeHeader(subject)}`,
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
