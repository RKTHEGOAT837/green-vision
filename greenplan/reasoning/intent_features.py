"""The feature map the intent classifier uses, shared by training and runtime.

Why hashed character n-grams rather than words: this assistant is asked
questions in fifteen languages, four scripts and a lot of transliteration
("kitne ped", "kitne पेड़", "how many ped"). A word vocabulary built at
training time cannot hold a spelling nobody typed yet; character n-grams
degrade gracefully into one, because "प्राथमिकता" and "prathmikta" share
nothing as words and a great deal as trigrams once transliteration is in the
training set at all.

Why hashing rather than a fitted vocabulary: the runtime then needs no
vocabulary file, no sklearn, and no tokenizer — just this function and numpy,
both of which the shipped engine already has. The model that sits on top is a
plain matrix multiply, which is exactly the shape OpenVINO compiles best and
the shape the forecaster already ships in.

The hash is FNV-1a over UTF-8 bytes: sixteen lines, no dependency, and
identical on every platform, which a language's built-in `hash()` is not.
"""

from __future__ import annotations

import re
import unicodedata

import numpy as np

DIM = 8192          # hash buckets; the classifier's input width (measured: 8192 beat 4096 and 16384 on held-out precision)
NGRAMS = (2, 5)     # character n-gram range, inclusive

_SPACE = re.compile(r"\s+")


def _fnv1a(text: str) -> int:
    """32-bit FNV-1a. Stable across platforms and Python versions, which
    `hash()` deliberately is not (PYTHONHASHSEED randomises str hashing, so a
    model trained in one process would mis-index in the next)."""
    h = 0x811C9DC5
    for b in text.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def normalise(msg: str) -> str:
    """Lowercase, strip accents that carry no meaning here, collapse space.

    NFKC first so that the same character typed two ways — and Indic digits
    against ASCII ones — lands in one bucket rather than two.
    """
    s = unicodedata.normalize("NFKC", str(msg or "")).lower().strip()
    return " " + _SPACE.sub(" ", s) + " "


def vector(msg: str, dim: int = DIM) -> np.ndarray:
    """One L2-normalised feature vector for a message.

    Counts are damped with log1p: a question that repeats a word is not ten
    times more about it, and without damping long messages dominate the
    decision purely by length.
    """
    s = normalise(msg)
    v = np.zeros(dim, dtype=np.float32)
    lo, hi = NGRAMS
    for n in range(lo, hi + 1):
        if len(s) < n:
            break
        for i in range(len(s) - n + 1):
            v[_fnv1a(s[i:i + n]) % dim] += 1.0
    if not v.any():
        return v
    v = np.log1p(v)
    return (v / np.linalg.norm(v)).astype(np.float32)


def matrix(messages, dim: int = DIM) -> np.ndarray:
    """A feature matrix for many messages, one row each."""
    out = np.zeros((len(messages), dim), dtype=np.float32)
    for i, m in enumerate(messages):
        out[i] = vector(m, dim)
    return out
