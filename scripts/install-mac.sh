#!/bin/bash
# Install or update Green Vision on a Mac, in one command:
#
#   curl -fsSL https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/install-mac.sh | bash
#
# Options (pass after `bash -s --`):
#   --dir <folder>   install somewhere other than /Applications
#   --no-open        install but do not launch it
#   --keep-zip       leave the downloaded zip in ~/Downloads
#
#   curl -fsSL .../install-mac.sh | bash -s -- --dir ~/Applications --no-open
#
# What it does, in order: checks this is an Apple Silicon Mac with room for
# the app, finds the newest macOS release, downloads it, checks its SHA-256
# against the one GitHub published, quits any running copy, replaces it,
# clears the download quarantine so macOS opens it without the "cannot be
# opened" refusal, and launches it.
#
# Written for the bash 3.2 and BSD tools every Mac ships with: no Homebrew,
# no Python, no Xcode needed.

set -euo pipefail

REPO="RKTHEGOAT837/green-vision"
APP_NAME="Green Vision"
DEST="/Applications"
OPEN_AFTER=1
KEEP_ZIP=0
NEED_GB=4            # 1.1 GB zip + about 2.5 GB unpacked, with room to spare

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)      DEST="${2:?--dir needs a folder}"; shift 2 ;;
    --no-open)  OPEN_AFTER=0; shift ;;
    --keep-zip) KEEP_ZIP=1; shift ;;
    -h|--help)  sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

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
say "Finding the newest Green Vision for Mac…"
API_JSON_FILE="$(mktemp -t greenvision-releases)"
curl -fsSL -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/releases?per_page=20" -o "$API_JSON_FILE" \
  || die "Could not reach GitHub. Check the internet connection and try again."

# Parse with the JavaScript engine built into macOS (osascript -l JavaScript):
# a stock Mac has no jq and no working python3, and grep/sed over JSON breaks
# the first time GitHub reorders a field. The list comes newest first; take
# the first mac-arm64 zip and the SHA-256 digest GitHub stored for it.
PARSED="$(osascript -l JavaScript -e '
function run(argv) {
  ObjC.import("Foundation");
  var s = $.NSString.stringWithContentsOfFileEncodingError(argv[0], $.NSUTF8StringEncoding, null).js;
  var rel = JSON.parse(s);
  for (var i = 0; i < rel.length; i++) {
    if (rel[i].draft) continue;
    var a = rel[i].assets || [];
    for (var j = 0; j < a.length; j++) {
      if (/mac-arm64.*\.zip$/.test(a[j].name)) {
        return [a[j].browser_download_url, a[j].name, (a[j].digest || "").replace(/^sha256:/, "")].join("\n");
      }
    }
  }
  return "";
}' "$API_JSON_FILE" 2>/dev/null || true)"
rm -f "$API_JSON_FILE"

ZIP_URL="$(printf '%s\n' "$PARSED" | sed -n 1p)"
ZIP_NAME="$(printf '%s\n' "$PARSED" | sed -n 2p)"
EXPECT_SHA="$(printf '%s\n' "$PARSED" | sed -n 3p)"
[ -n "$ZIP_URL" ] || die "No macOS build was found on the releases page."
VERSION="$(printf '%s' "$ZIP_NAME" | sed -n 's/^GreenVision-\([0-9.]*\)-mac.*/\1/p')"

INSTALLED=""
if [ -d "$DEST/$APP_NAME.app" ]; then
  INSTALLED="$(defaults read "$DEST/$APP_NAME.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || true)"
fi
if [ -n "$INSTALLED" ]; then
  say "Installed: $INSTALLED   Newest: ${VERSION:-unknown}"
else
  say "Newest: ${VERSION:-unknown}"
fi

# ---- 4. download --------------------------------------------------------
WORK="$(mktemp -d -t greenvision)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

ZIP="$WORK/$ZIP_NAME"
say "Downloading $ZIP_NAME (about 1.1 GB)…"
# --retry covers a dropped connection; -C - resumes instead of starting over.
curl -fL --retry 5 --retry-delay 3 -C - --progress-bar -o "$ZIP" "$ZIP_URL" \
  || die "The download did not finish. Run the command again; it resumes where it stopped."

# ---- 5. is it the file that was published -------------------------------
if [ -n "$EXPECT_SHA" ]; then
  say "Checking the download is intact…"
  GOT_SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
  [ "$GOT_SHA" = "$EXPECT_SHA" ] || die "Checksum mismatch: the download is damaged or not the published file. Nothing was installed."
else
  warn "GitHub gave no checksum for this file, so it could not be verified."
fi

# ---- 6. unpack ----------------------------------------------------------
say "Unpacking…"
# ditto, not unzip: it keeps the symlinks and extended attributes an .app
# bundle depends on, which unzip can flatten into a bundle that will not run.
ditto -x -k "$ZIP" "$WORK/x"
NEW_APP="$(find "$WORK/x" -maxdepth 2 -name "*.app" -type d | head -1)"
[ -n "$NEW_APP" ] || die "The download did not contain an app."

# ---- 7. replace any running / installed copy ----------------------------
if pgrep -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1; then
  say "Closing the running copy of $APP_NAME…"
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

say "Installing to $DEST…"
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

# ---- 8. open it ---------------------------------------------------------
if [ "$OPEN_AFTER" = "1" ]; then
  say "Opening it… (the first start takes up to a minute while the engine loads)"
  open "$DEST/$APP_NAME.app"
fi
