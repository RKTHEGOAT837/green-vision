"""Build a studio patch: one file, one hash, one manifest.

WHAT A PATCH IS

The installer is 1.33 GB and almost none of it is the app - it is CPython,
a 1.5-billion-parameter model, and 3.2 million building footprints. The
part that actually changes when a bug is fixed is the studio: one HTML
file. This script bakes that file the same way the installer's copy is
baked, hashes it, and prints the manifest the admin portal needs.

    python scripts/make_patch.py --version 1.4.1

It writes the payload into the GitHub Pages repo, because Pages serves it
free and with a real content-type, and the Worker only ever stores the
URL and the hash. The hash is the whole security model: the app executes
this file, so it refuses anything whose bytes do not match.

PUBLISHING

  1. run this
  2. commit and push the Pages repo
  3. paste the manifest into the admin portal's Patch section

Nothing else has to happen. Installed copies pick it up at their next
start, download about a megabyte, verify it, and render from it.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where the Pages repo lives on this machine. Same repo the download page
# and the reviews page are served from.
PAGES = Path("C:/gvrel/docs")
PAGES_URL = "https://rkthegoat837.github.io/green-vision-releases"


def bake(out: Path) -> Path:
    """Produce the studio exactly as the installer does.

    Deliberately the SAME script build_static.py the release uses. A patch
    built a different way from the thing it replaces is a second source of
    truth, and the whole point of this channel is that it ships the same
    page sooner.
    """
    tmp = ROOT / "dist_patch"
    if tmp.exists():
        shutil.rmtree(tmp)
    cmd = [sys.executable, str(ROOT / "scripts" / "build_static.py"),
           "--out", str(tmp), "--keep-local-osm"]
    print("  baking:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit("build_static.py failed")
    src = tmp / "index.html"
    if not src.is_file():
        raise SystemExit("no index.html came out of the bake")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--version", required=True,
                    help="the studio's version, e.g. 1.4.1 - not the app's")
    ap.add_argument("--min-app", default="",
                    help="lowest app version this studio is safe on")
    ap.add_argument("--notes", default="", help="one line, shown to nobody but you")
    ap.add_argument("--pages", default=str(PAGES))
    ap.add_argument("--raw", action="store_true",
                    help="copy index.html straight through instead of baking")
    args = ap.parse_args()

    pages = Path(args.pages)
    if not pages.is_dir():
        raise SystemExit(f"{pages} is not there - point --pages at the Pages repo")

    name = f"studio-{args.version}.html"
    dest = pages / "patch" / name

    if args.raw:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "index.html", dest)
        print("  copied index.html straight through (--raw)")
    else:
        bake(dest)

    # LINE ENDINGS, WHICH ARE NOT A DETAIL HERE.
    #
    # The app hashes the bytes it downloads. Git on Windows checks files
    # out with CRLF and stores them with LF, and GitHub Pages serves what
    # is stored - so the file on disk hashed to one value and the file
    # people actually received hashed to another. Measured on the first
    # payload: 1,006,116 bytes locally, 986,918 bytes served, and two
    # different SHA-256s. Every app would have downloaded the patch,
    # rejected it, and downloaded it again at the next start, forever,
    # with nothing on screen to say why.
    #
    # So the payload is written LF-only and .gitattributes marks
    # patch/*.html as `-text` so nothing converts it back.
    data = dest.read_bytes().replace(b"\r\n", b"\n")
    dest.write_bytes(data)
    if not data.lstrip()[:15].lower().startswith(b"<!doctype html"):
        raise SystemExit("that is not an HTML document")
    sha = hashlib.sha256(data).hexdigest()

    print()
    print(f"  wrote {dest}  ({len(data) / 1024:.0f} KB)")
    print()
    print("  Paste this into the admin portal, Patch section:")
    print()
    print(f"    version   {args.version}")
    print(f"    url       {PAGES_URL}/patch/{name}")
    print(f"    sha256    {sha}")
    print(f"    bytes     {len(data)}")
    if args.min_app:
        print(f"    min_app   {args.min_app}")
    if args.notes:
        print(f"    notes     {args.notes}")
    print()
    print("  Then commit and push the Pages repo.")
    print()
    print("  The admin portal re-fetches that URL and re-hashes it before it")
    print("  saves anything, so a mismatch is caught there rather than by")
    print("  every installed copy. Wait for Pages to rebuild (a minute or so)")
    print("  before pressing Publish.")


if __name__ == "__main__":
    main()
