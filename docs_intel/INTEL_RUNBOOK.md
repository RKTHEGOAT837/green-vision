# Intel Laptop Runbook — evidence for the global stage

This file is written to be executed by **Claude Code** on an Intel laptop
(plain Core i5 or a Core Ultra "AI PC"). Human teammates can follow it too.
Goal: produce the measured evidence for the Intel Technologies section of the
Intel® AI Global Impact Festival submission — a per-device benchmark table, a
full pipeline run on Intel silicon, and studio screenshots.

**For Claude Code:** work through the steps in order. Run every command,
verify its check before moving on, and at the end write `EVIDENCE.md` (step 8)
with all collected numbers. Ask the human only when a step needs a driver
install or a reboot.

## 1. Prerequisites

- Python 3.12 (on Windows: python.org installer, tick "Add python.exe to PATH")
- Git
- Up-to-date **Intel graphics driver**; on Core Ultra also the **Intel NPU
  driver** (Intel Driver & Support Assistant, then reboot). Without these the
  GPU/NPU devices will not appear in step 4 — if they are missing there,
  return here.

## 2. Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Linux/macOS: .venv/bin/pip
.venv/Scripts/python scripts/fetch_openvino_model.py # ~1 GB, one time
```

Check: `models/openvino/qwen2.5-1.5b-instruct-int4-ov/openvino_model.xml` exists.

## 3. Record the machine

Record the exact processor string (Windows: Settings → System → About, or
`(Get-CimInstance Win32_Processor).Name` in PowerShell). This goes on the
submission form verbatim.

## 4. THE benchmark — most important step

```bash
.venv/Scripts/python scripts/bench_devices.py
```

- Expected devices: i5 → `CPU` + `GPU`; Core Ultra → `CPU` + `GPU` + `NPU`.
- Save the complete output (copy-paste AND screenshot).
- The `json` column should read `yes` on Intel hardware. Record it either way:
  on an ARM Mac this model emits garbled JSON (suspected INT4 kernel issue in
  OpenVINO's ARM build), so a `yes` here is itself a finding worth reporting.
- If `GPU`/`NPU` are missing → step 1 drivers, reboot, rerun.

## 5. Full pipeline on CPU, then GPU/NPU

```bash
.venv/Scripts/python -u -m greenplan run --config config/city.yaml --recommend
```

Record: total wall time, and whether any line says
`falling back to the deterministic writer` (on Intel the expectation is NO —
meaning the local LLM wrote the justifications itself; record yes/no).

Then edit `config/city.yaml` → `model.device: GPU`, rerun, record the time.
On Core Ultra repeat with `NPU` and `AUTO`. Restore `device: CPU` afterwards.

## 6. Studio demo

```bash
.venv/Scripts/python -m http.server 8123
```

Open http://localhost:8123 → "Open the map" → click inside Ahmedabad → wait
for the 100 km² report → bottom bar **Priority** → click the darkest hexagon.
Screenshot: (a) the area report, (b) the Priority hexagons, (c) the cell
detail with species + justification. On the Core Ultra also screen-record
this ~30 s flow for the video.

## 7. Optional, high value (~20 min): NNCF compression on Intel silicon

```bash
.venv/Scripts/pip install "optimum[openvino]"
.venv/Scripts/python scripts/compress_model_nncf.py --base Qwen/Qwen2.5-0.5B-Instruct --verify
```

Record the printed size and time — "FP16 → INT4 with NNCF, run on an Intel
AI PC" is a lifecycle claim for the form.

## 8. Write EVIDENCE.md

Create `EVIDENCE.md` in the repo root containing: processor string, the full
benchmark table per machine, pipeline wall times per device, the
fallback yes/no, NNCF numbers if run, and paths of the screenshots taken.
Commit it to this branch and push. Every number in the submission form's
Intel section must trace back to a line in this file.
