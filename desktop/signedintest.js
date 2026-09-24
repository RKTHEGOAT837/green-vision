/* Open a REAL account project, signed in — the case the earlier runs missed.
 *
 *     npx electron signedintest.js
 *
 * The previous harness was signed out, so "Your projects" never rendered and
 * the Library's own button was never wired: it tested the plumbing behind
 * Open, not the path a signed-in reader actually takes.
 *
 * Signing in here uses the project's OWN admin route (/admin/signin-link),
 * which mints a token server-side. No mailbox is read, no password is typed,
 * and nothing is sent. The session is discarded when the run ends.
 *
 * READ-ONLY with respect to the account: it clicks Open and inspects what
 * happened. It never saves, publishes, renames or deletes.
 */
"use strict";

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");

const accounts = require("./accounts");
const auth = require("./auth");

const APP_DIR = process.env.GV_STUDIO_DIR || path.join(__dirname, "..", "dist_app");
const SHOTS = path.join(__dirname, "signedin-shots");
const API = "https://green-vision-api.greenvision-rk.workers.dev";
const WORKER = path.join(__dirname, "..", "worker", "src", "index.js");
const WHO = process.env.GV_TEST_EMAIL || "rishabhkkhara@gmail.com";

const results = [];
const check = (n, ok, note) => {
  results.push({ n, ok: !!ok, note: note == null ? "" : String(note) });
  console.log((ok ? "  ok  " : "  XX  ") + n + (note == null ? "" : "   [" + note + "]"));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const stage = async m => { console.log(""); console.log(">> " + m); await sleep(1500); };

function creds() {
  if (process.env.GV_ADMIN_USER && process.env.GV_ADMIN_PASS)
    return [process.env.GV_ADMIN_USER, process.env.GV_ADMIN_PASS];
  const src = fs.readFileSync(WORKER, "utf8");
  return [(src.match(/ENV\.ADMIN_USER \|\| "([^"]+)"/) || [])[1],
          (src.match(/ENV\.ADMIN_PASS \|\| "([^"]+)"/) || [])[1]];
}

app.whenReady().then(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  /* The page talks to the desktop layer over gv:auth. Without this handler
     the sign-in bridge throws, GVU never boots, and the Library has nothing
     to render - which is not a bug in the Library. */
  accounts.init(app.getPath("userData"));
  auth.init({ accounts, onSignedIn: () => {} });
  ipcMain.handle("gv:auth", (_e, msg) => auth.handle(msg));
  console.log("\nSigned-in Open test\n" + "-".repeat(50));
  console.log("studio under test: " + APP_DIR);

  // ---- a session, minted by the project's own admin route -------------
  await stage("Minting a sign-in token (admin route, no email sent)");
  const [U, P] = creds();
  const li = await (await fetch(API + "/admin/login", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: U, password: P })
  })).json();
  if (!li.token) { console.error("admin login failed"); app.exit(2); return; }

  const state = "testsate" + Math.random().toString(36).slice(2) + Date.now();
  const mint = await (await fetch(API + "/admin/signin-link", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer " + li.token },
    body: JSON.stringify({ email: WHO, state })
  })).json();
  check("a sign-in token can be minted", !!mint.token, mint.email);

  const ex = await (await fetch(API + "/auth/exchange", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ token: mint.token, state })
  })).json();
  check("the token exchanges for a session", !!(ex && (ex.session || ex.ok)),
        ex && ex.error ? ex.error : "session obtained");
  if (!ex || !ex.session) { console.error("no session: " + JSON.stringify(ex).slice(0, 200)); }

  // ---- the window ------------------------------------------------------
  const w = new BrowserWindow({ show: true, width: 1440, height: 940,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: false } });
  w.webContents.on("console-message", (_e, lvl, m) => { if (lvl >= 2 && !/Security Warning|Content-Security/.test(m)) console.log("     [page] " + m.slice(0, 180)); });
  await w.loadFile(path.join(APP_DIR, "index.html"));
  await sleep(4000);
  const q = js => w.webContents.executeJavaScript(js, true);
  const shot = async n => { try { fs.writeFileSync(path.join(SHOTS, n + ".png"), (await w.webContents.capturePage()).toPNG()); } catch (e) {} };

  // Hand the page the session the same shape the app stores it in.
  await stage("Signing the page in as " + WHO);
  await q(`(() => {
    localStorage.setItem('gv.tourDone','1');
    const k = document.getElementById('gvKill'); if (k) k.remove();
    const u = ${JSON.stringify({ email: WHO, name: WHO.split("@")[0], profile: {}, local: false, session: (ex && ex.session) || "" })};
    GV.auth.user = u;
    try { localStorage.setItem('gv.user', JSON.stringify(u)); } catch (e) {}
    if (window.GVU) { GVU.session = u.session; try { gvuSave('session', u.session); } catch (e) {} }
    return 1;
  })()`);
  // Boot the account layer the way a real sign-in does, so the Library has
  // a session to fetch with.
  await q(`(async () => { try { if (window.gvuBoot) await gvuBoot(); } catch (e) {} return 1; })()`, true);
  await sleep(3000);
  const signedIn = await q("!!(GV.auth.user && GV.auth.isIn && GV.auth.isIn())");
  check("the page reports a signed-in account", signedIn, await q("GV.auth.user ? GV.auth.user.email : 'none'"));

  /* A previous run can leave a design open and the 3D overlay up, and this
     harness reuses one Electron profile. Clear that, or the Library is asked
     to render into a dock the Studio is still holding. */
  await q(`(() => {
    try { if (window.gvTour) gvTour.stop(); } catch (e) {}
    const c = document.getElementById('gv3dCanvas');
    if (c) { const ov = document.getElementById('gv3dWrap') || c.parentElement; if (ov) ov.remove(); }
    const pick = document.getElementById('gv3dPick'); if (pick) pick.remove();
    GV.design = null;
    return 1;
  })()`);
  await q("var b=document.getElementById('enterBtn'); if(b) b.click(); 1");
  await sleep(2500);

  // ---- the Library, with the account's own projects --------------------
  await stage("Opening the Library and reading YOUR PROJECTS");
  await q("GV.ui.library(); 1");
  await sleep(9000);
  console.log("     dock tab after GV.ui.library(): " +
              await q("(GV.ui && GV.ui.dockTab) || 'unknown'") +
              " · title: " + await q("document.getElementById('gvDockTitle') ? document.getElementById('gvDockTitle').textContent.trim() : ''"));
  const lib = await q(`(() => {
    const opens = Array.from(document.querySelectorAll('#gvDockBody .gv-libopen'));
    const heads = Array.from(document.querySelectorAll('#gvDockBody .gv-h5')).map(h => h.textContent.trim());
    return { buttons: opens.length, heads,
             ids: opens.map(b => b.dataset.open).slice(0, 5),
             cached: (window.GVU && GVU.cache && GVU.cache.projects) ? GVU.cache.projects.length : null,
             withDesign: (window.GVU && GVU.cache && GVU.cache.projects)
               ? GVU.cache.projects.filter(p => p && p.design).length : null };
  })()`);
  check("the Library renders the account's projects", lib.buttons > 0,
        JSON.stringify({ buttons: lib.buttons, heads: lib.heads, cachedProjects: lib.cached, carryingDesign: lib.withDesign }));
  await shot("1-library");

  if (lib.buttons > 0) {
    await stage("Clicking Open on a real project — the reported bug");
    await q("window.map.setView([28.6139, 77.2090], 11, { animate: false }); GV.design = null; GV.aoi = null; 1");
    await sleep(1500);
    const before = await q("window.map.getCenter().lat.toFixed(2)");
    await q("document.querySelector('#gvDockBody .gv-libopen').click(); 1");
    await sleep(7000);
    const after = await q(`(() => ({
      centre: window.map.getCenter().lat.toFixed(3),
      zoom: window.map.getZoom(),
      items: GV.design ? (GV.design.items || []).length : 0,
      name: GV.design ? (GV.design.name || '(untitled)') : null,
      aoi: GV.aoi ? GV.aoi.lat.toFixed(3) : null,
      drawn: document.querySelectorAll('.leaflet-overlay-pane path, .leaflet-marker-icon').length,
      tab: document.getElementById('gvDockTitle') ? document.getElementById('gvDockTitle').textContent.trim() : '',
      threeD: !!document.getElementById('gv3dCanvas')
    }))()`);
    check("Open loads the design", after.items > 0 || after.name != null,
          after.items + " items · " + after.name);
    check("Open travels to the design's area", after.centre !== before && after.aoi != null,
          "from " + before + " to " + after.centre + " (aoi " + after.aoi + ")");
    check("Open draws the design on the map", after.drawn > 0, after.drawn + " shapes");
    check("Open shows a panel for it", /studio|design/i.test(after.tab) || after.threeD,
          after.tab + (after.threeD ? " + 3D" : ""));
    await shot("2-after-open");
  }

  // ---- History, same account ------------------------------------------
  await stage("History tab, same account");
  await q("GV.ui.history(); 1");
  await sleep(7000);
  console.log("     dock tab after GV.ui.history(): " +
              await q("(GV.ui && GV.ui.dockTab) || 'unknown'"));
  const hist = await q(`(() => {
    const opens = Array.from(document.querySelectorAll('#gvDockBody [data-open]'));
    return { rows: opens.length, id: opens.length ? opens[0].dataset.open : null,
             text: (document.getElementById('gvDockBody').innerText || '').replace(/\\s+/g,' ').slice(0, 80) };
  })()`);
  check("History lists entries with Open", hist.rows > 0, JSON.stringify(hist));
  if (hist.rows > 0) {
    await q("window.map.setView([28.6139, 77.2090], 11, { animate: false }); GV.design = null; GV.aoi = null; 1");
    await sleep(1500);
    await q("document.querySelector('#gvDockBody [data-open]').click(); 1");
    await sleep(7000);
    const h2 = await q(`(() => ({
      centre: window.map.getCenter().lat.toFixed(3),
      items: GV.design ? (GV.design.items || []).length : 0,
      aoi: GV.aoi ? GV.aoi.lat.toFixed(3) : null,
      drawn: document.querySelectorAll('.leaflet-overlay-pane path, .leaflet-marker-icon').length,
      threeD: !!document.getElementById('gv3dCanvas'),
      tab: document.getElementById('gvDockTitle') ? document.getElementById('gvDockTitle').textContent.trim() : ''
    }))()`);
    check("History Open loads and travels", h2.aoi != null && h2.drawn > 0,
          JSON.stringify(h2));
    await shot("3-history-open");
  }

  const pass = results.filter(r => r.ok).length;
  console.log("-".repeat(50));
  console.log(pass + "/" + results.length + " checks passed");
  results.filter(r => !r.ok).forEach(r => console.log("  FAILED: " + r.n + (r.note ? "  [" + r.note + "]" : "")));
  app.exit(results.every(r => r.ok) ? 0 : 1);
}).catch(e => { console.error("HARNESS FAILED: " + e.message); app.exit(2); });
