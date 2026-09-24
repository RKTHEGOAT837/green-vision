/* =====================================================================
   Green Vision account API - Cloudflare Worker
   =====================================================================
   Ported from the Netlify function, which ran out of credits mid-cycle and
   took production deploys down with it. Netlify meters builds, bandwidth and
   invocations against one 300-credit allowance; this runs on 100,000
   requests a day that reset every day and cost nothing.

   The routes are unchanged. Storage is D1 behind the same key/value helpers,
   configuration comes from `env` instead of `process.env`, and the mail path
   is Brevo's HTTP API because a Worker cannot open an SMTP socket - the
   Gmail transport simply cannot exist here.

   Bindings expected:
     DB                D1 database (see schema.sql)
     BREVO_KEY         Brevo API key            (secret)
     ADMIN_USER        admin username           (secret)
     ADMIN_PASS        admin password           (secret)
     INVITE_KEY        shared secret for /invite (secret)
     SENDER_EMAIL      verified Brevo sender
     SITE_URL          the studio, for email images
     DOWNLOAD_URL      the page people are sent to
     DOWNLOAD_FILE     the installer itself
   ===================================================================== */

/* base64url from Web Crypto, since Workers have neither Buffer nor
   randomBytes. 32 bytes, the same length the Netlify version used. */
/* Set once per request by fetch(), before any route runs.

   These are `let`, not `const`, and they are ASSIGNED per request rather
   than initialised at module load. A Worker evaluates the module once and
   then serves many requests from it, so a `const SENDER = ENV.SENDER_EMAIL`
   at the top level runs before any request exists: it threw on deploy,
   because ENV was not yet defined, and had it not thrown it would have
   captured the fallback for the lifetime of the isolate and ignored every
   configured value thereafter. */
let ENV = {};
let DB = null;
let SENDER, SITE, DOWNLOAD, DOWNLOAD_FILE, DOWNLOAD_MANAGED;

function readEnv(env) {
  ENV = env;
  DB = env.DB;
  SENDER = env.SENDER_EMAIL || "greenvision.support@gmail.com";
  /* The email header logo lives here. This was the old Netlify host, which
     still answered but is a site this project no longer maintains - and
     whose free allowance it had already exhausted once, which is why the
     API moved to Cloudflare in the first place. An email's logo outliving
     the deployment it points at is how a brand mark quietly turns into a
     broken-image icon in somebody's inbox months later.

     The GitHub Pages site that already serves the download pages now holds
     the mark under /brand/ as well, so this is one host fewer to keep
     alive for the whole product. */
  SITE = env.SITE_URL || "https://green-vision-india.netlify.app";
  DOWNLOAD = env.DOWNLOAD_URL || "https://rkthegoat837.github.io/green-vision-releases/";
  // The filename is part of the path even under /releases/latest/, so this
  // fallback pins a version and has to move with the release. It said 1.0.0
  // after 1.0.1 shipped and was a 404.
  DOWNLOAD_FILE = env.DOWNLOAD_FILE ||
    "https://github.com/RKTHEGOAT837/green-vision-releases/releases/latest/download/GreenVision-1.2.2-x64.exe";
  /* The managed edition - the same app with an account required for
     anything at all. It is what invitations point at, because revoking
     somebody's account only removes their access in the build where an
     account is what their access consists of. The open edition runs its
     whole analysis from an engine and a map index on the reader's own
     disk, so taking the account away there leaves a working planner. */
  DOWNLOAD_MANAGED = env.DOWNLOAD_MANAGED ||
    "https://github.com/RKTHEGOAT837/green-vision-releases/releases/latest/download/GreenVision-Managed-1.2.2-x64.exe";
}

function randomToken() {
  const b = new Uint8Array(32);
  crypto.getRandomValues(b);
  let s = "";
  for (const x of b) s += String.fromCharCode(x);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}


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

/* ---------- storage helpers ----------

   One table, key to JSON. The routes were written against a key/value store
   and stay that way; D1 is simply a more durable one with a free tier that
   does not expire. Everything is upserted, because "save this" is the only
   write the routes ever make. */
async function get(key) {
  try {
    const r = await DB.prepare("SELECT v FROM kv WHERE k = ?").bind(key).first();
    return r ? JSON.parse(r.v) : null;
  } catch { return null; }
}
async function set(key, value) {
  await DB.prepare("INSERT INTO kv (k, v) VALUES (?, ?) " +
                   "ON CONFLICT(k) DO UPDATE SET v = excluded.v")
          .bind(key, JSON.stringify(value)).run();
}
async function del(key) {
  try { await DB.prepare("DELETE FROM kv WHERE k = ?").bind(key).run(); }
  catch { /* gone */ }
}

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

const SENDER_NAME = "Green Vision";

/* Where a reply goes.

   These are transactional emails, so the temptation is a no-reply address.
   That is the wrong call here: the people receiving them are colleagues and
   reviewers being invited to try an unsigned installer, and the single most
   likely reply is "Windows blocked this, is it safe?". Bouncing that into a
   void is worse than answering it.

   Left unset, both providers default Reply-To to the sender, which already
   happens to be a real monitored mailbox. It is set explicitly anyway so
   that pointing SENDER_EMAIL at a domain address later - one that is not an
   inbox - does not silently start dropping replies. */
function replyTo() { return ENV.REPLY_TO || SENDER; }

/* What a revoked account is told, in one place so that the three routes that
   can hit it - asking for a link, exchanging a token, and every authenticated
   route - all say exactly the same thing.

   `support` travels with it so the app can offer an appeal without shipping
   an address of its own. The installer is on people's machines and cannot be
   edited; the support address can change, and when it does the app should
   follow rather than mail somewhere nobody reads.

   The reason is deliberately optional and never invented. An administrator
   who gave one has said something the person should see; one who did not has
   not, and filling in a plausible-sounding reason on their behalf would put
   words in their mouth. */
function revokedBody(acct) {
  return {
    error: "revoked",
    revoked: true,
    email: acct.email || null,
    reason: acct.revoked_reason || null,
    at: acct.revoked_at || null,
    support: replyTo()
  };
}

/* Brevo over HTTP, and only that.

   The Netlify build sent through Gmail SMTP, which was the right answer
   there: no third-party mail service, and a free Google account relays 500
   messages a day. It cannot come across. A Worker has no TCP sockets to
   arbitrary ports, so there is no way to speak SMTP from here at all - the
   transport is absent, not merely awkward.

   Brevo is the replacement because it needs no domain: a single sender
   address is verified by clicking a link, which suits an account that is
   already just a Gmail address. Free tier is 300 messages a day, well past
   what sign-in links and invitations use. */
async function sendMail({ to, subject, html: body, text }) {
  if (ENV.BREVO_KEY) {
    const r = await fetch("https://api.brevo.com/v3/smtp/email", {
      method: "POST",
      headers: { "api-key": ENV.BREVO_KEY, "content-type": "application/json" },
      body: JSON.stringify({
        sender: { email: SENDER, name: SENDER_NAME },
        replyTo: { email: replyTo(), name: SENDER_NAME },
        to: [{ email: to }], subject, htmlContent: body, textContent: text
      })
    });
    if (!r.ok) throw new Error("Brevo " + r.status + " " + (await r.text()).slice(0, 200));
    return "brevo";
  }
  if (ENV.RESEND_KEY) {
    const r = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: "Bearer " + ENV.RESEND_KEY,
                 "content-type": "application/json" },
      body: JSON.stringify({ from: SENDER_NAME + " <" + SENDER + ">",
                             reply_to: replyTo(),
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

/* Two URLs, because they are two different things.

   DOWNLOAD is the page a person is sent to - branded, in plain English, with
   the SmartScreen warning explained before they meet it. It is what goes in
   emails and in the app's update prompt.

   DOWNLOAD_FILE is where the bytes actually are: a GitHub release asset.
   GitHub does not meter bandwidth on those, and the installer is 178 MB, so
   every colleague who downloaded it from the web host was spending that
   site's allowance. The page is a few kilobytes; the file is not on it. */



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

function mailFor(kind, { name, link, profile, note, edition, subject, intro, steps, changes }) {
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
  if (kind === "broadcast") {
    /* An announcement, not a transaction. It carries the administrator's own
       words - `intro` - then the steps to follow and what changed, each
       supplied by the caller rather than written into this file, so the
       template can never claim something the sender did not say. */
    const stepHtml = (steps || []).map((t, i) => `
      <tr><td style="padding:0 0 9px;vertical-align:top;width:26px;">
            <div style="width:20px;height:20px;border-radius:50%;background:#0e9f6e;color:#fff;
                        font:700 11px -apple-system,Segoe UI,Roboto,sans-serif;
                        text-align:center;line-height:20px;">${i + 1}</div></td>
          <td style="padding:0 0 9px 9px;font-size:13.5px;line-height:1.6;color:#33473f;">${esc(t)}</td></tr>`).join("");
    const changeHtml = (changes || []).map(c => `
      <li style="margin:0 0 7px;font-size:13.5px;line-height:1.6;color:#33473f;">${esc(c)}</li>`).join("");
    return {
      subject,
      html: SHELL(`<tr><td style="padding:14px 28px 0;">
        <h1 style="margin:0 0 10px;font-size:20px;line-height:1.3;color:#0f2a22;font-weight:800;">
          ${esc(subject)}</h1>
        <p style="margin:0 0 14px;font-size:14px;line-height:1.65;color:#33473f;">${hi}</p>
        <p style="margin:0 0 16px;font-size:14px;line-height:1.65;color:#33473f;white-space:pre-wrap;">${esc(intro)}</p>
        </td></tr>
        ${steps && steps.length ? `<tr><td style="padding:4px 28px 0;">
          <p style="margin:0 0 10px;font-size:13px;font-weight:700;color:#0f2a22;">How to update</p>
          <table role="presentation" cellpadding="0" cellspacing="0" width="100%">${stepHtml}</table>
        </td></tr>` : ""}
        ${BUTTON(DOWNLOAD, "Download the latest version")}
        ${changes && changes.length ? `<tr><td style="padding:18px 28px 0;">
          <p style="margin:0 0 8px;font-size:13px;font-weight:700;color:#0f2a22;">What has changed</p>
          <ul style="margin:0;padding-left:18px;">${changeHtml}</ul>
        </td></tr>` : ""}
        <tr><td style="padding:16px 28px 0;">
          <p style="margin:0;font-size:12px;line-height:1.6;color:#5c7168;">
            Your saved projects and your sign-in are not affected by installing
            a new version. Reply to this email if anything does not work.</p>
        </td></tr>`, subject),
      text: [hi, "", intro, "",
             steps && steps.length ? "How to update:" : "",
             ...(steps || []).map((t, i) => (i + 1) + ". " + t), "",
             "Download: " + DOWNLOAD, "",
             changes && changes.length ? "What has changed:" : "",
             ...(changes || []).map(c => " - " + c), "",
             "Your saved projects and your sign-in are not affected."].join("\n")
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
        ${BUTTON(edition === "managed" ? DOWNLOAD_MANAGED : DOWNLOAD,
                 "Download for Windows &nbsp;&middot;&nbsp; 178 MB")}
        <tr><td style="padding:14px 28px 0;">
          <p style="margin:0 0 8px;font-size:13px;font-weight:700;color:#0f2a22;">Your account</p>
          <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 14px;background:#f7faf8;border-radius:10px;padding:10px 12px;width:100%;">${rows}</table>
          <p style="margin:0 0 14px;font-size:13px;line-height:1.65;color:#33473f;">
            It is already set up — open the app, click <b>Sign in</b>, and enter
            <b>${esc((profile || {}).email || "")}</b>. A link arrives in seconds.</p>
          ${edition === "managed" ? `<p style="margin:0 0 14px;font-size:12.5px;line-height:1.6;color:#5c7168;">
            This copy is licensed to your account and asks you to sign in before use.</p>` : ""}
          <p style="margin:0 0 6px;font-size:13px;font-weight:700;color:#0f2a22;">Windows will warn you — this is expected</p>
          <p style="margin:0 0 14px;font-size:12.5px;line-height:1.6;color:#5c7168;">
            The installer is not code-signed, so SmartScreen shows “Windows protected your PC”.
            Click <b>More info</b>, then <b>Run anyway</b>. Nothing else needs installing — the
            analysis engine and a 2.6-million-feature map index are inside the download.</p>
        </td></tr>`, "Download Green Vision and sign in"),
      text: [hi, "", note || "You have been given access to Green Vision.", "",
             "Download for Windows: " +
               (edition === "managed" ? DOWNLOAD_MANAGED : DOWNLOAD), "",
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
/* Set per request. A Worker gets its configuration and its bindings as
   arguments, not from a process, and the routes below read them as plain
   names - so they are put here once rather than threaded through every
   function that needs one. */
async function handle(req) {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  const url = new URL(req.url);
  // Strip the function prefix so routes read the same as they did on the
  // old host, whether Netlify called us through /api/* or directly.
  const path = url.pathname
    .replace(/^\/\.netlify\/functions\/api/, "")
    .replace(/^\/api/, "")
    .replace(/\/+$/, "") || "/";
  const method = req.method.toUpperCase();

  /* Read once, at the top. This used to be declared halfway down the route
     list, which was fine until a route ABOVE it needed a session - gating
     the Library on sign-in then threw "Cannot access 'bearer' before
     initialization" on every call, a 500 rather than the 401 it was meant
     to be. It is derived from the request and depends on nothing, so the
     top of the handler is where it belongs. */
  const bearer = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "").trim();

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
                         mailer: ENV.BREVO_KEY ? "brevo"
                               : ENV.RESEND_KEY ? "resend" : null });
    }

    /* ---------- ask for a sign-in link ---------- */
    if (method === "POST" && path === "/auth/request") {
      const email = String(body.email || "").trim().toLowerCase();
      const state = String(body.state || "");
      if (!isEmail(email)) return json(400, { error: "invalid email" });
      if (!state || state.length < 16) return json(400, { error: "missing state" });

      /* Refused before the rate limiter and before any mail goes out. A
         revoked account that could still request links would get an email,
         click it, and be turned away at the exchange - which spends our
         sending quota to deliver a dead end. */
      const known = await get("acct:" + email);
      if (known && known.revoked) return json(403, revokedBody(known));

      /* Rate limit per address, or this is a free mail cannon pointed at
         anyone whose address you can type - and it is our sending
         reputation that pays for it. */
      const rlKey = "rl:" + email;
      const now = Date.now();
      const hits = ((await get(rlKey)) || []).filter(t => now - t < 60000);
      if (hits.length >= 3) return json(429, { error: "Too many sign-in emails. Wait a minute." });
      await set(rlKey, [...hits, now]);

      const profile = body.profile && typeof body.profile === "object" ? body.profile : {};
      const token = randomToken();
      await set("tok:" + token,
                { email, state, profile, created: now, used: false,
                  expires: now + TOKEN_TTL_MS });

      const existing = await get("acct:" + email);
      const isNew = !existing;
      const merged = { ...((existing && existing.profile) || {}) };
      for (const [k, v] of Object.entries(profile)) {
        if (v !== "" && v != null) merged[k] = typeof v === "string" ? v.slice(0, 200) : v;
      }
      /* Spread `existing` first. Listing the fields explicitly used to be
         enough, but an account now carries state that is not in that list -
         revoked, revoked_at, revoked_reason - and rebuilding the object from
         named fields silently dropped it. That is a privilege escalation by
         accident: a revoked person asking for a sign-in link would have
         cleared their own revocation on the way past. */
      await set("acct:" + email, {
        ...(existing || {}),
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
          return json(503, { error: "No email provider is configured. Set " +
            "BREVO_KEY on the Worker (Brevo free tier, sender verified as " +
            SENDER + ")." });
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
      /* Revoked between asking for the link and clicking it. The token is
         left unspent: it will expire on its own, and burning it here would
         mean an administrator who revokes by mistake cannot simply restore
         access and tell the person to click the link they already have. */
      if (acct.revoked) return json(403, revokedBody(acct));
      const isFirst = !acct.sign_ins;
      const updated = { ...acct, sign_ins: (acct.sign_ins || 0) + 1, last_seen: now };
      await set("acct:" + rec.email, updated);

      const session = randomToken();
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
    /* ---------- roughly where the caller is ----------

       Electron ships without a Google geolocation API key, so Chromium's
       network location provider is absent and navigator.getCurrentPosition
       always fails with POSITION_UNAVAILABLE - "Failed to query location
       from network service". There is no page-side fix for that; the
       "My location" button simply could not work on the desktop.

       Cloudflare already resolves the connecting IP to a coarse location
       and hands it to the Worker for free. Using that avoids adding a
       third-party lookup service and avoids a billable Google key, and the
       app is talking to this host anyway.

       It is CITY-level and derived from the network route, not from the
       device. A VPN or a corporate connection will place the reader
       somewhere else entirely, so the response says how it was obtained and
       the app labels it approximate rather than presenting it as a fix.

       No session required: it reveals nothing the caller does not already
       know about themselves, and the button has to work before sign-in. */
    if (method === "GET" && path === "/whereami") {
      const cf = req.cf || {};
      const lat = parseFloat(cf.latitude), lon = parseFloat(cf.longitude);
      if (!isFinite(lat) || !isFinite(lon)) {
        return json(503, { error: "no location for this connection" });
      }
      return json(200, {
        ok: true, lat, lon,
        city: cf.city || null,
        region: cf.region || null,
        country: cf.country || null,
        source: "ip",
        accuracy: "city"
      });
    }

    /* ---------- the shared Library ----------

       Shared between every Green Vision account, and readable only with one.
       It was open to anyone who knew the URL, which is wrong for what it
       actually holds: named designs, with the author's name, organisation
       and role attached. That is a directory of who is planning what, and
       publishing to colleagues is not publishing to the web.

       Deliberately NOT filtered to the caller - the whole point is to see
       what other people have published. What is private is History, and
       that lives under u:<email>:history where it cannot be reached by
       anyone else. */
    if (method === "GET" && path === "/library") {
      const s = bearer ? await getFresh("sess:" + bearer) : null;
      if (!s) return json(401, { error: "sign in first" });
      const a = await get("acct:" + s.email);
      if (a && a.revoked) return json(403, revokedBody(a));
      const index = (await get("lib:index")) || [];
      return json(200, { ok: true, designs: index.slice(0, LIMITS.library) });
    }

    /* ---------- admin: a username and password, not a magic link ----------
       Different audience, different mechanism. This is a list of everybody
       who has an account, so it is gated on a shared credential held in the
       environment rather than on an emailed link to one of them. */
    if (method === "POST" && path === "/admin/login") {
      const u = String(body.username || ""), p = String(body.password || "");
      const U = ENV.ADMIN_USER || "greenvision-admin";
      const P = ENV.ADMIN_PASS || "ChangeThisNow!2026";
      /* Constant-time-ish compare. Length is allowed to leak; the bytes are
         not, so a timing oracle cannot walk the password out character by
         character. */
      // Constant-time-ish compare, written out because Workers have no
      // Buffer and no timingSafeEqual. Length is allowed to leak; the bytes
      // are not, so a timing oracle cannot walk the password out.
      const eq = (a, b) => {
        if (a.length !== b.length) return false;
        let d = 0;
        for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
        return d === 0;
      };
      if (!eq(u, U) || !eq(p, P)) {
        await new Promise(r => setTimeout(r, 400));   // blunt the guessing rate
        return json(401, { error: "wrong username or password" });
      }
      const t = randomToken();
      await set("adm:" + t, { user: u, issued: Date.now(),
                              expires: Date.now() + 12 * 60 * 60 * 1000 });
      return json(200, { ok: true, token: t, user: u });
    }

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
      const token = randomToken();
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
      /* Which build is asking.

         The two editions are separate applications to Windows - different
         appId, different install directory - so offering a managed user the
         open installer does not update them, it installs a second, UNGATED
         copy alongside the gated one. That is the opposite of what the
         managed build exists for.

         The app reports its own edition; anything that does not say is
         treated as the open build, which is what every copy shipped before
         1.1.3 will do. Those older managed copies still get the open link
         and have to be updated by hand once - there is no way to tell them
         apart from here, because they never said. */
      const managed = url.searchParams.get("edition") === "managed";
      return json(200, {
        ok: true,
        edition: managed ? "managed" : "open",
        killed: !!n.killed,
        message: n.message || "",
        title: n.title || "",
        latest: n.latest || "",          // newest version available
        /* The floor, below which the app stops and insists.
           `min_managed` applies it to the managed edition alone, so the
           invited build can be held at a current version without blocking
           the open build, which people run offline and for free and which
           has no account to enforce anything against. */
        min: (managed && n.min_managed) ? n.min_managed : (n.min || ""),
        download: managed ? (ENV.DOWNLOAD_MANAGED_PAGE || (DOWNLOAD.replace(/\/?$/, "/") + "managed.html"))
                          : DOWNLOAD,    // the friendly page, per edition
        file: managed ? DOWNLOAD_MANAGED : (n.file || DOWNLOAD_FILE),
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
        // The same floor, for the managed edition only.
        min_managed: String(body.min_managed || "").slice(0, 20),
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

    /* What Brevo actually said. A send can be accepted and still never
       arrive - an unverified sender is accepted with a 2xx and then dropped -
       so "the API returned ok" is not evidence of delivery. This asks Brevo
       directly and hands back its own words. */
    if (method === "GET" && path === "/admin/mailcheck") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });
      if (!ENV.BREVO_KEY) return json(200, { ok: false, reason: "BREVO_KEY not set" });

      const acct = await fetch("https://api.brevo.com/v3/account",
        { headers: { "api-key": ENV.BREVO_KEY } });
      const acctBody = await acct.text();

      const send = await fetch("https://api.brevo.com/v3/smtp/email", {
        method: "POST",
        headers: { "api-key": ENV.BREVO_KEY, "content-type": "application/json" },
        body: JSON.stringify({
          sender: { email: SENDER, name: "Green Vision" },
          to: [{ email: String(url.searchParams.get("to") || SENDER) }],
          subject: "Green Vision delivery check",
          textContent: "If you are reading this, Brevo delivered it."
        })
      });
      const sendBody = await send.text();
      return json(200, {
        ok: true,
        sender_used: SENDER,
        account_status: acct.status,
        account: acctBody.slice(0, 300),
        send_status: send.status,
        send_response: sendBody.slice(0, 400)
      });
    }

    /* ---------- how much sending is left ----------

       Asks Brevo, and sends nothing. The only way to check this before was
       /admin/mailcheck, which spends a real message to prove mail works -
       fine once, wrong as a way to answer "how many do I have left".

       Brevo reports the plan's remaining credits; the free tier also has a
       rolling daily cap that the account endpoint does not expose, so that
       number is described rather than invented. */
    /* ---------- why is mail not arriving? ----------

       Sends NOTHING. Asks Brevo three questions whose answers, together,
       say which link of the chain is broken:

         sender      an unverified sender is refused at the API, so nothing
                     leaves at all;
         blocked     Brevo drops mail to an address it has blocked after a
                     hard bounce or a spam complaint, and reports success;
         events      what actually became of the recent messages.

       ?email=<address> narrows the last two to one person, which is the
       form the question is usually asked in ("MY link never came"). */
    if (method === "GET" && path === "/admin/maildiag") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });
      if (!ENV.BREVO_KEY) return json(503, { error: "no BREVO_KEY configured" });

      const who = String(url.searchParams.get("email") || "").trim().toLowerCase();
      const H = { "api-key": ENV.BREVO_KEY, accept: "application/json" };
      const ask = async (u) => {
        try {
          const r = await fetch(u, { headers: H });
          const t = await r.text();
          let j = null; try { j = JSON.parse(t); } catch (e) {}
          return { status: r.status, body: j, raw: j ? null : t.slice(0, 200) };
        } catch (e) { return { status: 0, error: String(e.message || e).slice(0, 120) }; }
      };

      const senders = await ask("https://api.brevo.com/v3/senders");
      const senderRows = ((senders.body || {}).senders || []).map(x => ({
        email: x.email, active: x.active
      }));
      const mine = senderRows.find(x => (x.email || "").toLowerCase() === SENDER.toLowerCase());

      const blockedUrl = "https://api.brevo.com/v3/smtp/blockedContacts?limit=50" +
                         (who ? "&senders=" + encodeURIComponent(SENDER) : "");
      const blocked = await ask(blockedUrl);
      const blockedRows = ((blocked.body || {}).contacts || []).map(c => ({
        email: c.email, reason: (c.reason && (c.reason.code || c.reason.message)) || null,
        at: c.blockedAt || null
      }));

      /* Is the account itself allowed to send transactional mail?

         A free Brevo account placed under review keeps ACCEPTING API calls
         and quietly stops delivering: the message shows in their dashboard,
         the recipient never gets it, and no bounce is recorded. Nothing in
         the send path can detect that, so it has to be read from the
         account. `relay` is the transactional SMTP state. */
      const acct = await ask("https://api.brevo.com/v3/account");
      const ab = acct.body || {};
      const relay = ab.relay || {};

      /* A wider window, because the question "when did delivery stop?"
         cannot be answered from the last 25 events once a few retries have
         pushed the successful ones off the end. */
      const lim = Math.min(500, Math.max(25, +(url.searchParams.get("limit") || 25)));
      const evUrl = "https://api.brevo.com/v3/smtp/statistics/events?limit=" + lim + "&sort=desc" +
                    (who ? "&email=" + encodeURIComponent(who) : "");
      const events = await ask(evUrl);
      const evRows = ((events.body || {}).events || []).map(e => ({
        at: e.date, event: e.event, to: e.email, reason: e.reason || null
      }));

      /* The one-line answer, so the reader does not have to interpret three
         API payloads to find out whether to look in their spam folder. */
      let verdict;
      if (relay && relay.enabled === false)
        verdict = "Brevo's transactional relay is DISABLED on this account — it accepts messages and does not deliver them. Activate/verify the account in Brevo.";
      else if (!mine) verdict = "The sending address " + SENDER + " is not in this Brevo account's sender list — nothing can be sent.";
      else if (mine.active === false) verdict = "The sending address " + SENDER + " is NOT VERIFIED in Brevo — every send is refused.";
      else if (who && blockedRows.some(b => (b.email || "").toLowerCase() === who))
        verdict = who + " is on Brevo's blocked list, so mail to it is dropped and reported as sent.";
      else if (evRows.length && evRows.every(e => /bounce|blocked|spam|error|invalid/i.test(e.event)))
        verdict = "Recent messages all failed at the recipient's mail server — see events.";
      else if (evRows.some(e => /delivered/i.test(e.event)))
        verdict = "Brevo delivered recent messages. If they are not in the inbox, check Spam/Promotions.";
      else if (!evRows.length)
        verdict = who ? "Brevo has no recent events for that address — the app may not be reaching the Worker at all."
                      : "Brevo has no recent events at all.";
      else verdict = "See events.";

      return json(200, {
        ok: true,
        sender: SENDER,
        sender_verified: mine ? mine.active !== false : false,
        senders_configured: senderRows,
        asked_about: who || null,
        account_email: ab.email || null,
        account_company: ab.companyName || null,
        relay_enabled: relay.enabled === undefined ? null : relay.enabled,
        plan: (ab.plan || []).map(p => ({ type: p.type, credits: p.credits, creditsType: p.creditsType })),
        blocked_count: blockedRows.length,
        blocked: blockedRows.slice(0, 15),
        recent_events: evRows,
        verdict
      });
    }

    if (method === "GET" && path === "/admin/mailquota") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });
      if (!ENV.BREVO_KEY) return json(503, { error: "no BREVO_KEY configured" });
      const r = await fetch("https://api.brevo.com/v3/account", {
        headers: { "api-key": ENV.BREVO_KEY, accept: "application/json" }
      });
      const body = await r.text();
      if (!r.ok) return json(502, { error: "Brevo " + r.status, detail: body.slice(0, 300) });
      let a = {};
      try { a = JSON.parse(body); } catch (e) {}
      const plans = (a.plan || []).map(p => ({
        type: p.type, credits: p.credits, creditsType: p.creditsType
      }));
      /* How many of today's 300 are gone.
         The account endpoint reports the plan's limit but not the usage
         against it, so "how many are left" cannot be answered from it -
         which is the only question anyone actually asks. The aggregated
         report gives today's sends, and the subtraction is done here rather
         than left to the reader. */
      const today = new Date().toISOString().slice(0, 10);
      let sentToday = null, totalSent = null;
      try {
        const u = "https://api.brevo.com/v3/smtp/statistics/aggregatedReport";
        const rt = await fetch(u + "?startDate=" + today + "&endDate=" + today,
          { headers: { "api-key": ENV.BREVO_KEY, accept: "application/json" } });
        if (rt.ok) { const j = await rt.json(); sentToday = j.requests ?? null; }
        // Everything since the account was opened, for context.
        const ra = await fetch(u + "?startDate=2026-09-01&endDate=" + today,
          { headers: { "api-key": ENV.BREVO_KEY, accept: "application/json" } });
        if (ra.ok) { const j = await ra.json(); totalSent = j.requests ?? null; }
      } catch (e) { /* the plan figures above still stand */ }

      const dailyCap = 300;
      return json(200, {
        ok: true,
        email: a.email || null,
        company: (a.companyName || null),
        plan: plans,
        sent_today: sentToday,
        left_today: sentToday == null ? null : Math.max(0, dailyCap - sentToday),
        sent_since_sept: totalSent,
        daily_cap: dailyCap,
        note: "The free plan is a DAILY cap of 300 that resets every day - not " +
              "a finite pool that runs out. There is no expiry to count down to."
      });
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
          revoked: !!a.revoked, revoked_at: a.revoked_at || null,
          revoked_reason: a.revoked_reason || null,
          counts: { projects: (proj || []).length, chats: (chats || []).length,
                    history: (hist || []).length }
        });
      }
      rows.sort((x, y) => (y.last_seen || y.created || 0) - (x.last_seen || x.created || 0));
      return json(200, { ok: true, users: rows, total: rows.length });
    }

    /* ---------- admin: turn one person's access off, or back on ----------

       Access, not data. Revoking sets a flag and touches nothing the person
       has made: their projects, chats and history are all still there and
       come back intact when it is lifted. Deleting an account is a different
       operation with a different blast radius, and conflating the two would
       mean every accidental revocation destroyed somebody's work.

       Reversible on purpose. The likeliest use of this route is a mistake -
       the wrong row clicked in a list of similar addresses - and the fix has
       to be one click, not a support conversation. */
    /* ---------- admin: tell everyone something ----------

       One email, to every account on file. The in-app notice reaches people
       who OPEN the app; this reaches the ones who have not, which for an
       update they need to install is precisely the audience that matters.

       Revoked accounts are skipped. `dry_run` renders the message and
       returns the recipient list without sending, so the wording can be
       checked against the real list before anything leaves - a bulk send is
       the one mistake that cannot be withdrawn. */
    if (method === "POST" && path === "/admin/broadcast") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });

      const subject = String(body.subject || "").trim().slice(0, 200);
      const intro = String(body.intro || "").trim().slice(0, 2000);
      const steps = Array.isArray(body.steps) ? body.steps.slice(0, 12) : [];
      const changes = Array.isArray(body.changes) ? body.changes.slice(0, 40) : [];
      const dry = body.dry_run === true;
      if (!subject) return json(400, { error: "no subject" });
      if (!intro) return json(400, { error: "no intro" });

      const idx = (await get("idx:accounts")) || [];
      const targets = [];
      for (const email of idx) {
        const a = await get("acct:" + email);
        if (!a) continue;
        if (a.revoked) continue;                 // never chase a revoked account
        targets.push({ email, name: (a.profile || {}).name || null });
      }
      if (!targets.length) return json(200, { ok: true, sent: 0, note: "no accounts" });

      const results = [];
      for (const t of targets) {
        const m = mailFor("broadcast", {
          name: t.name ? String(t.name).split(" ")[0] : null,
          subject, intro, steps, changes
        });
        if (dry) { results.push({ email: t.email, status: "dry-run" }); continue; }
        try {
          await sendMail({ to: t.email, subject: m.subject, html: m.html, text: m.text });
          results.push({ email: t.email, status: "sent" });
        } catch (e) {
          /* Recorded, not thrown. One bad address must not stop the rest,
             and a caller that gets a 502 cannot tell who did receive it. */
          results.push({ email: t.email, status: "failed", error: String(e.message || e).slice(0, 160) });
        }
      }
      return json(200, {
        ok: true,
        dry_run: dry,
        accounts: idx.length,
        skipped_revoked: idx.length - targets.length,
        sent: results.filter(r => r.status === "sent").length,
        failed: results.filter(r => r.status === "failed").length,
        results
      });
    }

    if (method === "POST" && path === "/admin/revoke") {
      const adm = bearer ? await getFresh("adm:" + bearer) : null;
      if (!adm) return json(401, { error: "sign in first" });
      const who = String(body.email || "").trim().toLowerCase();
      if (!isEmail(who)) return json(400, { error: "invalid email" });
      const a = await get("acct:" + who);
      if (!a) return json(404, { error: "no such account" });

      const off = body.revoked !== false;         // default is to revoke
      const now = Date.now();
      const next = { ...a };
      if (off) {
        next.revoked = true;
        next.revoked_at = now;
        const why = String(body.reason || "").trim().slice(0, 400);
        if (why) next.revoked_reason = why; else delete next.revoked_reason;
      } else {
        delete next.revoked;
        delete next.revoked_at;
        delete next.revoked_reason;
        next.restored_at = now;
      }
      await set("acct:" + who, next);
      return json(200, { ok: true, email: who, revoked: !!next.revoked,
                         reason: next.revoked_reason || null });
    }

    /* ---------- "I think this is a mistake" ----------

       Reachable only with the session token of an account that is actually
       revoked, which is the whole access-control story: it is not an open
       form, so it cannot be used to send mail to the support address from
       outside, and there is nothing to guess because the token was already
       issued to this person before the revocation.

       401 is deliberately possible here. Somebody revoked long enough ago
       that their 15-day session has lapsed cannot appeal through the app -
       they have the address in front of them on the same screen and can
       write to it directly, which is the fallback the button's own subtitle
       points at. */
    if (method === "POST" && path === "/appeal") {
      const s = bearer ? await getFresh("sess:" + bearer) : null;
      if (!s) return json(401, { error: "sign in first" });
      const a = await get("acct:" + s.email);
      if (!a || !a.revoked) return json(400, { error: "access is not revoked" });

      // One an hour. An appeal is a considered act; a button that mails on
      // every press is a way to flood the inbox that has to read them.
      const seen = await get("appeal:" + s.email);
      if (seen && Date.now() - seen < 3600000) {
        return json(429, { error: "already sent",
          message: "Your message has been sent. Please wait for a reply." });
      }
      await set("appeal:" + s.email, Date.now());

      const note = String(body.note || "").trim().slice(0, 1500);
      const p = a.profile || {};
      const who = [p.name, p.org, p.role, p.city].filter(Boolean).join(" · ");
      try {
        await sendMail({
          to: replyTo(),
          subject: "Access appeal — " + s.email,
          text: [
            s.email + " says their access was removed by mistake.", "",
            who ? "Profile: " + who : "Profile: (none on file)",
            "Revoked: " + (a.revoked_at ? new Date(a.revoked_at).toUTCString() : "unknown"),
            "Reason given: " + (a.revoked_reason || "(none)"), "",
            "Their message:", note || "(they did not add one)", "",
            "Reply to this email to answer them, or restore access on the admin page."
          ].join("\n"),
          html: SHELL(
            `<tr><td style="padding:0 28px 18px;">
              <p style="margin:0 0 12px;font-size:15px;color:#0f2a22;">
                <b>${esc(s.email)}</b> says their access was removed by mistake.</p>
              <p style="margin:0 0 6px;font-size:13px;color:#5c7168;">
                ${esc(who || "No profile on file")}</p>
              <p style="margin:0 0 14px;font-size:13px;color:#5c7168;">
                Revoked ${esc(a.revoked_at ? new Date(a.revoked_at).toUTCString() : "at an unknown time")} ·
                reason: ${esc(a.revoked_reason || "none given")}</p>
              <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#0f2a22;">Their message</p>
              <p style="margin:0;font-size:14px;line-height:1.65;color:#33473f;white-space:pre-wrap;">${
                esc(note || "(they did not add one)")}</p></td></tr>`,
            "An access appeal from " + s.email)
        });
      } catch (e) {
        return json(502, { error: "Could not send: " + e.message });
      }
      return json(200, { ok: true, sent_to: replyTo() });
    }

    /* ---------- invite a colleague (key-gated) ---------- */
    if (method === "POST" && path === "/invite") {
      const KEY = ENV.INVITE_KEY || "SKKJPWnWu5reNl9UBfrVUUl_n4wQ9htJ";
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
      /* Inviting somebody whose access was revoked does not restore it, and
         must not look as though it did. Un-revoking is a deliberate act on
         the admin page, not a side effect of re-sending an invitation. */
      if (had && had.revoked) return json(409, {
        error: "revoked",
        message: "Access for " + to + " has been revoked. Restore it on the " +
                 "admin page before inviting them again."
      });
      const merged = { ...((had && had.profile) || {}), ...prof };
      // ...had first, for the same reason as /auth/request above.
      await set("acct:" + to, {
        ...(had || {}),
        email: to, profile: merged,
        created: (had && had.created) || now, updated: now,
        sign_ins: (had && had.sign_ins) || 0
      });
      if (!had) {
        const idx = (await get("idx:accounts")) || [];
        if (!idx.includes(to)) await set("idx:accounts", [...idx, to]);
      }

      /* Managed unless the sender explicitly asks for the open build.

         The default matters. An invitation is the one path where somebody
         who is not the author ends up with a copy, and the whole point of
         being able to revoke their access is that it works - which it only
         does in the managed edition. Defaulting to the open build would
         mean the common case silently produced the one where revocation
         leaves a fully working planner behind. */
      const edition = String(body.edition || "managed").toLowerCase() === "open"
        ? "open" : "managed";
      const m = mailFor("invite", {
        name: merged.name ? String(merged.name).split(" ")[0] : null,
        profile: { ...merged, email: to },
        note: String(body.note || "").trim().slice(0, 600),
        edition
      });
      try { await sendMail({ to, subject: m.subject, html: m.html, text: m.text }); }
      catch (e) {
        if (e.code === "NO_MAILER") return json(503, {
          error: "No email provider configured. Set BREVO_KEY on the Worker." });
        return json(502, { error: "Could not send: " + e.message });
      }
      return json(200, { ok: true, sent: to, isNew: !had, edition });
    }

    /* ---------- everything below needs a user session ---------- */
    const sess = bearer ? await getFresh("sess:" + bearer) : null;
    if (!sess) return json(401, { error: "sign in first" });
    const email = sess.email;
    const key = suffix => "u:" + email + ":" + suffix;
    const acct = (await get("acct:" + email)) || { email, profile: {} };

    /* Access revoked.
       Checked here rather than by deleting sessions, because a session is
       keyed by its own random token: revoking would mean scanning every row
       to find the ones belonging to this address, and any session issued in
       the seconds after that scan would survive it. Reading the account on
       the way past is O(1), already happening, and cannot be raced.

       403 and not 401. They are two different facts and the app must not
       confuse them: 401 means "your session ended, sign in again", and the
       app responds by signing out and offering the sign-in screen - which,
       for a revoked account, would send them round a loop that quietly fails
       at the mail step and never explains why. 403 means "we know who you
       are and the answer is no". */
    if (acct.revoked) return json(403, revokedBody(acct));

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
                      description: String(d.description || "").slice(0, 600),
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
        // The author's own line about the design, kept on the row so the
        // Library can show it without unpacking the whole design.
        description: String(d.description || "").slice(0, 600),
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
        /* Two kinds of row share this list, and only one of them used to
           survive.

           A SEARCH is a place someone looked at; repeats are noise, so the
           newest wins and the older duplicate is dropped by place name.

           A DESIGN - saved or published - carries `kind` and `design_id`,
           and every field except `place` was being discarded here, because
           the row was rebuilt from three named properties. Worse, the
           de-duplication was by place, so saving two designs in the same
           city silently deleted the first. Design rows now de-duplicate by
           their design id, which is what identifies them. */
        const isDesign = !!body.kind && !!body.design_id;
        const row = isDesign
          ? { kind: String(body.kind).slice(0, 20),
              design_id: String(body.design_id).slice(0, 80),
              place: String(body.place || "Untitled design").slice(0, 200),
              trees: Number(body.trees) || 0,
              area_m2: Number(body.area_m2) || 0,
              cost: body.cost == null ? null : Number(body.cost),
              at: Date.now() }
          : { place: String(body.place || "").slice(0, 200),
              lat: body.lat, lon: body.lon, at: Date.now() };
        if (!row.place) return json(400, { error: "no place" });
        const dropped = isDesign
          ? list.filter(x => x.design_id !== row.design_id)
          : list.filter(x => x.kind || x.place !== row.place);
        const next = [row, ...dropped].slice(0, LIMITS.history);
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
}

export default {
  async fetch(req, env) {
    readEnv(env);
    try {
      return await handle(req);
    } catch (e) {
      /* Never return Cloudflare's own error page. The client reads JSON, and
         an HTML body at any status is the failure mode that made Pipedream's
         outage invisible - a 200 with a page of markup that nothing parsed. */
      return new Response(JSON.stringify({ error: String((e && e.message) || e) }),
        { status: 500, headers: { ...CORS, "content-type": "application/json" } });
    }
  }
};
