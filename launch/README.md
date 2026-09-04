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

## Why it checks the port first

If something else is already on port 8000 the launcher says so and stops,
rather than starting an engine that cannot bind and then opening a page that
something else is serving. That failure is silent and confusing: the app looks
stale or wrong when it is in fact a different program's copy of the page. It
happened during development, with a second checkout answering on 8000 while
the edits went to this one.

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
