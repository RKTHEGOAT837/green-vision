# Green Vision — Windows desktop edition

The same studio as the website, in a window that can be signed in to.

```
desktop/
  main.js       window, protocol handler, single-instance lock, IPC
  preload.js    the only surface the page can see of the desktop
  auth.js       the magic-link flow and the state check
  accounts.js   the local account + session store (DPAPI via safeStorage)
  windows.js    window state, application menu, taskbar Jump List
  build/        icon.ico
```

The renderer loads `../dist_app/index.html` — the identical bundle the web
build serves. **Nothing about the studio is reimplemented here.** A fix in
`index.html` reaches the desktop by rebuilding the bundle, never by porting.

---

## Run it

```bash
cd desktop
npm install
npm start
```

## Build the installer

```bash
npm run dist        # -> desktop/release/GreenVision-1.0.0-x64.exe  (~79 MB)
```

That runs `pack.js` and then wraps the result in an NSIS installer.

**Why not electron-builder end to end?** It insists on unpacking a
code-signing bundle that contains macOS symlinks, and creating a symlink on
Windows needs administrator rights or Developer Mode. The build dies there —
on a step this project does not use, because nothing here is code-signed.

`pack.js` assembles the runnable app directly (Electron runtime +
`resources/app` + `resources/studio`), stamps the icon and version strings
with `rcedit`, and `--prepackaged` hands that to the installer step. The
config sets `win.signAndEditExecutable: false` so builder does not reach for
the bundle at all.

`pack.js` alone also gives you a portable build — copy
`release/GreenVision-win32-x64/` anywhere and run `GreenVision.exe`.

`extraResources` copies `../dist_app` into the package, so rebuild that first
if the studio changed:

```bash
python scripts/build_static.py --out dist_app \
    --cities config/city.yaml config/bengaluru.yaml config/chennai.yaml \
             config/delhi.yaml config/mumbai.yaml
```

---

## What the desktop adds

| | |
|---|---|
| **Works offline** | The baked engine ships inside the app. Ranking, forecast, soil and species need no network. Air quality, canopy and the OSM census are read live when you are online, and every figure says which it is. |
| **Real accounts** | An emailed sign-in link that returns to *this window* through `greenvision://`, not to a browser tab. |
| **Native save** | The bill of quantities goes through the Windows save dialog into the folder the tender is being assembled in — not into Downloads. It is added to the taskbar Jump List. |
| **Window memory** | Reopens where you left it, and refuses to reopen onto a monitor that is no longer attached. |
| **Menus and shortcuts** | `Ctrl+1…5` for the dock tabs, `Ctrl+K` for the assistant, `Ctrl+S` to export, `Ctrl+P` to print or save as PDF. |
| **Notifications** | A Windows toast when a long read finishes behind the app. |

## Verified on a real install

- installs to `%LOCALAPPDATA%\Programs\Green Vision`, ~277 MB
- Start Menu and Desktop shortcuts, and an entry in Apps & Features
- `greenvision://` registered to the **installed** exe
- a `greenvision://` link reaches the **running** window: no second copy
  starts, the single-instance lock holds
- an unsolicited callback is refused, because the app did not issue that state

Registration happens at runtime, so whichever copy ran last owns the scheme.
If you run a dev build and then delete it, run the installed app once to take
the handler back.

## What signing in gives you

Stated in the sign-in sheet before you sign in, and read from `accounts.js`
so the list cannot drift from what the app actually gates:

Working now:

- your name and organisation on published designs and on the exported BOQ
- named exports — the BOQ CSV is stamped with who produced it

Waiting on a gallery server (`COMMUNITY_URL`), and shown greyed with a
**"not set up yet"** pill until there is one:

- publish a design to the shared Library — without a server this saves to
  this PC instead, and the app says so
- designs and history following the account to another machine

Each benefit carries what it needs, so the sheet cannot promise something the
app does not deliver.

Everything else — the studio, the costing, the review, the 3D builder — works
signed out. The app says so on the sign-in sheet rather than implying a
paywall that does not exist.

**The web build has no login.** The whole desktop layer is behind
`window.__GV_DESKTOP__`, which only the Electron preload defines, so the
hosted site keeps its device-only sign-in and gains nothing. One `index.html`,
two behaviours.

---

## Setting up sign-in (Pipedream)

Sign-in is off until you point the app at a Pipedream deployment. Without it
the app runs exactly as it does now and the sign-in sheet says so.

### 1. The Google account

Make the Green Vision Google account and use it for the Gmail connection in
step 3. Everything the reader sees comes from that address.

### 2. Data store

Pipedream → **Data Stores** → new store, call it `greenvision`. Both workflows
share it.

### 3. Workflow A — send the link

New workflow, trigger **HTTP / Webhook**, response mode
**"Return a custom response from your workflow"**.

Add one Node.js code step and paste `pipedream/01-auth-request.js`. Then:

- connect the **data store** to the step as the prop `db`
- connect the **Gmail** account as the prop `gmail`
- workflow **Settings → Environment Variables**:
  - `GV_FROM_EMAIL` — the Green Vision Gmail address
  - `GV_VERIFY_URL` — workflow B's trigger URL (fill in after step 4)

### 4. Workflow B — the click and the exchange

New workflow, same trigger type and response mode. One code step, paste
`pipedream/02-auth-verify-and-exchange.js`, connect the **same** data store as
`db`. Copy this workflow's trigger URL back into `GV_VERIFY_URL` on workflow A.

### 5. Point the app at it

`GV_AUTH_URL` is read by `desktop/auth.js` at launch. Set it to **workflow A's**
trigger URL:

```powershell
setx GV_AUTH_URL "https://<workflow-a>.m.pipedream.net"
```

For a shipped build, set it in the build environment before `npm run dist`.
It is deliberately not committed: the repository must not carry a live
endpoint, and the web build must never gain one.

### Paths

Both workflows key off the request path, so a single trigger URL serves all
three:

| method | path | who calls it |
|---|---|---|
| `POST` | `/auth/request` | the app, when you ask for a link |
| `GET` | `/auth/verify?token=` | the button in the email |
| `POST` | `/auth/exchange` | the app, redeeming the token |

---

## How the sign-in actually works

1. The app generates a random `state`, keeps it in memory, and POSTs it with
   your email and signup profile.
2. Pipedream stores a one-time token for fifteen minutes and emails the link.
3. You click. The browser opens Pipedream, which hands the browser on to
   `greenvision://auth?token=…&state=…`.
4. Windows routes that to the **running** app — the single-instance lock in
   `main.js` is what stops a second copy starting and signing in instead.
5. The app checks the `state` came back unchanged, then exchanges the token
   for a session in the **main process**. The token never enters the page.

### Why `state` is not optional

The protocol handler is a public door: any web page you visit can send
`greenvision://auth?token=…` to this app. Without the state check the app
would accept a token an attacker minted for **their own** account, silently
signing you into it — and every design you then published would go to their
library. The state is generated on your machine, leaves it only in the request
that starts the flow, and a callback that does not carry it back is dropped.

### Why the email link does not spend the token

Outlook Safe Links, corporate mail filters and antivirus all follow links
before a human does. If the `GET` consumed the token those scanners would burn
it and you would click a dead link. The `GET` only hands the token onward;
nothing is spent until the app itself `POST`s, which no scanner does.

### Where the session is kept

`%APPDATA%/Green Vision/account.json`, with the session encrypted through
Electron's `safeStorage` — DPAPI on Windows, so it is bound to your Windows
user and useless if the file is copied elsewhere. If DPAPI is unavailable the
session is held in memory for the run and **not** written in the clear; you
sign in again next launch. A token at rest in plain text on a shared municipal
machine is worse than an extra sign-in.

The profile — name, organisation, role, city, state — is stored beside it in
clear. It is what you typed about yourself and it is shown straight back to
you, so encrypting it would buy nothing and make the file impossible to
inspect.
