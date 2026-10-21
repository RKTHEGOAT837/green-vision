# Install or update Green Vision on Windows with one command:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/install-windows.ps1 | iex"
#
# What it does:
#   1. Finds an installed copy (from its uninstall entry) and its version.
#   2. Asks the Green Vision update server which installer is current.
#        not installed       -> downloads and installs it
#        older version       -> downloads and updates it in place
#        already newest      -> skips the 1.6 GB download
#   3. Brings the studio up to the newest published patch, the same way the
#      app does by itself over two restarts: downloads it, checks its SHA-256,
#      stages it in %APPDATA%\Green Vision\patch, and restarts the app so it
#      switches over now.
#   4. Opens Green Vision.
#
# Every download is checked against the SHA-256 the server published;
# nothing is installed or staged if it does not match. A dropped download
# resumes when the command is run again. Saved projects, the account and
# settings are kept.
#
# To reinstall the same version anyway, set GV_FORCE=1 first:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:GV_FORCE='1'; irm https://raw.githubusercontent.com/RKTHEGOAT837/green-vision/main/scripts/install-windows.ps1 | iex"
#
# KEEP THIS FILE ASCII: it is piped through Invoke-Expression, and a stray
# non-ASCII byte decoded in the wrong code page breaks the whole script.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Say($m)  { Write-Host "==> $m" -ForegroundColor Green }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "xx  $m" -ForegroundColor Red; throw "Green Vision setup stopped." }

function Get-GvVersion($s) {
  try { return [version](($s -replace '[^0-9.]', '').Trim('.')) } catch { return [version]"0.0" }
}

# The installed copy, from the uninstall entry the installer writes.
function Find-GreenVision {
  foreach ($root in "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
                    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
                    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall") {
    if (-not (Test-Path $root)) { continue }
    foreach ($k in Get-ChildItem $root -ErrorAction SilentlyContinue) {
      $d = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
      if ($d.DisplayName -notlike "Green Vision*") { continue }
      # The installer leaves InstallLocation empty, so the folder is read
      # from the uninstaller's own path, then the icon's, as a fallback.
      $loc = $d.InstallLocation
      if (-not $loc -and $d.UninstallString -match '"?([A-Za-z]:\\[^"]+?)\\[^\\"]+\.exe') { $loc = $Matches[1] }
      if (-not $loc -and $d.DisplayIcon) { $loc = Split-Path (($d.DisplayIcon -split ',')[0].Trim('"')) -Parent }
      if (-not $loc -or -not (Test-Path $loc)) { continue }
      $exe = Get-ChildItem $loc -Filter *.exe -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -notlike "Uninstall*" -and $_.Name -notlike "elevate*" } |
             Select-Object -First 1
      if ($exe) {
        $v = $d.DisplayVersion
        if (-not $v) { $v = $exe.VersionInfo.ProductVersion }
        return [pscustomobject]@{ Version = $v; Exe = $exe.FullName; Dir = $loc
                                  AllUsers = ($root -like "HKLM:*" -or $d.UninstallString -match '/allusers') }
      }
    }
  }
  return $null
}

function Stop-GreenVision {
  $p = Get-Process -Name "GreenVision" -ErrorAction SilentlyContinue
  if ($p) {
    Say "Closing Green Vision..."
    $p | ForEach-Object { try { $_.CloseMainWindow() | Out-Null } catch {} }
    Start-Sleep -Seconds 3
    Get-Process -Name "GreenVision" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    # Its engine is a child Python process; make sure the port is freed too.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -like "*greenplan.server*" } |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    return $true
  }
  return $false
}

function Write-Utf8NoBom($path, $text) {
  [IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Invoke-GvSetup {
  if ([Environment]::OSVersion.Platform -ne "Win32NT") { Fail "This is for Windows. On a Mac use install-mac.sh." }
  if (-not [Environment]::Is64BitOperatingSystem) { Fail "Green Vision needs 64-bit Windows." }

  $base = "https://green-vision-api.greenvision-rk.workers.dev"
  $force = ($env:GV_FORCE -eq "1")

  # ---- 1. what is here -----------------------------------------------------
  $have = Find-GreenVision
  if ($have) { Say "Green Vision $($have.Version) is installed at $($have.Dir)" }
  else       { Say "Green Vision is not installed yet." }

  # ---- 2. what is current --------------------------------------------------
  Say "Asking the update server for the newest version..."
  try { $n = Invoke-RestMethod -Uri "$base/app/notice?edition=open&platform=windows" -Headers @{ "Cache-Control" = "no-cache" } -TimeoutSec 30 }
  catch { Fail "Could not reach the update server. Check the internet connection." }
  if (-not $n.file -or -not $n.sha256 -or -not $n.latest) { Fail "The update server did not name an installer." }
  $latest = $n.latest

  $needInstall = $true
  if ($have -and -not $force -and ((Get-GvVersion $have.Version) -ge (Get-GvVersion $latest))) {
    $needInstall = $false
    Say "The app is already the newest version ($latest) - no download needed."
  }

  $wasRunning = $false
  if ($needInstall) {
    $url  = $n.file
    $want = $n.sha256.ToLower()
    $dir  = Join-Path $env:TEMP "GreenVisionInstall"
    $exe  = Join-Path $dir (Split-Path $url -Leaf)
    New-Item -ItemType Directory -Force $dir | Out-Null

    $ok = $false
    if (Test-Path $exe) { $ok = ((Get-FileHash $exe -Algorithm SHA256).Hash.ToLower() -eq $want) }
    if (-not $ok) {
      if ($have) { Say "Updating from $($have.Version) to $latest - downloading $($n.size)..." }
      else       { Say "Downloading Green Vision $latest ($($n.size)) - this can take a while..." }
      $curl = Join-Path $env:SystemRoot "System32\curl.exe"
      if (Test-Path $curl) {
        & $curl -L --fail --retry 5 --retry-delay 3 -C - -o $exe $url
        if ($LASTEXITCODE -ne 0) { Fail "The download did not finish. Run the command again to resume it." }
      } else {
        Start-BitsTransfer -Source $url -Destination $exe
      }
      Say "Checking the download..."
      if ((Get-FileHash $exe -Algorithm SHA256).Hash.ToLower() -ne $want) {
        Remove-Item $exe -Force -ErrorAction SilentlyContinue
        Fail "Checksum mismatch: the download is not the published installer. Nothing was changed."
      }
    } else {
      Say "Green Vision $latest is already downloaded and verified."
    }

    $wasRunning = Stop-GreenVision
    if ($have) { Say "Updating Green Vision to $latest..." } else { Say "Installing Green Vision $latest..." }
    # /S is a silent install. An update goes into the folder the copy already
    # lives in, so shortcuts and the uninstall entry stay where they were.
    # /D= must be last and unquoted (NSIS reads the rest of the line), so the
    # arguments go as one string rather than an array that would quote it.
    $instArgs = "/S"
    $elevate = $false
    if ($have) {
      # Same scope as the existing copy (all users or just this one), so the
      # update replaces it instead of installing a second copy beside it.
      $scope = if ($have.AllUsers) { "/allusers" } else { "/currentuser" }
      $instArgs = "/S $scope /D=$($have.Dir.TrimEnd('\'))"
      # A copy in Program Files needs an administrator to replace it.
      if ($have.AllUsers) { $elevate = $true }
      $probe = Join-Path $have.Dir ".gv-write-test"
      try { [IO.File]::WriteAllText($probe, "x"); Remove-Item $probe -Force } catch { $elevate = $true }
      if ($elevate) { Warn "Green Vision is installed for all users; Windows will ask for permission to update it." }
    }
    if ($elevate) { $p = Start-Process -FilePath $exe -ArgumentList $instArgs -Wait -PassThru -Verb RunAs }
    else          { $p = Start-Process -FilePath $exe -ArgumentList $instArgs -Wait -PassThru }
    if ($p.ExitCode -ne 0) { Fail "The installer stopped with code $($p.ExitCode)." }
    Remove-Item $exe -Force -ErrorAction SilentlyContinue

    $have = Find-GreenVision
    if (-not $have) { Fail "The installer finished but Green Vision could not be found. Open it from the Start menu." }
    Say "Green Vision $($have.Version) is installed."
  }

  # ---- 3. the newest studio patch -------------------------------------------
  $staged = $false
  try {
    $pt = (Invoke-RestMethod -Uri "$base/app/patch?v=$($have.Version)&platform=windows&nc=$(Get-Random)" -Headers @{ "Cache-Control" = "no-cache" } -TimeoutSec 30).patch
  } catch { $pt = $null; Warn "Could not ask for a studio update; the app will check by itself." }

  if ($pt -and $pt.version -and $pt.url -and $pt.sha256) {
    $pdir  = Join-Path $env:APPDATA "Green Vision\patch"
    $state = Join-Path $pdir "state.json"
    $st = $null
    if (Test-Path $state) { try { $st = Get-Content $state -Raw | ConvertFrom-Json } catch { $st = $null } }
    $active = if ($st -and $st.active) { $st.active.version } else { $null }
    $already = if ($st -and $st.staged) { $st.staged.version } else { $null }

    if ($active -and (Get-GvVersion $active) -ge (Get-GvVersion $pt.version) -and -not $force) {
      Say "The studio is already the newest ($active)."
    } elseif ($already -eq $pt.version -and (Test-Path (Join-Path $pdir "$($pt.version)\index.html"))) {
      Say "Studio $($pt.version) is already downloaded; it applies when the app starts."
      $staged = $true
    } else {
      Say "Downloading studio update $($pt.version)..."
      $vdir = Join-Path $pdir $pt.version
      New-Item -ItemType Directory -Force $vdir | Out-Null
      $part = Join-Path $vdir "index.html.part"
      Invoke-WebRequest -Uri $pt.url -OutFile $part -UseBasicParsing -TimeoutSec 120
      if ((Get-FileHash $part -Algorithm SHA256).Hash.ToLower() -ne $pt.sha256.ToLower()) {
        Remove-Item $part -Force -ErrorAction SilentlyContinue
        Warn "The studio update did not match its published checksum; skipped it. The app will retry by itself."
      } else {
        Move-Item $part (Join-Path $vdir "index.html") -Force
        # Recorded exactly as the app's patch.js records a verified patch, so
        # the next start promotes it. Written without a BOM: the app parses
        # this file with JSON.parse, which rejects one.
        if (-not $st) { $st = [pscustomobject]@{} }
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        $stg = [pscustomobject]@{
          version = $pt.version; url = $pt.url; sha256 = $pt.sha256.ToLower()
          bytes = $pt.bytes; min_app = $pt.min_app; notes = $pt.notes; at = $now
        }
        $st | Add-Member -NotePropertyName staged   -NotePropertyValue $stg -Force
        $st | Add-Member -NotePropertyName failures -NotePropertyValue 0    -Force
        if ($st.PSObject.Properties["rolledBack"]) { $st.PSObject.Properties.Remove("rolledBack") }
        Write-Utf8NoBom $state ($st | ConvertTo-Json -Depth 6)
        Say "Studio $($pt.version) verified and staged."
        $staged = $true
      }
    }
  }

  # ---- 4. open it (restarting it if a studio update is waiting) ---------------
  if ($staged) { if (Stop-GreenVision) { $wasRunning = $true } }
  if (-not (Get-Process -Name "GreenVision" -ErrorAction SilentlyContinue)) {
    Say "Opening Green Vision... (the first start takes a little longer while the engine loads)"
    Start-Process -FilePath $have.Exe
  }
  Say "Done. Run the same command any time to update."
}

try { Invoke-GvSetup } catch { if ($_.Exception.Message -ne "Green Vision setup stopped.") { Write-Host "xx  $($_.Exception.Message)" -ForegroundColor Red } }
