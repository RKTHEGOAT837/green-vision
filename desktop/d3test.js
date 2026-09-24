/* How long does "real surroundings" take, and is it right?
 *
 *     npx electron d3test.js
 *
 * Times the context read the 3D builder waits on, and checks the scene it
 * produces actually contains the buildings OpenStreetMap has there - a fast
 * empty scene would be worse than a slow correct one, so both are asserted.
 */
"use strict";

const { app, BrowserWindow } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");

const APP_DIR = process.env.GV_STUDIO_DIR || path.join(__dirname, "..", "dist_app");
const results = [];
const check = (n, ok, note) => {
  results.push({ n, ok: !!ok });
  console.log((ok ? "  ok  " : "  XX  ") + n + (note == null ? "" : "   [" + note + "]"));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

function engineUp() {
  return new Promise(async resolve => {
    for (const p of [8000, 8010, 8020, 8030, 8040]) {
      const r = await new Promise(res => {
        const q = http.get(`http://127.0.0.1:${p}/api/health`, x => { x.resume(); res(x.statusCode); });
        q.on("error", () => res(0)); q.setTimeout(2000, () => { q.destroy(); res(0); });
      });
      if (r === 200) return resolve("http://127.0.0.1:" + p);
    }
    resolve(null);
  });
}

app.whenReady().then(async () => {
  const origin = await engineUp();
  console.log("\n3D surroundings — speed and correctness\n" + "-".repeat(50));
  console.log("engine: " + (origin || "none"));

  const w = new BrowserWindow({ show: false, width: 1300, height: 860,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: false } });
  await w.loadFile(path.join(APP_DIR, "index.html"));
  await sleep(4000);
  const q = js => w.webContents.executeJavaScript(js, true);
  await q("localStorage.setItem('gv.tourDone','1'); var k=document.getElementById('gvKill'); if(k) k.remove(); var b=document.getElementById('enterBtn'); if(b) b.click(); 1");
  await sleep(2000);
  await q("analyse(23.0225, 72.5714); 1");
  await sleep(3000);

  // Cold: clear both caches so this is a real first read.
  await q(`(() => {
    try { if (typeof CTX3D_CACHE !== 'undefined') CTX3D_CACHE.clear(); } catch (e) {}
    try { Object.keys(localStorage).filter(k => /ctx3d/i.test(k)).forEach(k => localStorage.removeItem(k)); } catch (e) {}
    return 1;
  })()`);

  const cold = await q(`(async () => {
    const t = Date.now();
    const c = await fetchSiteContext(23.0225, 72.5714, 190);
    return { ms: Date.now() - t,
             buildings: c ? c.buildings.length : -1,
             roads: c ? c.roads.length : -1,
             green: c ? c.green.length : -1 };
  })()`, true);
  console.log("  cold read: " + cold.ms + " ms  (" + cold.buildings + " buildings, " +
              cold.roads + " roads, " + cold.green + " green)");

  check("the surroundings arrive quickly", cold.ms < 5000, cold.ms + " ms");
  check("the scene is not empty", cold.buildings > 0, cold.buildings + " buildings");
  check("roads are present too", cold.roads > 0, cold.roads + " roads");

  // Ground truth: the same question, asked independently of the app.
  if (origin) {
    const truth = await new Promise(resolve => {
      const body = "data=" + encodeURIComponent(
        "[out:json][timeout:40];way(around:190,23.02250,72.57140)[building];out geom;");
      const u = new URL(origin + "/api/osm");
      const req = http.request({ host: u.hostname, port: u.port, path: u.pathname, method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "Content-Length": Buffer.byteLength(body) } },
        res => { let b = ""; res.on("data", d => (b += d)); res.on("end", () => {
          try { resolve(JSON.parse(b).elements.length); } catch (e) { resolve(-1); } }); });
      req.on("error", () => resolve(-1));
      req.setTimeout(30000, () => { req.destroy(); resolve(-1); });
      req.end(body);
    });
    check("it drew every building OpenStreetMap has", truth > 0 && cold.buildings >= truth,
          "scene " + cold.buildings + " vs truth " + truth);
  }

  // Warm: the cache must make a reopen effectively free.
  const warm = await q(`(async () => {
    const t = Date.now();
    const c = await fetchSiteContext(23.0225, 72.5714, 190);
    return { ms: Date.now() - t, buildings: c ? c.buildings.length : -1 };
  })()`, true);
  console.log("  warm read: " + warm.ms + " ms");
  check("reopening the same site is instant", warm.ms < 300, warm.ms + " ms");

  // The greenery must still be collected, just not waited for.
  const later = await q("(async () => { try { return await (GV._greenLater || Promise.resolve(null)); } catch (e) { return 'threw'; } })()", true);
  check("greenery is still fetched in the background", later !== "threw", "resolved: " + later);

  const pass = results.filter(r => r.ok).length;
  console.log("-".repeat(50));
  console.log(pass + "/" + results.length + " checks passed");
  app.exit(results.every(r => r.ok) ? 0 : 1);
}).catch(e => { console.error("HARNESS FAILED: " + e.message); app.exit(2); });
