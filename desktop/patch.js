/* =====================================================================
   The patch channel — small fixes without a 1.33 GB reinstall
   =====================================================================
   THE PROBLEM THIS EXISTS FOR.

   The installer is 1.33 GB and almost none of it is the app. It is a
   CPython build, a 1.5-billion-parameter language model, and a map index
   holding 3.2 million building footprints. None of that changes when a
   label is wrong, a button does nothing, or a cost line double-counts.
   What changes is the studio: one HTML file with roughly 870 KB of
   JavaScript inside it.

   Shipping a whole installer for a one-line fix is not just wasteful, it
   is self-defeating. Ask people to download 1.33 GB for a typo and by the
   third time they will not, and then the copy running in a municipal
   office is the one with the bug in it. A compulsory-update floor makes
   that worse, not better: it turns "I'll do it later" into "I can't work
   today".

   So there are now two update paths, and they carry different things:

     THE INSTALLER   Electron itself, the Python engine, the model, the
                     map index, this file. Rare, large, deliberate.

     A PATCH         The studio page. About a megabyte. As often as
                     needed, applied silently at the next start.

   HOW IT WORKS.

   At startup the app asks the Worker whether a studio newer than the one
   it shipped with exists. If so it downloads it, hashes it, and writes it
   into userData. Next launch — and only the next launch — renders from
   there. The window that is already open is never swapped underneath
   somebody mid-design.

   THE HASH IS THE WHOLE SECURITY MODEL.

   A patch is a page this app executes with a preload bridge attached, so
   an unverified patch is arbitrary code execution with extra steps. The
   manifest carries a SHA-256 recorded by an administrator; the payload is
   served by GitHub Pages, which is not the same host and not under the
   same key. A byte that does not match is discarded and the app keeps
   running the copy it already trusted. There is no "try anyway".

   WHAT A PATCH CANNOT DO.

   It cannot touch Python, the model, the map index, or Electron's main
   process — the things large enough or privileged enough to be worth
   being careful about. If a fix needs those, it needs the installer, and
   `min_app` on the manifest is how a patch declares which shells it is
   safe to run on.

   IT MUST ALWAYS BE POSSIBLE TO GET BACK. A patch that breaks the studio
   would otherwise be unfixable from inside a broken studio, so: the
   previous good patch is kept, a patch that fails to load twice in a row
   is rolled back automatically, and `--gv-no-patch` ignores the whole
   mechanism.
   ===================================================================== */

"use strict";

const { app } = require("electron");
const https = require("https");
const crypto = require("crypto");
const path = require("path");
const fs = require("fs");

const CONFIG = (() => {
  try { return require("./config.json"); } catch { return {}; }
})();

const BASE = String(CONFIG.authUrl || "").replace(/\/+$/, "");

/* Never larger than this. The studio is ~900 KB; anything an order of
   magnitude past that is not a studio, and downloading it to find out
   is the wrong way round. */
const MAX_BYTES = 8 * 1024 * 1024;
const NET_TIMEOUT_MS = 20000;

function dir() { return path.join(app.getPath("userData"), "patch"); }
function statePath() { return path.join(dir(), "state.json"); }

function readState() {
  try { return JSON.parse(fs.readFileSync(statePath(), "utf8")) || {}; }
  catch { return {}; }
}
function writeState(s) {
  try {
    fs.mkdirSync(dir(), { recursive: true });
    fs.writeFileSync(statePath(), JSON.stringify(s, null, 2));
  } catch (e) { /* a patch we cannot record is a patch we will not use */ }
}

/* GET, following redirects, with a ceiling and a deadline. GitHub Pages
   serves the payload; release assets redirect, so redirects are followed
   rather than treated as failure. */
function get(url, { binary = false, redirects = 0 } = {}) {
  return new Promise((resolve, reject) => {
    if (redirects > 4) return reject(new Error("too many redirects"));
    const req = https.get(url, { timeout: NET_TIMEOUT_MS }, res => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
        res.resume();
        return resolve(get(new URL(res.headers.location, url).href,
                           { binary, redirects: redirects + 1 }));
      }
      if (res.statusCode !== 200) { res.resume(); return reject(new Error("HTTP " + res.statusCode)); }
      const chunks = [];
      let n = 0;
      res.on("data", d => {
        n += d.length;
        if (n > MAX_BYTES) { req.destroy(); return reject(new Error("patch too large")); }
        chunks.push(d);
      });
      res.on("end", () => {
        const buf = Buffer.concat(chunks);
        resolve(binary ? buf : buf.toString("utf8"));
      });
    });
    req.on("timeout", () => { req.destroy(); reject(new Error("timeout")); });
    req.on("error", reject);
  });
}

/* ---------------------------------------------------------------------
   Where the window should load the studio from, decided at startup.

   Called before the window is created, so it does no network work and
   makes no promises: it reports what is already on disk and verified.
   --------------------------------------------------------------------- */
function studioDir(shippedDir, log = () => {}) {
  if (process.argv.includes("--gv-no-patch")) {
    log("patch channel disabled by --gv-no-patch");
    return shippedDir;
  }
  const st = readState();
  if (!st.active || !st.active.version) return shippedDir;

  const p = path.join(dir(), st.active.version);
  const idx = path.join(p, "index.html");
  if (!fs.existsSync(idx)) {
    log("patch " + st.active.version + " is recorded but missing on disk — using the shipped studio");
    return shippedDir;
  }

  /* A PATCH THAT DID NOT COME UP LAST TIME DOES NOT GET A THIRD GO.

     `pending` is written before the window loads and cleared once the
     renderer says it is alive. Finding it still set means the last start
     with this patch never got that far: the page threw during parse, or
     the app was killed on a white screen. Twice is a pattern, and a
     broken studio cannot be fixed from inside itself. */
  if ((st.failures || 0) >= 2) {
    log("patch " + st.active.version + " failed to come up twice — rolled back to the shipped studio");
    writeState({ ...st, active: null, rolledBack: st.active, failures: 0 });
    return shippedDir;
  }

  writeState({ ...st, failures: (st.failures || 0) + 1 });
  log("studio patch " + st.active.version + " in use (" + p + ")");
  return p;
}

/* The renderer got far enough to run. Whatever is active is good. */
function markHealthy() {
  const st = readState();
  if (!st.active || !st.failures) return;
  writeState({ ...st, failures: 0 });
}

/* ---------------------------------------------------------------------
   Check, download, verify, stage. Everything here happens behind the
   window and applies at the NEXT start — swapping the page under
   somebody who is mid-design is not an improvement over waiting.
   --------------------------------------------------------------------- */
async function check(version, log = () => {}) {
  if (process.argv.includes("--gv-no-patch") || !BASE) return null;
  try {
    const raw = await get(BASE + "/app/patch?v=" + encodeURIComponent(version || ""));
    const j = JSON.parse(raw);
    const pt = j && j.patch;
    if (!pt || !pt.version || !pt.url || !pt.sha256) return null;

    const st = readState();
    if (st.active && st.active.version === pt.version) return null;     // already running it
    if (st.staged && st.staged.version === pt.version) return null;     // already waiting
    if (st.rolledBack && st.rolledBack.version === pt.version) {
      log("patch " + pt.version + " was rolled back here before — not retrying it");
      return null;
    }

    log("studio patch " + pt.version + " available (" + Math.round((pt.bytes || 0) / 1024) + " KB)");
    const buf = await get(pt.url, { binary: true });

    const got = crypto.createHash("sha256").update(buf).digest("hex");
    if (got !== String(pt.sha256).toLowerCase()) {
      /* Not a warning. Either the payload was corrupted in transit or it
         is not the file the administrator signed off, and this app runs
         whatever it writes here. */
      log("PATCH REJECTED: sha256 " + got.slice(0, 16) + "… does not match the manifest");
      return null;
    }
    if (!/^\s*<!doctype html/i.test(buf.slice(0, 200).toString("utf8"))) {
      log("PATCH REJECTED: payload is not an HTML document");
      return null;
    }

    const dest = path.join(dir(), pt.version);
    fs.mkdirSync(dest, { recursive: true });
    fs.writeFileSync(path.join(dest, "index.html"), buf);

    writeState({ ...readState(), staged: { ...pt, at: Date.now() } });
    log("studio patch " + pt.version + " verified and staged — it applies at the next start");
    return pt;
  } catch (e) {
    /* Offline, or the Worker is down, or GitHub is. None of that is an
       error the reader needs to see: the app they have keeps working. */
    log("patch check skipped: " + e.message);
    return null;
  }
}

/* Promote whatever is staged. Called at startup, BEFORE studioDir, so a
   patch downloaded during the previous session becomes the active one
   exactly once and only between runs. */
function promoteStaged(log = () => {}) {
  const st = readState();
  if (!st.staged || !st.staged.version) return;
  const idx = path.join(dir(), st.staged.version, "index.html");
  if (!fs.existsSync(idx)) { writeState({ ...st, staged: null }); return; }

  const old = st.active;
  writeState({ active: st.staged, staged: null, failures: 0,
               rolledBack: st.rolledBack || null, previous: old || null });
  log("studio patch " + st.staged.version + " is now active");

  /* Keep the previous one; delete anything older. A rollback needs one
     step back, not a museum. */
  try {
    const keep = new Set([st.staged.version, old && old.version].filter(Boolean));
    for (const name of fs.readdirSync(dir())) {
      const full = path.join(dir(), name);
      if (name === "state.json" || keep.has(name)) continue;
      if (fs.statSync(full).isDirectory()) fs.rmSync(full, { recursive: true, force: true });
    }
  } catch (e) { /* housekeeping is not worth failing a start over */ }
}

/* What the About box and the admin need to see. */
function status() {
  const st = readState();
  return {
    active: st.active ? { version: st.active.version, notes: st.active.notes || "" } : null,
    staged: st.staged ? { version: st.staged.version } : null,
    rolledBack: st.rolledBack ? { version: st.rolledBack.version } : null
  };
}

/* Back to the studio that shipped in the installer, now. Used by the
   Help menu when a patch misbehaves in a way the automatic rollback
   cannot see - it renders, it just renders wrongly. */
function revert(log = () => {}) {
  const st = readState();
  writeState({ ...st, active: null, staged: null,
               rolledBack: st.active || st.rolledBack || null, failures: 0 });
  log("reverted to the studio that shipped with this build");
}

module.exports = { studioDir, check, promoteStaged, markHealthy, status, revert, dir };
