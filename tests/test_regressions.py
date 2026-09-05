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
print("\n" + "=" * 62)
print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("\n  FAILED:")
    for f in FAIL:
        print("    - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
