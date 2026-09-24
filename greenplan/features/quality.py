"""Flag readings that cannot be right, before they reach the map.

This is NOT a model and it is not described as one. It is robust statistics:
a median and a median absolute deviation per zone, plus the physical bounds
each metric actually has. That choice is deliberate — an anomaly detector
trained on this panel would learn the panel's own faults as normal, and there
are 146 zones × 42 months here, which is far too little to train on and
plenty to describe.

Two kinds of wrong reading matter, and they fail differently:

  IMPOSSIBLE   outside what the quantity can physically be — a negative NDVI
               index where the engine's scale is 0–1, an AQI of 1,200 where
               the scale tops out at 500. These are data faults: a fetch that
               returned an error page, a unit mix-up, a fill value like -9999
               read as a number. They are always wrong.

  IMPLAUSIBLE  inside the bounds but far from what THIS zone has ever done —
               a cell whose NDVI has sat between 0.28 and 0.34 for three
               years reporting 0.71 for one month. Usually a cloud, a
               compositing artefact, or a mis-joined row. Sometimes real: a
               monsoon flush or a fire genuinely moves a cell. So these are
               flagged and counted, never deleted.

Nothing here modifies the panel. The engine keeps using the numbers it has;
this reports what looks wrong so the interface can say so and a reader can
judge. Silently repairing data is how a plausible wrong answer gets made.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# What each quantity can physically be. AQI is the US scale the engine uses;
# NDVI here is the MODIS index rescaled to 0–1 as the panel stores it.
BOUNDS: dict[str, tuple[float, float]] = {
    "aqi": (0.0, 500.0),
    "ndvi": (0.0, 1.0),
    "traffic": (0.0, 1.0),
}

# Values that mean "no data" in the source formats and must never be averaged.
FILL = (-9999.0, -999.0, -99.0, 9999.0)

# How many MADs from a zone's own median counts as implausible. 6 is loose on
# purpose: at 3 the monsoon flags every green cell every August, which trains
# a reader to ignore the warning, and a warning that is ignored is worse than
# none.
K = 6.0


def _mad(x: np.ndarray) -> float:
    """Median absolute deviation, scaled to compare with a standard deviation."""
    med = float(np.median(x))
    return 1.4826 * float(np.median(np.abs(x - med)))


def check_series(values, metric: str) -> dict[str, Any]:
    """Grade one zone's history of one metric."""
    lo, hi = BOUNDS.get(metric, (-np.inf, np.inf))
    v = np.asarray([np.nan if x is None else float(x) for x in values], dtype=float)

    fill = np.zeros(len(v), dtype=bool)
    for f in FILL:
        fill |= np.isclose(v, f, rtol=0, atol=1e-6)
    seen = ~np.isnan(v) & ~fill

    impossible = seen & ((v < lo) | (v > hi))
    good = seen & ~impossible

    implausible = np.zeros(len(v), dtype=bool)
    if good.sum() >= 8:
        g = v[good]
        med, mad = float(np.median(g)), _mad(g)
        # A flat series has MAD 0, and everything then looks infinitely far
        # from it. Fall back to the metric's own scale so a genuinely constant
        # cell does not flag its whole history the moment it moves once.
        scale = mad if mad > 1e-9 else 0.02 * (hi - lo)
        implausible = good & (np.abs(v - med) > K * scale)

    return {
        "n": int(len(v)),
        "missing": int(np.isnan(v).sum()),
        "fill_values": int(fill.sum()),
        "impossible": int(impossible.sum()),
        "implausible": int(implausible.sum()),
    }


def check_panel(panel: Any, metrics=("aqi", "ndvi")) -> dict[str, Any]:
    """Grade the whole loaded panel, per metric and in total.

    `panel` is the engine's long dataframe: one row per zone-month. Anything
    that does not look like that is reported as unreadable rather than
    guessed at, because a quality report that quietly checked nothing would
    be the worst possible output of this module.
    """
    out: dict[str, Any] = {"checked": False}
    try:
        cols = set(getattr(panel, "columns", []))
        if not {"zone"} <= cols:
            return out
        per: dict[str, Any] = {}
        for m in metrics:
            if m not in cols:
                continue
            tot = {"zones": 0, "missing": 0, "fill_values": 0,
                   "impossible": 0, "implausible": 0, "worst_zones": []}
            for zone, grp in panel.groupby("zone"):
                r = check_series(list(grp[m]), m)
                tot["zones"] += 1
                for k in ("missing", "fill_values", "impossible", "implausible"):
                    tot[k] += r[k]
                bad = r["impossible"] + r["implausible"] + r["fill_values"]
                if bad:
                    tot["worst_zones"].append((str(zone), bad))
            tot["worst_zones"] = [
                {"zone": z, "flagged": n}
                for z, n in sorted(tot["worst_zones"], key=lambda t: -t[1])[:5]
            ]
            per[m] = tot
        out = {"checked": True, "metrics": per,
               "flagged_total": sum(
                   per[m][k] for m in per
                   for k in ("impossible", "implausible", "fill_values")),
               "note": ("Robust median/MAD screening plus physical bounds. "
                        "Nothing is modified or dropped - implausible readings "
                        "are sometimes real.")}
    except Exception as exc:          # a report must never take the engine down
        log.debug("quality check failed: %s", exc)
        out = {"checked": False, "error": str(exc)[:120]}
    return out
