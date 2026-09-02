# Intel technology runbooks

Four documents written for the Intel AI Global Impact Festival Stage 3
submission, all researched against live Intel sources on **2026-08-27** — not
from memory, because two of the rubric's product names turned out to no longer
exist in the form the rubric implies:

- **Intel Geti's hosted trial is gone.** Geti v3 is open-source (Apache-2.0),
  self-hosted on x86_64 Linux/Windows only, and removed semantic segmentation.
- **"Intel Tiber AI Cloud" URLs are dead.** The live product is **Intel® Cloud
  Services** at <https://cloud.intel.com> (GA Feb 2026).

Each runbook says what was verified, what was not, and what may not be claimed
until someone has run it. None of them contains a number that has not been
measured.

| Document | What it is | Status |
|---|---|---|
| [npu-benchmark-runbook.md](./npu-benchmark-runbook.md) | Exact Windows procedure: install openvino-genai, verify the Core Ultra NPU, run `scripts/bench_devices.py`, capture the CPU/GPU/NPU table and the full-engine NPU run for the README | Procedure ready; **no NPU numbers exist yet** |
| [geti-canopy-runbook.md](./geti-canopy-runbook.md) | Train a canopy instance-segmentation model on Geti v3 from stratified Esri tiles (`scripts/geti_export_tiles.py`), export to OpenVINO IR, and test it against the studio's VARI index under a stated evidence gate | Plan; script built and smoke-tested, no model trained |
| [tiber-cloud-training-runbook.md](./tiber-cloud-training-runbook.md) | Run `greenplan.forecast.train` on Intel® Cloud Services, pull the ONNX artifacts back, and watch the engine's champion gate judge them | Procedure ready; expects the honest NEGATIVE verdict |
| [opea-assessment.md](./opea-assessment.md) | What OPEA is, why this single-laptop project does not use it, and the one real integration seam (OPEA's OpenVINO Model Server backend) as a labelled blueprint | Assessment; recommendation: do not claim usage |

The Intel technology that is real today and needs no runbook to claim: OpenVINO
GenAI running the INT4 LLM in-process (`scripts/fetch_openvino_model.py`), NNCF
compression done in this repo (`scripts/compress_model_nncf.py`, measured in the
README), the ONNX → OpenVINO forecaster challenger harness
(`greenplan/forecast/train.py`), and `model.device` targeting CPU / GPU / NPU
unchanged.

One rule spans all four documents: every Intel-specific timing must come from
Intel hardware — the Windows Core Ultra laptop or a Cloud Services Xeon
instance — never from the arm64 development Mac.
