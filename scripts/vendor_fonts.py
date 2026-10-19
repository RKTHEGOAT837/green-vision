"""Bundle the studio's two web fonts so the app never waits on Google Fonts.

Reads vendor/fonts-google.css (the CSS Google serves a current Chrome), keeps
the latin and latin-ext subsets, downloads each variable-weight woff2 once and
writes vendor/fonts.css with the files embedded as data URIs.

    python scripts/vendor_fonts.py
"""
import base64
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "vendor" / "fonts-google.css").read_text(encoding="utf-8")

blocks = re.findall(r"/\* ([\w-]+) \*/\s*@font-face \{(.*?)\}", src, re.S)
seen = {}
rules = []
for subset, body in blocks:
    if subset not in ("latin", "latin-ext"):
        continue
    fam = re.search(r"font-family: '([^']+)'", body).group(1)
    url = re.search(r"url\((https://[^)]+\.woff2)\)", body).group(1)
    rng = re.search(r"unicode-range: ([^;]+);", body).group(1)
    weights = [int(w) for w in re.findall(r"font-weight: (\d+)", body)]
    key = (fam, url)
    if key in seen:
        seen[key]["lo"] = min(seen[key]["lo"], *weights)
        seen[key]["hi"] = max(seen[key]["hi"], *weights)
        continue
    seen[key] = {"fam": fam, "url": url, "range": rng, "lo": min(weights), "hi": max(weights)}

for v in seen.values():
    data = urllib.request.urlopen(v["url"], timeout=60).read()
    b64 = base64.b64encode(data).decode()
    rules.append(
        "@font-face{font-family:'%s';font-style:normal;font-weight:%d %d;font-display:swap;"
        "src:url(data:font/woff2;base64,%s) format('woff2');unicode-range:%s;}"
        % (v["fam"], v["lo"], v["hi"], b64, v["range"]))
    print("  %-8s %-9s %d-%d  %5.1f KB" % (v["fam"], "", v["lo"], v["hi"], len(data) / 1024))

(ROOT / "vendor" / "fonts.css").write_text(
    "/* Manrope and Sora (SIL Open Font License), latin + latin-ext, bundled by "
    "scripts/vendor_fonts.py so the studio does not wait on Google Fonts. */\n"
    + "\n".join(rules) + "\n", encoding="utf-8")
print("wrote vendor/fonts.css")
