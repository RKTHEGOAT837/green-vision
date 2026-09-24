/* =====================================================================
   The bridge — the only thing the studio can see of the desktop
   =====================================================================
   contextIsolation is on and nodeIntegration is off, so the page cannot
   reach Node. What it gets is this object and nothing else: five methods
   and a small set of events. Every one of them is something a browser
   tab genuinely cannot do.

   `window.__GV_DESKTOP__` is also the flag the studio checks to decide
   whether it is running in the desktop app. On the web build it is
   undefined, every desktop branch is skipped, and the page behaves
   exactly as it does today — which is what keeps the login out of the
   web version without maintaining two copies of index.html.
   ===================================================================== */

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

const listeners = {};
function on(channel, fn) {
  (listeners[channel] = listeners[channel] || []).push(fn);
}
function emit(channel, payload) {
  (listeners[channel] || []).forEach(fn => { try { fn(payload); } catch (e) {} });
}

// main → renderer
ipcRenderer.on("gv:signed-in", (_e, user) => emit("signed-in", user));
ipcRenderer.on("gv:auth-error", (_e, msg) => emit("auth-error", msg));
[
  "new", "export", "signin", "account", "assistant", "sources", "about",
  "tab:area", "tab:traffic", "tab:studio", "tab:cost", "tab:review"
].forEach(k => ipcRenderer.on("gv:menu:" + k, () => emit("menu", k)));

contextBridge.exposeInMainWorld("__GV_DESKTOP__", {
  platform: "windows",
  /* The packaged version, from the argument main.js adds. The old value
     came from npm_package_version, which npm sets and an installed app
     never has - so every shipped copy reported "1.0.0" regardless, and an
     update check against it could not tell old from new. */
  version: (process.argv.find(a => a.startsWith("--gv-version=")) || "=1.0.0")
             .split("=")[1] || "1.0.0",

  /* Sign-in. The renderer never sees the session token's storage, the
     endpoint, or the state secret — it asks, and is told what happened. */
  auth: {
    status:   ()               => ipcRenderer.invoke("gv:auth", { type: "status" }),
    request:  (email, profile) => ipcRenderer.invoke("gv:auth", { type: "request", email, profile }),
    signOut:  ()               => ipcRenderer.invoke("gv:auth", { type: "sign-out" }),
    openMail: ()               => ipcRenderer.invoke("gv:auth", { type: "open-mail" })
  },

  /* A real save dialog, so a bill of quantities lands in the folder the
     tender is being assembled in rather than in Downloads. */
  saveFile: (name, data, filters) =>
    ipcRenderer.invoke("gv:save-file", { name, data, filters }),

  reveal: p => ipcRenderer.invoke("gv:reveal", p),

  /* Show a web page in its own window inside the app - Street view, today.
     Deliberately not a general browser: main.js only accepts http(s) and
     gives the window no preload of ours. */
  openWindow: (url, title, width, height) =>
    ipcRenderer.invoke("gv:open-window", { url, title, width, height }),

  notify: (title, body) => ipcRenderer.invoke("gv:notify", { title, body }),

  /* Where this machine is, from Windows' own location service rather than
     from the IP address. Chromium's geolocation cannot work in this build
     without a Google key, so without this the "My location" button could
     only ever offer the city the connection comes from. */
  locate: () => ipcRenderer.invoke("gv:locate"),

  /* Close the app from inside the page. Used by the managed edition's
     sign-in gate, which covers the whole window: an overlay with no way out
     except the title bar is what people force-quit and then distrust. */
  quit: () => ipcRenderer.invoke("gv:quit"),

  on
});
