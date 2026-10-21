# Green Vision: context for agents editing this repo

Green Vision is an urban tree-planting planner for Indian cities. It has three parts:

- a **studio**: a single-page map app (`index.html`);
- a local **Python engine** (`greenplan/`) with on-device AI on Intel OpenVINO;
- an **Electron desktop shell** (`desktop/`).

It ships to:

- **Windows:** NSIS installer;
- **macOS:** beta zip, built only by CI;
- **web:** GitHub Pages, a taster version without the engine.

Owner: Rishabh Khara (signs in with kaushal@drivingcomfort.in on this PC). Current release: **1.6.2** (2026-10-21). Latest studio patch: **1.9.30**.

## Standing rules from the owner (do not break)

- **Never send email.** Updates go to users inside the app (notice and patch channel). If an email is needed, make a draft and save it; the owner presses Send.
- **Never sign in, create accounts or enter passwords or credentials** for the owner. Never print or commit API keys, tokens or secrets. Never create or handle the owner's GitHub or Cloudflare tokens.
- **Test the installed desktop app on this computer, not only the web version.** Drive it over CDP (see Testing).
- **If the admin portal is open and signed in, do not navigate that tab away.** Work in another tab.
- **Publishing third-party recordings or reviews** needs the owner's confirmation that the person consented.
- **Keep the green view's original colours.** The layout and logic may change.
- **Every figure and every AI sentence names its source, including the model name.** For example: "AI-written. Model: Qwen 2.5 1.5B Instruct … run by Intel OpenVINO on …". Every AI feature has a deterministic fallback that says it is not AI-written.
- **Report outcomes honestly,** with measured numbers. Do not invent features or results in comments or release notes.

## Repo layout (this folder is the git repo `RKTHEGOAT837/green-vision`)

| Path | What it is |
|---|---|
| `index.html` | **The studio.** About 25k lines, one file and two `<script>` blocks: the "host app" (map, `analyse()`, tiles, Overpass) and the "Studio module" (closure: `GV.*`, 3D builder, review, AI). Code in one block cannot see `let`/`const` names in the other; cross over via `window.*` or `GV.*`. The comment style is long "why" comments; match it. |
| `greenplan/` | Python engine. `server.py` is the HTTP server. `osmlocal.py` is the local Overpass subset and **cell pack**. `reasoning/` holds AI and the assistant (`ai_tools.py`, `llm_assistant.py`, `client.py`). `engine.py` is training and ranking. |
| `desktop/` | Electron shell. `main.js` handles windows and IPC (`backgroundThrottling:false`). `engine.js` starts or adopts the engine on ports 8000/8010/8020/8030/8040, plus the long-path junction fix. `patch.js` is the studio patch channel. `preload.js` exposes `window.__GV_DESKTOP__`. `pack.js` packs the app; `package.json` holds the version. |
| `desktop/engine/` | Portable engine bundle: embeddable CPython 3.13 with deps, a copy of `greenplan/` (synced by `pack.js`), `data/osm/` (index, Overture buildings, cell pack) and models. Built by `scripts/build_portable_engine.py`. |
| `scripts/build_static.py` | Bakes the studio. `--out dist` is the web build (copy to `docs/` for Pages). `--out dist_app --keep-local-osm` is the desktop studio. It also inlines the `vendor/` libraries (Leaflet, three.js, fonts). |
| `scripts/make_patch.py` | Builds a studio patch into `C:\gvrel\docs\patch\studio-<v>.html` (the separate Pages repo `green-vision-releases`). |
| `scripts/install-mac.sh`, `scripts/update-studio-mac.sh`, `scripts/install-windows.ps1` | One-command installers and updaters. |
| `worker/` | Cloudflare Worker API (`green-vision-api.greenvision-rk.workers.dev`): accounts, reviews, notice, patch, admin. D1 database `green-vision`, table `kv(k, v)`. |
| `docs/` | GitHub Pages web build: https://rkthegoat837.github.io/green-vision/ |
| `.github/workflows/mac-beta.yml` | The only way to build macOS. Manual dispatch with inputs `version` and `with_model`; publishes `mac-v<version>`. |

Ignored and large, so never commit them: `data/osm/*.jsonl.gz`, `*.pbf`, `*.pack`, `dist_app/`, `dist_patch/`, `desktop/release/`.

**Python:** always use the bundled `desktop/engine/python/python.exe` for build scripts; the system Python lacks pandas. That interpreter ignores the current directory (`._pth`), so to run the repo's engine code use a runpy launcher that adds `os.getcwd()` to `sys.path`. Otherwise you silently run the installed code.

## Performance architecture (1.6.2, do not regress)

### Cell pack: `greenplan/osmlocal.py`

- **What it is:** `index.cells.pack` holds the map index, stored per 0.01° cell with each cell zlib+marshal compressed and a table of offsets.
- **Speed:** it opens in 0.01 s and a query takes about 20 ms. The old loader parsed 2.6M JSON lines plus 3.2M Overture buildings at every start: over 5 minutes and about 10 GB of RAM.
- **Validity:** a pack is used only if its recorded source sizes match `index.slim.jsonl.gz` and `buildings.overture.jsonl.gz`. Otherwise one is built into `%LOCALAPPDATA%\Green Vision\engine\cache` with bounded memory (about 4 minutes, once).
- **Rebuild:** `python -c "from greenplan import osmlocal; from pathlib import Path; osmlocal.build_pack(Path('data/osm/index.slim.jsonl.gz'), Path('data/osm/index.cells.pack'))"`, then copy it to `desktop/engine/data/osm/`. `pack.js` refuses to package without it.

### Engine boot: `server.py`

- A stdlib-only **boot listener** binds the port before the heavy imports (numpy, pandas and OpenVINO take 6–11 s).
- It answers `/api/ping` and `/api/osm` at once and returns 503 for everything else.
- `main()` then swaps in the real handler on the same socket (`httpd.RequestHandlerClass = …`).
- The boot city builds on a background thread. `/api/health` blocks until the city is ready; `/api/ping` never blocks.

### Engine discovery (studio `aProbe`)

- Pings all candidate ports **in parallel** (`Promise.any`) and records the origin from `/api/ping`, so local maps work before the city is ready.
- It then uses `/api/health`; older engines that lack `/api/ping` fall back to health.
- Desktop retries every second for 90 s.

### Satellite tiles (`loadTile`)

- At most 5 in flight.
- One request per tile, deduplicated.
- `{urgent:true}` jumps the queue.
- Failed tiles are not cached.

### Site finder (`findOpenPlot`)

- Reads a small 450 m imagery buffer at the same zoom first, and the full radius only if a candidate lies outside it.

### 3D builder

- Renders on demand: a camera/objects signature, input wake-ups, `B.invalidate()`, and a 4 fps floor.
- Disposes the WebGL context on close.
- `b3dTimeSlider` paint calls `B.invalidate()`.

### Web fallback

- When every Overpass mirror is down, `gvImageryContext` estimates buildings and trees from the satellite photo, clearly labelled as estimates.
- The web gives mirrors 6 s before drawing the estimate and swaps in mapped data if it arrives later.
- The web site check waits 10 s before going imagery-only.

### Long install paths (Windows)

- DLL paths over about 248 characters fail with error 206.
- `desktop/engine.js` runs the engine through a junction at `%APPDATA%\Green Vision\e` and copies `openvino_tokenizers.dll` to `%APPDATA%\Green Vision\ovt`.
- It points both `OV_TOKENIZER_PREBUILD_EXTENSION_PATH` and `OPENVINO_TOKENIZERS_PATH_GENAI` at that copy.
- This is committed after 1.6.2 and ships with the **next installer**.

Measured on the owner's laptop in 1.6.2:

| | Result |
|---|---|
| Engine port open | 0.6 s (dev) / about 3 s (packaged) |
| Local maps | about 1 s |
| City ready | 13 s |
| "Real surroundings" | 1.3–3 s |
| Point report | 3–6 s |

## On-device AI (OpenVINO GenAI 2026.4)

- **Models:**
  - Writing: Qwen2.5-1.5B INT4 (bundled), on the GPU.
  - Voice: Whisper-small INT8 (downloaded on demand).
  - Photos: InternVL2-1B INT4 (downloaded on demand).
  - Downloads go to `%LOCALAPPDATA%\Green Vision\engine\models\media` with SHA-256 checks and a `.complete` marker.
- **Installer size:** NSIS cannot build an installer over 2 GB. 1.6.2 is 1.66 GB, which is why the media models are not bundled.
- **Guardrails for the 1.5B writer:** it cannot reason reliably. Compute every judgement in JS and send 3–6 short facts with comparisons already stated in words. Use one worked example per task (`EXAMPLES` in `ai_tools.py`). The engine rejects ungrounded numbers, copied example names and off-topic replies.
- **First photo check:** it compiles the vision model and can take more than 120 s, so the studio waits up to 300 s.

## Releasing

### 1. Studio-only change (`index.html`): patch channel, no installer

1. Delete `dist_patch/` first, then run `desktop/engine/python/python.exe scripts/make_patch.py --version 1.9.NN --min-app 1.4.1 --notes "…"`.
2. In `C:\gvrel`: `git add docs/patch/studio-1.9.NN.html`, then commit and push.
3. **Verify** that the served file hashes to the manifest sha256 before publishing: `curl` the `https://rkthegoat837.github.io/green-vision-releases/patch/...` URL and pipe it to `sha256sum`.
4. Publish the D1 key `patch`. Wrangler is logged in on this PC:
   `cd worker && npx wrangler@3.114.14 d1 execute green-vision --remote --file=x.sql`
   with `INSERT INTO kv (k, v) VALUES ('patch', '{json}') ON CONFLICT(k) DO UPDATE SET v = excluded.v;`
   The admin portal's Patch form also works if the owner is signed in.
5. Check `GET /app/patch?v=1.6.1&platform=windows` (and `darwin`).

Patches apply on the next start (download) and the start after (promote). 1.6.1+ `patch.js` never stages an older version.

### 2. Engine or shell change: installer

1. Bump `desktop/package.json`.
2. Run `build_static.py --out dist_app --keep-local-osm`, then `cd desktop && npm run dist`. The output is `desktop/release/GreenVision-<v>-x64.exe`.
3. Upload it: `gh release create v<v> <exe> --repo RKTHEGOAT837/green-vision-releases`. Check the asset `digest` against your local `sha256sum`.
4. Push the main repo, then dispatch the Mac build: `gh workflow run mac-beta.yml -f version=<v> -f with_model=true`. CI builds from GitHub, so push first.
5. Make it required: update D1 key `app:notice` with `latest`, `min`, `latest_mac`, `min_mac`, `file`, `file_mac`, `sha256`, `size`, `title`, `title_other` and `message`. Read the current value first and keep its other fields.

### 3. Web version

1. Run `build_static.py --out dist`.
2. `cp -r dist/. docs/`, then commit and push. Pages serves it in about a minute.

### Update and install commands

- **Windows install or update** (skips the download if the newest app is installed, then stages the newest studio patch; `GV_FORCE=1` reinstalls): `powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/install-windows.ps1 | iex"`
- **Mac install or update** (same behaviour; `bash -s -- --force` reinstalls): `curl -fsSL https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/install-mac.sh | bash`
- **Mac studio update now:** `curl -fsSL https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/update-studio-mac.sh | bash`

## Testing

- **Installed app over CDP:** `cmd //c start "" "C:\Program Files\GreenVision\GreenVision.exe" --remote-debugging-port=9440`. Launch it with `cmd start`; a PowerShell-launched app dies with the command. Drive it with `Runtime.evaluate` through a small Node CDP script. Close every `GreenVision.exe` and `python.exe` engine first, or two engines share port 8000.
- **Locked laptop screen:** the window counts as hidden. 1.6.2 disables throttling, but older builds stretch timers heavily. For unthrottled studio timing, use headless Edge (`msedge --headless=new --remote-debugging-port=9450`) with a `window.__GV_DESKTOP__` stub injected through `Page.addScriptToEvaluateOnNewDocument`. The stub needs `auth.status`, `patch`, `on` and similar.
- **Web-mode test:** serve `dist/` and open it by the machine name (not 127.0.0.1), so the page does not find the local engine.
- **Main functions to drive:** `analyse(lat,lon)`, `setView("green"|"priority"|"satellite")`, `GV.ui.studio()`, `GV.ui.review()`, `gvOpenBuilder("occupied"|"empty")`, `GVA.ask("build a complete park in 3D")`, `b3dCrownRings(true)`, `#gv3dTRange`, and `#pPhotoAI` with a `window.gvPickImage` override.
- **Public Overpass mirrors:** they are often down or overloaded, and `overpass-api.de` returns 406 to browser user agents. Never treat an empty mirror answer as "nothing here".

## Known open items

- The long-path junction fix (`desktop/engine.js`) is committed but not yet in an installer. It ships with the next installer, 1.6.3.
- The Mac CI build has no cell pack (it is gitignored), so a Mac builds one on first start (about 4 minutes, once).
- The web version depends on public map servers; when they are down it shows satellite estimates, not mapped buildings.
- The Netlify web copy (`green-vision-india.netlify.app`) is stale because its build allowance is used up.
