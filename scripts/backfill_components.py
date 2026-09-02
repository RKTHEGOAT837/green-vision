"""Add the MCDA score decomposition to recommendations.geojson files written
before the engine started emitting it.

Why a backfill rather than "just re-run the engine": a run costs a language
model pass over the top cells, and the ranking in these files is the one that
was reviewed. Re-running would change the justifications, and possibly the
order, purely to add a field the run had already computed.

Had already computed, and had already written down: recommendations.csv sits
next to the geojson and carries the c_* columns straight out of rank_zones.
So this copies them across on the zone id rather than deriving anything. The
first version of this script did try to derive them, re-running the percentile
normalisation over the geojson's own features, and it was wrong by up to 0.019
of score on Mumbai - small enough to look plausible in a table and large enough
to reorder cells. The check below is what caught that, so it stays: the five
weighted contributions must sum back to the priority_score the engine
published, for every cell, or the file is left alone. A decomposition that does
not add up to the published score is worse than none, because it reads as an
explanation.

    python scripts/backfill_components.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from greenplan.config import load_config  # noqa: E402

# The c_* columns are written at 4 decimals, so five weighted terms can drift a
# few units in the fourth place. Anything past this is a real disagreement.
TOL = 1e-3

KEYS = ("aqi_worsening", "traffic_worsening", "ndvi_decline",
        "low_green_cover", "plantable_space")


def city_configs() -> dict[str, Path]:
    """outputs/<slug>/ -> the config that produced it."""
    out = {}
    for cfg_path in sorted((ROOT / "config").glob("*.yaml")):
        try:
            cfg = load_config(cfg_path)
        except Exception:
            continue
        out[Path(cfg.run.outputs_dir).name] = cfg_path
    return out


def backfill(geo_path: Path, cfg_path: Path) -> str:
    gj = json.loads(geo_path.read_text(encoding="utf-8"))
    feats = [f for f in (gj.get("features") or []) if f.get("properties")]
    if not feats:
        return "empty"
    # Only the SCORED cells ever get a components block, so requiring one on
    # every feature made this look unfinished forever and rewrite the file on
    # every run.
    scored = [f for f in feats if f["properties"].get("priority_score") is not None]
    if scored and all("components" in f["properties"] for f in scored):
        return "already has components (%d scored cells)" % len(scored)

    csv_path = geo_path.with_name("recommendations.csv")
    if not csv_path.is_file():
        return "REFUSED: no recommendations.csv beside it to read components from"
    csv = pd.read_csv(csv_path)
    missing = [c for c in ("zone",) + tuple("c_" + k for k in KEYS) if c not in csv.columns]
    if missing:
        return "REFUSED: recommendations.csv lacks %s" % ", ".join(missing)
    by_zone = csv.set_index("zone")

    w = load_config(cfg_path).mcda.weights
    if any(k not in w for k in KEYS):
        return "REFUSED: config weights do not cover %s" % ", ".join(KEYS)

    staged, worst = [], 0.0
    for f in feats:
        p = f["properties"]
        zone = p.get("zone")
        if zone not in by_zone.index:
            return "REFUSED: zone %s is in the geojson but not the csv" % zone
        row = by_zone.loc[zone]
        comp, total = {}, 0.0
        for k in KEYS:
            v = float(row["c_" + k])
            if not math.isfinite(v):
                continue
            contrib = float(w[k]) * v
            comp[k] = round(contrib, 4)
            total += contrib
        published = p.get("priority_score")
        if published is None:
            # A cell outside the NDVI source's coverage. The engine scored it
            # NaN and it sits at the bottom of the ranking; there is no score to
            # decompose, and attaching a components block of zeroes would read
            # as "every criterion says this place is fine". It gets none.
            continue
        worst = max(worst, abs(total - float(published)))
        staged.append((p, comp))

    if worst > TOL:
        return "REFUSED: components sum to a score differing by up to %.4f (> %.4f)" % (worst, TOL)

    for p, comp in staged:
        p["components"] = comp
    gj["mcda_weights"] = dict(w)
    geo_path.write_text(json.dumps(gj, indent=2), encoding="utf-8")
    return "backfilled %d cells (max score error %.2e)" % (len(staged), worst)


def main() -> int:
    cfgs = city_configs()
    bad = 0
    for geo_path in sorted((ROOT / "outputs").glob("*/recommendations.geojson")):
        slug = geo_path.parent.name
        cfg_path = cfgs.get(slug)
        if cfg_path is None:
            print("  XX %-14s no config maps to this output dir" % slug)
            bad += 1
            continue
        try:
            msg = backfill(geo_path, cfg_path)
        except Exception as exc:
            msg = "ERROR: %s" % exc
        flag = "XX" if ("REFUSED" in msg or "ERROR" in msg) else "ok"
        bad += flag == "XX"
        print("  %s %-14s %s" % (flag, slug, msg))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
