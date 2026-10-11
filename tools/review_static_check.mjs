#!/usr/bin/env node
// Open a pcbc static review page (static.html) in headless Chrome with JavaScript OFF, as a phone (375x812 CSS px,
// touch, mobile viewport, DPR 3) and at desktop size (1280x900), and report whether every section shows with its
// pictures decoded.
//
// Why: the owner's phone app shows a review page with scripts off; static.html is the page made for that. This checks it
// the way that viewer shows it: no script runs (Emulation.setScriptExecutionDisabled), and the page's own
// Content-Security-Policy (default-src 'none'; img-src data:) is in force, so a picture that needed the network would
// not decode and fail here.
//
// Usage:
//   node tools/review_static_check.mjs PAGE.html [--shots DIR] [--prefix NAME] [--three 4] [--timeout 90] [--chrome PATH]
// Prints one JSON object; exit status 0 when ALL of these hold, in BOTH viewports:
//   * the document has no <script> element (and none ran), no frame, object or embed,
//   * the status header and every section (sch, front, back, silk, both, three, bom) are there and laid out (a box
//     with height), and each picture section (sch, front, back, silk, both) holds a picture and three holds --three,
//   * every <img> has decoded: complete, naturalWidth > 0 and naturalHeight > 0, and it is drawn with a size,
//   * nothing is wider than the screen: the document's scroll width is the viewport's, and every picture's box lies
//     inside it,
//   * the page made no request except its own document and its own `data:` pictures, and logged no error (a
//     Content-Security-Policy violation is one),
//   * the viewport lets the phone zoom (no user-scalable=no, no maximum-scale below 2).
// Screenshots: DIR/PREFIX-phone.png and DIR/PREFIX-desktop.png (the whole page), DIR/PREFIX-phone-zoom.png (the middle
// of the schematic on the phone drawn at 4x, as a pinch zoom redraws it: an SVG stays sharp at that scale).
// Needs: Node >= 22 (global WebSocket) and Google Chrome (or --chrome / $CHROME). No npm packages. Page reads are
// Runtime.evaluate from DevTools, which runs while the page's own scripts are off.

import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const a = { page: null, shots: null, prefix: "static", three: 4, timeout: 90, chrome: process.env.CHROME || null };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--shots") a.shots = argv[++i];
    else if (k === "--prefix") a.prefix = argv[++i];
    else if (k === "--three") a.three = Number(argv[++i]);
    else if (k === "--timeout") a.timeout = Number(argv[++i]);
    else if (k === "--chrome") a.chrome = argv[++i];
    else if (!a.page) a.page = k;
    else throw new Error(`unexpected argument ${k}`);
  }
  if (!a.page) throw new Error("usage: review_static_check.mjs PAGE.html [--shots DIR]");
  return a;
}

function findChrome(explicit) {
  const c = [explicit, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium", "/usr/bin/google-chrome", "/usr/bin/chromium",
    "/usr/bin/chromium-browser"].filter(Boolean);
  for (const p of c) if (existsSync(p)) return p;
  throw new Error("no Chrome found (pass --chrome or set $CHROME)");
}

class Cdp {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.id = 0; this.pending = new Map(); this.handlers = [];
    this.ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { res, rej } = this.pending.get(msg.id); this.pending.delete(msg.id);
        msg.error ? rej(new Error(`${msg.error.message} ${msg.error.data || ""}`)) : res(msg.result);
      } else if (msg.method) for (const h of this.handlers) h(msg);
    };
  }
  open() { return new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = rej; }); }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
  on(h) { this.handlers.push(h); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PICTURE_SECTIONS = ["sch", "front", "back", "silk", "both"];
const SECTIONS = ["sch", "front", "back", "silk", "both", "three", "bom"];

// Runs in the page through DevTools (the page's own scripts are off).
const READ = `(() => {
  const vw = document.documentElement.clientWidth;
  const box = (el) => { const r = el.getBoundingClientRect(); return { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height }; };
  const sections = {};
  for (const el of document.querySelectorAll("header#status, section[id]")) {
    const imgs = [...el.querySelectorAll("img")].map((i) => ({ complete: i.complete, nw: i.naturalWidth, nh: i.naturalHeight,
      type: (i.getAttribute("src") || "").slice(0, 30).split(";")[0], ...box(i) }));
    sections[el.id] = { display: getComputedStyle(el).display, ...box(el), imgs };
  }
  const meta = document.querySelector('meta[name="viewport"]');
  return JSON.stringify({ vw, scrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
    scripts: document.querySelectorAll("script").length, frames: document.querySelectorAll("iframe, frame, object, embed").length,
    allImgs: document.images.length, viewport: meta ? meta.content : null, sections,
    title: document.title });
})()`;

async function main() {
  const a = parseArgs(process.argv.slice(2));
  const page = resolve(a.page);
  const work = mkdtempSync(join(tmpdir(), "pcbc-static-"));
  const prof = join(work, "profile");
  const chrome = spawn(findChrome(a.chrome), ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${prof}`,
    "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--window-size=1280,900", "about:blank"],
  { stdio: "ignore" });
  const result = { page, bytes: readFileSync(page).length, views: {}, failures: [], ok: false };
  const fail = (msg) => result.failures.push(msg);
  try {
    let port = null;
    for (let i = 0; i < 200 && !port; i++) {
      const f = join(prof, "DevToolsActivePort");
      if (existsSync(f)) port = readFileSync(f, "utf8").split("\n")[0].trim() || null;
      if (!port) await sleep(50);
    }
    if (!port) throw new Error("Chrome did not open a DevTools port");
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const cdp = new Cdp(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
    await cdp.open();
    let loaded = false, requests = [], logs = [];
    cdp.on((m) => {
      const p = m.params;
      if (m.method === "Page.loadEventFired") loaded = true;
      else if (m.method === "Network.requestWillBeSent") requests.push({ url: p.request.url.slice(0, 120), type: p.type });
      else if (m.method === "Runtime.exceptionThrown") logs.push("exception: " + ((p.exceptionDetails.exception || {}).description || p.exceptionDetails.text));
      else if (m.method === "Runtime.consoleAPICalled") logs.push(`console.${p.type}: ` + p.args.map((x) => x.value ?? x.description ?? "").join(" "));
      else if (m.method === "Log.entryAdded" && (p.entry.level === "error" || p.entry.level === "warning"))
        logs.push(`log(${p.entry.source}/${p.entry.level}): ${p.entry.text}`);
    });
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");
    await cdp.send("Network.enable");
    await cdp.send("Emulation.setScriptExecutionDisabled", { value: true });
    if (a.shots) mkdirSync(a.shots, { recursive: true });
    const url = pathToFileURL(page).href;
    const views = [
      { name: "phone", width: 375, height: 812, dpr: 3, mobile: true },
      { name: "desktop", width: 1280, height: 900, dpr: 1, mobile: false },
    ];
    for (const v of views) {
      loaded = false; requests = []; logs = [];
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: v.width, height: v.height, deviceScaleFactor: v.dpr,
        mobile: v.mobile, screenWidth: v.width, screenHeight: v.height });
      await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: v.mobile, maxTouchPoints: v.mobile ? 5 : 1 });
      await cdp.send("Page.navigate", { url });
      const deadline = Date.now() + a.timeout * 1000;
      while (!loaded && Date.now() < deadline) await sleep(100);
      if (!loaded) throw new Error(`${v.name}: the page did not load`);
      await sleep(500);
      const r = JSON.parse((await cdp.send("Runtime.evaluate", { expression: READ, returnByValue: true })).result.value);
      const f = (msg) => fail(`${v.name}: ${msg}`);
      if (r.scripts) f(`the page has ${r.scripts} <script> element(s)`);
      if (r.frames) f(`the page has ${r.frames} frame/object/embed element(s)`);
      if (r.scrollWidth > r.vw + 0.5 || r.bodyScrollWidth > r.vw + 0.5) f(`the page is ${Math.max(r.scrollWidth, r.bodyScrollWidth)}px wide on a ${r.vw}px screen`);
      const vp = r.viewport || "";
      const maxScale = /maximum-scale\s*=\s*([0-9.]+)/.exec(vp);
      if (!vp) f("no viewport meta");
      if (/user-scalable\s*=\s*(no|0)/.test(vp) || (maxScale && Number(maxScale[1]) < 2)) f(`the viewport stops pinch zoom (${vp})`);
      if (!r.sections.status || r.sections.status.h < 20) f("no status header");
      for (const id of SECTIONS) {
        const s = r.sections[id];
        if (!s) { f(`no section ${id}`); continue; }
        if (s.display === "none" || s.h < 20) f(`section ${id} is not laid out (display ${s.display}, height ${s.h})`);
        const want = id === "three" ? a.three : PICTURE_SECTIONS.includes(id) ? 1 : 0;
        if (s.imgs.length < want) f(`section ${id} has ${s.imgs.length} of ${want} pictures`);
      }
      let images = 0;
      for (const [id, s] of Object.entries(r.sections)) for (const i of s.imgs) {
        images++;
        if (!i.complete || !(i.nw > 0) || !(i.nh > 0)) f(`a picture in ${id} did not decode (${i.type}, complete ${i.complete}, natural ${i.nw}x${i.nh})`);
        if (!(i.w > 20) || !(i.h > 10)) f(`a picture in ${id} is drawn ${i.w}x${i.h}`);
        if (i.x < -0.5 || i.x + i.w > r.vw + 0.5) f(`a picture in ${id} spans ${i.x}..${i.x + i.w} on a ${r.vw}px screen`);
      }
      if (images !== r.allImgs) f(`${r.allImgs - images} picture(s) outside the sections`);
      const outside = requests.filter((q) => q.type !== "Document" && !(q.type === "Image" && q.url.startsWith("data:image/")));
      if (outside.length) f(`requests: ${JSON.stringify(outside)}`);
      if (logs.length) f(`logged: ${logs.join(" | ")}`);
      const out = { viewport: [v.width, v.height, v.dpr], scrollWidth: r.scrollWidth, vw: r.vw, scripts: r.scripts, images,
        decoded: Object.values(r.sections).flatMap((s) => s.imgs).filter((i) => i.complete && i.nw > 0).length,
        pageHeight: Math.round(Math.max(...Object.values(r.sections).map((s) => s.y + s.h))),
        sections: Object.fromEntries(Object.entries(r.sections).map(([k, s]) => [k, { h: Math.round(s.h), imgs: s.imgs.map((i) => [i.type, i.nw, i.nh, Math.round(i.w), Math.round(i.h)]) }])),
        requests: requests.length, logs };
      if (a.shots) {
        const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
          clip: { x: 0, y: 0, width: r.vw, height: out.pageHeight, scale: v.mobile ? 0.5 : 1 } });
        out.shot = join(a.shots, `${a.prefix}-${v.name}.png`);
        writeFileSync(out.shot, Buffer.from(shot.data, "base64"));
        if (v.mobile && r.sections.sch && r.sections.sch.imgs[0]) {
          // the middle of the schematic drawn 4x larger, as the phone's own pinch zoom redraws it
          const i = r.sections.sch.imgs[0];
          const z = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
            clip: { x: i.x + i.w / 2 - 47, y: i.y + i.h / 2 - 50, width: 94, height: 100, scale: 4 } });
          out.zoomShot = join(a.shots, `${a.prefix}-phone-zoom.png`);
          writeFileSync(out.zoomShot, Buffer.from(z.data, "base64"));
        }
      }
      result.views[v.name] = out;
    }
    result.ok = result.failures.length === 0;
  } catch (err) {
    result.error = String(err && err.stack || err);
  } finally {
    chrome.kill("SIGKILL");
    await sleep(100);
    try { rmSync(work, { recursive: true, force: true }); } catch {}
  }
  process.stdout.write(JSON.stringify(result, null, 1) + "\n");
  process.exit(result.ok ? 0 : 1);
}

main();
