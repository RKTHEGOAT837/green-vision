# Green Vision for Android

A WebView shell around the same `index.html` the web build serves, with the
trained engine baked to JSON inside the APK.

## Why it is built this way

The studio is one HTML file; the engine is Python. A phone will not run
Python, so the APK does not try. `scripts/build_static.py` bakes the trained
engine down to JSON — the ranking, the canopy forecast, the soil table, the
species knowledge base, per city — and those files ship inside the APK. The
page already prefers a live engine and falls back to those exports, so the
same `index.html` runs here unmodified. The phone and a static web deploy run
byte-identical files.

## What works with no connection

The planting priority ranking for five cities, the score decomposition, the
worklist with its quantities, costs and CSV export, the canopy watch list, the
species picks, the design studio and its 25-year projection. All of that is
arithmetic over files already on the phone.

## What needs a connection

The basemap tiles, which are Esri's and not ours to bundle, and the live
air-quality and weather readings for a point you tap. Without a connection the
map is blank tiles and the area panel says the reading did not load — which is
the honest thing for it to say, and is what it already says on the web.

## Building it

    # 1. Bake the five-city bundle (from the repo root)
    python scripts/build_static.py --out dist_app \
        --cities config/city.yaml config/delhi.yaml config/mumbai.yaml \
                 config/bengaluru.yaml config/chennai.yaml

    # 2. Point local.properties at your SDK, then
    cd android
    ./gradlew assembleRelease

The APK lands in `app/build/outputs/apk/release/`.

`copyWebBundle` copies `dist_app/` into `app/src/main/assets/www` before the
build and fails loudly if it is missing, because an APK whose only screen is a
404 is worse than a build error. Override the location with
`-PgvBundle=/path/to/dist_app`.

## Signing

The release build is signed with the **debug** key so `assembleRelease`
produces an APK that installs on a device. That is deliberate for a
competition and field-test build, and it is **not** Play-ready: uploading to a
store needs a real upload key, which is yours to generate and keep, and is not
something to commit to a repository.

## What the shell deliberately does not do

No JavaScript bridge is installed. `addJavascriptInterface` is the standard
way to turn a WebView into a remote code execution surface, and nothing here
needs it. `allowFileAccess` and `allowContentAccess` are off; the page reads
its baked JSON through relative `fetch` under `android_asset`, which is
same-origin, so neither is required. Camera and microphone permission requests
from the page are denied outright.
