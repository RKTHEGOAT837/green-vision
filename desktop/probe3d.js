/* Why is the builder's plot status not recorded? Surface the error. */
"use strict";
const { app, BrowserWindow } = require("electron");
const path = require("path");
const APP_DIR = path.join(__dirname, "..", "dist_app");
const sleep = ms => new Promise(r => setTimeout(r, ms));

app.whenReady().then(async () => {
  const w = new BrowserWindow({ show: false, width: 1200, height: 800,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: false } });
  w.webContents.on("console-message", (_e, lvl, m) => {
    if (lvl >= 2) console.log("  [page] " + m.slice(0, 200));
  });
  await w.loadFile(path.join(APP_DIR, "index.html"));
  await sleep(4000);
  const q = js => w.webContents.executeJavaScript(js, true);

  await q("localStorage.setItem('gv.tourDone','1'); var k=document.getElementById('gvKill'); if(k) k.remove(); var b=document.getElementById('enterBtn'); if(b) b.click(); 1");
  await sleep(2000);
  await q("analyse(23.0445, 72.5117); 1");
  await sleep(4000);

  console.log("\nprobe: open the builder with no design");
  const out = await q(`(async () => {
    GV.design = null;
    try {
      await GV.builder.open('occupied');
      return { threw: null };
    } catch (e) {
      return { threw: String((e && e.stack) || e).slice(0, 400) };
    }
  })()`, true);
  console.log("  open() threw: " + (out.threw || "nothing"));

  await sleep(6000);
  const st = await q(`(() => ({
    overlay: !!document.getElementById('gv3dCanvas'),
    design: !!GV.design,
    plot: GV.design && GV.design.plot ? {
      reason: GV.design.plot.reason, sited: !!GV.design.plot.sited,
      on: GV.design.plot.on_buildings || 0,
      audit: GV.design.plot.audit || null,
      corners: (GV.design.plot.ring || []).length
    } : null,
    hasSquareRing: typeof squareRing,
    hasEnsure: typeof actEnsureDesign
  }))()`);
  console.log("  " + JSON.stringify(st));
  app.exit(0);
}).catch(e => { console.error("probe failed: " + e.message); app.exit(2); });
