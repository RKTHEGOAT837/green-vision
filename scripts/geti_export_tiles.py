"""Sample Esri World Imagery tiles into a canopy segmentation training set.

WHY THIS EXISTS — stated honestly. The studio's canopy layer (`vegScore()` in
index.html, ~line 236) is a hand-written greenness index: VARI = (g-r)/(g+r-b)
behind three brightness gates, tuned by eye on Ahmedabad imagery. It is fast,
needs no model download, and runs in any browser — but it cannot tell a tree
from a cricket outfield, an irrigated median from a lawn, or a shadowed canopy
from a dark roof. Every one of those is a false reading on the map.

A supervised segmentation model, trained on human-drawn canopy polygons, can
learn that distinction. This script builds the raw material for one: it
downloads N Esri World Imagery tiles spread across the city bbox, ready to be
uploaded to Intel Geti (or any annotation tool) and labelled with a single
"canopy" class. See docs/intel/geti-canopy-runbook.md for the full workflow
and data/geti_tiles/LABELING.md (written by this script) for the label rules.

WHY STRATIFIED, NOT RANDOM. Ahmedabad's bbox is mostly low-canopy: a uniform
random draw of 120 tiles comes back as a pile of rooftops and scrub with three
parks in it, and a model trained on that learns "predict no canopy" because
that is right 95% of the time. So this script ports the SAME vegScore to
Python, scores each candidate tile, buckets it low / mid / high by the
studio's own heatmap breakpoints (`heatColor()`: 0.08 and 0.34), and fills the
three buckets evenly. VARI is only the sampler here — it decides which tiles a
human looks at, never what the label is. The label is the human's.

Both the sampler and the thing it is meant to replace are the same index, on
purpose: a fair test needs the challenger trained on tiles the incumbent
already has an opinion about.

IMAGERY. Esri World Imagery, the same XYZ endpoint the studio already uses
(https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer).
It is free to access without a key and Esri asks for attribution — keep
"Imagery (c) Esri, Maxar, Earthstar Geographics" on anything you publish, and
check Esri's terms before redistributing the tiles or a dataset built from
them. This script is deliberately slow: one tile at a time, a real
User-Agent, and a sleep between requests. Do not remove those.

At zoom 17 near 23 deg N one 256 px tile covers roughly 270 m of ground at
about 1.1 m/px — individual mature trees are a few pixels across, which is
about the coarsest ground sample where "draw around the tree crowns" is still
a sane instruction to give a human annotator. Lower --zoom for more context
per tile and less detail.

Usage:
    python scripts/geti_export_tiles.py --config config/city.yaml
    python scripts/geti_export_tiles.py --count 60 --zoom 18 --out-dir data/geti_tiles_z18

Outputs (default --out-dir data/geti_tiles/):
    {z}_{x}_{y}.png   one RGB tile each, filename = its XYZ address
    manifest.csv      tile,z,x,y,lat,lon,mean_vari,bucket
    LABELING.md       the labelling scheme to follow in Geti

Needs Pillow to decode the tiles (Esri serves JPEG). Same lazy-import deal as
rasterio in sentinel2_ndvi_export.py: `pip install Pillow` when you want to
run this, it is not needed by the engine itself.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from greenplan.config import load_config  # noqa: E402

# The studio's own tile endpoint (index.html CFG.ESRI). Note the {z}/{y}/{x}
# order — Esri puts row before column, unlike most XYZ services.
ESRI_TILE = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
USER_AGENT = (
    "GreenVision/1.0 (tree-planting research; "
    "https://github.com/ - contact via repo issues)"
)
TILE_PX = 256

# Bucket edges on mean VARI score. These are two of the studio's own
# heatColor() breakpoints, so "low/mid/high" here means the same thing it
# means on the map: red-to-orange, yellow, green.
LOW_CUT = 0.08
HIGH_CUT = 0.34
BUCKETS = ("low", "mid", "high")


# ---------------------------------------------------------------- tile math
# Straight ports of lon2x/lat2y/x2lon/y2lat in index.html, so a tile id here
# and a tile id in the browser are the same tile.


def lon2x(lon: float, z: float) -> int:
    return int(math.floor((lon + 180.0) / 360.0 * (2**z)))


def lat2y(lat: float, z: float) -> int:
    r = math.radians(lat)
    return int(
        math.floor((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (2**z))
    )


def x2lon(x: float, z: float) -> float:
    return x / (2**z) * 360.0 - 180.0


def y2lat(y: float, z: float) -> float:
    n = math.pi - 2 * math.pi * y / (2**z)
    return math.degrees(math.atan(0.5 * (math.exp(n) - math.exp(-n))))


def tile_center(z: int, x: int, y: int) -> tuple[float, float]:
    """Centre of tile (z,x,y) as (lat, lon)."""
    return (y2lat(y + 0.5, z), x2lon(x + 0.5, z))


def tile_span_m(z: int, lat: float) -> float:
    """Ground width of one 256 px tile, metres — for the docstring's claim."""
    return 156543.03392 * math.cos(math.radians(lat)) / (2**z) * TILE_PX


# ------------------------------------------------------- vegetation index
def veg_score(rgb: np.ndarray) -> np.ndarray:
    """Port of vegScore() from index.html (~line 236), vectorised.

    JS:  const denom=(g+r-b)||1; const vari=(g-r)/denom;
         return (2*g-r-b)>22 && g>r+6 && g>b+4 ? clamp((vari+.1)/.5,0,1) : 0;

    Identical arithmetic, identical gates. If you ever retune the browser
    index, retune this one in the same commit or the two stop agreeing.
    """
    r = rgb[..., 0].astype(np.float32)
    g = rgb[..., 1].astype(np.float32)
    b = rgb[..., 2].astype(np.float32)
    denom = g + r - b
    denom = np.where(denom == 0, 1.0, denom)  # JS `|| 1`
    vari = (g - r) / denom
    gate = ((2 * g - r - b) > 22) & (g > r + 6) & (g > b + 4)
    return np.where(gate, np.clip((vari + 0.1) / 0.5, 0.0, 1.0), 0.0)


def mean_veg(rgb: np.ndarray) -> tuple[float, float]:
    """(mean vegScore over usable pixels, usable fraction).

    Usable = the studio's own filter: mean brightness in [16, 244]. That drops
    black no-data fill and blown-out white, both of which would otherwise drag
    a tile's mean toward zero and pretend it is bare ground.
    """
    bright = rgb.astype(np.float32).mean(axis=-1)
    usable = (bright >= 16) & (bright <= 244)
    frac = float(usable.mean())
    if frac < 1e-6:
        return 0.0, 0.0
    return float(veg_score(rgb)[usable].mean()), frac


def bucket_of(score: float, low_cut: float, high_cut: float) -> str:
    if score < low_cut:
        return "low"
    if score < high_cut:
        return "mid"
    return "high"


# ------------------------------------------------------------- downloading
def fetch_tile(z: int, x: int, y: int, timeout: float, retries: int = 2) -> bytes | None:
    """One tile, or None. Never raises — a missing tile is normal at the edges."""
    url = ESRI_TILE.format(z=z, x=x, y=y)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None  # no imagery here; retrying will not help
            if attempt == retries:
                return None
        except Exception:
            if attempt == retries:
                return None
        time.sleep(1.5 * (attempt + 1))  # back off before trying again
    return None


def decode_rgb(blob: bytes) -> np.ndarray | None:
    from PIL import Image  # lazy: see module docstring

    try:
        with Image.open(io.BytesIO(blob)) as im:
            return np.asarray(im.convert("RGB"))
    except Exception:
        return None


def looks_blank(rgb: np.ndarray) -> bool:
    """Esri serves a flat grey/black placeholder outside its coverage.

    A real aerial tile has texture. A standard deviation this low across all
    three channels means nothing is in the picture, so do not ask a human to
    annotate it.
    """
    return float(rgb.astype(np.float32).std()) < 4.0


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sample Esri World Imagery tiles for a canopy segmentation dataset"
    )
    ap.add_argument("--config", default="config/city.yaml")
    ap.add_argument("--count", type=int, default=120, help="tiles to keep (default 120)")
    ap.add_argument("--zoom", type=int, default=17, help="XYZ zoom (default 17, ~1.1 m/px)")
    ap.add_argument("--out-dir", default="data/geti_tiles")
    ap.add_argument(
        "--max-candidates",
        type=int,
        default=0,
        help="stop after inspecting this many tiles (default: 5x --count)",
    )
    ap.add_argument("--low-cut", type=float, default=LOW_CUT,
                    help=f"mean VARI below this = 'low' bucket (default {LOW_CUT})")
    ap.add_argument("--high-cut", type=float, default=HIGH_CUT,
                    help=f"mean VARI at or above this = 'high' bucket (default {HIGH_CUT})")
    ap.add_argument("--min-usable", type=float, default=0.60,
                    help="skip tiles with less than this fraction of usable pixels")
    ap.add_argument("--sleep", type=float, default=0.4,
                    help="seconds between tile requests — be polite (default 0.4)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--seed", type=int, default=42, help="same default as run.seed")
    args = ap.parse_args()

    try:
        import PIL  # noqa: F401
    except ImportError:
        print(
            "Pillow is required to decode Esri tiles (they are JPEG).\n"
            "  pip install Pillow",
            file=sys.stderr,
        )
        return 2

    cfg = load_config(args.config)
    bbox = list(cfg.city.bbox)  # [lon_min, lat_min, lon_max, lat_max]
    z = args.zoom
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Every tile whose address falls inside the bbox at this zoom.
    x0, x1 = lon2x(bbox[0], z), lon2x(bbox[2], z)
    y0, y1 = lat2y(bbox[3], z), lat2y(bbox[1], z)  # y grows southward
    all_tiles = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    if not all_tiles:
        print(f"bbox {bbox} covers no tiles at zoom {z}", file=sys.stderr)
        return 1

    lat_mid = (bbox[1] + bbox[3]) / 2
    span = tile_span_m(z, lat_mid)
    print(
        f"{cfg.city.name}: {len(all_tiles)} tiles at zoom {z} over the bbox "
        f"({span:.0f} m per tile, {span / TILE_PX:.2f} m/px)"
    )

    # Even quotas across the three buckets; the remainder goes to the first
    # buckets, which are the ones the city actually has plenty of.
    quota = {b: args.count // len(BUCKETS) for b in BUCKETS}
    for i in range(args.count % len(BUCKETS)):
        quota[BUCKETS[i]] += 1
    print("target per bucket: " + ", ".join(f"{b}={quota[b]}" for b in BUCKETS))

    rng = random.Random(args.seed)
    rng.shuffle(all_tiles)
    max_cand = args.max_candidates or args.count * 5

    kept: dict[str, int] = {b: 0 for b in BUCKETS}
    rows: list[tuple] = []
    inspected = skipped_blank = skipped_missing = skipped_full = 0

    for x, y in all_tiles:
        if inspected >= max_cand or sum(kept.values()) >= args.count:
            break
        inspected += 1
        blob = fetch_tile(z, x, y, args.timeout)
        time.sleep(args.sleep)
        if blob is None:
            skipped_missing += 1
            continue
        rgb = decode_rgb(blob)
        if rgb is None or rgb.shape[0] < TILE_PX or looks_blank(rgb):
            skipped_blank += 1
            continue
        score, usable = mean_veg(rgb)
        if usable < args.min_usable:
            skipped_blank += 1
            continue
        b = bucket_of(score, args.low_cut, args.high_cut)
        if kept[b] >= quota[b]:
            skipped_full += 1
            continue

        name = f"{z}_{x}_{y}.png"
        from PIL import Image

        Image.fromarray(rgb).save(out_dir / name, format="PNG", optimize=True)
        lat_c, lon_c = tile_center(z, x, y)
        rows.append((name, z, x, y, round(lat_c, 6), round(lon_c, 6), round(score, 4), b))
        kept[b] += 1
        done = sum(kept.values())
        print(
            f"  [{done:3d}/{args.count}] {name}  VARI {score:.3f}  {b:<4}  "
            f"({lat_c:.4f}, {lon_c:.4f})"
        )

    if not rows:
        print("no tiles kept — check the bbox and your network", file=sys.stderr)
        return 1

    manifest = out_dir / "manifest.csv"
    with open(manifest, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tile", "z", "x", "y", "lat", "lon", "mean_vari", "bucket"])
        w.writerows(sorted(rows))

    write_labeling_md(out_dir, cfg.city.name, z, lat_mid, len(rows))

    print(f"\nwrote {len(rows)} tiles to {out_dir}/")
    print("  " + ", ".join(f"{b}={kept[b]}/{quota[b]}" for b in BUCKETS))
    print(f"  inspected {inspected} candidates "
          f"({skipped_missing} missing, {skipped_blank} blank/unusable, "
          f"{skipped_full} dropped as bucket-full)")
    print(f"  manifest: {manifest}")
    print(f"  labelling rules: {out_dir / 'LABELING.md'}")
    short = [b for b in BUCKETS if kept[b] < quota[b]]
    if short:
        print(
            "\nNOTE: under quota in " + ", ".join(short) + ". This city's bbox may "
            "simply not contain that much of it at this zoom. Raise "
            "--max-candidates, or move --low-cut/--high-cut and re-run — but do "
            "not pretend the balance is better than it is when reporting."
        )
    return 0


LABELING_MD = """# Labelling these tiles — canopy vs not

{n} Esri World Imagery tiles of **{city}**, zoom {z}, 256x256 px
(~{span:.0f} m across, ~{mpp:.2f} m per pixel). Sampled by
`scripts/geti_export_tiles.py`; `manifest.csv` holds each tile's XYZ address,
centre lat/lon, and the VARI bucket it was drawn from.

The task: **instance segmentation, one label — `canopy`.** Draw the woody
plant cover. Everything you do not draw is background; there is no second
label to draw.

> Why instance and not semantic: Geti v3 **removed** semantic segmentation —
> the only polygon project type left is Instance Segmentation
> (https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/project-management/,
> checked 2026-08-27). It costs us nothing: everything downstream unions the
> instances into one binary canopy mask, so where you split two touching
> crowns has no effect on the canopy fraction we actually measure. Split when
> it is obvious, merge when it is not, and do not agonise over it.

## What counts as `canopy`

**Yes — draw it:**

- Tree crowns — one polygon per crown where crowns are separable, one polygon
  around the whole block where they merge and you cannot pick them apart.
- Large shrubs and hedgerows with visible woody structure — a 2 m hedge along
  a compound wall is canopy.
- Dense scrub and thickets where the texture is clearly woody, not grassy.
- **Shadow cast onto or by a crown that is part of that crown's footprint.**
  Half-shaded trees are still trees. Include the dark side of the crown; the
  model has to learn that canopy is often dark, or it will only ever find
  sunlit treetops.
- Trees over water, over roofs, over parked cars — occluding something else
  does not stop it being canopy.

**No — leave it as background:**

- **Grass and lawn.** Green is not the label; *trees* are the label. The
  studio's existing VARI index already cannot tell these apart, and that is
  precisely the failure this dataset exists to fix.
- **Cropland**, including intensely green irrigated fields.
- Sports turf, cricket outfields, golf greens, artificial pitches.
- Water — rivers, tanks, lakes, canals — however green the water looks.
- Green roofs, green painted surfaces, green shade-cloth, green vehicles,
  green tarpaulins, algae.
- Bare ground, roads, buildings, rubble, sand.
- **Ground shadow with nothing above it** — a building's shadow on open
  ground is background, even when it is next to trees.

**Edge cases, decided once so we decide them the same way every time:**

| Situation | Call |
|---|---|
| Isolated tree < ~3 px across | Draw it if you can see a crown; skip the tile if the whole tile is like this |
| Row of street trees along a road | One polygon per crown where separable, one strip where merged |
| Palm trees | Canopy — draw the frond spread, not the trunk |
| Tall shrub vs low tree | Both canopy; the distinction is not worth the argument |
| Dead / leafless tree | Canopy — it occupies the same planting slot |
| Potted plants on a terrace | Background, too small to matter and not plantable ground |

## The rule that matters most: skip, do not guess

**If you cannot tell what something is, do not annotate the tile at all.**
Move it out of the training set and note it.

A guessed label is worse than a missing one. Half of what makes this dataset
worth more than the VARI index is that a human looked and *knew*; a tile where
the human squinted and picked the likelier option teaches the model to squint
too. Ambiguous tiles are cheap to discard — there are always more tiles.

Reasons to skip a whole tile:
- Heavy cloud, haze, or deep shadow across most of it.
- A seam where two imagery dates meet (Esri World Imagery is a mosaic — one
  tile can be two seasons).
- Motion blur, colour banding, obvious compression mush.
- You genuinely cannot separate a dense orchard from a plantation from scrub.

## How to be consistent

1. Work in **mixed bucket order**, not sorted — take a few `high`, a few
   `mid`, a few `low` in rotation (`manifest.csv` has the bucket column). If
   you annotate all the parks first, you will get better at annotating parks
   and the model will see your worst work on the sparse tiles it needs most.
2. In Geti, reach for the **Automatic Segmentation tool** (`S`, Segment
   Anything behind it) first — hover a crown, accept the outline if it is
   right — and fall back to the **Polygon tool** (`P`) or the **Magnetic
   Lasso** (`M`) when it is not. Accepting a wrong machine outline because it
   was offered is the fastest way to poison this dataset; the assist is there
   to save clicks, not to make the call.
3. Follow the **crown edge**, not the shadow's outer edge on the ground.
3. Be consistent about *tightness*. Slightly loose but uniform beats tight in
   some tiles and loose in others — the model learns your bias either way, but
   only a consistent bias is correctable.
4. Re-read this file after your first ten tiles and fix what you did
   differently. Then leave the rules alone.

## Honest limit, inherited

Esri World Imagery is a **mosaic of scenes from mixed dates and seasons** with
no per-tile date exposed here. A model trained on it inherits that: it learns
"canopy as it looked whenever this patch was last flown", not canopy on a
particular day. Anything built from this dataset carries that caveat, in the
README and anywhere a number from it is quoted.

Imagery (c) Esri, Maxar, Earthstar Geographics.
"""


def write_labeling_md(out_dir: Path, city: str, z: int, lat: float, n: int) -> None:
    """The label rules travel with the tiles, not in someone's head."""
    span = tile_span_m(z, lat)
    text = LABELING_MD.format(n=n, city=city, z=z, span=span, mpp=span / TILE_PX)
    (out_dir / "LABELING.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
