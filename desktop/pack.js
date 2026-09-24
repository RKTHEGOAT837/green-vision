/* =====================================================================
   Package Green Vision for Windows without electron-builder
   =====================================================================
   electron-builder insists on unpacking its code-signing bundle, which
   contains macOS symlinks. Creating a symlink on Windows needs either
   administrator rights or Developer Mode, and on a stock machine the
   build stops there — on a step we do not use, because nothing here is
   code-signed.

   An Electron app does not need any of that to be distributable. It is
   the runtime directory with the app's own code in resources/app. This
   assembles exactly that:

       release/GreenVision-win32-x64/
         GreenVision.exe          the Electron runtime, renamed
         resources/app/           main, preload, auth, accounts, windows
         resources/studio/        the baked studio bundle (dist_app)
         ...the Electron runtime files

   Renaming the executable is what makes app.isPackaged true, which is
   what switches main.js from the repo's dist_app to resources/studio.

   Run: node pack.js
   Then: release/GreenVision-win32-x64/GreenVision.exe
   ===================================================================== */

"use strict";

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const ROOT = __dirname;
const OUT = path.join(ROOT, "release", "GreenVision-win32-x64");
const ELECTRON = path.join(ROOT, "node_modules", "electron", "dist");
const STUDIO_SRC = path.join(ROOT, "..", "dist_app");

const APP_FILES = ["main.js", "preload.js", "auth.js", "accounts.js", "windows.js", "engine.js", "config.json"];

function rmrf(p) { fs.rmSync(p, { recursive: true, force: true }); }
function copyDir(from, to) { fs.cpSync(from, to, { recursive: true }); }

function main() {
  if (!fs.existsSync(ELECTRON)) {
    console.error("Electron runtime missing. Run: npm install");
    process.exit(1);
  }
  if (!fs.existsSync(path.join(STUDIO_SRC, "index.html"))) {
    console.error("dist_app/index.html missing. Build the studio bundle first.");
    process.exit(1);
  }

  console.log("Packaging Green Vision …");
  rmrf(OUT);
  fs.mkdirSync(OUT, { recursive: true });

  // 1. the runtime
  copyDir(ELECTRON, OUT);
  // The default app that ships with the runtime would otherwise shadow ours.
  rmrf(path.join(OUT, "resources", "default_app.asar"));

  // 2. our code
  const appDir = path.join(OUT, "resources", "app");
  fs.mkdirSync(appDir, { recursive: true });
  for (const f of APP_FILES) fs.copyFileSync(path.join(ROOT, f), path.join(appDir, f));
  copyDir(path.join(ROOT, "build"), path.join(appDir, "build"));

  /* A trimmed manifest. The build block and devDependencies describe how
     to package, not how to run, and shipping them would only mislead
     anyone who opened the file. */
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  /* appId travels with the package.

     main.js hands it to app.setAppUserModelId() so Windows knows which
     application the window belongs to. Without that the taskbar has no
     identity to attach the window to: it shows a generic icon and groups
     the app under Electron, however good the icon file is. It comes from
     here rather than being written into main.js because the two editions
     have different ids, and hard-coding one would mis-identify the other. */
  fs.writeFileSync(path.join(appDir, "package.json"), JSON.stringify({
    name: pkg.name, productName: pkg.productName, version: pkg.version,
    description: pkg.description, author: pkg.author, main: pkg.main,
    appId: (pkg.build || {}).appId || null
  }, null, 2) + "\n", "utf8");

  // 3. the studio
  copyDir(STUDIO_SRC, path.join(OUT, "resources", "studio"));

  /* 3b. the engine: a complete CPython plus the map index, so a colleague
     who has never installed Python still gets local map reads. Without it
     the Traffic tab depends on a public Overpass instance that is simply
     unreachable on some networks. It is optional at BUILD time - a developer
     iterating on the UI should not have to wait on a 175 MB copy - but the
     installer is not worth shipping without it, so say so loudly. */
  const engineSrc = path.join(ROOT, "engine");
  if (fs.existsSync(path.join(engineSrc, "python", "python.exe"))) {
    copyDir(engineSrc, path.join(OUT, "resources", "engine"));
    console.log("  engine bundled (" + (dirSize(engineSrc) / 1048576).toFixed(0) + " MB)");
  } else {
    console.warn("  NO ENGINE BUNDLED - run: python scripts/build_portable_engine.py");
    console.warn("  (the app will still run, but map reads fall back to public Overpass)");
  }

  // 4. rename, which is also what sets app.isPackaged
  const exe = path.join(OUT, "GreenVision.exe");
  fs.renameSync(path.join(OUT, "electron.exe"), exe);

  /* 5. Stamp the icon and version strings onto the exe. rcedit ships in
     electron-builder's cache; if it is not there the app still runs and
     simply wears the Electron icon, so this is a warning, not a failure. */
  const rcedit = findRcedit();
  if (rcedit) {
    try {
      execFileSync(rcedit, [exe,
        "--set-icon", path.join(ROOT, "build", "icon.ico"),
        "--set-file-version", pkg.version,
        "--set-product-version", pkg.version,
        "--set-version-string", "ProductName", "Green Vision",
        "--set-version-string", "FileDescription", pkg.description,
        "--set-version-string", "CompanyName", "Green Vision",
        "--set-version-string", "LegalCopyright", "Green Vision",
        "--set-version-string", "OriginalFilename", "GreenVision.exe"
      ], { stdio: "pipe" });
      console.log("  icon and version strings applied");
    } catch (e) {
      console.warn("  rcedit failed (" + e.message.split("\n")[0] + ") — the app runs, with the Electron icon");
    }
  } else {
    console.warn("  rcedit not found — the app runs, with the Electron icon");
  }

  const size = dirSize(OUT) / (1024 * 1024);
  console.log("\nPackaged: " + OUT);
  console.log("  " + size.toFixed(0) + " MB");
  console.log("  run: " + path.relative(process.cwd(), exe));
}

function findRcedit() {
  const cache = path.join(process.env.LOCALAPPDATA || "", "electron-builder", "Cache", "winCodeSign");
  if (!fs.existsSync(cache)) return null;
  for (const d of fs.readdirSync(cache)) {
    const p = path.join(cache, d, "rcedit-x64.exe");
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function dirSize(p) {
  let n = 0;
  for (const e of fs.readdirSync(p, { withFileTypes: true })) {
    const q = path.join(p, e.name);
    n += e.isDirectory() ? dirSize(q) : fs.statSync(q).size;
  }
  return n;
}

main();
