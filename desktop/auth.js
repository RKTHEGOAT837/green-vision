/* =====================================================================
   Sign-in — an emailed link that comes back to this window
   =====================================================================
   The flow, and why each step is shaped the way it is:

     1. The app POSTs the email address and the signup profile to the
        Pipedream workflow, along with a `state` it just generated.
     2. Pipedream stores a one-time token against that email, and sends a
        Green Vision branded email containing a single button.
     3. The reader clicks it. Their browser opens the Pipedream verify
        endpoint, which 302s to greenvision://auth?token=…&state=…
     4. Windows hands that URL to this app — the running one, because of
        the single-instance lock in main.js.
     5. The app checks the `state` it gets back is the one it sent, then
        exchanges the token for a session and stores it.

   `state` is the part that is easy to leave out and should not be. The
   protocol handler is a public door: ANY page the reader visits can send
   greenvision://auth?token=… to this app. Without state the app would
   accept a token an attacker obtained for their OWN account, silently
   signing the planner into it — and every design they then published
   would go to the attacker's library. The state is generated here, never
   leaves the machine except in the request that starts the flow, and a
   callback that does not carry it back is dropped.

   The token is single-use and short-lived on the Pipedream side; this
   side additionally refuses one older than the window it was issued for,
   so a link recovered from an inbox months later is inert even if the
   data store forgot to expire it.
   ===================================================================== */

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { shell } = require("electron");

/* Where the sign-in service lives.

   Read from the environment first, so a developer can point a dev build
   at a scratch deployment without editing anything; then from
   config.json, which is what a SHIPPED app uses — a packaged Windows
   binary has no shell environment, and requiring one is how "the sign-in
   button does nothing" happens on someone else's machine.

   Empty means no sign-in service, and the app says so rather than
   pretending: the sheet states it and the request is refused. */
function readConfig() {
  for (const p of [path.join(__dirname, "config.json"),
                   path.join(process.resourcesPath || "", "app", "config.json")]) {
    try {
      const j = JSON.parse(fs.readFileSync(p, "utf8"));
      if (j && typeof j === "object") return j;
    } catch (e) { /* absent or unreadable: try the next */ }
  }
  return {};
}

const CONFIG = readConfig();

function readConfiguredBase() {
  if (process.env.GV_AUTH_URL) return process.env.GV_AUTH_URL;
  return CONFIG.authUrl ? String(CONFIG.authUrl) : "";
}

/* Which edition this build is.

   Two are produced from one codebase. The OPEN edition is the product as
   designed: the map, the analysis and the costing all work with no account,
   and signing in adds saved projects, assistant memory and the shared
   Library. The MANAGED edition requires an account for anything at all.

   The managed edition exists because revoking somebody's access is only
   meaningful if an account is what their access consists of. Withdraw it in
   the open edition and they keep a fully working offline planner - the
   engine and the map index are inside the installer, on their machine, and
   no server is consulted to draw a map. Nothing dishonest about that; it is
   simply not what "blocking access" is supposed to mean, so a build where
   the claim is true had to be a different build.

   It is a build-time constant read from a file inside the package, not a
   preference and not a server response. A renderer flag could be flipped in
   devtools; a server-driven one would unlock the app for anybody who could
   make the network fail. This is baked in at pack time and the packaged
   file is what ships.

   Honest about the threat model: the package is unsigned and its contents
   are readable, so somebody determined can edit config.json in an installed
   copy and turn the gate off. This is access control for colleagues and
   reviewers - it makes revocation real for people acting in good faith. It
   is not DRM and is not presented as any. */
const REQUIRE_SIGN_IN =
  process.env.GV_REQUIRE_SIGN_IN === "1" || CONFIG.requireSignIn === true;

const AUTH_BASE = readConfiguredBase();

const STATE_TTL_MS = 15 * 60 * 1000;     // matches the email's stated 15 minutes

let accounts = null;
let onSignedIn = null;
let pending = null;                      // { state, email, issued }

function init(opts) {
  accounts = opts.accounts;
  onSignedIn = opts.onSignedIn || (() => {});
}

function configured() { return !!AUTH_BASE; }

async function post(pathname, body) {
  const r = await fetch(AUTH_BASE.replace(/\/+$/, "") + pathname, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    /* A revoked account is not a service fault, and must not be reported as
       one. Left to the generic branch this surfaced as "Sign-in service
       returned 403: {"error":"revoked"...}" - a raw status and a fragment of
       JSON, which reads as something broken and invites the reader to keep
       retrying a thing that will never succeed. */
    let info = null;
    try { info = JSON.parse(text); } catch (e) { /* not JSON; fall through */ }
    if (r.status === 403 && info && info.revoked) {
      const e = new Error(
        "Access for this account has been withdrawn by an administrator." +
        (info.reason ? " Reason: " + info.reason : "") +
        " If you think this is a mistake, email " +
        (info.support || "greenvision.support@gmail.com") + ".");
      e.revoked = info;
      throw e;
    }
    throw new Error("Sign-in service returned " + r.status + (text ? ": " + text.slice(0, 200) : ""));
  }
  return r.json();
}

/* ---------- the four things the renderer can ask for ---------- */

async function handle(msg) {
  switch (msg && msg.type) {

    case "status":
      /* `base` is handed over so the renderer can reach the data API on
         the same deployment. It is only ever sent after a status call
         succeeds, so a build with no sign-in service never leaks a URL
         or lets the page try one. */
      return { ok: true, configured: configured(), base: AUTH_BASE || null,
               requireSignIn: REQUIRE_SIGN_IN, edition: REQUIRE_SIGN_IN ? "managed" : "open",
               user: accounts.get(), benefits: accounts.BENEFITS };

    /* Start the flow. `profile` is what the signup form collected; it is
       sent now rather than after verification so the account exists with
       its details the moment the link is clicked, and the reader is not
       asked the same questions twice. */
    case "request": {
      if (!configured()) {
        return { ok: false, error: "No sign-in service is configured in this build." };
      }
      const email = String((msg.email || "")).trim().toLowerCase();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) {
        return { ok: false, error: "That email address doesn't look right." };
      }
      const state = crypto.randomBytes(24).toString("base64url");
      pending = { state, email, issued: Date.now() };
      try {
        await post("/auth/request", {
          email,
          state,
          profile: msg.profile || {},
          client: "windows-desktop"
        });
        return { ok: true, email };
      } catch (e) {
        pending = null;
        return { ok: false, error: e.message };
      }
    }

    /* The callback, handed over by main.js. */
    case "callback": {
      let u;
      try { u = new URL(msg.url); } catch (e) { return { ok: false, error: "Malformed link." }; }
      const token = u.searchParams.get("token");
      const state = u.searchParams.get("state");

      if (!pending) {
        return { ok: false, error: "This app did not start that sign-in. Ask for a new link from the Sign in button." };
      }
      if (Date.now() - pending.issued > STATE_TTL_MS) {
        pending = null;
        return { ok: false, error: "That link has expired. Ask for a new one." };
      }
      /* Constant-time compare: the state is a secret for as long as the
         flow is open, and a timing oracle on it is free to exploit. */
      const a = Buffer.from(String(state || ""));
      const b = Buffer.from(pending.state);
      if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
        return { ok: false, error: "That sign-in link was not the one this app asked for, so it was ignored." };
      }
      if (!token) return { ok: false, error: "That link carried no token." };

      try {
        const j = await post("/auth/exchange", { token, state: pending.state });
        if (!j || !j.session || !j.email) {
          return { ok: false, error: "The sign-in service did not return a session." };
        }
        pending = null;
        const user = accounts.save({
          email: j.email,
          session: j.session,
          profile: j.profile || {},
          since: Date.now()
        });
        onSignedIn(user);
        return { ok: true, user };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    }

    case "sign-out":
      pending = null;
      return { ok: true, user: accounts.clear() };

    /* Opening the inbox is a courtesy, not part of the flow — some
       readers use a desktop client and will never see a web inbox. */
    case "open-mail":
      shell.openExternal("https://mail.google.com/");
      return { ok: true };

    default:
      return { ok: false, error: "Unknown auth request." };
  }
}

module.exports = { init, handle, configured };
