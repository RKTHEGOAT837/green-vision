# Green Vision — Full Project Write-Up

**Tagline:** *Where should this city plant trees next — and what should go in the ground?*

Green Vision is a complete, end-to-end urban-greening decision system. It combines a
Python **priority engine** (`greenplan/`) that ranks every ~5 km² hexagonal cell of a
city by where green cover is declining fastest against worsening air quality, with a
zero-install browser **design studio** (`index.html`) where a planner clicks the map,
reads live environmental conditions across 100 km², draws a plot, places trees from a
curated species catalogue, costs the plantation at real 2026 Indian rates, and projects
canopy, cooling, and survival over 25 years. The two halves share one subject and one
grid: the studio's **Priority view** renders the engine's `recommendations.geojson`
directly on the map, so the AI's ranking, its per-cell plain-English justification, and
its soil-aware species picks all surface exactly where a human will design the planting.

---

## 1. The problem

Indian cities are losing green cover precisely where air quality is deteriorating
fastest, and municipal greening decisions are usually made with none of the data that
already exists to guide them. Satellite vegetation indices, air-quality archives, soil
surveys, and 10 m imagery are all free and global — but they live in different formats,
different resolutions, different APIs, and none of them answers the planner's actual
question, which is a *ranking*: of all the places we could plant this season, which
cells matter most, why, and what species will actually survive there? Existing tools
either show current conditions (dashboards) or require GIS expertise and paid data.
Green Vision closes that gap with free, keyless, global data sources and — by default —
**AI inference that runs entirely on a local CPU**, so no city figures ever leave the
machine and there is no per-token cost.

## 2. What the system does, end to end

1. **Ingest.** CSV adapters load three per-cell monthly streams onto a common Uber H3
   resolution-7 grid (~5 km² cells): real NASA **MOD13Q1** vegetation index (250 m,
   16-day, via ORNL DAAC, keyless), real **US AQI** from the Open-Meteo Air-Quality
   archive (keyless), and a disclosed *inert* traffic placeholder whose MCDA weight is
   pinned to 0.0 so it can never influence a ranking. The measured Ahmedabad panel is
   **146 cells × 42 months (2023-01 → 2026-06)**.
2. **Enrich.** Real soil chemistry per cell — pH, sand/silt/clay texture, organic
   carbon, nitrogen — from **ISRIC SoilGrids v2.0** (250 m, free, keyless). Because
   SoilGrids masks built-up land, the exporter re-samples four offsets around each
   empty cell centre and recovers most of the dense urban core; anything still empty
   falls through to pollution-only species matching rather than inventing values.
3. **Locate.** MODIS answers *how a cell is changing*; **Sentinel-2 L2A at 10 m**
   (via AWS Earth Search STAC, still keyless) answers *where inside the cell the bare
   ground actually is*. The exporter computes true NDVI (NIR vs red) from the most
   recent cloud-free scene and writes the lowest-NDVI ground per cell as candidate
   planting sites — 2,190 measured candidates for Ahmedabad from a 3.9 %-cloud scene,
   each honestly labelled in the file header as a snapshot for a human to verify, not
   a survey.
4. **Learn.** A backtesting loop predicts a *past* month from history strictly before
   a cutoff, scores the prediction against what really happened, asks the model for a
   one-line lesson, and appends it to `models/{city}/memory.jsonl`. Later prompts
   retrieve the most relevant records — **in-context learning with no weight updates,
   no fine-tuning, no retraining**, and each city keeps its own separate memory.
5. **Rank.** A transparent multi-criteria (MCDA) score orders all cells: AQI worsening
   0.40, NDVI decline 0.35, low green cover 0.15, plantable space 0.10, traffic 0.0
   (inert). The AI *explains* the ranking and picks species; it never overrides the
   numeric score.
6. **Recommend.** `--recommend` writes `recommendations.geojson`,
   `recommendations.csv`, and a human-readable `planting_brief.txt`: per-cell priority,
   predicted AQI and canopy change, species matched on soil pH, texture, and pollution
   load, and the model's own justification in plain English.
7. **Design.** The studio loads that GeoJSON, draws each ranked cell as a colour-coded
   hexagon (a warm urgency ramp, deliberately distinct from the green canopy ramp),
   and hands the planner off to a full design workflow: AOI analysis over exactly
   100 km² (r ≈ 5.64 km circle, air quality sampled at 9 points), an OSM feature
   census via Overpass, a canopy heatmap computed from the RGB of Esri World Imagery
   tiles, a 16-species India-climate catalogue matched on goal, pollution tolerance,
   drought tolerance and local rainfall, itemised costing (labour ₹650/day, mali
   ₹14,000/month, water ₹45/kL, 12 % contingency, 8 % design fee, 18 % GST), and a
   25-year projection using logistic canopy growth, survival curves, species lifespan
   and saturating cooling — explicitly labelled `PROJECTED, not forecast` and
   `UNVALIDATED` in the source.

## 3. The AI: measured, honest, and local by default

The project's central engineering claim is that **model selection is an empirical
question, answered per task, from evidence produced by the repo itself.**

**The numeric bake-off.** Every contender got the same held-out task: predict a cell's
metrics 12 months past a cutoff, seeing only history strictly before it, scored against
a Theil–Sen + seasonality baseline on the identical tasks, over the real 146-cell panel:

| Metric | Statistical forecaster (trend + season + memory) | Qwen2.5-1.5B INT4 local LLM | Trained MLP |
|---|---|---|---|
| MAE — AQI | **13.5** | 33.3 | 41.3 |
| MAE — NDVI | **0.042** | 0.103 | 0.173 |
| Skill vs baseline | **+0.075 → +0.084** | −0.385 → −0.333 | −1.41 |

The findings are stated bluntly in the README: a 1.5B-parameter LLM is *worse than a
trend line* at numeric extrapolation, and no prompting fixes that; a neural network
trained on 42 months is worse still, because the residuals left by a robust baseline
are dominated by city-wide shocks that repeat too few times to learn (a ridge probe on
zone-relative residuals confirmed no hidden spatial signal: −0.03). Meanwhile the
statistical forecaster's memory loop genuinely works — skill improves +0.05 from the
first half of training to the second.

**So the default provider is `hybrid`:** numbers come from the measured champion (the
statistical forecaster), and words — per-cell justifications, species selection with
soil fit, projection caveats — come from the local LLM, which produced **0 malformed
replies in 30 iterations** at exactly that job. Champion selection is re-checked from
evidence on every run: `python -m greenplan.forecast.train` scores a trained challenger
on held-out months and writes its report, and the day a longer panel lets it report
positive skill, `hybrid` deploys it automatically. The challenger harness — ONNX
export, OpenVINO inference, `CPU`/`GPU`/`NPU` via the same `model.device` knob — is
production-ready and waiting for data.

**Local inference on Intel OpenVINO.** The default LLM is Qwen2.5-1.5B-Instruct,
weight-compressed to **INT4** OpenVINO IR: ~1 GB on disk instead of ~3 GB at FP16,
**2.5 s to load and ~4 s per reply on a plain laptop CPU**, no GPU anywhere. Because
`model.device` passes straight through to the OpenVINO runtime, the identical build
targets `CPU`, an integrated `GPU`, or an `NPU` on an Intel Core Ultra "AI PC" with
zero code changes. The repo also ships the full self-serve toolchain:
`scripts/fetch_openvino_model.py --list` catalogues ready models (TinyLlama 1.1B →
Phi-3.5-mini 3.8B); `scripts/compress_model_nncf.py` compresses *any* Hugging Face
instruct model to INT4 via Intel NNCF (measured: Qwen2.5-0.5B FP16 → INT4 in 292 s on
a laptop CPU, 343 MB on disk, loads and answers in ~6 s); and
`scripts/bench_devices.py` benchmarks every OpenVINO device the runtime can see —
load time, time-to-first-token, throughput, and whether the reply parsed as strict
JSON. Small-model robustness is engineered, not hoped for: JSON is extracted with a
balanced-brace parser using `strict=False` (small local models emit literal newlines
inside string values), and recommendation calls are **chunked to 3 zones per
generation** for local providers, turning one fragile 2,048-token generation into a
few short, reliable ones, while hosted large models keep the single call.

Hosted providers remain one config line away — NVIDIA NIM
(`meta/llama-3.1-70b-instruct`) or OpenRouter — with keys read strictly from the
environment, never hard-coded.

## 4. Validation honesty (a design principle, not a footnote)

- A forecast horizon can only be *checked* if it fits inside the data.
  `python -m greenplan horizon` computes the ceiling — with 42 months of history and
  `min_history=18`, the largest honestly validatable horizon is **23 months** — and
  requesting more fails loudly. Anything beyond must go through `--project N`, which
  stamps **UNVALIDATED** into the output file itself.
- The traffic stream is a disclosed inert placeholder (weight 0.0), because no free
  historical traffic source exists for arbitrary coordinates.
- Ten years of history is impossible from free keyless sources — Open-Meteo's AQI
  archive returns nothing before 2023-01 (verified back to 2013) — so the README says
  exactly that instead of claiming a decade of data.
- The studio's 25-year projection is labelled a *defensible shape, not a prediction*.
- Every accuracy number quoted anywhere in the project is reproduced by a script in
  the repo.

## 5. Privacy, stated accurately

Designs never leave the browser (LocalStorage; nothing is uploaded unless the user
sets `COMMUNITY_URL` and publishes deliberately). With `provider: openvino` or
`hybrid`, the engine's reasoning never leaves the machine — no key, no endpoint, no
per-cell figures in anyone's logs. What still goes out is the map layer itself:
clicks send coordinates to Open-Meteo, Overpass, Nominatim and Esri. So the claim is
**local-first, and fully local for inference — but the map layer is not offline**.

## 6. Scale: a second city is one config file

Everything downstream of `config/<city>.yaml` is city-agnostic, and every data source
(NASA, Open-Meteo, SoilGrids, OSM, Sentinel-2) is global — the recipe works for any
city on Earth with a bounding box. **Delhi is live end-to-end**: real AQI
(10,419 rows, 2023-01 to 2026-06), 42 months of MODIS NDVI (144 cells), SoilGrids,
Sentinel-2 10 m NDVI with 3,390 candidate planting sites, and a completed recommend
run with Delhi's own backtest (memory_helped: true, skill +0.03) and its own
species picks. Crucially, each city's run backtests *itself*, so each city ships
with its own measured skill — never Ahmedabad's borrowed one.

## 7. Technology stack

- **Engine:** Python; H3 hexagonal grid (res 7); pluggable adapters
  (`mock` / `csv` / custom); MCDA ranking; backtest-driven JSONL memory;
  Theil–Sen + seasonal statistical forecaster; scikit-learn-trained MLP and random-forest challengers
  exported to ONNX; OpenVINO GenAI runtime with INT4 NNCF compression;
  providers `hybrid | openvino | nvidia | openrouter | mock`; CLI + server;
  Dockerfile for containerised runs.
- **Data:** NASA MOD13Q1 (ORNL DAAC), Open-Meteo Air-Quality archive, ISRIC
  SoilGrids v2.0, Sentinel-2 L2A via AWS Earth Search STAC, OpenStreetMap Overpass,
  Nominatim, Esri World Imagery — all free, all keyless.
- **Studio:** single-file HTML/JS/Leaflet; no install, no server, no build step;
  runs from `file://` or `http://`.

## 8. How to run it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m greenplan run --config config/city.yaml --mock --recommend   # zero-network demo
python scripts/fetch_openvino_model.py                                           # ~1 GB, once
.venv/bin/python -m greenplan run --config config/city.yaml --recommend          # real local run
```

Then open `index.html`, switch to the **Priority** view, and design on the ranked map.

## 9. What is deliberately not claimed

No soil moisture (NASA SMAP requires an Earthdata login, breaking the no-key
property); no historical imagery in the studio (history lives in the engine); no
pretence that low 10 m NDVI is always plantable ground (it can be water, rock, or
roofs at scene time — candidates for a human to verify). The project's differentiator
is exactly this: every number is measured, every limitation is written down next to
the feature it limits, and the whole AI stack runs on the machine in front of you.
