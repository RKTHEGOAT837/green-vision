"""Run the full pipeline for one city and report what actually came out.

Adding a city to Green Vision is four data exports and this. It exists so the
claim "any city is one config file away" is something you can run rather than
something you have to take on trust:

    python scripts/osm_index.py --pbf ... --out ...        # optional, map panels
    python scripts/openmeteo_aqi_export.py --config config/<city>.yaml ...
    python scripts/modis_ndvi_export.py    --config config/<city>.yaml ...
    python scripts/soilgrids_export.py     --config config/<city>.yaml ...
    python scripts/make_traffic_placeholder.py --config config/<city>.yaml
    python scripts/build_city.py           --config config/<city>.yaml   <- here

What it prints is a checkable summary, not a success message: cell count,
green/amber/red split, the AQI and NDVI ranges the engine actually ingested,
and how many cells carry soil. If a city's numbers come out looking like
another city's, that is visible immediately rather than after a demo.

It also flags AQI readings past 500. The US index is undefined above that -
the EPA's own term is "Beyond the AQI" - and CAMS reports raw values well
past it during Delhi's pre-monsoon dust season. The engine keeps the value
because the RANKING needs the ordering, but a reader should be told.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("build_city")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--quiet", action="store_true", help="summary only")
    a = ap.parse_args(argv)

    if a.quiet:
        logging.disable(logging.INFO)

    from greenplan.config import load_config
    from greenplan.server import Engine

    cfg = load_config(a.config)

    # Fail early and clearly on a missing stream, rather than deep inside pandas.
    missing = []
    for label, spec in (("aqi", cfg.adapters.aqi),
                        ("ndvi", cfg.adapters.green_cover),
                        ("traffic", cfg.adapters.traffic)):
        if spec.startswith("csv:"):
            p = cfg.resolve(spec[4:].strip())
            if not p.is_file():
                missing.append("%s -> %s" % (label, p))
    if missing:
        print("  cannot build %s, these streams are not exported yet:" % cfg.city.name)
        for m in missing:
            print("    " + m)
        return 2

    eng = Engine(a.config)
    h = eng.health()
    r = eng.ranked

    print()
    print("  %s" % cfg.city.name)
    print("  " + "-" * (len(cfg.city.name)))
    print("  cells ranked   : %d" % h["zones"])
    print("  green / amber / red: %(green)d / %(yellow)d / %(red)d" % h["greenloss"])
    print("  memory records : %d" % h["memory_records"])
    # Soil coverage, with a warning when it is thin.
    #
    # SoilGrids legitimately masks water and dense built-up land, so partial
    # coverage is normal - Ahmedabad gets 120 of 146. But a run that was
    # interrupted also produces a short file, and the two look identical from
    # the outside. Anything under half the cells is worth a second look rather
    # than a silent pass: re-run the export and see whether the number is
    # stable. If it is, that is masking; if it grows, the first run was cut off.
    n_soil, n_cells = h["soil_cells"], h["zones"]
    print("  soil cells     : %d of %d" % (n_soil, n_cells))
    if 0 < n_soil < n_cells * 0.5:
        print("    NOTE: under half the cells have soil. Normal for a coastal or")
        print("          dense city (SoilGrids masks water and built-up land), but")
        print("          re-run scripts/soilgrids_export.py to confirm the count is")
        print("          stable rather than a truncated export.")
    elif n_soil == 0:
        print("    NOTE: no soil. Species are matched on pollution tolerance only.")

    aqi_hi = float(r.aqi_latest.max())
    beyond = int((r.aqi_latest > 500).sum())
    print("  aqi_latest     : %.0f - %.0f" % (r.aqi_latest.min(), aqi_hi))
    if beyond:
        print("    NOTE: %d of %d cells read above 500, which is off the top of the"
              % (beyond, len(r)))
        print("          US AQI scale. Kept for ranking, shown as \"500+\" in the studio.")
    print("  ndvi_latest    : %.3f - %.3f" % (r.ndvi_latest.min(), r.ndvi_latest.max()))
    print("  plantable      : %.2f - %.2f" % (r.plantable_space.min(), r.plantable_space.max()))
    print()
    print("  top 3 by priority:")
    cols = ["rank", "zone", "score", "aqi_latest", "ndvi_latest", "plantable_space"]
    print("    " + r[cols].head(3).to_string(index=False).replace("\n", "\n    "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
