"""Bake the trained engine down to static JSON for a serverless deploy.

Why this exists
---------------
`greenplan.server` is Python: pandas, numpy and h3 hold the 42-month panel and
the MCDA ranking. Cloudflare Pages serves files; Workers run JS/WASM. Neither
will run that stack.

But almost nothing the browser asks the engine for is actually *dynamic*. For a
fixed city the ranking, the forecast, the soil table and the species knowledge
base are constants — they change when you re-run training on new data, not when
a visitor clicks. So we compute them once, here, and ship them as files.

The one genuinely dynamic part is the assistant, and it is deterministic: no
model, no weights, just intent matching and planning. That is ported to
JavaScript in `web/gv-engine.js` and kept honest by `scripts/parity_check.py`,
which runs the same corpus through this Python and that JavaScript and diffs
the results.

What ships
----------
    dist/
      index.html                 the studio, unchanged
      data/i18n/*.json           interface + assistant strings (5 shipped languages)
      engine/zones.geojson       146 ranked cells: score, species, justification
      engine/greenloss.json      the same cells as polygons, green/amber/red
      engine/cells.json          the ranked panel as rows
      engine/soil.json           SoilGrids pH + texture per cell
      engine/species.json        the 30-species knowledge base
      engine/meta.json           city, counts, thresholds, when it was baked
      gv-engine.js               the client-side engine + assistant

Point-in-polygon over 146 hexagons replaces h3 in the browser, so no H3
library is needed client-side — the polygons are already in greenloss.json.

Run:
    .venv/Scripts/python scripts/build_static.py --config config/city.yaml
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import pathlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from greenplan.features.h3grid import cell_center  # noqa: E402
from greenplan.reasoning.species import SPECIES_KB  # noqa: E402
from greenplan.config import load_config
from greenplan.server import Engine  # noqa: E402

log = logging.getLogger("build_static")


def _finite(v):
    """JSON has no NaN. Anything not finite becomes null, never a guess."""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _json_safe(obj):
    """Recursively replace non-finite floats with None.

    Belt and braces: engine.py now writes clean GeoJSON, but this build is
    the thing a public deploy is cut from, and a single bare NaN makes the
    WHOLE document unparseable in a browser — JSON.parse is all-or-nothing.
    Cheap insurance against a regression upstream."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _write(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    # separators: no wasted bytes; ensure_ascii=False keeps Indic text readable
    # in the file and is smaller over the wire once gzipped.
    # allow_nan=False turns a stray NaN into a build failure rather than a
    # site that loads for nobody.
    text = json.dumps(_json_safe(obj), ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def build(config: str, out_dir: Path, slug: str | None = None,
          shared: bool = True, keep_local_osm: bool = False) -> dict:
    """Bake one city.

    `slug` puts the engine files in engine/<slug>/ instead of engine/, which is
    what makes a multi-city bundle possible: the five cities differ only in
    those seven files, and index.html, gv-engine.js and the five i18n
    catalogues are identical for all of them.

    `shared` writes those common files. main() sets it once, for the first
    city, so a five-city build does not copy the same 700 KB page five times
    and then mirror it five times.

    Returns what the manifest needs: the slug, the display name and the bbox
    the page tests a point against.
    """
    log.info("loading + training the engine (this is the slow part) …")
    eng = Engine(config)

    engine_dir = out_dir / "engine" / slug if slug else out_dir / "engine"
    sizes: dict[str, int] = {}

    # --- the ranked cells, exactly as /api/zones serves them ---------------
    sizes["zones.geojson"] = _write(engine_dir / "zones.geojson", eng.zones_geojson)

    # --- green / amber / red forecast, exactly as /api/greenloss ----------
    sizes["greenloss.json"] = _write(engine_dir / "greenloss.json", eng.greenloss)

    # --- the ranked panel as rows, plus the cell centre so the client can
    #     answer "nearest bare cell" without h3 -------------------------------
    cells = []
    for r in eng.ranked.itertuples():
        score = _finite(r.score)
        if score is None:
            continue                      # no real coverage — omit, don't guess
        try:
            lat, lon = cell_center(r.zone)
        except Exception:
            lat = lon = None
        cells.append({
            "zone": r.zone,
            "rank": int(r.rank),
            "score": round(score, 4),
            "lat": _finite(lat),
            "lon": _finite(lon),
            "aqi_latest": _finite(r.aqi_latest),
            "aqi_pred_delta": _finite(r.aqi_pred_delta),
            "ndvi_latest": _finite(r.ndvi_latest),
            "ndvi_trend_per_year": _finite(float(r.ndvi_slope) * 12),
            "ndvi_pred_delta": _finite(r.ndvi_pred_delta),
            "plantable_space": _finite(r.plantable_space),
        })
    rec_by_zone = {r["zone"]: r for r in eng.recommendations}
    for c in cells:
        rec = rec_by_zone.get(c["zone"], {})
        c["species"] = rec.get("species", [])
        c["justification"] = rec.get("justification", "")
    sizes["cells.json"] = _write(engine_dir / "cells.json", cells)

    # --- soil, per cell ----------------------------------------------------
    soil = {}
    for zone, prof in eng.soil.items():
        soil[zone] = {
            "ph": _finite(prof.ph),
            "ph_class": prof.ph_class,
            "texture": prof.texture_class,
            "texture_simple": prof.texture_simple,
            "sand": _finite(prof.sand),
            "silt": _finite(prof.silt),
            "clay": _finite(prof.clay),
            "organic_carbon": _finite(prof.soc),
            "nitrogen": _finite(prof.nitrogen),
            "moisture": _finite(prof.moisture),
        }
    sizes["soil.json"] = _write(engine_dir / "soil.json", soil)

    # --- species knowledge base -------------------------------------------
    sizes["species.json"] = _write(engine_dir / "species.json", SPECIES_KB)

    # --- what this build is ------------------------------------------------
    health = eng.health()
    meta = {
        "city": health["city"],
        "built_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "zones": health["zones"],
        "memory_records": health["memory_records"],
        "soil_cells": len(soil),
        "species_kb": len(SPECIES_KB),
        "greenloss": health["greenloss"],
        "languages": health["languages"],
        "h3_resolution": eng.cfg.grid.h3_resolution,
        "months_history": eng.cfg.data.months_history,
        "thresholds": eng.greenloss["thresholds"],
        "mcda_weights": dict(eng.cfg.mcda.weights),
        # Said plainly, in the payload itself, so the claim travels with the
        # data rather than living only in a README nobody opened.
        "reasoning": "deterministic",
        "note": (
            "Baked from the trained engine. The ranking, forecast, soil and "
            "species tables are computed by greenplan and are not recomputed "
            "in the browser. The assistant is deterministic: intent matching "
            "and planning, no model and no weights."
        ),
    }
    sizes["meta.json"] = _write(engine_dir / "meta.json", meta)

    # --- static assets the page already expects ---------------------------
    # The studio tries "/api/osm" (this repo's local OSM index) before the
    # public Overpass instance. That endpoint cannot exist on a static host,
    # and worse, the SPA fallback in _redirects answers it with index.html at
    # HTTP 200 — so every map query would fetch a page of HTML, fail to parse,
    # and only then try Overpass. Strip the local endpoint out of the baked
    # copy so the static build goes straight to the public instance.
    html = (ROOT / "index.html").read_text(encoding="utf-8")

    # `keep_local_osm` is for the DESKTOP bundle, and the distinction is not a
    # nicety. The Windows app was packaged from this same static output, so it
    # shipped with the local endpoint stripped — and then, on a machine running
    # greenplan.server with 2.6 million indexed features, every map read went
    # to the public Overpass instance anyway. On a network where that host is
    # slow or blocked the Traffic tab simply never loaded, and said OSM was
    # throttling. The app is not a static host; it has an engine to talk to,
    # and it must be allowed to.
    if keep_local_osm:
        log.info("  desktop build: /api/osm kept — the app can reach a local engine")
    else:
        # Match the whole array however it is formatted, rather than one exact
        # string. The previous literal match silently stopped working the
        # moment a mirror was added to the list, and the build only warned.
        m = re.search(r"OVERPASS:\s*\[[^\]]*\]", html)
        if not m:
            log.warning("could not strip /api/osm from the static build — check CFG.OVERPASS")
        else:
            eps = [e for e in re.findall(r'"([^"]+)"', m.group(0))
                   if not e.startswith("/")]
            html = html.replace(
                m.group(0),
                "OVERPASS:[" + ", ".join('"%s"' % e for e in eps) + "]", 1)
            log.info("  static build: /api/osm removed, %d public mirror(s) left",
                     len(eps))

    if not shared:
        # A per-city pass: the engine files above are all that differ.
        total = sum(sizes.values())
        log.info("  %-14s %8.1f KB in engine/%s/", eng.cfg.city.name, total / 1024, slug)
        return _city_entry(eng, slug)

    (out_dir / "index.html").write_text(html, encoding="utf-8")
    i18n_src, i18n_dst = ROOT / "data" / "i18n", out_dir / "data" / "i18n"
    if i18n_src.is_dir():
        i18n_dst.mkdir(parents=True, exist_ok=True)
        for f in i18n_src.glob("*.json"):
            shutil.copy2(f, i18n_dst / f.name)

    # The brand marks ship with the site because the transactional EMAILS
    # need somewhere to point an <img> at. Mail clients block SVG and refuse
    # data: URIs in images, so a real logo in an email has to be a hosted
    # raster file - there is no self-contained option. These are small, they
    # change roughly never, and _headers gives them a long cache.
    brand_src, brand_dst = ROOT / "brand", out_dir / "brand"
    if brand_src.is_dir():
        brand_dst.mkdir(parents=True, exist_ok=True)
        for pat in ("*.png", "*.svg"):
            for f in sorted(brand_src.glob(pat)):
                shutil.copy2(f, brand_dst / f.name)
        log.info("  brand: %d files", len(list(brand_dst.iterdir())))

    web = ROOT / "web" / "gv-engine.js"
    if web.is_file():
        shutil.copy2(web, out_dir / "gv-engine.js")
        sizes["gv-engine.js"] = web.stat().st_size
    else:
        log.warning("web/gv-engine.js missing — the static build will have no "
                    "assistant. Build it before deploying.")

    # Cloudflare Pages: long-cache the immutable engine payloads, never the
    # HTML. Without this the browser re-downloads 230 KB of unchanged JSON on
    # every visit, and worse, serves a stale index.html after a redeploy.
    (out_dir / "_headers").write_text(
        "/engine/*\n"
        "  Cache-Control: public, max-age=3600, must-revalidate\n"
        "/data/i18n/*\n"
        "  Cache-Control: public, max-age=3600, must-revalidate\n"
        "/brand/*\n"
        "  Cache-Control: public, max-age=604800, immutable\n"
        "/gv-engine.js\n"
        "  Cache-Control: public, max-age=600, must-revalidate\n"
        "/index.html\n"
        "  Cache-Control: no-cache\n"
        "/\n"
        "  Cache-Control: no-cache\n",
        encoding="utf-8",
    )

    # SPA fallback. The studio is one page that routes in the browser, so a
    # deep link must still serve index.html rather than 404. This file was
    # present in the deployed site but nothing here wrote it, so it survived
    # only because the build does not clear dist/ first — a clean rebuild
    # dropped it silently. Written here so the build reproduces the deployment
    # instead of quietly diverging from it.
    (out_dir / "_redirects").write_text(
        "/*  /index.html  200\n", encoding="utf-8", newline="\n"
    )

    # A DESKTOP bundle is never mirrored to docs/. docs/ is the live public
    # web site, and this build deliberately keeps "/api/osm" in CFG.OVERPASS —
    # a path that cannot exist on a static host, where the SPA fallback would
    # answer it with a page of HTML at HTTP 200 and every map read would parse
    # a document as JSON before giving up. Publishing the app's bundle to the
    # web would break the web.
    if keep_local_osm:
        log.info("  desktop build: docs/ mirror skipped (that is the web site)")
        total = sum(sizes.values())
        log.info("wrote %s", out_dir)
        for name, n in sorted(sizes.items(), key=lambda kv: -kv[1]):
            log.info("  %-20s %8.1f KB", name, n / 1024)
        log.info("  %-20s %8.1f KB", "TOTAL", total / 1024)
        return _city_entry(eng, slug)

    # Mirror the build into docs/ so GitHub Pages can serve it straight from
    # the default branch. Pages needs .nojekyll or it silently drops any path
    # beginning with an underscore - which would take out _headers and could
    # take out future assets. The two folders stay byte-identical so a deploy
    # to Pages, Netlify or Cloudflare all come from the same artefact.
    import shutil as _sh
    docs = ROOT / "docs"

    # Copy OVER the existing tree rather than deleting it first.
    #
    # rmtree fails with WinError 5 whenever OneDrive (or an editor, or an
    # antivirus scan) holds a handle on any file underneath - and it failed
    # exactly that way here, leaving docs/ stale while the build reported
    # success and the commit went out. A stale docs/ means the LIVE SITE
    # silently serves the previous version: the worst kind of failure,
    # because everything looks fine.
    #
    # dirs_exist_ok merges in place and never needs the directory handle, so
    # a locked file fails loudly on that one file instead of taking out the
    # whole mirror. Stale leftovers are then pruned explicitly.
    _sh.copytree(out_dir, docs, dirs_exist_ok=True)
    keep = {q.relative_to(out_dir) for q in out_dir.rglob("*") if q.is_file()}
    keep.add(pathlib.Path(".nojekyll"))
    for q in sorted((f for f in docs.rglob("*") if f.is_file()), reverse=True):
        if q.relative_to(docs) not in keep:
            try:
                q.unlink()
            except OSError as exc:
                log.warning("  could not remove stale %s: %s", q.name, exc)
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    # Verify rather than assume. If the mirror did not actually take, say so
    # loudly - a silent stale mirror is what caused this comment to exist.
    src_html = (out_dir / "index.html").read_bytes()
    dst_html = (docs / "index.html").read_bytes()
    if src_html != dst_html:
        raise RuntimeError(
            "docs/index.html does not match dist/index.html after the mirror. "
            "Something is holding the file open (OneDrive, an editor, antivirus). "
            "Close it and re-run; do NOT commit, the live site would go stale."
        )
    log.info("  mirrored to docs/ for GitHub Pages (verified identical)")

    total = sum(sizes.values())
    log.info("wrote %s", out_dir)
    for k, v in sorted(sizes.items(), key=lambda kv: -kv[1]):
        log.info("  %-18s %8.1f KB", k, v / 1024)
    log.info("  %-18s %8.1f KB", "TOTAL", total / 1024)
    return _city_entry(eng, slug)


def _city_entry(eng, slug: str | None) -> dict:
    """One row of engine/cities.json: enough for the page to decide, offline,
    whether this bundle can answer for the point the reader is looking at."""
    lon0, lat0, lon1, lat1 = eng.cfg.city.bbox
    return {
        "slug": slug or _slug(eng.cfg.city.name),
        "name": eng.cfg.city.name,
        "bbox": [lon0, lat0, lon1, lat1],
        "lat": (lat0 + lat1) / 2.0,
        "lon": (lon0 + lon1) / 2.0,
        "cells": len(eng.zones_geojson.get("features", [])),
    }


def _slug(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "city"


def main() -> None:
    ap = argparse.ArgumentParser(description="Bake the engine to static JSON")
    ap.add_argument("--config", default="config/city.yaml")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--keep-local-osm", action="store_true",
                    help="keep \"/api/osm\" in CFG.OVERPASS. Use this for the "
                         "DESKTOP bundle (dist_app), which runs beside a local "
                         "greenplan.server and must be allowed to reach it. "
                         "Leave it off for a static web host, where that path "
                         "cannot exist and the SPA fallback would answer it "
                         "with a page of HTML at HTTP 200.")
    ap.add_argument("--cities", nargs="*", metavar="CONFIG",
                    help="bake several cities into one bundle, each under "
                         "engine/<slug>/, plus an engine/cities.json manifest "
                         "the page routes on. Without it a single city is "
                         "baked into engine/, which is the old layout and what "
                         "an existing deploy expects.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    if not args.cities:
        build(args.config, out, keep_local_osm=args.keep_local_osm)
        return

    # Multi-city. The shared files (index.html, gv-engine.js, i18n, the docs/
    # mirror) are written once, on the LAST pass, so the per-city passes stay
    # cheap and the mirror happens after everything it mirrors exists.
    entries = []
    cfgs = list(args.cities)
    for i, cfg in enumerate(cfgs):
        slug = _slug(load_config(ROOT / cfg if not Path(cfg).is_absolute() else cfg).city.name)
        entries.append(build(cfg, out, slug=slug, shared=(i == len(cfgs) - 1),
                             keep_local_osm=args.keep_local_osm))

    manifest = {"cities": sorted(entries, key=lambda e: e["name"])}
    _write(out / "engine" / "cities.json", manifest)
    log.info("baked %d cities: %s", len(entries),
             ", ".join("%s (%d cells)" % (e["name"], e["cells"]) for e in manifest["cities"]))

    # The mirror ran during the shared pass, before cities.json existed.
    docs = ROOT / "docs"
    if docs.is_dir() and not args.keep_local_osm:
        import shutil as _sh
        _sh.copy2(out / "engine" / "cities.json", docs / "engine" / "cities.json")
        for e in entries:
            src, dst = out / "engine" / e["slug"], docs / "engine" / e["slug"]
            _sh.copytree(src, dst, dirs_exist_ok=True)
        log.info("  mirrored %d city folders + cities.json into docs/", len(entries))


if __name__ == "__main__":
    main()
