# Green Vision launcher.
#
# Finds a Green Vision engine if one is already running, starts one on the
# first free port if not, waits until it actually answers, and opens the studio
# in the default browser. Double-clicking the desktop shortcut should be the
# whole of it - no terminal, no port to remember, no second copy of the engine.
#
# Run directly for a visible log:  powershell -File launch\green-vision.ps1
# The desktop shortcut runs it through green-vision.vbs, which hides the
# console window.

$ErrorActionPreference = 'Stop'

# 8000 first because that is the one people have in their history; the rest are
# there so a port already in use is an inconvenience rather than a dead end.
$Ports  = 8000..8020
$Root   = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogOut = Join-Path $env:TEMP 'green-vision.out.log'
$LogErr = Join-Path $env:TEMP 'green-vision.err.log'

function Get-ListeningPorts {
    # One netstat, not one per candidate: probing 21 ports over HTTP takes
    # seconds, reading the listener table takes one call.
    $set = @{}
    foreach ($line in (netstat -ano | Select-String 'LISTENING')) {
        if ($line -match ':(\d+)\s') { $set[[int]$Matches[1]] = $true }
    }
    return $set
}

function Test-Engine([int]$port) {
    # /api/health is the engine's own readiness answer. A socket that accepts
    # is not an engine that has finished loading its panel, and some other
    # program answering on the port is not our engine at all - so this checks
    # the payload, not just the status code.
    #
    # The URL is built by concatenation on purpose. "$Url`api/health" reads as
    # appending a path; in PowerShell the backtick before the 'a' is an escape,
    # so it asked for /<BEL>pi/health. Every check failed, the launcher started
    # an engine that was working perfectly, polled a path that does not exist
    # for three minutes, and reported that the engine never came up.
    $url = 'http://127.0.0.1:' + $port + '/api/health'
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -ne 200) { return $false }
        $j = $r.Content | ConvertFrom-Json
        return ($j.ok -eq $true -and $null -ne $j.city)
    } catch { return $false }
}

function Show-Problem($message) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $message, 'Green Vision', 'OK', 'Error') | Out-Null
}

function Open-Studio([int]$port) {
    Start-Process ('http://127.0.0.1:' + $port + '/')
}

if (-not (Test-Path $Python)) {
    Show-Problem ("The Python environment is missing:`n`n$Python`n`n" +
                  "Create it with:  python -m venv .venv")
    exit 1
}

$listening = Get-ListeningPorts

# 1. Is one of ours already up? Only bother asking ports that have a listener.
foreach ($p in $Ports) {
    if ($listening.ContainsKey($p) -and (Test-Engine $p)) {
        Open-Studio $p
        exit 0
    }
}

# 2. Otherwise take the first port nobody is on.
$port = $null
foreach ($p in $Ports) { if (-not $listening.ContainsKey($p)) { $port = $p; break } }
if ($null -eq $port) {
    Show-Problem ("Every port from $($Ports[0]) to $($Ports[-1]) is in use, and " +
                  "none of them is a Green Vision engine.`n`n" +
                  "Close something on one of those ports and try again.")
    exit 1
}

Start-Process -FilePath $Python `
    -ArgumentList '-m', 'greenplan.server', '--port', "$port" `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput $LogOut -RedirectStandardError $LogErr

# First start builds the city panel and can take the better part of a minute;
# opening the browser before then shows a connection error and teaches the
# reader the app is broken when it is merely still starting.
$deadline = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $deadline) {
    if (Test-Engine $port) { Open-Studio $port; exit 0 }
    Start-Sleep -Seconds 2
}

# Name the address that was polled. When this last fired, the engine was up and
# answering the whole time and only the URL was wrong; the message said "the
# engine did not come up", which pointed at the engine and away from the bug.
Show-Problem ("The engine did not answer within three minutes." +
    "`n`nPolled:`nhttp://127.0.0.1:$port/api/health" +
    "`n`nIf that address looks wrong, the launcher is at fault, not the engine." +
    "`n`nLog:`n$LogErr")
exit 1
