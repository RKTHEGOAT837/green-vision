# Runbook — producing the CPU / GPU / NPU benchmark table

**Status: a plan, not a result.** This repo currently has **no NPU numbers**. The
development machine is an Apple M3 Pro (arm64), so it can only ever produce a CPU
row, and not an Intel one. This document is the exact procedure for producing the
real table on a Windows Intel Core Ultra laptop. Nothing here should be quoted as
a measurement until someone has run it and pasted the output back into the README.

Everything below is verified against official documentation as of **2026-08-27**;
the source URL is linked next to each claim that depends on it. Where a claim
could not be verified against an official source, it says so inline.

Target: one console block showing the same INT4 OpenVINO IR loading and answering
on `CPU`, `GPU` and `NPU`, with `model.device` as the only thing that changed.

---

## Step 1 — confirm the laptop actually has a usable NPU

"It's a Core Ultra, so it has an NPU" is an assumption, and it is the assumption
this whole exercise rests on. Settle it first, before installing anything.

The NPU was introduced with the Intel Core Ultra generation (formerly Meteor
Lake) — [OpenVINO NPU device
docs](https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html).
Later families ship one too, but they are not equivalent: Core Ultra 200V
(Lunar Lake) carries a ~48-TOPS NPU while the Core Ultra 200S desktop parts carry
the older ~13-TOPS design, which is why no 200S SKU meets Microsoft's 40-TOPS
Copilot+ bar ([Tom's
Hardware](https://www.tomshardware.com/pc-components/cpus/intel-prepping-arrow-lake-refresh-with-minor-clock-speed-bump-and-a-new-copilot-ai-compliant-npu-lifted-from-core-ultra-200v-reportedly-launches-in-the-second-half-of-2025)).
For this benchmark the TOPS figure does not matter — presence and a working
driver do. Do not infer either from the marketing name.

### 1a. Device Manager

1. Press `Win`+`X`, choose **Device Manager**.
2. Look for a **Neural processors** category containing **Intel(R) AI Boost**.

Two device names are in circulation depending on driver vintage. The OpenVINO
configuration guide words it as: if a driver is installed you should find
`'Intel(R) NPU Accelerator' in Windows Device Manager`, and if not, the NPU is
"most likely listed in *Other devices* as *Multimedia Video Controller*"
([configurations-intel-npu](https://docs.openvino.ai/2026/get-started/install-openvino/configurations/configurations-intel-npu.html)).
Intel's own driver page and support notes use **Intel(R) AI Boost** under
**Neural processors**. Either name counts as "the NPU is present".

Three outcomes:

| What Device Manager shows | Means |
|---|---|
| **Neural processors → Intel(R) AI Boost** (or *Intel(R) NPU Accelerator*) | NPU present, driver installed. Continue. |
| **Other devices → Multimedia Video Controller** | NPU present, **driver missing**. Go to step 7.1. |
| Neither, anywhere | Probably no NPU on this part — see step 7.1 before concluding it. |

### 1b. Record the driver version

Right-click the device → **Properties** → **Driver** tab → note **Driver
Version**. This number goes in the results block for the judges.

The documented floor for LLM work is **32.0.100.3104 or newer**; OpenVINO's NPU
troubleshooting section says to update to that or later when generation fails
silently or with errors ([OpenVINO GenAI on
NPU](https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html)).

Optional PowerShell equivalent (convenience only — Device Manager is the
documented path):

```powershell
Get-CimInstance Win32_PnPSignedDriver |
  Where-Object { $_.DeviceName -like "*AI Boost*" -or $_.DeviceName -like "*NPU*" } |
  Select-Object DeviceName, DriverVersion
```

### 1c. OS check

NPU inference requires **Windows 11 64-bit, 22H2 or later** ([OpenVINO system
requirements](https://docs.openvino.ai/2026/about-openvino/release-notes-openvino/system-requirements.html)).
Windows 10 is supported for CPU and GPU but **not** for the NPU. Check with
`winver`.

The runtime-level confirmation (`openvino.Core().available_devices` reporting
`NPU`) comes in step 4, once Python exists — that check, not Device Manager, is
the one that decides whether the benchmark can run.

---

## Step 2 — Python, virtual environment, packages

**Python version.** OpenVINO supports **Python 3.10–3.14, 64-bit** on Windows
([system
requirements](https://docs.openvino.ai/2026/about-openvino/release-notes-openvino/system-requirements.html)).
The current `openvino-genai` release on PyPI is **2026.3.1.0**, declares
`Requires-Python >=3.10`, and publishes Windows wheels for CPython 3.10 through
3.14 — `win_amd64` only, so this must be an **x86-64** Windows install, not
Windows-on-ARM ([openvino-genai on PyPI](https://pypi.org/project/openvino-genai/)).
**Python 3.12** is a safe pick: inside the supported range, well past end-of-life
concerns, and every dependency ships a wheel for it.

1. Download the **Windows installer (64-bit)** from
   [python.org/downloads/windows](https://www.python.org/downloads/windows/).
2. In the installer, tick **Add python.exe to PATH** — the OpenVINO install guide
   calls this out explicitly.
3. Verify:

```powershell
py -3.12 --version
# Python 3.12.x
```

Then, from the repo root (step 3 clones it — do that first if the folder does not
exist yet):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` already pins `openvino-genai>=2024.4` and `huggingface_hub`,
so this pulls the current release. The bare-minimum equivalent, if the repo is
not present yet:

```powershell
.venv\Scripts\python.exe -m pip install openvino-genai huggingface_hub
```

`openvino` itself is a dependency of `openvino-genai`; installing it separately
is not needed.

**Note on activation.** Every command in this runbook calls
`.venv\Scripts\python.exe` directly, matching the README. That deliberately
avoids `Activate.ps1`, which a default PowerShell execution policy may refuse to
run. Do not change the execution policy to work around it — the direct path
works.

Record the installed versions for the results block:

```powershell
.venv\Scripts\python.exe -m pip show openvino-genai openvino | Select-String "Name|Version"
```

---

## Step 3 — get the repo and the model

```powershell
git clone <repo-url> green-vision
cd green-vision
```

(If git is not available, copy the folder across — the benchmark needs
`scripts/`, `greenplan/` and `config/` only. Do not copy `.venv` from another
machine; build it on this one, per step 2.)

Fetch the model — about **1 GB**, one time, over the network:

```powershell
.venv\Scripts\python.exe scripts\fetch_openvino_model.py
```

Success looks like:

```
downloading OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov (~1.0 GB) -> models\openvino\qwen2.5-1.5b-instruct-int4-ov
done: models\openvino\qwen2.5-1.5b-instruct-int4-ov
```

Confirm the file the benchmark looks for exists:

```powershell
Test-Path models\openvino\qwen2.5-1.5b-instruct-int4-ov\openvino_model.xml
# True
```

### Is this model NPU-compatible? Yes — checked, not assumed

The NPU LLM pipeline requires models exported with symmetric weights, a 4-bit
weight format, channel-wise **or** group-wise quantization (`--group-size -1` or
`--group-size 128`), and `--ratio 1.0`
([OpenVINO GenAI on
NPU](https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html)).
The docs recommend group quantization at group size 128 for smaller models, "up
to 4B–5B parameters".

The fetched model's card states its compression parameters as **mode: INT4_SYM,
ratio: 1, group_size: 128**
([OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov](https://huggingface.co/OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov)).
That is exactly the supported group-quantized case for a 1.5B model, so **no
re-export is expected**. Step 7.3 covers the re-export anyway, in case the load
fails for a quantization reason on this specific driver.

---

## Step 4 — confirm OpenVINO sees the NPU

```powershell
.venv\Scripts\python.exe -c "import openvino; print(openvino.Core().available_devices)"
```

Expected on a Core Ultra laptop with both drivers healthy:

```
['CPU', 'GPU', 'NPU']
```

If `NPU` is absent, stop and work through step 7.1 — the rest of this runbook
cannot produce an NPU row without it. If `GPU` is absent, see step 7.4; a
CPU + NPU table is still worth having.

---

## Step 5 — run the benchmark

```powershell
.venv\Scripts\python.exe scripts\bench_devices.py
```

and the explicit form, which is the one to capture (it fixes the row order, so
the table is reproducible):

```powershell
.venv\Scripts\python.exe scripts\bench_devices.py --devices CPU GPU NPU
```

The script enumerates devices, loads the same IR on each, sends one engine-style
strict-JSON prompt, and prints one table. Output format, copied from
`scripts/bench_devices.py` (values here are placeholders — **do not reuse them**):

```
host: AMD64 / Intel64 Family 6 Model 170 Stepping 4, GenuineIntel
model: qwen2.5-1.5b-instruct-int4-ov
devices OpenVINO can see:
  CPU    Intel(R) Core(TM) Ultra 7 155H
  GPU    Intel(R) Arc(TM) Graphics (iGPU)
  NPU    Intel(R) AI Boost

benchmarking CPU ...
benchmarking GPU ...
benchmarking NPU ...

device    load s  reply s  ttft ms   tok/s  json
CPU          _._      _._      ___     __._   ___
GPU          _._      _._      ___     __._   ___
NPU          _._      _._      ___     __._   ___
```

A device that fails prints `  NPU: FAILED — <message>` and the run continues with
the others. Capture that line verbatim if it happens; it is data too.

### Why the benchmark prompt fits inside the NPU's constraints

The NPU LLM pipeline uses a static-shape approach, with `MAX_PROMPT_LEN`
defaulting to **1024** input tokens and `MIN_RESPONSE_LEN` to **128** generated
tokens ([OpenVINO GenAI on
NPU](https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html)).
`bench_devices.py` sends a prompt of roughly 250 tokens with
`max_new_tokens=128`, one prompt per call, so it sits inside both defaults with
no configuration needed.

Its sampling settings are also within what the NPU pipeline accepts. The static
NPU pipeline asserts `config.is_greedy_decoding() || config.is_multinomial()` and
a batch size of 1
([`pipeline_static.cpp`](https://github.com/openvinotoolkit/openvino.genai/blob/master/src/cpp/src/llm/pipeline_static.cpp)),
and the script uses `do_sample=True, temperature=0.2` — multinomial, batch 1.
Beam search would be rejected; nothing in this repo uses it.

**Read the NPU `load s` figure carefully.** LLM compilation for the NPU happens
on the fly and "may take substantial time" (same page). `bench_devices.py` passes
no `pipeline_config`, so it sets neither `CACHE_DIR` nor an ahead-of-time blob —
every NPU run pays a cold compile. The NPU `load s` cell is therefore a
*compile* time and is not comparable with the CPU's *load* time. Say so under the
table rather than letting a reader draw the wrong conclusion.

---

## Step 6 — the full-lifecycle run (the part that proves deployment)

The benchmark proves the IR runs on the NPU. Running the actual engine proves the
product does.

Edit `config/city.yaml`:

```yaml
model:
  provider: openvino          # or leave hybrid; the LLM half still goes to `device`
  model_dir: models/openvino/qwen2.5-1.5b-instruct-int4-ov
  device: NPU                 # the only line that changes
```

Then:

```powershell
.venv\Scripts\python.exe -m greenplan run --config config/city.yaml --recommend
```

Success looks like a log line `loading OpenVINO model qwen2.5-1.5b-instruct-int4-ov
on NPU`, followed by a normal run writing `recommendations.geojson`,
`recommendations.csv` and `planting_brief.txt`. Capture that log line and the run
summary — one config value, no code change, full pipeline on the NPU, is the
deployment evidence the rubric is asking for.

**Known constraint, stated up front.** `config/city.yaml` sets
`max_new_tokens: 2048`, but the NPU's maximum context is defined as
`MAX_PROMPT_LEN + MIN_RESPONSE_LEN` — **1152 tokens by default**. The engine's
justification prompts may also exceed the 1024-token prompt default. Both limits
are raisable only through pipeline-config values passed to `LLMPipeline`.

A passthrough for exactly this exists on branch `claude/gracious-wu-af8081`
(commit `3862b24`): when `model.device` is `NPU`, the client passes
`MAX_PROMPT_LEN` (new config knob `npu_max_prompt_len`, default 4096) and sets
`MIN_RESPONSE_LEN` to `max_new_tokens`, and logs
`NPU static pipeline: MAX_PROMPT_LEN=... MIN_RESPONSE_LEN=...` at load — capture
that line as step-6 evidence. It is CPU-smoke-tested only; **NPU validation is
pending this very runbook's Windows run.** Check `git log` for that commit
before running; if it has not merged into the branch you are on:

* lower `model.max_new_tokens` in the config and note the reduction alongside
  the result — a run that needed a smaller token budget on NPU is a legitimate
  finding; a run silently reported as equivalent is not.

---

## Step 7 — troubleshooting

### 7.1 NPU missing from `available_devices`

In order:

1. **Driver missing.** Device Manager shows *Other devices → Multimedia Video
   Controller*. The Windows NPU driver arrives through **Windows Update**, or can
   be installed manually from Intel's [NPU Driver — Windows
   page](https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html)
   (this URL is the one linked from the OpenVINO configuration guide). Install,
   reboot, re-run step 4.
2. **Driver too old.** Update to **32.0.100.3104 or newer**, per OpenVINO's NPU
   troubleshooting note. Older drivers are a documented cause of failures that
   are silent as well as loud.
3. **Wrong Windows version.** NPU support requires Windows 11 22H2+; Windows 10
   will show CPU and GPU only.
4. **Reinstall order.** A reported fix on Intel's community forum is to uninstall
   the NPU driver from Device Manager, uninstall the OpenVINO runtime, then
   install the latest driver first and the runtime second. Community advice, not
   documentation — try it only after 1–3.
5. **Firmware.** Some OEM firmware exposes a toggle for the NPU / AI
   accelerator. **Unverified:** no official Intel or OpenVINO page confirming
   such a setting was found while writing this. Check the laptop vendor's own
   manual before concluding the silicon lacks an NPU.
6. **The part may genuinely have no NPU.** Confirm the exact processor
   (`Get-CimInstance Win32_Processor | Select-Object Name`) against Intel ARK
   rather than trusting the "Core Ultra" badge.

### 7.2 NPU listed, but `LLMPipeline(model_dir, "NPU")` fails

* **Update the driver first** (≥ 32.0.100.3104). This is the documented first
  response to failures on NPU.
* **Out-of-memory errors** where the driver cannot be updated: set
  `DISABLE_OPENVINO_GENAI_NPU_L0=1` to disable Level Zero memory allocation
  ([OpenVINO GenAI on NPU, Troubleshooting](https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html)):

  ```powershell
  $env:DISABLE_OPENVINO_GENAI_NPU_L0 = "1"
  .venv\Scripts\python.exe scripts\bench_devices.py --devices NPU
  ```

* **Version mismatch.** The NPU page pins a known-good set for model generation
  (`nncf==2.18.0 onnx==1.18.0 optimum-intel==1.25.2 transformers==4.51.3` with
  `openvino==2026.2.1 openvino-tokenizers==2026.2.1 openvino-genai==2026.2.1`).
  Inference alone does not need the export packages, but if the current release
  misbehaves on this driver, pinning the openvino triplet to that exact version
  set is the documented fallback.
* **RAM.** For Core Ultra Series 2 systems, more than 16 GB may be needed for
  prompts over 1024 tokens on models above 7B. The 1.5B model here is far below
  that, so this should not bite — mentioned only so it is not mistaken for a
  mystery if it does.

### 7.3 Load fails for a quantization reason

Not expected (see step 3 — the fetched model is INT4_SYM / group_size 128 /
ratio 1, a supported combination). If it happens anyway, re-export the base model
channel-wise, which the docs describe as generally the best-performing scheme:

```powershell
.venv\Scripts\python.exe -m pip install "optimum[openvino]"
.venv\Scripts\optimum-cli.exe export openvino -m Qwen/Qwen2.5-1.5B-Instruct `
  --weight-format int4 --sym --ratio 1.0 --group-size -1 `
  models\openvino\qwen2.5-1.5b-instruct-int4-cw-ov
```

Then point `--model-dir` (and `model.model_dir`) at the new folder.

Two cautions, both from the docs: the group-size argument must be **`-1`**
("minus one"), **not** `1`; and channel-wise quantization "generally offers the
best performance but may reduce model accuracy" — so if this path is taken, the
`json_ok` column may get worse, and that has to be reported, not hidden.

Note that `scripts/compress_model_nncf.py` passes `--weight-format` and `--sym`
but exposes no `--group-size` or `--ratio` flag, so run `optimum-cli` directly as
above (or add the flags to the script) rather than expecting the helper to
produce a channel-wise export.

### 7.4 `GPU` missing from `available_devices`

* Install/update the Intel graphics driver from the laptop vendor or Intel.
* On a desktop Core Ultra part with an **F** suffix there is no integrated
  graphics at all, so a missing GPU row is correct, not a fault. Report CPU and
  NPU and say why the GPU row is absent.

### 7.5 First NPU load takes minutes

Expected on a cold compile — see step 5. `CACHE_DIR` (or ahead-of-time
`EXPORT_BLOB` / `BLOB_PATH`) would cut subsequent initialisation times, but both
require a `pipeline_config`, which `bench_devices.py` does not pass today. Two
honest options: report the cold-compile number and label it, or add the config
passthrough and report both cold and warm. Do not report a warm number as if it
were the first load.

---

## Step 8 — what to capture for the judges

### 8a. Raw evidence, saved verbatim

Save the complete console session — do not retype it:

```powershell
.venv\Scripts\python.exe scripts\bench_devices.py --devices CPU GPU NPU `
  *> docs\intel\bench-core-ultra.txt
Get-Content docs\intel\bench-core-ultra.txt
```

Capture alongside it, in the same file or next to it:

* the `available_devices` output from step 4;
* the NPU driver version from step 1b, and `winver`;
* `openvino-genai` and `openvino` versions from step 2;
* the processor name (`Get-CimInstance Win32_Processor | Select-Object Name`);
* the `loading OpenVINO model ... on NPU` log line from step 6, plus whether the
  `--recommend` run completed and wrote its outputs.

### 8b. README subsection, ready to paste

Add this under the existing **"Benchmark every OpenVINO device on this machine"**
section in `README.md`, filling in the blanks from the captured output:

```markdown
#### Measured on an Intel Core Ultra AI PC

`<processor name>`, Windows 11 `<build>`, NPU driver `<version>`,
openvino-genai `<version>`, Python `<version>`. Same INT4 IR
(`qwen2.5-1.5b-instruct-int4-ov`), same command, `model.device` the only
difference between rows.

| device | load s | reply s | ttft ms | tok/s | json |
|---|---|---|---|---|---|
| CPU |  |  |  |  |  |
| GPU |  |  |  |  |  |
| NPU |  |  |  |  |  |

The NPU `load s` figure is a cold on-the-fly compile, not a weights load, and is
not comparable with the CPU row. `json` records whether that device's single
reply parsed as strict JSON — a `no` is an honest quality signal from a 1.5B
INT4 model, not a benchmark failure. One run per device; no repeats.

`model.device: NPU` in `config/city.yaml` also runs the full engine
(`python -m greenplan run --config config/city.yaml --recommend`) on the NPU,
with no code change. Procedure: `docs/intel/npu-benchmark-runbook.md`.
```

### 8c. What the output actually proves

* **One IR, three devices, zero code changes.** The same
  `openvino_model.xml` that runs on the Mac's CPU runs on the Core Ultra's CPU,
  integrated GPU and NPU, selected by a single string in a YAML file. That is a
  portability claim, and the table is direct evidence for it.
* **The NPU is used by the product, not by a demo.** Step 6 runs the ranking
  pipeline end to end with `device: NPU`, so the accelerator is on the real
  path — data in, `recommendations.geojson` out.
* **`json_ok` is the quality column.** Speed with unparseable output is not a
  result. Keeping this column visible is the point.
* It does **not** prove the NPU is faster than the CPU or the GPU. Whatever the
  numbers say is what gets reported.

---

## Step 9 — honest claims

Read this before writing a single sentence about NPU performance.

* **No NPU numbers exist yet.** Until this runbook has actually been executed on
  the Windows laptop, the defensible claim is device *portability*, not NPU
  *performance*. The README's CPU figures came from a real run; NPU figures must
  come from a real run too.
* **Every Intel-specific timing in this project must come from the Windows Core
  Ultra laptop.** The development machine is an **Apple M3 Pro (arm64)**.
  OpenVINO's ARM CPU plugin runs there, and it is useful for development, but no
  number measured on that machine — CPU row, GPU row, or any
  Intel-accelerated-library speedup — may be presented as Intel hardware
  evidence. If a number cannot be traced to the Windows machine, it does not go
  in an Intel table.
* **Do not pre-write a speedup.** An NPU is a low-power offload engine — the
  OpenVINO docs describe it as "a low-power hardware solution ... enables you to
  offload certain neural network computation tasks for more streamlined resource
  management". It may well produce fewer tok/s than the integrated GPU. The
  honest framings are throughput per watt, freeing the CPU/GPU for other work,
  and running offline on a municipal laptop — not "N× faster", unless a measured
  N says so.
* **Label the cold compile.** The NPU `load s` cell is a compilation, not a load.
  Presenting it next to the CPU's load time without that note is misleading.
* **Report failures.** A `FAILED —` line, a `json` value of `no`, or a run that
  needed a reduced `max_new_tokens` all belong in the write-up. The rubric
  rewards evidence, and a benchmark with no failure modes reads as one that was
  not really run.
* **One run is one run.** These are single-shot measurements with no repeats and
  no variance. Either say so, or run each device three times and report the
  spread.

---

## Sources verified (2026-08-27)

* OpenVINO GenAI on NPU — export requirements, `MAX_PROMPT_LEN` / `MIN_RESPONSE_LEN`,
  `PREFILL_HINT` / `GENERATE_HINT`, `CACHE_DIR`, driver floor 32.0.100.3104,
  `DISABLE_OPENVINO_GENAI_NPU_L0`:
  https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html
* NPU device page — NPU introduced with Core Ultra, driver requirement,
  static-shapes limitation:
  https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html
* Configurations for Intel NPU — Windows driver via Windows Update or manual
  download, Device Manager names:
  https://docs.openvino.ai/2026/get-started/install-openvino/configurations/configurations-intel-npu.html
* System requirements — Python 3.10–3.14 64-bit, Windows 11 22H2+ for NPU:
  https://docs.openvino.ai/2026/about-openvino/release-notes-openvino/system-requirements.html
* `openvino-genai` on PyPI — current release, `Requires-Python >=3.10`, Windows
  `win_amd64` wheels for CPython 3.10–3.14: https://pypi.org/project/openvino-genai/
* Model card — INT4_SYM, ratio 1, group_size 128:
  https://huggingface.co/OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov
* GenAI static NPU pipeline — greedy/multinomial only, batch size 1:
  https://github.com/openvinotoolkit/openvino.genai/blob/master/src/cpp/src/llm/pipeline_static.cpp
* Intel NPU driver for Windows (download page, linked from the OpenVINO docs):
  https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html
* Python for Windows downloads: https://www.python.org/downloads/windows/
