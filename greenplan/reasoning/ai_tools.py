"""On-device AI helpers behind /api/ai/*: grounded writing, speech, photos.

Three jobs, all offline, all on Intel OpenVINO:

  write       the assistant's own Qwen 2.5 1.5B (shared with llm_assistant,
              never a second copy) rewrites FACTS the studio has already
              computed into plain sentences for a particular reader. Every
              number in the reply must appear in the facts, or the reply is
              thrown away - see llm_assistant.numbers_are_grounded for why
              that rule is not negotiable.
  transcribe  Whisper small (INT8) turns a short voice clip into text, in
              any of the app's languages.
  see         InternVL2 1B (INT4) answers narrow questions about a photo of
              a site, or picks the likeliest species from a closed list.

The speech and vision models load on first use, not at startup: most
sessions never touch them, and together they are another gigabyte of
memory. Each reports honestly in status() whether it is present, loaded,
or failed and why.

Nothing here raises to the HTTP layer. A job that cannot run returns a
dict with `ok: False` and a reason the studio can show, and the studio
then falls back to the deterministic text it already has.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import llm_assistant

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_MEDIA = Path(os.environ.get("GV_MEDIA_DIR") or (_ROOT / "models" / "media"))
WHISPER_DIR = _MEDIA / "whisper-small-int8-ov"
VLM_DIR = _MEDIA / "internvl2-1b-int4-ov"

# Clips longer than this are cut. Whisper's window is 30 s, and a request
# body has to stay inside the server's limit.
MAX_SPEECH_S = 25
SAMPLE_RATE = 16000

_MODELS: dict[str, dict[str, Any]] = {
    "whisper": {"pipe": None, "device": None, "load_s": None, "error": None},
    "vlm": {"pipe": None, "device": None, "load_s": None, "error": None},
}
_LOAD_LOCK = threading.Lock()
_RUN_LOCKS = {"whisper": threading.Lock(), "vlm": threading.Lock()}


# --------------------------------------------------------------------------
# status

def _present(d: Path, need: str) -> bool:
    try:
        return (d / need).is_file()
    except Exception:
        return False


def status() -> dict[str, Any]:
    """What can actually run, for the studio to decide which buttons to show."""
    llm = llm_assistant.info()
    out = {"write": {"present": llm["present"], "loaded": llm["loaded"],
                     "device": llm["device"], "model": llm["model"]}}
    for key, d, need, name in (("transcribe", WHISPER_DIR, "openvino_encoder_model.xml", "whisper"),
                               ("see", VLM_DIR, "openvino_language_model.xml", "vlm")):
        m = _MODELS[name]
        out[key] = {"present": _present(d, need), "loaded": bool(m["pipe"]),
                    "device": m["device"], "load_s": m["load_s"],
                    "model": d.name if _present(d, need) else None, "error": m["error"]}
    out["runtime"] = "openvino"
    return out


# --------------------------------------------------------------------------
# loading

def _load(name: str) -> Any:
    m = _MODELS[name]
    if m["pipe"]:
        return m["pipe"]
    with _LOAD_LOCK:
        if m["pipe"]:
            return m["pipe"]
        d, need = ((WHISPER_DIR, "openvino_encoder_model.xml") if name == "whisper"
                   else (VLM_DIR, "openvino_language_model.xml"))
        if not _present(d, need):
            m["error"] = "model not installed"
            return None
        try:
            import openvino_genai as genai  # noqa: PLC0415
        except ImportError as exc:
            m["error"] = "openvino-genai not installed (%s)" % exc
            return None
        cache = llm_assistant._cache_dir()                  # same kernel cache
        last = None
        for dev in llm_assistant._devices():
            try:
                t0 = time.time()
                kw = {"CACHE_DIR": cache} if cache else {}
                cls = genai.WhisperPipeline if name == "whisper" else genai.VLMPipeline
                pipe = cls(str(d), dev, **kw)
                m.update(pipe=pipe, device=dev, load_s=round(time.time() - t0, 1), error=None)
                log.info("%s ready on %s in %.1fs", name, dev, time.time() - t0)
                return pipe
            except Exception as exc:
                last = exc
                log.warning("%s would not load on %s: %s", name, dev, exc)
        m["error"] = str(last or "no device would load the model")
        return None


# --------------------------------------------------------------------------
# job 1: grounded writing

_LANGS = llm_assistant._LANGS

# Each task: who is reading, what shape the answer takes, how long. The
# FACTS block is the only thing the model may assert; the reply is checked.
TASKS: dict[str, dict[str, Any]] = {
    "explain_review": {
        "reader": "a municipal ward officer who is not a landscape architect",
        "shape": "In two or three sentences, say what the score means for whether this can be built, then the single most important thing to fix and why it matters.",
        "words": 70},
    "justify_cell": {
        "reader": "a city planner deciding where to plant",
        "shape": "In two sentences, say why this place needs planting and why the chosen species suit it.",
        "words": 55},
    "explain_area": {
        "reader": "a resident or councillor looking at their neighbourhood",
        "shape": "Say what the readings mean for living here and what kind of planting would help most.",
        "words": 70},
    "coach": {
        "reader": "the person drawing this design right now",
        "shape": "Give the next two or three concrete steps, most important first, as plain sentences that start with a verb.",
        "words": 70},
    "brief_help": {
        "reader": "someone who has just chosen this kind of project and has not designed one before",
        "shape": "Explain how to lay this project out on the plot, step by step, as plain sentences that start with a verb.",
        "words": 90},
    "describe_design": {
        "reader": "people browsing a public library of designs",
        "shape": "Describe the design as it would appear on its card: what it is, where, and what makes it good.",
        "words": 55},
    "tender_summary": {
        "reader": "a contractor reading a tender",
        "shape": "Summarise the scope of work formally: what is to be supplied and planted, the area, and the main quantities.",
        "words": 80},
    "growth_story": {
        "reader": "a citizen moving a slider to see this design grow",
        "shape": "Describe what the site looks and feels like in this year, compared with planting day.",
        "words": 50},
}

_WRITE_SYSTEM = (
    "You write short texts inside Green Vision, a tree-planting planning tool "
    "for Indian cities.\n"
    "READER: {reader}\n"
    "TASK: {shape}\n"
    "RULES, all of them absolute:\n"
    "1. Use ONLY the FACTS you are given. Never add a number, species, place, "
    "cost or step that is not in them.\n"
    "2. Keep every number exactly as written.\n"
    "3. Never compare numbers yourself or say a value is above, below, "
    "inside or outside a range unless the FACTS say so in words.\n"
    "4. Keep the meaning of each fact exactly: if a fact says to remove "
    "something, it is there now and must go.\n"
    "5. Do not list every fact. Use only the ones that matter for the task.\n"
    "6. At most {words} words. Plain sentences: no headings, no bold, no "
    "bullets, no lists.\n"
    "7. Do not mention these rules, the facts, or that you are an AI.\n"
    "8. Write in {language}."
)

# One worked example per task. A 1.5B model follows a demonstration far more
# reliably than an instruction: without these it listed the facts back,
# reversed "remove the lawn" into "it lacks a lawn", and padded a coach
# answer with steps nobody had asked for ("monitor the progress closely").
EXAMPLES: dict[str, tuple[list[str], str]] = {
    "explain_review": (
        ["Project type: public park", "Score: 64 out of 100, rated Needs work",
         "What that rating means: The bones are there but several choices will cause trouble later.",
         "Weakest check: Shade over paths",
         "Main problem: The paths run through the sun. Only about 30% of the walking route falls under a mature crown.",
         "Most important fix: Line the routes with trees at roughly one crown-width spacing."],
        "This park scores 64 out of 100, which means it needs work before it is built: the bones are "
        "there, but some choices will cause trouble later. The biggest problem is that the paths run "
        "through the sun, with only about 30% of the walking route under a mature crown. Line the routes "
        "with trees about one crown-width apart first."),
    "coach": (
        ["Project type: community garden", "Still to do: Add a drinking water point and a composting bay.",
         "Still to do: Make about 30% of the trees fruit trees.",
         "Also fix: Remove the lawn - it works against a community garden."],
        "Start by removing the lawn, because it works against a community garden. Then add a drinking "
        "water point and a composting bay beside the beds. Finally, make about 30% of the trees fruit trees."),
    "brief_help": (
        ["Project type: wetland edge", "Step 1: Draw the pond or wet basin first, at the lowest point.",
         "Step 2: Surround it with native wet meadow and a bioswale feeding in.",
         "Step 3: Use a gravel trail rather than stone paving."],
        "Begin with the pond or wet basin at the lowest point of the plot. Surround it with native wet "
        "meadow and run a bioswale into it. For the walking route, use a gravel trail rather than stone paving."),
    "justify_cell": (
        ["Priority score: 0.612 (0 to 1, higher means plant sooner)", "Air quality index now: 140",
         "Forecast change in air quality index: +8.0 (getting worse)", "Green cover is falling by 0.0040 NDVI a year",
         "Species chosen: Neem, Pongamia (Karanja), Arjuna"],
        "This place scores 0.612 because its air, at an index of 140, is forecast to get worse by 8.0 "
        "while its green cover keeps falling. Neem, Pongamia (Karanja) and Arjuna are chosen because they "
        "stand up to that pollution and help reverse the loss of green."),
    "explain_area": (
        ["Place: Naranpura, Ahmedabad", "Tree canopy visible in satellite imagery: 9% of the view",
         "Air quality index (US AQI): 152, band: Unhealthy", "Parks nearby: 1",
         "Project the reader is planning: public park"],
        "Naranpura has very little tree cover, about 9% of the view, and its air is in the Unhealthy band "
        "at an index of 152, with only one park nearby. A public park with plenty of shade trees would "
        "help both the heat and the air here."),
    "describe_design": (
        # No name in the example on purpose: given one, the model put the
        # example's name on the real design instead of its own.
        ["Project type: public park", "Place: Vastrapur, Ahmedabad",
         "Plot area: 6,200 m2 (0.62 ha)", "Trees: 48 (8 Neem, 6 Jamun, 6 Arjuna)",
         "Review score: 91 out of 100, rated Strong"],
        "A 0.62 ha public park in Vastrapur, Ahmedabad, planted with 48 trees led by Neem, Jamun and "
        "Arjuna. It scores 91 out of 100 on the design review."),
    "tender_summary": (
        ["Project type: roadside avenue", "Plot area: 2,400 m2 (0.24 ha)",
         "Estimated total cost including GST: ₹18,40,000", "Item: Neem, 40 each, ₹28,000",
         "Item: Hedge screen, 300 m, ₹2,55,000"],
        "Supply, planting and three-year establishment of a roadside avenue over 2,400 m2, including 40 "
        "Neem trees and 300 m of hedge screen, at an estimated cost of ₹18,40,000 including GST."),
    "growth_story": (
        ["Project type: public park", "Year shown: 15 years after planting (2041)",
         "Canopy on planting day: 4% of the plot", "Canopy in the year shown: 38% of the plot",
         "Carbon dioxide stored by then: 41 tonnes"],
        "By 2041 the saplings have become real shade: crowns that covered 4% of the plot on planting day "
        "now cover 38%, and the trees have stored about 41 tonnes of carbon dioxide."),
}

_REFUSE = re.compile(r"\b(as an ai|language model|i cannot|i can't|facts block|the facts)\b", re.I)

_STOP = set("""about above after again against also because been before being below between both
could does doing down during each from further have having here into itself just more most much
must need only other over same should some such than that their them then there these they this
those through under until very were what when where which while will with would your yours
first then finally start begin next make made using used keep kept well like""".split())


def _content(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP]


def _stem(w: str) -> str:
    for suf in ("ingly", "ings", "ing", "edly", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _on_topic(text: str, facts: str, extra: str) -> float:
    """Share of the reply's content words that come from the facts (or the
    task wording). A reply that drifts into its own advice scores low."""
    words = _content(text)
    if not words:
        return 1.0
    vocab = {_stem(w) for w in _content(facts + " " + extra)}
    return sum(1 for w in words if _stem(w) in vocab) / len(words)


def _turn(role: str, text: str) -> str:
    return "<|im_start|>%s\n%s<|im_end|>\n" % (role, text)


def write(body: dict[str, Any]) -> dict[str, Any]:
    task = str(body.get("task") or "")
    spec = TASKS.get(task)
    if not spec:
        return {"ok": False, "reason": "unknown task", "tasks": sorted(TASKS)}
    facts_in = body.get("facts")
    if isinstance(facts_in, list):
        facts = "\n".join("- " + str(f).strip()[:300] for f in facts_in[:40] if str(f).strip())
    else:
        facts = str(facts_in or "").strip()[:6000]
    if not facts:
        return {"ok": False, "reason": "no facts given"}
    if not llm_assistant.available():
        return {"ok": False, "reason": "the on-device model is not installed"}
    lang = str(body.get("lang") or "en")
    words = int(spec["words"])
    system = _WRITE_SYSTEM.format(reader=spec["reader"], shape=spec["shape"], words=words,
                                  language=_LANGS.get(lang, "English"))
    ask = "Write it now."
    prompt = _turn("system", system)
    ex = EXAMPLES.get(task)
    if ex and lang == "en":
        prompt += _turn("user", "FACTS\n" + "\n".join("- " + f for f in ex[0]) + "\n\n" + ask)
        prompt += _turn("assistant", ex[1])
    prompt += _turn("user", "FACTS\n" + facts + "\n\n" + ask) + "<|im_start|>assistant\n"
    t0 = time.time()
    raw = llm_assistant.generate_prompt(prompt, max_tokens=int(words * 2.2) + 40)
    ms = int((time.time() - t0) * 1000)
    if not raw:
        return {"ok": False, "reason": "the on-device model did not answer", "ms": ms}
    old_cap = llm_assistant.ANSWER_WORDS
    try:
        llm_assistant.ANSWER_WORDS = words + 10
        text = llm_assistant._dedupe(llm_assistant._clean(raw))
    finally:
        llm_assistant.ANSWER_WORDS = old_cap
    text = re.sub(r"^\s*[-*•]\s*", "", text).strip()
    # A small model echoes the facts' own labels ("Most important fix: ...",
    # "Still to do: ..."). They are scaffolding, not prose.
    for lab in set(re.findall(r"^-?\s*([A-Z][A-Za-z ()/-]{2,48}):\s", facts, re.M)):
        text = re.sub(r"\b" + re.escape(lab) + r"\s*:\s*", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    if len(text) < 20:
        return {"ok": False, "reason": "the reply was too short to use", "ms": ms}
    if _REFUSE.search(text):
        return {"ok": False, "reason": "the reply talked about itself instead of the design", "ms": ms}
    if not llm_assistant.numbers_are_grounded(text, facts, ask):
        log.warning("ai write (%s) rejected: ungrounded figure in %r", task, text[:300])
        return {"ok": False, "reason": "the model added a number that is not in the design", "ms": ms}
    if ex and lang == "en":
        # The example is there to show the shape, and a small model will
        # copy its names: "Ward 12 Park" turned up in a real description.
        ex_names = set(re.findall(r"[A-Z][a-z]{3,}", ex[1] + " " + " ".join(ex[0])))
        leaked = [w for w in ex_names if w in text and w not in facts and w.lower() not in spec["shape"].lower()
                  and w not in ("This", "Then", "Start", "Begin", "Finally", "Line", "Supply", "Make", "Remove")]
        if leaked:
            log.warning("ai write (%s) rejected: copied %s from the example", task, leaked)
            return {"ok": False, "reason": "the model copied the worked example instead of this design", "ms": ms}
    if lang == "en":
        share = _on_topic(text, facts, spec["shape"] + " " + spec["reader"])
        if share < 0.5:
            log.warning("ai write (%s) rejected: off the facts (%.2f) in %r", task, share, text[:300])
            return {"ok": False, "reason": "the model wandered away from the design's own facts", "ms": ms}
    return {"ok": True, "text": text, "task": task, "ms": ms,
            "by": "local-llm", "model": llm_assistant.info().get("model"),
            "device": llm_assistant.info().get("device")}


# --------------------------------------------------------------------------
# job 2: speech to text

_WHISPER_LANG = {"en", "hi", "gu", "mr", "bn", "ta", "te", "kn", "ml", "pa", "ur", "fr", "de",
                 "as", "or"}
# Whisper's well-known filler when it is handed silence or noise.
_HALLUCINATED = re.compile(r"^\s*(you|thank you\.?|thanks for watching!?|\.+|bye\.?)\s*$", re.I)


def transcribe(body: dict[str, Any]) -> dict[str, Any]:
    import numpy as np  # noqa: PLC0415
    b64 = body.get("pcm16")
    if not b64:
        return {"ok": False, "reason": "no audio"}
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return {"ok": False, "reason": "audio was not base64"}
    pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    rate = int(body.get("rate") or SAMPLE_RATE)
    if rate != SAMPLE_RATE and len(pcm):
        n = int(len(pcm) * SAMPLE_RATE / rate)
        pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(np.float32)
    pcm = pcm[: MAX_SPEECH_S * SAMPLE_RATE]
    secs = len(pcm) / SAMPLE_RATE
    if secs < 0.4:
        return {"ok": False, "reason": "the clip was too short"}
    rms = float(np.sqrt(np.mean(pcm ** 2))) if len(pcm) else 0.0
    if rms < 0.004:
        return {"ok": False, "reason": "no speech was heard - check the microphone"}
    pipe = _load("whisper")
    if not pipe:
        return {"ok": False, "reason": _MODELS["whisper"]["error"] or "speech model unavailable"}
    lang = str(body.get("lang") or "").lower()
    t0 = time.time()
    try:
        with _RUN_LOCKS["whisper"]:
            cfg = pipe.get_generation_config()
            cfg.max_new_tokens = 160
            cfg.task = "transcribe"
            if lang in _WHISPER_LANG:
                cfg.language = "<|%s|>" % lang
            res = pipe.generate(pcm.tolist(), generation_config=cfg)
        text = str(res).strip()
    except Exception as exc:
        log.warning("transcribe failed: %s", exc)
        return {"ok": False, "reason": "the speech model failed: %s" % exc}
    ms = int((time.time() - t0) * 1000)
    if not text or _HALLUCINATED.match(text):
        return {"ok": False, "reason": "no clear words were heard", "ms": ms}
    return {"ok": True, "text": text, "seconds": round(secs, 1), "ms": ms,
            "model": WHISPER_DIR.name, "device": _MODELS["whisper"]["device"]}


# --------------------------------------------------------------------------
# job 3: photos

# Narrow yes/no questions. A 1B vision model describing a scene freely
# invents things; asked one concrete question at a time it is far steadier,
# and every answer is shown as a guess to confirm on site.
SITE_QUESTIONS = [
    ("open_ground", "Is there open bare soil or empty open ground in this photo? Answer yes or no."),
    ("trees", "Are there trees in this photo? Answer yes or no."),
    ("water", "Is there standing water, a pond, a lake or a river in this photo? Answer yes or no."),
    ("waste", "Is there garbage, rubble or construction debris in this photo? Answer yes or no."),
    ("paved", "Is most of the ground in this photo paved, concrete or asphalt? Answer yes or no."),
    ("wires", "Are there overhead electric wires or poles in this photo? Answer yes or no."),
]


def _image_tensor(body: dict[str, Any]):
    import numpy as np  # noqa: PLC0415
    import openvino as ov  # noqa: PLC0415
    w, h = int(body.get("w") or 0), int(body.get("h") or 0)
    raw = base64.b64decode(body.get("rgb") or "")
    if w < 32 or h < 32 or w * h * 3 != len(raw) or w > 1600 or h > 1600:
        raise ValueError("image must be raw RGB bytes matching w x h (32..1600 px)")
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
    return ov.Tensor(np.ascontiguousarray(arr))


def _ask(pipe: Any, img: Any, q: str, tokens: int) -> str:
    import openvino_genai as genai  # noqa: PLC0415
    cfg = genai.GenerationConfig()
    cfg.max_new_tokens = tokens
    cfg.do_sample = False
    r = pipe.generate(q, images=[img], generation_config=cfg)
    return str(r.texts[0] if hasattr(r, "texts") else r).strip()


def see(body: dict[str, Any]) -> dict[str, Any]:
    task = str(body.get("task") or "site")
    try:
        img = _image_tensor(body)
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    pipe = _load("vlm")
    if not pipe:
        return {"ok": False, "reason": _MODELS["vlm"]["error"] or "vision model unavailable"}
    t0 = time.time()
    try:
        with _RUN_LOCKS["vlm"]:
            if task == "species":
                cands = [str(c).strip() for c in (body.get("candidates") or []) if str(c).strip()][:40]
                if len(cands) < 2:
                    return {"ok": False, "reason": "give at least two candidate species"}
                said = _ask(pipe, img, "Which one of these trees or plants is shown in this photo: "
                            + ", ".join(cands) + "? Answer with exactly one name from the list.", 16)
                low = said.lower()
                hit = next((c for c in cands if c.lower() in low), None)
                if not hit:
                    hit = next((c for c in cands if c.split("(")[0].strip().lower() in low), None)
                ms = int((time.time() - t0) * 1000)
                if not hit:
                    return {"ok": False, "reason": "the photo did not match any species in the list",
                            "said": said[:80], "ms": ms}
                return {"ok": True, "species": hit, "ms": ms, "model": VLM_DIR.name,
                        "device": _MODELS["vlm"]["device"]}
            answers: dict[str, bool | None] = {}
            for key, q in SITE_QUESTIONS:
                a = _ask(pipe, img, q, 4).lower()
                answers[key] = True if a.startswith("yes") else False if a.startswith("no") else None
            desc = _ask(pipe, img, "Describe the ground and vegetation in this photo in one short "
                        "sentence, for someone planning to plant trees here.", 48)
    except Exception as exc:
        log.warning("see failed: %s", exc)
        return {"ok": False, "reason": "the vision model failed: %s" % exc}
    desc = llm_assistant._clean(desc).split("\n")[0][:240]
    return {"ok": True, "task": "site", "answers": answers, "description": desc,
            "ms": int((time.time() - t0) * 1000), "model": VLM_DIR.name,
            "device": _MODELS["vlm"]["device"]}
