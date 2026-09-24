"""The trained intent classifier at runtime, through the OpenVINO runtime.

Loaded once, lazily, and never on the critical path of a message the rules
already understood. If the weights are absent, OpenVINO is absent, or
anything at all goes wrong, `predict()` returns None and the assistant
behaves exactly as it did before this file existed — an assistant that dies
because an optional model is missing is worse than one without the model.

The contract with `assistant.classify()` is deliberately narrow:

  * it is asked only where the rules would have answered "unknown";
  * it may return an intent only above the threshold measured at training
    time and recorded in meta.json, never a threshold chosen here;
  * it never overrides a regular expression, a native phrase table hit, or
    the word-overlap fallback.

So the worst this can do is turn "I did not understand that" into a wrong
answer, and the threshold is set from held-out data to keep that rate low —
90% of the messages it chooses to answer were right in the measurement the
training script prints.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MODEL: Any = None          # compiled network, False once loading has failed
_META: dict[str, Any] = {}


def _dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "models" / "intent"


def _load() -> Any:
    global _MODEL, _META
    if _MODEL is not None:
        return _MODEL
    d = _dir()
    try:
        meta_path = d / "meta.json"
        onnx_path = d / "intent.onnx"
        if not (meta_path.exists() and onnx_path.exists()):
            _MODEL = False
            return _MODEL
        _META = json.loads(meta_path.read_text(encoding="utf-8"))
        import openvino as ov  # noqa: PLC0415
        core = ov.Core()
        _MODEL = core.compile_model(core.read_model(str(onnx_path)), "CPU")
        log.info("intent classifier: %d intents, threshold %.2f, "
                 "%.0f%% held-out accuracy, compiled by OpenVINO",
                 len(_META.get("classes", [])), _META.get("threshold", 0.5),
                 100 * float(_META.get("held_out_accuracy", 0)))
    except Exception as exc:
        log.info("intent classifier unavailable (%s) - the rules are unaffected",
                 str(exc).split("\n")[0][:120])
        _MODEL = False
    return _MODEL


def available() -> bool:
    return bool(_load())


def info() -> dict[str, Any]:
    """What health() reports: what is loaded, and how good it measured."""
    if not _load():
        return {"loaded": False}
    return {"loaded": True,
            "intents": len(_META.get("classes", [])),
            "threshold": _META.get("threshold"),
            "held_out_accuracy": _META.get("held_out_accuracy"),
            "languages": _META.get("languages"),
            "runtime": "openvino"}


def predict(msg: str) -> tuple[str, float] | None:
    """(intent, confidence) when the model is sure enough, else None."""
    model = _load()
    if not model:
        return None
    try:
        from . import intent_features as F  # noqa: PLC0415

        x = F.vector(msg, int(_META.get("dim", F.DIM)))
        if not x.any():
            return None
        probs = list(model(x[None, :]).values())[0][0]
        i = int(probs.argmax())
        conf = float(probs[i])
        if conf < float(_META.get("threshold", 0.5)):
            return None
        return _META["classes"][i], conf
    except Exception as exc:
        log.debug("intent prediction failed: %s", exc)
        return None
