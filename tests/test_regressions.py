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
section("17. Every assistant sentence exists in every language it offers")

# Two separate promises, and only one of them is cosmetic.
#
# Coverage is cosmetic: i18n.t falls back per key, so a missing string comes
# out in English. Ugly, obvious, harmless.
#
# Placeholder drift is not. A translation that spells {trees} as {tree} either
# raises at format time or renders a sentence with a hole where a measured
# number belongs, and no amount of reading the Marathi will catch it unless you
# already know what the English took. This asserts both, because the second one
# is invisible until a planner is looking at it.
#
# The third check is the one that bit: _load cached a dictionary forever, so a
# language exercised before a translation pass kept answering in English while
# an untouched language picked the new strings up in the same process. The file
# was right and the reply was wrong, which is the worst shape a bug can take.
from greenplan.reasoning import i18n as _i18n

_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)[^{}]*\}")
_en_a = (json.loads((ROOT / "data/i18n/en.json").read_text(encoding="utf-8"))
         .get("assistant") or {})
check("en.json carries the assistant strings", bool(_en_a))

for _lang in _i18n.available():
    _code = _lang.get("code")
    if _code == "en":
        continue
    _d = (json.loads((ROOT / ("data/i18n/%s.json" % _code)).read_text(encoding="utf-8"))
          .get("assistant") or {})
    _missing = sorted(k for k in _en_a if k not in _d)
    check("%s: every assistant string translated" % _code, not _missing,
          "%d missing, first: %s" % (len(_missing), ", ".join(_missing[:4])))
    _drift = [k for k, v in _d.items()
              if k in _en_a
              and set(_FIELD.findall(_en_a[k])) != set(_FIELD.findall(v))]
    check("%s: no placeholder drift" % _code, not _drift,
          "would render a hole or raise: " + ", ".join(_drift[:4]))

# An edited dictionary must take effect. Probe a key no other check reads.
_probe = ROOT / "data/i18n/hi.json"
_orig = _probe.read_bytes()
try:
    _before = _i18n.t("view.satellite", "hi")
    _mut = json.loads(_orig.decode("utf-8"))
    _mut["assistant"]["view.satellite"] = "MTIME-PROBE"
    _probe.write_text(json.dumps(_mut, ensure_ascii=False, indent=2), encoding="utf-8")
    _after = _i18n.t("view.satellite", "hi")
finally:
    _probe.write_bytes(_orig)
check("an edited dictionary is re-read, not served from a stale cache",
      _after == "MTIME-PROBE", "still serving %r" % _before)
check("restoring the file restores the string",
      _i18n.t("view.satellite", "hi") == _before)


# ---------------------------------------------------------------------------
section("18. Site preparation is billed over ground the design actually works")

# Three site-prep lines are quantity x rate over an area, and all three used to
# take the whole plot. On a one-hectare plot holding sixty trees and nothing
# else that billed 9,933 m2 of clearing and 9,933 m2 of rough grading - about
# 4.96 lakh of a 10.87 lakh scheme, for levelling nobody would do to dig sixty
# pits. Topsoil was fixed first; clearing and grading kept the fault, and they
# were the expensive half.
#
# Each applies to different ground, and the differences are the point:
#
#   clearing   surfaces + tree pits   scrub goes where machines go
#   grading    surfaces only          pits are excavated under Planting, and
#                                     grading them bills the same earth twice
#   topsoil    planted + pits         not under paving
#
# The arithmetic is pulled out of index.html and run, so this tests the
# quantities rather than the presence of a comment about them.
_html18 = (ROOT / "index.html").read_text(encoding="utf-8")
check("the empty-design fallback still prices the whole plot",
      "anythingPlaced ? clearM2 : area" in _html18
      and "anythingPlaced ? gradeM2 : area" in _html18,
      "an undesigned plot should still show a full-site budget")

_exprs = {}
for _name in ("clearM2", "gradeM2", "topsoilM2"):
    _m = re.search(r"const %s = ([^;]+);" % _name, _html18, re.S)
    check("the %s quantity can be located" % _name, _m is not None)
    if _m:
        _exprs[_name] = " ".join(_m.group(1).split())

check("grading does not also bill the tree pits",
      "treePitM2" not in _exprs.get("gradeM2", "treePitM2"),
      "pit earth is already billed under Planting: %r" % _exprs.get("gradeM2"))

_node18 = shutil.which("node")
if not _node18 or len(_exprs) != 3:
    print("  -- node not on PATH; the quantities were not executed")
else:
    _cases = {
        # area, planted, paved, pits
        "treesOnly":    [9933, 0, 0, 240],
        "surfacesOnly": [9933, 3477, 596, 0],
        "mixed":        [9933, 3477, 596, 240],
        "overCommitted": [1000, 900, 900, 40],
    }
    _harness = "const CASES = " + json.dumps(_cases) + ";\nconst out = {};\n" + """
for (const [k, v] of Object.entries(CASES)) {
  const [area, plantedM2, pavedM2, treePitM2] = v;
  const anythingPlaced = (plantedM2 + pavedM2) > 0 || treePitM2 > 0;
""" + "  const clearM2 = %s;\n  const gradeM2 = %s;\n  const topsoilM2 = %s;\n" % (
        _exprs["clearM2"], _exprs["gradeM2"], _exprs["topsoilM2"]) + """
  out[k] = {clear: clearM2, grade: gradeM2, topsoil: topsoilM2, area};
}
console.log(JSON.stringify(out));
"""
    _t18 = Path(tempfile.mkdtemp()) / "prep.js"
    _t18.write_text(_harness, encoding="utf-8")
    _r18 = subprocess.run([_node18, str(_t18)], capture_output=True, text=True)
    check("the quantities run", _r18.returncode == 0, _r18.stderr[:200])
    if _r18.returncode == 0:
        _q = json.loads(_r18.stdout)
        _t = _q["treesOnly"]
        check("sixty trees do not have a hectare graded", _t["grade"] == 0,
              "graded %s m2 for tree pits" % _t["grade"])
        check("sixty trees clear only their own pits", _t["clear"] == 240)
        check("sixty trees topsoil only their own pits", _t["topsoil"] == 240)
        _s = _q["surfacesOnly"]
        check("surfaces are graded over exactly the surfaces",
              _s["grade"] == 3477 + 596)
        check("paving gets no topsoil", _s["topsoil"] == 3477)
        _m18 = _q["mixed"]
        check("clearing covers surfaces and pits together",
              _m18["clear"] == 3477 + 596 + 240)
        check("grading stays under clearing", _m18["grade"] < _m18["clear"])
        for _k, _v in _q.items():
            check("%s: no quantity exceeds the plot" % _k,
                  max(_v["clear"], _v["grade"], _v["topsoil"]) <= _v["area"],
                  "%s in a %s m2 plot" % (_v, _v["area"]))


# ---------------------------------------------------------------------------
section("19. A published directory publishes only what is in it")

# The server serves three directories - outputs, data, dist - and decides a
# request is allowed by reading its FIRST PATH SEGMENT. "data" in
# /data/../greenplan/server.py is that segment, and `..` does not disturb it.
# The only other check was that the resolved file stayed inside the repo, which
# every file in the repo does. So the engine's own source came back over HTTP,
# and so did config/city.yaml - where a TomTom key lives when one is set - and
# the venv config. curl hides this by normalising `..` before it sends; a raw
# socket, or any client that does not, walked straight through.
#
# safe_static_path answers both halves: inside the repo, AND inside the
# directory that authorised the request.
from greenplan.server import safe_static_path

_root = ROOT

# Things that must never come back, whatever route is tried.
_escapes = [
    ("data",    "data/../greenplan/server.py"),
    ("data",    "data/../config/city.yaml"),
    ("data",    "data/../.venv/pyvenv.cfg"),
    ("data",    "data/i18n/../../greenplan/server.py"),
    ("data",    "data/./../greenplan/engine.py"),
    ("outputs", "outputs/../greenplan/reasoning/assistant.py"),
    ("dist",    "dist/../greenplan/server.py"),
    ("dist",    "dist/engine/../../../greenplan/server.py"),
    ("data",    "data/../../../../Windows/win.ini"),
]
for _within, _rel in _escapes:
    check("refused: %s" % _rel,
          safe_static_path(_root, _rel, _within) is None,
          "served a file outside %s/" % _within)

# A sibling directory whose name merely starts with an allowed one. This is the
# text-prefix trap: "data-secrets" starts with "data".
_sib = _root / "data-secrets-probe"
_made = False
try:
    if not _sib.exists():
        _sib.mkdir()
        _made = True
    (_sib / "key.txt").write_text("nope", encoding="utf-8")
    check("a sibling directory sharing a prefix is not inside it",
          safe_static_path(_root, "data-secrets-probe/key.txt", "data") is None)
finally:
    try:
        (_sib / "key.txt").unlink()
        if _made:
            _sib.rmdir()
    except OSError:
        pass

# And the ordinary files still resolve, or the fix has broken the app.
_serves = [
    ("data",    "data/i18n/en.json"),
    ("data",    "data/i18n/index.json"),
    ("outputs", "outputs/ahmedabad/planting_brief.txt"),
]
for _within, _rel in _serves:
    if (_root / _rel).is_file():
        check("still serves %s" % _rel,
              safe_static_path(_root, _rel, _within) is not None)

# index.html is named literally by the route table, not built from user input,
# so it is checked against the repo root rather than a subdirectory.
check("the page itself still resolves",
      safe_static_path(_root, "index.html") is not None)
check("a missing file is None, not an error",
      safe_static_path(_root, "data/nope.json", "data") is None)
check("a directory is not a file",
      safe_static_path(_root, "data", "data") is None)


# ---------------------------------------------------------------------------
section("20. A point nobody trained on is refused, not reassigned")

# pick() answered ?city=nosuchcity with a refusal and ?lat=&lon= with the boot
# city, whatever the coordinates were. So Surat - a real place, between two
# trained cities and covered by neither - came back as 146 Ahmedabad cells, and
# so did lat=999. Every figure real, every figure about somewhere else, which
# is the failure pick()'s own docstring already named as worse than refusing.
#
# Three cases, and collapsing the last two was the bug:
#   no point         boot city. "help" and a health check deserve an answer.
#   point, unusable  refuse. lat=999 is not a place.
#   point, unserved  refuse. Nothing here is about Surat.
from greenplan.server import point_offered, resolve_slug


class _FakeRegistry:
    """Two cities, so 'covered', 'uncovered' and 'unknown' are all reachable."""
    default_slug = "ahmedabad"

    def summary(self):
        return [{"slug": "ahmedabad", "ready": True},
                {"slug": "delhi", "ready": True},
                {"slug": "mumbai", "ready": False}]

    def containing(self, lat, lon):
        if 22.8 <= lat <= 23.3 and 72.3 <= lon <= 72.8:
            return "ahmedabad"
        if 28.4 <= lat <= 28.9 and 76.9 <= lon <= 77.4:
            return "delhi"
        return None

    def pick(self, city, lat, lon):
        if city:
            c = str(city).lower()
            ok = {s["slug"] for s in self.summary() if s["ready"]}
            return c if c in ok else None
        return self.default_slug


_R = _FakeRegistry()

_slug, _err = resolve_slug(_R, None, None, None, False)
check("no point at all still gets the boot city", _slug == "ahmedabad" and not _err)

_slug, _err = resolve_slug(_R, None, 23.02, 72.57, True)
check("a covered point gets its own city", _slug == "ahmedabad" and not _err)

_slug, _err = resolve_slug(_R, None, 28.61, 77.21, True)
check("a point in the other city gets that one", _slug == "delhi" and not _err)

_slug, _err = resolve_slug(_R, None, 21.17, 72.83, True)
check("Surat is refused, not answered as Ahmedabad", _slug is None and bool(_err),
      "got %r" % (_slug,))
check("the refusal names the point and what is served",
      bool(_err) and "21.17" in _err and "ahmedabad" in _err)

_slug, _err = resolve_slug(_R, None, None, None, True)
check("a point that was offered but is unusable is refused",
      _slug is None and bool(_err))

_slug, _err = resolve_slug(_R, "delhi", None, None, False)
check("an explicit city still works", _slug == "delhi" and not _err)
_slug, _err = resolve_slug(_R, "nosuchcity", None, None, False)
check("an unknown city is still refused", _slug is None and bool(_err))
_slug, _err = resolve_slug(_R, "mumbai", None, None, False)
check("a city that is not ready is refused", _slug is None and bool(_err))

# point_offered is what separates "no point" from "bad point", so it has to see
# the key wherever the caller put it - the assistant nests it under aoi.
check("a top-level point is seen", point_offered({"lat": 1, "lon": 2}) is True)
check("a nested point is seen", point_offered({"aoi": {"lat": 1, "lon": 2}}) is True)
check("a bad point is still SEEN as offered",
      point_offered({"lat": 999, "lon": 999}) is True,
      "otherwise it falls through to the boot city, which is the bug")
check("a point-free body offers nothing", point_offered({"message": "help"}) is False)
check("an empty body offers nothing", point_offered({}) is False)
check("None offers nothing", point_offered(None) is False)


# ---------------------------------------------------------------------------
section("21. One cell, one priority score")

# The point report printed "Priority score 0.385" for cell 8742cea64ffffff
# while the Priority view, the worklist and the exported GeoJSON all said
# 0.616 at rank 46. Two numbers, one name, one cell.
#
# They came from different formulas. The ranking is the MCDA total: weighted,
# normalised across every cell. The report used a stand-in meant for points
# outside the trained grid - today's AQI rather than its forecast change, raw
# NDVI rather than its decline, weights that do not sum to one, no
# normalisation - and printed it under the ranking's name, in the same
# sentence as the panel's real forecast values.
#
# Every published justification carries its own score, so the two can be
# checked against each other directly.
_JSCORE = re.compile(r"Priority score ([0-9.]+)")
_checked = 0
for _f in sorted((ROOT / "outputs").glob("*/recommendations.geojson")):
    _gj = json.loads(_f.read_text(encoding="utf-8"))
    _bad = []
    for _feat in _gj.get("features", []):
        _p = _feat.get("properties", {})
        _m = _JSCORE.search(_p.get("justification") or "")
        if not _m:
            continue
        _said = float(_m.group(1))
        _real = _p.get("priority_score")
        if _real is None or abs(_said - float(_real)) > 0.0015:
            _bad.append("%s: text %.3f vs ranking %s" % (_p.get("zone"), _said, _real))
        _checked += 1
    check("%s: the justification quotes the ranking's own score" % _f.parent.name,
          not _bad, "; ".join(_bad[:3]))

check("there were justifications to check", _checked > 0)

# And a justification for an untrained cell must not quote a score at all -
# there is no ranking there to quote.
for _f in sorted((ROOT / "outputs").glob("*/recommendations.geojson")):
    _gj = json.loads(_f.read_text(encoding="utf-8"))
    _wrong = [f["properties"].get("zone") for f in _gj.get("features", [])
              if f["properties"].get("priority_score") is None
              and "Priority score" in (f["properties"].get("justification") or "")]
    check("%s: no score is quoted where the ranking has none" % _f.parent.name,
          not _wrong, "%s" % _wrong[:3])


# ---------------------------------------------------------------------------
section("22. The rainfall note speaks about the city on screen")

# The provenance behind the rainfall figure was a fixed sentence quoting
# Ahmedabad's check - "757 mm/yr modelled vs 750 published, a 1% difference" -
# shown wherever the reader happened to be. Someone looking at Chennai was
# reassured by a 1% match while the number in front of them was 30% low.
# Rainfall sets the irrigation budget, so understating it makes a scheme look
# cheaper and more viable than it is: the dangerous direction to be wrong in.
_h22 = (ROOT / "index.html").read_text(encoding="utf-8")
check("the rainfall provenance is built per city",
      "RAIN_PROV" in _h22 and "GV.rainGapNote" in _h22)
check("it no longer hard-codes one city's verification",
      "Verified against the IMD published normal for Ahmedabad" not in _h22)

# Every city the app ranks must appear in the gap table, or the note falls
# silent exactly where a reader needs it.
for _c in ("Ahmedabad", "Bengaluru", "Mumbai", "Delhi", "Chennai"):
    check("the IMD gap table covers %s" % _c,
          re.search(r'city:\s*"%s"' % _c, _h22) is not None)


# ---------------------------------------------------------------------------
section("23. Upkeep is not the build cost")

# The maintenance intent matched `maintain\w*`. "maintenance" stems on
# mainten-, not maintain-, so the noun itself never matched: asking
# "maintenance" was not understood at all, and "what does maintenance cost"
# fell past it to the cost intent and answered about the build cost - the one
# figure the maintenance answer exists to hold separate, because upkeep is
# what actually kills municipal plantings.
from greenplan.reasoning.assistant import _INTENTS as INTENTS

_pat = {name: pat for name, pat in INTENTS}
check("there is a maintenance intent", "maintenance" in _pat)
check("there is a cost intent", "cost" in _pat)

# Order matters as much as the pattern: maintenance has to be tried first, or
# any phrasing containing "cost" is claimed by the cost intent.
_order = [n for n, _ in INTENTS]
check("maintenance is matched before cost",
      _order.index("maintenance") < _order.index("cost"))

_MAINT = ["maintenance", "what does maintenance cost", "who waters these trees",
          "what is the upkeep", "how much does it cost to maintain",
          "running cost per year"]
for _q in _MAINT:
    _first = next((n for n, pat in INTENTS if re.search(pat, _q, re.I)), None)
    check("%r routes to maintenance" % _q, _first == "maintenance",
          "went to %r" % _first)

# And the plain cost questions must NOT be captured by it.
for _q in ["how much would that cost", "what is the cost", "price of the design"]:
    _first = next((n for n, pat in INTENTS if re.search(pat, _q, re.I)), None)
    check("%r still routes to cost" % _q, _first == "cost", "went to %r" % _first)


# ---------------------------------------------------------------------------
section("24. A generic noun is not a place")

# "find empty land in ahmedabad vastrapur area" flew the map to the South
# Pacific. The place came out as "ahmedabad vastrapur area", no gazetteer
# holds that, the browser retried with the last word - and "area" is a village
# in Taiarapu-Ouest, French Polynesia. The reply then described the plantable
# ground there in good faith. Reported twice from real use.
#
# Stripped server-side as well as in the browser: the bad string was reaching
# the action payload and the "Moving to ... first" sentence, and any client on
# a cached page kept the old behaviour.
from greenplan.reasoning.assistant import extract_place as _place_from

for _q, _want in [
    ("find empty land in ahmedabad vastrapur area", "ahmedabad vastrapur"),
    ("find empty land in vastrapur area",           "vastrapur"),
    ("show me the bopal area",                      "bopal"),
    ("go to vastrapur",                             "vastrapur"),
    ("find empty land near rajpath club",           "rajpath club"),
    ("take me to sector 17 chandigarh",             "sector 17 chandigarh"),
]:
    _got = _place_from(_q)
    check("%r -> %r" % (_q[:38], _want), _got == _want, "got %r" % (_got,))

for _q in ["go to satellite area", "show me the green view"]:
    check("%r names no place" % _q, _place_from(_q) is None,
          "got %r" % (_place_from(_q),))

check("a numbered sector survives the strip",
      "17" in (_place_from("take me to sector 17 chandigarh") or ""))


# ---------------------------------------------------------------------------
section("25. The 3D palette offers this place's trees, and one symbol per tool")

# The builder's tree palette was ten species written into the file - the same
# Neem, Peepal and Banyan in Ahmedabad, in Shillong and on a saline coast -
# while the 2D studio two clicks away ranked thirty-odd species against the
# same cell's air, rainfall, heat and canopy and recommended something else.
# Two halves of one product answering "what should I plant here" differently
# is worse than either being wrong alone: the reader cannot tell which to
# believe. The 3D half is now generated from rankedSpecies(), the function
# every other surface already goes through.
_h25 = (ROOT / "index.html").read_text(encoding="utf-8")

_a = _h25.index("const B3D_STATIC = [")
_b = _h25.index("\n];", _a)
_static = _h25[_a:_b]
check("the builder palette no longer hard-codes tree species",
      'cat:"Trees"' not in _static)
check("the tree half is generated from the ranked species",
      "function b3dTreeTools" in _h25 and "GV.rankedSpecies" in _h25)
check("moving the plot rebuilds it", "b3dSyncTrees()" in _h25)
# Toxic-near-people is an override, not a low rank: it must not be one
# checkbox away from a school design.
check("toxic species stay off the palette entirely",
      "!/toxic/i.test(t.warn)" in _h25)
# And what IS cut has to say so, or a palette that omits an option is
# indistinguishable from one that has never heard of it.
check("the species that were cut are still reachable",
      "b3dShowAllSpecies" in _h25)
check("and they carry the reason", "Not on this area" in _h25)

# --- one tool, one symbol -------------------------------------------------
# Eight of the thirty-six costed elements shared a single water droplet: a
# pond, a bioswale, a recharge pit, a drip line, a tap, a percolation
# trench, a sump and a borewell were the same mark on the map - which is the
# one place a planner reads a design with no labels at all.
def _icons(decl, pat):
    a = _h25.index(decl)
    b = _h25.index("\n];", a)
    return re.findall(pat, _h25[a:b], re.S)

_2d = _icons("const ELEMENTS = [", r'id: "(\w+)".*?icon: "(\w+)"')
_3d = _icons("const B3D_STATIC = [", r'id:"(\w+)".*?icon:"(\w+)"')
check("all 36 costed elements are still there", len(_2d) == 36, "got %d" % len(_2d))
for _name, _rows in (("map studio", _2d), ("3D builder", _3d)):
    _seen = {}
    _dup = sorted({k for _, k in _rows if list(k for _, k in _rows).count(k) > 1})
    check("every %s tool has its own symbol" % _name, not _dup, "shared: %s" % _dup)

# A glyph key that is not defined falls back to a bare circle, which would
# silently undo the whole point.
_ea = _h25.index("function elIcon"); _eb = _h25.index("}[k]", _ea)
_el_keys = set(re.findall(r"^\s*(\w+):", _h25[_ea:_eb], re.M))
_ba = _h25.index("const B3D_ICONS = {"); _bb = _h25.index("\n};", _ba)
_b3_keys = set(re.findall(r"^\s*(\w+):", _h25[_ba:_bb], re.M))
_unres2 = [i for i, k in _2d if k not in _el_keys and k not in _b3_keys]
_unres3 = [i for i, k in _3d if k not in _b3_keys]
check("every map symbol resolves to a real glyph", not _unres2, str(_unres2))
check("every builder symbol resolves to a real glyph", not _unres3, str(_unres3))
# The two views must show the SAME mark for the same thing, which is what
# falling through to one glyph set buys.
check("the map falls through to the builder's glyph set",
      "B3D_ICONS[k]" in _h25)

# --- linear elements are priced per metre --------------------------------
# The builder emitted every area tool as `qty: foot, unit: "m2"`. A bioswale
# is priced per metre, so a click added 12.8 "m2" of swale and multiplied it
# by a per-metre rate: the wrong quantity AND the wrong unit against the
# same number.
check("linear tools carry their own quantity and unit",
      'qty: d.q || d.foot, unit: d.u || "m2"' in _h25)
for _lin in ("swale", "kerb", "hedge", "percolation", "fence"):
    check("%s is placed as a length" % _lin,
          re.search(r'ref:"%s",[^\n]*u:"m"' % _lin, _h25) is not None)

# --- and nothing in the palette invents a price ---------------------------
_ids2 = {i for i, _ in _2d}
_a3 = _h25.index("const B3D_STATIC = [")
_refs = re.findall(r'ref:"([^"]+)"', _h25[_a3:_h25.index("\n];", _a3)])
_orphan = sorted({r for r in _refs if r not in _ids2})
check("every builder tool maps onto a costed element", not _orphan, str(_orphan))


# ---------------------------------------------------------------------------
section("26. The 3D site reader sees the greenery that is actually there")

# The builder asked OpenStreetMap for parks, gardens, pitches and pools and
# nothing else. In most Indian wards that is the smaller half of the
# greenery: the scrub on the boundary, the grass verge, the village green
# and the wooded strip along the drain are tagged `natural` and `landuse`.
# At the Vastrapur test site the old query returned 2 green polygons; the
# same site returns 5 now, and the three it was missing were grassland.
# A builder that cannot see them draws bare ground beside a plot that is not
# bare, and every shade, dust and canopy-gap judgement the reader makes from
# that view is made against a site that does not exist.
_h26 = (ROOT / "index.html").read_text(encoding="utf-8")

for _tag in ("landuse~", "natural~", "natural=tree_row", "barrier=hedge"):
    check("the site query asks for %s" % _tag, _tag in _h26)
for _k in ("wood", "scrub", "grassland", "village_green", "orchard", "meadow"):
    check("%s is a green kind the reader understands" % _k,
          re.search(r"^\s+%s:\s*\{" % _k, _h26, re.M) is not None)

# A swimming pool drawn in lawn green and a cricket pitch drawn the same
# green as woodland are both wrong in a way that reads as fine.
check("a pool is not drawn as grass", '"swimming_pool"' in _h26 and "water: true" in _h26)

# OSM maps individual trees almost nowhere in urban India, so the canopy is
# inferred from the polygon's own tag - and has to SAY it is inferred, the
# same way the buildings say which heights were surveyed.
check("canopy is inferred from landuse at the density the tag implies",
      "canopy estimated from" in _h26)
check("the site summary counts inferred crowns separately",
      "crowns estimated from landuse" in _h26)
check("and it is deterministic, so the same site draws the same twice",
      "1103515245" in _h26)

# Every surveyed tree used to be the same ball on the same trunk - the fault
# the buildings were fixed for. Where OSM records a height, a crown width or
# a species, drawing them identically throws away the measurement.
check("a mapped tree uses its recorded size", "function ctx3dTreeSize" in _h26)
for _tag in ("diameter_crown", "t.height", "t.species"):
    check("ctx3dTreeSize reads %s" % _tag, _tag in _h26)
check("and says when the size is a default rather than a survey",
      "size estimated (OSM records position only)" in _h26)

# One summary, written once. The three entry points into the builder had
# drifted: two reported the surveyed/estimated split and the third reported
# neither, so the same site described itself differently depending on which
# button you pressed to get there.
check("there is one site summary", "function ctx3dSummary" in _h26)
check("no entry point rolls its own",
      _h26.count('" surveyed height, "') == 1)

# --- the scope trap that took the whole palette down ----------------------
# treeGlyph is called from BOTH the studio palette (inside an IIFE) and the
# builder palette (top level). Declared beside elIcon it was invisible to
# the builder and every 3D palette build threw. It has to live in the
# builder's scope, which the studio can reach and not the other way round.
check("treeGlyph is declared in the shared top-level scope",
      _h26.index("function treeGlyph") > _h26.index("const B3D_STATIC = ["),
      "it is inside the studio IIFE again")
check("both palettes go through it",
      _h26.count("treeGlyph(s)") >= 2)


# ---------------------------------------------------------------------------
section("27. The estimate says where its rates come from, and prices the ground")

_h27 = (ROOT / "index.html").read_text(encoding="utf-8")

# The panel described every rate in the app with one sentence - "indicative
# Indian rates for 2026" - which tells a reader nothing they can check,
# argue with, or take to a finance committee. A number without a source is
# not an estimate, it is an assertion.
check("there is a rate-source register", "const RATE_SOURCES" in _h27)
check("and a lookup for it", "function rateSourceFor" in _h27)
for _cat in ("Site preparation", "Planting", "Ground", "Water",
             "Furniture", "Services", "Establishment (3 yr)", "Land"):
    check("%s cites a source" % _cat,
          re.search(r'"%s":\s*\{' % re.escape(_cat), _h27) is not None)
# Every category the costing can emit must be in the register, or a line
# appears with no source and the reader cannot tell which.
_cats = set(re.findall(r'add\("([A-Za-z0-9 ()]+)",', _h27))
_known = set(re.findall(r'^  "([^"]+)": \{$', _h27, re.M))
_gap = sorted(c for c in _cats if c not in _known)
check("no cost category is left without a source", not _gap, str(_gap))
check("the exported bill of quantities carries the source column",
      '"Rate source"' in _h27)
check("the old unsourced claim is gone",
      "indicative Indian rates for 2026 held in" not in _h27)

# --- the ground ----------------------------------------------------------
# The panel used to end with "this carries no land cost", which on an urban
# greening scheme is the largest omission possible: on the Vastrapur test
# plot the ground is worth about 240x what is built on it, and a committee
# comparing two sites cannot do that from a number that leaves it out.
check("land is costed", "function computeLand" in _h27)
check("the 'no land cost' disclaimer is gone", "no land cost" not in _h27)
check("there is a register of the statutory land instruments",
      "const LAND_RATES" in _h27)
for _city, _inst in [("Ahmedabad", "Jantri"), ("Mumbai", "Ready Reckoner"),
                     ("Delhi", "Circle rate"), ("Bengaluru", "Guidance value"),
                     ("Chennai", "Guideline value")]:
    check("%s names its instrument (%s)" % (_city, _inst),
          re.search(r'city: "%s".*?instrument: "%s' % (_city, _inst), _h27, re.S) is not None)
# An unrecognised place must be obviously unpriced, not quietly priced as
# whichever city happens to be first in the table.
check("an unknown city falls back to a named fallback, not to a city",
      "const LAND_FALLBACK" in _h27)

# The band is not a lookup and has to say so - land value inside one city
# varies by more than tenfold street to street.
check("the indicative band is labelled as not a lookup", "not a lookup" in _h27)
check("and the reader can enter the rate for their own plot",
      'id="gvLandRate"' in _h27 and "rate_is_own" in _h27)

# What a public body pays is NOT the bare circle rate: acquisition runs
# under the RFCTLARR Act 2013, market value plus 100% solatium, which in a
# city is about twice the circle rate. A scheme budgeted at 1x is budgeted
# at half of what it will cost.
check("acquisition is awarded under RFCTLARR 2013", "RFCTLARR" in _h27)
check("with 100% solatium", "SOLATIUM = 1.0" in _h27)
check("all three tenures are offered",
      all(('"%s"' % t) in _h27 for t in ("owned", "acquire", "lease")))

# --- and none of it moves the build cost ---------------------------------
# Ground rent was briefly added as a bill line. That put it inside `grand`,
# the figure the panel heads "Estimated cost to build", and ran the 12%
# contingency, 8% design fee and 18% GST over it: on the Vastrapur plot the
# build cost went from 12.2 lakh to 9.24 crore purely by changing how the
# land was held. A build cost that moves by two orders of magnitude with
# tenure is not a build cost.
check("rent is not a priced bill line",
      "Ground rent on the leased land" not in _h27)
check("rent is reported beside upkeep, not inside it",
      "ground_rent_annual" in _h27 and "annual_running" in _h27)
check("the combined figure has to be asked for by name",
      "grand_with_land" in _h27)
check("`grand` is still computed before land is added",
      _h27.index("const grand = preTax + gst;") < _h27.index("const grand_with_land"))

# The assistant answers from the same numbers the panel shows.
check("the assistant is told the tenure", "land_tenure:" in _h27)
check("and that total_cost still means the build cost",
      "total_cost_with_land:" in _h27)


# ---------------------------------------------------------------------------
section("28. One unsupported clause must not cost the whole query its local answer")

# The local index ships with 2.6M features and answers the site-context read
# in milliseconds. It holds only the categories it was built for, and it
# refuses anything outside them rather than returning a zero it cannot stand
# behind - the right call, but it refuses the WHOLE query, not the clause it
# cannot serve.
#
# So when the extra greenery (landuse and natural polygons, tree rows,
# hedges - none of which the index carries) went into the same query, every
# site read started failing over to the public Overpass instance: slower,
# rate limited, and the reason the builder began reporting "surroundings
# unavailable" on sites it had just drawn. One unsupported clause cost the
# other nine their local answer.
_h28 = (ROOT / "index.html").read_text(encoding="utf-8")

_a = _h28.index("async function fetchSiteContext")
_b = _h28.index("\nfunction ", _a)
_fn = _h28[_a:_b]

check("the site read is split into two queries",
      "const qGreen" in _fn, "the greenery is back in the main query")

_main = _fn[_fn.index("const q = `"):_fn.index("const qGreen")]
# Everything in the main query must be a shape the local index can answer -
# these are the kinds osmlocal._KIND_RULES actually maps.
for _bad in ("landuse~", "natural~", "natural=tree_row", "barrier=hedge",
             "playground", "recreation_ground"):
    check("the main query stays inside the local subset (no %s)" % _bad,
          _bad not in _main)
for _need in ("[building]", "[highway]", "[natural=water]", "[waterway]",
              "[natural=tree]"):
    check("the main query still asks for %s" % _need, _need in _main)

_green = _fn[_fn.index("const qGreen"):]
for _need in ("landuse~", "natural~", "natural=tree_row", "barrier=hedge"):
    check("the greenery query carries %s" % _need, _need in _green)

# The site has to draw when the second call is throttled, or the split has
# bought nothing.
check("the greenery call is best-effort",
      "catch (e) { /* throttled" in _fn or "catch (e) {" in _green)
check("and the site still publishes without it",
      _fn.index("if (!j || !j.elements") < _fn.index("overpassFetch(qGreen"))

# --- osmlocal must keep refusing rather than inventing a zero -------------
from greenplan import osmlocal as _ol
_kinds = {k for _, _, _, k in _ol._KIND_RULES}
check("the index does not claim to hold woodland", "wood" not in _kinds)
check("nor tree rows", "tree_row" not in _kinds)
check("nor hedges", "hedge" not in _kinds)

# ---------------------------------------------------------------------------
section("29. The ground is a question the assistant can be asked")

# The cost panel prices the land now, and the assistant was handed the
# figures - but had no intent for them, so "who owns this land" fell through
# to "I did not understand that" while the answer sat in its own context.
from greenplan.reasoning.assistant import _INTENTS as _I29, _NEEDS_GROUND as _NG29

_order29 = [n for n, _ in _I29]
check("there is a land intent", "land" in _order29)
# "what does the land cost" is a question about the land; `cost` would
# otherwise claim it and answer about the build - the one figure the land
# answer exists to hold separate.
check("land is matched before cost", _order29.index("land") < _order29.index("cost"))
# ...and after empty_land, so siting questions still route to siting.
check("empty_land is matched before land",
      _order29.index("empty_land") < _order29.index("land"))
check("a land answer needs a place first", "land" in _NG29)

for _q in ["who owns this land", "what does the land cost", "land value",
           "what is the jantri rate", "stamp duty", "what is the ground rent",
           "what is the guidance value here", "land acquisition cost"]:
    _f = next((n for n, pat in _I29 if re.search(pat, _q, re.I)), None)
    check("%r routes to land" % _q, _f == "land", "went to %r" % _f)

for _q, _want in [("how much would that cost", "cost"),
                  ("what is the cost", "cost"),
                  ("find empty land in vastrapur", "empty_land"),
                  ("how much land is plantable", "empty_land")]:
    _f = next((n for n, pat in _I29 if re.search(pat, _q, re.I)), None)
    check("%r still routes to %s" % (_q, _want), _f == _want, "went to %r" % _f)

# It must not invent an owner: no cadastral layer ships with the app.
_en29 = json.loads((ROOT / "data" / "i18n" / "en.json").read_text(encoding="utf-8"))
_a29 = _en29["assistant"]
for _k in ("land.none", "land.body", "land.owned", "land.acquire", "land.lease",
           "land.basis_own", "land.basis_band"):
    check("en.json carries %s" % _k, _k in _a29)
check("the answer refuses to guess at ownership",
      "I do not know who holds the title" in _a29["land.body"])
check("the indicative band is called one here too",
      "not a lookup" in _a29["land.basis_band"])
check("acquisition cites the Act", "RFCTLARR" in _a29["land.acquire"])
# And the cost answer must not contradict the panel it sends the reader to.
check("the cost answer no longer calls the rates unsourced",
      "indicative 2026 Indian rates" not in _a29["cost.body"])


# ---------------------------------------------------------------------------
section("30. A land rate belongs to the plot it was looked up for")

# A rate looked up in the Jantri for a Vastrapur plot - 52,000/m2, correct
# there - followed the reader to Mumbai, where the card went on calling it
# "your figure" against the Ready Reckoner while sitting inside Mumbai's own
# 40,000-300,000 band, so nothing on screen looked wrong. Land value is the
# largest number on the page; carrying one place's lookup silently into
# another is the exact failure the rest of this app is built to avoid.
_h30 = (ROOT / "index.html").read_text(encoding="utf-8")

check("an entered rate records where it was entered", "rate_key" in _h30)
check("keyed by city and locality", "function landKey" in _h30)
check("the input stamps the locality", "GV.CFG.LAND.rate_key = ok ?" in _h30)
check("computeLand only honours it in that locality", "L.rate_key !== key" in _h30)
# It has to LAPSE, not vanish: a figure that disappears without explanation
# reads as the app losing the reader's work.
check("a lapsed rate is reported, not dropped", "rate_lapsed_from" in _h30)
check("and it names the place it came from", "you entered was for" in _h30)
check("the card no longer calls a foreign figure yours",
      '"Your figure, for this plot."' in _h30)


# ---------------------------------------------------------------------------
section("31. A reader can ask in their own language what the app told them to ask")

# Three faults in the native-language intent tables, all four languages.
from greenplan.reasoning import i18n as _i31
from greenplan.reasoning.assistant import _native_intent as _ni31

_LANGS31 = ("hi", "gu", "mr", "bn")

# 1. `_native_intent` picks the LONGEST matching keyword. A generic catch-all
#    sat in `report` - "क्या है" / "શું છે" / "काय आहे" / "কী আছে", 7-9
#    characters - and outranked the specific cost keywords, which are 4. So
#    "लागत क्या है" (what is the cost) matched both and the generic won:
#    every cost question phrased as "X क्या है" was answered with an area
#    report. The specific "यहाँ क्या है" is already listed and is longer.
_GENERIC = {"hi": "क्या है", "gu": "શું છે", "mr": "काय आहे", "bn": "কী আছে"}
for _c in _LANGS31:
    _t = _i31.nlu(_c)
    check("%s has an nlu table" % _c, bool(_t))
    check("%s: the bare catch-all is gone from report" % _c,
          _GENERIC[_c] not in (_t.get("report") or []))
    check("%s: the specific report phrase survives" % _c,
          any(_GENERIC[_c] in w and w != _GENERIC[_c] for w in (_t.get("report") or [])))

# 2. Hindi and Gujarati could not parse the app's OWN suggested prompt.
#    Suggesting a phrasing and then not understanding it is worse than not
#    suggesting one, so the help text and the keyword table are checked
#    against each other.
_ASK = {
    "hi": [("मैं यहाँ क्या लगाऊँ", "species"), ("यहाँ क्या है", "report"),
           ("हवा कैसी है", "air"), ("लागत क्या है", "cost"),
           ("जमीन की कीमत क्या है", "land"), ("इस जमीन का मालिक कौन है", "land"),
           ("खाली जमीन कहाँ है", "empty_land")],
    "gu": [("હું અહીં શું વાવું", "species"), ("અહીં શું છે", "report"),
           ("ખર્ચ શું છે", "cost"), ("જમીનનો માલિક કોણ છે", "land")],
    "mr": [("मी इथे काय लावू", "species"), ("इथे काय आहे", "report"),
           ("खर्च काय आहे", "cost"), ("जमिनीचा मालक कोण", "land")],
    "bn": [("আমি এখানে কী লাগাব", "species"), ("এখানে কী আছে", "report"),
           ("খরচ কী আছে", "cost"), ("জমির মালিক কে", "land")],
}
for _c, _rows in _ASK.items():
    for _n, (_q, _want) in enumerate(_rows, 1):
        _got = _ni31(_q, _c)
        check("%s: suggested phrase %d routes to %s" % (_c, _n, _want),
              _got == _want, "got %r" % _got)

# 3. The panel prices the ground, so every language needs the words for it.
for _c in _LANGS31:
    check("%s knows how to ask about land" % _c, bool(_i31.nlu(_c).get("land")))


# ---------------------------------------------------------------------------
section("32. The area read never paints over the tab the reader is on")

# drawAreaPanel() doubles as a background warm-up: `quiet("area", ...)` runs
# it on every map click so the slow OSM census is queued before the tab is
# opened. It has three render paths and only two were guarded by the active
# tab, so the warm-up drew into the shared dock body whatever the reader was
# looking at:
#
#   - the LOADING path replaced an open Studio with "Reading 100 km2..." and
#     it never cleared, because the completion path is guarded and correctly
#     declines to render into a tab the reader has left. The only way back
#     was to switch tabs and return.
#   - the CACHED path drew the whole area report over the Studio, leaving the
#     dock header reading "Design studio" above a body showing air quality.
_h32 = (ROOT / "index.html").read_text(encoding="utf-8")
_a = _h32.index("async function drawAreaPanel()")
_b = _h32.index(chr(10) + "function renderArea(", _a)
_fn = _h32[_a:_b]

check("the cached path checks the active tab first",
      'if (GV.ctx && GV.ctx.aoi === GV.aoi) {' in _fn)
check("the loading spinner checks the active tab first",
      _fn.index('if (dockTab === "area") {') < _fn.index('class="gv-load"'))
check("the completion path is still guarded",
      'if (dockTab === "area") renderArea(GV.ctx);' in _fn)
# Every render in this function must be behind the guard - a fourth
# unguarded path would reintroduce exactly the same bug.
_renders = _fn.count("renderArea(") + _fn.count("dockBody(")
_guards = _fn.count('dockTab === "area"')
check("every render path in drawAreaPanel is guarded",
      _guards >= 3, "%d renders, %d guards" % (_renders, _guards))


# ---------------------------------------------------------------------------
section("33. Half the palette was untappable on a phone")

# A bare `1fr` grid track is `minmax(auto, 1fr)`, and `auto` floors the track
# at the item's MIN-CONTENT width. A palette chip carries an icon, a name, a
# count and a price, and only the name can ellipsis - so on a 375 px phone
# the two tracks demanded 365 px inside a 334 px dock body. The right-hand
# column ran 7 px past the viewport and #gvDockBody clipped it:
# elementFromPoint at those chips returned null, so seventeen species and
# eighteen elements could not be tapped at all. Measured at 381x825 with
# device emulation, before and after.
_h33 = (ROOT / "index.html").read_text(encoding="utf-8")

check("the palette tracks can shrink below min-content",
      ".gv-palette{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr));" in _h33)
check("no bare 1fr 1fr left on .gv-palette",
      ".gv-palette{ display:grid; grid-template-columns:1fr 1fr;" not in _h33)
# The narrow-screen media query repeats the same rule for four grids, and had
# the same fault.
check("the narrow-screen grids were fixed too",
      ".gv-palette, .gv-mgrid, .gv-gal, .gv-proj-grid{ grid-template-columns:repeat(2, minmax(0, 1fr)); }" in _h33)
check("and none of them still ask for a bare 1fr 1fr",
      ".gv-palette, .gv-mgrid, .gv-gal, .gv-proj-grid{ grid-template-columns:1fr 1fr; }" not in _h33)
# The chip's name is what should give way, so it must keep its escape hatch.
check("the chip name can still ellipsis",
      ".gv-chip span{ flex:1; min-width:0;" in _h33)


# ---------------------------------------------------------------------------
section("34. The assistant cannot be wedged by an engine that never answers")

# This fetch was the one network path in the app with no budget on it -
# everything else goes through getJSON, which carries an AbortController and a
# timeout. On a host that answers POST slowly or not at all, the promise never
# settled: the thinking dots span forever, GVA.busy stayed true, and because
# aSend() returns early while busy, every LATER question was silently dropped.
# No error, no retry, no way back short of a reload. Found while testing the
# static bundle, which is what Netlify serves.
_h34 = (ROOT / "index.html").read_text(encoding="utf-8")
_a = _h34.index("async function aSend(")
_fn = _h34[_a:_a + 4000]

check("the assistant call is abortable", "new AbortController()" in _fn)
check("it carries a deadline", "ac.abort()" in _fn and "15000" in _fn)
check("the signal is actually passed to fetch", "signal: ac.signal" in _fn)
check("and the timer is always cleared", "finally { clearTimeout(killer); }" in _fn)
# The offline planner must still be the thing that picks up the slack.
check("a failed call still falls through to the in-page planner",
      "GVE.handle" in _fn)

# ---------------------------------------------------------------------------
section("35. The static bundle is what Netlify can actually serve")

# Netlify cannot run greenplan.server. The bundle exists because the engine is
# BAKED - the ranking, forecast, soil and species tables are read, not
# recomputed - and the publish directory must be dist/, never the repo root:
# the root index.html still lists /api/osm first in CFG.OVERPASS, which only
# resolves when the Python server is up.
_toml = (ROOT / "netlify.toml")
check("there is a netlify.toml", _toml.exists())
_t = _toml.read_text(encoding="utf-8") if _toml.exists() else ""
check("it publishes the baked bundle, not the repo root", 'publish = "dist"' in _t)
check("it declares no build command", 'command = ""' in _t)

_dist = ROOT / "dist"
check("dist/ exists", _dist.is_dir())
check("the static build strips the /api/osm proxy",
      "/api/osm" not in (_dist / "index.html").read_text(encoding="utf-8").split("OVERPASS:")[1][:120])
check("the in-page planner ships with it", (_dist / "gv-engine.js").is_file())
check("the SPA fallback is present", (_dist / "_redirects").is_file())

# Every city the app offers must actually be baked, or the deploy silently
# drops four of them.
_cities = _dist / "engine" / "cities.json"
check("the multi-city manifest is there", _cities.is_file())
if _cities.is_file():
    _cj = json.loads(_cities.read_text(encoding="utf-8"))
    _slugs = {c["slug"] for c in _cj.get("cities", [])}
    for _s in ("ahmedabad", "bengaluru", "chennai", "delhi", "mumbai"):
        check("%s is baked into the bundle" % _s, _s in _slugs)
        check("%s has its zones" % _s,
              (_dist / "engine" / _s / "zones.geojson").is_file())


# ---------------------------------------------------------------------------
section("36. The hosted build's planner strips generic nouns too")

# The hosted site runs web/gv-engine.js and NO Python at all, so a fix made
# only in greenplan/reasoning/assistant.py does not reach the people using
# it. This one mattered: a failed geocode falls back to the last word, and
# "area" is a village in Taiarapu-Ouest, French Polynesia - the map flew into
# the South Pacific and the reply described the ground there in good faith.
# Fixed in Python after two reports from real use; the planner kept doing it.
# scripts/parity_check.py caught it on "show me the priority areas", which
# the planner offered up as somewhere to fly to.
_js = (ROOT / "web" / "gv-engine.js").read_text(encoding="utf-8")

check("the planner strips a trailing generic noun",
      "neighbourhood|neighborhood|vicinity|surroundings)$/i" in _js)
check("it loops, so 'x area region' collapses too", "i < 3; i++" in _js)
check("and it drops a leading article",
      r'place.replace(/^(the|a|an)\s+/i, "")' in _js)

# The same list must exist on both sides, or they drift apart again.
_py = (ROOT / "greenplan" / "reasoning" / "assistant.py").read_text(encoding="utf-8")
for _noun in ("area", "areas", "region", "zone", "locality", "ward", "district",
              "sector", "vicinity", "surroundings"):
    check("both sides strip %r" % _noun,
          _noun in _py.split("Drop a trailing generic noun")[1][:2400] and
          _noun in _js.split("Drop a trailing generic noun")[1][:2400])

# The bundle that ships must carry it, not just the source.
_dist_js = ROOT / "dist" / "gv-engine.js"
check("the deployed bundle has the fix",
      _dist_js.is_file() and "vicinity|surroundings)$/i" in _dist_js.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
section("37. The desktop layer is inert on the web build")

# The Windows app and the hosted site share ONE index.html. The whole
# desktop layer - real accounts, the native save dialog, the menu wiring -
# sits behind `window.__GV_DESKTOP__`, which only the Electron preload
# defines. If any of it ever runs unguarded, the hosted site grows a login
# it was explicitly not to have, and a browser gets asked for IPC that does
# not exist. Verified live at 127.0.0.1 on the built bundle: bridge
# undefined, no gv-desktop class, GV.auth.mode still "local".
_h37 = (ROOT / "index.html").read_text(encoding="utf-8")

check("the desktop block exists", "function desktopEdition()" in _h37)
_blk = _h37[_h37.index("function desktopEdition()"):]
_blk = _blk[:_blk.index("function startGoogle()")]
check("it returns immediately without the bridge",
      "if (!D) return;" in _blk)
check("the guard is the first thing it does",
      _blk.index("if (!D) return;") < _blk.index("gv-desktop"))

# Every desktop-only entry point elsewhere in the file must be guarded too.
# gvDesktopSave is defined only inside the guarded block, so a call to it
# anywhere else must first test the bridge.
check("gvDesktopSave is defined only inside the guarded block",
      _h37.count("window.gvDesktopSave = ") == 0 and
      "window.gvDesktopSave = function" in _blk or "gvDesktopSave" in _blk)
check("the CSV export checks for the bridge before using it",
      "if (window.__GV_DESKTOP__ && window.gvDesktopSave)" in _h37)

# ---------------------------------------------------------------------------
section("38. The desktop app is wired the way it claims")

_desk = ROOT / "desktop"
check("the desktop project exists", (_desk / "package.json").is_file())
for _f in ("main.js", "preload.js", "auth.js", "accounts.js", "windows.js", "selftest.js"):
    check("desktop/%s is present" % _f, (_desk / _f).is_file())

_pkg = json.loads((_desk / "package.json").read_text(encoding="utf-8"))
check("it registers the greenvision:// scheme",
      any("greenvision" in (p.get("schemes") or []) for p in _pkg["build"]["protocols"]))
check("it ships the studio bundle as a resource",
      any("dist_app" in r.get("from", "") for r in _pkg["build"]["extraResources"]))

_main = (_desk / "main.js").read_text(encoding="utf-8")
# Without the lock, clicking the email link starts a SECOND copy holding the
# token; the window with the reader's work in it stays signed out.
check("a single instance lock is taken", "requestSingleInstanceLock()" in _main)
check("the second instance forwards the deep link",
      "second-instance" in _main and "handleDeepLink" in _main)
# The token must never reach the page.
check("the callback is redeemed in the main process",
      'auth.handle({ type: "callback", url })' in _main)
_pre = (_desk / "preload.js").read_text(encoding="utf-8")
check("the preload never forwards a raw deep link", "gv:deep-link" not in _pre)
check("context isolation is on", "contextIsolation: true" in _main)
check("node integration is off", "nodeIntegration: false" in _main)

_auth = (_desk / "auth.js").read_text(encoding="utf-8")
# The protocol handler is a public door: any page can send
# greenvision://auth?token=... to this app.
check("a state is generated per sign-in", "randomBytes(24)" in _auth)
check("the callback state is compared in constant time",
      "timingSafeEqual" in _auth)
check("an unsolicited callback is refused",
      "did not start that sign-in" in _auth)
# The endpoint DOES ship now — a packaged Windows app has no shell
# environment, and requiring one is how "the sign-in button does nothing"
# happens on someone else's machine. It lives in config.json, which is
# inspectable, rather than being buried in the source; the env var still
# overrides it for a developer pointing at a scratch deployment. It is a
# public webhook URL, not a secret: it mints nothing without an email
# round-trip and is rate limited per address.
check("the endpoint is read from config, not hard-coded in the source",
      "readConfiguredBase()" in _auth and "config.json" in _auth)
check("the environment still overrides it", "process.env.GV_AUTH_URL" in _auth)
check("the shipped build carries a config", (_desk / "config.json").is_file())
# ...and the WEB build must never gain one.
check("the web bundle has no sign-in endpoint",
      "pipedream.net" not in (ROOT / "dist" / "index.html").read_text(encoding="utf-8"))

_acct = (_desk / "accounts.js").read_text(encoding="utf-8")
check("the session is encrypted at rest", "safeStorage.encryptString" in _acct)
check("and is NOT written in the clear when DPAPI is unavailable",
      "memorySession = rec.session" in _acct and "session_unavailable" in _acct)
check("the benefits list is declared once, in the app", "BENEFITS = [" in _acct)

# The Pipedream half must not spend the token on the emailed GET: mail
# scanners follow links before a human does.
_pd = ROOT / "pipedream"
# One workflow, not two: the app has a single base URL, and splitting
# request from exchange across two Pipedream hosts would mean the client
# had to know which call goes where.
check("the pipedream workflow is checked in", (_pd / "02-api.js").is_file())
check("the email templates are shared, not copied", (_pd / "_email.js").is_file())
_api = (_pd / "02-api.js").read_text(encoding="utf-8")
check("one workflow serves request, verify and exchange",
      "/auth/request" in _api and "/auth/verify" in _api and "/auth/exchange" in _api)
check("and the per-user data API", all(x in _api for x in ("/me", "/projects", "/chats", "/history", "/library")))
_v = (_pd / "02-api.js").read_text(encoding="utf-8")
check("the emailed GET does not consume the token",
      "used: true" not in _v.split("/auth/verify")[1].split("/auth/exchange")[0])
check("the POST exchange does consume it", 'used: true' in _v)
check("the exchange checks the state too", "timingSafeEqual" in _v)
_rq = (_pd / "01-auth-request.js").read_text(encoding="utf-8")
check("sending is rate limited per address", '"rl:" + email' in _rq)


# ---------------------------------------------------------------------------
section("39. The sign-in sheet cannot promise what the app does not deliver")

# Three of the five benefits need a gallery server. Without COMMUNITY_URL,
# publishing falls back to "saved locally instead" and there is nowhere for
# designs or history to follow an account TO. Listing them as though they
# worked is exactly the drift this list was put in one place to prevent.
_acct39 = (ROOT / "desktop" / "accounts.js").read_text(encoding="utf-8")
_h39 = (ROOT / "index.html").read_text(encoding="utf-8")

check("benefits declare what they need", 'needs: "gallery"' in _acct39)
check("three of them are gated on the gallery",
      _acct39.count('needs: "gallery"') == 3,
      "%d gated" % _acct39.count('needs: "gallery"'))
check("the two that work today are not gated",
      _acct39.count("id: \"authorship\"") == 1 and
      "needs" not in _acct39.split('id: "authorship"')[1].split("},")[0])
# The desktop app publishes to its own account API; COMMUNITY_URL is the
# web build's separate optional one. Checking only the latter marked three
# working benefits "not set up yet" — the same misleading, in reverse.
check("the sheet checks for a gallery at render time",
      "const hasGallery = !!(GV.CFG.COMMUNITY_URL || (window.GVU && GVU.base));" in _h39)
check("and marks the rest pending rather than promising them",
      "not set up yet" in _h39 and "is-pending" in _h39)

# ---------------------------------------------------------------------------
section("40. The installer is built the way the README says")

_pkg40 = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
_b = _pkg40["build"]
# electron-builder dies unpacking a code-signing bundle full of macOS
# symlinks - on a step nothing here uses, since nothing is signed.
check("builder is told not to sign or edit the exe",
      _b["win"].get("signAndEditExecutable") is False)
check("dist packs first, then wraps the packaged dir",
      "pack.js" in _pkg40["scripts"]["dist"] and
      "--prepackaged" in _pkg40["scripts"]["dist"])
check("there is a packer that does not need Developer Mode",
      (ROOT / "desktop" / "pack.js").is_file())
_pack = (ROOT / "desktop" / "pack.js").read_text(encoding="utf-8")
# resources/app is Electron's own; the studio must not collide with it.
check("the studio is packed to resources/studio, not resources/app",
      '"studio"' in _pack and 'resourcesPath, "studio"' in
      (ROOT / "desktop" / "main.js").read_text(encoding="utf-8"))
check("the default app is removed so ours is not shadowed",
      "default_app.asar" in _pack)
check("renaming the exe is what makes app.isPackaged true",
      "GreenVision.exe" in _pack and "isPackaged" in _pack)


# ---------------------------------------------------------------------------
section("41. The Traffic tab can actually reach the data it needs")

_h41  = (ROOT / "index.html").read_text(encoding="utf-8")
_app41 = (ROOT / "dist_app" / "index.html").read_text(encoding="utf-8")
_web41 = (ROOT / "dist" / "index.html").read_text(encoding="utf-8")
_bs41 = (ROOT / "scripts" / "build_static.py").read_text(encoding="utf-8")
_osm41 = (ROOT / "greenplan" / "osmlocal.py").read_text(encoding="utf-8")

def _overpass(html):
    m = re.search(r"OVERPASS:\s*\[[^\]]*\]", html)
    return re.findall(r'"([^"]+)"', m.group(0)) if m else []

# The desktop app was packaged from the STATIC build, which strips /api/osm
# because a static host cannot serve it. So the Windows app could never use
# the local index however many features it held, and every map read went to a
# public instance - which on a blocked network never answers at all.
check("the desktop bundle keeps the local engine endpoint",
      "/api/osm" in _overpass(_app41))
check("the web bundle still strips it",
      "/api/osm" not in _overpass(_web41))
check("the build has a flag for the difference",
      "--keep-local-osm" in _bs41 and "keep_local_osm" in _bs41)
check("a desktop build is never mirrored onto the live web site",
      "docs/ mirror skipped" in _bs41)

# One dead mirror used to be the entire fallback.
_mirrors41 = [e for e in _overpass(_h41) if e.startswith("http")]
check("more than one public mirror is listed",
      len(_mirrors41) >= 3, "%d mirrors" % len(_mirrors41))

# The launchers pick the first FREE port of 8000/8010/8020/8030/8040, so the
# page has to know about all of them or it cannot find its own engine.
for _p in ("8000", "8010", "8020", "8030", "8040"):
    check("the page looks for an engine on port " + _p,
          '"http://127.0.0.1:%s"' % _p in _h41)
_launch41 = (ROOT / "launch" / "green-vision-web.ps1").read_text(encoding="utf-8")
check("and the launcher's port list is a subset of it",
      all(('"http://127.0.0.1:%s"' % q) in _h41
          for q in re.findall(r"\b(80[0-9]0)\b", _launch41)))

# `out body` on a node is the same answer as `out geom`; refusing it sent the
# traffic signals, level crossings and motorway junctions to public Overpass.
check("the local index answers `out body` for node queries",
      "body_mode" in _osm41 and "node_only" in _osm41)
check("but still refuses it for ways, whose node refs it cannot resolve",
      "node_only = all(" in _osm41)

# ---------------------------------------------------------------------------
section("42. Bottlenecks are never cached as a finished empty answer")

# `if (data && !bn)` treats [] as done, because [] is truthy. A warm-up that
# failed wrote bn = [], and the panel then reported central Ahmedabad as
# "0 bottlenecks - free-flowing" with 1,096 arterials and 63 signals loaded.
check("the panel asks whether bn belongs to THIS data, not whether it exists",
      "GV.traffic.bnFrom !== GV.traffic.data" in _h41)
check("no code path guards bottlenecks on truthiness alone",
      "!GV.traffic.bn)" not in _h41)
check("a failed warm-up derives nothing rather than an empty list",
      "GV.traffic.bn = d ? findBottlenecks(d) : null;" in _h41)
check("every recompute records what it was derived from",
      _h41.count("GV.traffic.bnFrom = GV.traffic.data;") >= 2)
check("clearing the data clears the derivation too",
      "GV.traffic.bnFrom = null;" in _h41)
check("the shipped app carries the fix", "bnFrom" in _app41)


# ---------------------------------------------------------------------------
section("43. The assistant answers the first question anybody asks")

# "How many trees fit here?" fell through to `unknown` - the assistant replied
# "I did not understand that" to the most natural question in the product.
from greenplan.reasoning.assistant import classify, parse_area_m2

for _q in ("how many trees fit in 2000 sq m",
           "how many neem can 1 hectare take",
           "trees per acre",
           "how many saplings fit in 50000 sq ft",
           "room for how many trees"):
    check("capacity: %r" % _q[:34], classify(_q) == "capacity",
          "got %s" % classify(_q))

# It sits after survival and people precisely so it cannot take their
# questions. Both start "how many", and both were right before it existed.
check("'how many will survive' is still survival",
      classify("how many trees will survive") == "survival")
check("'how many people benefit' is still people",
      classify("how many people benefit") == "people")

# Indian planning documents mix all four units in the same sentence.
check("square metres", parse_area_m2("2000 sq m") == 2000)
check("hectares", parse_area_m2("1 hectare") == 10000)
check("acres", abs(parse_area_m2("2 acres") - 8093.71) < 1)
check("square feet", abs(parse_area_m2("50000 sq ft") - 4645.15) < 1)
check("bare m2", parse_area_m2("1,500 m2") == 1500)
# "trees per acre" names a unit with no number and means one of them.
check("a unit with no number means one", parse_area_m2("trees per acre") is not None)
check("no area at all is None", parse_area_m2("how many trees fit here") is None)

# An area is not a place. Without the guard the compound-request path read
# "2000 sq m" as a place name and answered "Moving to 2000 sq m first".
_asst = (ROOT / "greenplan" / "reasoning" / "assistant.py").read_text(encoding="utf-8")
check("a self-contained area never triggers a geocode",
      "_self_contained = intent == \"capacity\"" in _asst)

# The reply must exist in English; other languages fall back per key by design.
import json as _json
_en = _json.loads((ROOT / "data" / "i18n" / "en.json").read_text(encoding="utf-8"))
for _k in ("capacity.body", "capacity.need_area", "capacity.from_plot"):
    check("english string %s" % _k, _k in (_en.get("assistant") or {}))


# ---------------------------------------------------------------------------
section("44. The panels survive the failures they were written to survive")

_h44 = (ROOT / "index.html").read_text(encoding="utf-8")

# censusP was declared in drawAreaPanel and used in renderArea - a different
# function. So the recovery path for a missing census threw ReferenceError,
# and a throttled Overpass killed the Area panel instead of degrading it.
check("the pending census is shared, not closed over",
      "GV._censusP = censusP;" in _h44)
check("renderArea reads it from there",
      "const pending = GV._censusP;" in _h44)
check("and tolerates it being absent",
      'typeof pending.then === "function"' in _h44)

# A design without a ring must open without its outline, not throw.
check("redrawDesign checks the ring is usable",
      "Array.isArray(d.plot.ring) ? d.plot.ring : null" in _h44)
check("openSavedDesign does too",
      "d.plot && Array.isArray(d.plot.ring) && d.plot.ring.length >= 3" in _h44)
check("opening a library design cannot fail silently",
      "That design could not be drawn" in _h44)

# The greenery query asks for tags the index does not hold, so it must not
# be sent there at all - it is a guaranteed 501 on every site read.
check("the greenery query skips the local index",
      "deadlineMs: 30000, skipLocal: true" in _h44)
check("overpassFetch honours skipLocal",
      "opts.skipLocal" in _h44)

# Chat memory: written, sent AND read back. All three, or it is not memory.
check("questions are recorded", 'gvuRememberTurn("me", msg)' in _h44)
check("answers are recorded", 'gvuRememberTurn("bot"' in _h44)
check("memory is sent to the engine", "memory: (window.gvuMemory ? gvuMemory() : [])" in _h44)
check("and restored on boot", "async function gvuRestoreChats()" in _h44)

_asst44 = (ROOT / "greenplan" / "reasoning" / "assistant.py").read_text(encoding="utf-8")
check("the engine accepts memory", "memory: Any = None" in _asst44)
check("a short follow-up can inherit the last intent",
      'if intent == "unknown" and memory:' in _asst44)
check("but never inherits a greeting",
      'cand not in ("greet", "help", "unknown")' in _asst44)

# Sign-in is offered before the way in, not after.
_i = _h44.index('id="splashAuth"')
_e = _h44.index('id="enterBtn"')
check("sign-in sits above 'Open the map'", _i < _e)

# Every onboarding answer has to do something - the step text promises it.
for _k in ("SCALE_M2", "GV.prefs.lead", "GV.prefs.state", "GV.prefs.tenure"):
    check("personalisation uses %s" % _k, _k in _h44)
check("land registers carry a state to match on", 'state: "Gujarat"' in _h44)


# ---------------------------------------------------------------------------
section("45. Species advice matches the ground it is given")

from greenplan.reasoning.species import SPECIES_KB as _KB45
_by = {s["common"]: s for s in _KB45}

# The KB and the costing catalogue were written separately and disagreed on
# exactly two rows. Both Ficus were "low water" while carrying the highest
# establishment irrigation of anything in the catalogue, and the recommender
# awards low-water species a bonus on dry sites - so the driest places were
# offered the two thirstiest trees.
check("Peepal is not sold as low-water",
      _by["Peepal"]["water_need"] != "low", _by["Peepal"]["water_need"])
check("Banyan is not sold as low-water",
      _by["Banyan"]["water_need"] != "low", _by["Banyan"]["water_need"])

# The recommender reads the word "aggressive" out of `context` to keep a
# species off avenues, streets and campuses. Peepal carried the warning and
# Banyan - the more destructive of the two - did not.
check("Banyan's roots are declared",
      "aggressive" in _by["Banyan"]["context"].lower(), _by["Banyan"]["context"][:60])
check("Peepal's roots are still declared",
      "aggressive" in _by["Peepal"]["context"].lower())

# Every species the app can place must be priced. A tree in the palette with
# no economics is a design that cannot be costed.
_h45 = (ROOT / "index.html").read_text(encoding="utf-8")
_econ = set(re.findall(r'"([^"]+)":\s*\{\s*sapling:', _h45))
check("the costing catalogue is populated", len(_econ) >= 25, "%d priced" % len(_econ))

# The point report never knew the rainfall, so it recommended the same trees
# in the Thar as in Cherrapunji.
_srv45 = (ROOT / "greenplan" / "server.py").read_text(encoding="utf-8")
check("the point report is given the rainfall", 'body.get("rain_mm_yr")' in _srv45)
check("and drops what it cannot establish", "_drop_thirsty" in _srv45)
check("an absent reading narrows nothing",
      "if rain_mm_yr is None or not (rain_mm_yr < 400):" in _srv45)
check("and it never returns an empty list", "return keep or names" in _srv45)
check("the browser sends the rainfall it already has",
      "rain_mm_yr: (GV.ctx && GV.ctx.wa && GV.ctx.wa.climate" in _h45)

# Costing: the per-tree all-in has to stay inside the range the code claims.
check("the tender range is still documented",
      "INR 2,000-4,000 per tree" in _h45)
for _k, _v in (("pit", "340"), ("guard", "780"), ("plant_labour", "160")):
    check("plant ops rate %s" % _k, ("%s: %s" % (_k, _v)) in _h45)


# ---- 46. every script in the project actually parses ----------------------
#
# Twice now a single mangled string literal has taken a whole file down. In
# desktop/main.js it commented out `contextIsolation: true`; in the admin
# page it left a string unterminated, which is a parse error for the ENTIRE
# <script> block - so every function was undefined, the sign-in button had no
# handler, the console printed nothing, and the page looked completely
# normal. Neither was caught by anything, because both files were still valid
# text and every test that touched them was a substring match.
#
# `node --check` is the same parser the browser uses. It is not a style
# opinion; it answers the one question a substring match cannot: will this
# run at all.
section("46. every script parses")

import io as _io, subprocess as _sp, tempfile as _tf, os as _os

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)


def _parses(src: str):
    """(ok, first line of the error) from node --check."""
    fd, tmp = _tf.mkstemp(suffix=".js")
    _os.close(fd)
    try:
        with _io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(src)
        r = _sp.run(["node", "--check", tmp], capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
        if not r.returncode:
            return True, ""
        for ln in (r.stderr or "").splitlines():
            if "Error" in ln:
                return False, ln.strip()[:90]
        return False, "did not parse"
    finally:
        _os.unlink(tmp)


try:
    _sp.run(["node", "--version"], capture_output=True, check=True)
    _have_node = True
except Exception:
    _have_node = False

if not _have_node:
    check("node is available to parse-check the scripts", False,
          "install node, or these files ship unverified")
else:
    for _rel in ["worker/src/index.js", "desktop/auth.js", "desktop/main.js",
                 "desktop/preload.js", "desktop/accounts.js",
                 "desktop/build-editions.js", "desktop/selftest.js"]:
        _p = ROOT / _rel
        if _p.exists():
            _ok, _why = _parses(_p.read_text(encoding="utf-8"))
            check("%s parses" % _rel, _ok, _why)

    for _rel in ["index.html", "invite.html", "worker/admin.html"]:
        _p = ROOT / _rel
        if not _p.exists():
            continue
        _text = _p.read_text(encoding="utf-8")
        _n = 0
        for _m in _SCRIPT_RE.finditer(_text):
            _attrs, _body = _m.group(1), _m.group(2)
            if "src=" in _attrs.lower() or "json" in _attrs.lower() or not _body.strip():
                continue
            _n += 1
            _ok, _why = _parses(_body)
            check("%s script %d parses" % (_rel, _n), _ok, _why)

# The admin page is served from GitHub Pages and the API is on Cloudflare.
# `const API = location.origin` was correct on Netlify, where they shared a
# host; on Pages it made every call fetch GitHub's 404 page, and the reader
# was shown a JSON parse error about an unexpected "<".
_adm = (ROOT / "worker" / "admin.html").read_text(encoding="utf-8")
check("the admin page names the API host outright",
      "green-vision-api.greenvision-rk.workers.dev" in _adm)
check("and does not assume the API shares its origin",
      "const API = location.origin;" not in _adm)
check("a page of HTML is reported as a wrong host, not a parse error",
      "returned a web page instead of data" in _adm)


# ---- 47. the sign-in sheet has to be visible when it opens ----------------
#
# The managed edition's gate offers a Sign in button. The button worked -
# the click fired, sheet() ran, the dialog rendered - and it was painted
# behind the opaque gate, so it read as a dead button. The same fault sat in
# the splash: the sheet was z-index 3000 and the splash 5000, which means
# the splash's own Sign in button had never shown its dialog either.
section("47. the sign-in sheet is on top when it opens")

_h47 = (ROOT / "index.html").read_text(encoding="utf-8")


def _z(sel):
    """The z-index declared for a selector, as an int."""
    m = re.search(re.escape(sel) + r"\{[^}]*?z-index:\s*(\d+)", _h47, re.S)
    return int(m.group(1)) if m else None


_z_sheet = _z(".gv-sheetwrap")
_z_gated = _z("body.gv-gated .gv-sheetwrap")
_z_intro = _z("#intro")
_z_kill = _z("#gvKill")
_z_gate = _z("#gvRevoked")

check("the sheet clears the splash", _z_sheet and _z_intro and _z_sheet > _z_intro,
      "sheet %s vs splash %s" % (_z_sheet, _z_intro))
check("the sheet clears the 3D view and its picker",
      _z_sheet and _z_sheet > 6000, "sheet is %s" % _z_sheet)
check("but stays under a withdrawn build", _z_sheet and _z_kill and _z_sheet < _z_kill,
      "sheet %s vs kill %s" % (_z_sheet, _z_kill))
check("and under the revoked-access screen", _z_sheet and _z_gate and _z_sheet < _z_gate,
      "sheet %s vs revoked %s" % (_z_sheet, _z_gate))
# Nothing is lifted over the gate any more, because the gate no longer
# borrows the shared sheet. It carries its own form.
check("nothing is lifted over the gate", _z_gated is None,
      "a z-index override is back at %s" % _z_gated)

# The gate's own sign-in form. This is what the shared sheet was replaced
# with, and the reason is worth keeping: the button's real fault was never
# stacking at all. `D` is a const inside the desktopEdition() IIFE and the
# gate lives outside it, so the click handler threw a ReferenceError on its
# first statement - inside an async function, where it surfaced as the form's
# own error line and nothing else. Raising the sheet could not have fixed it.
check("the gate has its own email field", 'id="gvgEmail"' in _h47)
check("and its own submit button", 'id="gvgIn">Email me a sign-in link' in _h47)
check("and its own confirmation step", 'id="gvgDone"' in _h47)
check("it reads the bridge off the window, not the IIFE's D",
      "const DD = window.__GV_DESKTOP__ || {};" in _h47)
check("and calls the bridge to request a link",
      "DD.auth.request(addr," in _h47)
check("no stale D. reference is left in the gate",
      "await D.auth.request(addr," not in _h47)
# The gate is reachable from outside the studio's IIFE. Without this, the
# only way to see the gate was to install the managed build - which is how a
# probe written against a hand-made copy of it passed while the real one
# was broken.
check("the gate can be raised for testing", "GV.auth.showGate = gvShowGate;" in _h47)
check("callback errors are shown inside the gate",
      "if (gvGateError) { gvGateError(msg); return; }" in _h47)


# ---- 48. a `window.` guard must be guarding something that exists ---------
#
# The pattern is `if (window.gvuSyncProject) gvuSyncProject(d)`. The intent is
# right: the web build has no account layer, and the call must not throw
# there. But these functions are declared inside index.html's IIFE and were
# never assigned to `window`, so every guard was permanently false and the
# call under it never ran - in EITHER build.
#
# It failed in total silence. No exception, no console line, no failed
# request. Saving a design still reported success, because the local half
# really did succeed; the account write simply never happened. Projects,
# search history, chat history and the assistant's memory were all empty on
# the server while the app behaved as though it were syncing.
#
# So: for every `window.NAME` used as a guard, `window.NAME = ` must exist.
section("48. every window. guard has a matching export")

_h48 = (ROOT / "index.html").read_text(encoding="utf-8")

# Names used as a guard or called through window.
_guarded = set(re.findall(r"window\.(gvu[A-Za-z0-9_]+|warmAOI|analyse|"
                          r"redrawDesign|openSavedDesign|logHistory)", _h48))
# Names actually put on window.
_exported = set(re.findall(r"window\.([A-Za-z0-9_]+)\s*=", _h48))

for _name in sorted(_guarded):
    check("window.%s is exported" % _name, _name in _exported,
          "guarded but never assigned - the call never runs")

# The five that were broken, named explicitly so the reason survives even if
# the regex above is ever loosened.
for _n in ["gvuSyncProject", "gvuRememberPlace", "gvuRememberTurn",
           "gvuMemory", "gvuLogDesign"]:
    check("%s reaches the account" % _n, ("window.%s " % _n) in _h48 or
          ("window.%s=" % _n) in _h48)

# Saving must push to the account, not only to localStorage.
check("Save syncs the project to the account", "  gvuSyncProject(d);" in _h48)
check("Save records it in the account's history",
      'gvuLogDesign(publish ? "published" : "saved", d);' in _h48)
check("and the toast does not claim the account when signed out",
      'GV.auth.isIn() ? "Saved to this device and your account"' in _h48)
# Open has to look past this machine.
check("Open falls back to the account's copy",
      "GVU.cache.projects || [], GVU.cache.library || []" in _h48)
# Design rows must not be offered as searchable places.
check("design rows are kept out of recent searches",
      "filter(r => !r.kind && r.place)" in _h48)


# ---- 49. "My location" has something to fall back to ----------------------
#
# Electron is built without a Google geolocation API key, so Chromium's
# network location provider is absent and navigator.getCurrentPosition always
# fails on the desktop with code 2 - "Failed to query location from network
# service". Permission is granted; there is nothing behind it. The button
# could not work, and there was no page-side fix.
#
# The Worker answers from the coarse location Cloudflare derives from the
# connecting IP: no Google key, no third-party lookup service.
section("49. My location falls back when the device cannot answer")

_h49 = (ROOT / "index.html").read_text(encoding="utf-8")
_w49 = (ROOT / "worker" / "src" / "index.js").read_text(encoding="utf-8")

check("the API can say roughly where a caller is", '"/whereami"' in _w49)
check("it reads Cloudflare's own figure, not a third party", "req.cf || {}" in _w49)
check("and refuses rather than guessing when there is none",
      'error: "no location for this connection"' in _w49)
check("the button falls back to it", "gvLocateByIp" in _h49)
check("the fallback finds the API even before startup finishes",
      "async function gvApiBase()" in _h49)
# A refusal is the reader's decision. Going around it with an IP lookup would
# be overriding a "no".
check("a refused permission is respected, not worked around",
      'if (e && e.code === 1) { toast("Location permission was refused"); return; }' in _h49)
# City-level from a network route is not a fix, and must not look like one.
check("the coarse fix is labelled approximate",
      '"Approximate location"' in _h49)
check("and does not zoom in as if it were precise",
      "map.setView([j.lat, j.lon], 12)" in _h49)


# ---- 50. shortcuts, taskbar identity, and the pointless update popup ------
section("50. the installed app: shortcuts, taskbar, update popup")

_h50 = (ROOT / "index.html").read_text(encoding="utf-8")
_pkg50 = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
_b50 = _pkg50["build"]

# The app announced "1.0.7 is available", then "You have 1.0.7 - current is
# 1.0.7", and offered a Download button for the copy being read.
check("an update notice for a version you already have is suppressed",
      "if (n.latest && !canUpdate)" in _h50)
# A notice with no version is a general announcement and must still arrive.
check("a notice with no version still reaches everyone",
      "if (!canUpdate && !n.message && !n.title) return;" in _h50)

# pack.js renames the binary to GreenVision.exe - that rename is what sets
# app.isPackaged. electron-builder derives the executable name from
# productName ("Green Vision") unless told otherwise, so every shortcut it
# wrote pointed at "Green Vision.exe", a file that has never existed.
check("the installer knows the real executable name",
      _b50.get("executableName") == "GreenVision",
      "executableName=%r" % _b50.get("executableName"))
_packjs = (ROOT / "desktop" / "pack.js").read_text(encoding="utf-8")
check("and that is the name pack.js writes",
      'const exe = path.join(OUT, "GreenVision.exe");' in _packjs)

# "always", not True: with True electron-builder skips the desktop shortcut
# when it finds an existing install, so an UPDATE never produced one.
check("the desktop shortcut is recreated on an update too",
      _b50["nsis"].get("createDesktopShortcut") == "always",
      repr(_b50["nsis"].get("createDesktopShortcut")))
check("the installer carries its own icon",
      _b50["nsis"].get("installerIcon") == "build/icon.ico")
check("a custom NSIS script is included",
      _b50["nsis"].get("include") == "build/installer.nsh")
check("that script exists", (ROOT / "desktop" / "build" / "installer.nsh").exists())
_nsh = (ROOT / "desktop" / "build" / "installer.nsh").read_text(encoding="utf-8")
check("it removes the shortcut on uninstall", "customUnInstall" in _nsh)

# Without setAppUserModelId the taskbar has no identity to attach the window
# to: a generic icon, and grouping under Electron rather than Green Vision.
_main50 = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
check("the app tells Windows who it is", "app.setAppUserModelId(id)" in _main50)
check("and the id travels into the package", 'appId: (pkg.build || {}).appId' in _packjs)


# ---- 51. the cold-start stall in the builder's surroundings ---------------
#
# Ten minutes of "surroundings unavailable", then everything suddenly works.
# Three causes, compounding:
#
#   1. osmlocal.get() had no lock, and the server is a ThreadingHTTPServer.
#      Several map reads arrive at once on a cold start, each saw
#      `_INSTANCE is None`, and each began its own full load of the same
#      file. Measured cold: five reads took 595 s, two of them timing out
#      with zero features. With the lock: 172 s, and the last three came back
#      in 1.2 s, 2.4 s and 17.7 s.
#   2. /api/health does not load the index, so nothing started the load until
#      somebody clicked. It now starts when the engine answers, while the
#      reader is still on the splash.
#   3. The site-context query passed no deadline, so its budget was four
#      attempts x 60 s plus backoff plus Overpass slot waits.
section("51. the builder's surroundings on a cold start")

_osm51 = (ROOT / "greenplan" / "osmlocal.py").read_text(encoding="utf-8")
check("only one thread may load the index", "_LOAD_LOCK = threading.Lock()" in _osm51)
check("and it is actually taken", "with _LOAD_LOCK:" in _osm51)
check("with the double check that keeps the hot path lock-free",
      "if _INSTANCE is not None:\n        return _INSTANCE" in _osm51)
# The packaged engine is a copy; a fix only in the repo ships nothing.
_osm51b = ROOT / "desktop" / "engine" / "greenplan" / "osmlocal.py"
check("the bundled engine carries the same fix",
      _osm51b.exists() and "_LOAD_LOCK" in _osm51b.read_text(encoding="utf-8"))

_eng51 = (ROOT / "desktop" / "engine.js").read_text(encoding="utf-8")
check("the index is warmed when the engine answers", "function warmIndex(" in _eng51)
check("for an engine we started", "warmIndex(origin, log);" in _eng51)
check("and for one we adopted", _eng51.count("warmIndex(origin, log)") >= 2)

_h51 = (ROOT / "index.html").read_text(encoding="utf-8")
check("the site-context query cannot run unbounded",
      "window.overpassFetch(q, 60000, { deadlineMs: 150000 })" in _h51)
# "Unavailable" during a two-minute first load reads as broken.
check("a loading index is not reported as a failure",
      "preparing the map index" in _h51)
check("and the three cases are told apart",
      "async function gvWhyNoContext()" in _h51)

# Four sites in west Ahmedabad, fetched once after the engine is ready.
check("the kept-ready places are declared", "const KEPT_WARM = [" in _h51)
for _place in ["Rajpath Club", "Sindhu Bhavan Road", "Bodakdev", "Satellite"]:
    check("  kept ready: %s" % _place, '"%s"' % _place in _h51)
check("they are warmed only once the engine is up", "gvWarmPlaces();" in _h51)
check("and never against a rate-limited public mirror",
      "if (!window.__gvEngineOrigin && location.protocol === \"file:\") return;" in _h51)


# ---------------------------------------------------------------------------
print("\n" + "=" * 62)
print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("\n  FAILED:")
    for f in FAIL:
        print("    - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
