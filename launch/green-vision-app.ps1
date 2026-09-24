<#
  Green Vision (App) — start the engine, then open the Windows app.

  The app carries its own baked engine and runs offline, so unlike the web
  shortcut it does not NEED the Python server. It starts it anyway, for one
  reason: the local OpenStreetMap index. With the engine up, the 3D site
  reads and the area census are served from 2.6 million indexed features on
  this machine; without it they go to the public Overpass instance, which is
  slower and rate-limits. The app discovers the engine on its own, so this
  is purely a speed-up and the app opens either way.

  The app is launched first and the engine started behind it, because
  waiting on a cold index load before showing a window is how a shortcut
  feels broken.
#>

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv\Scripts\python.exe"

$candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Green Vision\GreenVision.exe"),
    (Join-Path $root "desktop\release\GreenVision-win32-x64\GreenVision.exe")
)
$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $exe) {
    [System.Windows.Forms.MessageBox]::Show(
        "Green Vision is not installed yet.`n`nRun the installer at:`ndesktop\release\GreenVision-1.0.0-x64.exe",
        "Green Vision") | Out-Null
    exit 1
}

# The window first — the engine is an optimisation, not a prerequisite.
Start-Process $exe

if (-not (Test-Path $venv)) { exit 0 }

# Already listening? Nothing to do.
foreach ($p in @(8000, 8010, 8020)) {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$p/api/health" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { exit 0 }
    } catch { }
}

$port = 0
foreach ($p in @(8000, 8010, 8020)) {
    if (-not (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue)) { $port = $p; break }
}
if ($port -eq 0) { exit 0 }

Start-Process -FilePath $venv `
    -ArgumentList "-m", "greenplan.server", "--port", "$port" `
    -WorkingDirectory $root -WindowStyle Hidden
