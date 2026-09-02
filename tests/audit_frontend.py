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

# --- 8. read before it exists ----------------------------------------------
# `const` and `let` are NOT hoisted: reading one above its declaration is a
# ReferenceError in the temporal dead zone. In a single 11,000-line script this
# is easy to do by accident and hard to see, because it only throws when the
# code path that reads it actually runs - GV.rainGapNote took the whole page
# down at load, while SITE_SEARCH_R sat quiet until someone asked the studio to
# site a plot and got "SITE_SEARCH_R is not defined".
#
# Only SCREAMING_CASE module constants are checked. Lower-case names are
# overwhelmingly locals and parameters that happen to share a spelling with a
# module constant, and flagging those buries the real finding in noise.


def code_only(src):
    r"""`src` with comments, string bodies and regex literals blanked,
    positions preserved.

    One linear scan. The obvious regex - alternating comment and quoted-string
    patterns with re.S - backtracks catastrophically on a file this size and
    had to be killed after five minutes; and because offsets must survive for
    the position comparison below, blanking in place beats stripping anyway.

    Regex literals have to be recognised, not just strings. `replace(/[\[\]']/g, "")`
    contains an apostrophe, and a scanner that does not know it is inside a
    regex reads that as the start of a string, swallows everything to the next
    apostrophe, and loses whatever braces were in between - which threw the
    brace depth off and made a read inside a function look like a top-level
    one. A `/` starts a regex when the last significant character before it is
    one that cannot end an expression.
    """
    out = list(src)
    i, n = 0, len(src)
    prev_sig = ""          # last significant char seen, for the regex test
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
            continue
        if c == "/" and (prev_sig == "" or prev_sig in "(,=:[!&|?{};+-*%~^<>"):
            # A regex literal. Blank its body; a newline inside one is a
            # syntax error in JS, so stopping at one is also a safety net
            # against mistaking a division for a regex.
            j = i + 1
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    while j < n and src[j] != "]" and src[j] != "\n":
                        if src[j] == "\\":
                            j += 1
                        j += 1
                if src[j] == "/":
                    break
                j += 1
            if j < n and src[j] == "/":
                for k in range(i, j + 1):
                    if src[k] != "\n":
                        out[k] = " "
                i = j + 1
                prev_sig = "0"          # a regex is a value
                continue
            # not a terminated regex: fall through and treat as division
        if c in "'\"`":
            quote = c
            out[i] = " "
            i += 1
            while i < n:
                if src[i] == "\\":
                    out[i] = " "
                    if i + 1 < n and src[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if src[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            prev_sig = "0"
            continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return "".join(out)


def top_level_spans(code):
    """Byte ranges of `code` that sit at brace depth 0.

    A read inside a function body is not a temporal-dead-zone error: the body
    does not run until it is called, by which time the whole script has
    executed and every top-level const exists. Only a read that happens WHILE
    the script is executing - depth 0 - can hit the dead zone. Without this
    distinction the check reports every forward reference in the file, which
    is most of them, and buries the one that matters.
    """
    spans, depth, run_start = [], 0, 0
    for i, c in enumerate(code):
        if c == "{":
            if depth == 0:
                spans.append((run_start, i))
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                run_start = i + 1
            if depth < 0:            # defensive: unbalanced input
                depth = 0
    spans.append((run_start, len(code)))
    return spans


for bi, blk in enumerate(scripts):
    code = code_only(blk)
    spans = top_level_spans(code)
    decl_at = {}
    for m in re.finditer(r"^(?:const|let)\s+([A-Z][A-Z0-9_]{2,})\s*=", code, re.M):
        decl_at.setdefault(m.group(1), m.start())
    for name, at in sorted(decl_at.items(), key=lambda kv: kv[1]):
        pat = re.compile(r"(?<![.\w$])" + re.escape(name) + r"(?![\w$])")
        hit = None
        for a, b in spans:
            if a >= at:
                break              # this span starts after the declaration
            # Search only the part of the span that precedes the declaration:
            # breaking on the span that CONTAINS it skipped the most likely
            # place for the bug to be, which is a line or two above.
            m = pat.search(code, a, min(b, at))
            if m:
                hit = m.start()
                break
        if hit is not None:
            line = code.count("\n", 0, hit) + 1
            problems.append("script block %d reads %s at top level (line ~%d) before "
                            "its declaration (ReferenceError at load)"
                            % (bi, name, line))

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
