/* =====================================================================
   Green Vision — Windows desktop edition
   =====================================================================
   The same studio, the same engine, the same numbers. What the desktop
   adds is the set of things a browser tab cannot do:

     - it opens offline, because the baked engine ships inside the app
       rather than being fetched;
     - it can be signed in to, and the sign-in link comes back to THIS
       window through the greenvision:// protocol rather than stranding
       the reader in a browser tab;
     - it saves a bill of quantities where the reader chose to put it,
       through the real Windows save dialog;
     - it remembers where its window was, and it puts the designs you
       opened recently on the taskbar Jump List.

   Nothing here re-implements any part of the studio. The renderer loads
   the identical dist_app bundle the web build serves, so a fix in
   index.html reaches the desktop by rebuilding, never by porting.
   ===================================================================== */

"use strict";

const { app, BrowserWindow, shell, dialog, ipcMain, Menu, Notification } = require("electron");
const path = require("path");
const fs = require("fs");

const auth = require("./auth");
const accounts = require("./accounts");
const win32 = require("./windows");
const engine = require("./engine");

const PROTOCOL = "greenvision";
const isDev = !app.isPackaged;

/* Where the studio lives. Packaged, it is unpacked beside the binary; in
   development it is the repo's own dist_app, so `npm start` runs exactly
   what the installer will ship. */
const APP_DIR = isDev
  ? path.join(__dirname, "..", "dist_app")
  : path.join(process.resourcesPath, "studio");

let mainWindow = null;
/* A deep link can arrive before the window exists — Windows launches the
   app to deliver it. Hold it until the renderer is ready to be told. */
let pendingDeepLink = null;

/* ---------- single instance ----------------------------------------
   Clicking the sign-in link must reach the window the reader is already
   looking at. Without the lock, Windows starts a SECOND copy holding the
   token, the first stays signed out, and the reader watches a new window
   sign in while the one with their work in it does not. */
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => {
    handleDeepLink(argv.find(a => a.startsWith(PROTOCOL + "://")));
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

/* Register greenvision:// so the browser hands the callback back to us.
   In development Electron is the executable, so the argv dance is needed
   for Windows to route it to this project rather than to electron.exe. */
function registerProtocol() {
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient(PROTOCOL, process.execPath, [path.resolve(process.argv[1])]);
  } else {
    app.setAsDefaultProtocolClient(PROTOCOL);
  }
}

function handleDeepLink(url) {
  if (!url) return;
  pendingDeepLink = url;
  if (mainWindow && !mainWindow.webContents.isLoading()) flushDeepLink();
}

/* The callback is redeemed HERE, in the main process, and only the
   outcome crosses into the page. The renderer never sees the token or
   the state: a token in the renderer is a token any injected script on
   that page could read, and the whole point of the state check in
   auth.js is that this app decides which sign-ins it accepts. */
async function flushDeepLink() {
  if (!pendingDeepLink || !mainWindow) return;
  const url = pendingDeepLink;
  pendingDeepLink = null;

  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();

  const res = await auth.handle({ type: "callback", url });
  if (res && res.ok) {
    mainWindow.webContents.send("gv:signed-in", res.user);
  } else {
    mainWindow.webContents.send("gv:auth-error", (res && res.error) || "Sign-in failed.");
  }
}

/* ---------- the window ---------- */
function createWindow() {
  const state = win32.readWindowState();

  mainWindow = new BrowserWindow({
    ...state,
    minWidth: 940,
    minHeight: 620,
    show: false,
    backgroundColor: "#0d1a17",          // the studio's own ground, so no white flash
    title: "Green Vision",
    icon: path.join(__dirname, "build", "icon.ico"),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      // The real version, handed to the preload as an argument so it is
      // available synchronously and without an IPC round trip.
      // app.getVersion() is the packaged value; npm_package_version is only
      // ever set when npm launched the process - never true of an installed
      // app, so every shipped copy reported the same number.
      additionalArguments: ["--gv-version=" + app.getVersion()],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      spellcheck: false
    }
  });

  if (state.maximized) mainWindow.maximize();

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    flushDeepLink();
  });

  mainWindow.on("close", () => win32.saveWindowState(mainWindow));
  mainWindow.on("closed", () => { mainWindow = null; });

  /* Anything that is not the studio opens in the reader's own browser.
     A planner who clicks an OpenStreetMap credit should not lose their
     design behind a web page with no back button. */
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith("file://")) { e.preventDefault(); shell.openExternal(url); }
  });

  mainWindow.loadFile(path.join(APP_DIR, "index.html"));
  mainWindow.webContents.on("did-finish-load", flushDeepLink);

  Menu.setApplicationMenu(win32.buildMenu(mainWindow, { isDev }));
}

/* ---------- lifecycle ---------- */
app.whenReady().then(() => {
  /* Tell Windows which application this is.

     Without it the taskbar has no identity to attach the window to, so it
     falls back to a generic icon and groups the app under Electron rather
     than under Green Vision - the window had its icon set and the taskbar
     still showed a blank one, which is this and not the icon file.

     It must be the same string as the installer's appId, or the pinned
     shortcut and the running window are two different applications to the
     shell and pinning does not stick. That is read from the packaged
     package.json rather than written twice, because the two editions have
     different ids and hard-coding one here would silently mis-identify the
     other. */
  try {
    const id = (require("./package.json").build || {}).appId;
    if (id) app.setAppUserModelId(id);
  } catch (e) { /* unpackaged dev run: the shell identity does not matter */ }

  registerProtocol();
  accounts.init(app.getPath("userData"));
  auth.init({ accounts, onSignedIn: u => {
    if (mainWindow) mainWindow.webContents.send("gv:signed-in", u);
  }});
  createWindow();

  /* The engine starts BEHIND the window, never in front of it. Waiting on a
     cold index load before showing anything is how a fast app is made to
     feel broken, and the studio is fully usable while it comes up - only the
     map reads care. Failures are logged, not raised: no engine is a degraded
     app, not a broken one. */
  engine.start(app, m => console.log("[engine] " + m))
        .then(o => { if (o && mainWindow) mainWindow.webContents.send("gv:engine", o); })
        .catch(e => console.log("[engine] " + e.message));

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Windows delivers the protocol callback through argv on first launch.
handleDeepLink(process.argv.find(a => a.startsWith(PROTOCOL + "://")));

// macOS/Linux path, harmless on Windows and correct if this is ever ported.
app.on("open-url", (e, url) => { e.preventDefault(); handleDeepLink(url); });

app.on("window-all-closed", () => app.quit());

/* Never leave a Python process behind. The user closed the app; a 700 MB
   background interpreter they cannot see is not something to inherit. Only
   what we spawned is stopped - an engine we adopted was somebody else's. */
app.on("before-quit", () => engine.stop());
app.on("will-quit", () => engine.stop());
process.on("exit", () => engine.stop());

/* ---------- what the page may ask the desktop to do -------------------
   Deliberately small. The renderer is the studio, unmodified; these are
   the four things it genuinely cannot do inside a browser tab. */

// 1. Save a file where the reader wants it, not into Downloads.
ipcMain.handle("gv:save-file", async (_e, { name, data, filters }) => {
  const r = await dialog.showSaveDialog(mainWindow, {
    defaultPath: path.join(app.getPath("documents"), name || "green-vision.csv"),
    filters: filters || [{ name: "CSV", extensions: ["csv"] }]
  });
  if (r.canceled || !r.filePath) return { ok: false, canceled: true };
  await fs.promises.writeFile(r.filePath, data, "utf8");
  win32.addRecent(r.filePath);
  return { ok: true, path: r.filePath };
});

// 2. Show it in Explorer once written.
ipcMain.handle("gv:reveal", (_e, p) => { shell.showItemInFolder(p); return true; });

// 3. A real Windows notification when a long read finishes behind the app.
ipcMain.handle("gv:notify", (_e, { title, body }) => {
  if (!Notification.isSupported()) return false;
  new Notification({ title: title || "Green Vision", body: body || "" }).show();
  return true;
});

// 4. Sign-in, delegated to auth.js.
ipcMain.handle("gv:auth", (_e, msg) => auth.handle(msg));

/* 5. Where the machine actually is.

   Chromium's geolocation is dead in this build and cannot be revived
   honestly: it asks Google's network location service, that service needs
   an API key, and a key baked into a downloadable installer is the
   developer's key being spent by everybody who runs it. So
   `navigator.geolocation` here always fails with POSITION_UNAVAILABLE, and
   the fallback was the Cloudflare IP lookup - which returns the city the
   connection appears to come from. For a home broadband line in Ahmedabad
   that is the middle of Ahmedabad, kilometres from the reader, and "My
   location" put them somewhere they were not.

   Windows has its own location service, the one the Maps and Weather apps
   use: Wi-Fi and GNSS, permissioned in Windows Settings rather than by us,
   and no key. `System.Device.Location` is the .NET Framework face of it and
   is present on every supported Windows. Measured on the development
   machine: 135 m of accuracy against roughly 5 km for the IP lookup.

   It is read-only, it asks for nothing but a coordinate, and if location is
   switched off in Windows it says so and the app falls back exactly as
   before. */
const PS_LOCATE = [
  "$ErrorActionPreference='Stop'",
  "try {",
  "  Add-Type -AssemblyName System.Device",
  "  $w = New-Object System.Device.Location.GeoCoordinateWatcher('Default')",
  "  $w.Start()",
  "  $n = 0",
  "  while ($w.Status -ne 'Ready' -and $n -lt 24) { Start-Sleep -Milliseconds 400; $n++ }",
  "  $l = $w.Position.Location",
  "  if ($l -and -not $l.IsUnknown) {",
  "    Write-Output ('OK ' + $l.Latitude + ' ' + $l.Longitude + ' ' + $l.HorizontalAccuracy)",
  "  } else {",
  "    Write-Output ('NO ' + $w.Permission + ' ' + $w.Status)",
  "  }",
  "  $w.Stop()",
  "} catch { Write-Output ('ERR ' + $_.Exception.Message) }"
].join("; ");

ipcMain.handle("gv:locate", () => new Promise(resolve => {
  /* Windows only, and honest about it.

     System.Device.Location is a .NET Framework face onto the Windows
     location service; there is no powershell.exe on a Mac and nothing
     behind this on one. Saying so lets the page fall through to its next
     source rather than waiting out a twelve-second timeout for a command
     that was never going to run. */
  if (process.platform !== "win32") {
    return resolve({ ok: false, reason: "unsupported" });
  }
  const { execFile } = require("child_process");
  let done = false;
  const finish = v => { if (!done) { done = true; resolve(v); } };
  /* Bounded. A location fix indoors can take a while and sometimes never
     arrives; past twelve seconds the reader is better served by the
     approximate answer than by a button that is still thinking. */
  const timer = setTimeout(() => { try { child.kill(); } catch (e) {} 
                                   finish({ ok: false, reason: "timeout" }); }, 12000);
  const child = execFile("powershell.exe",
    ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", PS_LOCATE],
    { windowsHide: true, timeout: 12000 },
    (err, stdout) => {
      clearTimeout(timer);
      if (err && !stdout) return finish({ ok: false, reason: "unavailable" });
      const m = String(stdout || "").match(/OK\s+(-?[\d.]+)\s+(-?[\d.]+)\s+([\d.]+)?/);
      if (!m) {
        const denied = /NO\s+Denied/i.test(String(stdout || ""));
        return finish({ ok: false, reason: denied ? "denied" : "unavailable" });
      }
      const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
      if (!isFinite(lat) || !isFinite(lon)) return finish({ ok: false, reason: "unavailable" });
      finish({ ok: true, lat, lon,
               accuracy_m: isFinite(parseFloat(m[3])) ? Math.round(parseFloat(m[3])) : null,
               source: "windows" });
    });
}));

/* An ordinary quit, not a kill: app.quit() runs the before-quit handlers, so
   the bundled Python engine is stopped rather than left behind as an orphan
   process holding its port. */
ipcMain.handle("gv:quit", () => { app.quit(); return true; });

/* Street view, and anything else the studio wants to SHOW rather than hand
   off. Without this the renderer's window.open went to setWindowOpenHandler,
   which denies the window and calls shell.openExternal - so clicking
   "Street view" inside a desktop app quit to the user's browser, or, if the
   browser was slow to appear, looked like the button did nothing at all.
   The one thing it must not become is a way to navigate the app itself, so
   it opens a SEPARATE window with the studio's preload absent: no bridge, no
   node integration, nothing of ours reachable from a Google page. */
const viewerWindows = new Map();
ipcMain.handle("gv:open-window", (_e, { url, title, width, height }) => {
  let u;
  try { u = new URL(String(url)); } catch { return false; }
  if (u.protocol !== "https:" && u.protocol !== "http:") return false;

  const key = title || u.href;
  const existing = viewerWindows.get(key);
  if (existing && !existing.isDestroyed()) {
    existing.loadURL(u.href);
    existing.focus();
    return true;
  }

  const w = new BrowserWindow({
    width: width || 1000, height: height || 700,
    parent: mainWindow || undefined,
    title: title || "Green Vision",
    autoHideMenuBar: true,
    backgroundColor: "#0c1512",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true }
  });
  w.setMenuBarVisibility(false);
  w.loadURL(u.href);
  // Links inside the viewer go to the real browser; the viewer is for looking
  // at one thing, not for browsing the web inside our app.
  w.webContents.setWindowOpenHandler(({ url: nu }) => { shell.openExternal(nu); return { action: "deny" }; });
  w.on("closed", () => viewerWindows.delete(key));
  viewerWindows.set(key, w);
  return true;
});
