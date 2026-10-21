# Install (or update) Green Vision on Windows with one command:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/install-windows.ps1 | iex"
#
# Asks the Green Vision update server which installer is current, downloads
# it with the curl.exe that ships with Windows 10/11 (resumable - run the
# command again if the connection drops), checks its SHA-256 against the
# published value, installs it silently for the current user and opens it.
# Nothing is installed if the checksum does not match.
#
# KEEP THIS FILE ASCII: it is piped through Invoke-Expression, and a stray
# non-ASCII byte decoded in the wrong code page breaks the whole script.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Say($m)  { Write-Host "==> $m" -ForegroundColor Green }
function Fail($m) { Write-Host "xx  $m" -ForegroundColor Red; exit 1 }

if ([Environment]::OSVersion.Platform -ne "Win32NT") { Fail "This is for Windows." }
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Green Vision needs 64-bit Windows." }

$api = "https://green-vision-api.greenvision-rk.workers.dev/app/notice?edition=open&platform=windows"
Say "Asking the update server for the current Green Vision installer..."
try { $n = Invoke-RestMethod -Uri $api -Headers @{ "Cache-Control" = "no-cache" } -TimeoutSec 30 }
catch { Fail "Could not reach the update server. Check the internet connection." }
if (-not $n.file -or -not $n.sha256) { Fail "The update server did not name an installer." }

$ver  = $n.latest
$url  = $n.file
$want = $n.sha256.ToLower()
$name = Split-Path $url -Leaf
$dir  = Join-Path $env:TEMP "GreenVisionInstall"
$exe  = Join-Path $dir $name
New-Item -ItemType Directory -Force $dir | Out-Null

$have = $false
if (Test-Path $exe) { $have = ((Get-FileHash $exe -Algorithm SHA256).Hash.ToLower() -eq $want) }
if (-not $have) {
  Say "Downloading Green Vision $ver ($($n.size)) - this can take a while..."
  $curl = Join-Path $env:SystemRoot "System32\curl.exe"
  if (Test-Path $curl) {
    & $curl -L --fail --retry 5 --retry-delay 3 -C - -o $exe $url
    if ($LASTEXITCODE -ne 0) { Fail "The download did not finish. Run the command again to resume it." }
  } else {
    Start-BitsTransfer -Source $url -Destination $exe
  }
  Say "Checking the download..."
  $got = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $want) {
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
    Fail "Checksum mismatch: the download is not the published installer. Nothing was installed."
  }
} else {
  Say "Green Vision $ver is already downloaded and verified."
}

Say "Closing Green Vision if it is open..."
Get-Process -Name "GreenVision" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Say "Installing Green Vision $ver..."
$p = Start-Process -FilePath $exe -ArgumentList "/S" -Wait -PassThru
if ($p.ExitCode -ne 0) { Fail "The installer stopped with code $($p.ExitCode)." }

# Find where it went, from the uninstall entry the installer writes.
$app = $null
foreach ($root in "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
                  "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
                  "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall") {
  if (-not (Test-Path $root)) { continue }
  foreach ($k in Get-ChildItem $root) {
    $d = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
    if ($d.DisplayName -like "Green Vision*" -and $d.InstallLocation) {
      $c = Get-ChildItem $d.InstallLocation -Filter *.exe -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -notlike "Uninstall*" } | Select-Object -First 1
      if ($c) { $app = $c.FullName; break }
    }
  }
  if ($app) { break }
}

if ($app) {
  Say "Opening Green Vision..."
  Start-Process -FilePath $app
  Say "Done: Green Vision $ver is installed."
} else {
  Say "Done: Green Vision $ver is installed. Open it from the Start menu."
}
Remove-Item $exe -Force -ErrorAction SilentlyContinue
