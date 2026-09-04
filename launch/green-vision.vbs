' Runs the launcher with no console window.
'
' A .ps1 cannot be the target of a shortcut without a visible powershell
' window flashing up and staying open behind the browser. This wrapper starts
' it hidden (the 0) and does not wait (the False), so the shortcut returns
' immediately and the launcher gets on with waiting for the engine.
Option Explicit
Dim shell, fso, here, ps1
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ps1   = here & "\green-vision.ps1"
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """", 0, False
