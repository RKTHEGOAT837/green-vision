/* Loads the real bundle with the real preload in a hidden window and asks
   the page whether the desktop layer actually took effect. Run with:

       npx electron selftest.js

   It exits non-zero on the first failure, so it can gate a release. */

"use strict";

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");

const accounts = require("./accounts");
const auth = require("./auth");

const APP_DIR = path.join(__dirname, "..", "dist_app");

const results = [];
const check = (name, ok, note) => {
  results.push({ name, ok: !!ok, note: note || "" });
  console.log((ok ? "  ok  " : "  XX  ") + name + (ok || !note ? "" : "   [" + note + "]"));
};

app.whenReady().then(async () => {
  accounts.init(app.getPath("userData"));
  auth.init({ accounts, onSignedIn: () => {} });
  // the same handler main.js registers, so the page exercises the real path
  ipcMain.handle("gv:auth", (_e, msg) => auth.handle(msg));

  const w = new BrowserWindow({
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  await w.loadFile(path.join(APP_DIR, "index.html"));
  // the studio boots on DOMContentLoaded; give it a beat
  await new Promise(r => setTimeout(r, 2500));

  const q = js => w.webContents.executeJavaScript(js, true);

  console.log("\nGreen Vision — desktop self-test\n" + "-".repeat(48));

  check("the bridge is exposed to the page", await q("typeof window.__GV_DESKTOP__ === 'object'"));
  check("it reports the platform", (await q("window.__GV_DESKTOP__.platform")) === "windows");
  check("the desktop class is on the document",
        await q("document.documentElement.classList.contains('gv-desktop')"));

  check("auth switched out of device-only mode",
        (await q("GV.auth.mode")) === "desktop");
  check("the studio still exposes its engines",
        await q("typeof GV.engines.computeCost === 'function'"));

  // The rebinding that makes every existing call site use the new sheet.
  check("openAuth was rebound to the desktop sign-in sheet",
        (await q("GV.auth.uiVariant")) === "desktop");
  check("the benefits reached the page over IPC",
        await q("Array.isArray(GV.auth.benefits) && GV.auth.benefits.length >= 3"));

  check("the native save hook is present",
        (await q("typeof window.gvDesktopSave")) === "function");
  check("the bridge offers a save dialog",
        (await q("typeof window.__GV_DESKTOP__.saveFile")) === "function");
  check("the bridge offers sign-in",
        (await q("typeof window.__GV_DESKTOP__.auth.request")) === "function");

  // The page must NOT be able to reach Node.
  check("the page cannot reach require()", (await q("typeof require")) === "undefined");
  check("the page cannot reach process", (await q("typeof process")) === "undefined");

  // Benefits are read from accounts.js, so the sheet cannot promise
  // something the app does not gate.
  const st = await auth.handle({ type: "status" });
  check("status answers", st && st.ok);
  check("benefits are declared", st && Array.isArray(st.benefits) && st.benefits.length >= 3);
  // config.json now carries the deployment URL, so a shipped build is
  // configured without needing a shell environment.
  check("sign-in is configured", st && st.configured === true, "no authUrl found");
  check("the renderer is given the API base", st && typeof st.base === "string" && /^https:/.test(st.base));

  /* Which edition this working copy is. The two installers differ only by
     `requireSignIn` in config.json, and the failure that matters is the
     silent one: a build that carries the gate flag but ships under the open
     name, or the reverse. The build script restores config.json to the open
     edition in a `finally`, so a checkout that reports "managed" here means
     a build was interrupted and the working copy was left dirty - and the
     next ordinary `npm run dist` would quietly produce a gated app under the
     ungated filename. */
  check("the edition is reported to the renderer",
        st && (st.edition === "open" || st.edition === "managed"),
        st && ("edition was " + JSON.stringify(st.edition)));
  check("requireSignIn and edition agree",
        st && st.requireSignIn === (st.edition === "managed"));
  check("the working copy is the open edition",
        st && st.edition === "open",
        "config.json still has requireSignIn set - a build was interrupted");

  // The request path is the one the Sign in button actually takes.
  const req = await auth.handle({ type: "request", email: "selftest@example.com",
                                  profile: { name: "Self Test" } });
  /* What is being tested is REACHABILITY - that the button's path gets an
     answer from the service. Any considered answer proves that, so the rate
     limiter and a revoked account both count as a pass.

     Not pedantry: this account is a real row in the live database that
     anyone can revoke from the admin page, and revoking it turned this check
     red while nothing was wrong with the app. A test that fails because
     somebody exercised a feature is a test that gets ignored. Only silence -
     a network error, a missing handler, a mangled URL - should fail here. */
  check("the sign-in request reaches the service",
        req && (req.ok === true ||
                /Too many/.test(req.error || "") ||
                /withdrawn by an administrator/.test(req.error || "")),
        req && req.error);

  // A callback that was never asked for must be refused.
  const bogus = await auth.handle({
    type: "callback",
    url: "greenvision://auth?token=abc&state=whatever"
  });
  check("an unsolicited sign-in callback is rejected", bogus && bogus.ok === false,
        bogus && bogus.error);

  const failed = results.filter(r => !r.ok);
  console.log("-".repeat(48));
  console.log(`  ${results.length - failed.length} passed, ${failed.length} failed\n`);
  app.exit(failed.length ? 1 : 0);
});
