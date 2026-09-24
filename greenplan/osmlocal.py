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
import threading
import time
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

    def __init__(self, path: Path,
                 focus: tuple[float, float] | list[tuple[float, float]] | None = None,
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
        # One focus or several. A single-city server passes one point; the
        # multi-city server passes every city it serves, because a lone focus
        # cannot cover Ahmedabad and Bengaluru at once - they are 1,500 km
        # apart, and a radius wide enough to span them would load the whole
        # subcontinent into memory to serve two discs of it.
        if focus is None:
            self.focus: list[tuple[float, float]] = []
        elif isinstance(focus, tuple):
            self.focus = [focus]
        else:
            self.focus = [tuple(f) for f in focus if f]
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
        # Pre-compute a degree box per focus so the haversine runs only for
        # points that are already close to one of them.
        boxes = []
        for flat, flon in self.focus:
            dlat = self.radius_m / 111320.0
            dlon = self.radius_m / (111320.0 * max(0.2, math.cos(math.radians(flat))))
            boxes.append((flat, flon, dlat, dlon))
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
                if boxes:
                    keep = False
                    for flat, flon, dlat, dlon in boxes:
                        if abs(lat - flat) > dlat or abs(lon - flon) > dlon:
                            continue
                        if _haversine(flat, flon, lat, lon) <= self.radius_m:
                            keep = True
                            break
                    if not keep:
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
            log.warning(
                "local OSM index at %s held nothing within %.0f km of %s - the "
                "extract does not cover these cities. Download the right "
                "Geofabrik zone and re-run scripts/osm_index.py; until then map "
                "features fall back to the public Overpass instance.",
                self.path, self.radius_m / 1000, self.focus)

    @property
    def ready(self) -> bool:
        return self.n > 0

    def covers(self, lat: float, lon: float, margin: float = 0.05) -> bool:
        """Is this point somewhere we actually hold data?

        Tested against the LOADED DISCS, not the union bounding box. With one
        focus the two are the same; with several they are not, and the
        difference is a false answer. Ahmedabad and Mumbai together span a
        bbox reaching from 18.9 to 23.2 N - Surat sits inside that rectangle
        and we hold nothing there, so a bbox test would report "covered" and
        the query would return zero buildings for a city of six million. An
        empty result and an out-of-coverage result are different claims, and
        this is the method that keeps them apart.

        `margin` is the slack a caller may sit outside a disc and still be
        answered; it is applied to the radius, in metres, rather than to a
        rectangle in degrees."""
        if not self.ready:
            return False
        if not self.focus:
            # No focus means the whole extract was loaded; the bbox IS the
            # coverage in that case.
            return (self.bbox[0] - margin <= lat <= self.bbox[2] + margin
                    and self.bbox[1] - margin <= lon <= self.bbox[3] + margin)
        slack_m = self.radius_m + margin * 111320.0
        for flat, flon in self.focus:
            if _haversine(flat, flon, lat, lon) <= slack_m:
                return True
        return False

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
                    # A fixed-value kind needs NO per-record test: the index
                    # label already is the answer. Building one anyway asked
                    # each record for a field it does not carry - signals and
                    # motorway junctions have no `hw`, level crossings have no
                    # `rv` - so every record failed and the query returned an
                    # empty set. The traffic panel then printed "0 signals"
                    # over a city with 85 in the loaded index.
                    #
                    # A false zero is worse here than a refusal: the panel had
                    # no way to tell "none exist" from "I cannot answer", and
                    # showed the first.
                    if kind in cls._FIXED_VALUE:
                        out.append((kind, None))
                    else:
                        out.append((kind, cls._value_pred(key, op, val)))
                    continue

                if not val:
                    out.append((kind, None))                # [building], [highway]
                    continue

                # Fixed-value kinds are settled by the KIND, before any
                # per-record field is considered.
                #
                # This test used to sit behind `if field is None`, so it was
                # only reachable for keys absent from _VALUE_FIELD. Three of
                # the six fixed-value kinds are keyed on "highway" or
                # "railway", which ARE in that table - so signals, motorway
                # junctions and level crossings took the per-record path
                # instead, looked for an `hw`/`rv` field those records do not
                # carry, matched nothing, and returned an empty result.
                #
                # Empty, not refused: the traffic panel printed "0 signals"
                # for a city with 85 of them in the loaded index. A false zero
                # presented as a measurement is the exact failure this whole
                # module is built to avoid, and it survived because a count of
                # zero looks like an answer.
                fixed = cls._FIXED_VALUE.get(kind)
                if fixed is not None:
                    if not val or re.search(val, fixed, re.I):
                        out.append((kind, None))
                    continue
                field = cls._VALUE_FIELD.get(key)
                if field is None:
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
        # `out geom` asks for full geometry; `out body`, `out meta`, `out skel`
        # and a bare `out` ask for the element itself. For a NODE those are the
        # same answer - a node IS its coordinate - and the index emits lat/lon
        # for nodes either way, so refusing `body` bought nothing.
        #
        # It cost the Traffic tab, which is where this was found. Its second
        # query - traffic signals, level crossings, motorway junctions - ends
        # `out body qt 1200`, and `body` was in the _OUT regex but not in this
        # accept list, so `query()` returned None and the server answered 501.
        # The client then did exactly what a 501 tells it to and went to the
        # public Overpass instance, which throttles: the roads came back from
        # the local index in half a second and the junctions sat waiting out a
        # rate limit, so the panel loaded with nothing on it. Signal and
        # junction density is one of the three inputs the congestion model
        # uses, so this was not a cosmetic loss.
        #
        # WAY geometry is a different matter. Real Overpass `out body` on a way
        # returns node REFERENCES, not coordinates, and this index does not
        # hold the node table to resolve them - so answering a way query with
        # full geometry would be returning a shape the caller did not ask for.
        # `body` is therefore accepted only when every statement is a node;
        # a way asking for `body` still gets the honest refusal.
        node_only = all(st.group("type").lower() == "node" for st in stmts)
        body_mode = (not count_mode) and node_only and any(
            m in ("body", "meta", "skel", "") for m in modes)
        geom_mode = (not count_mode) and (
            any(m == "geom" for m in modes) or body_mode)
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

# One loader at a time. The server is a ThreadingHTTPServer, so several map
# reads arrive CONCURRENTLY on a cold start - the area panel, the traffic
# tab and the studio all ask at once. Without this lock every one of them
# saw `_INSTANCE is None` and each began its own full load of the same file:
# four threads parsing the same gzip, competing for the same disk and the
# same GIL, and the machine paging. Measured cold, that turned reads that
# should take a second into 60-150 s each and roughly ten minutes before the
# app settled - the "surroundings unavailable, then suddenly fine" that this
# lock exists to end.
#
# The double check around it is the standard one: the fast path stays
# lock-free once the index is built, which is every call after the first.
_LOAD_LOCK = threading.Lock()

# When the one loader started, or None when nobody is loading. `peek()` reads
# it to answer "not yet, come back" WITHOUT joining the queue behind the lock.
#
# This is the difference between a request that returns in a millisecond and
# one that returns in fifty seconds. /api/osm used to call get(), which blocks
# on the lock above until the index is built - so a map click during the
# warm-up did not fall through to the public instance as the code claimed, it
# STALLED for the rest of the load, blew the studio's 25 s budget, and only
# then went to public Overpass with nothing left. Which is exactly when the
# public instance is most likely to throttle, so the feature census came back
# empty while a local index that answers the same query in 0.07 s finished
# loading a few seconds later.
_LOAD_STARTED: float | None = None
# Roughly how long a cold load takes, used only to tell a caller when to
# come back. Measured on the shipped slim index: 120 s for 2.6M features
# across five cities, so this is that with room to spare. Wrong high costs
# one late poll; wrong low costs a burst of early ones.
_LOAD_BUDGET_S = 150.0


def peek() -> LocalOSM | None:
    """The index if it is already built, else None. NEVER blocks."""
    return _INSTANCE


def warming() -> bool:
    """True when a load is under way and has not finished."""
    return _INSTANCE is None and _LOAD_STARTED is not None


def eta_s() -> int:
    """Roughly how many seconds until a warming index is ready, at least 2.

    A caller that is told to come back needs a number to come back AFTER;
    guessing it client-side would bake this file's load time into the page."""
    if _INSTANCE is not None:
        return 0
    if _LOAD_STARTED is None:
        return int(_LOAD_BUDGET_S)
    # An estimate that has run out must not count down to zero. The budget
    # is a guess - the shipped index measured 120 s on one machine and 166 s
    # on another - and a load that overruns it is still a load in progress.
    # Answering "10 s" forever is honest ("I do not know, keep asking") and
    # holds the caller at a sane polling rate; counting down to 1 would have
    # it asking every second for however long the overrun lasts.
    left = int(_LOAD_BUDGET_S - (time.time() - _LOAD_STARTED))
    return left if left > 10 else 10


def start(index_path: Path | str = "data/osm/index.jsonl.gz",
          focus: tuple[float, float] | list[tuple[float, float]] | None = None,
          radius_km: float = 60.0) -> None:
    """Begin building the index in the background if nobody has yet.

    The server warms the index at boot, so this is a safety net rather than
    the usual path: it exists so that a /api/osm arriving on a server whose
    warm-up thread died still gets an index eventually, instead of being told
    "warming" forever by a loader that is not running."""
    global _LOAD_STARTED
    if _INSTANCE is not None or _LOAD_STARTED is not None:
        return
    # Claimed here, not in the thread: two requests arriving in the same
    # millisecond would both see None and both spawn a loader. The lock in
    # get() would keep them from parsing twice, but the second thread would
    # sit on it for the whole load for no reason.
    _LOAD_STARTED = time.time()
    threading.Thread(
        target=lambda: get(index_path, focus=focus, radius_km=radius_km),
        name="osm-load", daemon=True).start()


def get(index_path: Path | str = "data/osm/index.jsonl.gz",
        focus: tuple[float, float] | list[tuple[float, float]] | None = None,
        radius_km: float = 60.0) -> LocalOSM:
    """The process-wide index. Built once, on first use.

    `focus` should be the city being planned — greenplan.server passes the
    centre of config/city.yaml's bbox. Loading is bounded to a disc around it
    so memory stays in tens of megabytes rather than over a gigabyte.

    Callers that arrive while the index is loading BLOCK here until it is
    ready, rather than starting a second load. Waiting on the one loader is
    both faster and less memory than racing it."""
    global _INSTANCE, _LOAD_STARTED
    if _INSTANCE is not None:
        return _INSTANCE
    with _LOAD_LOCK:
        # Re-check: another thread may have finished while we waited.
        if _INSTANCE is None:
            if _LOAD_STARTED is None:
                _LOAD_STARTED = time.time()
            _INSTANCE = LocalOSM(Path(index_path), focus=focus, radius_km=radius_km)
    return _INSTANCE
