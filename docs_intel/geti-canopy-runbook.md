# Canopy segmentation on Intel Geti — runbook

> **Status: PLAN.** Nothing in this document has been trained or measured yet.
> No accuracy number appears here because none exists. What follows is the
> procedure to produce one, and the rule that decides what happens to it.

**The problem.** The studio's canopy layer is `vegScore()` in `index.html`
(~line 236): VARI = (g−r)/(g+r−b) behind three brightness gates, computed on
Esri World Imagery RGB. It is a colour rule. It cannot tell a tree from a
cricket outfield, a mature crown from an irrigated lawn, or a shadowed canopy
from a dark roof — and the README already says the studio's canopy reading is
imagery-derived, not measured. Those confusions matter for a tool whose whole
job is deciding where trees are missing.

**The proposal.** Train a supervised segmentation model on human-drawn tree
crowns over the same Esri tiles, using **Intel Geti**, export it to
**OpenVINO IR**, and test it head-to-head against VARI on tiles neither method
has seen. Then apply §6's gate. The engine already runs on OpenVINO
(`provider: hybrid`, Qwen2.5-1.5B INT4 on CPU), so a second OpenVINO model is
a familiar dependency, not a new one.

**Read §0 before you plan your week.** Geti changed a lot in 2026 and several
things a student would expect from older tutorials no longer exist.

---

## 0. What Geti actually is right now

All of the following was checked against primary sources on **2026-08-27**.
Every claim has a URL. Where I could not verify something from a page I
actually fetched, it says UNVERIFIED and you should check it yourself rather
than trust this file.

### It is open source, local, and free

| Fact | Value | Source |
|---|---|---|
| Repository | `github.com/open-edge-platform/geti` | https://github.com/open-edge-platform/geti |
| Licence | **Apache-2.0** (relicensed at v3.0.0) | https://github.com/open-edge-platform/geti/releases/tag/app%2Fv3.0.0 |
| Current release | **v3.1.0**, tag `app/v3.1.0`, 2026-08-13 | https://github.com/open-edge-platform/geti/releases |
| Docs | https://docs.geti.intel.com/ (v3 default, v2 archived under `/docs/2.0/`) | — |
| Account / signup / cost | **None.** Runs entirely on your machine | https://docs.geti.intel.com/docs/user-guide/reference/faq/ |

Docs URLs need a **trailing slash** or you get an empty page shell.

### The hosted trial is gone

`geti.intel.com` and `geti.intel.com/request-trial` now redirect to Intel's
dead-link handler. The v3 FAQ says Geti "runs entirely on the machine where
you install it", and v3 removed workspaces and user management entirely
(https://docs.geti.intel.com/docs/user-guide/getting-started/installation/upgrade/whats-new-and-removed/).
Search engines still surface "request a trial" pages and reseller SKUs for
the old commercial platform — those are dead. *(UNVERIFIED: I found no formal
Intel notice announcing the SaaS retirement, only the redirects and the FAQ.)*

**So "browser access to Geti" now means one thing: a browser pointed at a Geti
server that someone is running.** There is no instance to sign up for.

### Geti v3 does not run on macOS

This is the single biggest planning fact for this team, and it is stated
plainly in the FAQ:

> "Geti™ is supported on x86_64 Linux and Windows hosts. Running through
> Docker Desktop on an M-series Mac is possible via emulation but is not a
> supported configuration and will be slow."
> — https://docs.geti.intel.com/docs/user-guide/reference/faq/

Documented minimum requirements are modest — **8 CPU threads, 16 GB RAM, 40 GB
free disk**, GPU **optional** (Intel XPU or NVIDIA, for larger models). No
NVIDIA card is required; the FAQ confirms you can pick CPU in the training
device selector and simply wait longer.
(https://github.com/open-edge-platform/geti/blob/develop/application/docs/install.md)

Documented install paths, and which are realistic for a student on a Mac:

| Path | What it is | Realistic here? |
|---|---|---|
| **Windows MSIX app** | `geti-cpu-3.x.msix` etc. from `storage.geti.intel.com` | Yes, on a borrowed Windows laptop. Note: MSIX builds **omit Ultralytics/YOLO models** for AGPL reasons |
| **Docker image** | `ghcr.io/open-edge-platform/geti-cpu` (also `-xpu`, `-cuda`); needs **Docker v29+**; UI at **https://localhost:7860** | Yes — on an x86_64 Linux box. On Apple Silicon it is emulated, unsupported and slow |
| **Install script from source** | bash (Linux/WSL2) or PowerShell, auto-detects hardware | Yes on Ubuntu 24+ / WSL2 |
| **Run from source** | Node.js v24.2+, `just` v1.46+, UI on `http://localhost:3000` | Only if you are already comfortable with the toolchain |

Supported OS as documented: **Ubuntu 24+, WSL2 with Ubuntu 24+, Windows.**

**The honest recommendation for a student on a Mac, in order:**

1. **Run Geti on any x86_64 Linux or Windows machine you can reach** — a lab
   PC, a desktop, a cloud VM with 8 threads / 16 GB / 40 GB — and open its UI
   in Safari or Chrome on the Mac. This is the path that gives you the actual
   Geti annotation UI, which is the point of using Geti.
2. **Windows on a borrowed laptop**, MSIX install, work locally.
3. **Docker Desktop on the Mac under emulation.** It may start. Expect it to
   be slow enough that a training round is an overnight job, and expect no
   support if it breaks. Do not build a demo deadline on this.
4. **Skip the app; use `getitune`** — the Python training library that
   replaced OpenVINO Training Extensions (`otx`). It is documented for
   **Linux, Windows *and* macOS**, Python 3.11–3.14, and exports OpenVINO IR
   directly (https://docs.geti.intel.com/docs/user-guide/library/get-started/installation/,
   https://pypi.org/project/getitune/). You lose the browser annotation UI —
   you would annotate elsewhere, e.g. CVAT or Label Studio, and import COCO.
   *(UNVERIFIED: whether Apple MPS/Metal acceleration works under `getitune`;
   the docs only make CPU explicit.)*

Pick the path before you download a single tile. Path 1 is what the rest of
this runbook assumes.

### Semantic segmentation was removed — instance segmentation is the only option

Geti v3 offers exactly **three** project types
(https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/project-management/):

- **Object detection** — "Locate objects within an image using Bounding Boxes"
- **Instance Segmentation** — "Precisely outline objects using Polygons"
- **Image classification** — single-label and multi-label

Removed in v3: **semantic segmentation**, task chaining, hierarchical
classification, rotated detection, keypoint detection; anomaly detection moved
to a separate product, Anomalib Studio. Models trained on 2.x cannot migrate,
and project import/export is unavailable.
(https://docs.geti.intel.com/docs/user-guide/getting-started/installation/upgrade/whats-new-and-removed/)

So the "semantic vs instance — which should we pick?" question answers itself:
**Instance Segmentation, because it is the only polygon project type that
exists.** Fortunately it costs us nothing — see §2.

### Annotation tools (v3)

| Tool | Key | Documented as |
|---|---|---|
| Selection | `V` | select and edit existing annotations |
| Bounding Box | `B` | detection projects only |
| **Polygon** | `P` | "pixel-level precision"; instance segmentation only |
| **Magnetic Lasso** | `M` | "delineate an object by drawing lines around its edges" |
| **Automatic Segmentation** | `S` | smart hover-to-preview tool, **backed by Segment Anything** |

https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/annotations/annotation-tools/

There is **no brush, no watershed, and no "quick selection"** in v3. Those
existed in Geti 2.x (Object selection / Object coloring / Interactive
Segmentation) and are gone. If a tutorial mentions them, it is a 2.x tutorial.

### Training is manual in v3 — the "12 images" number is historical

Older Geti documentation had an auto-training trigger, stated as a flat count:

> "In auto-training mode, Geti™ currently requires a fixed number of newly
> annotated images (12) before auto-training starts…"
> — Geti **2.x** release notes, https://docs.geti.intel.com/docs/2.0/user-guide/release-notes/cloud/december-release/

That is a **v2 fact**. In v3 there is no active-learning page in the docs at
all and training is a button you press:

- Quick start: "click **Train model** to open the training dialog", with the
  guidance "aim for **20–50 images** that look like the data your model will
  see in production"
  (https://docs.geti.intel.com/docs/user-guide/quick-start/training-your-first-model/)
- FAQ: "Often as few as **30 annotated images** thanks to transfer learning."
- Annotation mode still does *pre-labelling*: "Upon annotating a given number
  of images yourself, the application will start annotating the images for you
  and prompt you to accept, reject, or edit" — **no number is given, and this
  is pre-labelling, not a training trigger**
  (https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/annotations/annotation-mode/).

*(UNVERIFIED: any per-task-type annotation threshold — e.g. "3 for
classification, 12 for segmentation". No fetched page states one. Do not
repeat that claim.)*

**Do not write "Geti auto-trains after 12 images" in a submission.** It is
true of a version you are not running.

### Export goes straight to OpenVINO IR

From the v3 quick start, verbatim:

> "click the download button on the variant you want (OpenVINO IR in FP32,
> FP16, or INT8, or ONNX), and load it with the OpenVINO™ runtime in your own
> application."

Variants comprise "the trained PyTorch model plus OpenVINO (FP32 / FP16 /
INT8) and ONNX variants generated from it." The FAQ confirms models run
without Geti.

Two things that changed and will trip you up:

- **There is no "Deployments" tab in v3.** Downloads live on the **Model
  Details** screen. The Deployments / code-deployment bundle (inference
  models + sample image + Jupyter notebooks + `deployment.load_inference_models`)
  is a **2.x** feature; its docs only exist under `/docs/2.0/`.
- **`geti-sdk` is deprecated.** Its own README says so: "no longer maintained
  and does not support Geti v3.0+"
  (https://github.com/open-edge-platform/geti-sdk). v3 points at a REST API
  with autogenerated clients (https://docs.geti.intel.com/docs/rest-api/spec).
  Anything that tells you to `pip install geti-sdk` and pull a deployment is
  out of date.

*(UNVERIFIED: whether a v3 download bundle ships an OpenVINO **Model API**
(`model_api`) wrapper or config. The docs say only "load it with the OpenVINO
runtime". Check the downloaded folder before writing inference code that
assumes Model API.)*

### Media limits worth knowing before you upload

Images `.jpg .jpeg .png .jfif .tif .tiff .webp .bmp`; **32–20 000 px per
side**; dataset import capped at **40 GB**; import formats Datumaro, COCO,
YOLO, Pascal VOC.
(https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/datasets/dataset-management/)

Our tiles are 256×256 PNG — comfortably inside every limit.

---

## 1. Build the dataset

```bash
pip install Pillow                                   # only dep this script adds
python scripts/geti_export_tiles.py --config config/city.yaml
```

The script samples Esri World Imagery tiles across the city bbox — the same
tiles, the same endpoint, the same `{z}/{y}/{x}` ordering the studio already
uses.

| Flag | Default | What it does |
|---|---|---|
| `--config` | `config/city.yaml` | bbox comes from `city.bbox`, same as every other exporter |
| `--count` | `120` | tiles to keep |
| `--zoom` | `17` | ~1.1 m/px at 23° N, ~280 m per tile |
| `--out-dir` | `data/geti_tiles` | |
| `--max-candidates` | `5 × --count` | hard cap on tiles downloaded; stops a runaway |
| `--low-cut` / `--high-cut` | `0.08` / `0.34` | bucket edges on mean VARI |
| `--min-usable` | `0.60` | skip tiles that are mostly no-data or blown out |
| `--sleep` | `0.4` | seconds between requests — politeness, leave it alone |
| `--seed` | `42` | same default as `run.seed`; reruns reproduce the sample |

**What lands where** (`data/geti_tiles/`):

- `{z}_{x}_{y}.png` — one RGB tile each, filename *is* its XYZ address, so any
  tile can be re-derived, re-fetched, or plotted on the map later.
- `manifest.csv` — `tile,z,x,y,lat,lon,mean_vari,bucket`.
- `LABELING.md` — the labelling rules, written next to the data so the
  annotator cannot fail to find them. **Read it before annotating.**

**Why stratified, and what stratification does *not* mean.** A uniform random
draw over Ahmedabad's bbox returns mostly rooftops and scrub; a model trained
on that learns "predict no canopy", which is correct often enough to look
fine and be useless. So the script ports `vegScore()` to Python, scores each
candidate, and fills three buckets evenly — `low` / `mid` / `high`, split at
0.08 and 0.34, which are two of the studio's own `heatColor()` breakpoints.
**VARI decides which tiles a human looks at. It never decides a label.** The
label is the human's, and that is the entire source of the model's advantage.

The script reports its bucket fill and tells you when a bucket came up short.
Report that number as it is; do not re-run with moved cut-points and present
the result as balanced.

**Runtime and repo hygiene.** 120 tiles with a `5×` candidate cap is up to 600
polite requests — roughly 5–10 minutes. 120 PNGs is ~15 MB. Add
`data/geti_tiles/*.png` to `.gitignore` and commit only `manifest.csv` and
`LABELING.md`: the manifest is enough to regenerate the exact tile set, and
Esri's terms govern redistributing their imagery even though access is
keyless. Keep "Imagery © Esri, Maxar, Earthstar Geographics" on anything you
publish from this.

---

## 2. Create the Geti project and upload

1. Open the Geti UI (`https://localhost:7860` for the Docker image — expect a
   self-signed certificate warning).
2. **Create project → Instance Segmentation.**
   - Not a choice: v3 removed semantic segmentation (§0).
   - It is also not a loss. Everything downstream unions the predicted
     instances into **one binary canopy mask** before measuring anything, so
     where the annotator splits two touching crowns has no effect on the
     canopy fraction we score. Instance segmentation is semantic segmentation
     plus information we then throw away — and the extra information is free
     if we ever want per-tree counts.
3. **One label: `canopy`.** Instance segmentation requires at least one label,
   so one is legal. Resist adding `grass` or `water` "for completeness": every
   extra class multiplies the annotation budget and none of them are what the
   studio asks the imagery.
4. **Upload** `data/geti_tiles/*.png`. 256×256 PNG is inside every documented
   limit.
5. Do **not** upload the held-out set. See §6 — this is the one step that, if
   you get it wrong, invalidates the whole exercise and cannot be undone.

Practically: run the exporter twice with different seeds into different
directories, and let only one of them anywhere near Geti.

```bash
python scripts/geti_export_tiles.py --count 120 --seed 42 --out-dir data/geti_tiles
python scripts/geti_export_tiles.py --count  40 --seed 99 --out-dir data/geti_holdout
```

The manifests overlap only by coincidence at these sizes; check for and drop
any tile id that appears in both before you upload anything.

---

## 3. Annotation strategy

Follow `data/geti_tiles/LABELING.md`. It is the source of truth for what
counts as canopy; the short version is **trees and woody shrubs yes, grass and
cropland and turf no, shadow that belongs to a crown yes, and if you cannot
tell — skip the tile rather than guess.**

**Order matters.** Annotate in **mixed bucket order** — a few `high`, a few
`mid`, a few `low`, in rotation, using the `bucket` column of `manifest.csv`.
Two reasons:

1. Annotators get better with practice. If you do all the parks first, your
   worst work lands on the sparse tiles, which are exactly where VARI already
   struggles and where the model has to win.
2. Geti trains on what you have annotated so far. Annotating one bucket first
   produces a first model that has only ever seen parks.

**Realistic counts.** Stated separately, because they are different questions:

| Milestone | Annotated tiles | Basis |
|---|---|---|
| Enough to *see a model exist* | **~20–30** | Geti v3 quick start: "aim for 20–50 images"; FAQ: "often as few as 30 annotated images thanks to transfer learning" |
| Enough to be **credible for the fair test** | **60–100** | Judgement, not documentation — flagged as such |
| **Held out, never uploaded to Geti** | **at least 30** | §6 |

The 60–100 figure is **our estimate, not an Intel claim.** It comes from
wanting each of the three buckets to carry 20–30 annotated tiles so the model
sees dense canopy, sparse street trees and near-bare ground in comparable
amounts. If you stop at 30 total, say "30" in the write-up and let the
held-out numbers speak; a small honest dataset with a measured result beats a
bigger one with a hand-wave.

**Time.** At 256×256 with the Automatic Segmentation tool doing most of the
outlining, budget roughly 2–5 minutes per tile — more on dense canopy. 80
tiles is a long afternoon, not a weekend. Do it in two sittings, and re-read
LABELING.md at the start of the second one.

**Use the assist, do not obey it.** The `S` tool is Segment Anything; it is
very good at crowns with clean edges and confidently wrong on merged canopy
and on shadow. Accepting a wrong outline because it was offered is the fastest
way to poison the dataset.

---

## 4. Train and iterate

The loop, as v3 documents it:

1. Annotate a batch in mixed bucket order.
2. Press **Train model**. Pick the training device (CPU is fine; slower).
   Architecture tiers are offered as Balance / Speed / Accuracy — take the
   default for the first run and change one thing at a time afterwards.
   (https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/model-training-and-optimization/training-configuration/)
3. Once a model exists, annotation mode will **pre-label** new tiles for you to
   accept, reject or edit. This is the labour-saving part of the loop. It is
   also the part that quietly drags your labels toward the model's current
   bias — reject freely, especially on the `low` bucket.
4. Repeat. Retrain after each batch of roughly 20 new annotations.

**When to stop.** Stop when the *pre-labels stop needing correction* — when
you are accepting most suggested crowns unedited on tiles from all three
buckets. That is a better stopping signal than Geti's own validation score,
because the validation set is a slice of your own annotations and shares every
bias in them. The number that decides anything is in §6, and it is computed
outside Geti.

Two habits worth keeping from the moment you press Train the first time:

- Record the **dataset revision** each model was trained on. Geti lets you fix
  the train/val/test split per revision; without the revision id you cannot
  say later what a number referred to.
- Keep a plain-text log: date, tiles annotated, architecture, val score. It
  costs nothing and it is the difference between "the model got better" and
  "we can show the model got better".

---

## 5. Export to OpenVINO IR

On the **Model Details** screen, download the variant you want: **OpenVINO IR
FP32 / FP16 / INT8, or ONNX.** There is no Deployments tab in v3, and no
`geti-sdk` — see §0.

Which precision: take **FP32 first** and treat it as the reference. Get a
measured number with it before you consider INT8. The engine's LLM is INT4
because a 1.5B model at FP16 was 3 GB and the compression is what made it
practical on a laptop; a small segmentation model has no such problem, and
quantisation is one more thing that can silently move your score. If you do
compare precisions, report all of them — that comparison is itself a decent
Intel-stack result.

**Proposed repo path:**

```
models/geti/canopy/
  canopy.xml            # OpenVINO IR topology
  canopy.bin            # weights
  MODEL_CARD.md         # dataset revision, tile count, buckets, train date, Geti version
  metrics.json          # the §6 numbers — the only place accuracy claims may live
```

`models/` is **gitignored** in this repo (it holds runtime state and fetched
model weights), which is right for the IR files and wrong for the evidence.
So:

- The `.xml`/`.bin` stay untracked. Ship them as a release asset or a small
  fetch script, the way `scripts/fetch_openvino_model.py` already handles the
  LLM.
- **Copy `MODEL_CARD.md` and `metrics.json` into `docs/intel/` where they are
  tracked**, or add a `!models/geti/canopy/*.md` exception. A number nobody
  can find in the repo is a number nobody can check.

Loading it is ordinary OpenVINO — the same runtime the engine already
depends on via `openvino-genai`, so no new heavy dependency:

```python
import openvino as ov
core = ov.Core()
model = core.compile_model("models/geti/canopy/canopy.xml", "CPU")   # or GPU / NPU
```

`model.device` in `config/city.yaml` passes straight through to OpenVINO for
the LLM today; the harness in §6 should take the same `--device` flag so the
canopy model can be benchmarked on CPU, integrated GPU and NPU with the same
one-word change — exactly what `scripts/bench_devices.py` already does.

Before writing inference code, **open the downloaded folder and look at what
is in it** — the input layout, colour order and normalisation the model
expects. The docs promise "load it with the OpenVINO runtime" and nothing
about a Model API config being present (§0, UNVERIFIED).

---

## 6. The fair-test harness — SPECIFICATION

> **This section is a plan.** No code exists yet, and this document
> deliberately does not contain any. It exists so that the test is designed
> before anyone has a result to protect.

### What is being tested

Per tile, three numbers:

| | Source |
|---|---|
| `frac_truth` | Human-annotated canopy mask, unioned, ÷ tile pixels |
| `frac_geti` | Geti model mask, all instances unioned, thresholded, ÷ tile pixels |
| `frac_vari` | The studio's `vegScore()` pipeline on the same tile, ÷ tile pixels |

`frac_vari` must be computed **exactly as the studio computes it** — the same
gates, the same brightness filter, the same score-to-fraction step. The port
in `scripts/geti_export_tiles.py` (`veg_score`, `mean_veg`) is that
computation and should be imported, not re-typed. If the browser index is
ever retuned, both move together or the comparison is meaningless.

Note the asymmetry, and state it in the write-up: VARI returns a *continuous
greenness score per pixel*, not a binary mask. Two defensible readings —
(a) mean score as the fraction directly, (b) threshold the per-pixel score at
some cut and take the area — give different answers. **Compute both, report
both, and pick the one that flatters VARI most as the number the gate uses.**
The incumbent gets the benefit of the doubt.

### The held-out set

- **At least 30 tiles**, sampled by the same script with a different `--seed`,
  stratified the same way.
- **Never uploaded to Geti. Not to the training project, not to a "test"
  dataset inside the project, not once.** v3 does offer dedicated test-dataset
  splits, and using them would still leave the images inside a system that
  pre-labels and retrains; keeping the files out entirely is unambiguous, and
  unambiguous is the point.
- Verify by tile id before uploading anything, and again before scoring. Cheap
  to check, impossible to undo.

### Ground truth — annotate it outside Geti

Two options; **option A is the specified one.**

**A. Annotate the held-out masks outside Geti** — CVAT, Label Studio, QGIS,
anything that exports polygons or PNG masks. Same annotator, same
`LABELING.md`, no pre-labelling assistance of any kind. Slower, and correct:
the ground truth is then produced by a process the model under test cannot
have influenced.

**B. A second pass inside Geti, in a separate project, never trained on.**
Tempting because the tooling is right there. Rejected as the default: v3's
Automatic Segmentation tool and the pre-labelling loop mean the annotator is
being shown machine suggestions while producing "ground truth", and a
separate-project promise is a policy, not a mechanism. If you take option B
anyway because time ran out, say so in the write-up in those words, and
disable pre-labelling.

Whichever is used, annotate the held-out set **before** looking at any model
output on it.

### Metrics

| Metric | Applies to | Why |
|---|---|---|
| **Per-tile IoU** (mask ∩ truth ÷ mask ∪ truth) | Geti only | VARI produces no comparable mask; reporting an IoU for VARI would be inventing one |
| **MAE of canopy fraction** = mean \|frac_method − frac_truth\| | **Both** | The head-to-head number. It is also the quantity the studio actually displays |
| **Bias** = mean (frac_method − frac_truth), signed | Both | Systematic over- or under-counting is a different failure from noise, and matters differently for planting decisions |
| **MAE per VARI bucket** | Both | The interesting result is probably "VARI is fine on parks and bad on sparse street trees". A single average hides that |

Plots: predicted-vs-truth scatter with the 1:1 line, both methods on one
figure; Bland–Altman (difference vs mean) for agreement; the three or four
worst tiles per method shown as images, because a picture of a failure is
worth more than a decimal place.

Report **n** next to every number. With 30–40 held-out tiles, differences
smaller than a few percentage points of canopy fraction are noise. A bootstrap
confidence interval over tiles costs ten lines and prevents a bad claim.

### THE EVIDENCE GATE

Stated in the same form as this repo's existing champion-selection rule, where
the numeric forecaster only stays champion while it measurably beats the
baseline:

> **The Geti canopy model replaces `vegScore()` in the studio only if it beats
> VARI on held-out canopy-fraction MAE by at least 0.05 absolute (5 percentage
> points of canopy fraction), using VARI's best of its two readings, on a
> held-out set of at least 30 tiles that were never uploaded to Geti.**
>
> **If it loses, or wins by less than that margin, VARI stays in the studio
> and the measured score is published in the README anyway — as a table, with
> the tile count, next to the numbers it failed to beat.**

A negative result is a result. This repo already publishes one: the README
states plainly that a 1.5B LLM is worse than a trend line at numeric
extrapolation and that a trained MLP is worse still, and both numbers are in
the bake-off table. A canopy model that loses to a colour rule belongs in
exactly that table, for exactly that reason.

**Why the bar is "measurably better" and not "fancier".** VARI's advantages
are real and the model does not get them for free:

| | VARI (`vegScore`) | Geti model |
|---|---|---|
| Download | none — ~40 characters of JavaScript | an IR file to fetch and host |
| Runs | in any browser, any machine, offline once tiles load | needs OpenVINO, or a server, or ONNX Runtime Web |
| Latency | instant, per pixel, over a whole 9-tile mosaic | inference per tile |
| Failure mode | known and explainable: it is a colour rule | opaque; wrong in ways nobody predicted |
| Inherits imagery bias | yes | yes, **plus** whatever the training tiles happened to contain |

A model that ties VARI is a *worse* product than VARI, because it costs all of
the above and returns nothing. Hence a margin, not a tie-break.

### Deliverables of the harness

1. `metrics.json` — every number above, plus n, seeds, dataset revision, Geti
   version, model precision.
2. A results table in the README, in the same voice as the bake-off table,
   whichever way it goes.
3. The scatter and Bland–Altman figures.
4. One paragraph naming the worst failure mode of each method, with a tile.

---

## 7. Honest-claims box

Pin these to the write-up, the slide, and the README section, in this order:

1. **Until this is trained and tested, it is a plan.** This document describes
   a procedure and a decision rule. It contains no accuracy figure because no
   model has been trained. Do not present the pipeline as a result.
2. **No accuracy claim without the held-out number.** Geti's own validation
   score is computed on a split of your own annotations and shares every bias
   in them. It is a training diagnostic, not evidence. The only quotable
   numbers come from §6, on tiles Geti never saw.
3. **The imagery is a mosaic of mixed dates and seasons.** Esri World Imagery
   stitches scenes flown at different times; a single tile can straddle a seam,
   and no per-tile date is exposed here. A model trained on it learns "canopy
   as it looked whenever this patch was last flown" — not canopy today, and
   not canopy on a comparable date across the city. That limit is inherited by
   every number downstream, including the fair-test numbers. It also caps how
   good any method can look: some disagreement with ground truth is the
   imagery's, not the method's.
4. **VARI is the sampler and the incumbent.** Tiles were chosen using the very
   index the model is meant to beat. That biases the dataset toward places
   VARI has an opinion about — reasonable for a fair fight, and worth stating
   rather than hiding.
5. **One city, one bbox, one annotator.** Ahmedabad tiles, one person's idea of
   a crown edge. Nothing here transfers to another city without re-testing,
   and the exporter is city-agnostic precisely so that re-testing is cheap.
6. **Geti version drift.** Everything in §0 was true on **2026-08-27** against
   Geti **v3.1.0**. This project changed shape substantially between 2.x and
   3.0 — hosted trial retired, semantic segmentation removed, SDK deprecated.
   Re-check the docs before quoting §0 in anything public.
7. **Attribution.** "Imagery © Esri, Maxar, Earthstar Geographics" travels with
   every tile, figure and derived dataset.

---

## Sources

Checked 2026-08-27.

| Topic | URL |
|---|---|
| Repo, licence, releases | https://github.com/open-edge-platform/geti |
| v3.0.0 release notes (Apache-2.0, removals, requirements) | https://github.com/open-edge-platform/geti/releases/tag/app%2Fv3.0.0 |
| Install paths + system requirements | https://github.com/open-edge-platform/geti/blob/develop/application/docs/install.md |
| FAQ (local-only, macOS, CPU training, export) | https://docs.geti.intel.com/docs/user-guide/reference/faq/ |
| What's new and removed in v3 | https://docs.geti.intel.com/docs/user-guide/getting-started/installation/upgrade/whats-new-and-removed/ |
| Project types | https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/project-management/ |
| Annotation tools | https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/annotations/annotation-tools/ |
| Annotation mode / pre-labelling | https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/annotations/annotation-mode/ |
| Train your first model (20–50 images, export) | https://docs.geti.intel.com/docs/user-guide/quick-start/training-your-first-model/ |
| Training configuration | https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/model-training-and-optimization/training-configuration/ |
| Dataset limits and formats | https://docs.geti.intel.com/docs/user-guide/geti-fundamentals/datasets/dataset-management/ |
| `getitune` library install (macOS supported) | https://docs.geti.intel.com/docs/user-guide/library/get-started/installation/ |
| `getitune` export / deploy | https://docs.geti.intel.com/docs/user-guide/library/guides/base/export-and-deploy/ |
| `geti-sdk` deprecation notice | https://github.com/open-edge-platform/geti-sdk |
| v3 REST API | https://docs.geti.intel.com/docs/rest-api/get-started |
| **v2 only** — "12 images" auto-training | https://docs.geti.intel.com/docs/2.0/user-guide/release-notes/cloud/december-release/ |
| **v2 only** — active learning | https://docs.geti.intel.com/docs/2.0/user-guide/learn-geti/active-learning/ |
| **v2 only** — code deployment bundle | https://docs.geti.intel.com/docs/2.0/user-guide/geti-fundamentals/deployments/code-deployment/ |

**Flagged as unverified**, and repeated here so it is not lost in the prose:
the formal announcement retiring the hosted Geti SaaS; any per-task-type
annotation threshold; whether Apple MPS works under `getitune`; whether the v3
download bundle includes an OpenVINO Model API config; Geti 2.x's on-prem
hardware requirements (obtained from a search snippet, not a fetched page).
