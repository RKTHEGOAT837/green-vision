/* =====================================================================
   The Windows-specific half
   =====================================================================
   Window state, the application menu, and the taskbar Jump List. None of
   this changes what the studio computes; it is the difference between a
   web page in a frame and something that behaves like an installed
   Windows application.
   ===================================================================== */

"use strict";

const { app, Menu, shell, screen } = require("electron");
const fs = require("fs");
const path = require("path");

const DEFAULTS = { width: 1440, height: 900, maximized: false };

function stateFile() { return path.join(app.getPath("userData"), "window.json"); }

/* Restore where the reader left it — but only if that place still
   exists. A window remembered on a second monitor that has since been
   unplugged is a window the reader cannot see, and "the app opens to
   nothing" is indistinguishable from "the app is broken". */
function readWindowState() {
  let s;
  try { s = JSON.parse(fs.readFileSync(stateFile(), "utf8")); }
  catch (e) { return { ...DEFAULTS }; }

  const out = {
    width: Math.max(940, s.width || DEFAULTS.width),
    height: Math.max(620, s.height || DEFAULTS.height),
    maximized: !!s.maximized
  };
  if (Number.isFinite(s.x) && Number.isFinite(s.y)) {
    const visible = screen.getAllDisplays().some(d => {
      const b = d.workArea;
      return s.x < b.x + b.width && s.x + 200 > b.x &&
             s.y < b.y + b.height && s.y + 100 > b.y;
    });
    if (visible) { out.x = s.x; out.y = s.y; }
  }
  return out;
}

function saveWindowState(win) {
  if (!win || win.isDestroyed()) return;
  try {
    // getNormalBounds, not getBounds: saving a maximized window's bounds
    // makes "restore down" restore to full screen, which looks broken.
    const b = win.getNormalBounds();
    fs.writeFileSync(stateFile(),
      JSON.stringify({ ...b, maximized: win.isMaximized() }), "utf8");
  } catch (e) { /* a window position is not worth an error dialog */ }
}

/* ---------- Jump List: the designs you exported recently ---------- */
const RECENT_MAX = 8;

function addRecent(filePath) {
  try {
    app.addRecentDocument(filePath);
    const f = path.join(app.getPath("userData"), "recent.json");
    let list = [];
    try { list = JSON.parse(fs.readFileSync(f, "utf8")); } catch (e) {}
    list = [filePath, ...list.filter(p => p !== filePath)].slice(0, RECENT_MAX);
    fs.writeFileSync(f, JSON.stringify(list), "utf8");
  } catch (e) {}
}

/* ---------- menu ---------- */
function buildMenu(win, { isDev }) {
  const send = ch => () => win && win.webContents.send(ch);

  const template = [
    {
      label: "&File",
      submenu: [
        { label: "New design", accelerator: "Ctrl+N", click: send("gv:menu:new") },
        { label: "Export bill of quantities…", accelerator: "Ctrl+S", click: send("gv:menu:export") },
        { type: "separator" },
        { label: "Print / save as PDF…", accelerator: "Ctrl+P",
          click: () => win && win.webContents.print({ silent: false, printBackground: true }) },
        { type: "separator" },
        { role: "quit", label: "Exit" }
      ]
    },
    {
      label: "&Edit",
      submenu: [
        { role: "undo" }, { role: "redo" }, { type: "separator" },
        { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" }
      ]
    },
    {
      label: "&View",
      submenu: [
        { label: "Area", accelerator: "Ctrl+1", click: send("gv:menu:tab:area") },
        { label: "Traffic", accelerator: "Ctrl+2", click: send("gv:menu:tab:traffic") },
        { label: "Studio", accelerator: "Ctrl+3", click: send("gv:menu:tab:studio") },
        { label: "Cost", accelerator: "Ctrl+4", click: send("gv:menu:tab:cost") },
        { label: "Review", accelerator: "Ctrl+5", click: send("gv:menu:tab:review") },
        { type: "separator" },
        { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
        ...(isDev ? [{ type: "separator" }, { role: "toggleDevTools" }, { role: "reload" }] : [])
      ]
    },
    {
      label: "&Account",
      submenu: [
        { label: "Sign in…", click: send("gv:menu:signin") },
        { label: "Your account", click: send("gv:menu:account") }
      ]
    },
    {
      label: "&Help",
      submenu: [
        { label: "Open the assistant", accelerator: "Ctrl+K", click: send("gv:menu:assistant") },
        { type: "separator" },
        { label: "Where the numbers come from", click: send("gv:menu:sources") },
        { label: "Green Vision on the web",
          click: () => shell.openExternal("https://green-vision-india.netlify.app/") },
        { type: "separator" },
        { label: "About Green Vision", click: send("gv:menu:about") }
      ]
    }
  ];
  return Menu.buildFromTemplate(template);
}

module.exports = { readWindowState, saveWindowState, buildMenu, addRecent };
