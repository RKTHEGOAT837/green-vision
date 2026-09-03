"""Which English strings in index.html have no translation, and which
translations no longer match any English string.

Translation here works by walking text nodes and matching the English source
against a key in data/i18n/<code>.json. That makes the English copy the key,
and it means an ordinary edit to a sentence silently un-translates it in every
language at once: nothing errors, nothing logs, the string simply comes out in
English and only a reader of that language notices.

That is exactly what happened to the intro paragraph. The dictionary carries

    "... draws a 100 km2 perimeter around that point, reads only what's
     inside it, and opens the Studio - where you can design a park or oasis,
     cost it, and see how it holds up over 25 years."

translated into all thirteen languages, while the page had been reworded to

    "... draws a 100 km2 perimeter around that point, and reads only what
     falls inside it."

so the lookup missed and the sentence stayed English everywhere.

Two directions matter and this reports both:

  MISSING   the page's STATIC markup says it and no dictionary has it, so it
            shows in English for everyone. This half is reliable.

A deliberate limitation: most of this interface is built by JavaScript, and
this script reads the HTML source rather than a live DOM, so it only sees the
static markup. That makes the missing list a floor, never a ceiling - and it
is why this does NOT report "orphaned" keys. A dictionary entry with no match
in static markup is usually just a string that JavaScript writes, and calling
those orphaned would bury three real findings under a hundred false ones.

    python scripts/i18n_gaps.py
    python scripts/i18n_gaps.py --lang hi
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "data" / "i18n"

# Strings the interface deliberately never translates: proper nouns, map
# attributions, units, and anything carrying a botanical or H3 identifier.
SKIP = re.compile(
    r"^(GreenVision|Green Vision|Esri|OpenStreetMap|Leaflet|Maxar|Earthstar|"
    r"MODIS|NDVI|AQI|CPCB|H3|ERA5|IMD|SoilGrids|Open-Meteo|km|m|ha|INR)\b"
    r"|^[\s\d.,%°+\-/()]*$"
    r"|©"
)


def page_strings() -> set[str]:
    """Every English string the page marks as translatable.

    The runtime walks text nodes; this reads the same text out of the source
    instead, taking anything inside a tag that is long enough to be a sentence
    and is not inside a script, a style, or an element opted out with
    data-no-i18n.
    """
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    out: set[str] = set()
    for m in re.finditer(r">([^<>{}$]{12,})<", html):
        # Decode entities and normalise whitespace, because the runtime
        # compares against a DOM text node, not against the source. The source
        # says `100&nbsp;km&sup2;`; the DOM says `100 km²`; the dictionary
        # says `100 km²`. Comparing raw source reported two strings as
        # untranslated that the browser matches perfectly, and hid the one
        # real difference - the non-breaking space - behind them.
        t = html_mod.unescape(m.group(1))
        t = " ".join(t.replace(" ", " ").split())
        if not t or SKIP.search(t):
            continue
        if not re.search(r"[A-Za-z]{3,}", t):
            continue
        out.add(t)
    return out


def dict_for(code: str) -> dict:
    p = I18N / f"{code}.json"
    if not p.is_file():
        return {}
    return (json.loads(p.read_text(encoding="utf-8")) or {}).get("ui", {}) or {}


def languages() -> list[str]:
    idx = I18N / "index.json"
    if idx.is_file():
        try:
            rows = json.loads(idx.read_text(encoding="utf-8"))
            rows = rows.get("languages", rows) if isinstance(rows, dict) else rows
            codes = [r["code"] if isinstance(r, dict) else r for r in rows]
            return [c for c in codes if c != "en"]
        except Exception:
            pass
    return sorted(p.stem for p in I18N.glob("*.json")
                  if p.stem not in {"en", "index"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lang", help="check one language instead of all")
    ap.add_argument("--quiet", action="store_true", help="counts only")
    a = ap.parse_args()

    page = page_strings()
    codes = [a.lang] if a.lang else languages()
    worst = 0

    print("  %d translatable strings found in index.html" % len(page))
    print()
    for code in codes:
        d = dict_for(code)
        if not d:
            print("  %-4s no dictionary" % code)
            continue
        missing = sorted(s for s in page if s not in d)
        pct = 100 * (len(page) - len(missing)) / max(1, len(page))
        worst = max(worst, len(missing))
        print("  %-4s %3.0f%% of static strings covered   %3d missing   (%d keys in dictionary)"
              % (code, pct, len(missing), len(d)))
        if not a.quiet and missing:
            for s in missing[:6]:
                print("        MISSING  %s" % (s[:88] + ("…" if len(s) > 88 else "")))
            if len(missing) > 6:
                print("        ... and %d more" % (len(missing) - 6))
    print()
    print("  A missing string shows in English in that language. When one appears")
    print("  after an edit, the dictionary usually still holds the OLD wording")
    print("  translated - move it to the new key rather than retranslating.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
