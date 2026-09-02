"""Static audit of index.html: the wiring a browser only fails on at click time.

Every finding here is a thing that looks fine until someone presses a button:
a handler bound to an id that no longer exists, two elements answering to the
same id so the second is unreachable, a function called from an event handler
that was renamed. None of these throw at load; they throw, or silently do
nothing, on the interaction.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / "index.html").read_text(encoding="utf-8")

scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)
js = "\n".join(scripts)
markup = re.sub(r"<script(?![^>]*src=)[^>]*>.*?</script>", "", html, flags=re.S)

problems = []
notes = []

# --- 1. duplicate ids in the static markup ---------------------------------
static_ids = re.findall(r'\bid="([^"]+)"', markup)
seen, dupes = set(), set()
for i in static_ids:
    if i in seen:
        dupes.add(i)
    seen.add(i)
if dupes:
    problems.append("duplicate id(s) in markup: %s" % ", ".join(sorted(dupes)))

# ids the JS creates at runtime (template literals, innerHTML, createElement)
dynamic_ids = set(re.findall(r"""id=\\?["']([A-Za-z][\w-]*)\\?["']""", js))
dynamic_ids |= set(re.findall(r"""\.id\s*=\s*["']([\w-]+)["']""", js))
all_ids = set(static_ids) | dynamic_ids

# --- 2. every id the JS looks up should exist somewhere --------------------
looked_up = set()
for pat in (r"""getElementById\(\s*["']([\w-]+)["']""",
            r"""\$\(\s*["']#([\w-]+)["']\s*\)""",
            r"""querySelector\(\s*["']#([\w-]+)["']""",
            r"""querySelectorAll\(\s*["']#([\w-]+)["']"""):
    looked_up |= set(re.findall(pat, js))

missing = sorted(i for i in looked_up if i not in all_ids)
if missing:
    problems.append("JS looks up id(s) that are never created: %s" % ", ".join(missing))

unused = sorted(i for i in static_ids if i not in looked_up and i not in dynamic_ids)
if unused:
    notes.append("%d static id(s) never looked up (may be CSS-only): %s"
                 % (len(unused), ", ".join(unused[:12]) + ("…" if len(unused) > 12 else "")))

# --- 3. inline handlers must name a function that exists -------------------
inline = set(re.findall(r'on(?:click|change|input|submit)="([A-Za-z_$][\w$]*)\s*\(', markup))
defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", js))
defined |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", js))
defined |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function", js))
bad_inline = sorted(f for f in inline if f not in defined)
if bad_inline:
    problems.append("inline handler(s) call undefined function(s): %s" % ", ".join(bad_inline))

# --- 4. functions called from JS that are never defined anywhere -----------
BUILTINS = set("""if for while switch catch return typeof function of in new delete void
throw do else try finally class extends super this await yield case break continue
Math JSON Object Array String Number Boolean Date RegExp Promise Set Map WeakMap
parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent setTimeout
clearTimeout setInterval clearInterval requestAnimationFrame fetch console alert
document window navigator localStorage sessionStorage URL Blob FormData Image
Error TypeError RangeError Intl Symbol BigInt Proxy Reflect structuredClone
queueMicrotask AbortController Event CustomEvent IntersectionObserver
ResizeObserver MutationObserver TextDecoder TextEncoder atob btoa crypto
performance matchMedia getComputedStyle CSS L h3 XMLHttpRequest FileReader
Uint8Array Float32Array Int32Array ArrayBuffer DataView WeakSet Function
require module exports process globalThis undefined null true false NaN Infinity""".split())
called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]{2,})\s*\(", js))
undefined_calls = sorted(c for c in called
                         if c not in defined and c not in BUILTINS
                         and not c[0].isupper())
# Filter out obvious locals/params by requiring the name to be called 2+ times
counts = {c: len(re.findall(r"(?<![.\w$])%s\s*\(" % re.escape(c), js)) for c in undefined_calls}
suspicious = sorted(c for c, n in counts.items() if n >= 2)
if suspicious:
    notes.append("called but not defined at top level (likely locals/imports): %s"
                 % ", ".join(suspicious[:20]))

# --- 5. the JS must actually parse -----------------------------------------
# A hand-rolled brace counter cannot survive template literals, regex literals
# or nested quotes - it reported this file unbalanced when it parses perfectly.
# Node's parser is the authority, so ask it rather than approximate it.
node = shutil.which("node")
if not node:
    notes.append("node not on PATH; JS was not parsed (only pattern-checked)")
else:
    with tempfile.TemporaryDirectory() as td:
        for i, blk in enumerate(scripts):
            f = Path(td) / ("blk%d.js" % i)
            f.write_text(blk, encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            if r.returncode != 0:
                problems.append("script block %d does not parse: %s"
                                % (i, (r.stderr or "").strip().splitlines()[-1][:160]))

# --- 6. CSS: rules whose transform can be shadowed by an equal-specificity
#        rule declared later. This is the exact bug the dock rail had.
css_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
css = "\n".join(css_blocks)
rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
by_target = {}
for i, (sel, body) in enumerate(rules):
    sel = sel.strip()
    if "transform" not in body:
        continue
    m = re.match(r"^(#[\w-]+)((?:\.[\w-]+)*)$", sel)
    if not m:
        continue
    by_target.setdefault(m.group(1), []).append((i, sel, m.group(2).count(".")))
for target, entries in by_target.items():
    for a in range(len(entries)):
        for b in range(len(entries)):
            ia, sa, na = entries[a]
            ib, sb, nb = entries[b]
            if ia < ib and na == nb and na > 0 and sa != sb:
                notes.append("CSS: %s and %s have equal specificity and both set "
                             "transform; the later (%s) always wins when both apply"
                             % (sa, sb, sb))

# --- 7. one namespace, one owner -------------------------------------------
# This is a single 11,000-line file with ~20 module namespaces, so two sections
# can reach for the same short name years apart. A repeated top-level `const`
# in ONE script block is a hard SyntaxError: the whole block stops parsing and
# every feature in the app dies at once, which looks nothing like a naming
# problem when you meet it. (GVL was taken by localisation; a layout module
# claimed it and took the entire page down.)
#
# Per block, deliberately. Separate <script> elements are separate scopes, so
# the same name in block 0 and block 1 is legal - checking the concatenation
# reported $ , clamp and round as collisions in a file that parses perfectly.
for bi, blk in enumerate(scripts):
    decls = {}
    for m in re.finditer(r"^(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=", blk, re.M):
        decls.setdefault(m.group(1), []).append(m.start())
    repeated = sorted(n for n, at in decls.items() if len(at) > 1)
    if repeated:
        problems.append("script block %d declares the same top-level const/let "
                        "twice (SyntaxError, kills the block): %s"
                        % (bi, ", ".join(repeated)))

print("=" * 70)
print("  STATIC AUDIT: index.html  (%d lines, %d script blocks)"
      % (html.count("\n") + 1, len(scripts)))
print("=" * 70)
if problems:
    print("\n  PROBLEMS")
    for p in problems:
        print("   XX %s" % p)
else:
    print("\n  no wiring problems found")
if notes:
    print("\n  NOTES")
    for n in notes:
        print("   -- %s" % n)
print()
sys.exit(1 if problems else 0)
