# Desktop launcher

Double-click **Green Vision** on the desktop. It starts the engine if it is not
already running, waits until the engine actually answers, and then opens the
studio in the default browser.

| file | what it is |
|---|---|
| `green-vision.ps1` | the launcher: start engine, wait, open browser |
| `green-vision.vbs` | runs the above with no console window (what the shortcut calls) |
| `green-vision.ico` | the app's own mark, rendered from the toolbar SVG at seven sizes |

## Why it waits

A socket that accepts is not an engine that has finished loading. The first
start builds the city panel and can take the better part of a minute, so the
launcher polls `/api/health` and only opens the browser once that returns 200.
Opening sooner shows a connection error and teaches the reader the app is
broken when it is merely still starting.

## Which port it uses

It tries 8000 first and falls back through 8001-8020, in three steps:

1. **Is one of ours already running?** Every candidate port that has a listener
   is asked for `/api/health`, and the reply has to look like this engine -
   `ok: true` and a city. If one answers, the browser opens on that port and
   nothing new is started.
2. **Otherwise, the first port nobody is on** gets a fresh engine.
3. If all twenty-one are busy and none is ours, it says so and stops.

Checking the payload rather than the status code matters. Starting an engine
that cannot bind and then opening a page some other program is serving fails
silently: the app looks stale or broken when it is in fact a different
program's copy of the page. That happened during development, with a second
checkout answering on 8000 while the edits went into this one.

## Recreating the shortcut

If the desktop shortcut is deleted:

```powershell
$repo    = (Resolve-Path "$PSScriptRoot\..").Path
$desktop = [Environment]::GetFolderPath('Desktop')
$sh = New-Object -ComObject WScript.Shell
$s  = $sh.CreateShortcut((Join-Path $desktop 'Green Vision.lnk'))
$s.TargetPath       = "$env:SystemRoot\System32\wscript.exe"
$s.Arguments        = '"' + (Join-Path $repo 'launch\green-vision.vbs') + '"'
$s.WorkingDirectory = $repo
$s.IconLocation     = (Join-Path $repo 'launch\green-vision.ico') + ',0'
$s.Description      = 'Start the Green Vision engine and open the studio'
$s.Save()
```

## Stopping it

The engine keeps running after the browser is closed. To stop it:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*greenplan.server*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Logs are written to `%TEMP%\green-vision.out.log` and `.err.log`.
