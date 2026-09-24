/* Full functional test of the shipped app, run the way selftest.js is:
 *
 *     npx electron functest.js
 *
 * selftest.js proves the DESKTOP LAYER is wired (bridge, preload, isolation,
 * edition). This proves the APP WORKS: that the bundled engine comes up, that
 * the local OpenStreetMap index answers, that a map click fills every panel
 * with real numbers, that each figure carries a label and a named source, and
 * that the walkthrough runs.
 *
 * It deliberately does NOT exercise the sign-in request. selftest.js does, and
 * that path asks the live service to send mail; a functional test that is run
 * repeatedly must not put mail on the wire as a side effect.
 *
 * Exits non-zero if any check fails, so it can gate a release.
 */
"use strict";

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");

const accounts = require("./accounts");
const auth = require("./auth");
const engine = require("./engine");

const APP_DIR = path.join(__dirname, "..", "dist_app");
const SHOTS = path.join(__dirname, "functest-shots");

const results = [];
const check = (name, ok, note) => {
  results.push({ name, ok: !!ok, note: note == null ? "" : String(note) });
  console.log((ok ? "  ok  " : "  XX  ") + name + (note == null ? "" : "   [" + note + "]"));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

function get(url, ms = 5000) {
  return new Promise(resolve => {
    const req = http.get(url, res => {
      let b = "";
      res.on("data", d => (b += d));
      res.on("end", () => resolve({ status: res.statusCode, text: b }));
    });
    req.on("error", () => resolve(null));
    req.setTimeout(ms, () => { req.destroy(); resolve(null); });
  });
}

function postOsm(port, ql, ms = 9000) {
  return new Promise(resolve => {
    const body = "data=" + encodeURIComponent(ql);
    const t0 = Date.now();
    const req = http.request({ host: "127.0.0.1", port, path: "/api/osm", method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Content-Length": Buffer.byteLength(body) } },
      res => { let b = ""; res.on("data", d => (b += d)); res.on("end", () => resolve({ status: res.statusCode, text: b, ms: Date.now() - t0 })); });
    req.on("error", () => resolve(null));
    req.setTimeout(ms, () => { req.destroy(); resolve({ status: "STALL", ms: Date.now() - t0 }); });
    req.end(body);
  });
}

app.whenReady().then(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  accounts.init(app.getPath("userData"));
  auth.init({ accounts, onSignedIn: () => {} });
  ipcMain.handle("gv:auth", (_e, msg) => auth.handle(msg));

  console.log("\nGreen Vision — functional test\n" + "-".repeat(52));
  console.log("app version: " + app.getVersion());

  // ---------- 1. the bundled engine ----------------------------------
  let origin = null;
  await engine.start(app, m => console.log("  [engine] " + m));
  for (let k = 0; k < 120 && !origin; k++) {
    for (const p of [8000, 8010, 8020, 8030, 8040]) {
      const h = await get(`http://127.0.0.1:${p}/api/health`, 2500);
      if (h && h.status === 200) { origin = "http://127.0.0.1:" + p; break; }
    }
    if (!origin) await sleep(2000);
  }
  check("the bundled engine starts and answers", !!origin, origin || "no engine after 240s");

  const port = origin ? +origin.split(":").pop() : 0;

  // The 1.1.5 failure: the engine wrote into its own install directory and
  // died with PermissionError before it finished booting.
  if (origin) {
    const rec = await get(origin + "/outputs/ahmedabad/recommendations.geojson", 8000);
    check("the engine's written outputs are served", rec && rec.status === 200 && rec.text.length > 100,
          rec ? "HTTP " + rec.status + ", " + rec.text.length + " bytes" : "no answer");

    // The 1.1.4 failure: /api/osm blocked for the whole index load.
    const first = await postOsm(port, "[out:json][timeout:30];way(around:800,23.0225,72.5714)[building];out count;");
    check("the local OSM endpoint never stalls", first && first.status !== "STALL",
          first ? "HTTP " + first.status + " in " + first.ms + "ms" : "no answer");

    let osm = first, tries = 0;
    while (osm && osm.status === 503 && tries++ < 100) { await sleep(3000); osm = await postOsm(port, "[out:json][timeout:30];way(around:800,23.0225,72.5714)[building];out count;"); }
    check("the local OSM index answers the census query", osm && osm.status === 200 && /"total"/.test(osm.text || ""),
          osm ? "HTTP " + osm.status + " in " + osm.ms + "ms" : "no answer");
  }

  // ---------- 2. the real window, real preload -------------------------
  const w = new BrowserWindow({
    show: false, width: 1440, height: 900,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: false }
  });
  const pageErrors = [];
  w.webContents.on("console-message", (_e, level, message) => { if (level >= 2 && !/favicon|ERR_|Electron Security Warning|Content-Security-Policy/.test(message)) pageErrors.push(message.slice(0, 160)); });
  await w.loadFile(path.join(APP_DIR, "index.html"));
  await sleep(4000);
  const q = js => w.webContents.executeJavaScript(js, true);
  const shot = async n => { try { const img = await w.webContents.capturePage(); fs.writeFileSync(path.join(SHOTS, n + ".png"), img.toPNG()); } catch (e) {} };

  check("the desktop bridge reached the page", await q("typeof window.__GV_DESKTOP__ === 'object'"));
  check("the page cannot reach Node", (await q("typeof require")) === "undefined");

  // ---------- 3. the mandatory-update gate -----------------------------
  await sleep(8000);
  const pageVer = await q("(window.D && D.version) || null");
  const killed = await q("!!document.getElementById('gvKill')");
  /* The version the PAGE sees is what the gate judges. Under `npx electron`
     there is no packaged version to report, so the page falls back to 1.0.0
     and is correctly blocked - that is the gate working, not the app being
     broken. The check is therefore on the gate's REASONING, not on the
     absence of the screen: block iff the page's version is below the floor. */
  const floor = await q("(() => { try { return (JSON.parse(localStorage.getItem('gv.notice') || '{}').min) || ''; } catch (e) { return ''; } })()");
  const below = !pageVer || pageVer.split('.').map(Number)
      .some((v, i) => v !== (floor.split('.').map(Number)[i] || 0)
        ? v < (floor.split('.').map(Number)[i] || 0) : false);
  check("the update gate judges this build correctly", killed === below,
        "page version " + pageVer + ", floor " + floor + ", blocked " + killed);
  if (killed) { await q("var k=document.getElementById('gvKill'); if(k) k.remove(); 1"); }

  // ---------- 4. into the map ------------------------------------------
  await q("localStorage.setItem('gv.tourDone','1'); 1");
  await q("var b=document.getElementById('enterBtn'); if (b) b.click(); 1");
  await sleep(3000);
  const entered = await q("!!document.getElementById('gvDock') || !!window.map");
  check("the map opens from the start screen", entered);

  await q(`(() => { const m = window.map; if (m && m.fireEvent) {}
            if (window.GV && typeof analyse === 'function') { analyse(23.0225, 72.5714); }
            else if (window.map) { window.map.fire('click', { latlng: { lat: 23.0225, lng: 72.5714 } }); }
            return 1; })()`);
  await sleep(4000);
  const aoi = await q("window.GV && GV.aoi ? (GV.aoi.lat.toFixed(3) + ',' + GV.aoi.lon.toFixed(3)) : null");
  check("a map click sets the area of interest", !!aoi, aoi || "no AOI");

  // ---------- 5. the Area panel and the census -------------------------
  await q("GV.ui.area(); 1");
  let census = null;
  for (let k = 0; k < 90; k++) {
    await sleep(5000);
    census = await q("(window.GV && GV.ctx && GV.ctx.census) ? GV.ctx.census.buildings : null");
    if (census) break;
  }
  check("the feature census arrives", !!census, census ? census + " buildings" : "stuck on the retry card after 450s");
  await shot("1-area");

  const air = await q("(window.GV && GV.ctx && GV.ctx.wa && GV.ctx.wa.aqi) ? GV.ctx.wa.aqi.mean : null");
  check("air quality is read", air != null, air);

  const chips = await q(`(() => {
    const out = [];
    document.querySelectorAll('#gvDock .gv-prov').forEach(e => {
      const s = e.nextElementSibling && e.nextElementSibling.className === 'gv-src' ? e.nextElementSibling.textContent.trim() : '';
      out.push({ label: e.textContent.trim(), src: s });
    });
    return out;
  })()`);
  check("area figures carry provenance labels", chips.length >= 8, chips.length + " chips");
  /* A "no data" chip has no source to name, and inventing one would be the
     opposite of the point. The claim under test is that every figure WITH a
     reading says where the reading came from. */
  const withData = chips.filter(c => c.label !== "no data");
  check("every area figure with data names its source",
        withData.length > 0 && withData.every(c => c.src.length > 0),
        withData.filter(c => !c.src).map(c => c.label).join(",") ||
        ("all " + withData.length + " named, " + (chips.length - withData.length) + " had no data"));
  check("sourced area figures read 'measured'",
        chips.every(c => c.label !== "modelled"),
        Array.from(new Set(chips.map(c => c.label))).join(","));
  check("the named sources are the real providers",
        chips.some(c => /Open-Meteo/.test(c.src)) && chips.some(c => /OpenStreetMap/.test(c.src)) && chips.some(c => /Esri/.test(c.src)),
        Array.from(new Set(chips.map(c => c.src))).join(" / "));

  // ---------- 5b. build a real design ----------------------------------
  /* Cost and Review are empty until something has been designed, and their
     empty states ("Nothing to cost yet") are correct rather than broken. To
     test what they actually do, draw a plot and plant it first - through
     GV.act, which is the same surface the assistant drives, so this is the
     real path and not a fixture. */
  let plot = null, planted = null;
  try {
    plot = await q("GV.act.plot(10000)", true);
    check("a plot can be sited on open ground", !!plot && !plot.unverified,
          plot ? (plot.unverified ? "unverified: " + plot.reason : Math.round(plot.area_m2) + " m²") : "no plot");
    if (plot && !plot.unverified) {
      planted = await q(`GV.act.plant([{ species: "Neem", count: 40 }, { species: "Peepal", count: 20 }], 8)`, true);
      check("trees can be planted into the plot", planted && planted.placed > 0,
            planted ? planted.placed + " placed, missing: " + JSON.stringify(planted.missing) : "none");
      await sleep(2500);
    }
  } catch (e) { check("a plot can be sited on open ground", false, String(e.message).slice(0, 90)); }

  // ---------- 6. the other panels --------------------------------------
  for (const [fn, label] of [["traffic", "Traffic"], ["cost", "Cost"], ["review", "Review"], ["gallery", "Library"], ["history", "History"]]) {
    try {
      await q(`GV.ui.${fn}(); 1`);
      await sleep(8000);
      const body = await q(`(() => {
        const d = document.getElementById('gvDockBody') || document.getElementById('gvDock');
        const t = (d && d.innerText || '').replace(/\s+/g, ' ').trim();
        return { chars: t.length, nodes: d ? d.querySelectorAll('*').length : 0, head: t.slice(0, 90) };
      })()`);
      const title = await q("document.getElementById('gvDockTitle') ? document.getElementById('gvDockTitle').textContent.trim() : ''");
      /* The Library is shared between signed-in accounts, so signed out the
         correct render is the invitation to sign in - not a list. This test
         never signs in (that would put mail on the wire), so that is the
         expected pass. */
      const wantsSignIn = fn === "gallery" && /sign in/i.test(body.head);
      check(label + " panel renders content", (body.chars > 40 && body.nodes > 5) || wantsSignIn,
            title + " · " + body.nodes + " nodes, " + body.chars + " chars · " + body.head);
      await shot("2-" + fn);
    } catch (e) { check(label + " panel renders content", false, String(e.message).slice(0, 90)); }
  }

  // ---------- 6b. the figures those panels computed --------------------
  if (plot && !plot.unverified) {
    const cost = await q("(window.GV && GV.design && GV.design.cost) ? GV.design.cost.grand : null");
    check("the costing produces a figure", typeof cost === "number" && cost > 0, cost);
    const rev = await q("(window.GV && GV.design && GV.design.review) ? GV.design.review.score : null");
    check("the review produces a score", typeof rev === "number", rev);
  }

  // ---------- 7. the walkthrough ---------------------------------------
  try {
    await q("gvTour.start(); 1");
    await sleep(3500);
    check("the walkthrough opens", await q("!!document.getElementById('gvTour')"),
          await q("document.querySelector('#gvTour .gvt-step') ? document.querySelector('#gvTour .gvt-step').textContent : ''"));
    for (let i = 0; i < 4; i++) { await q("var b=document.querySelector('#gvTour .gvt-next'); if (b) b.click(); 1"); await sleep(2500); }
    check("the walkthrough advances through its steps", await q("!!document.getElementById('gvTour')"),
          await q("document.querySelector('#gvTour .gvt-step') ? document.querySelector('#gvTour .gvt-step').textContent : 'closed early'"));
    await shot("3-tour");
    await q("gvTour.stop(); 1");
  } catch (e) { check("the walkthrough opens", false, String(e.message).slice(0, 90)); }

  check("no uncaught page errors", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | ") || "clean");

  const pass = results.filter(r => r.ok).length;
  console.log("-".repeat(52));
  console.log(pass + "/" + results.length + " checks passed");
  results.filter(r => !r.ok).forEach(r => console.log("  FAILED: " + r.name + (r.note ? "  [" + r.note + "]" : "")));
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify(results, null, 2));
  try { engine.stop && engine.stop(); } catch (e) {}
  app.exit(results.every(r => r.ok) ? 0 : 1);
}).catch(e => { console.error("HARNESS FAILED: " + e.message); app.exit(2); });
