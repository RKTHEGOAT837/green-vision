# Green Vision launcher.
#
# Starts the engine if it is not already up, waits until it actually answers,
# then opens the studio in the default browser. Double-clicking the desktop
# shortcut should be the whole of it - no terminal, no ports to remember, no
# second copy of the engine if one is already running.
#
# Run directly for a visible log:  powershell -File launch\green-vision.ps1
# The desktop shortcut runs it through green-vision.vbs, which hides the
# console window.

$ErrorActionPreference = 'Stop'

$Port    = 8000
$Root    = Split-Path -Parent $PSScriptRoot
$Python  = Join-Path $Root '.venv\Scripts\python.exe'
$Url     = "http://127.0.0.1:$Port/"
$LogOut  = Join-Path $env:TEMP 'green-vision.out.log'
$LogErr  = Join-Path $env:TEMP 'green-vision.err.log'

function Test-Engine {
    # /api/health is the engine's own readiness answer; a socket that accepts
    # is not the same as an engine that has finished loading its panel.
    try {
        $r = Invoke-WebRequest -Uri "$Url`api/health" -UseBasicParsing -TimeoutSec 4
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Show-Problem($message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message, 'Green Vision', 'OK', 'Error') | Out-Null
}

if (-not (Test-Path $Python)) {
    Show-Problem "The Python environment is missing:`n`n$Python`n`nCreate it with:  python -m venv .venv"
    exit 1
}

if (Test-Engine) {
    # Already running - just bring the page up rather than starting a second.
    Start-Process $Url
    exit 0
}

# Nothing answering. If something else holds the port, say so plainly instead
# of starting an engine that cannot bind and then opening a page it does not
# serve - which is exactly how a stale copy of the app ends up on screen.
$held = netstat -ano | Select-String "LISTENING" | Select-String ":$Port\s"
if ($held) {
    Show-Problem "Port $Port is held by another program, and it is not the Green Vision engine.`n`nClose it and try again."
    exit 1
}

Start-Process -FilePath $Python `
    -ArgumentList '-m', 'greenplan.server', '--port', "$Port" `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput $LogOut -RedirectStandardError $LogErr

# First start builds the city panel and can take the better part of a minute;
# opening the browser before then shows a connection error and teaches the
# reader the app is broken.
$deadline = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $deadline) {
    if (Test-Engine) { Start-Process $Url; exit 0 }
    Start-Sleep -Seconds 2
}

Show-Problem "The engine did not come up within three minutes.`n`nThe log is at:`n$LogErr"
exit 1
