"""A local Overpass, limited to the subset Green Vision actually speaks.

This is deliberately NOT a general Overpass implementation. Overpass QL is a
real language with recursion, unions, filters and set arithmetic, and writing
a partial interpreter that silently mis-answers the parts it does not
understand would be far worse than not having one. So this parses only the
statement shapes the studio emits, and REFUSES anything else — the caller
then falls back to the public instance, which is exactly where it was before.

The shapes the studio emits, all of them formulaic:

    node(around:R,LAT,LON)[key=value];out count;
    way(around:R,LAT,LON)[key~"regex"];out count;
    way(around:R,LAT,LON)[key];out geom qt N;
    ( ...several of the above... );out geom qt N;

Everything is answered from data/osm/index.jsonl.gz, built by
scripts/osm_index.py from a Geofabrik extract. Responses are shaped exactly
like Overpass's own JSON so the browser cannot tell the difference: `count`
elements carry tags.total, geometry elements carry `geometry` arrays.

Why bother, rather than just using overpass-api.de: the public instance gives
two slots per IP and 429s the third request. That made the census, the traffic
panel and the 3D builder a coin toss at peak. Locally there is no limit, no
network, and no third-party dependency to explain to anyone.

Coverage is whatever extract was indexed. `covers()` reports honestly, and the
server falls through to the public instance outside it rather than answering
"nothing here" — an empty result and an out-of-coverage result are completely
different claims and must never be conflated.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# --- geometry ---------------------------------------------------------------
_R_EARTH = 6371008.8


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _R_EARTH * math.asin(min(1.0, math.sqrt(a)))


# --- the query subset -------------------------------------------------------
# node|way (around:RADIUS , LAT , LON) [ ...filters... ] ;
_STMT = re.compile(
    r"(?P<type>node|way|relation)\s*"
    r"\(\s*around\s*:\s*(?P<r>[\d.]+)\s*,\s*(?P<lat>-?[\d.]+)\s*,\s*(?P<lon>-?[\d.]+)\s*\)\s*"
    r"(?P<filters>(?:\[[^\]]*\]\s*)*)\s*;",
    re.I,
)
_FILTER = re.compile(r"\[\s*(?P<neg>!?)(?P<key>[\w:]+)\s*(?P<op>=|~|!=)?\s*(?P<val>\"[^\"]*\"|[^\]]*)?\s*\]")
_OUT = re.compile(r"\bout\s+(?P<mode>count|geom|bb|body|meta|skel)?\s*(?:qt\s*)?(?P<limit>\d+)?\s*;", re.I)

# Map an OSM tag test onto the `k` label the index stores. The index keeps only
# the categories the app asks for, so anything outside this table is a refusal
# rather than a zero.
_KIND_RULES: list[tuple[str, str | None, str, str]] = [
    # (key, value-regex or None, node/way, index kind)
    ("natural", r"^tree$", "node", "tree"),
    ("amenity", r"school|college|university|kindergarten", "node", "school"),
    ("amenity", r"hospital|clinic|doctors", "node", "health"),
    ("highway", r"^traffic_signals$", "node", "signal"),
    ("railway", r"^level_crossing$", "node", "crossing"),
    ("highway", r"^motorway_junction$", "node", "ramp"),
    ("building", None, "way", "building"),
    ("highway", None, "way", "highway"),
    # Two leisure kinds, not one. A query for pitches must not be answered
    # with parks, and vice versa; both carry their exact tag value so the
    # per-record predicate can tell them apart.
    ("leisure", r"park|garden|nature_reserve", "way", "park"),
    ("leisure", r"pitch|swimming_pool|track|golf_course|sports_centre", "way", "sport"),
    ("natural", r"^water$", "way", "water"),
    ("waterway", None, "way", "waterway"),
    ("amenity", r"^parking$", "way", "parking"),
    ("railway", None, "way", "railway"),
    ("landuse", r"industrial|quarry|landfill", "way", "industrial"),
]


class LocalOSM:
    """The indexed extract, held in memory with a coarse grid for lookup.

    A 0.01-degree grid (about 1.1 km) is enough: the studio's largest query is
    a 5.6 km radius, which touches ~121 cells, and scanning those beats both a
    full scan and the complexity of a real spatial index.
    """

    CELL = 0.01

    def __init__(self, path: Path, focus: tuple[float, float] | None = None,
                 radius_km: float = 60.0) -> None:
        """Load the index, keeping only what is within `radius_km` of `focus`.

        The western-zone extract yields roughly 1.8 million features. Held as
        Python dicts that is well over a gigabyte of RAM and many seconds of
        startup — unacceptable for a tool whose whole pitch is that it runs on
        an ordinary municipal laptop.

        Almost none of it is ever asked for. The studio reads a 100 km2 ring
        around one point; a 60 km disc around the city being planned covers
        that ring, its neighbours, and any realistic pan, for a fraction of
        the memory. Everything outside simply is not loaded, `covers()` says
        so, and the public Overpass answers instead — the same honest fallback
        used for points outside the extract entirely.

        Re-point it by passing a different `focus` (or widen `radius_km`) when
        planning another city; nothing here is Ahmedabad-specific.
        """
        self.path = path
        self.focus = focus
        self.radius_m = radius_km * 1000.0
        # cell -> kind -> records. Bucketing by kind INSIDE the cell is what
        # makes this usable: the census asks nine questions about the same
        # disc, and a flat cell list made each one re-scan every feature in
        # range — nine full scans of ~35,000 buildings to count 26 schools.
        # First measured run of the census took 92 s; with these buckets it is
        # a fraction of that, because each statement only ever touches records
        # of the kind it asked about.
        self.grid: dict[tuple[int, int], dict[str, list[dict]]] = {}
        self.n = 0
        self.skipped = 0
        self.bbox = [90.0, 180.0, -90.0, -180.0]   # minlat, minlon, maxlat, maxlon
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            log.info("no local OSM index at %s — the public Overpass stays the only source", self.path)
            return
        flat, flon = self.focus if self.focus else (None, None)
        # Cheap pre-filter in degrees so the haversine runs only near the edge.
        if flat is not None:
            dlat = self.radius_m / 111320.0
            dlon = self.radius_m / (111320.0 * max(0.2, math.cos(math.radians(flat))))
        opener = gzip.open if self.path.suffix == ".gz" else open
        with opener(self.path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                lat, lon = rec.get("lat"), rec.get("lon")
                if lat is None or lon is None:
                    continue
                if flat is not None:
                    if abs(lat - flat) > dlat or abs(lon - flon) > dlon:
                        self.skipped += 1
                        continue
                    if _haversine(flat, flon, lat, lon) > self.radius_m:
                        self.skipped += 1
                        continue
                cell = self.grid.setdefault((int(lat / self.CELL), int(lon / self.CELL)), {})
                cell.setdefault(rec.get("k"), []).append(rec)
                self.n += 1
                if lat < self.bbox[0]: self.bbox[0] = lat
                if lon < self.bbox[1]: self.bbox[1] = lon
                if lat > self.bbox[2]: self.bbox[2] = lat
                if lon > self.bbox[3]: self.bbox[3] = lon
        if self.n:
            log.info("local OSM index: %s features loaded (%s outside the %.0f km focus, "
                     "left on disk), bbox %.3f,%.3f -> %.3f,%.3f",
                     f"{self.n:,}", f"{self.skipped:,}", self.radius_m / 1000,
                     self.bbox[0], self.bbox[1], self.bbox[2], self.bbox[3])
        else:
            log.warning("local OSM index at %s held nothing within %.0f km of %s",
                        self.path, self.radius_m / 1000, self.focus)

    @property
    def ready(self) -> bool:
        return self.n > 0

    def covers(self, lat: float, lon: float, margin: float = 0.05) -> bool:
        """Is this point inside the indexed extract, with a little slack?

        Outside it we must NOT answer — an empty result would read as 'nothing
        is there', which is a different and false claim."""
        if not self.ready:
            return False
        return (self.bbox[0] - margin <= lat <= self.bbox[2] + margin
                and self.bbox[1] - margin <= lon <= self.bbox[3] + margin)

    def _near(self, lat: float, lon: float, r_m: float, kind: str) -> Iterable[dict]:
        """Records of ONE kind within r_m. Kind is filtered before distance so
        a count of schools never measures its way through every building."""
        dlat = r_m / 111320.0
        dlon = r_m / (111320.0 * max(0.2, math.cos(math.radians(lat))))
        y0, y1 = int((lat - dlat) / self.CELL), int((lat + dlat) / self.CELL)
        x0, x1 = int((lon - dlon) / self.CELL), int((lon + dlon) / self.CELL)
        # Equirectangular distance in metres: at city scale the error against
        # haversine is under a metre, and it avoids six trig calls per record.
        mlat = 111320.0
        mlon = 111320.0 * math.cos(math.radians(lat))
        r2 = r_m * r_m
        for y in range(y0, y1 + 1):
            row = self.grid.get
            for x in range(x0, x1 + 1):
                cell = row((y, x))
                if not cell:
                    continue
                for rec in cell.get(kind, ()):
                    dy = (rec["lat"] - lat) * mlat
                    dx = (rec["lon"] - lon) * mlon
                    if dy * dy + dx * dx <= r2:
                        yield rec

    # -- query parsing -----------------------------------------------------
    # Which index field holds the OSM value for a given tag key. A kind like
    # "highway" covers every road class, so the class itself has to be tested
    # against the query's own filter — see _kind_for.
    _VALUE_FIELD = {"highway": "hw", "building": "bt", "leisure": "lv",
                    "waterway": "wv", "railway": "rv", "landuse": "luv"}
    # Kinds whose index label already IS the answer to the tag test, so no
    # per-record value check is possible or needed.
    _FIXED_VALUE = {
        "tree": "tree", "water": "water", "parking": "parking",
        "signal": "traffic_signals", "crossing": "level_crossing",
        "ramp": "motorway_junction",
    }

    @classmethod
    def _kind_for(cls, osm_type: str, filters: str):
        """Resolve a statement's filters to (index kind, value test).

        The value test is the part this originally threw away, and doing so
        was a real defect rather than a missing nicety. `[highway~"^(motorway|
        trunk|primary|secondary)(_link)?$"]` maps to the kind "highway", which
        the index uses for EVERY road class — so ignoring the regex answered a
        query about arterials with 2,061 residential streets, plus footways,
        steps and pedestrian paths. The traffic panel would have scored
        footpaths as arterial bottlenecks and had no way to know.

        Returns (kind, predicate) where predicate takes a record and says
        whether it really matches, or None when the tag test cannot be
        represented — in which case the caller refuses the whole query.
        """
        want = "node" if osm_type == "node" else "way"
        parsed = []
        for m in _FILTER.finditer(filters):
            key = m.group("key")
            op = m.group("op") or ""
            val = (m.group("val") or "").strip().strip('"')
            parsed.append((key, op, val))
        if not parsed:
            return None

        for key, op, val in parsed:
            out: list = []
            for rk, rv, rtype, kind in _KIND_RULES:
                if key != rk or rtype != want:
                    continue

                if rv is not None:
                    # The rule's own value set. Keep it if the query's pattern
                    # overlaps it AT ALL — a single query can legitimately span
                    # several index kinds, e.g. the 3D builder asking for
                    # [leisure~"^(pitch|swimming_pool|park|garden)$"], which is
                    # "park" and "sport" together. Returning only the first
                    # match answered that with parks alone and silently lost
                    # every pitch and pool on the site.
                    if val and not re.search(rv, val, re.I):
                        continue
                    out.append((kind, cls._value_pred(key, op, val)))
                    continue

                if not val:
                    out.append((kind, None))                # [building], [highway]
                    continue

                field = cls._VALUE_FIELD.get(key)
                if field is None:
                    fixed = cls._FIXED_VALUE.get(kind)
                    if fixed is not None and re.search(val, fixed, re.I):
                        out.append((kind, None))
                    continue
                pred = cls._value_pred(key, op, val)
                if pred is False:                            # unparseable pattern
                    return None
                out.append((kind, pred))

            if out:
                return out
        return None

    @classmethod
    def _value_pred(cls, key: str, op: str, val: str):
        """A predicate testing one record's real tag value, or None when the
        kind already implies the answer. False means 'cannot represent this'."""
        field = cls._VALUE_FIELD.get(key)
        if field is None or not val:
            return None
        try:
            pat = re.compile(val, re.I) if op == "~" else None
        except re.error:
            return False
        if pat is not None:
            def pred(rec, _p=pat, _f=field, _k=key):
                v = rec.get(_f)
                if v is None:
                    # building=yes carries no bt; highway always has hw.
                    v = "yes" if _k == "building" else ""
                return bool(_p.search(str(v)))
            return pred

        def eq(rec, _v=val.lower(), _f=field, _k=key):
            v = rec.get(_f)
            if v is None:
                v = "yes" if _k == "building" else ""
            return str(v).lower() == _v
        return eq

    def query(self, ql: str) -> dict[str, Any] | None:
        """Answer an Overpass QL string, or return None to mean 'not mine'."""
        if not self.ready:
            return None
        stmts = list(_STMT.finditer(ql))
        if not stmts:
            return None

        outs = list(_OUT.finditer(ql))
        if not outs:
            return None
        # `out count` after every statement = the census shape; a single
        # trailing `out geom` = the geometry shape.
        modes = [(m.group("mode") or "").lower() for m in outs]
        count_mode = all(m == "count" for m in modes)
        geom_mode = (not count_mode) and any(m == "geom" for m in modes)
        # `out bb` returns a bounding box per feature instead of full geometry.
        # The site finder uses it for compact obstacles (buildings, pools, car
        # parks) where a box is a fair stand-in for the footprint.
        bb_mode = (not count_mode) and (not geom_mode) and any(m == "bb" for m in modes)
        if not (count_mode or geom_mode or bb_mode):
            return None

        first = stmts[0]
        if not self.covers(float(first.group("lat")), float(first.group("lon"))):
            return None

        elements: list[dict] = []
        limit = None
        for m in outs:
            if m.group("limit"):
                limit = int(m.group("limit"))

        for st in stmts:
            resolved = self._kind_for(st.group("type").lower(), st.group("filters"))
            if not resolved:
                # One unsupported statement makes the whole answer wrong.
                return None
            lat, lon, r = float(st.group("lat")), float(st.group("lon")), float(st.group("r"))
            hits = []
            for kind, pred in resolved:
                found = self._near(lat, lon, r, kind)
                hits.extend(found if pred is None else (x for x in found if pred(x)))

            if count_mode:
                elements.append({"type": "count", "id": 0,
                                 "tags": {"total": str(len(hits)), "nodes": "0",
                                          "ways": str(len(hits)), "relations": "0",
                                          "areas": "0"}})
            else:
                for rec in hits:
                    elements.append(_as_overpass(rec, bounds_only=bb_mode))
                    if limit and len(elements) >= limit:
                        break
            if limit and (geom_mode or bb_mode) and len(elements) >= limit:
                break

        return {
            "version": 0.6,
            "generator": "Green Vision local OSM index (Geofabrik extract)",
            "osm3s": {"copyright": "Data © OpenStreetMap contributors, ODbL."},
            "elements": elements,
        }


def _as_overpass(rec: dict, bounds_only: bool = False) -> dict:
    """One index record in the JSON shape the browser already parses.

    `bounds_only` emits Overpass's `out bb` shape - a {minlat,minlon,maxlat,
    maxlon} box instead of the point list - which is what the site finder
    reads for compact obstacles."""
    tags: dict[str, str] = {}
    k = rec["k"]
    if k == "building":
        tags["building"] = rec.get("bt") or "yes"
        if rec.get("h"):
            tags["height"] = str(rec["h"])
        if rec.get("lvl"):
            tags["building:levels"] = str(rec["lvl"])
    elif k == "highway":
        tags["highway"] = rec.get("hw") or "road"
        for src, dst in (("la", "lanes"), ("on", "oneway"), ("br", "bridge"), ("ju", "junction")):
            if rec.get(src):
                tags[dst] = str(rec[src])
    elif k in ("park", "sport"):
        tags["leisure"] = rec.get("lv") or ("park" if k == "park" else "pitch")
    elif k == "water":
        tags["natural"] = "water"
    elif k == "waterway":
        tags["waterway"] = rec.get("wv") or "stream"
    elif k == "parking":
        tags["amenity"] = "parking"
    elif k == "railway":
        tags["railway"] = rec.get("rv") or "rail"
    elif k == "industrial":
        tags["landuse"] = rec.get("luv") or "industrial"
    elif k == "tree":
        tags["natural"] = "tree"
    elif k == "school":
        tags["amenity"] = "school"
    elif k == "health":
        tags["amenity"] = "hospital"
    elif k == "signal":
        tags["highway"] = "traffic_signals"
    elif k == "crossing":
        tags["railway"] = "level_crossing"
    elif k == "ramp":
        tags["highway"] = "motorway_junction"
    if rec.get("nm"):
        tags["name"] = rec["nm"]

    out: dict[str, Any] = {
        "type": "way" if rec.get("t") == "w" else "node",
        "id": abs(hash((rec.get("lat"), rec.get("lon"), k))) % (10 ** 12),
        "tags": tags,
    }
    if out["type"] == "node":
        out["lat"] = rec["lat"]
        out["lon"] = rec["lon"]
    else:
        g = rec.get("g")
        if bounds_only:
            if g:
                lats = [q[0] for q in g]
                lons = [q[1] for q in g]
                out["bounds"] = {"minlat": min(lats), "minlon": min(lons),
                                 "maxlat": max(lats), "maxlon": max(lons)}
            else:
                out["bounds"] = {"minlat": rec["lat"], "minlon": rec["lon"],
                                 "maxlat": rec["lat"], "maxlon": rec["lon"]}
        elif g:
            out["geometry"] = [{"lat": q[0], "lon": q[1]} for q in g]
        else:
            out["center"] = {"lat": rec["lat"], "lon": rec["lon"]}
    return out


_INSTANCE: LocalOSM | None = None


def get(index_path: Path | str = "data/osm/index.jsonl.gz",
        focus: tuple[float, float] | None = None,
        radius_km: float = 60.0) -> LocalOSM:
    """The process-wide index. Built once, on first use.

    `focus` should be the city being planned — greenplan.server passes the
    centre of config/city.yaml's bbox. Loading is bounded to a disc around it
    so memory stays in tens of megabytes rather than over a gigabyte."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = LocalOSM(Path(index_path), focus=focus, radius_km=radius_km)
    return _INSTANCE
