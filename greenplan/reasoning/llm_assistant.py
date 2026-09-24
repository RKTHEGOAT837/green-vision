"""The local language model, in the assistant.

WHAT THIS IS FOR, AND WHAT IT IS NOT FOR.

Green Vision's assistant has always been deterministic: a message is
classified into one of thirty-odd intents, and each intent is answered by a
function that reads the same panel the map is drawn from. Every number in
those answers is traceable to a measurement. That is the part of the
assistant people are entitled to trust, and it is not going anywhere.

What it could not do is hold a conversation. Anything outside the intent
list came back as "I did not understand that", which is an honest answer and
a useless one, and a planner asking "why does this ward need trees more than
the next one" got nothing.

So the model is given exactly the two jobs it is good at:

  ROUTING - when the rules and the trained classifier both fail, it is shown
  the list of things the assistant can actually do and asked which one the
  message means. If it names one, the deterministic handler answers. The
  reply is still computed from measurements; only the decision about WHICH
  question was asked came from the model.

  ANSWERING - when nothing routes, it answers in words, from a block of
  facts this engine has already measured, under an instruction to use
  nothing else. It is told to say it does not have a figure rather than
  produce one.

It never rewrites an answer a handler produced. A 1.5-billion-parameter
model paraphrasing "PM2.5 is 22 ug/m3" is a chance to get it wrong for no
gain, and the rule against inventing numbers is only enforceable if the
numbers never pass through it in the first place.

EVERY failure is a fallback. No weights, no runtime, a slow load, a refusal,
a timeout - all of them return None and the assistant behaves exactly as it
did before this file existed.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# How long a single reply may take before the assistant gives up on it and
# answers the old way. On the integrated GPU a 90-token reply takes about
# four seconds; on a CPU-only machine the same reply is nearer thirty, which
# is past the point where a person has decided the app is broken.
REPLY_BUDGET_S = float(os.environ.get("GV_LLM_BUDGET_S", "25"))

# Short on purpose. The assistant's answers are a chat bubble, not an essay,
# and every token is time the reader spends watching a cursor blink.
ANSWER_TOKENS = 130
ROUTE_TOKENS = 12

_STATE: dict[str, Any] = {
    "pipe": None,        # the compiled pipeline, or False once it has failed
    "device": None,
    "load_s": None,
    "error": None,
    "loading": False,
}
_LOCK = threading.Lock()


def _model_dir() -> Path:
    """Where the weights live.

    The environment wins, then the packaged engine's own models folder, then
    the repository layout. The desktop build ships the model beside the
    engine, so the second of those is the one that answers in an install.
    """
    env = os.environ.get("GV_LLM_DIR")
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent.parent
    return root / "models" / "openvino" / "qwen2.5-1.5b-instruct-int4-ov"


def available() -> bool:
    """Are the weights on disk? Cheap - no import, no load."""
    try:
        return (_model_dir() / "openvino_model.xml").is_file()
    except Exception:
        return False


def _devices() -> list[str]:
    """Which OpenVINO device to try, best first.

    The integrated GPU is worth the preference by a wide margin: measured on
    an Intel Core 5 210H, 100 tokens a second against 5 on the CPU cores of
    the same chip. The first compile for a GPU is slow - about fifty seconds
    - which is why CACHE_DIR below exists and why loading happens in the
    background at startup rather than inside somebody's first question.
    """
    want = (os.environ.get("GV_LLM_DEVICE") or "").strip()
    if want:
        return [want]
    try:
        import openvino as ov  # noqa: PLC0415
        devs = ov.Core().available_devices
    except Exception:
        return ["CPU"]
    order = [d for d in devs if d.startswith("GPU")] + ["CPU"]
    return order


def _cache_dir() -> str:
    """Where OpenVINO may keep compiled kernels between runs.

    Beside the weights when that is writable - an install is usually under
    Program Files and is not - otherwise the user's temp directory. Without
    it every start pays the GPU compile again.
    """
    # The user's temp directory FIRST, not the model folder. An install
    # under Program Files is read-only to the person running it, so the
    # model folder usually fails anyway - and when it succeeds, in a
    # development tree, it leaves a directory inside models/openvino that
    # the packaging script then tries to ship as a second model.
    for cand in (Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
                 / "greenvision-ovcache",
                 _model_dir().parent / ".ovcache"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".w"
            probe.write_text("1", encoding="utf-8")
            probe.unlink()
            return str(cand)
        except Exception:
            continue
    return ""


def _load() -> Any:
    """Compile the pipeline. Called once, off the request path."""
    if _STATE["pipe"] is not None:
        return _STATE["pipe"]
    d = _model_dir()
    if not (d / "openvino_model.xml").is_file():
        _STATE["pipe"] = False
        _STATE["error"] = "weights not installed"
        return False
    try:
        import openvino_genai as genai  # noqa: PLC0415
    except ImportError as exc:
        _STATE["pipe"] = False
        _STATE["error"] = "openvino-genai not installed (%s)" % exc
        return False

    cache = _cache_dir()
    last = None
    for dev in _devices():
        try:
            t0 = time.time()
            kw = {"CACHE_DIR": cache} if cache else {}
            pipe = genai.LLMPipeline(str(d), dev, **kw)
            _STATE.update({"pipe": pipe, "device": dev,
                           "load_s": round(time.time() - t0, 1), "error": None})
            log.info("assistant LLM ready on %s in %.1fs", dev, time.time() - t0)
            return pipe
        except Exception as exc:                       # try the next device
            last = exc
            log.warning("assistant LLM would not load on %s: %s", dev, exc)
    _STATE["pipe"] = False
    _STATE["error"] = str(last or "no device would load the model")
    return False


def warm() -> None:
    """Start loading in the background.

    Called when the engine starts. A GPU compile is tens of seconds on a cold
    cache, and nobody should discover that inside their first question - by
    the time the map has finished its first read, this is usually done.
    """
    if _STATE["pipe"] is not None or _STATE["loading"]:
        return
    if not available():
        return
    _STATE["loading"] = True

    def run() -> None:
        try:
            _load()
        finally:
            _STATE["loading"] = False

    threading.Thread(target=run, name="gv-llm-warm", daemon=True).start()


def info() -> dict[str, Any]:
    """What is actually answering, for /api/health. Never a claim that the
    model is in use when it is not."""
    return {
        "present": available(),
        "loaded": bool(_STATE["pipe"]),
        "loading": bool(_STATE["loading"]),
        "device": _STATE["device"],
        "load_s": _STATE["load_s"],
        "model": _model_dir().name if available() else None,
        "error": _STATE["error"],
    }


# --------------------------------------------------------------------------
# generation

_CHATML = ("<|im_start|>system\n{system}<|im_end|>\n"
           "<|im_start|>user\n{user}<|im_end|>\n"
           "<|im_start|>assistant\n")


def _generate(system: str, user: str, max_tokens: int) -> str | None:
    pipe = _STATE["pipe"]
    if pipe is None:
        # Not warmed - load now rather than refuse, but only once.
        with _LOCK:
            pipe = _load()
    if not pipe:
        return None
    try:
        import openvino_genai as genai  # noqa: PLC0415
        cfg = genai.GenerationConfig()
        cfg.max_new_tokens = max_tokens
        cfg.do_sample = False              # a planning tool is not a chatbot
        # A small greedy model falls into loops. Asked whether the air was
        # safe for children it produced the same two sentences four times
        # and hit the token ceiling. Both of these are cheap insurance and
        # neither affects a short factual answer.
        try:
            cfg.repetition_penalty = 1.12
            cfg.no_repeat_ngram_size = 8
        except Exception:
            pass
        t0 = time.time()
        out = str(pipe.generate(_CHATML.format(system=system, user=user), cfg))
        dt = time.time() - t0
        if dt > REPLY_BUDGET_S:
            # Answered, but too late to be useful next time: say so in the
            # log rather than silently keeping a device nobody can wait for.
            log.warning("assistant LLM took %.1fs on %s", dt, _STATE["device"])
        return out.strip()
    except Exception as exc:
        log.warning("assistant LLM generate failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# job 1: routing


def route(message: str, intents: dict[str, str]) -> str | None:
    """Which of the assistant's own intents does this message mean?

    `intents` maps name -> one line of what it does. The model is asked for a
    bare name, not JSON: a 1.5B model emits a stray comma or an unclosed
    brace often enough that JSON parsing becomes the failure mode, while
    "which of these words did it say" is a question with no syntax to get
    wrong.

    Returns a name from `intents`, or None - and None is the honest answer
    whenever the output does not contain exactly one of them.
    """
    if not message or not intents:
        return None
    menu = "\n".join("%s = %s" % (k, v) for k, v in intents.items())
    system = (
        "You label questions for a city tree-planting tool. "
        "Reply with ONE label from this list and nothing else. "
        "If none of them fits, reply: none\n\n" + menu
    )
    out = _generate(system, message.strip()[:400], ROUTE_TOKENS)
    if not out:
        return None
    low = out.lower()
    # Exactly one known label, or nothing. Two means it did not choose.
    hits = [k for k in intents if re.search(r"\b%s\b" % re.escape(k), low)]
    if len(hits) == 1:
        log.info("assistant LLM routed to %s", hits[0])
        return hits[0]
    return None


# --------------------------------------------------------------------------
# job 2: answering in words

_LANGS = {
    "en": "English", "hi": "Hindi", "gu": "Gujarati", "mr": "Marathi",
    "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "kn": "Kannada",
    "ml": "Malayalam", "pa": "Punjabi", "or": "Odia", "as": "Assamese",
    "ur": "Urdu", "fr": "French", "de": "German",
}

_ANSWER_SYSTEM = (
    "You are the assistant inside Green Vision, a tree-planting planning tool "
    "for Indian cities. A planner is asking you a question about the place "
    "they are looking at.\n"
    "RULES, all of them absolute:\n"
    "1. Use ONLY the figures in FACTS. Never state a number that is not there.\n"
    "2. Never judge a figure yourself. Do not call the air clean, high, poor, "
    "safe or unhealthy on your own account. FACTS carries the official band "
    "for the air; quote that band and add nothing to it.\n"
    "3. If FACTS does not answer the question, say so in one sentence and "
    "suggest what the planner could open in the app instead.\n"
    "4. Answer the question that was asked. Do not list the other facts.\n"
    "5. At most 60 words, plain sentences, no headings, no bold, no bullets.\n"
    "6. Reply in {language}.\n\n"
    "FACTS\n{facts}"
)


def answer(message: str, facts: str, lang: str = "en") -> str | None:
    """A grounded free-text answer, or None to fall back.

    `facts` is assembled by the caller from what the engine has already
    measured. Everything the model is allowed to assert is in there; the
    prompt says so four different ways, because a small model will happily
    produce a confident PM2.5 reading out of nothing if the instruction is
    only implied.
    """
    if not message:
        return None
    system = _ANSWER_SYSTEM.format(language=_LANGS.get(lang, "English"),
                                   facts=facts or "(no readings yet)")
    out = _generate(system, message.strip()[:600], ANSWER_TOKENS)
    if not out:
        return None
    out = _dedupe(_clean(out))
    if len(out) < 12:
        # A reply that says nothing is worse than the deterministic apology,
        # which at least lists what the assistant can do.
        return None
    if not numbers_are_grounded(out, facts, message):
        # See numbers_are_grounded: this is the rule the whole feature rests
        # on, and the answer is thrown away rather than shown with a caveat.
        log.warning("assistant LLM answer rejected: ungrounded figure")
        return None
    return out


# Numbers, with the thousands separators and decimals a reply may carry.
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

def _nums(text: str) -> set[str]:
    """Every number in `text`, normalised so 4,462 and 4462 are the same
    thing and 14.2 and 14.20 are too."""
    out = set()
    for m in _NUM.finditer(text or ""):
        raw = m.group(0).replace(",", "")
        try:
            f = float(raw)
        except ValueError:
            continue
        out.add(("%g" % f))
    return out


def numbers_are_grounded(reply: str, facts: str, question: str = "") -> bool:
    """Does every figure in this reply come from the facts it was given?

    This is not a nicety, it is the condition under which a language model
    is allowed anywhere near this app at all.

    Measured, with the model that ships here: asked "how many storeys is the
    tallest building?" against a FACTS block that says nothing about
    storeys, it answered "The tallest building in Bopal has 15 storeys." No
    hedging, no marker, in the same voice as every true sentence around it.
    A planning tool whose numbers are its whole claim to be believed cannot
    put that in front of a planner, and no prompt reliably stops a
    1.5-billion-parameter model doing it.

    So the check is mechanical rather than persuasive: pull every number out
    of the reply and require it to appear in the facts, or in the question
    the reader asked. Anything else and the answer is discarded and the
    assistant apologises as it always did - which is a worse answer and an
    honest one.

    It is deliberately blunt. It will throw away a good reply that rounds
    22.4 to 22, and that is the right side to be wrong on: the cost of a
    lost paraphrase is a fallback message, and the cost of a kept invention
    is every other number in the app.
    """
    said = _nums(reply)
    if not said:
        return True
    known = _nums(facts) | _nums(question)
    # Percentages and units are written both ways round ("14.2%" / "14.2 %"),
    # and a figure may legitimately be quoted with its integer part only when
    # the facts carry the decimal - 755 mm is 755 mm however it is phrased.
    loose = set(known)
    for k in known:
        try:
            f = float(k)
        except ValueError:
            continue
        loose.add("%g" % round(f))
        if f >= 1000:                       # 2870000 quoted as 28.7 lakh etc.
            loose.add("%g" % (f / 100000))
            loose.add("%g" % (f / 10000000))
            loose.add("%g" % round(f / 100000, 1))
    return said.issubset(loose)


def _dedupe(text: str) -> str:
    """Drop sentences the model has already said.

    Greedy decoding on a small model repeats itself when it has run out of
    things to say and still has tokens left. The repetition penalty above
    reduces it; this removes what is left, because four identical sentences
    read as a broken app rather than as a chatty one.
    """
    seen, keep = set(), []
    for part in re.split(r"(?<=[.!?।])\s+", text or ""):
        k = re.sub(r"\W+", "", part.lower())
        if not k or k in seen:
            continue
        seen.add(k)
        keep.append(part.strip())
    return " ".join(keep).strip()


# Roughly where a chat bubble stops being an answer and becomes an essay.
ANSWER_WORDS = 70


def _clean(text: str) -> str:
    """Trim the artefacts a small instruct model leaves behind."""
    t = text.strip()
    for stop in ("<|im_end|>", "<|im_start|>", "<|endoftext|>"):
        t = t.split(stop)[0]
    # It has been trained to write a short essay, and produces one: an
    # answer, then "**Answer:**" restating it, then "**Suggestion:**". Cut
    # at the first of those markers - everything after it is a second go at
    # the same question.
    t = re.split(r"\*\*(?:Answer|Suggestion|Note|Conclusion)", t)[0]
    t = t.replace("**", "").strip()
    # It sometimes restates the question as a heading first.
    t = re.sub(r"^\s*(assistant|answer)\s*:\s*", "", t, flags=re.I)
    # An unterminated final sentence reads as a truncation bug; cut back to
    # the last full stop when the tail is clearly mid-sentence.
    if len(t) > 80 and t[-1] not in ".!?।":
        cut = max(t.rfind("."), t.rfind("।"), t.rfind("!"), t.rfind("?"))
        if cut > len(t) * 0.5:
            t = t[:cut + 1]
    # And a hard ceiling on length, at a sentence boundary. The prompt asks
    # for sixty words and is ignored about half the time; a chat bubble that
    # runs to a screen and a half is not an answer anybody reads.
    words = t.split()
    if len(words) > ANSWER_WORDS:
        trimmed = " ".join(words[:ANSWER_WORDS])
        cut = max(trimmed.rfind("."), trimmed.rfind("।"),
                  trimmed.rfind("!"), trimmed.rfind("?"))
        t = trimmed[:cut + 1] if cut > len(trimmed) * 0.4 else trimmed + "…"
    return t.strip()


def facts_from(context: dict[str, Any] | None, place: str | None,
               extra: dict[str, Any] | None = None) -> str:
    """Turn what the page already read into the FACTS block.

    Only measurements, each with its unit, one per line. Nothing derived,
    nothing rounded twice, and nothing at all when a reading is missing -
    an absent line is what teaches the model to say "I do not have that".

    The keys are the ones the studio actually sends (see actSnapshot in
    index.html); reading a key that is not there is how a model ends up
    told that the canopy is "None".
    """
    c = context or {}
    r = c.get("readings") if isinstance(c.get("readings"), dict) else {}
    census = c.get("census") if isinstance(c.get("census"), dict) else {}
    design = c.get("design") if isinstance(c.get("design"), dict) else {}
    traffic = c.get("traffic") if isinstance(c.get("traffic"), dict) else {}
    surr = c.get("surroundings") if isinstance(c.get("surroundings"), dict) else {}
    rows: list[str] = []

    def add(label: str, value: Any, unit: str = "") -> None:
        if value is None or value == "" or value == []:
            return
        rows.append("%s: %s%s" % (label, value, unit))

    add("place", place or c.get("place"))
    aoi = c.get("aoi") if isinstance(c.get("aoi"), dict) else {}
    if aoi.get("km2"):
        add("area being read", aoi.get("km2"), " km2")
    add("CPCB National AQI", r.get("cpcb_aqi"))
    add("CPCB band", r.get("cpcb_band"))
    add("pollutant driving the AQI", r.get("cpcb_driver"))
    add("US EPA AQI", r.get("aqi"))
    add("PM2.5", r.get("pm25"), " ug/m3")
    add("PM10", r.get("pm10"), " ug/m3")
    add("temperature now", r.get("temp"), " C")
    add("humidity", r.get("humidity"), "%")
    add("tree canopy over the area", r.get("canopy_pct"), "%")
    add("annual rainfall", r.get("rain_mm_yr"), " mm/yr")
    add("days over 40C", r.get("hot_days_yr"), " per year")

    add("buildings mapped in the area", census.get("buildings"))
    add("parks and gardens mapped", census.get("parks"))
    add("trees mapped", census.get("trees"))
    add("schools", census.get("schools"))
    add("hospitals and clinics", census.get("hospitals"))

    if traffic:
        add("traffic congestion across the network", traffic.get("network"), "/100")
        worst = traffic.get("worst") or []
        if worst and isinstance(worst[0], dict) and worst[0].get("name"):
            add("worst junction", "%s (%s/100)" % (worst[0]["name"], worst[0].get("score")))

    if design:
        add("design goal", design.get("goal"))
        add("plot drawn", design.get("plot_m2"), " m2")
        add("trees placed in the design", design.get("n_trees"))
        add("cost to build the design", design.get("total_cost"), " INR")
        add("land tenure assumed", design.get("land_tenure"))

    if surr:
        add("buildings surveyed around the plot", surr.get("buildings"))
        add("roads around the plot", surr.get("roads"))

    for k, v in (extra or {}).items():
        add(k, v)
    return "\n".join(rows)
