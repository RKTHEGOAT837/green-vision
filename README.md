<div align="center">

# 🌳 Green Vision

### Where should this city plant trees next — and what should go in the ground?

**Two parts, one grid, one rule: no number without its source.**

[![Engine](https://img.shields.io/badge/engine-Python%203.11%2B-1f6f4a)](#the-engine)
[![Studio](https://img.shields.io/badge/studio-one%20HTML%20file-1f6f4a)](#the-studio)
[![Runs offline](https://img.shields.io/badge/runs-fully%20offline-1f6f4a)](#running-it)
[![No API keys](https://img.shields.io/badge/API%20keys-none%20required-1f6f4a)](#running-it)
[![Local inference](https://img.shields.io/badge/inference-Intel%20OpenVINO-0068b5)](#the-reasoning-model)
[![Licence](https://img.shields.io/badge/licence-MIT-555)](LICENSE)

[![Ahmedabad](https://img.shields.io/badge/Ahmedabad-146%20cells-1f6f4a)](#the-cities)
[![Delhi](https://img.shields.io/badge/Delhi-127%20cells-1f6f4a)](#the-cities)
[![Mumbai](https://img.shields.io/badge/Mumbai-142%20cells-1f6f4a)](#the-cities)
[![Bengaluru](https://img.shields.io/badge/Bengaluru-133%20cells-1f6f4a)](#the-cities)
[![Chennai](https://img.shields.io/badge/Chennai-84%20cells-1f6f4a)](#the-cities)

Built for the **India AI Impact Festival 2026**.

**Ahmedabad · Delhi · Mumbai · Bengaluru · Chennai** ship with real data —
632 hexagonal cells, 42 months of satellite and air-quality history each.
Any other city is one config file and four data exports away.

</div>

---

## What it does

Most urban-greening tools stop at a heat map. Green Vision carries the answer
through to a **costed, reviewable planting design on a specific plot**.

| | |
|---|---|
| **`greenplan/`** | **The priority engine.** Ranks every H3 cell in a city by where green cover is declining fastest against worsening air quality, forecasts which cells lose canopy next, and picks species against measured soil and pollution. Python, offline, one command. |
| **`index.html`** | **The design studio.** Click a place, read live conditions across 100 km², draw a plot, place trees, cost it in rupees, and get a twelve-check design review — plus a 3D builder that reconstructs the real buildings and streets around your site. One file, no build step. |

---

## The cities

Each is real data on the same H3 res-7 grid — 42 months of NDVI and air quality,
its own trained memory, its own ranking. Nothing is shared between them but code.

| City | Config | Cells | AQI range | NDVI range | Notes |
|---|---|---:|---|---|---|
| **Ahmedabad** | `config/city.yaml` | 146 | 75–81 | 0.15–0.40 | reference city, soil included |
| **Delhi** | `config/delhi.yaml` | 127 | 301–622 | 0.14–0.54 | ⚠️ 55 cells past the AQI scale — see below |
| **Mumbai** | `config/mumbai.yaml` | 142 | 62–74 | −0.05–0.64 | coastal; 5 water cells carry no plantable estimate |
| **Bengaluru** | `config/bengaluru.yaml` | 133 | 49–50 | 0.20–0.57 | cleanest air and greenest of the five |
| **Chennai** | `config/chennai.yaml` | 84 | 70–75 | −0.07–0.64 | coastal; 2 water cells |

```bash
python -m greenplan.server --config config/delhi.yaml     # any city, same command
python scripts/build_city.py --config config/mumbai.yaml  # run it and print a checkable summary
```

The differences are the point: Bengaluru really is the greenest and cleanest,
Delhi's air really is an order of magnitude worse, and the two coastal cities
really do have cells that are partly sea. If a new city's numbers came out
looking like Ahmedabad's, that would be the bug.

**Two caveats these cities exposed**, both left visible rather than smoothed away:

*Delhi runs off the top of the AQI scale.* The US index is undefined above 500 —
the EPA's own term is "Beyond the AQI" — and CAMS reports raw values well past it
during the pre-monsoon dust season, up to a monthly mean of 1050. The value is
kept because the ranking needs the ordering, but those cells are flagged
`aqi_beyond_index` and the studio shows **"500+"** rather than a number the scale
cannot express.

*Coastal cells cannot be measured by the plantable proxy.* `1 − NDVI` reads open
water as almost entirely plantable; Mumbai's top-ranked cell sits over Colaba,
roughly half of it sea, and came out at 0.96. Cells with negative NDVI now carry
**no plantable estimate at all** rather than a confident wrong one. Inland cities
are unaffected.

Soil is exported per city and is optional — without it species are matched on
pollution tolerance alone, and the engine says so rather than pretending.

---

## The rule that shaped everything

> **A figure you cannot trace is worse than no figure at all.**

Every number on screen carries a provenance chip:

| Chip | Means |
|---|---|
| 🟢 **Measured** | An instrument or a survey produced it |
| 🟡 **Modelled** | A documented formula transformed measured inputs |
| 🔴 **Assumed** | A catalogue rate or planning default — replace it with your own |

This is not decoration. It is why the tool **refuses rather than guesses**: when
OpenStreetMap does not answer, the site finder declines instead of dropping a
plot on a building. A satellite "plantability" classifier was built, measured —
it scored a clubhouse roof 60% plantable and open water 61% — and **deleted**.

---

## Where the data comes from

Every stream is free, key-less, and re-fetchable by a script in this repo.

| Signal | Source | Resolution | Role |
|---|---|---|---|
| **NDVI** | NASA MOD13Q1 via ORNL DAAC | 250 m, 16-day | 🟢 5,415 rows. Green-cover decline and the loss forecast |
| **Air quality** | Open-Meteo Air-Quality archive | ~11 km CAMS | 🟢 6,135 rows. Highest-weighted ranking criterion |
| **Climate normal** | Open-Meteo ERA5, **1991–2020** | ~9 km | 🟢 Rainfall and days over 40 °C — sets the irrigation budget |
| **Soil** | ISRIC SoilGrids v2.0 | 250 m | 🟡 pH and texture for 120 of 146 cells. Gates species choice |
| **Buildings, roads, land use** | OpenStreetMap | vector | 🟢 The sole siting authority — served **locally**, see below |
| **Imagery** | Esri World Imagery, Google Street View | sub-metre | Visual verification only. Nothing is computed from pixels |
| **Traffic** | *no free historical source exists* | — | 🔴 Disclosed placeholder, **MCDA weight 0.0**. It cannot move a ranking |

### OpenStreetMap, served from your own machine

The public Overpass API allows **two requests per IP at a time** and returns
HTTP 429 for the third — which made the census, the traffic panel and the 3D
builder a coin toss at peak hours. Of seven public endpoints measured from a
browser, exactly two serve Indian data with CORS.

So this repo indexes a [Geofabrik](https://download.geofabrik.de/asia/india/)
extract onto your machine and serves it locally: **no rate limit, no network,
no third-party dependency.**

```bash
python scripts/osm_index.py --pbf data/osm/western-zone-latest.osm.pbf \
                            --out data/osm/index.jsonl.gz
```

| | Public Overpass | Local index |
|---|---|---|
| Census query | 25 s, often 429 | **145 ms** |
| Road segments (traffic) | 311 | **1,139** |
| Traffic signals | **0** | **67** |
| Buildings with surveyed height | **0 of 32** | **22 of 33** |
| Named features in 3D view | 6 | **28** |

Points outside the indexed extract **fall back to the public API** rather than
answering "nothing here" — an empty result and an out-of-coverage result are
different claims, and the code never conflates them.

---

## How the ranking works

**Trend** — Theil–Sen, the median of all pairwise slopes. Ordinary least
squares has a breakdown point of zero: one cloud-contaminated MODIS composite
drags the line. Theil–Sen tolerates ~29% garbage.

**Forecast** — the seasonal term is learned *after* detrending, so a declining
series does not contaminate its own seasonal profile:

```
estimate = slope · t + level + seasonal
```

**Deltas are year-over-year**, so "worsening" means real deterioration rather
than the monsoon cycle.

**Score** — four weighted criteria, min-max normalised, from `config/city.yaml`:

```
score = 0.40 · Δ air quality (predicted)
      + 0.35 · NDVI decline
      + 0.15 · low green cover now
      + 0.10 · plantable space
      + 0.00 · traffic          ← inert placeholder
```

> **The MCDA ranking is authoritative and purely numeric.** The language model
> never reorders it. It writes justifications and picks species, nothing more.

---

## Training — and an honest negative result

**The deployed forecaster has no learned weights.** `greenplan/training/` runs a
rigorous backtest and accumulates an in-context memory that later predictions
retrieve from. Models *with* weights are trained here — in order to be **scored**
— and they lose.

Every candidate is measured on the same held-out task: predict a cell's AQI and
NDVI 12 months out, history strictly before the cutoff, **split by time, not
randomly** (1,056 train / 396 test).

| Candidate | Held-out skill | |
|---|---|---|
| **Statistical forecaster + memory** | **+0.043** | ✅ **deployed** |
| RandomForest, 200 trees | −0.500 | benched |
| MLP 64×32 | −1.408 | benched |

With 42 months of history, city-wide shocks dominate the residual and neither
trained challenger finds signal the robust baseline misses. **The negative
result is the finding.**

The harness is production-ready, not a sketch: every exported graph is re-run
through the **OpenVINO runtime** against sklearn's own predictions — worst
deviation **1.43e-06** (MLP), **2.10e-07** (forest), against a 1e-3 tolerance.
Intel oneDAL cuts forest training **1.5 s → 0.7 s** with skill unchanged.

```bash
pip install -r requirements-forecast.txt
python -m greenplan.forecast.train --config config/city.yaml --model rf --intel
```

---

## The studio

**Area analysis** samples a **nine-point grid** across the perimeter and reports
the mean *and the spread* — a 30-point AQI difference between corners is a
different planning problem, and a single centre reading erases it.

**CPCB National AQI**, computed from concentrations against the official
breakpoints (CUPS/82/2014-15, Table 3.11), because that is the index an Indian
municipal user acts on. The US EPA figure is shown beside it, clearly labelled —
the same air reads *CPCB 58 "Satisfactory"* and *US EPA 62 "Moderate"*.

**Costing** is a bill of quantities, not a per-tree multiplier: site preparation,
planting, and the three-year establishment line most estimates omit. Irrigation
scales with the site's own 30-year rainfall normal, so the same design costs
₹56.2 L in one climate and ₹70.4 L in another.

**Design review** — twelve weighted checks encoding published practice:
Santamour's 10/20/30 rule, mature-canopy spacing, water balance against the
site's own rainfall, shade over walking routes. Only safety, water, canopy and
diversity may reach *critical*; a design with no benches is not in the same
category as one that plants oleander beside a playground.

**3D builder** — reconstructs the real block from OSM at true scale, with
surveyed heights distinguished from inferred ones on the model itself, and real
names on buildings and roads.

---

## The assistant

Not a chat wrapper. **31 intent patterns → 29 handlers**, each calling the same
engine functions the interface calls. Pure standard library.

```
normalise (fold Indic digits) → ordered regex → native-language keywords
  → slot extraction → handler → engine call → answer with provenance
```

Because every answer is a formatted tool result, the failure mode a language
model has — a fluent, confident, invented figure — is **structurally
unavailable**. The worst it can do is decline.

Ships in **English, Hindi, Gujarati, Marathi and Bengali**.

---

## Running it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
python -m greenplan.server --config config/city.yaml
# open http://127.0.0.1:8000/
```

No API key. No account. `index.html` also opens straight from disk — live
layers degrade to a stated "unavailable" rather than to a guess.

**Static build** (no server at all):

```bash
python scripts/build_static.py --config config/city.yaml   # → dist/, ~327 KB
```

### The reasoning model

Local by default: an Intel **OpenVINO** INT4-compressed Qwen2.5-1.5B-Instruct on
the CPU. No key, no per-token cost, and **no city's figures leave the machine
planning that city.** Absent the weights, the server falls back to a
deterministic offline writer and `/api/health` says so rather than pretending.

---

## Testing

Five layers, each aimed at a failure class the others cannot see.

| Layer | Catches | Status |
|---|---|---|
| Syntax + import gate | Silent failure in a 457 KB single-file app | ✅ |
| **Engine invariants** | Property violations that survive tuning | **23 / 23** |
| **Python ↔ JS parity** | Silent drift between the two implementations | **31 / 31** |
| **Local OSM subset** | A partial query interpreter mis-answering | **30 / 30** |
| Live DOM + visual audit | Contrast, clipping, overlap, focus, tap targets | ✅ |
| Cross-boundary scope scan | Code that *looks fine and does nothing* | **0 leaks** |

Every published cell value is re-derived from the source CSVs through the
engine's own panel: **584 / 584 exact.**

The parity corpus deliberately contains gibberish (`"asdf qwerty zxcv"`). Every
message used to match a regex, which is exactly why a crash in the regex-*miss*
fallback survived every parity run ever made.

---

## Known limits

Stated here so nobody discovers them in a review meeting.

- **Cost rates are not verified** against a published schedule of rates. Chipped
  🔴 *Assumed*; replace with your own SOR.
- **The species table is not fully sourced** — 34 species with a standing
  `TODO: VERIFY`. Relative ordering is sound; check absolutes before a tender.
- **Traffic is a placeholder**, weighted 0.0. Nothing depends on it.
- **CPCB AQI is hourly fed into a 24-hour scale** — it reacts faster than the
  official daily bulletin and will not equal it. Stated in the interface.
- **42 months of history** — enough for a validated 12-month horizon, not enough
  for multi-year monsoon cycles, and not enough for a trained model to win.
- **SoilGrids covers 120 of 146 cells**; it masks built-up land.
- **Five cities, one of them fully complete.** Ahmedabad has soil; the other
  four are exporting it. Species there are matched on pollution tolerance alone
  until it lands, which the interface states.
- **Delhi carries a data caveat** (beyond-index AQI) and the coastal cities
  carry another (water cells). Both are described above rather than hidden.

---

## Licence

[MIT](LICENSE) · Data © OpenStreetMap contributors ([ODbL](https://www.openstreetmap.org/copyright)) ·
NASA MOD13Q1 · Open-Meteo · ISRIC SoilGrids · Esri World Imagery

<div align="center">

**🌳 Green Vision** — India AI Impact Festival 2026

</div>
