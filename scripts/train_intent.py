"""Train the assistant's intent classifier and export it for OpenVINO.

    python scripts/train_intent.py
    python scripts/train_intent.py --dim 4096 --out models/intent

WHAT THIS IS FOR. `assistant.classify()` decides what a message is asking for
with regular expressions first, then a per-language phrase table, then a
word-overlap fallback, and it answers "unknown" when none of them fire. That
is precise and it is honest, but it only knows phrasings somebody wrote down.
A reader who types "kitna kharcha aayega" or "wie teuer wird das" falls off
the end of the rules and is told the assistant did not understand.

This trains a small classifier on every phrase the fifteen dictionaries
already carry, so that tail is answered rather than refused. It does NOT
outrank the rules: `classify()` still returns the regex answer when one
matches, and only consults this model where it would otherwise have said
"unknown" — and only when the model is confident. A wrong confident guess is
worse than an honest "I did not understand that", so the threshold is
deliberately high and is measured below rather than assumed.

WHAT IT IS. Hashed character n-grams (see intent_features.py) into a
multinomial logistic regression: one matrix multiply, one bias, one softmax.
Exported as standard ONNX ops — MatMul/Add/Softmax — because OpenVINO's ONNX
frontend converts those, while the ai.onnx.ml LinearClassifier that skl2onnx
emits for a classifier it cannot (the same wall `onnx_trees.py` documents for
TreeEnsembleRegressor).

Scores are reported on messages held out by LANGUAGE-STRATIFIED split, so the
number below is accuracy on phrasings the model was not shown.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from greenplan.reasoning import intent_features as F  # noqa: E402

I18N = ROOT / "data" / "i18n"

# English seed phrases. The dictionaries carry the other languages' trigger
# words; English lives in the regexes, so it needs its own rows here or the
# model would be good at Hindi and poor at the language most people type in.
ENGLISH: dict[str, list[str]] = {
    "greet": ["hello", "hi there", "good morning", "thanks a lot", "hey"],
    "help": ["help", "what can you do", "how do i use this", "show me the commands",
             "i don't know what to ask"],
    "priority": ["where should the city plant first", "which areas are worst",
                 "rank the cells", "top 5 zones", "what is most urgent",
                 "where is planting most needed"],
    "empty_land": ["where is there empty land", "find bare ground near me",
                   "any vacant plots", "where can we plant", "how much plantable space"],
    "species": ["what should i plant here", "which trees suit this soil",
                "recommend some species", "what kind of tree grows here",
                "best trees for pollution"],
    "design": ["design a one hectare park", "lay out a garden for a school",
               "make me a green belt", "plan a park here", "draw a community garden"],
    "plant": ["plant 60 neem here", "add some trees", "put twenty saplings on the plot",
              "place shrubs along the edge"],
    "carbon": ["how much co2 will this absorb", "carbon sequestration of these trees",
               "what is the carbon benefit"],
    "budget": ["what can i do with 5 lakh", "budget of 25 lakh",
               "i have ten lakh rupees to spend"],
    "cost": ["how much would that cost", "what is the price of this design",
             "is it expensive", "give me an estimate", "kitna kharcha aayega"],
    "heat": ["how hot does it get here", "will trees cool this street",
             "how many days above 40", "does this reduce temperature"],
    "air": ["how is the air here", "what is the aqi", "is it polluted",
            "pm2.5 levels", "air quality of this area"],
    "canopy": ["how green is this area", "what is the tree cover",
               "canopy percentage here", "how many trees are there"],
    "water": ["how much rain does this get", "will it need irrigation",
              "is this a dry area", "rainfall here"],
    "soil": ["what is the soil like", "soil ph of this cell", "is the ground clayey"],
    "traffic": ["show me the traffic", "where are the bottlenecks",
                "how congested is this road"],
    "timing": ["when should i plant", "which month is best for planting",
               "is monsoon the right time"],
    "survival": ["how many will survive", "what is the survival rate",
                 "will these trees die"],
    "maintenance": ["who will water these", "what is the upkeep",
                    "how much maintenance does it need"],
    "people": ["how many people live here", "what is the population",
               "how many residents benefit"],
    "sources": ["where does this data come from", "how accurate is this",
                "what are your sources", "can i trust these numbers"],
    "review": ["review my design", "how good is this plan", "what is wrong with it",
               "score my park"],
    "project": ["project it 25 years", "what will it look like in future",
                "how does it hold up over time"],
    "compare": ["compare bopal and naranpura", "which is better of the two",
                "bopal versus vastral"],
    "goto": ["show me bopal", "take me to sabarmati riverfront", "go to 23.03, 72.58",
             "find vastral on the map"],
    "view": ["switch to satellite view", "show the green layer", "open the street map"],
    "report": ["what's here", "tell me about this area", "give me the report",
               "what is this place like"],
    "land": ["what is the land worth", "who owns this plot", "circle rate here",
             "cost of acquiring this land"],
}


def rows() -> tuple[list[str], list[str], list[str]]:
    """(message, intent, language) for everything the product already knows."""
    msgs, labels, langs = [], [], []
    for intent, phrases in ENGLISH.items():
        for p in phrases:
            msgs.append(p); labels.append(intent); langs.append("en")
    for path in sorted(I18N.glob("*.json")):
        if path.stem in ("index", "en"):
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        for intent, phrases in (d.get("nlu") or {}).items():
            for p in phrases if isinstance(phrases, list) else [phrases]:
                msgs.append(p); labels.append(intent); langs.append(path.stem)
    return msgs, labels, langs


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def train(X: np.ndarray, y: np.ndarray, n_classes: int, C: float = 20.0):
    """Multinomial logistic regression, fitted with L-BFGS.

    A hand-rolled gradient descent was tried first and reached 23% held-out
    accuracy — not because the model was wrong but because it had not
    converged: 4096 hashed features against a few hundred short phrases needs
    a real optimiser, not 400 fixed steps. sklearn is a TRAINING dependency
    only; what ships is the weight matrix below, exported to ONNX, so the
    engine still needs nothing but numpy and OpenVINO at runtime.

    C is loose on purpose. The phrases are short and the classes many, so the
    useful signal is in rare n-grams that heavy regularisation flattens.
    """
    from sklearn.linear_model import LogisticRegression

    clf = LogisticRegression(C=C, max_iter=2000)   # multinomial by default in sklearn 1.9
    clf.fit(X, y)
    # sklearn keeps (classes, features); the ONNX graph multiplies the other
    # way round, and a class the split never saw must still have a column.
    W = np.zeros((X.shape[1], n_classes), dtype=np.float32)
    b = np.zeros(n_classes, dtype=np.float32)
    for j, c in enumerate(clf.classes_):
        W[:, int(c)] = clf.coef_[j]
        b[int(c)] = clf.intercept_[j]
    return W, b


def to_onnx(W: np.ndarray, b: np.ndarray, path: Path) -> None:
    """MatMul -> Add -> Softmax, in standard ops only."""
    from onnx import TensorProto, helper, numpy_helper, save

    w = numpy_helper.from_array(W.astype(np.float32), "W")
    bb = numpy_helper.from_array(b.astype(np.float32), "b")
    x = helper.make_tensor_value_info("features", TensorProto.FLOAT, [1, W.shape[0]])
    out = helper.make_tensor_value_info("probabilities", TensorProto.FLOAT, [1, W.shape[1]])
    graph = helper.make_graph(
        [helper.make_node("MatMul", ["features", "W"], ["z0"]),
         helper.make_node("Add", ["z0", "b"], ["z"]),
         helper.make_node("Softmax", ["z"], ["probabilities"], axis=1)],
        "gv_intent", [x], [out], [w, bb])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9      # OpenVINO 2026.4 reads up to IR 10; 9 is safe
    save(model, str(path))


def main() -> int:
    ap = argparse.ArgumentParser(description="Train the assistant intent classifier")
    ap.add_argument("--out", default="models/intent")
    ap.add_argument("--dim", type=int, default=F.DIM)
    ap.add_argument("--threshold", type=float, default=None,
                    help="confidence floor; default is measured, not guessed")
    args = ap.parse_args()

    msgs, labels, langs = rows()
    classes = sorted(set(labels))
    idx = {c: i for i, c in enumerate(classes)}
    print("%d phrases, %d intents, %d languages"
          % (len(msgs), len(classes), len(set(langs))))

    # Hold out a quarter of every (language, intent) group, so the score is
    # accuracy on phrasings this model was not shown rather than on its own
    # training rows.
    rnd = random.Random(7)
    groups: dict[tuple[str, str], list[int]] = {}
    for i, (l, g) in enumerate(zip(labels, langs)):
        groups.setdefault((g, l), []).append(i)
    test: set[int] = set()
    for key, ids in groups.items():
        if len(ids) >= 3:
            test.update(rnd.sample(ids, max(1, len(ids) // 4)))
    train_ids = [i for i in range(len(msgs)) if i not in test]
    test_ids = sorted(test)
    print("train %d / held out %d" % (len(train_ids), len(test_ids)))

    X = F.matrix(msgs, args.dim)
    y = np.array([idx[l] for l in labels])

    W, b = train(X[train_ids], y[train_ids], len(classes))
    P = softmax(X[test_ids] @ W + b)
    pred = P.argmax(axis=1)
    conf = P.max(axis=1)
    truth = y[test_ids]
    acc = float((pred == truth).mean())
    print("\nheld-out accuracy: %.1f%%  (%d of %d)"
          % (100 * acc, int((pred == truth).sum()), len(truth)))

    # Pick the confidence floor from the data: the lowest threshold at which
    # what the model still answers is right at least 90% of the time. An
    # assistant that guesses confidently and wrongly is worse than one that
    # says it did not understand, so this is measured, not chosen.
    chosen, rows_out = args.threshold, []
    for t in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60]:
        keep = conf >= t
        if not keep.any():
            continue
        prec = float((pred[keep] == truth[keep]).mean())
        cover = float(keep.mean())
        rows_out.append((t, prec, cover))
        if chosen is None and prec >= 0.90:
            chosen = t
    print("\n  threshold   correct-when-answered   answered")
    for t, prec, cover in rows_out:
        print("     %.2f            %5.1f%%             %5.1f%%"
              % (t, 100 * prec, 100 * cover))
    if chosen is None:
        chosen = 0.50
        print("\nno threshold reached 90% - defaulting to %.2f" % chosen)
    print("\nchosen threshold: %.2f" % chosen)

    # Retrain on everything for the shipped weights: the split above exists to
    # produce an honest number, not to throw away a quarter of the data.
    W, b = train(X, y, len(classes))

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    to_onnx(W, b, out / "intent.onnx")
    (out / "meta.json").write_text(json.dumps({
        "classes": classes,
        "dim": args.dim,
        "ngrams": list(F.NGRAMS),
        "threshold": chosen,
        "held_out_accuracy": round(acc, 4),
        "n_train_phrases": len(msgs),
        "languages": sorted(set(langs)),
        "note": ("Consulted only where the rule-based classifier would return "
                 "'unknown'. Never overrides a regex match."),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print("wrote %s" % (out / "intent.onnx"))

    # Prove it through OpenVINO, the way it will actually run.
    try:
        import openvino as ov
        core = ov.Core()
        cm = core.compile_model(core.read_model(str(out / "intent.onnx")), "CPU")
        probe = F.vector("kitna kharcha aayega", args.dim)[None, :]
        r = list(cm(probe).values())[0]
        print("OpenVINO check: '%s' -> %s (%.2f)"
              % ("kitna kharcha aayega", classes[int(r.argmax())], float(r.max())))
    except Exception as exc:
        print("OpenVINO check skipped: %s" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
