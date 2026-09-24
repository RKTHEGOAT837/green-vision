/* While the map data is loading, does the app say so - and refuse to guess?
 *
 *     npx electron waittest.js
 *
 * The warm-up window is hard to catch by hand: it lasts a couple of minutes
 * once, after which the behaviour is unreachable. So the warming state is
 * forced, and what matters is asserted: the card appears, it counts down, it
 * does NOT offer to place an unchecked square, and the site finder returns
 * "still loading" rather than "OpenStreetMap did not answer".
 */
"use strict";

const { app, BrowserWindow } = require("electron");
const path = require("path");
const fs = require("fs");

const APP_DIR = process.env.GV_STUDIO_DIR || path.join(__dirname, "..", "dist_app");
const SHOTS = path.join(__dirname, "wait-shots");
const results = [];
const check = (n, ok, note) => {
  results.push({ n, ok: !!ok });
  console.log((ok ? "  ok  " : "  XX  ") + n + (note == null ? "" : "   [" + note + "]"));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

app.whenReady().then(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const w = new BrowserWindow({ show: false, width: 1300, height: 860,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: false } });
  await w.loadFile(path.join(APP_DIR, "index.html"));
  await sleep(4000);
  const q = js => w.webContents.executeJavaScript(js, true);
  const shot = async n => { try { fs.writeFileSync(path.join(SHOTS, n + ".png"), (await w.webContents.capturePage()).toPNG()); } catch (e) {} };

  console.log("\nWhile the map data loads\n" + "-".repeat(46));
  await q("localStorage.setItem('gv.tourDone','1'); var k=document.getElementById('gvKill'); if(k) k.remove(); var b=document.getElementById('enterBtn'); if(b) b.click(); 1");
  await sleep(2000);
  await q("analyse(23.0225, 72.5714); 1");
  await sleep(2500);

  /* Force the two conditions the window is made of: the index reports itself
     as warming, and the ground query therefore yields nothing. */
  await q(`(() => {
    window.__realFetch = window.fetch;
    window.gvOsmWarming = () => true;
    window.gvOsmWaitSeconds = () => 42;
    window.overpassFetch = async () => null;      // no ground data yet
    if (window.GV && GV._sites && GV._sites.cache) GV._sites.cache.clear();
    return 1;
  })()`);

  const plot = await q("(async () => JSON.parse(JSON.stringify(await GV.findOpenPlot(10000))))()", true);
  check("the finder reports 'still loading', not 'unreachable'",
        plot && plot.reason === "osm_warming", plot && plot.reason);
  check("it places nothing while blind", !!(plot && plot.unverified), "unverified=" + (plot && plot.unverified));
  check("it says how long to wait", plot && plot.wait_s > 0, plot && plot.wait_s);

  // The assistant path: it must show the card and refuse, without offering
  // the unchecked square.
  let msg = "";
  try {
    await q(`(async () => { await GV.act.plot(10000); })()`, true);
  } catch (e) { msg = String(e.message || e); }
  const said = await q(`(async () => {
    try { await GV.act.plot(10000); return ""; } catch (e) { return String(e.message || e); }
  })()`, true).catch(() => "");

  await sleep(500);
  const card = await q(`(() => {
    const el = document.getElementById('gvGroundWait');
    if (!el) return null;
    return { shown: !el.hidden, text: (el.innerText || '').replace(/\\s+/g, ' ').trim(),
             spinner: !!el.querySelector('.gv-spin') };
  })()`);
  check("a waiting card is shown", !!(card && card.shown), card && card.text.slice(0, 60));
  check("it has a spinner", !!(card && card.spinner));
  check("it tells the reader to wait", !!(card && /wait/i.test(card.text)));
  check("it shows a countdown", !!(card && /second/i.test(card.text)), card && (card.text.match(/About \d+ seconds left/) || [""])[0]);
  await shot("1-waiting");

  // Quick-plot must not offer to place an unchecked square right now.
  const offered = await q(`(() => {
    let asked = false;
    const real = window.confirm;
    window.confirm = () => { asked = true; return false; };
    try { const b = document.getElementById('gvQuickPlot') ||
                    Array.from(document.querySelectorAll('button')).find(x => /hectare square/i.test(x.textContent));
          if (b) b.click(); } catch (e) {}
    window.confirm = real;
    return asked;
  })()`);
  check("it does NOT offer an unchecked square while loading", offered === false, "confirm shown=" + offered);

  // And it closes itself when the data arrives.
  await q("window.gvOsmWarming = () => false; 1");
  await sleep(1600);
  const gone = await q("!document.getElementById('gvGroundWait')");
  check("the card closes itself once data is ready", gone);

  const pass = results.filter(r => r.ok).length;
  console.log("-".repeat(46));
  console.log(pass + "/" + results.length + " checks passed");
  app.exit(results.every(r => r.ok) ? 0 : 1);
}).catch(e => { console.error("HARNESS FAILED: " + e.message); app.exit(2); });
