"""Overture building footprints, written in this app's own index format.

WHY.

The ground check, the site finder and the 3D surroundings all ask
OpenStreetMap what is already built, and OSM's building coverage is thin in
exactly the neighbourhoods this tool is for. That thinness is not a detail:
"no building is mapped here" was being read as "nothing is built here", and
that is how a park came to be drawn over a dense block whose roofs are
plainly visible in the imagery underneath it.

Overture merges OSM with Microsoft's and Google's machine-extracted
footprints. Measured over one 4 x 3 km box across Bopal and Thaltej:

    OpenStreetMap   a few thousand buildings
    Overture           13,398 buildings

It is a superset of OSM rather than a competitor to it, which is why the
loader REPLACES the OSM buildings with these rather than adding them - the
same building would otherwise be counted twice and every overlap test would
see a phantom neighbour.

ABOUT HEIGHTS, HONESTLY.

Overture carries `height` and `num_floors` fields. Over the same box:

    height        0 of 13,398
    num_floors    1,356 of 13,398   (10%)

So footprints are a large real gain and heights are very nearly absent.
Google's Open Buildings 2.5D dataset does hold measured heights for India,
but it is published through Earth Engine rather than as files anyone can
fetch: the public Cloud Storage bucket holds the v1/v2/v3 POLYGONS, which
carry area and a confidence score and no height at all. There is no plain
HTTP route to it, so this script does not pretend to one. What is here is
what is actually obtainable; the 3D builder keeps inferring the rest from
footprint area and says that it is inferring.

USAGE

    python scripts/fetch_overture_buildings.py --config config/city.yaml
    python scripts/fetch_overture_buildings.py --bbox 72.45 22.9 72.7 23.15

It writes data/osm/buildings.overture.jsonl.gz, which the engine picks up on
its next start. Nothing else has to change.

It is a BUILD step, not a runtime one. It needs duckdb, which is not in the
shipped engine and is not meant to be:

    pip install duckdb
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("overture")

# The release to read. Pinned rather than "latest" on purpose: a build that
# silently changes its source between two runs cannot be compared with
# itself, and Overture republishes monthly.
RELEASE = "2026-08-19.0"
S3 = ("s3://overturemaps-us-west-2/release/%s/theme=buildings/type=building/*"
      % RELEASE)

OUT_NAME = "buildings.overture.jsonl.gz"

# A footprint smaller than this is a shed, a canopy or an extraction
# artefact. They are numerous, they are not obstacles at park scale, and
# keeping them triples the file for no decision anybody makes differently.
MIN_AREA_M2 = 12.0

# Ring detail. Overture footprints from machine extraction carry far more
# vertices than a 5 m obstacle test can use; the bounding box is what the
# site finder actually compares against, so the ring is simplified to keep
# the file honest about its own precision rather than implying survey grade.
RING_MAX_PTS = 24


def _ring_area_m2(ring: list[list[float]]) -> float:
    """Planar area of a lat/lon ring, in m2. Equirectangular is well under
    1% at building scale."""
    if len(ring) < 3:
        return 0.0
    lat0 = math.radians(ring[0][0])
    mx = 111320.0 * math.cos(lat0)
    my = 111320.0
    a = 0.0
    for i in range(len(ring)):
        p, q = ring[i], ring[(i + 1) % len(ring)]
        a += (p[1] * mx) * (q[0] * my) - (q[1] * mx) * (p[0] * my)
    return abs(a / 2.0)


def _thin(ring: list[list[float]], keep: int = RING_MAX_PTS) -> list[list[float]]:
    """Evenly spaced vertices, first one kept. Not Douglas-Peucker: the
    consumer compares bounding boxes, and an even sample preserves the box
    while a tolerance-based simplifier can shave a corner off it."""
    if len(ring) <= keep:
        return ring
    step = len(ring) / float(keep)
    return [ring[int(i * step)] for i in range(keep)]


def _rings_from_wkt(wkt: str) -> list[list[list[float]]]:
    """The outer ring of a POLYGON or of each part of a MULTIPOLYGON.

    Holes are dropped. A courtyard is not somewhere a park may be sited
    either - the building is still there around it - so the outer ring is
    the honest obstacle.
    """
    if not wkt:
        return []
    out: list[list[list[float]]] = []
    text = wkt.strip()
    upper = text.upper()
    if upper.startswith("MULTIPOLYGON"):
        body = text[text.index("(") + 1:text.rindex(")")]
        parts, depth, start = [], 0, 0
        for i, ch in enumerate(body):
            if ch == "(":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    parts.append(body[start:i + 1])
        chunks = parts
    elif upper.startswith("POLYGON"):
        chunks = [text[text.index("("):]]
    else:
        return []

    for chunk in chunks:
        inner = chunk
        # the first bracketed group of a polygon is its outer ring
        first = inner.find("(", 1)
        if first == -1:
            continue
        depth, end = 0, len(inner)
        for i in range(first, len(inner)):
            if inner[i] == "(":
                depth += 1
            elif inner[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        coords = inner[first + 1:end]
        ring: list[list[float]] = []
        for pair in coords.split(","):
            bits = pair.strip().split()
            if len(bits) < 2:
                continue
            try:
                lon, lat = float(bits[0]), float(bits[1])
            except ValueError:
                continue
            ring.append([round(lat, 6), round(lon, 6)])
        if len(ring) >= 4:
            out.append(ring)
    return out


def fetch(bbox: tuple[float, float, float, float], out_path: Path) -> int:
    import duckdb  # noqa: PLC0415 - a build dependency, never a runtime one

    west, south, east, north = bbox
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")

    # bbox.* are the parquet's own partition-friendly columns; filtering on
    # them is what keeps this from reading the planet.
    q = f"""
        SELECT ST_AsText(geometry) AS wkt,
               height,
               num_floors,
               names.primary AS name,
               class
        FROM read_parquet('{S3}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {west} AND {east}
          AND bbox.ymin BETWEEN {south} AND {north}
    """
    log.info("reading Overture %s over %.3f,%.3f -> %.3f,%.3f",
             RELEASE, west, south, east, north)
    log.info("this reads remote parquet and takes minutes, not seconds")
    t0 = time.time()
    rows = con.execute(q).fetchall()
    log.info("  %s rows in %.0fs", f"{len(rows):,}", time.time() - t0)

    kept = 0
    small = 0
    with_h = 0
    with_f = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        for wkt, height, floors, name, cls in rows:
            for ring in _rings_from_wkt(wkt):
                area = _ring_area_m2(ring)
                if area < MIN_AREA_M2:
                    small += 1
                    continue
                lat = sum(p[0] for p in ring) / len(ring)
                lon = sum(p[1] for p in ring) / len(ring)
                rec = {
                    "k": "building",
                    "t": "w",
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "nm": name or None,
                    "g": _thin(ring),
                }
                # Only when they are real. A null height written as a number
                # is the exact mistake this whole file exists to avoid.
                if height is not None:
                    try:
                        rec["ht"] = round(float(height), 1)
                        with_h += 1
                    except (TypeError, ValueError):
                        pass
                if floors is not None:
                    try:
                        rec["fl"] = int(floors)
                        with_f += 1
                    except (TypeError, ValueError):
                        pass
                if cls:
                    rec["bt"] = str(cls)
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                kept += 1

    mb = out_path.stat().st_size / 1e6
    log.info("wrote %s  (%s buildings, %.1f MB)", out_path, f"{kept:,}", mb)
    log.info("  dropped %s under %.0f m2", f"{small:,}", MIN_AREA_M2)
    log.info("  height known for %s (%.1f%%), floors for %s (%.1f%%)",
             f"{with_h:,}", 100.0 * with_h / max(kept, 1),
             f"{with_f:,}", 100.0 * with_f / max(kept, 1))
    if with_h == 0:
        log.warning("  no heights at all in this extract - the 3D builder will "
                    "keep inferring them from footprint area and keep saying so")
    return kept


def bbox_from_config(path: Path) -> tuple[float, float, float, float]:
    import yaml  # noqa: PLC0415
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    city = cfg.get("city", {})
    bb = city.get("bbox")
    if not bb or len(bb) != 4:
        raise SystemExit(f"{path} has no city.bbox")
    return tuple(float(v) for v in bb)            # type: ignore[return-value]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", help="a city yaml to take the bbox from")
    ap.add_argument("--bbox", nargs=4, type=float,
                    metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    ap.add_argument("--out", default=str(ROOT / "data" / "osm" / OUT_NAME))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.bbox:
        bbox = tuple(args.bbox)                   # type: ignore[assignment]
    elif args.config:
        bbox = bbox_from_config(Path(args.config))
    else:
        raise SystemExit("give --bbox or --config")

    fetch(bbox, Path(args.out))                   # type: ignore[arg-type]


if __name__ == "__main__":
    main()
