/* Render the brand SVGs to PNG at the sizes people actually ask for.

   Electron is the rasteriser because it is already a dependency of the
   desktop build and renders with the same engine the app does — so the
   exported logo is what the product shows, webfonts included.

   Run from desktop/ so it can find Electron:
       cd desktop && ./node_modules/.bin/electron ../brand/render.js

   One window, reused. Creating a fresh offscreen BrowserWindow per size
   rendered the first and then failed every subsequent load with
   ERR_FAILED; resizing a single window sidesteps that and is faster.
*/

"use strict";

const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

const DIR = __dirname;
const TMP = path.join(DIR, ".render.tmp.html");

// Sora and Manrope are what the app uses. The SVG only names them, so they
// are pulled in here to make the wordmark render correctly.
const FONTS = "https://fonts.googleapis.com/css2?family=Sora:wght@800&family=Manrope:wght@700&display=swap";

const JOBS = [
  /* 48, 32 and 16 are here for the Windows icon rather than for anyone to
     ask for directly: an .ico that downscales one large bitmap to 16px is
     visibly softer than one carrying a 16px render. */
  { svg: "green-vision-mark.svg",        out: "green-vision-mark",        sizes: [1024, 512, 256, 128, 64, 48, 32, 16] },
  { svg: "green-vision-lockup.svg",      out: "green-vision-lockup",      sizes: [1600, 800, 400] },
  { svg: "green-vision-lockup-dark.svg", out: "green-vision-lockup-dark", sizes: [1600, 800, 400], dark: true }
];

app.disableHardwareAcceleration();

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 1024, height: 1024, show: false, frame: false,
    transparent: true, backgroundColor: "#00000000",
    webPreferences: { offscreen: true }
  });

  let made = 0;
  for (const job of JOBS) {
    const svg = fs.readFileSync(path.join(DIR, job.svg), "utf8")
                  .replace(/\swidth="\d+"\s+height="\d+"/, "");
    const vb = svg.match(/viewBox="0 0 ([\d.]+) ([\d.]+)"/);
    const ratio = parseFloat(vb[2]) / parseFloat(vb[1]);

    for (const w of job.sizes) {
      const h = Math.round(w * ratio);
      try {
        fs.writeFileSync(TMP, `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="${FONTS}">
<style>html,body{margin:0;padding:0;width:${w}px;height:${h}px;
  background:${job.dark ? "#0d1a17" : "transparent"};overflow:hidden}
  svg{display:block;width:${w}px;height:${h}px}</style></head>
<body>${svg}</body></html>`, "utf8");

        win.setContentSize(w, h);
        await win.loadFile(TMP);
        await new Promise(r => setTimeout(r, 1500));   // webfont + layout

        const img = await win.webContents.capturePage();
        const file = path.join(DIR, `${job.out}-${w}.png`);
        fs.writeFileSync(file, img.toPNG());
        const kb = (fs.statSync(file).size / 1024).toFixed(0);
        console.log(`  ${path.basename(file).padEnd(34)} ${w}x${h}  ${kb} KB`);
        made++;
      } catch (err) {
        console.error("  FAILED " + job.out + "-" + w + ": " + (err && err.message));
      }
    }
  }

  try { fs.unlinkSync(TMP); } catch (e) {}
  console.log(`\n${made} PNGs written to brand/`);

  /* THE WINDOWS ICON, from the same render as everything else.

     It used to be made by hand somewhere else, which is exactly how it came
     to be the one file still showing the previous mark after the logo was
     updated everywhere. Built here, it cannot drift again.

     An .ico is a small container and nothing more: a 6-byte header, a 16-byte
     directory entry per image, then the payloads. Windows has read PNG-
     compressed entries since Vista, so the PNGs just written go in verbatim -
     no re-encoding, no image library. 256 is stored as 0 in the directory,
     which is the format's way of saying "256". */
  try {
    const ICO_SIZES = [16, 32, 48, 64, 128, 256];
    const imgs = ICO_SIZES.map(s => ({
      size: s, buf: fs.readFileSync(path.join(DIR, `green-vision-mark-${s}.png`))
    }));
    const header = Buffer.alloc(6);
    header.writeUInt16LE(0, 0);              // reserved
    header.writeUInt16LE(1, 2);              // 1 = icon
    header.writeUInt16LE(imgs.length, 4);

    let offset = 6 + imgs.length * 16;
    const dir = [];
    for (const im of imgs) {
      const e = Buffer.alloc(16);
      e.writeUInt8(im.size >= 256 ? 0 : im.size, 0);   // width
      e.writeUInt8(im.size >= 256 ? 0 : im.size, 1);   // height
      e.writeUInt8(0, 2);                               // palette
      e.writeUInt8(0, 3);                               // reserved
      e.writeUInt16LE(1, 4);                            // colour planes
      e.writeUInt16LE(32, 6);                           // bits per pixel
      e.writeUInt32LE(im.buf.length, 8);
      e.writeUInt32LE(offset, 12);
      offset += im.buf.length;
      dir.push(e);
    }
    const ico = path.join(DIR, "green-vision.ico");
    fs.writeFileSync(ico, Buffer.concat([header, ...dir, ...imgs.map(i => i.buf)]));
    console.log(`  green-vision.ico                   ${ICO_SIZES.join(", ")}  ` +
                `${(fs.statSync(ico).size / 1024).toFixed(0)} KB`);
  } catch (err) {
    console.error("  FAILED green-vision.ico: " + (err && err.message));
  }
  app.exit(0);
});
