/* =====================================================================
   Build both editions of Green Vision
   =====================================================================
   One codebase, two installers:

     open      the product as designed. The map, the analysis and the
               costing all work with no account; signing in adds saved
               projects, assistant memory and the shared Library. This is
               the build to keep and to demonstrate with.

     managed   the same app with an account required for anything at all.
               This is the build to send to colleagues and reviewers,
               because it is the one where withdrawing somebody's account
               actually withdraws their access.

   Why two builds and not one with a switch. Everything the app computes
   runs from an engine and a map index that ship inside the installer, on
   the reader's own disk - none of it consults a server to draw a map or
   price a plan. So in the open edition, revoking an account takes away
   saved projects and the Library and leaves a complete working planner
   behind. That is a perfectly reasonable product; it is just not what
   "blocking access" means. The gate has to be in the build.

   The two are separate applications to Windows - different appId,
   different install directory, different user-data directory - so both
   can be installed side by side without one overwriting the other or
   inheriting the other's signed-in session. That is deliberate: the
   author needs to run the open build while testing what a colleague sees
   in the managed one.

   Usage:  node build-editions.js [open|managed|both]
   ===================================================================== */

"use strict";

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const HERE = __dirname;
const CONFIG = path.join(HERE, "config.json");
const PKG = path.join(HERE, "package.json");

const version = JSON.parse(fs.readFileSync(PKG, "utf8")).version;

const EDITIONS = {
  open: {
    requireSignIn: false,
    appId: "in.greenvision.studio",
    productName: "Green Vision",
    artifact: "GreenVision-${version}-x64.exe"
  },
  managed: {
    requireSignIn: true,
    /* A different appId is what makes Windows treat this as a separate
       application rather than an upgrade of the other one. Same for the
       product name, which decides the install folder, the Start menu entry
       and - importantly - the user-data directory holding the session. */
    appId: "in.greenvision.studio.managed",
    productName: "Green Vision Managed",
    artifact: "GreenVision-Managed-${version}-x64.exe"
  }
};

/* Refuse to start from a dirty working copy.
 *
 * `finally` restores package.json after every build - but a run that is
 * KILLED does not reach it, and that happened: a crashed build left the file
 * holding the managed edition's appId, product name and artifact name, and
 * every build afterwards dutifully "restored" that dirty state. The builds
 * themselves stayed correct, because each one overwrites those fields for
 * the edition it is making; what rotted was the resting state, so a plain
 * `npm run dist` would have produced a MANAGED app under the open name.
 *
 * Checking costs nothing and turns a silent mix-up into a message. */
function assertClean() {
  const b = JSON.parse(fs.readFileSync(PKG, "utf8")).build;
  const o = EDITIONS.open;
  const wrong = [];
  if (b.appId !== o.appId) wrong.push("appId=" + b.appId);
  if (b.productName !== o.productName) wrong.push("productName=" + b.productName);
  if (b.win.artifactName !== "GreenVision-${version}-${arch}.${ext}")
    wrong.push("artifactName=" + b.win.artifactName);
  /* Without this every shortcut points at a file that does not exist: pack.js
     renames the binary to GreenVision.exe, and electron-builder would
     otherwise derive "Green Vision.exe" from productName. */
  if (b.executableName !== "GreenVision")
    wrong.push("executableName=" + b.executableName);
  if (wrong.length) {
    console.error(
      "\npackage.json is not in its resting state - a previous build was" +
      "\ninterrupted before it could restore it:\n  " + wrong.join("\n  ") +
      "\n\nRestore it (git checkout desktop/package.json) and run again.\n");
    process.exit(1);
  }
  const c = JSON.parse(fs.readFileSync(CONFIG, "utf8"));
  if (c.requireSignIn === true) {
    console.error("\nconfig.json still has requireSignIn: true from an" +
                  "\ninterrupted build. Restore it and run again.\n");
    process.exit(1);
  }
}

function build(name) {
  const e = EDITIONS[name];
  const original = fs.readFileSync(CONFIG, "utf8");
  const originalPkg = fs.readFileSync(PKG, "utf8");
  const cfg = JSON.parse(original);

  console.log("\n=== " + name + " edition " + version + " ===");
  try {
    /* The flag is written into the file that gets packaged, not passed as
       an environment variable, because a packaged Windows binary has no
       shell environment to read. What is on disk at pack time is what
       ships. */
    cfg.requireSignIn = e.requireSignIn;
    cfg._edition = name;
    fs.writeFileSync(CONFIG, JSON.stringify(cfg, null, 2) + "\n", "utf8");

    /* The build identity is edited into package.json rather than passed on
       the command line. `-c.appId=...` style overrides were tried first and
       electron-builder 25 ignored them silently: it kept reading the "build"
       block out of package.json and wrote the OPEN edition's filename both
       times, so the managed installer never existed while the build still
       reported success. Editing the file it actually reads is the version
       that works. Restored in `finally` below. */
    const pkg = JSON.parse(originalPkg);
    pkg.build.appId = e.appId;
    pkg.build.productName = e.productName;
    /* Under `win`, not at the top level. A top-level artifactName is
       overridden by the per-platform one, which this project already sets -
       so setting only the top-level key changed nothing and both editions
       came out under the open edition's filename. */
    pkg.build.win.artifactName = e.artifact;
    /* What Windows calls it once installed: the desktop shortcut and the
       entry in Add/Remove Programs. Both editions carrying the name "Green
       Vision" would leave two identical shortcuts and no way to tell which
       one is gated. */
    pkg.build.nsis.shortcutName = e.productName;
    pkg.build.nsis.uninstallDisplayName = e.productName;
    fs.writeFileSync(PKG, JSON.stringify(pkg, null, 2) + "\n", "utf8");

    execFileSync(process.execPath, [path.join(HERE, "pack.js")],
                 { cwd: HERE, stdio: "inherit" });

    /* electron-builder's own entry point, run by node.

       Not `npx`, and not a shell. Going through cmd meant cmd re-parsed the
       arguments and split "Green Vision" on its space; quoting it back only
       moved the failure to the parentheses in the managed product name,
       which cmd reads as grouping. Dropping `shell: true` then hit a
       different wall - Node 24 refuses to spawn a .cmd file without a shell
       at all (EINVAL), which is a deliberate hardening against argument
       injection through those shims.

       Resolving the package's real cli.js and handing it to this same node
       binary sidesteps both: one process, a real argument vector, and no
       interpreter in between to re-split anything. */
    const builderCli = path.join(
      path.dirname(require.resolve("electron-builder/package.json")), "cli.js");
    execFileSync(process.execPath, [
      builderCli, "--win", "nsis",
      "--prepackaged", path.join("release", "GreenVision-win32-x64")
    ], { cwd: HERE, stdio: "inherit" });
  } finally {
    /* Always put the working copy back to the open edition, even if the
       build threw. Leaving `requireSignIn: true` behind would mean the next
       ordinary `npm run dist` silently produced a gated build under the
       ungated name - which is exactly the sort of mix-up that ends with the
       wrong installer on a download page. */
    fs.writeFileSync(CONFIG, original, "utf8");
    fs.writeFileSync(PKG, originalPkg, "utf8");
  }

  const out = path.join(HERE, "release",
                        e.artifact.replace("${version}", version));
  const size = fs.existsSync(out) ? fs.statSync(out).size : 0;
  console.log("  -> " + path.basename(out) + "  " +
              (size / 1048576).toFixed(1) + " MB");
  return out;
}

const which = (process.argv[2] || "both").toLowerCase();
const list = which === "both" ? ["open", "managed"] : [which];
for (const n of list) {
  if (!EDITIONS[n]) { console.error("Unknown edition: " + n); process.exit(1); }
}
assertClean();
const made = list.map(build);
console.log("\nBuilt:");
for (const f of made) console.log("  " + f);
