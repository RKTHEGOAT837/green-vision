"""Several cities behind one server, chosen by where the user clicked.

The engine was built around one city: one config, one Engine, one set of
endpoints. That is correct for the pipeline - each city has its own panel,
its own trained memory, its own ranking, and nothing is shared between them
but code. It is wrong for the server, because a person looking at a map does
not want to restart a process to move from Ahmedabad to Bengaluru. Clicking
Rajajinagar on an Ahmedabad server answered "this point is outside
Ahmedabad", which was honest and still felt like a fault.

So this registry holds every configured city and picks one per request:

    explicit   ?city=bengaluru          - the caller knows which
    by point   lat/lon inside a bbox    - the map click decides
    fallback   the city named at boot   - unchanged behaviour

LAZY, deliberately. Building an Engine loads a 42-month panel, runs the
recommendation pass and touches the trained memory; doing that for five
cities at startup would put several minutes in front of the first page load,
and most sessions never leave one city. Each city is built on first use and
kept, so only the cities actually looked at are ever paid for.

Thread safety matters here: ThreadingHTTPServer serves each request on its
own thread, and two clicks arriving together on a cold city would otherwise
build the same Engine twice. One lock per city means the second caller waits
for the first rather than duplicating several minutes of work.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from .config import apply_env_overrides, load_config

log = logging.getLogger(__name__)


class CityRegistry:
    """Every configured city, built on demand and cached."""

    def __init__(self, config_dir: Path, default_config: str,
                 engine_factory) -> None:
        self._factory = engine_factory
        self._engines: dict[str, Any] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.cities: dict[str, dict[str, Any]] = {}

        default_path = Path(default_config).resolve()
        self.default_slug: str | None = None

        for path in sorted(Path(config_dir).glob("*.yaml")):
            try:
                cfg = load_config(str(path))
            except Exception as exc:            # a broken config must not
                log.warning("skipping %s: %s", path.name, exc)   # kill the rest
                continue
            slug = _slug(cfg.city.name)
            # A city with no data cannot answer, and offering it in the picker
            # would be a promise the server cannot keep. Check the streams
            # rather than assuming the config implies data.
            missing = _missing_streams(cfg)
            self.cities[slug] = {
                "slug": slug,
                "name": cfg.city.name,
                "config": str(path),
                "bbox": list(cfg.city.bbox),        # lon_min, lat_min, lon_max, lat_max
                "ready": not missing,
                "missing": missing,
            }
            if path.resolve() == default_path:
                self.default_slug = slug

        if self.default_slug is None and self.cities:
            self.default_slug = next(iter(self.cities))

        usable = [c for c in self.cities.values() if c["ready"]]
        log.info("cities configured: %d (%d with data): %s",
                 len(self.cities), len(usable),
                 ", ".join(c["name"] for c in usable) or "none")

    # -- selection ---------------------------------------------------------
    def pick(self, city: str | None = None,
             lat: float | None = None, lon: float | None = None) -> str | None:
        """Which city should answer? Explicit name, else the point, else default."""
        if city:
            c = _slug(city)
            if c in self.cities and self.cities[c]["ready"]:
                return c
            return None                     # asked for by name and not available
        if lat is not None and lon is not None:
            hit = self.containing(lat, lon)
            if hit:
                return hit
        return self.default_slug

    def containing(self, lat: float, lon: float) -> str | None:
        """The city whose bbox holds this point, nearest centre first.

        Bboxes can overlap for neighbouring cities, so ties go to whichever
        centre is closer - the alternative is depending on dict order, which
        would make the answer depend on filename."""
        best, best_d = None, None
        for slug, c in self.cities.items():
            if not c["ready"]:
                continue
            lon0, lat0, lon1, lat1 = c["bbox"]
            if lat0 <= lat <= lat1 and lon0 <= lon <= lon1:
                d = ((lat - (lat0 + lat1) / 2) ** 2 + (lon - (lon0 + lon1) / 2) ** 2)
                if best_d is None or d < best_d:
                    best, best_d = slug, d
        return best

    # -- construction ------------------------------------------------------
    def engine(self, slug: str | None):
        """The Engine for a city, building it on first use."""
        if not slug or slug not in self.cities:
            return None
        if not self.cities[slug]["ready"]:
            return None
        if slug in self._engines:
            return self._engines[slug]

        with self._guard:
            lock = self._locks.setdefault(slug, threading.Lock())
        with lock:
            # Re-check inside the lock: another thread may have built it while
            # this one waited.
            if slug in self._engines:
                return self._engines[slug]
            cfg_path = self.cities[slug]["config"]
            log.info("building engine for %s (first request) …", self.cities[slug]["name"])
            eng = self._factory(cfg_path)
            self._engines[slug] = eng
            return eng

    @property
    def loaded(self) -> list[str]:
        return sorted(self._engines)

    def summary(self) -> list[dict[str, Any]]:
        """What the browser needs to know: names, bboxes, readiness."""
        return [
            {
                "slug": c["slug"],
                "name": c["name"],
                "bbox": c["bbox"],
                "ready": c["ready"],
                "loaded": c["slug"] in self._engines,
                "missing": c["missing"],
            }
            for c in sorted(self.cities.values(), key=lambda x: x["name"])
        ]


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _missing_streams(cfg) -> list[str]:
    """Which required CSV streams are absent. A city missing any of them
    cannot be built, and saying so up front beats failing mid-request."""
    out = []
    for label, spec in (("aqi", cfg.adapters.aqi),
                        ("ndvi", cfg.adapters.green_cover),
                        ("traffic", cfg.adapters.traffic)):
        if isinstance(spec, str) and spec.startswith("csv:"):
            p = cfg.resolve(spec[4:].strip())
            if not p.is_file():
                out.append(label)
    return out
