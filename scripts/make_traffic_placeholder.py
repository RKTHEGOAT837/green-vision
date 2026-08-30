"""Generate the inert traffic placeholder for a city.

There is no free historical traffic source for Indian cities, so the third
stream in the panel is a flat constant with MCDA weight 0 — it keeps the
three-stream shape the engine expects and can never move a ranking. See the
header written into the file, and `mcda.weights.traffic_worsening: 0.0` in
every city config.

It is generated rather than committed per city because it carries no
information: the zone list and month range come from whichever real stream
the city already has.

    python scripts/make_traffic_placeholder.py --config config/delhi.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from greenplan.config import load_config  # noqa: E402

FLAT = 50.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default=None, help="defaults to the path in the config")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    out = Path(a.out) if a.out else Path(cfg.adapters.traffic.split(":", 1)[1].strip())
    out = cfg.resolve(out)

    # Take the grid and month range from a REAL stream, so the placeholder
    # lines up cell-for-cell and month-for-month with the data that matters.
    for src_spec in (cfg.adapters.aqi, cfg.adapters.green_cover):
        if src_spec.startswith("csv:"):
            src = cfg.resolve(src_spec[4:].strip())
            if src.is_file():
                break
    else:
        print("no real stream to take the grid from; export AQI or NDVI first")
        return 2

    df = pd.read_csv(src, comment="#")
    grid = df[["zone", "lat", "lon", "month"]].drop_duplicates()
    grid["traffic"] = FLAT

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# INERT PLACEHOLDER — NOT real data. No free historical traffic source exists.\n")
        fh.write("# Flat value on every cell/month; MCDA traffic weight is set to 0 so this\n")
        fh.write("# NEVER influences any ranking. Present only to keep the 3-stream shape.\n")
        grid.to_csv(fh, index=False)

    print("  %s: %d rows, %d cells, months %d..%d"
          % (out.name, len(grid), grid["zone"].nunique(),
             grid["month"].min(), grid["month"].max()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
