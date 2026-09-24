/* =====================================================================
   The account store — one signed-in planner, held on this machine
   =====================================================================
   Green Vision keeps no user database of its own. Pipedream verifies who
   owns an email address and hands back a session; this file is the local
   half: it remembers that session between launches so a planner is not
   asked to check their inbox every morning.

   The session token is written through Electron's safeStorage, which on
   Windows is DPAPI — encrypted to the Windows user account, so another
   account on the same PC cannot read it and a copied file is useless
   elsewhere. The PROFILE (name, organisation, role, city) is stored in
   clear alongside it: it is what the planner typed about themselves and
   it is shown back to them in the account sheet, so encrypting it would
   buy nothing and make the file impossible to inspect or hand-edit.

   If safeStorage is unavailable — a stripped Windows build, a service
   account with no DPAPI — the session is NOT written in the clear. It is
   held in memory for the run and the planner signs in again next time.
   A token at rest in plain text on a shared municipal machine is worse
   than an extra sign-in.
   ===================================================================== */

"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { safeStorage } = require("electron");

let FILE = null;
let cache = null;              // { profile, session?, email, since }
let memorySession = null;      // fallback when safeStorage cannot encrypt

let KEYFILE = null;

function init(userDataDir) {
  FILE = path.join(userDataDir, "account.json");
  KEYFILE = path.join(userDataDir, "session.key");
  cache = read();
}

/* ---------- the durable seal ----------------------------------------

   safeStorage is the right first choice and it is used first. What it is
   NOT is stable across an application update: replacing the executable
   left every previously encrypted session undecryptable, so the app
   signed everybody out every time a new build was installed. That was
   reproduced here repeatedly - sign in, restart, still signed in; install
   a new build, restart, signed out with `session_enc` stripped from the
   file by the failure path.

   A session that does not survive an update is not remembered at all, so
   there is a second copy sealed with a key this code owns: 32 random bytes
   in the user's own profile directory, AES-256-GCM. On read, safeStorage
   is tried first and this is the fallback.

   Be clear about what that is worth. The key sits beside the token in
   %APPDATA%, which is per-user and not readable by other standard Windows
   accounts - so it protects against casual inspection and against the token
   being lifted out of a backup or a synced folder in the clear. It does not
   protect against somebody who already has your Windows account, and it is
   weaker than DPAPI, which binds to the account itself. That is the trade
   being made: a fortnight of staying signed in, against a token that a
   determined local attacker could unseal. It is a revocable 15-day session,
   not a password, and the alternative on offer was signing in every time. */
function localKey() {
  try {
    if (fs.existsSync(KEYFILE)) {
      const k = fs.readFileSync(KEYFILE);
      if (k.length === 32) return k;
    }
  } catch (e) { /* fall through and mint a new one */ }
  try {
    const k = crypto.randomBytes(32);
    fs.writeFileSync(KEYFILE, k, { mode: 0o600 });
    return k;
  } catch (e) { return null; }
}

function seal(text) {
  const key = localKey();
  if (!key) return null;
  try {
    const iv = crypto.randomBytes(12);
    const c = crypto.createCipheriv("aes-256-gcm", key, iv);
    const ct = Buffer.concat([c.update(String(text), "utf8"), c.final()]);
    return Buffer.concat([iv, c.getAuthTag(), ct]).toString("base64");
  } catch (e) { return null; }
}

function unseal(b64) {
  const key = localKey();
  if (!key || !b64) return null;
  try {
    const raw = Buffer.from(b64, "base64");
    const iv = raw.subarray(0, 12), tag = raw.subarray(12, 28), ct = raw.subarray(28);
    const d = crypto.createDecipheriv("aes-256-gcm", key, iv);
    d.setAuthTag(tag);
    return Buffer.concat([d.update(ct), d.final()]).toString("utf8");
  } catch (e) { return null; }
}

/* How long a sign-in lasts on this machine.

   There was no local expiry at all: the session sat on disk until the server
   rejected it, which is ninety days. Fifteen is a working fortnight - long
   enough that nobody signs in twice in the same piece of work, short enough
   that a laptop left in a drawer does not stay signed in indefinitely. */
const SESSION_DAYS = 15;
const SESSION_MS = SESSION_DAYS * 24 * 60 * 60 * 1000;

function read() {
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(FILE, "utf8"));
  } catch (e) {
    return null;                       // no file, or it is not JSON
  }

  if (raw.session_enc && safeStorage.isEncryptionAvailable()) {
    try {
      raw.session = safeStorage.decryptString(Buffer.from(raw.session_enc, "base64"));
    } catch (e) {
      /* Almost always an application update: the executable changed and the
         old ciphertext can no longer be read. The durable copy below is
         exactly for this, so try it before giving up on the sign-in. */
      const alt = unseal(raw.session_alt);
      if (alt) {
        raw.session = alt;
        log("session recovered from the durable copy after safeStorage failed");
      } else {
      /* The ciphertext cannot be read on this machine any more - a different
         Windows user wrote it, the profile was reset, or it predates a change
         in how Chromium derives the key.

         This used to just `delete raw.session` and carry on, which left the
         file holding a blob that would fail again on every single launch,
         and an account record with an email but no session. The app then
         looked signed out while claiming to remember who you were, and no
         amount of restarting fixed it. Drop it from disk too, so the next
         sign-in starts clean. */
        delete raw.session;
        delete raw.session_enc;
        delete raw.session_alt;
        try {
          fs.writeFileSync(FILE, JSON.stringify(raw, null, 2), "utf8");
        } catch (e2) { /* read-only profile: the in-memory drop still stands */ }
        log("stored session could not be read by either method; signing in again");
      }
    }
  }
  // No safeStorage at all - a stripped build, or a service account with no
  // DPAPI. The durable copy is the whole answer there, not a fallback.
  if (!raw.session && raw.session_alt) {
    const alt = unseal(raw.session_alt);
    if (alt) raw.session = alt;
  }
  delete raw.session_enc;
  delete raw.session_alt;

  /* Expired? Keep the email and the profile - they make signing in again one
     click and keep the person's details on their designs - but the session
     itself is gone. */
  if (raw.session && raw.session_expires && Date.now() > raw.session_expires) {
    delete raw.session;
    delete raw.session_expires;
    try {
      const out = { email: raw.email, profile: raw.profile || {}, since: raw.since };
      fs.writeFileSync(FILE, JSON.stringify(out, null, 2), "utf8");
    } catch (e) { /* as above */ }
    log("sign-in expired after " + SESSION_DAYS + " days");
  }
  return raw;
}

function log(m) { try { console.log("[accounts] " + m); } catch (e) {} }

function write(rec) {
  cache = rec;
  if (!rec) {
    memorySession = null;
    try { fs.unlinkSync(FILE); } catch (e) {}
    return;
  }
  const out = { email: rec.email, profile: rec.profile || {}, since: rec.since || Date.now() };
  if (rec.session) {
    // Set on first write and carried forward afterwards, so saving a profile
    // does not quietly extend the sign-in by another fortnight.
    out.session_expires = rec.session_expires || (Date.now() + SESSION_MS);
    if (safeStorage.isEncryptionAvailable()) {
      out.session_enc = safeStorage.encryptString(rec.session).toString("base64");
    }
    // Always, alongside it. This is the copy that survives an update, and
    // writing it only when safeStorage is missing would mean it was never
    // there on the machines that actually need it.
    const alt = seal(rec.session);
    if (alt) {
      out.session_alt = alt;
    } else if (!out.session_enc) {
      // Neither method available: hold it for this run only, never on disk
      // in the clear.
      memorySession = rec.session;
      out.session_unavailable = "no usable encryption — session not persisted";
    }
  }
  fs.writeFileSync(FILE, JSON.stringify(out, null, 2), "utf8");
}

function get() {
  if (!cache) return null;
  // The file is read once at startup, so a long-running app has to check the
  // clock here too - otherwise a session that expires while the app is open
  // stays usable until the next launch.
  if (cache.session_expires && Date.now() > cache.session_expires) {
    delete cache.session;
    memorySession = null;
  }
  const s = cache.session || memorySession;
  return s ? { ...cache, session: s } : { ...cache };
}

/* How long is left, in days, or null when there is no session. Used by the
   account sheet so the reader can see it rather than being surprised. */
function daysLeft() {
  if (!cache || !cache.session_expires) return null;
  return Math.max(0, Math.ceil((cache.session_expires - Date.now()) / 86400000));
}

function save(rec) { write(rec); return get(); }
function clear() { write(null); return null; }

/* What a signed-in planner gets that a device-only one does not.

   `needs` is the honest half. Three of these want a gallery server —
   COMMUNITY_URL in the studio config — and there is not one yet: without
   it `publish` falls back to "saved locally instead", and there is
   nowhere for designs or history to follow an account TO. Listing them
   as though they worked would be the exact drift this list was put in
   one place to prevent, so each one says what it is waiting on and the
   sign-in sheet renders it differently.

   The two with no `needs` work today, on this machine, with nothing
   further configured. */
const BENEFITS = [
  { id: "authorship", name: "Your name on the design",
    detail: "Published designs and exported bills of quantities carry your name and organisation instead of “Anonymous”." },
  { id: "export", name: "Named exports",
    detail: "The BOQ CSV is stamped with the planner and the organisation that produced it — what a tender file needs." },
  { id: "publish", name: "Publish to the shared Library", needs: "gallery",
    detail: "Put a finished design in front of colleagues. Until a gallery server is configured, publishing saves to this PC instead." },
  { id: "sync", name: "Designs follow the account", needs: "gallery",
    detail: "Sign in on another machine and your saved designs come with you." },
  { id: "history", name: "History kept beyond this device", needs: "gallery",
    detail: "Autosaves survive a reinstall or a new PC." }
];

module.exports = { init, get, save, clear, daysLeft, SESSION_DAYS, BENEFITS };
