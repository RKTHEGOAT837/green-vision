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

## Toolchain, and why the build is not run from the agent shell

Everything needed is installed at spaceless paths, because the Android
`.bat` wrappers do not quote `JAVA_HOME` and the repository lives under a path
with spaces in it:

    C:\gvsdk\jdk\jdk-17.0.20.1+1      Temurin JDK 17
    C:\gvsdk\platforms\android-34     compile SDK
    C:\gvsdk\build-tools\34.0.0       aapt2, d8, apksigner
    C:\gvsdk\platform-tools           adb
    C:\gvsdk\gradle-8.7               Gradle

Gradle's official distribution host 307-redirects to somewhere that stalls on
this connection; `https://mirrors.cloud.tencent.com/gradle/` served the same
zip in ten seconds.

**Run `build-apk.bat` from a normal Windows terminal.** Gradle always forks a
build process and speaks to it over a loopback socket, and that fork fails
inside a sandboxed shell with

    java.io.IOException: Unable to establish loopback connection

before any compilation begins. Plain loopback works there — a Java program
that binds 127.0.0.1 and connects to itself succeeds — so this is the fork
specifically, not the network. Matching `org.gradle.jvmargs` to the launcher
exactly does not avoid it: `gradle.bat` starts the launcher with
`-Xmx64m -Xms64m` plus an instrumentation agent, so a fork is unconditional.
