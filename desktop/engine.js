/* =====================================================================
   The bundled Green Vision engine
   =====================================================================
   The app opens and works without this. What it does NOT do without it is
   answer map questions from this machine: the 100 km2 feature census and the
   whole Traffic tab read OpenStreetMap, and with no engine those reads go to
   the public Overpass instance. On a network that cannot reach it — measured
   on the development machine as an 84-second connect timeout, not a rate
   limit — they never complete, and the Traffic tab spins until it gives up.

   So the installer now carries a complete engine: a CPython embeddable build
   with numpy, pandas, h3, yaml and requests, the greenplan package, the city
   data, and a 75 MB index of 2.6 million map features. This module is what
   starts it, finds it, and stops it.

   Three rules it follows:

   DO NOT START A SECOND ONE. Somebody running the repo's own server, or a
   second copy of the app, already has one listening. Probing first and
   adopting what is there costs one HTTP request and avoids two processes
   fighting over the same port and the same 700 MB of loaded index.

   DO NOT BLOCK THE WINDOW. The index loads lazily on the first map query and
   takes about half a minute; making the window wait on that would turn a
   fast app into a slow one for a feature the user may not open. The window
   comes up immediately and the engine arrives behind it.

   DO NOT LEAVE IT RUNNING. A background Python process the user cannot see
   and did not start is not acceptable, so anything this module spawned is
   killed on quit. An engine it merely ADOPTED is left alone: it was not ours
   to stop.
   ===================================================================== */

"use strict";

const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

// The same ports the PowerShell launchers try, in the same order, so the app
// and the shortcuts cannot disagree about where an engine would be.
const PORTS = [8000, 8010, 8020, 8030, 8040];

let child = null;          // only set when WE spawned it
let origin = null;         // the engine we are using, ours or adopted
let starting = false;

/* Where the bundle is. Packaged: resources/engine beside the binary.
   In development: desktop/engine, exactly what pack.js will ship. */
function engineDir(app) {
  return app.isPackaged
    ? path.join(process.resourcesPath, "engine")
    : path.join(__dirname, "engine");
}

/* The interpreter inside the bundle.

   Windows ships an embeddable CPython, whose executable sits at the root of
   the folder. macOS has no embeddable build, so the mac bundle carries a
   relocatable venv instead, and a venv keeps its interpreter in bin/ under
   a different name. Hard-coding python.exe meant a mac build could never
   find its own engine: isBundled() would return false, nothing would start,
   and the app would open to a studio with no analysis behind it. */
function pythonExe(dir) {
  return process.platform === "win32"
    ? path.join(dir, "python", "python.exe")
    : path.join(dir, "python", "bin", "python3");
}

function isBundled(app) {
  try { return fs.existsSync(pythonExe(engineDir(app))); } catch { return false; }
}

/* A HEAD-weight health probe with a short deadline. Used both to find an
   engine already running and to wait for the one we start. */
function probe(port, ms = 1500) {
  return new Promise(resolve => {
    const req = http.get(
      { host: "127.0.0.1", port, path: "/api/health", timeout: ms },
      res => {
        if (res.statusCode !== 200) { res.resume(); return resolve(false); }
        let body = "";
        res.setEncoding("utf8");
        res.on("data", d => { if (body.length < 4096) body += d; });
        res.on("end", () => { try { resolve(!!JSON.parse(body).ok); } catch { resolve(false); } });
      }
    );
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

async function findRunning() {
  for (const p of PORTS) if (await probe(p)) return "http://127.0.0.1:" + p;
  return null;
}

/* Start the engine, unless one is already there.

   Returns the origin in use, or null when there is no bundle to start —
   which is a normal state, not an error: the app still opens, the studio
   still works from its baked data, and map reads fall back to the public
   mirrors. */
async function start(app, log = () => {}) {
  if (origin || starting) return origin;
  starting = true;
  try {
    const adopted = await findRunning();
    if (adopted) {
      origin = adopted;
      log("engine already running at " + adopted + " — adopted, not started");
      /* Warm it too. An adopted engine has answered /api/health, which says
         nothing about whether the index is loaded - health deliberately does
         not load it. A fresh engine started by a terminal a second ago is
         exactly as cold as one we started ourselves, and skipping the
         warm-up here would leave the slow first read in place for anyone
         running the app twice. */
      warmIndex(origin, log);
      return origin;
    }

    const dir = engineDir(app);
    const exe = pythonExe(dir);
    if (!fs.existsSync(exe)) {
      log("no bundled engine at " + exe + " — map reads will use public mirrors");
      return null;
    }

    // First port with nothing on it. We already know none of them answered
    // /api/health, but something unrelated could still hold one.
    const port = PORTS[0];

    /* PYTHONNOUSERSITE matters here as much as it did at build time. If the
       person running this happens to have their own Python with a different
       numpy, the embeddable interpreter would pick their user site-packages
       up and import a version this engine was never tested against. The
       bundle is self-contained and must stay that way. */
    const env = Object.assign({}, process.env, {
      PYTHONNOUSERSITE: "1",
      PYTHONDONTWRITEBYTECODE: "1"
    });
    delete env.PYTHONPATH;
    delete env.PYTHONHOME;

    child = spawn(exe, ["-m", "greenplan.server", "--port", String(port)], {
      cwd: dir,
      env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"]
    });
    log("starting bundled engine on port " + port);

    child.on("error", e => log("engine failed to start: " + e.message));
    child.on("exit", code => {
      if (child) log("engine exited with code " + code);
      child = null; origin = null;
    });
    // Keep the last lines, so a failure can be reported rather than guessed.
    const tail = [];
    const keep = d => {
      tail.push(String(d).trimEnd());
      while (tail.length > 40) tail.shift();
    };
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    module.exports.tail = () => tail.join("\n");

    /* Wait for it, but in the background. 90 seconds is a cold start with
       the city data; past that something is wrong and the app carries on
       with public mirrors rather than pretending. */
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      if (!child) break;                       // it died; the exit handler said so
      if (await probe(port, 1200)) {
        origin = "http://127.0.0.1:" + port;
        log("engine ready at " + origin);
        warmIndex(origin, log);          // deliberately not awaited
        return origin;
      }
      await new Promise(r => setTimeout(r, 700));
    }
    if (!origin) log("engine did not answer within 90s\n" + tail.slice(-8).join("\n"));
    return origin;
  } finally {
    starting = false;
  }
}

/* Force the map index to load, now, instead of on somebody's first click.

   /api/health deliberately does not load the index - it answers in
   milliseconds and says "present; loads on the first map query". That is
   honest, and it means the FIRST real query pays the whole load: a gigabyte
   of gzipped JSONL parsed into memory. While that is happening the query
   that triggered it is simply waiting, and the app - having no way to tell
   "loading" from "broken" - failed over to the public Overpass instance,
   which allows two requests per IP and throttles the rest. That is the ten
   minutes of "surroundings unavailable" before everything suddenly works:
   not a hang, a cold index plus a rate-limited fallback.

   So the load is started the moment the engine answers, in the background,
   while the reader is still looking at the splash. The query is the
   cheapest thing that forces it - one tiny bbox - and its result is thrown
   away; what matters is the side effect.

   Failure here is not reported as an error. If the warm-up cannot run, the
   app behaves exactly as it did before: the first query loads the index. */
function warmIndex(origin, log) {
  const started = Date.now();
  const body = "data=" + encodeURIComponent(
    "[out:json][timeout:60];(way(23.036,72.508,23.038,72.510)[building];);out count;");
  const req = http.request(origin + "/api/osm", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded",
               "content-length": Buffer.byteLength(body) },
    timeout: 300000          // a cold index on a slow disk is minutes, not seconds
  }, res => {
    res.resume();            // drain, we only wanted the side effect
    res.on("end", () => log("map index warm in " +
      Math.round((Date.now() - started) / 1000) + "s — the first map read will be instant"));
  });
  req.on("error", () => { /* the first query will load it, as before */ });
  req.on("timeout", () => { try { req.destroy(); } catch (e) {} });
  req.end(body);
}

/* Stop only what we started. An adopted engine belongs to whoever started
   it — very possibly a terminal the user is watching. */
function stop() {
  if (!child) return;
  const c = child;
  child = null;
  origin = null;
  try { c.kill(); } catch { /* already gone */ }
}

module.exports = { start, stop, isBundled, engineDir, PORTS,
                   get origin() { return origin; },
                   tail: () => "" };
