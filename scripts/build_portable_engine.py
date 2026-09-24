"""Build a self-contained Green Vision engine that runs without installing Python.

Why this exists
---------------
The Windows app ships a baked studio and opens offline, but the two things
people actually notice as "slow" or "broken" - the 100 km2 feature census and
the Traffic tab - are map reads. Those go to the local OpenStreetMap index
when `greenplan.server` is running, and to the public Overpass instance when
it is not. On a machine that cannot reach overpass-api.de (measured on the
development machine: an 84-second connect timeout, not a rate limit) the
second path never completes at all, so the Traffic tab simply never loads.

A colleague who installs the app has no Python, no virtualenv, and no repo.
So the engine has to travel with the installer, complete: an interpreter, the
four libraries it imports, the package itself, the city data, and the map
index. That is what this assembles.

    desktop/engine/
      python/                 CPython embeddable + numpy, pandas, h3, yaml,
                              requests - no system Python involved
      greenplan/              the engine package
      config/                 the city configs
      web/                    gv-engine.js (parity target)
      data/
        *.csv                 AQI, NDVI and traffic panels per city
        i18n/                 interface + assistant strings
        osm/index.slim.jsonl.gz   2.6M indexed map features

The EMBEDDABLE distribution is the right base and a normal install is not:
it is a plain directory, it is relocatable, it does not touch the registry or
PATH, and it does not care that the user has some other Python. Its one quirk
is `python313._pth`, which disables `site` and therefore site-packages; the
line has to be uncommented or nothing pip installs is importable.

Run:
    .venv/Scripts/python scripts/build_portable_engine.py

Then `node desktop/pack.js` copies the result into the app, and main.js
starts it. Re-running is cheap: the downloads are cached and the tree is only
rebuilt when it is missing or --force is given.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("build_portable_engine")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "desktop" / "engine"
CACHE = ROOT / "desktop" / ".engine-cache"

# Pinned to the version the repo's own virtualenv runs, so the wheels that
# pip resolves here are the wheels that were tested here.
PY_VERSION = "3.13.14"
PY_ZIP = f"python-{PY_VERSION}-embed-amd64.zip"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{PY_ZIP}"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# The engine's actual imports. Deliberately NOT requirements.txt, which also
# describes the optional OpenVINO reasoning stack - a gigabyte of runtime the
# server degrades away from and no colleague needs to receive.
DEPS = ["numpy>=1.26", "pandas>=2.1", "h3>=3.7", "PyYAML>=6.0", "requests>=2.31"]

# The local-reasoning stack, added by --with-openvino. openvino-genai pulls
# openvino and openvino-tokenizers with it: about 260 MB of runtime, on top of
# roughly 1 GB of compressed model weights under models/openvino.
#
# huggingface_hub is NOT here. It is what DOWNLOADS a model; running one needs
# only the runtime, and a shipped bundle has its model already.
OV_DEPS = ["openvino-genai>=2024.4"]

# The runtime for the TRAINED FORECASTER, which is a different and much
# smaller thing than the language model: 60 KB of ONNX per city, no weights
# to download, and it is what makes the OpenVINO integration real in every
# shipped copy rather than only on a machine that fetched a 1 GB model.
FORECAST_DEPS = ["openvino>=2024.4"]

# Pruned from the runtime after install. The wheel carries every plugin and
# every model-format frontend; the forecaster needs the CPU plugin, the GPU
# plugin (so `device: GPU` works on an Intel laptop) and the ONNX frontend.
# The NPU compiler alone is 94 MB and cannot be used without NPU hardware
# drivers, so shipping it costs every user a third of the bundle for nothing.
OV_PRUNE = ("openvino_intel_npu_compiler.dll", "openvino_intel_npu_plugin.dll",
            "openvino_tensorflow_frontend.dll", "openvino_tensorflow_lite_frontend.dll",
            "openvino_pytorch_frontend.dll", "openvino_paddle_frontend.dll")

# Which model directory travels with the bundle. Everything else under
# models/ is per-city training state the engine writes for itself.
OV_MODEL_DIR = "openvino"

# What the engine reads at run time. Everything else in data/ is a source
# artefact - the .osm.pbf extracts alone are 1 GB and are only inputs to the
# indexer, which has already run.
DATA_KEEP_SUFFIXES = (".csv", ".json", ".yaml", ".yml")
OSM_INDEX = "index.slim.jsonl.gz"


def fetch(url: str, dest: Path) -> Path:
    """Download once, keep it. A rebuild should not re-pull 11 MB."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        log.info("  cached  %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
        return dest
    log.info("  fetching %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "green-vision-build"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    log.info("  got     %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
    return dest


def build_python(py_dir: Path) -> Path:
    """Unpack the embeddable interpreter and give it pip and the libraries."""
    zip_path = fetch(PY_URL, CACHE / PY_ZIP)

    log.info("unpacking the interpreter")
    py_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(py_dir)

    # The embeddable build ships `python313._pth` with `import site` commented
    # out, which switches OFF site-packages entirely. Leave it that way and
    # every single pip install below is invisible to the interpreter - it
    # imports nothing and fails at `import numpy` with no clue why.
    pths = list(py_dir.glob("python*._pth"))
    if not pths:
        raise SystemExit("no python*._pth in the embeddable zip - layout changed?")
    pth = pths[0]
    text = pth.read_text(encoding="utf-8")
    if "\n#import site" in text or text.startswith("#import site"):
        text = text.replace("#import site", "import site")
    if "import site" not in text:
        text = text.rstrip() + "\nimport site\n"
    # Lib/site-packages is where pip --target will put things.
    if "Lib\\site-packages" not in text:
        text = text.rstrip() + "\nLib\\site-packages\n"
    # ".." is the engine root, one level above python/ - the directory that
    # holds greenplan/. A _pth file REPLACES the default sys.path rather than
    # adding to it, so neither the current directory nor the parent is on it,
    # and `python -m greenplan.server` failed with "No module named
    # 'greenplan'" even standing in the right folder. Naming it here means the
    # engine starts the same way from any working directory, which is what
    # main.js needs when it spawns this from inside Program Files.
    if "\n..\n" not in "\n" + text:
        text = text.rstrip() + "\n..\n"
    pth.write_text(text, encoding="utf-8")
    log.info("  %s: site enabled", pth.name)

    exe = py_dir / "python.exe"
    get_pip = fetch(GET_PIP_URL, CACHE / "get-pip.py")
    log.info("installing pip")
    subprocess.run([str(exe), str(get_pip), "--no-warn-script-location"],
                   check=True, cwd=str(py_dir), env=isolated_env(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    # --target and --upgrade, and an environment that cannot see the builder's
    # own packages. Both halves matter, and leaving either out produced a
    # bundle that worked HERE and nowhere else:
    #
    # Enabling `import site` (which site-packages needs) also turns the user
    # site directory back on, so pip saw the build machine's
    #   ...\LocalCache\local-packages\Python313\site-packages
    # answered "Requirement already satisfied: numpy", and installed nothing.
    # The bundle shipped with pandas and h3 but no numpy, yaml, requests,
    # dateutil, certifi or urllib3 - and still passed a naive import check,
    # because that check was borrowing the very packages it had failed to
    # copy. On a colleague's machine it would have died at `import numpy`.
    #
    # PYTHONNOUSERSITE removes that directory from the answer; --target names
    # exactly where files must land; --upgrade stops pip treating anything it
    # can still see as good enough.
    sp = py_dir / "Lib" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    log.info("installing %s", ", ".join(d.split(">=")[0] for d in DEPS))
    subprocess.run([str(exe), "-m", "pip", "install",
                    "--target", str(sp), "--upgrade",
                    "--no-warn-script-location", "--no-compile", *DEPS],
                   check=True, cwd=str(py_dir), env=isolated_env())
    return exe


def isolated_env() -> dict:
    """The build machine's own Python must be invisible to the bundle."""
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def slim_python(py_dir: Path) -> None:
    """Drop what a running engine never reads.

    pip and its vendored wheels, the bundled test suites and every __pycache__
    together come to well over a hundred megabytes, and the installer carries
    all of it to every colleague otherwise. None of it is imported at run time
    - the engine installs nothing and runs no tests.
    """
    before = dir_size(py_dir)
    sp = py_dir / "Lib" / "site-packages"
    for name in ("pip", "pip-*.dist-info", "setuptools", "setuptools-*.dist-info",
                 "pkg_resources", "wheel", "wheel-*.dist-info", "_distutils_hack"):
        for p in sp.glob(name):
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    for pat in ("**/tests", "**/test", "**/__pycache__"):
        for p in sorted(sp.glob(pat), reverse=True):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    for p in sp.rglob("*.pyc"):
        p.unlink(missing_ok=True)
    after = dir_size(py_dir)
    log.info("  trimmed %.0f MB -> %.0f MB", before / 1e6, after / 1e6)


def copy_engine(out: Path) -> None:
    """The package, the configs, and only the data the server actually opens."""
    for name in ("greenplan", "config", "web"):
        src = ROOT / name
        if not src.is_dir():
            continue
        dst = out / name
        # Merge in place rather than delete-then-copy. rmtree(ignore_errors)
        # fails SILENTLY when OneDrive holds a handle on anything underneath,
        # and the copytree that follows then dies on "file already exists" -
        # which is what happened here. dirs_exist_ok never needs the directory
        # handle at all.
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(
                            "__pycache__", "*.pyc", ".pytest_cache"))
        log.info("  %-10s %6.1f MB", name, dir_size(dst) / 1e6)

    data_src, data_dst = ROOT / "data", out / "data"
    data_dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in data_src.iterdir():
        if f.is_file() and f.suffix.lower() in DATA_KEEP_SUFFIXES:
            shutil.copy2(f, data_dst / f.name)
            n += 1
    if (data_src / "i18n").is_dir():
        shutil.rmtree(data_dst / "i18n", ignore_errors=True)
        shutil.copytree(data_src / "i18n", data_dst / "i18n", dirs_exist_ok=True)

    # The map index: the single biggest file, and the reason the Traffic tab
    # and the census work at all without a reachable Overpass instance.
    idx = data_src / "osm" / OSM_INDEX
    if idx.is_file():
        (data_dst / "osm").mkdir(parents=True, exist_ok=True)
        shutil.copy2(idx, data_dst / "osm" / OSM_INDEX)
        log.info("  %-10s %6.1f MB  (%s)", "osm index",
                 idx.stat().st_size / 1e6, OSM_INDEX)
    else:
        log.warning("  %s missing - the app will fall back to public Overpass, "
                    "which is exactly the failure this bundle exists to remove",
                    OSM_INDEX)
    log.info("  %-10s %6.1f MB  (%d csv/json + i18n)", "data",
             dir_size(data_dst) / 1e6, n)

    # Trained per-city state: the forecaster networks and the engine's
    # memory. Never copied before, so an installed app started from nothing
    # and rebuilt what it could at runtime.
    models_src = ROOT / "models"
    if models_src.is_dir():
        models_dst = out / "models"
        models_dst.mkdir(parents=True, exist_ok=True)
        for city in models_src.iterdir():
            if not city.is_dir() or city.name == OV_MODEL_DIR:
                continue
            shutil.copytree(city, models_dst / city.name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        log.info("  %-10s %6.1f MB", "models", dir_size(models_dst) / 1e6)


def copy_openvino_model(out: Path) -> bool:
    """The compressed model, copied whole.

    Refuses rather than shipping half a model: an INT4 build is an .xml graph
    plus a .bin of weights plus the tokenizer pair, and a bundle missing any
    of them fails at load time on the user's machine, which is the worst
    possible place to discover it.
    """
    src = ROOT / "models" / OV_MODEL_DIR
    if not src.is_dir():
        log.error("no models/%s - fetch one first: "
                  "python scripts/fetch_openvino_model.py", OV_MODEL_DIR)
        return False
    # Real models only. A dot-directory in here is a cache or an editor's
    # leavings, not something to ship - and treating one as a model made
    # this function report an incomplete model and refuse the whole build.
    models = [d for d in src.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not models:
        log.error("models/%s is empty", OV_MODEL_DIR)
        return False

    ok = True
    dst_root = out / "models" / OV_MODEL_DIR
    for m in models:
        need = ["openvino_model.xml", "openvino_model.bin",
                "openvino_tokenizer.xml", "openvino_detokenizer.xml"]
        missing = [f for f in need if not (m / f).is_file()]
        if missing:
            log.error("  %s is incomplete, missing: %s", m.name, ", ".join(missing))
            ok = False
            continue
        dst = dst_root / m.name
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(m, dst, dirs_exist_ok=True)
        log.info("  %-10s %6.1f MB  (%s)", "ov model", dir_size(dst) / 1e6, m.name)
    return ok


def prune_openvino(py_dir: Path) -> None:
    """Drop the parts of the runtime this engine cannot use.

    The wheel is built for every use OpenVINO has: NPU compilation, TensorFlow
    and PyTorch model import, Paddle. The forecaster reads ONNX and runs on
    CPU or GPU. Everything else is weight a colleague downloads and never
    executes - and the NPU compiler alone is 94 MB.
    """
    libs = py_dir / "Lib" / "site-packages" / "openvino" / "libs"
    if not libs.is_dir():
        return
    freed = 0
    for name in OV_PRUNE:
        f = libs / name
        if f.is_file():
            freed += f.stat().st_size
            f.unlink()
    if freed:
        log.info("  pruned    %6.1f MB of unused OpenVINO plugins", freed / 1e6)


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def verify(py_exe: Path, out: Path, with_openvino: bool = False,
           forecast_runtime: bool = True) -> None:
    """Import what the server imports, with the shipped interpreter.

    A build that produces a directory but cannot `import pandas` is worse than
    no build: it ships, it starts, and it fails on the first request.
    """
    log.info("verifying the bundle")
    # Verified in the SAME isolation a colleague's machine provides. The first
    # version of this check ran with the build machine's environment intact,
    # so it imported the builder's numpy and reported a bundle that had none.
    # A check that can pass by using something the bundle does not contain is
    # not a check.
    code = ("import sys, numpy, pandas, h3, yaml, requests;"
            "sys.path.insert(0, r'%s');"
            "import greenplan.server as s;"
            "bad=[p for p in sys.path if 'local-packages' in p or 'Roaming' in p];"
            "assert not bad, 'leaked host paths: %%r' %% bad;"
            "print('OK', sys.version.split()[0], 'numpy', numpy.__version__,"
            "'pandas', pandas.__version__, 'h3', h3.__version__)" % str(out))
    r = subprocess.run([str(py_exe), "-c", code], capture_output=True, text=True,
                       env=isolated_env())
    if r.returncode != 0:
        log.error("  FAILED\n%s\n%s", r.stdout, r.stderr[-2000:])
        raise SystemExit("the bundled interpreter cannot import the engine")
    log.info("  %s", r.stdout.strip())

    if forecast_runtime:
        """Compile a real exported network with the SHIPPED interpreter.

        Importing openvino proves the wheels arrived. It does not prove this
        stripped-down runtime can still read an ONNX graph and produce a
        number, which is the only thing the feature rests on - and pruning
        plugins is exactly the kind of change that breaks it quietly.
        """
        log.info("verifying the forecaster runtime")
        net = next((p for p in (out / "models").glob("*/forecaster/ndvi.onnx")), None)
        if net is None:
            log.warning("  no exported network in the bundle - the challenger "
                        "will simply not run")
        else:
            fcode = (
                "import numpy as np, openvino as ov;"
                "c=ov.Core();"
                "m=c.compile_model(c.read_model(r'%s'),'CPU');"
                "x=np.zeros((1,%d),dtype=np.float32);"
                "r=list(m(x).values())[0];"
                "print('OK openvino', ov.__version__.split('-')[0],"
                "'devices', c.available_devices, 'out', float(r.ravel()[0]))"
                % (str(net), 13))
            rf = subprocess.run([str(py_exe), "-c", fcode], capture_output=True,
                                text=True, env=isolated_env())
            if rf.returncode != 0:
                log.error("  FAILED\n%s\n%s", rf.stdout, rf.stderr[-1500:])
                raise SystemExit("the bundled runtime cannot run the forecaster; "
                                 "refusing to ship a build that reports it can")
            log.info("  %s", rf.stdout.strip())

    if not with_openvino:
        return

    """Prove the MODEL loads, not merely that the runtime imports.

    Importing openvino_genai says the wheels arrived. It says nothing about
    whether the weights beside them are readable by this interpreter on a
    machine with no PyTorch and no network - which is the only thing the
    shipped claim rests on. So the check loads the compressed model and
    generates a token, in the bundle's own isolation.
    """
    log.info("verifying the local model")
    mdir = out / "models" / OV_MODEL_DIR
    picked = next((d for d in mdir.iterdir() if d.is_dir()), None) if mdir.is_dir() else None
    if picked is None:
        raise SystemExit("--with-openvino: no model directory in the bundle")
    ov_code = (
        "import sys, time, openvino as ov, openvino_genai as g;"
        "t=time.time();"
        "p=g.LLMPipeline(r'%s','CPU');"
        "c=g.GenerationConfig(); c.max_new_tokens=8;"
        "out=str(p.generate('Say READY.', c));"
        "print('OK openvino', ov.__version__.split('-')[0],"
        "'load %%.1fs' %% (time.time()-t), '|', out.strip()[:40])" % str(picked))
    r2 = subprocess.run([str(py_exe), "-c", ov_code], capture_output=True, text=True,
                        env=isolated_env())
    if r2.returncode != 0:
        log.error("  FAILED\n%s\n%s", r2.stdout, r2.stderr[-2000:])
        raise SystemExit("the bundled interpreter cannot run the local model; "
                         "refusing to ship a build that claims it can")
    log.info("  %s", r2.stdout.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--force", action="store_true",
                    help="rebuild the interpreter even if it is already there")
    ap.add_argument("--no-forecast-runtime", dest="forecast_runtime",
                    action="store_false",
                    help="leave out the OpenVINO runtime the trained forecaster "
                         "runs on (~135 MB pruned). The engine still works; the "
                         "network simply never runs and nothing reports it.")
    ap.add_argument("--with-openvino", action="store_true",
                    help="ship the local reasoning model: the OpenVINO runtime "
                         "(~260 MB) and the compressed weights under "
                         "models/openvino (~1 GB). Without this the engine "
                         "falls back to its deterministic offline writer.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    py_dir = OUT / "python"
    if args.force:
        shutil.rmtree(py_dir, ignore_errors=True)

    OUT.mkdir(parents=True, exist_ok=True)
    py_exe = py_dir / "python.exe"
    if py_exe.is_file() and not args.force:
        log.info("interpreter already built (use --force to redo it)")
        have_ov = (py_dir / "Lib" / "site-packages" / "openvino").is_dir()
        if args.forecast_runtime and not have_ov:
            raise SystemExit(
                "this engine tree was built without the OpenVINO runtime, and "
                "pip has already been stripped from it, so it cannot be added "
                "now. Rebuild it:\n"
                "    python scripts/build_portable_engine.py --force\n"
                "or build without the runtime:\n"
                "    python scripts/build_portable_engine.py --no-forecast-runtime")
    else:
        py_exe = build_python(py_dir)

        # Extra runtimes go in HERE, before slim_python strips pip out. Doing
        # it afterwards fails with "No module named pip", which reads like a
        # broken interpreter rather than the ordering mistake it is.
        extra = []
        if args.forecast_runtime:
            extra += FORECAST_DEPS
        if args.with_openvino:
            extra += OV_DEPS
        if extra:
            log.info("installing %s", ", ".join(d.split(">=")[0] for d in extra))
            subprocess.run([str(py_exe), "-m", "pip", "install",
                            "--no-warn-script-location", *extra],
                           check=True, env=isolated_env())
            prune_openvino(py_dir)

        slim_python(py_dir)

    copy_engine(OUT)
    if args.with_openvino and not copy_openvino_model(OUT):
        raise SystemExit("--with-openvino asked for, but the model is not "
                         "there or is incomplete; refusing to ship a build "
                         "that claims local reasoning and cannot do it")
    verify(py_exe, OUT, with_openvino=args.with_openvino,
           forecast_runtime=args.forecast_runtime)

    total = dir_size(OUT)
    log.info("")
    log.info("engine bundle: %s", OUT)
    log.info("  python      %6.1f MB", dir_size(py_dir) / 1e6)
    log.info("  everything  %6.1f MB", total / 1e6)


if __name__ == "__main__":
    main()
