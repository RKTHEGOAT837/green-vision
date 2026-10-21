#!/bin/bash
# Install or update Green Vision on a Mac, in one command:
#
#   curl -fsSL https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/install-mac.sh | bash
#
# Options (pass after `bash -s --`):
#   --dir <folder>   install somewhere other than /Applications
#   --no-open        install but do not launch it
#   --keep-zip       leave the downloaded zip in ~/Downloads
#   --force          download and reinstall even if the newest is installed
#
#   curl -fsSL .../install-mac.sh | bash -s -- --dir ~/Applications --no-open
#
# What it does, in order: checks this is an Apple Silicon Mac with room for
# the app, finds the newest macOS release and the copy already installed.
#   not installed     -> downloads, checks its SHA-256, installs
#   older version     -> downloads, checks it, replaces the installed copy
#   already newest    -> skips the 1.1 GB download
# Then, in every case, brings the studio up to the newest published patch
# (update-studio-mac.sh: downloaded, hash-checked, staged the way the app
# does it) and opens the app. Saved projects, account and settings are kept.
# Run the same command any time to update.
#
# Written for the bash 3.2 and BSD tools every Mac ships with: no Homebrew,
# no Python, no Xcode needed.

set -euo pipefail

# EVERYTHING IS INSIDE main(), CALLED ON THE LAST LINE.
#
# Run as `curl ... | bash`, bash reads this script from the same pipe the
# commands inherit as their input. Any command that reads input would
# swallow the rest of the script. A function has to be read in full before
# it can be called, and main's input is /dev/null, so nothing can.
#
# KEEP THIS FILE ASCII OUTSIDE COMMENTS. A Mac's bash 3.2 in the C locale
# reads the first byte of a character like an ellipsis as part of the
# variable name before it: "$DEST..." with a real ellipsis became an
# unbound variable and killed the install right after unpacking.
main() {


REPO="RKTHEGOAT837/green-vision"
APP_NAME="Green Vision"
DEST="/Applications"
OPEN_AFTER=1
KEEP_ZIP=0
FORCE="${GV_FORCE:-0}"
STUDIO_URL="https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/update-studio-mac.sh"
NEED_GB=4            # 1.1 GB zip + about 2.5 GB unpacked, with room to spare

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)      DEST="${2:?--dir needs a folder}"; shift 2 ;;
    --no-open)  OPEN_AFTER=0; shift ;;
    --keep-zip) KEEP_ZIP=1; shift ;;
    --force)    FORCE=1; shift ;;
    -h|--help)  sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# 0 when version $1 >= version $2, compared number by number (1.10 > 1.9).
# awk, because the BSD sort on older Macs has no -V.
ver_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); m = split(b, y, "."); k = (n > m ? n : m)
    for (i = 1; i <= k; i++) { p = x[i] + 0; q = y[i] + 0
      if (p > q) exit 0; if (p < q) exit 1 }
    exit 0 }'
}

# The studio patch step. The update script stages the newest studio exactly
# as the app does and restarts the app so it switches over. A failure here
# is not fatal: the app checks for studio updates by itself on every start.
studio_update() {
  say "Bringing the studio up to the newest published update..."
  if curl -fsSL "$STUDIO_URL" -o "$WORK/update-studio-mac.sh" 2>/dev/null; then
    bash "$WORK/update-studio-mac.sh" < /dev/null \
      || warn "The studio update did not finish; the app will fetch it by itself."
    # The updater opens the app on success; make sure it is open either way.
    pgrep -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1 || open "$DEST/$APP_NAME.app"
  else
    warn "Could not fetch the studio updater; the app will update its studio by itself."
    [ "$OPEN_AFTER" = "1" ] && open "$DEST/$APP_NAME.app"
  fi
}

# ---- 1. the right kind of machine ---------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. On Windows, use the .exe from the download page."

# `uname -m` reports x86_64 inside a Rosetta shell even on Apple Silicon,
# so ask the hardware rather than the shell.
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]; then
  die "This Mac has an Intel processor. The Green Vision Mac build is for Apple Silicon (M1 or newer) only."
fi

OS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "${OS_MAJOR:-0}" -ge 11 ] || die "macOS 11 (Big Sur) or newer is needed; this Mac runs $(sw_vers -productVersion)."

# ---- 2. room for it -----------------------------------------------------
FREE_GB=$(( $(df -k "$HOME" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
[ "$FREE_GB" -ge "$NEED_GB" ] || die "Needs about ${NEED_GB} GB free; only ${FREE_GB} GB is available."

# ---- 3. the newest macOS release ----------------------------------------
say "Finding the newest Green Vision for Mac..."
# A release being re-published has no file for a few seconds. Wait for it
# rather than quietly installing the version before it.
for ATTEMPT in 1 2 3 4 5 6 7 8 9 10; do
API_JSON_FILE="$(mktemp -t greenvision-releases)"
AUTH=()
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
HTTP="$(curl -sSL ${AUTH[@]+"${AUTH[@]}"} -H "Accept: application/vnd.github+json" -w "%{http_code}" \
  "https://api.github.com/repos/$REPO/releases?per_page=20" -o "$API_JSON_FILE" 2>/dev/null || echo 000)"

[ -n "${GV_FORCE_FEED:-}" ] && HTTP=403     # test hook: exercise the feed path
if [ "$HTTP" = "000" ]; then
  rm -f "$API_JSON_FILE"
  die "Could not reach GitHub. Check the internet connection and try again."
fi

if [ "$HTTP" != "200" ]; then
  # Usually 403: the hourly limit for anonymous API calls from this network.
  # The Atom feed is a normal page and is not limited the same way.
  rm -f "$API_JSON_FILE"
  [ "$ATTEMPT" = 1 ] && warn "GitHub's API said $HTTP (usually its hourly limit); using the releases feed instead."
  FEED_VER="$(curl -fsSL "https://github.com/$REPO/releases.atom" 2>/dev/null \
    | grep -o 'releases/tag/mac-v[0-9][0-9.]*' | sed 's#.*mac-v##' \
    | sort -t. -k1,1n -k2,2n -k3,3n | tail -1 || true)"
  [ -n "$FEED_VER" ] || die "Could not find a macOS release on GitHub. Try again in a few minutes."
  NEWEST_TAG="$FEED_VER"
  ZIP_NAME="GreenVision-$FEED_VER-mac-arm64-beta.zip"
  ZIP_URL="https://github.com/$REPO/releases/download/mac-v$FEED_VER/$ZIP_NAME"
  EXPECT_SHA=""
  # A HEAD request tells a published file from one still uploading.
  if curl -fsIL -o /dev/null "$ZIP_URL" 2>/dev/null; then VERSION="$FEED_VER"; break; fi
  VERSION=""
  [ "$ATTEMPT" = 10 ] && die "Version $NEWEST_TAG is still being published. Try again in a few minutes."
  warn "Version $NEWEST_TAG is still being uploaded; waiting 30 seconds..."
  sleep 30
  continue
fi

# Parse with the JavaScript engine built into macOS (osascript -l JavaScript):
# a stock Mac has no jq and no working python3, and grep/sed over JSON breaks
# the first time GitHub reorders a field. The list comes newest first; take
# the first mac-arm64 zip and the SHA-256 digest GitHub stored for it.
PARSED="$(osascript -l JavaScript -e '
function run(argv) {
  ObjC.import("Foundation");
  var s = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null).js;
  var rel = JSON.parse(s), best = null;
  // HIGHEST VERSION, NOT FIRST IN THE LIST. Re-publishing a release deletes
  // its zip before uploading the new one, and for those seconds the newest
  // release has no file at all - taking the first match then installed
  // 1.3.0 on a clean Mac while 1.5.0 was being re-uploaded.
  function v(name) { var m = /GreenVision-([0-9.]+)-mac/.exec(name); return m ? m[1].split(".").map(Number) : [0]; }
  function newer(a, b) { for (var k = 0; k < Math.max(a.length, b.length); k++) { var x = a[k] || 0, y = b[k] || 0; if (x !== y) return x > y; } return false; }
  for (var i = 0; i < rel.length; i++) {
    if (rel[i].draft) continue;
    var a = rel[i].assets || [];
    for (var j = 0; j < a.length; j++) {
      if (/mac-arm64.*\.zip$/.test(a[j].name) && a[j].state === "uploaded" &&
          (!best || newer(v(a[j].name), v(best.name)))) best = a[j];
    }
  }
  var top = [0];
  for (var t = 0; t < rel.length; t++) {
    var mt = /^mac-v([0-9.]+)$/.exec(rel[t].tag_name || "");
    if (mt && !rel[t].draft && newer(mt[1].split(".").map(Number), top)) top = mt[1].split(".").map(Number);
  }
  if (best) return [best.browser_download_url, best.name, (best.digest || "").replace(/^sha256:/, ""), top.join(".")].join("\n");
  return "";
}' "$API_JSON_FILE" 2>/dev/null || true)"
rm -f "$API_JSON_FILE"

ZIP_URL="$(printf '%s\n' "$PARSED" | sed -n 1p)"
ZIP_NAME="$(printf '%s\n' "$PARSED" | sed -n 2p)"
EXPECT_SHA="$(printf '%s\n' "$PARSED" | sed -n 3p)"
[ -n "$ZIP_URL" ] || die "No macOS build was found on the releases page."
VERSION="$(printf '%s' "$ZIP_NAME" | sed -n 's/^GreenVision-\([0-9.]*\)-mac.*/\1/p')"
NEWEST_TAG="$(printf '%s\n' "$PARSED" | sed -n 4p)"
if [ -z "$NEWEST_TAG" ] || [ "$NEWEST_TAG" = "$VERSION" ]; then break; fi
[ "$ATTEMPT" = 10 ] && die "Version $NEWEST_TAG is still being published. Try again in a few minutes."
warn "Version $NEWEST_TAG is still being uploaded; waiting 30 seconds..."
sleep 30
done

INSTALLED=""
# A copy in ~/Applications counts too, and an update goes where it already is.
if [ ! -d "$DEST/$APP_NAME.app" ] && [ "$DEST" = "/Applications" ] && [ -d "$HOME/Applications/$APP_NAME.app" ]; then
  DEST="$HOME/Applications"
fi
if [ -d "$DEST/$APP_NAME.app" ]; then
  INSTALLED="$(defaults read "$DEST/$APP_NAME.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || true)"
fi
if [ -n "$INSTALLED" ]; then
  say "Installed: $INSTALLED   Newest: ${VERSION:-unknown}"
else
  say "Green Vision is not installed yet. Newest: ${VERSION:-unknown}"
fi

WORK="$(mktemp -d -t greenvision)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# ---- already the newest? then only the studio needs updating -------------
if [ -n "$INSTALLED" ] && [ -n "${VERSION:-}" ] && [ "$FORCE" != "1" ] && ver_ge "$INSTALLED" "$VERSION"; then
  say "The app is already the newest version ($INSTALLED) - no download needed."
  if [ "$OPEN_AFTER" = "1" ]; then
    studio_update
  else
    say "Skipping the studio update because of --no-open; the app updates its studio by itself."
  fi
  say "Done. Run the same command any time to update."
  return 0
fi
if [ -n "$INSTALLED" ]; then
  say "Updating Green Vision from $INSTALLED to ${VERSION:-the newest version}..."
fi

# ---- 4. download --------------------------------------------------------

ZIP="$WORK/$ZIP_NAME"
say "Downloading $ZIP_NAME (about 1.1 GB)..."
# --retry covers a dropped connection; -C - resumes instead of starting over.
curl -fL --retry 5 --retry-delay 3 -C - --progress-bar -o "$ZIP" "$ZIP_URL" \
  || die "The download did not finish. Run the command again; it resumes where it stopped."

# ---- 5. is it the file that was published -------------------------------
if [ -n "$EXPECT_SHA" ]; then
  say "Checking the download is intact..."
  GOT_SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
  [ "$GOT_SHA" = "$EXPECT_SHA" ] || die "Checksum mismatch: the download is damaged or not the published file. Nothing was installed."
else
  warn "GitHub gave no checksum for this file, so it could not be verified."
fi

# ---- 6. unpack ----------------------------------------------------------
say "Unpacking..."
# ditto, not unzip: it keeps the symlinks and extended attributes an .app
# bundle depends on, which unzip can flatten into a bundle that will not run.
ditto -x -k "$ZIP" "$WORK/x"
NEW_APP="$(find "$WORK/x" -maxdepth 2 -name "*.app" -type d | head -1)"
[ -n "$NEW_APP" ] || die "The download did not contain an app."

# ---- 7. replace any running / installed copy ----------------------------
if pgrep -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1; then
  say "Closing the running copy of $APP_NAME..."
  osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1 || break
    sleep 1
  done
  pkill -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1 || true
  # the engine is a child Python process; make sure it is gone too
  pkill -f "greenplan.server" >/dev/null 2>&1 || true
fi

mkdir -p "$DEST" 2>/dev/null || true
SUDO=""
if [ ! -w "$DEST" ]; then
  warn "$DEST needs an administrator password to write to."
  SUDO="sudo"
fi

say "Installing to $DEST..."
$SUDO rm -rf "$DEST/$APP_NAME.app"
$SUDO ditto "$NEW_APP" "$DEST/$APP_NAME.app"

# The build is signed ad hoc rather than by an Apple developer account, so a
# quarantined copy is refused on first open. This is your own app; clearing
# the flag is the command-line equivalent of right-click → Open.
$SUDO xattr -dr com.apple.quarantine "$DEST/$APP_NAME.app" 2>/dev/null || true

INSTALLED_NOW="$(defaults read "$DEST/$APP_NAME.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo "$VERSION")"

if [ "$KEEP_ZIP" = "1" ]; then
  cp "$ZIP" "$HOME/Downloads/" && say "Kept the zip in ~/Downloads"
fi

say "Green Vision $INSTALLED_NOW is installed at $DEST/$APP_NAME.app"
echo "    Your saved projects, account and settings are kept between updates."
echo "    To update later, run the same command again."

# ---- 8. newest studio, then open it ---------------------------------------
# The app just installed carries the studio it was built with; a newer studio
# patch may be published since. The updater stages it and opens the app.
if [ "$OPEN_AFTER" = "1" ]; then
  say "(the first start takes up to a minute while the engine loads)"
  studio_update
  pgrep -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1 || open "$DEST/$APP_NAME.app"
fi
}

main "$@" < /dev/null
