/* Does the site finder ever put a plot on something already built?
 *
 *     npx electron sitetest.js
 *
 * The app's own answer cannot be trusted to grade itself: it decides a
 * square is clear using the obstacle list it fetched, so asking it "is this
 * clear?" only repeats its own arithmetic. This harness therefore does its
 * OWN query for what is on the ground and its OWN overlap test, then grades
 * the plot the app produced.
 *
 * It samples many points across every shipped city, because one lucky
 * placement proves nothing. A single overlap is a failure: the product
 * claim is that it will refuse rather than build on a building.
 */
"use strict";

const { app, BrowserWindow } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");

const APP_DIR = process.env.GV_STUDIO_DIR || path.join(__dirname, "..", "dist_app");
const OUT = path.join(__dirname, "site-report");
const ENGINE_PORTS = [8000, 8010, 8020, 8030, 8040];

/* Where to test. Each city gets its centre plus a ring of offsets, so the
   sample includes dense core, edge and in between rather than one hand-
   picked spot that happens to work. */
const CITIES = [
  ["Ahmedabad", 23.0225, 72.5714],
  ["Bengaluru", 12.9716, 77.5946],
  ["Chennai",   13.0827, 80.2707],
  ["Delhi",     28.6139, 77.2090],
  ["Mumbai",    19.0760, 72.8777]
];
const OFFSETS = [[0, 0], [0.008, 0], [-0.008, 0], [0, 0.008], [0, -0.008],
                 [0.005, 0.005], [-0.005, -0.005]];

const sleep = ms => new Promise(r => setTimeout(r, ms));
let engineOrigin = null;

function post(origin, ql, ms = 60000) {
  return new Promise(resolve => {
    const u = new URL(origin + "/api/osm");
    const body = "data=" + encodeURIComponent(ql);
    const req = http.request({ host: u.hostname, port: u.port, path: u.pathname, method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Content-Length": Buffer.byteLength(body) } },
      res => { let b = ""; res.on("data", d => (b += d)); res.on("end", () => {
        try { resolve({ status: res.statusCode, json: JSON.parse(b) }); }
        catch (e) { resolve({ status: res.statusCode, json: null }); } }); });
    req.on("error", () => resolve(null));
    req.setTimeout(ms, () => { req.destroy(); resolve(null); });
    req.end(body);
  });
}

/* The same obstacle question the app asks, asked independently. */
function obstacleQuery(lat, lon, r) {
  const la = lat.toFixed(5), lo = lon.toFixed(5);
  return `[out:json][timeout:50];
(
  way(around:${r},${la},${lo})[building];
  way(around:${r},${la},${lo})[natural=water];
  way(around:${r},${la},${lo})[leisure~"^(swimming_pool|pitch|track|golf_course)$"];
  way(around:${r},${la},${lo})[amenity=parking];
);
out bb;`;
}

/* Overlap in plain degrees. Both shapes are axis-aligned boxes, so this is
   exact for the data being compared - no projection, nothing to get subtly
   wrong. A shared EDGE is not an overlap; a shared interior is. */
function overlaps(ring, b) {
  const lats = ring.map(p => p[0]), lons = ring.map(p => p[1]);
  const s = Math.min(...lats), n = Math.max(...lats);
  const w = Math.min(...lons), e = Math.max(...lons);
  return !(s >= b.maxlat || n <= b.minlat || w >= b.maxlon || e <= b.minlon);
}

const results = [];

app.whenReady().then(async () => {
  fs.mkdirSync(OUT, { recursive: true });

  for (const p of ENGINE_PORTS) {
    const r = await new Promise(res => {
      const q = http.get(`http://127.0.0.1:${p}/api/health`, x => { x.resume(); res(x.statusCode); });
      q.on("error", () => res(0)); q.setTimeout(2500, () => { q.destroy(); res(0); });
    });
    if (r === 200) { engineOrigin = "http://127.0.0.1:" + p; break; }
  }
  console.log("\nSite finder — does it ever build on a building?\n" + "-".repeat(56));
  console.log("engine: " + (engineOrigin || "NONE (the app will use public Overpass)"));
  if (!engineOrigin) { console.error("no engine; start one first"); app.exit(2); return; }

  const w = new BrowserWindow({ show: false, width: 1400, height: 900,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: false } });
  await w.loadFile(path.join(APP_DIR, "index.html"));
  await sleep(4000);
  const q = js => w.webContents.executeJavaScript(js, true);
  await q("localStorage.setItem('gv.tourDone','1'); var k=document.getElementById('gvKill'); if(k) k.remove(); var b=document.getElementById('enterBtn'); if(b) b.click(); 1");
  await sleep(2500);

  let sited = 0, refused = 0, violations = 0, errors = 0;

  for (const [city, clat, clon] of CITIES) {
    console.log("\n" + city);
    for (const [dlat, dlon] of OFFSETS) {
      const lat = clat + dlat, lon = clon + dlon;
      let plot = null;
      try {
        await q(`analyse(${lat}, ${lon}); 1`);
        await sleep(2500);
        plot = await q(`(async () => { const p = await GV.findOpenPlot(10000);
                        return p ? JSON.parse(JSON.stringify(p)) : null; })()`, true);
      } catch (e) { errors++; console.log("  " + lat.toFixed(4) + "," + lon.toFixed(4) + "  harness error: " + String(e.message).slice(0, 60)); continue; }

      if (!plot) { errors++; console.log("  " + lat.toFixed(4) + "  no answer at all"); continue; }

      if (plot.unverified) {
        refused++;
        console.log("  " + lat.toFixed(4) + "," + lon.toFixed(4) + "  refused (" + plot.reason + ")  <- correct when it cannot see");
        results.push({ city, lat, lon, outcome: "refused", reason: plot.reason });
        continue;
      }

      // It claims a sited plot. Grade it independently.
      const r = await post(engineOrigin, obstacleQuery(lat, lon, 1200));
      if (!r || r.status !== 200 || !r.json || !Array.isArray(r.json.elements)) {
        errors++;
        console.log("  " + lat.toFixed(4) + "  could not fetch ground truth (HTTP " + (r && r.status) + ")");
        continue;
      }
      const boxes = r.json.elements.filter(e => e.bounds).map(e => e.bounds);
      const hits = boxes.filter(b => overlaps(plot.ring, b));
      sited++;
      if (hits.length) {
        violations++;
        console.log("  " + lat.toFixed(4) + "," + lon.toFixed(4) +
                    "  *** ON " + hits.length + " BUILT THING(S) *** moved " + plot.moved_m +
                    "m, app saw " + plot.obstacles + " obstacles, truth had " + boxes.length);
        results.push({ city, lat, lon, outcome: "VIOLATION", hits: hits.length,
                       moved_m: plot.moved_m, appSaw: plot.obstacles, truth: boxes.length,
                       ring: plot.ring, firstHit: hits[0] });
      } else {
        console.log("  " + lat.toFixed(4) + "," + lon.toFixed(4) +
                    "  clear (moved " + plot.moved_m + "m, checked against " + boxes.length + ")");
        results.push({ city, lat, lon, outcome: "clear", moved_m: plot.moved_m, truth: boxes.length });
      }
    }
  }

  console.log("\n" + "-".repeat(56));
  console.log("sited " + sited + " · refused " + refused + " · errors " + errors);
  console.log(violations ? ("FAILURES: " + violations + " plot(s) placed on something built")
                         : "no plot was placed on anything built");
  fs.writeFileSync(path.join(OUT, "sitetest.json"), JSON.stringify(results, null, 2));
  app.exit(violations ? 1 : 0);
}).catch(e => { console.error("HARNESS FAILED: " + e.message); app.exit(2); });
