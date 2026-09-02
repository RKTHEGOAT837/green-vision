# OPEA and Green Vision

**Green Vision does not use OPEA.** This page says what OPEA is, why it does not
fit a single-laptop tool, and what a real integration would look like if a city
ever wanted one. Everything below was checked against the OPEA repositories and
LF AI & Data listings in **August 2026**; links are at the bottom.

## What OPEA is

OPEA — Open Platform for Enterprise AI — is an open-source framework for
composing GenAI applications out of containerised microservices. It was launched
by the **LF AI & Data Foundation on 16 April 2024** and is still listed there at
**Sandbox** stage; Intel remains the dominant contributor (most of the top
committers on `GenAIComps` are Intel accounts, and files carry Intel copyright
headers). Its four main repositories: **GenAIComps** (the microservices — `llm`,
`embedding`, `retriever`, `rerank`, `dataprep`, `asr`, `tts`), **GenAIExamples**
(assembled reference apps: ChatQnA, DocSum, CodeGen, CodeTrans, AudioQnA,
VisualQnA, MultimodalQnA, Text2Image and more), **GenAIInfra** (containerisation,
Helm charts, the microservices connector) and **GenAIEval** (benchmarking). The
latest tagged release is **v1.5, 22 December 2025**, with both main repos still
committed to in August 2026. Deployment is Docker Compose or Kubernetes, and the
reference hardware in the GenAIExamples README is server-class: 64 vCPU / 100 GB
RAM Xeon, Gaudi cards, AMD EPYC, 8× MI300X. The site claims the architecture runs
on "cloud, data center, edge and PC", but nothing in the repositories describes a
laptop-sized deployment path — so treat the edge claim as marketing until
someone points at an artefact.

## What Green Vision is

One Python process on one laptop. The model is Qwen2.5-1.5B compressed to INT4
and loaded **in-process** through OpenVINO GenAI — no container, no HTTP hop, no
orchestrator, no key. That is the architectural opposite of a microservices mesh,
and deliberately so: a municipal office with no IT department can run
`python -m greenplan run` and no per-cell figure leaves the machine.

## Where a real overlap would be — a blueprint, not usage

There is one honest point of contact, and it is worth naming precisely, because
OPEA does have an OpenVINO serving path.

OPEA's LLM text-generation microservice supports **OpenVINO Model Server (OVMS)**
as a backend: `comps/llms/src/text-generation/README_ovms.md`, the integration
`integrations/ovms.py`, and a `textgen-service-ovms` service in
`comps/llms/deployment/docker_compose/compose_text-generation.yaml`, which brings
up `openvino/model_server:2025.0` from `comps/third_parties/ovms/`. OPEA's own
instructions have you export a Hugging Face model to OpenVINO IR with weight
compression before serving it — the same artefact class Green Vision already
ships at INT4. OVMS exposes an OpenAI-compatible REST API (`/v3/chat/completions`).

So the blueprint, **labelled as a plan and not built**: a city running Green
Vision as a shared service for many planners instead of one analyst's laptop
would stand up OVMS behind OPEA's `llm-textgen` microservice, point it at the
same INT4 IR, and set the engine's `model.provider` to that endpoint. The engine
already abstracts hosted OpenAI-compatible providers (`nvidia`, `openrouter`), so
this is the existing seam, not a new one — one endpoint URL, no change to the
reasoning code. Costs of that move, stated plainly: it needs Docker, a machine
that stays up, and someone to maintain it; and the privacy claim weakens from
"never leaves this laptop" to "never leaves city IT". Neither has been built or
tested. It is a paragraph in a document, and that is all it is.

## Recommendation

**Do not claim OPEA usage.** The rubric names OPEA as one Intel technology among
several, and the Intel story here already stands on things that are real and
measured: OpenVINO GenAI running the model in-process, NNCF INT4 compression done
in this repo (Qwen2.5-0.5B FP16 → INT4 in 292 s, 343 MB on disk), the ONNX →
OpenVINO forecaster challenger, and `model.device` passing straight through to
CPU / integrated GPU / NPU with no code change. Adding a Docker Compose file that
wraps the model in a microservice nobody uses would add a dependency, a moving
part and a weaker privacy claim, in exchange for a checkbox — and a judge who
opens the repo would see it for what it is.

Keeping the blueprint paragraph above as declared future work is fine, provided
it stays labelled as future work. An honest "not applicable, and here is exactly
why, and here is the component we would use if it were" is a stronger answer than
a decorative microservice.

### If judges ask about OPEA

> We don't use OPEA, and it would be the wrong fit — it's a Kubernetes and Docker
> Compose framework for multi-node enterprise deployments, and this whole project
> is one offline laptop with the model loaded in-process through OpenVINO GenAI.
> The real overlap is that OPEA's LLM microservice can be backed by OpenVINO Model
> Server, so if a city ran this as a shared service for many planners, the same
> INT4 model would sit behind that component and our provider setting would point
> at it instead. We've written that down as a plan, not shipped it as a claim.

## Sources (checked August 2026)

- OPEA site and stated values — https://opea.dev/
- LF AI & Data launch announcement, 16 Apr 2024 — https://lfaidata.foundation/blog/2024/04/16/lf-ai-data-foundation-launches-open-platform-for-enterprise-ai-opea-for-groundbreaking-enterprise-ai-collaboration/
- LF AI & Data project listing (OPEA under Sandbox) — https://lfaidata.foundation/projects/
- GenAIComps — https://github.com/opea-project/GenAIComps
- OPEA LLM microservice with OVMS — https://github.com/opea-project/GenAIComps/blob/main/comps/llms/src/text-generation/README_ovms.md
- GenAIExamples, deployment options and reference hardware — https://github.com/opea-project/GenAIExamples
- Release history (v1.5, 22 Dec 2025) — https://github.com/opea-project/GenAIExamples/releases
- OpenVINO Model Server — https://docs.openvino.ai/2025/model-server/ovms_what_is_openvino_model_server.html
