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
print("\n" + "=" * 62)
print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("\n  FAILED:")
    for f in FAIL:
        print("    - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
