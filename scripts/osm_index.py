"""Build a local, queryable OSM index from a Geofabrik extract.

Why this exists
---------------
Every map feature in the studio — the area census, the traffic bottlenecks,
the 3D builder's real buildings — came from the public Overpass instance at
overpass-api.de. That instance allocates TWO SLOTS PER IP and answers HTTP 429
the moment you want a third, which is not a bug in Overpass: it is a free
service protecting itself. But it means the product's core panels were a
coin-toss at peak hours, and there is no paid tier or key that changes it.

Of the seven public Overpass endpoints measured from a browser, exactly two
serve Indian data with CORS: overpass-api.de and maps.mail.ru. One is rate
limited; the other is operated by a Russian company, which is a dependency
this project should not have to explain.

So: the same OSM data, from the same distributor the world uses (Geofabrik),
indexed once onto this machine and served locally with no limit at all. It
also makes the studio work with no internet, which was always the claim on
the tin — "a municipal laptop with no install rights and no outbound access".

What it keeps
-------------
Only what the app actually asks for. The western-zone extract is 220 MB of
PBF describing millions of features; the app queries nine categories inside a
few km of a point. Everything else is dropped at parse time, which is what
keeps the index small enough to hold in memory and fast enough to answer
without a spatial database.

    buildings   geometry, height, building:levels, name, building type
    highways    geometry, name, highway class
    leisure     park / garden / nature_reserve
    natural     water, tree
    waterway    any
    amenity     school / college / university / kindergarten,
                hospital / clinic / doctors
    landuse     industrial / quarry / landfill

Coverage, and what happens outside it
-------------------------------------
An extract covers what it covers. Ask about a point outside the indexed bbox
and the local service REFUSES (HTTP 501) rather than answering — the browser
then falls through to the public Overpass instance, so those cities keep
working exactly as they did before, rate limit and all. Answering "0 buildings"
from an extract that simply does not contain that city would be a false
reading dressed as a measurement — the same failure that made
overpass.osm.ch unusable (HTTP 200, zero elements, for all of India).

Geofabrik's India zones, and the cities each unlocks:

    western-zone    220 MB  Gujarat, Maharashtra, Goa  -> Ahmedabad, Surat,
                            Vadodara, Rajkot, Mumbai, Pune, Nashik, Nagpur
    northern-zone   223 MB  Delhi, Punjab, Haryana, UP, Rajasthan, HP, J&K
    southern-zone   559 MB  Karnataka, Tamil Nadu, Kerala, Andhra, Telangana
                            -> Bengaluru, Chennai, Hyderabad, Kochi
    eastern-zone            West Bengal, Odisha, Bihar, Jharkhand -> Kolkata
    central-zone            Madhya Pradesh, Chhattisgarh
    north-eastern-zone      Assam and the seven sisters

Run once (about 3-6 minutes per zone on a laptop):

    python scripts/osm_index.py --pbf data/osm/western-zone-latest.osm.pbf                                 --out data/osm/index.jsonl.gz

Several zones append into ONE index, so widening coverage means adding a file
rather than starting over:

    python scripts/osm_index.py         --pbf data/osm/western-zone-latest.osm.pbf              data/osm/northern-zone-latest.osm.pbf         --out data/osm/index.jsonl.gz

Then start the server as usual; it picks the index up automatically,
`/api/osm` answers the Overpass subset the studio speaks, and /api/health
reports how many features loaded and the bbox they cover.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import sys
import re
import time
from pathlib import Path

import osmium

log = logging.getLogger("osm_index")

# --- what we keep -----------------------------------------------------------
# Each entry is (kind, predicate). `kind` is the label the query layer filters
# on, so it must match the vocabulary in greenplan/osmlocal.py.
AMENITY_SCHOOL = {"school", "college", "university", "kindergarten"}
AMENITY_HEALTH = {"hospital", "clinic", "doctors"}
LEISURE_GREEN = {"park", "garden", "nature_reserve"}
# Sports and water surfaces. The 3D builder draws these as ground, and the
# site finder must not put a plot on top of one, so they are their own kind
# rather than being folded into "park" — a pitch is not a park.
LEISURE_SPORT = {"pitch", "swimming_pool", "track", "golf_course", "sports_centre"}
LANDUSE_IND = {"industrial", "quarry", "landfill"}

# Geometry is stored for EVERY road, not just arterials. The first version
# kept it only for motorway..secondary to save space, which was fine for the
# traffic panel and quietly broke the 3D builder: a neighbourhood rendered
# with its arterials and none of its actual streets. Whether a feature needs
# geometry is a property of the feature, not of the panel that asked first.


def _f(v: str | None) -> float | None:
    """OSM height/level strings are free text: '12', '12 m', '12.5m', ''."""
    if not v:
        return None
    s = "".join(c for c in str(v).strip() if c.isdigit() or c in ".-")
    try:
        f = float(s)
        return f if 0 < f < 1000 else None
    except ValueError:
        return None


ROOT_DIR = Path(__file__).resolve().parent.parent

# Coordinate precision. Six decimals is ~0.11 m, which is finer than OSM's own
# survey accuracy and finer than anything drawn at city zoom; five is ~1.1 m
# and measured 16% smaller. The extra digit was buying nothing.
COORD_DP = 5

# Douglas-Peucker tolerance, in metres. Long polylines dominate the file -
# waterways average 86 points each and coastlines far more - and most of those
# points describe wiggles well under a metre. At 2 m, 34% of all points go and
# nothing visibly changes at the zoom levels the studio draws.
SIMPLIFY_M = 2.0


def _hav_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def _simplify(pts, eps_deg):
    """Douglas-Peucker, iterative so a 5,000-point coastline cannot blow the
    recursion limit - which a recursive version does, on real data."""
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ay, ax = pts[a]
        by, bx = pts[b]
        dy, dx = by - ay, bx - ax
        den = dy * dy + dx * dx
        best_i, best_d = -1, eps_deg
        for i in range(a + 1, b):
            py, px = pts[i]
            if den == 0:
                d = math.hypot(py - ay, px - ax)
            else:
                t = ((py - ay) * dy + (px - ax) * dx) / den
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                d = math.hypot(py - (ay + t * dy), px - (ax + t * dx))
            if d > best_d:
                best_i, best_d = i, d
        if best_i >= 0:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [pts[i] for i in range(n) if keep[i]]


def _resolve_near(items):
    """--near values into (lat, lon). Accepts a config path or 'lat,lon', so
    one flag serves both "the cities I serve" and "this spot"."""
    out = []
    for it in items or []:
        m = re.match(r"^\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$", str(it))
        if m:
            out.append((float(m.group(1)), float(m.group(2))))
            continue
        try:
            sys.path.insert(0, str(ROOT_DIR))
            from greenplan.config import load_config
            cfg = load_config(it)
            lon0, lat0, lon1, lat1 = cfg.city.bbox
            out.append(((lat0 + lat1) / 2.0, (lon0 + lon1) / 2.0))
            log.info("  --near %-24s %s (%.4f, %.4f)", it, cfg.city.name, out[-1][0], out[-1][1])
        except Exception as exc:
            log.warning("  --near %s ignored: %s", it, exc)
    return out


class Collector(osmium.SimpleHandler):
    """One pass over the PBF, writing matched features straight out.

    Geometry comes from osmium's own location cache rather than a second pass,
    which is why this needs `--index` on the reader (see main). Ways whose
    nodes fall outside the extract are skipped rather than emitted half-drawn.
    """

    def __init__(self, out, near=None, radius_km=25.0) -> None:
        super().__init__()
        self.out = out
        # Bounding at INDEX time, not load time, is what makes the file
        # shippable. The loader can already discard what it does not need, but
        # it still has to stream the whole thing to do it - and a gigabyte
        # cannot go near a static host at all.
        self.near = list(near or [])
        self.radius_km = radius_km
        self.dropped_far = 0
        self.pts_in = 0
        self.pts_out = 0
        self.n = 0
        self.kept = 0
        self._t0 = time.time()

    def _emit(self, rec: dict) -> None:
        if self.near:
            la, lo = rec.get("lat"), rec.get("lon")
            if la is None or lo is None:
                return
            if not any(_hav_km(f[0], f[1], la, lo) <= self.radius_km for f in self.near):
                self.dropped_far += 1
                return
        g = rec.get("g")
        if g and len(g) > 2:
            self.pts_in += len(g)
            g = _simplify([tuple(q) for q in g], SIMPLIFY_M / 111320.0)
            rec["g"] = [[a, b] for a, b in g]
            self.pts_out += len(g)
        elif g:
            self.pts_in += len(g)
            self.pts_out += len(g)
        self.out.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.kept += 1

    # -- nodes: trees and the point amenities ------------------------------
    def node(self, n) -> None:
        self.n += 1
        t = n.tags
        kind = None
        if t.get("natural") == "tree":
            kind = "tree"
        elif t.get("amenity") in AMENITY_SCHOOL:
            kind = "school"
        elif t.get("amenity") in AMENITY_HEALTH:
            kind = "health"
        elif t.get("highway") == "traffic_signals":
            kind = "signal"
        elif t.get("railway") == "level_crossing":
            kind = "crossing"
        elif t.get("highway") == "motorway_junction":
            kind = "ramp"
        if kind is None:
            return
        self._emit({"k": kind, "t": "n", "lat": round(n.location.lat, COORD_DP),
                    "lon": round(n.location.lon, COORD_DP),
                    "nm": t.get("name") or None})

    # -- ways: buildings, roads, parks, water, landuse ---------------------
    def way(self, w) -> None:
        self.n += 1
        t = w.tags
        kind = geom_wanted = None

        if t.get("building"):
            kind, geom_wanted = "building", True
        elif t.get("highway"):
            kind, geom_wanted = "highway", True
        elif t.get("leisure") in LEISURE_GREEN:
            kind, geom_wanted = "park", True
        elif t.get("leisure") in LEISURE_SPORT:
            kind, geom_wanted = "sport", True
        elif t.get("natural") == "water":
            kind, geom_wanted = "water", True
        elif t.get("waterway"):
            kind, geom_wanted = "waterway", True
        elif t.get("amenity") == "parking":
            kind, geom_wanted = "parking", True
        elif t.get("railway"):
            kind, geom_wanted = "railway", True
        elif t.get("landuse") in LANDUSE_IND:
            kind, geom_wanted = "industrial", True
        if kind is None:
            return

        # Centroid always; full geometry only where something draws it.
        try:
            pts = [(round(nd.lat, COORD_DP), round(nd.lon, COORD_DP)) for nd in w.nodes if nd.location.valid()]
        except (osmium.InvalidLocationError, RuntimeError):
            return
        if not pts:
            return
        clat = sum(p[0] for p in pts) / len(pts)
        clon = sum(p[1] for p in pts) / len(pts)

        rec: dict = {"k": kind, "t": "w", "lat": round(clat, COORD_DP), "lon": round(clon, COORD_DP)}
        nm = t.get("name")
        if nm:
            rec["nm"] = nm
        if kind == "building":
            bt = t.get("building")
            if bt and bt != "yes":
                rec["bt"] = bt
            h = _f(t.get("height")) or _f(t.get("building:height"))
            lvl = _f(t.get("building:levels"))
            if h:
                rec["h"] = h
            if lvl:
                rec["lvl"] = lvl
        # The exact tag value, for kinds whose label covers several values.
        # Without it a query for [leisure~"pitch"] cannot be told apart from
        # one for [leisure~"park"], and answering either with both is the kind
        # of quiet wrongness this index must never produce.
        if kind in ("park", "sport"):
            rec["lv"] = t.get("leisure")
        elif kind == "waterway":
            rec["wv"] = t.get("waterway")
        elif kind == "railway":
            rec["rv"] = t.get("railway")
        elif kind == "industrial":
            rec["luv"] = t.get("landuse")
        if kind == "highway":
            rec["hw"] = t.get("highway")
            for k in ("lanes", "oneway", "bridge", "junction"):
                if t.get(k):
                    rec[k[:2]] = t.get(k)
        if geom_wanted and len(pts) > 1:
            rec["g"] = [[p[0], p[1]] for p in pts]
        self._emit(rec)

        if self.kept % 200000 == 0:
            log.info("  %s features kept (%.0fs)", f"{self.kept:,}", time.time() - self._t0)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True, nargs="+",
                    help="one or more Geofabrik .osm.pbf extracts; pass several to "
                         "widen coverage (e.g. western-zone northern-zone)")
    ap.add_argument("--out", default="data/osm/index.jsonl.gz", help="output index")
    ap.add_argument("--near", nargs="*", default=None, metavar="CONFIG_OR_LATLON",
                    help="keep only features within --radius-km of these places; "
                         "each is a config file or a raw 'lat,lon'. Without it "
                         "the whole extract is indexed, which for three Indian "
                         "zones is about a gigabyte.")
    ap.add_argument("--radius-km", type=float, default=25.0,
                    help="radius around each --near place (default 25)")
    a = ap.parse_args(argv)

    pbfs = [Path(x) for x in a.pbf]
    missing = [x for x in pbfs if not x.is_file()]
    if missing:
        for x in missing:
            log.error("no such extract: %s", x)
        log.error("download extracts from https://download.geofabrik.de/asia/india/")
        return 2

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # Several extracts append into ONE index, so coverage widens by adding a
    # file rather than rebuilding. Geofabrik zones overlap slightly at their
    # borders, which double-counts a handful of features in the border strip;
    # that is a rounding error against a city-scale census and far cheaper
    # than de-duplicating millions of records by OSM id.
    near = _resolve_near(a.near)
    if near:
        log.info("bounding to %d place(s) at %.0f km", len(near), a.radius_km)
    else:
        log.warning("no --near given: indexing the FULL extract. Three Indian "
                    "zones come to ~972 MB and take ~12 minutes to load.")

    total_kept = 0
    total_far = 0
    pts_in = pts_out = 0
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        for pbf in pbfs:
            log.info("indexing %s (%.0f MB)", pbf.name, pbf.stat().st_size / 1e6)
            h = Collector(fh, near=near, radius_km=a.radius_km)
            h.apply_file(str(pbf), locations=True, idx="flex_mem")
            log.info("  %s features from %s", f"{h.kept:,}", pbf.name)
            total_kept += h.kept
            total_far += h.dropped_far
            pts_in += h.pts_in
            pts_out += h.pts_out

    log.info("done in %.0fs: %s features from %d extract(s) -> %s (%.0f MB)",
             time.time() - t0, f"{total_kept:,}", len(pbfs), out_path,
             out_path.stat().st_size / 1e6)
    if total_far:
        log.info("  %s features dropped as outside the --near bound", f"{total_far:,}")
    if pts_in:
        log.info("  geometry simplified at %.0f m: %s points -> %s (-%.0f%%)",
                 SIMPLIFY_M, f"{pts_in:,}", f"{pts_out:,}",
                 100 * (1 - pts_out / max(pts_in, 1)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
