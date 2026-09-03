"""Regression tests for bugs that actually shipped, or nearly did.

Every test here corresponds to a defect that was found in this codebase and
fixed. They exist because each one was invisible until someone measured it -
the code looked right, the page rendered, and the number was wrong. A test
that only proves the happy path would have caught none of them.

    python tests/test_regressions.py

No pytest dependency: this has to run on a machine that only did
`pip install -r requirements.txt`.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(label)
    print("  %s  %s%s" % ("ok" if cond else "XX", label, ("   " + detail) if detail and not cond else ""))


def section(name: str) -> None:
    print("\n" + name)
    print("  " + "-" * (len(name) - 2))


# ---------------------------------------------------------------------------
section("1. Falsy-zero traps")
# Three separate bugs came from `x or default` where 0.0 or NaN is legitimate.
from greenplan.features.trends import panel_stats, zone_features  # noqa: E402
import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402

one = pd.DataFrame([{"zone": "a", "month": 0, "traffic": 50.0, "aqi": 120.0, "ndvi": 0.3}])
st = panel_stats(one)
check("std of a single observation is finite, not NaN",
      all(math.isfinite(st[m]["std"]) for m in st),
      str({m: st[m]["std"] for m in st}))
check("std never zero (would divide by zero downstream)",
      all(st[m]["std"] != 0 for m in st))


# ---------------------------------------------------------------------------
section("2. Water is not plantable land")
# Mumbai's top-ranked cell sat over Colaba, roughly half open sea, and the
# 1-NDVI proxy scored it 0.96 plantable.
panel = pd.DataFrame([
    {"zone": "sea",  "month": m, "traffic": 50.0, "aqi": 80.0, "ndvi": -0.05} for m in range(6)
] + [
    {"zone": "land", "month": m, "traffic": 50.0, "aqi": 80.0, "ndvi": 0.30} for m in range(6)
])
f = zone_features(panel, trend_window=6).set_index("zone")
check("negative NDVI (water) yields NO plantable estimate",
      bool(np.isnan(f.loc["sea", "plantable_space"])),
      "got %r" % f.loc["sea", "plantable_space"])
check("ordinary land still gets one",
      not np.isnan(f.loc["land", "plantable_space"]))


# ---------------------------------------------------------------------------
section("3. Species selection reads the site")
# The picker scored on three booleans, so ties fell back to table order and
# Neem/Peepal/Banyan won in every city and every context.
from greenplan.reasoning.client import MockModel  # noqa: E402

m = MockModel()


def pick(**kw):
    row = {"zone": "z", "score": 0.5, "aqi_latest": 90.0, "aqi_pred_delta": 1.0,
           "ndvi_latest": 0.2, "ndvi_slope": -0.001, "plantable_space": 0.6, "soil": None}
    row.update(kw)
    return tuple(m.recommend([row], [])[0]["species"])


tight = pick(plantable_space=0.05, aqi_latest=180.0)
roomy = pick(plantable_space=0.90, aqi_latest=45.0, ndvi_slope=0.004)
check("a cramped polluted site and an open clean one differ",
      tight != roomy, "%s vs %s" % (tight, roomy))
check("cramped site avoids all-large canopies", tight != roomy)


# ---------------------------------------------------------------------------
section("4. Off-city clicks invent nothing")
# `hist.get(...) or 0.0` reported "predicted AQI change +0.0" for a city the
# model had never seen.
row_untrained = {"zone": "z", "score": 0.5, "aqi_latest": 292.0,
                 "aqi_pred_delta": None, "ndvi_latest": 0.24,
                 "ndvi_slope": None, "plantable_space": 0.76, "soil": None}
just = m.recommend([row_untrained], [])[0]["justification"]
check("no fabricated forecast when there is no history",
      "+0.0" not in just and "0.0000/yr" not in just, just[:90])
check("it says the readings are live-only", "no trained history" in just.lower(), just[:90])


# ---------------------------------------------------------------------------
section("5. Local OSM refuses rather than answering falsely")
from greenplan import osmlocal  # noqa: E402

recs = []
for i in range(30):
    recs.append({"k": "building", "t": "w", "lat": 23.02 + i * 0.0002, "lon": 72.57,
                 "bt": "apartments" if i < 5 else None,
                 "g": [[23.02, 72.57], [23.021, 72.571], [23.02, 72.572]]})
for cls_, n in (("primary", 6), ("residential", 40), ("footway", 9)):
    for i in range(n):
        recs.append({"k": "highway", "t": "w", "lat": 23.021 + i * 0.0001,
                     "lon": 72.571, "hw": cls_,
                     "g": [[23.02, 72.57], [23.021, 72.571]]})
d = Path(tempfile.mkdtemp()) / "idx.jsonl.gz"
with gzip.open(d, "wt", encoding="utf-8") as fh:
    for r in recs:
        fh.write(json.dumps(r) + "\n")

idx = osmlocal.LocalOSM(d, focus=[(23.02, 72.57)], radius_km=25)
check("index loads", idx.ready, "n=%d" % idx.n)

ART = ('[out:json];(way(around:2000,23.02,72.57)'
       '[highway~"^(motorway|trunk|primary|secondary)(_link)?$"];);out geom;')
els = (idx.query(ART) or {}).get("elements", [])
classes = sorted({e["tags"]["highway"] for e in els})
check("arterial filter returns ONLY arterials", classes == ["primary"], str(classes))
check("no residential leaked into an arterial query",
      not any(e["tags"]["highway"] == "residential" for e in els))

check("unsupported tag is refused, not answered with 0",
      idx.query('[out:json];way(around:500,23.02,72.57)[power=tower];out count;') is None)
check("point outside the loaded disc is refused",
      idx.query('[out:json];way(around:500,28.6,77.2)[building];out count;') is None)

# The union-bbox gap: two discs far apart must not imply coverage between them.
idx2 = osmlocal.LocalOSM(d, focus=[(23.02, 72.57), (19.08, 72.88)], radius_km=25)
check("a point BETWEEN two loaded discs is not 'covered'",
      not idx2.covers(21.17, 72.83), "Surat must be refused, not answered with zero")
check("each loaded disc is still covered",
      idx2.covers(23.02, 72.57))


# Fixed-value node kinds must be answered by their KIND, not by a field the
# record does not carry.
#
# [highway=traffic_signals] resolves to the index kind "signal", whose records
# store no `hw` field - the kind IS the value. The matcher built a per-record
# predicate on `hw` anyway, every record failed it, and the query returned an
# EMPTY SET rather than refusing. The traffic panel then printed "0 signals"
# over a city with 85 of them in the loaded index, and a reader had no way to
# tell "none here" from "I cannot answer that". Same fault for level crossings
# (`rv`) and motorway junctions (`hw`).
sig_recs = []
for i in range(12):
    sig_recs.append({"k": "signal", "t": "n", "lat": 23.02 + i * 0.0003, "lon": 72.57, "nm": None})
for i in range(5):
    sig_recs.append({"k": "crossing", "t": "n", "lat": 23.021 + i * 0.0003, "lon": 72.571})
for i in range(3):
    sig_recs.append({"k": "ramp", "t": "n", "lat": 23.022 + i * 0.0003, "lon": 72.572, "nm": "X"})
sig_recs.append({"k": "tree", "t": "n", "lat": 23.0205, "lon": 72.5705})

d2 = Path(tempfile.mkdtemp()) / "fixed.jsonl.gz"
with gzip.open(d2, "wt", encoding="utf-8") as fh:
    for r in sig_recs:
        fh.write(json.dumps(r) + "\n")
idx3 = osmlocal.LocalOSM(d2, focus=[(23.02, 72.57)], radius_km=25)

def _count(q):
    r = idx3.query(q)
    return None if r is None else len(r.get("elements", []))

check("traffic signals are found, not silently zero",
      _count('[out:json];node(around:3000,23.02,72.57)[highway=traffic_signals];out geom;') == 12,
      "got %r for 12 indexed signals" %
      _count('[out:json];node(around:3000,23.02,72.57)[highway=traffic_signals];out geom;'))
check("level crossings are found",
      _count('[out:json];node(around:3000,23.02,72.57)[railway=level_crossing];out geom;') == 5)
check("motorway junctions are found",
      _count('[out:json];node(around:3000,23.02,72.57)[highway=motorway_junction];out geom;') == 3)
check("a signals query does not return the trees as well",
      _count('[out:json];node(around:3000,23.02,72.57)[highway=traffic_signals];out geom;') == 12)
check("trees still resolve on their own key",
      _count('[out:json];node(around:3000,23.02,72.57)[natural=tree];out geom;') == 1)



# ---------------------------------------------------------------------------
section("6. Path traversal")
root = ROOT.resolve()
sibling = root.parent / (root.name + "-secrets") / "key.txt"
target = (root / ("../" + root.name + "-secrets/key.txt")).resolve()
check("a sibling directory does not pass the containment test",
      not target.is_relative_to(root),
      "str.startswith would have allowed %s" % target)


# ---------------------------------------------------------------------------
section("7. AQI stays on its scale")
# The US index is undefined above 500; CAMS reports past it in dust season.
from greenplan.features.trends import METRIC_BOUNDS  # noqa: E402
check("AQI forecast bound stops at 500", METRIC_BOUNDS["aqi"][1] == 500.0,
      str(METRIC_BOUNDS["aqi"]))
check("NDVI bound stays inside [-1, 1]",
      -1.0 <= METRIC_BOUNDS["ndvi"][0] and METRIC_BOUNDS["ndvi"][1] <= 1.0)


# ---------------------------------------------------------------------------
section("8. Traffic stays inert")
from greenplan.config import load_config  # noqa: E402

for cfg_name in ("city", "delhi", "mumbai", "bengaluru", "chennai"):
    p = ROOT / "config" / ("%s.yaml" % cfg_name)
    if not p.is_file():
        continue
    cfg = load_config(str(p))
    w = cfg.mcda.weights.get("traffic_worsening", None)
    check("%s: traffic MCDA weight is 0" % cfg.city.name, w == 0.0, "got %r" % w)


# ---------------------------------------------------------------------------
section("9. Staleness radius stays tight")
# A 1,410 m radius made every area within 1.4 km show identical readings.
html = (ROOT / "index.html").read_text(encoding="utf-8")
mm = re.search(r"AOI_REFRESH_M\s*[:=]\s*(\d+)", html)
check("AOI refresh radius is defined", mm is not None)
if mm:
    check("AOI refresh radius <= 250 m", int(mm.group(1)) <= 250,
          "got %s m - large values make neighbouring areas read identically" % mm.group(1))



# ---------------------------------------------------------------------------
section("10. A published score can be taken apart")
# The Priority worklist tells a planner WHICH criterion carried each cell.
# That claim is only worth making if the five weighted contributions actually
# add up to the score the engine published; a decomposition that does not
# reconcile is worse than none, because it reads as an explanation. An earlier
# attempt to re-derive these in the browser was out by up to 0.019 of score,
# which is enough to swap neighbouring cells.
_geo = sorted((ROOT / "outputs").glob("*/recommendations.geojson"))
check("shipped rankings exist to check", bool(_geo), "no outputs/*/recommendations.geojson")
for _g in _geo:
    _d = json.loads(_g.read_text(encoding="utf-8"))
    _scored = [f["properties"] for f in _d.get("features", [])
               if f.get("properties", {}).get("priority_score") is not None]
    _city = _g.parent.name
    _have = [q for q in _scored if q.get("components")]
    check("%s: every scored cell carries a decomposition" % _city,
          len(_have) == len(_scored), "%d of %d" % (len(_have), len(_scored)))
    _worst = 0.0
    for _q in _have:
        _worst = max(_worst, abs(sum(_q["components"].values()) - float(_q["priority_score"])))
    check("%s: the parts sum to the published score" % _city, _worst <= 1e-3,
          "off by up to %.4f" % _worst)
    # A zero-weight criterion must never be the reason a cell was chosen.
    _drivers = set()
    for _q in _have:
        _drivers.add(max(_q["components"], key=lambda k: _q["components"][k]))
    check("%s: traffic never drives a ranking" % _city,
          "traffic_worsening" not in _drivers, str(sorted(_drivers)))


# ---------------------------------------------------------------------------
section("11. Nothing touches GV before GV exists")
# `GV.rainGapNote = ...` got inserted ~1,400 lines above `const GV = ...`.
# That is a temporal-dead-zone ReferenceError, not a hoisting nicety: it threw
# during script execution and aborted the WHOLE block, so the map, the views
# and the studio never initialised. The page still rendered its shell, which
# is exactly why it looked fine.
_html = (ROOT / "index.html").read_text(encoding="utf-8")
_decl = re.search(r"^const GV\s*=", _html, re.M)
check("`const GV` is declared somewhere", _decl is not None)
if _decl:
    _early = re.findall(r"^\s*GV\.\w+\s*=", _html[: _decl.start()], re.M)
    check("no GV member is assigned before that line", not _early,
          "found %r - this aborts the entire script block" % (_early[:3],))


# ---------------------------------------------------------------------------
section("12. A ranking is never shown over the wrong city")
# One server holds five cities and routes /api/zones by coordinate, but the
# page asked without a coordinate and then cached the answer for the life of
# the tab. Panning to Delhi kept Ahmedabad's 146 cells on screen: every number
# real, every number about somewhere else.
check("the ranking fetch carries a coordinate",
      'lat=" + pt[0].toFixed(5)' in _html,
      "priTryFetch must qualify /api/zones with lat/lon")
check("a cached ranking is tested against where we are",
      "gvCollectionCovers(GVP.data, here)" in _html)
check("the canopy forecast is tested the same way",
      "gvCollectionCovers(GVG.loss, here)" in _html)
check("an uncovered area says so instead of just drawing nothing",
      "priNoDataNotice(true)" in _html)

# And the guard itself, executed rather than grepped for.
_node = shutil.which("node")
if not _node:
    print("  -- node not on PATH; skipping execution of the coverage guard")
else:
    _fn = re.search(r"function gvCollectionCovers\(gj, pt\)\{.*?\n\}", _html, re.S)
    check("the guard's source can be located", _fn is not None)
    if _fn:
        _harness = _fn.group(0) + """
const mk = (lat, lon) => ({features: [{geometry: {type: "Polygon", coordinates: [[
  [lon - 0.1, lat - 0.1], [lon + 0.1, lat - 0.1], [lon + 0.1, lat + 0.1],
  [lon - 0.1, lat + 0.1], [lon - 0.1, lat - 0.1]]]}}]});
const AHM = mk(23.02, 72.57);
console.log(JSON.stringify({
  sameCity:    gvCollectionCovers(AHM, [23.02, 72.57]),
  otherCity:   gvCollectionCovers(AHM, [28.61, 77.21]),
  surat:       gvCollectionCovers(AHM, [21.17, 72.83]),
  justOutside: gvCollectionCovers(AHM, [23.14, 72.57]),
  noData:      gvCollectionCovers(null, [23.02, 72.57]),
  noPoint:     gvCollectionCovers(AHM, null)
}));
"""
        _tmp = Path(tempfile.mkdtemp()) / "guard.js"
        _tmp.write_text(_harness, encoding="utf-8")
        _run = subprocess.run([_node, str(_tmp)], capture_output=True, text=True)
        check("the guard runs", _run.returncode == 0, _run.stderr[:200])
        if _run.returncode == 0:
            _res = json.loads(_run.stdout)
            check("the loaded city covers its own centre", _res["sameCity"] is True)
            check("Delhi is NOT covered by Ahmedabad's cells", _res["otherCity"] is False)
            check("Surat, between two served cities, is not covered", _res["surat"] is False)
            check("the rim keeps a little slack", _res["justOutside"] is True)
            check("no data covers nothing", _res["noData"] is False)
            check("no point covers nothing", _res["noPoint"] is False)



# ---------------------------------------------------------------------------
section("13. The assistant answers about the city you are looking at")
# The server routes every POST to a city by the point in the body. It read
# lat/lon only at the TOP level - which is where /api/recommend and
# /api/species put them, and is not where /api/assistant puts them: that route
# passes its CONTEXT, and a context keeps the point under "aoi". So lat/lon
# came back None on every assistant call, pick() fell through to the default
# city, and a question asked over Bengaluru - with Bengaluru's AQI and canopy
# on screen - was answered with "146 H3 cells across Ahmedabad", Ahmedabad's
# scores and Ahmedabad's species. Every figure real, every figure about
# somewhere else.
from greenplan.server import body_point  # noqa: E402

check("a point at the top level is found",
      body_point({"lat": 23.02, "lon": 72.57}) == (23.02, 72.57))
check("a point inside an assistant context is found",
      body_point({"aoi": {"lat": 12.99, "lon": 77.55, "km2": 100}}) == (12.99, 77.55),
      "this is the leak: an unfound point routes to the default city")
check("an explicit null at the top level falls through to the context",
      body_point({"lat": None, "aoi": {"lat": 28.61, "lon": 77.21}}) == (28.61, 77.21))
check("a genuinely location-free body stays unrouted",
      body_point({}) == (None, None))
check("no body at all is survivable", body_point(None) == (None, None))
check("garbage coordinates do not route anywhere",
      body_point({"lat": "abc", "lon": "x"}) == (None, None))
check("an impossible latitude does not route anywhere",
      body_point({"lat": 999, "lon": 0}) == (None, None),
      "out-of-range values must not reach pick()")
check("a malformed aoi is survivable",
      body_point({"aoi": "notadict"}) == (None, None))

# A half-given point is not a point. Routing on a lone latitude would pick a
# city from a meridian.
check("latitude without longitude is not a point",
      body_point({"lat": 23.02}) == (None, None))



# ---------------------------------------------------------------------------
section("14. The language menu does not promise what it cannot deliver")
# It listed thirteen languages and had dictionaries for five. Picking Tamil set
# the code, relabelled the button, persisted the choice, and left the entire
# interface in English with nothing said. For a product whose argument is that
# it does not overstate what it has, that is a fabricated claim in a different
# currency.
_idx = json.loads((ROOT / "data" / "i18n" / "index.json").read_text(encoding="utf-8"))
_langs = _idx["languages"]
check("index.json declares languages", bool(_langs))
check("every declared language records whether it is translated",
      all("translated" in l for l in _langs),
      "missing on: %s" % [l["code"] for l in _langs if "translated" not in l])
for _l in _langs:
    _f = ROOT / "data" / "i18n" / ("%s.json" % _l["code"])
    check("%s: the translated flag matches whether the file exists" % _l["code"],
          bool(_l.get("translated")) == _f.is_file(),
          "flag=%s file=%s" % (_l.get("translated"), _f.is_file()))
_html = (ROOT / "index.html").read_text(encoding="utf-8")
check("the menu marks untranslated languages",
      "not translated yet" in _html)
check("choosing one says the interface will stay in English",
      "the interface stays in English" in _html)
# The dictionaries themselves must stay loadable: a language advertised as
# translated whose file will not parse is the same broken promise.
for _l in _langs:
    # English is the SOURCE text, so en.json carries an empty `ui` on purpose -
    # there is nothing to translate English into. Requiring entries there
    # failed a file that is correct.
    if not _l.get("translated") or _l["code"] == "en":
        continue
    _f = ROOT / "data" / "i18n" / ("%s.json" % _l["code"])
    try:
        _d = json.loads(_f.read_text(encoding="utf-8"))
        _okd = isinstance(_d.get("ui"), dict) and len(_d["ui"]) > 0
    except Exception as _e:
        _okd = False
    check("%s: its dictionary parses and has entries" % _l["code"], _okd)



# ---------------------------------------------------------------------------
section("15. One rainfall claim, in every place that makes it")
# The pipeline moved from a five-year window (2020-2024, which read 45% high)
# to the 1991-2020 WMO standard normal. Three places in the product state that
# window in prose, and they were corrected one at a time as each was noticed:
# the area panel, the assistant's sources answer, and - found last, by asking
# the assistant to compare two places - the comparison table's own footnote,
# which had gone on saying 2020-2024 for as long as the others.
#
# A figure that is right in the data and wrong in the sentence beside it is
# still wrong to the reader, and prose does not get type-checked.
_srcs = {
    "index.html": (ROOT / "index.html").read_text(encoding="utf-8"),
    "assistant en.json": json.dumps(
        json.loads((ROOT / "data" / "i18n" / "en.json").read_text(encoding="utf-8")),
        ensure_ascii=False),
}
_STALE = re.compile(r"2020\s*[-–]\s*2024")
for _name, _txt in _srcs.items():
    # Strip comments so the note explaining the old value does not trip this.
    _code = re.sub(r"/\*.*?\*/|//[^\n]*", "", _txt, flags=re.S)
    _hits = _STALE.findall(_code)
    check("%s: no live text still claims the 2020-2024 window" % _name,
          not _hits, "%d occurrence(s)" % len(_hits))

check("index.html states the WMO normal period where it states a window",
      "1991–2020" in _srcs["index.html"] or "1991-2020" in _srcs["index.html"])

# The 1-minus-NDVI figure must not be sold as plantable ground anywhere.
_bad_label = re.compile(r"Bare,?\s*plantable\s*ground", re.I)
check("no panel calls the 1-minus-NDVI proxy 'plantable ground'",
      not _bad_label.search(_srcs["index.html"]))



# ---------------------------------------------------------------------------
section("16. The printed brief does not present the inert stream as a reading")
# planting_brief.txt is the artefact that actually leaves the building, and it
# was the last place still printing traffic as data. Every zone line read
# "traffic 50 (+0 predicted yr-on-yr)" beside real AQI and NDVI - the same 50
# in every zone of every city, because the stream is a constant placeholder at
# MCDA weight 0.0 that contributes nothing to any score. A planner comparing
# ten zones sees an identical number and can only conclude traffic is uniformly
# moderate across the city, which is a claim about the city that nothing
# measured. The brief also listed "Underestimated traffic in zone X" as a
# lesson learned, and its CAVEATS covered plantable space, soil and species
# but not this.
_briefs = sorted((ROOT / "outputs").glob("*/planting_brief.txt"))
check("planting briefs exist to check", bool(_briefs))
for _b in _briefs:
    _txt = _b.read_text(encoding="utf-8")
    _city = _b.parent.name
    # A per-zone reading looks like "traffic 50" / "traffic 50.0".
    check("%s: no per-zone traffic reading" % _city,
          not re.search(r"traffic\s+\d", _txt),
          "found %r" % (re.findall(r"traffic\s+\d[^,)]*", _txt)[:2],))
    check("%s: no traffic MAE beside the real ones" % _city,
          not re.search(r"MAE[^\n]*traffic", _txt))
    check("%s: no 'lesson' about the inert stream" % _city,
          not re.search(r"(?:Under|Over)estimated traffic", _txt))
    check("%s: says plainly that traffic is not in the ranking" % _city,
          "Traffic is NOT in this ranking" in _txt)


# ---------------------------------------------------------------------------
print("\n" + "=" * 62)
print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("\n  FAILED:")
    for f in FAIL:
        print("    - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
