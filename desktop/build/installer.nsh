; Green Vision — installer additions
;
; Two things electron-builder's default NSIS script does not do for us.
;
; 1. A desktop shortcut that survives an UPDATE.
;
;    `createDesktopShortcut: "always"` covers the normal path, but an update
;    runs over an existing install and the shortcut can still end up missing -
;    which is what happened here: after several updates the machine had Start
;    menu entries and no desktop icon at all, while the download page promised
;    one. This recreates it unconditionally at the end of the install, using
;    the installed executable so the icon is the app's own.
;
; 2. Removing it again on uninstall, which nothing else does once we create
;    it ourselves. Leaving a dead shortcut behind after an uninstall is the
;    kind of litter that makes people distrust an installer.

!macro customInstall
  ; $INSTDIR\${APP_EXECUTABLE_FILENAME} is the real binary, so the shortcut
  ; takes its icon from the exe's own resources - the Green Vision mark that
  ; electron-builder compiled in from build/icon.ico.
  CreateShortCut "$DESKTOP\${SHORTCUT_NAME}.lnk" \
                 "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" \
                 "$INSTDIR\${APP_EXECUTABLE_FILENAME}" 0

  ; Tell the shell the icon cache is stale. Without this Windows can keep
  ; showing a blank or previous icon for a shortcut it has seen before, which
  ; looks exactly like the icon being broken.
  System::Call 'shell32.dll::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
!macroend

!macro customUnInstall
  Delete "$DESKTOP\${SHORTCUT_NAME}.lnk"
  System::Call 'shell32.dll::SHChangeNotify(i 0x08000000, i 0, i 0, i 0)'
!macroend
