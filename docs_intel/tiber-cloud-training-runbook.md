# Runbook — training the forecaster on Intel's cloud

**Status: a procedure, not a result.** Nobody on this project has run it yet, and
one part of it could not be checked from outside a signed-in account. Everything
that *could* be verified was verified against live Intel endpoints on
**2026-08-27**, and the URL is next to the claim. Where verification stopped at a
login wall or a dead hostname, the text says so instead of guessing.

Two honest warnings before you start, because they change what you should expect:

1. **The URLs Intel's own documentation still gives for "Intel Tiber AI Cloud"
   no longer resolve.** See §0. The live front door is
   <https://cloud.intel.com>, and it is now branded **Intel® Cloud Services**.
2. **This training job does not need cloud hardware.** It is a small
   scikit-learn MLP on a 146-cell × 42-month panel — minutes on a laptop CPU.
   Running it on Intel's cloud is worth doing for three specific reasons, none
   of which is speed:
   - a **reproducible, documented environment** — an x86 Linux box with a
     recorded package set, so the numbers in `report.json` came from somewhere
     you can describe;
   - **headroom for the work that isn't built yet** — the land-cover /
     canopy-segmentation model sketched in
     [`docs/intel/geti-canopy-runbook.md`](./geti-canopy-runbook.md) is the job
     that would actually want an accelerator (§7);
   - **lifecycle evidence** — Intel silicon at *both* ends: cloud training →
     ONNX → OpenVINO inference on client hardware (§8).

   If you catch yourself writing "trained on Intel Gaudi" in a slide, stop.
   It would be a lie, and §2 explains why it would also be pointless.

---

## 0. What was verified on 2026-08-27, and what is dead

Intel has renamed this service repeatedly: Intel DevCloud → Intel Developer
Cloud → Intel® Tiber™ Developer Cloud → Intel® Tiber™ AI Cloud. The live console
now calls itself something else again.

| Endpoint | Status on 2026-08-27 | How it was checked |
|---|---|---|
| `console.cloud.intel.com` (the Tiber console) | **NXDOMAIN — does not exist** | Google Public DNS `A` query returns `Status: 3` with an `intel.com` SOA in the authority section; `curl` fails with "Could not resolve host". Last Internet Archive capture returning `200`: **2025-08-07**. |
| `ai.cloud.intel.com` (Tiber AI Cloud marketing + pricing) | **NXDOMAIN** | Same DNS check. Archive captures show `200` until 2025-07-31, then `301`/`302` in Oct–Dec 2025 pointing at Intel's `corpredirect` 404 handler. |
| `intel.com/…/developer/tools/devcloud/services.html` | **Deleted** — `301` → `corpredirect.intel.com/Redirector/404Redirector.aspx` | Archive captures: `403` on 2026-02-28, then `301` on every capture from 2026-03-19 through 2026-07-25. |
| `developer.habana.ai/get-access/` (Gaudi "get access" page) | **404** | Fetched 2026-08-27. Its body still instructs you to "Go to the Intel AI Cloud" at `console.cloud.intel.com` — a dead host. |
| [Gaudi docs v1.24 "Intel Tiber AI Cloud Quick Start Guide"](https://docs.habana.ai/en/latest/Quick_Start_Guides/Intel_DevCloud_Quick_Start.html) | Live page, **stale links** | Current Gaudi documentation, still shipping links to `console.cloud.intel.com` and `console.cloud.intel.com/docs/guides/get_started.html`. Both dead. |
| **<https://cloud.intel.com>** | **Live**, `200` | Serves a console app titled **Intel® Cloud Services**. |
| <https://cloud.intel.com/docs/index.html> | **Live**, © 2026 Intel Corporation | Sphinx documentation set: *How to Sign In*, *How to Request*, *How to Access*, *How to Share*, *Release Notes*. |
| <https://cloud.intel.com/docs/release_notes.html> | **Live** | Latest entry: **GA 1.0.3, 02 February 2026** — "added the latest Intel's GPU instances on enterprise and client segments". |

The name change is not an inference. The console's own public configuration file,
<https://cloud.intel.com/configMap.json>, sets:

```
REACT_APP_CONSOLE_LONG_NAME  = Intel® Cloud Services
REACT_APP_CONSOLE_SHORT_NAME = Intel® Cloud Services
REACT_APP_AZURE_LANDING_PAGE_URL = https://cloud.intel.com
```

So: **write "Intel® Cloud Services (formerly Intel® Tiber™ AI Cloud)" in any
submission.** Do not cite `console.cloud.intel.com` — a judge who clicks it gets
a DNS error, and that looks worse than the honest note.

### What could NOT be verified

Everything below the sign-in wall. Specifically:

- **The hardware catalogue.** `cloud.intel.com/docs/hardware_catalog.html`
  returns the app shell, not a document, and the catalogue API
  (`prereleaseapi.intel.com/v1`, named in `configMap.json`) answers
  `400 no authorization header`. **No instance types, no specs, no prices could
  be read from outside an account.**
- **Whether a free tier still exists.** The console config still references three
  enrollment forms — `…/forms/developer-cloud/standard/enrollment.html`,
  `…/premium/…`, `…/enterprise/…` — so the **Standard / Premium / Enterprise**
  tier structure appears to survive the rename. Whether *Standard* is still free,
  and what it contains, is not stated anywhere publicly readable.
  The one surviving public description of the free tier is an Intel support
  article, [How to Get an Extension for the Free Standard Tier Batch-Mode
  Service?](https://www.intel.com/content/www/us/en/support/articles/000095928/software.html),
  which describes a *free standard tier batch-mode service* on SPR/PVC systems
  with 20-day reservations that auto-extend by 10 days if you log in during the
  last 4 days. That article **predates the migration** and should be treated as
  historical until you see it on screen.
- **Any price.** The pricing page lived at `ai.cloud.intel.com/pricing/`, which
  is gone. Third-party summaries quote **$1.30 per card-hour for Gaudi**, but
  that number traces to 2024–2025 Intel marketing and could not be confirmed
  against a live Intel page today. **Do not quote it as current.**
- **Whether payment details are demanded at signup.** The (now-404) Gaudi
  access page said "You will need to get Cloud Credits by entering your payment
  information or redeeming a coupon". Unconfirmed for the current console.
- **Student / academic access.** No live Intel page describing a student
  programme was found. There are community threads asking for student credits;
  none is an Intel commitment.

**Assume nothing here is free until the console shows you a price of $0.**

---

## 1. Sign up and orient yourself in the console

Source for this section: <https://cloud.intel.com/docs/how_to_register.html>
(© 2026 Intel Corporation). Screenshots are impossible from here, so the UI
landmarks are named instead. **Verify on screen — the UI may have moved.**

1. Open **<https://cloud.intel.com>**.
2. Click **Sign In / Sign Up**, top right corner.
3. You land on the Intel login page. Enter your email and click **Next**.
   - The docs say "corporate email address". A university address is the closest
     thing a student has; if it is rejected, that is a finding worth writing down
     rather than a bug to work around. Note that instance sharing (§ *How to
     Share*) is restricted to users with **the same email domain** as yours, so
     the whole team should register with the same domain if you want shared access.
4. **Existing Intel account** → enter your password. **New account** → provide
   email, name, preferred language, region, and a password, then
   **Next: Verify your email**.
5. Enter the emailed code and click **Create an account**.
6. Review and **Accept** the [Intel Cloud Services
   Agreement](https://www.intel.com/content/www/us/en/content-details/915978/intel-cloud-services-agreement.html).
   Read it — it is the terms you are agreeing to on behalf of whatever you upload.
7. You are redirected to the console home page.

**Landmarks to expect** (from the docs' own wording; verify on screen):

| Landmark | What it's for |
|---|---|
| **Overview** | Console home; links into the catalogue. |
| **Hardware Catalog** | Pick an instance type. This is where the *actual* current tiers and prices become visible — record what you see. |
| **Cloud Instances** | Your running/requested instances, their **State**, and the **Connect** / **SSH** buttons. |
| **SSH Keys** (**Keys** tab) | Upload public keys. Note the docs' caution: these are *stored separately* from a regular compute instance's SSH keys. |

**Success criterion for §1:** you can see the Hardware Catalog while signed in,
and you have written down — for §8 — the exact tier names and prices shown to
*your* account. That screenshot is the only trustworthy pricing evidence this
project will have.

**Stop rule.** If registration is refused, or the catalogue shows no instance you
can afford, **stop and run the training locally instead**. The job is small
(§2); the cloud run is evidence, not a dependency. Do not spend a day on access.

---

## 2. Launch the cheapest suitable compute

### What the job actually needs

Measured from the repo, not estimated:

| Property | Value | Where it comes from |
|---|---|---|
| Panel | 146 cells × 42 months | `README.md`; printed by `greenplan run` |
| Features per sample | **13** | `len(FEATURE_NAMES)` in `greenplan/forecast/features.py` |
| Samples per metric | ~1,700 (12 usable cutoffs × 146 cells) | `min_history_months: 18`, `horizon_months: 12` in `config/city.yaml`; the exact split is printed by the run |
| Metrics trained | **3** (`traffic`, `aqi`, `ndvi`) | `METRICS` in `greenplan/features/trends.py` |
| Model | `MLPRegressor(hidden_layer_sizes=(64, 32), alpha=1e-2, max_iter=4000)` | `greenplan/forecast/train.py` |
| Input data on disk | 1.3 MB total (`data/`) | `du -sh data` |

Three tiny dense networks on ~1,700 × 13 float rows. **Any Linux instance with a
couple of vCPUs finishes this in minutes.** RAM is not a constraint; disk is not
a constraint; there is no GPU kernel in the path at all.

**So: pick the smallest / cheapest Linux CPU instance in the Hardware Catalog.**
Prefer a Xeon CPU instance. Record its exact catalogue name and hourly price for §8.

### Requesting the instance

Source: <https://cloud.intel.com/docs/how_to_request.html>.

1. **Hardware Catalog** → select an instance type.
2. Fill the **Request a Cloud Instance** form: instance name, **Intended Use**
   (required — say what it is: "training a small scikit-learn regression model
   for an urban-greening project"), **Use case**, **Duration**, and a
   **Deployment details** description.
3. **SSH Public Keys — optional.** Add one if you want a shell (see §3); without
   one you can still use browser access. Up to 20 keys per instance.
4. **Termination Protection — optional.** Not needed for a job this short.
5. Click **Request Instance**.

The instance is **approved by an administrative team**, so there may be a wait —
plan for it, and don't leave this to the night before a deadline.

> **Caution, quoted from Intel's docs:** every reservation has a hard expiration
> date and **cannot exceed 30 days total**; data left on the instance **may be
> lost after expiration**. Pull your artifacts back the same day you produce them
> (§5).

### Getting a shell — two routes

Both are documented at <https://cloud.intel.com/docs/how_to_access.html>:

- **Browser ("Connect" button).** No keys required. Per the docs: *"For all Linux
  Operating Systems, JupyterLab is launched via Connect. For all Windows
  operating systems, remote desktop (RDP) is launched."* Pick a **Linux**
  instance and you get JupyterLab in the browser — a terminal, a file browser,
  upload and download. This is the lowest-friction path and it is enough for
  everything in this runbook.
- **SSH.** Generate a key locally first:

  ```bash
  # Linux / macOS
  ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
  cat ~/.ssh/id_ed25519.pub
  ```

  ```powershell
  # Windows PowerShell
  ssh-keygen -t ed25519 -f $env:UserProfile\.ssh\id_ed25519
  type $env:UserProfile\.ssh\id_ed25519.pub
  ```

  Upload the **public** key under **SSH Keys → Upload key**, attach it to the
  request, then use the instance's **SSH** button.

  **Copy the SSH command the console gives you verbatim.** Older Intel Developer
  Cloud instances were reached through a bastion/jump host rather than a direct
  `user@ip`, and this could not be re-verified for the current console — so do
  not assume the shape of the command. *(Unverified — read it off the screen.)*

**Success criterion for §2:** `nproc`, `free -g`, `python3 --version` and
`cat /etc/os-release` all answer on the instance, and you have saved that output.
It is your record of what "the reproducible environment" actually was.

### About Gaudi — read before someone puts it in a slide

**Gaudi is not needed for this job and should not be requested for it.** An
MLP with 13 inputs and 96 hidden units is not an accelerator workload; the
scikit-learn training loop has no HPU backend, so a Gaudi node would run the
identical CPU code on an idle accelerator. Asking for one would be gold-plating,
and a judge who knows the hardware will spot it.

Its actual accessibility, honestly:

- **Not verifiably available to a student for free.** The Gaudi access route
  Intel documents — `developer.habana.ai/get-access/` → `console.cloud.intel.com`
  — is a 404 pointing at a dead host (§0). The last public statement on the
  matter said you need Cloud Credits from **payment information or a coupon**.
- **Paid elsewhere.** Gaudi 3 is sold as a cloud service by third parties (e.g.
  [IBM Cloud](https://www.ibm.com/products/gpu-ai-accelerator/intel-gaudi3)).
  That is a purchase, not a student tier.
- **The roadmap is moving.** Intel cancelled the commercial release of Falcon
  Shores and is pointing its data-centre GPU line at inference with **Crescent
  Island** (Xe3P, 160 GB LPDDR5X), expected in customer testing in 2H 2026
  ([Phoronix](https://www.phoronix.com/review/intel-crescent-island),
  [DCD](https://www.datacenterdynamics.com/en/news/intel-unveils-crescent-island-data-center-gpu-for-inferencing-workloads/)).
  Any plan that assumes a specific Intel accelerator will be rentable to you in a
  year is a guess.

Where Gaudi (or any accelerator) *would* matter for this project: §7.

---

## 3. Get the repo and its data onto the instance

Everything below runs in the instance's JupyterLab terminal, or over SSH.

### 3a. The code

**If the repo is public:**

```bash
git clone <repo-url> green-vision
cd green-vision
```

**If the repo is private** (no credentials on a shared cloud box — do not paste a
token into an instance you don't control):

1. On your laptop: `git archive --format=zip -o green-vision.zip HEAD`
   (or just zip the working tree — it is ~2 MB including `data/`).
2. In JupyterLab, use the **file browser's Upload button** to drop
   `green-vision.zip` into your home directory.
3. On the instance:

   ```bash
   unzip green-vision.zip -d green-vision
   cd green-vision
   ```

Either way, confirm the data came with it — the CSVs are committed, so no
exporter script needs to run on the instance:

```bash
ls -la data/ahmedabad_aqi.csv data/ahmedabad_ndvi.csv \
       data/ahmedabad_traffic_placeholder.csv config/city.yaml
```

### 3b. The Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

**Install only what training needs.** `requirements.txt` pulls
`openvino-genai` and `huggingface_hub`, which exist for the *local LLM* side of
the project. `greenplan.forecast.train` never imports them — `openvino` is
imported lazily, only by the inference path (`greenplan/forecast/ovmodel.py`) —
so installing them here downloads roughly a gigabyte for nothing:

```bash
pip install "numpy>=1.26" "pandas>=2.1" "h3>=3.7" "PyYAML>=6.0" "requests>=2.31"
pip install scikit-learn skl2onnx
```

(`requests` is needed because `greenplan/reasoning/client.py` imports it at
module level, and the engine imports that module.)

If you would rather mirror the shipped environment exactly, `pip install -r
requirements.txt` also works and is fine — it is just slower. The two extra
packages in the second command are the ones `train.py` checks for itself; run it
without them and it exits with code 2 and prints exactly:

```
error: training needs scikit-learn and skl2onnx:
    pip install scikit-learn skl2onnx
```

**Success criterion for §3:** `python -c "import sklearn, skl2onnx, h3, pandas;
print(sklearn.__version__, skl2onnx.__version__)"` prints two versions. Save
`pip freeze > docs/intel/tiber-env-$(date +%F).txt` — that file *is* the
"reproducible environment" claim; without it the claim is just a word.

---

## 4. Run the training

```bash
python -m greenplan.forecast.train --config config/city.yaml
```

Optional flags, both defaulted in `train.py`: `--hidden 64 32` (layer sizes) and
`--test-cutoffs 3` (how many of the latest cutoff months form the held-out set).
**Change neither for the headline run.** If you want a sweep, do it *after* you
have the default result, and report it as a sweep.

### What you will see

First an INFO line reporting the time-based split (the model only ever sees
history strictly before each cutoff — the split is by cutoff month, never
random):

```
INFO greenplan.forecast.train: NNNN samples: NNNN train (cutoff < NN), NNN test — split by time, not randomly
```

Then the results table and the verdict, printed by `train.py`:

```
metric     test MAE  baseline    skill
traffic     …          …         …      (inert placeholder, excluded)
aqi         …          …         …
ndvi        …          …         …

combined skill vs Theil-Sen+seasonal baseline (real metrics): -1.410
(same definition scored the LLM at -0.333 — see README table)
verdict: NEGATIVE — provider `hybrid` keeps the statistical forecaster (the honest champion) and benches this challenger
wrote models/ahmedabad/forecaster/[metric].onnx + norm.json + report.json
```

### Expect the NEGATIVE verdict. It is the correct answer.

**The combined skill on the shipped 42-month panel is negative — the README
records −1.41 for this trained MLP — and the verdict line will read "NEGATIVE".**
That is not a failed run, a bad hyperparameter, or something to hide before a
demo. It is the finding:

- Skill is `(baseline_MAE − model_MAE) / std`, scored on held-out *future*
  months against a Theil–Sen + seasonality baseline — the **same** definition
  that scored the local LLM at −0.333. All three contenders are directly
  comparable because they answered the same held-out questions.
- The network is trained on the **residual** from that baseline, so at worst it
  learns nothing and collapses back to the baseline. Getting −1.41 means 42
  months of history simply does not contain a learnable residual: the leftovers
  are city-wide shocks that repeat too few times to fit.
- The `hybrid` provider reads `skill_combined` straight out of `norm.json` and
  deploys the network **only if it is > 0** (`greenplan/reasoning/client.py`).
  A negative number means the statistical forecaster stays champion — enforced by
  code, on every run, from evidence.

So the deliverable of this cloud run is **a measured, reproducible negative
result plus a working auto-deployment gate**, not a new champion. Write it up
that way. A project that ships the model that actually won, and can show you the
number that benched the other one, is doing science; a project that quietly
ships the network it just trained is not.

If the day comes that a longer panel makes this positive, the same command
prints `POSITIVE — provider hybrid will deploy this network` and nothing else
has to change.

**Success criteria for §4:**
- exit status `0` (`echo $?`);
- the results table and `verdict:` line present in your saved log;
- five files written (§5).

---

## 5. Pull the artifacts back and commit them

The output directory is `cfg.model.forecaster_dir`, which
`greenplan/config.py` expands from `models/{city}/forecaster` — for the shipped
config that is **`models/ahmedabad/forecaster/`**, containing exactly:

```
models/ahmedabad/forecaster/traffic.onnx
models/ahmedabad/forecaster/aqi.onnx
models/ahmedabad/forecaster/ndvi.onnx
models/ahmedabad/forecaster/norm.json      # feature names, mu/sd, resid_mu/sd, + embedded report
models/ahmedabad/forecaster/report.json    # per-metric MAE, baseline MAE, skill, skill_combined
```

One `.onnx` per entry in `METRICS` — including `traffic`, which trains and
exports for interface completeness only. Its stream is a disclosed inert
placeholder with MCDA weight 0, and its score is excluded from
`skill_combined`. Don't quote it.

On the instance, pack them:

```bash
tar czf forecaster-artifacts.tgz models/ahmedabad/forecaster \
    docs/intel/tiber-env-*.txt
sha256sum forecaster-artifacts.tgz
```

Then either:

- **JupyterLab:** right-click `forecaster-artifacts.tgz` in the file browser →
  **Download**; or
- **scp**, using the same host/user/jump-host shape the console's SSH tab gave
  you in §2:

  ```bash
  scp <exactly-what-the-console-showed>:~/green-vision/forecaster-artifacts.tgz .
  ```

Locally, unpack into the repo so the paths land where the engine expects, verify
the checksum matches, and commit:

```bash
tar xzf forecaster-artifacts.tgz          # writes models/ahmedabad/forecaster/*
python -c "import json;print(json.load(open('models/ahmedabad/forecaster/report.json'))['skill_combined'])"
git add models/ahmedabad/forecaster docs/intel/tiber-env-*.txt
git commit -m "forecaster: trained challenger artifacts from Intel Cloud Services run"
```

**Success criterion for §5 — the end-to-end proof.** Run the engine locally and
watch the gate fire on the artifacts you just brought home:

```bash
python -m greenplan run --config config/city.yaml --recommend
```

The log will contain, from `greenplan/reasoning/client.py`:

```
INFO greenplan.reasoning.client: hybrid numeric: trained challenger LOSES to the baseline (held-out skill -1.410) — deploying the statistical forecaster instead
```

That single line is the most valuable thing this whole runbook produces (§8):
a model trained on Intel's cloud, exported to ONNX, loaded by OpenVINO on the
client machine, and **rejected on measured evidence by code that would have
deployed it had it won.**

---

## 6. Optional — Intel Extension for Scikit-learn (`sklearnex`)

**Verified current, and verified not to help this particular job.** Both halves
matter; report both or neither.

- The package is real and actively maintained: pip name **`scikit-learn-intelex`**,
  documentation version **2026.1**, now under the UXL Foundation
  ([docs](https://uxlfoundation.github.io/scikit-learn-intelex/latest/algorithms.html),
  [GitHub](https://github.com/uxlfoundation/scikit-learn-intelex),
  [PyPI](https://pypi.org/project/scikit-learn-intelex)). Usage is two lines
  before importing sklearn:

  ```python
  from sklearnex import patch_sklearn
  patch_sklearn()
  ```

- **It would not accelerate `train.py`.** The extension patches a fixed list of
  **28** scikit-learn estimators. `MLPRegressor` and `MLPClassifier` are **not on
  it** — neural networks are absent from the CPU, GPU and SPMD tables alike. The
  patched list is SVM (`SVC`/`NuSVC`/`SVR`/`NuSVR`), forests
  (`RandomForest*`/`ExtraTrees*`), linear models (`LinearRegression`, `Ridge`,
  `Lasso`, `ElasticNet`, `LogisticRegression*`), `KMeans`, `DBSCAN`, `PCA`,
  `IncrementalPCA`, `TSNE`, k-NN, `LocalOutlierFactor`, `EmpiricalCovariance`,
  `GridSearchCV`, and pairwise/ROC helpers. Patching and running the current
  training would land back on stock scikit-learn and measure **≈1.0× — a null
  result**, which is a legitimate thing to publish but is not a speedup.

- **Where it would genuinely apply here:** the ridge probe on zone-relative
  residuals mentioned in the README (`Ridge` *is* patched), and any future move
  of the challenger to `RandomForestRegressor` or `Ridge` — both patched, both
  plausible for a panel this small.

- **Platform reality.** `scikit-learn-intelex` is built on oneDAL and is **x86-64
  only**. The development machine for this project is an Apple M3 Pro (arm64),
  so **it cannot be installed or smoke-tested there** — no attempt was made.
  The right places to measure it are (a) the Intel Cloud Services Xeon instance
  from §2, where it is native, or (b) the team's Windows Intel laptop. Never
  claim a local macOS measurement.

**If you do measure it, do it on the instance and report it like this** — same
data, same seed, patched vs unpatched, wall clock, both numbers:

```bash
pip install scikit-learn-intelex
python -c "import sklearnex, sklearn; print(sklearnex.__version__, sklearn.__version__)"

/usr/bin/time -v python -m greenplan.forecast.train --config config/city.yaml
# then re-run with a 2-line patch shim and time it identically
```

**Until someone runs that on x86 hardware, the sklearnex speedup for this repo is
`pending` — not "expected", not "up to Nx". Do not put a number in the README
that nobody measured.**

---

## 7. Future work — the job that would actually use an accelerator

**Labelled a plan. None of this is built.**

The current forecaster is a tabular regression on 146 × 42 numbers. The workload
that would justify cloud accelerators is the **land-cover / canopy segmentation**
model sketched in [`docs/intel/geti-canopy-runbook.md`](./geti-canopy-runbook.md):
per-pixel classification over Sentinel-2 10 m imagery, so that "where inside this
cell is the bare ground" comes from a trained segmentation model instead of the
current single-scene NDVI threshold (`scripts/sentinel2_ndvi_export.py`).

That job has the properties this one lacks:

| Property | Today's forecaster | Future canopy model |
|---|---|---|
| Input volume | 1.3 MB of CSV | tens–hundreds of GB of multi-band scenes |
| Compute shape | 3 tiny dense nets | convolutional/transformer segmentation over image tiles |
| Training time on a laptop CPU | minutes | impractical |
| Labels needed | none (self-supervised by history) | annotated masks — the real bottleneck |
| Would an accelerator help? | **No** | **Yes**, materially |

What would have to be true before that becomes a runbook rather than a paragraph:
labelled canopy masks for the Ahmedabad bbox exist; a per-month cloud-free
Sentinel-2 composite pipeline exists (the README already flags this as the honest
route to a longer panel, and as not yet built); and an accelerator tier is
actually rentable to this team at a known price (§0 — currently unverified).

Until then, the correct claim is: *"the training path is proven end-to-end on a
small model; the accelerator tiers are where the segmentation work would go."*
Not: *"we trained on Gaudi."*

---

## 8. What to capture for judges

Collect these five things. Together they show Intel silicon at both ends of the
lifecycle, with a measured decision in the middle.

| # | Artifact | Where from | What it proves |
|---|---|---|---|
| 1 | **Console screenshot** of the running instance — name, instance type, region, state `Ready`, and the price shown to your account | §2, Cloud Instances tab | The cloud training actually happened, on named Intel hardware. Also your only trustworthy record of current pricing (§0). |
| 2 | **Environment record** — `cat /etc/os-release`, `nproc`, `python3 --version`, and `pip freeze` output | §2–§3 | "Reproducible, documented environment" is a claim you can substantiate, file in hand. |
| 3 | **The `train.py` output block** — the INFO split line, the metric table, and the `verdict:` line | §4 | The training ran to completion, was scored on held-out *future* months, and reported honestly. |
| 4 | **`report.json`** (and the `.onnx` files) | §5 | Per-metric MAE vs baseline MAE vs skill, `skill_combined`, `n_train`/`n_test`, and `inert_placeholder: true` on traffic — the disclosure is in the artifact itself, not just the slide. |
| 5 | **The local `hybrid` log line** rejecting the challenger | §5 success criterion | The champion/challenger gate is live code driven by measured evidence, not a claim. |

The one-sentence story they support:

> A model trained on Intel's cloud (Xeon CPU instance, Intel® Cloud Services),
> exported to ONNX, executed locally by Intel OpenVINO on client CPU — and then
> **rejected** by the engine's own evidence gate, because on 42 months of real
> data the statistical forecaster is measurably better. Same skill definition
> for every contender; the champion is whichever one actually wins.

Do not oversell the compute. "We used a small Xeon CPU instance because the job
is small, and we can show you why an accelerator would have been theatre" is a
stronger answer than a Gaudi screenshot with no workload behind it.

---

## Appendix — sources, all checked 2026-08-27

- Live console: <https://cloud.intel.com> · public config
  <https://cloud.intel.com/configMap.json>
- Docs: [index](https://cloud.intel.com/docs/index.html) ·
  [How to Sign In](https://cloud.intel.com/docs/how_to_register.html) ·
  [How to Request](https://cloud.intel.com/docs/how_to_request.html) ·
  [How to Access](https://cloud.intel.com/docs/how_to_access.html) ·
  [Release Notes](https://cloud.intel.com/docs/release_notes.html)
- Intel Cloud Services Agreement:
  <https://www.intel.com/content/www/us/en/content-details/915978/intel-cloud-services-agreement.html>
- Historical free-tier description (predates the migration — treat as historical):
  <https://www.intel.com/content/www/us/en/support/articles/000095928/software.html>
- Gaudi documentation v1.24 Tiber quick start (live page, dead links inside):
  <https://docs.habana.ai/en/latest/Quick_Start_Guides/Intel_DevCloud_Quick_Start.html>
- Gaudi 3 on IBM Cloud (paid, third-party):
  <https://www.ibm.com/products/gpu-ai-accelerator/intel-gaudi3>
- Intel data-centre GPU roadmap (Falcon Shores cancelled commercially; Crescent
  Island 2H 2026): <https://www.phoronix.com/review/intel-crescent-island> ·
  <https://www.datacenterdynamics.com/en/news/intel-unveils-crescent-island-data-center-gpu-for-inferencing-workloads/>
- Extension for Scikit-learn 2026.1 supported algorithms:
  <https://uxlfoundation.github.io/scikit-learn-intelex/latest/algorithms.html> ·
  <https://github.com/uxlfoundation/scikit-learn-intelex> ·
  <https://pypi.org/project/scikit-learn-intelex>
- Dead as of this date: `console.cloud.intel.com`, `ai.cloud.intel.com`,
  `intel.com/…/developer/tools/devcloud/services.html`,
  `developer.habana.ai/get-access/`. Evidence in §0.
