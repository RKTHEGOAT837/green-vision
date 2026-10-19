#!/bin/bash
# Bring the Green Vision studio on a Mac up to the newest published patch, now:
#
#   curl -fsSL https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/update-studio-mac.sh | bash
#
# The app does this by itself - it downloads a new studio on one start and
# switches to it on the next - but a copy quit before the download finished
# stays on the old studio until it is opened twice more. This does the same
# steps in one go: asks the update server which studio is current, downloads
# it, checks its SHA-256 against the published one, stages it exactly as the
# app would, and restarts the app so it switches over.
#
# Only tools every Mac ships with: bash 3.2, curl, shasum, plutil, osascript.
# KEEP THIS FILE ASCII OUTSIDE COMMENTS (see install-mac.sh for why).

set -euo pipefail

main() {

APP_NAME="Green Vision"
API="https://green-vision-api.greenvision-rk.workers.dev/app/patch"
DIR="$HOME/Library/Application Support/$APP_NAME/patch"
STATE="$DIR/state.json"
APP_PATH="/Applications/$APP_NAME.app"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "This is for macOS."
[ -d "$APP_PATH" ] || APP_PATH="$HOME/Applications/$APP_NAME.app"
[ -d "$APP_PATH" ] || die "Green Vision is not installed. Install it first with install-mac.sh."

APP_VER="$(defaults read "$APP_PATH/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo 1.6.1)"

say "Asking the update server for the current studio..."
MANIFEST="$(curl -fsSL "$API?v=$APP_VER&platform=darwin&nc=$RANDOM" -H 'Cache-Control: no-cache')" \
  || die "Could not reach the update server. Check the internet connection."

# Parse with the JavaScript engine built into macOS: a stock Mac has no jq.
FIELDS="$(osascript -l JavaScript -e '
function run(argv) {
  var p = (JSON.parse(argv[0]) || {}).patch;
  if (!p || !p.version || !p.url || !p.sha256) return "";
  return [p.version, p.url, p.sha256.toLowerCase(), p.bytes || 0, p.min_app || "", p.notes || ""].join("\n");
}' "$MANIFEST" 2>/dev/null || true)"
[ -n "$FIELDS" ] || die "The update server has no studio update to offer."
VER="$(printf '%s\n' "$FIELDS" | sed -n 1p)"
URL="$(printf '%s\n' "$FIELDS" | sed -n 2p)"
SHA="$(printf '%s\n' "$FIELDS" | sed -n 3p)"
BYTES="$(printf '%s\n' "$FIELDS" | sed -n 4p)"
MINAPP="$(printf '%s\n' "$FIELDS" | sed -n 5p)"
NOTES="$(printf '%s\n' "$FIELDS" | sed -n 6p | tr -d '"\\')"

ACTIVE=""
[ -f "$STATE" ] && ACTIVE="$(plutil -extract active.version raw -o - "$STATE" 2>/dev/null || true)"
say "Studio on this Mac: ${ACTIVE:-shipped with the app}   Current: $VER"

mkdir -p "$DIR/$VER"
say "Downloading studio $VER..."
curl -fsSL --retry 3 -o "$DIR/$VER/index.html.part" "$URL" || die "The download did not finish. Run the command again."
GOT="$(shasum -a 256 "$DIR/$VER/index.html.part" | awk '{print $1}')"
if [ "$GOT" != "$SHA" ]; then
  rm -f "$DIR/$VER/index.html.part"
  die "Checksum mismatch: the download is not the published file. Nothing was changed."
fi
mv -f "$DIR/$VER/index.html.part" "$DIR/$VER/index.html"

# The studio also reads these beside itself. Take them from the studio that
# is running now, or from the one shipped inside the app.
SRC=""
[ -n "$ACTIVE" ] && [ -d "$DIR/$ACTIVE" ] && SRC="$DIR/$ACTIVE"
[ -z "$SRC" ] && [ -d "$APP_PATH/Contents/Resources/studio" ] && SRC="$APP_PATH/Contents/Resources/studio"
if [ -n "$SRC" ] && [ "$SRC" != "$DIR/$VER" ]; then
  for f in brand data gv-engine.js; do
    [ -e "$SRC/$f" ] && [ ! -e "$DIR/$VER/$f" ] && cp -R "$SRC/$f" "$DIR/$VER/" || true
  done
fi

say "Staging it the way the app does..."
NOW="$(($(date +%s) * 1000))"
STAGED="{\"version\":\"$VER\",\"url\":\"$URL\",\"sha256\":\"$SHA\",\"bytes\":$BYTES,\"min_app\":\"$MINAPP\",\"notes\":\"$NOTES\",\"updated\":$NOW,\"at\":$NOW}"
if [ ! -f "$STATE" ]; then
  printf '{}' > "$STATE"
fi
plutil -replace staged -json "$STAGED" "$STATE"
plutil -replace failures -integer 0 "$STATE"
plutil -remove rolledBack "$STATE" >/dev/null 2>&1 || true

say "Restarting Green Vision so it switches to $VER..."
if pgrep -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1; then
  osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1 || break
    sleep 1
  done
  pkill -f "$APP_NAME.app/Contents/MacOS" >/dev/null 2>&1 || true
fi
sleep 1
open "$APP_PATH"

for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 1
  NOWACTIVE="$(plutil -extract active.version raw -o - "$STATE" 2>/dev/null || true)"
  [ "$NOWACTIVE" = "$VER" ] && break
done
if [ "${NOWACTIVE:-}" = "$VER" ]; then
  say "Done: Green Vision is now running studio $VER."
else
  say "Staged studio $VER. If the window still shows the old studio, quit Green Vision (Cmd+Q) and open it once more."
fi
}

main "$@" < /dev/null
