"""Assistant translation coverage, and the one error that must never ship.

The assistant renders every sentence from data/i18n/<code>.json, and its
strings carry placeholders — {pct}, {trees}, {km2} — that the Python side
fills with measured numbers. Two failure modes matter, and they are not
equally visible:

  MISSING KEY      the reply comes out in English. Ugly, obvious to the reader,
                   harmless to the meaning. i18n.t falls back per key by
                   design, so nothing breaks.

  PLACEHOLDER DRIFT   a translation that spells {trees} as {tree}, or drops
                   {pct}, or invents {count}. The format call then either
                   raises, or silently renders a sentence with a hole where a
                   measured number belongs. THAT is the one that puts a
                   half-finished claim in front of a planner, and it cannot be
                   caught by reading the translation unless you already know
                   what the English took.

So this reports coverage, and refuses drift. Coverage is a to-do list;
drift is a defect.

    python scripts/i18n_assistant.py
    python scripts/i18n_assistant.py --lang hi
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "data" / "i18n"

# str.format fields: {name}, {name:spec}, {name!r}. Bare {} and {{ }} are not
# used by this codebase and would be a bug in the English source, not here.
FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)[^{}]*\}")


def fields(text: str) -> set[str]:
    return set(FIELD.findall(text or ""))


def block(code: str) -> dict:
    p = I18N / f"{code}.json"
    if not p.is_file():
        return {}
    return (json.loads(p.read_text(encoding="utf-8")) or {}).get("assistant", {}) or {}


def languages() -> list[str]:
    idx = I18N / "index.json"
    if idx.is_file():
        try:
            raw = json.loads(idx.read_text(encoding="utf-8"))
            rows = raw.get("languages", raw) if isinstance(raw, dict) else raw
            return [r["code"] for r in rows
                    if isinstance(r, dict) and r.get("code") != "en"
                    and r.get("translated")]
        except Exception:
            pass
    return sorted(p.stem for p in I18N.glob("*.json") if p.stem not in {"en", "index"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lang")
    ap.add_argument("--list-missing", action="store_true",
                    help="print every missing key, not just a count")
    a = ap.parse_args()

    en = block("en")
    if not en:
        print("  en.json has no assistant block")
        return 1

    codes = [a.lang] if a.lang else languages()
    drift: list[str] = []

    print("  %d assistant strings in English" % len(en))
    print()
    for code in codes:
        d = block(code)
        if not d:
            print("  %-4s no dictionary" % code)
            continue
        missing = sorted(k for k in en if k not in d)
        bad = []
        for k, v in d.items():
            if k not in en:
                continue                      # a key English dropped; harmless
            want, got = fields(en[k]), fields(v)
            if want != got:
                bad.append((k, sorted(want - got), sorted(got - want)))
        pct = 100 * (len(en) - len(missing)) / max(1, len(en))
        print("  %-4s %3.0f%% covered   %3d missing   %d placeholder problem(s)"
              % (code, pct, len(missing), len(bad)))
        if a.list_missing:
            for k in missing:
                print("        MISSING  %s" % k)
        for k, lost, extra in bad:
            drift.append(code + ":" + k)
            print("        DRIFT    %s" % k)
            if lost:
                print("                 dropped: %s" % ", ".join("{%s}" % x for x in lost))
            if extra:
                print("                 invented: %s" % ", ".join("{%s}" % x for x in extra))
    print()
    if drift:
        print("  %d placeholder problem(s). Each one renders a sentence with a hole" % len(drift))
        print("  where a measured number belongs, or raises at format time.")
        return 1
    print("  No placeholder drift. Missing keys fall back to English per key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
