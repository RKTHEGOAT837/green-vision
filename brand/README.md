# Green Vision — brand files

The mark is lifted from `index.html` unchanged, so the logo files and the
running product cannot drift apart. If the app's header changes, re-copy it
and re-run the renderer.

## Files

| File | Use |
|---|---|
| `green-vision-mark.svg` | The mark alone. Scales to anything — favicons, app icons, stickers. |
| `green-vision-lockup.svg` | Mark + wordmark + strapline, for light backgrounds. |
| `green-vision-lockup-dark.svg` | The same for dark backgrounds. |
| `green-vision-mark-{64…1024}.png` | Transparent PNGs of the mark. |
| `green-vision-lockup-{400,800,1600}.png` | Transparent PNGs of the lockup. |
| `green-vision-lockup-dark-*.png` | On the app's own dark ground (`#0d1a17`). |
| `green-vision.ico` | Windows icon, 16→256 px. Already used by the desktop app. |

Prefer the SVG wherever the medium allows it. The PNGs exist because the
wordmark is live text with a font stack, not outlined paths — these were
rendered here with the right fonts so a machine without Sora installed still
shows the real thing.

## Colours

| | Hex | Where |
|---|---|---|
| Gradient start | `#12b981` | Top-left of the disc |
| Gradient end | `#2f7ef0` | Bottom-right of the disc |
| Brand green | `#0e9f6e` | The branch veins, primary buttons |
| Ink | `#0f2a22` | Wordmark on light |
| Ink soft | `#5c7168` | Strapline on light |
| Ground | `#0d1a17` | The app's dark background |

## What it means

The disc runs green to blue: canopy and water, the two things the tool
weighs. The white shape is a leaf and a map pin at once — what to plant, and
where. The veins are the branching the ranking produces: one trunk, decisions
coming off it.

## Re-rendering

```bash
cd desktop && ./node_modules/.bin/electron ../brand/render.js
```

Electron is the rasteriser because it is already a dependency and renders
with the same engine the app does, webfonts included.
