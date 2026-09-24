<#
  Green Vision (Web) — start the engine, then open the studio in the browser.

  "Start everything" means the Python engine as well as the page. The engine
  is what makes the web build fast: it holds the 2.6-million-feature local
  OpenStreetMap index, so the census and the 3D site reads come back in
  milliseconds instead of going to the public Overpass instance and being
  throttled. Without it the page still works — it falls back to the hosted
  bundle — but the difference is roughly 40 seconds against 8 on the first
  read, so it is worth waiting a moment for.

  There are no API keys to set. Every source the app uses is either baked
  into the bundle or a public endpoint that needs no key; the script says so
  rather than leaving you wondering what it did not configure.
#>

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv\Scripts\python.exe"
$hosted = "https://green-vision-india.netlify.app/"

function Find-FreePort {
    param([int[]]$Try = @(8000, 8010, 8020, 8030, 8040))
    foreach ($p in $Try) {
        $inUse = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
        if (-not $inUse) { return $p }
    }
    return 0
}

# Already running? Use it rather than starting a second copy.
foreach ($p in @(8000, 8010, 8020, 8030, 8040)) {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$p/api/health" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) {
            Start-Process "http://127.0.0.1:$p/"
            exit 0
        }
    } catch { }
}

if (-not (Test-Path $venv)) {
    # No local Python environment: the hosted build is the honest fallback,
    # and it is a complete app — just without the local index.
    Start-Process $hosted
    exit 0
}

$port = Find-FreePort
if ($port -eq 0) { Start-Process $hosted; exit 0 }

Start-Process -FilePath $venv `
    -ArgumentList "-m", "greenplan.server", "--port", "$port" `
    -WorkingDirectory $root -WindowStyle Hidden

# Wait for it, but not forever. Ninety seconds is the cold start with the
# index load; past that something is wrong and the hosted build is better
# than a browser pointed at nothing.
$deadline = (Get-Date).AddSeconds(90)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$port/api/health" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 700
}

if ($ready) { Start-Process "http://127.0.0.1:$port/" }
else        { Start-Process $hosted }
