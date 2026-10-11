#!/usr/bin/env node
// Load a pcbc review page in headless Chrome inside a MODELLED HOST and report whether its 3D tab works without the
// page making any request.
//
// Why: Claude's desktop file pane showed the 3D tab as
//   "Error: DataCloneError: Failed to execute 'postMessage' on 'Window': Request object could not be cloned."
// The host replaces the page's `fetch` with one that forwards its arguments to the parent window by postMessage, and
// the old viewer loaded its model with `fetch(new Request(url))`: a Request is not structured-cloneable. The host's own
// code is not on this machine, and how it installs its fetch (assigned, redefined, frozen, late, on load, re-installed)
// is unknown, so the page must not use fetch at all. This harness models such hosts:
//   * a parent page holding the review page in <iframe sandbox="allow-scripts" srcdoc=...> (opaque origin),
//   * a strict CSP on the parent, inherited by the srcdoc frame: no network, inline scripts only, connect-src 'none',
//   * a host fetch that does parent.postMessage({args}, "*") and rejects, installed in one of many ways (--host).
// and also opens the page as a plain file:// and from http://localhost with no host at all.
//
// Hosts (--host):
//   file                 plain Chrome, the page opened as file://
//   http                 plain Chrome, the page served from http://localhost:<port>/ by this script
//   noshim               modelled host (sandbox + CSP) without a host fetch
//   shim-early           host fetch ASSIGNED before any of the page's scripts (first thing in <head>)
//   shim-late            host fetch assigned after all of the page's scripts (a module script appended last)
//   shim-late-fallback   as shim-late, falling back to the fetch it found when the parent fails
//   shim-late-proxy      a Proxy around the fetch it found, assigned after the page's scripts
//   shim-onload          assigned in a window 'load' listener (before the 3D tab is opened)
//   shim-reinstall       assigned early and re-assigned every 20 ms whenever window.fetch is something else
//   shim-getter          early Object.defineProperty accessor (configurable) that always returns the host fetch
//   shim-getter-nonconfig  the same, non-configurable
//   shim-frozen          early defineProperty, value, non-writable, non-configurable
//   shim-define-onload   defineProperty (value, configurable) in a window 'load' listener
//   shim-define-late     defineProperty (value, configurable) after the page's scripts
//   getter-counts        early non-configurable accessor that counts every READ of window.fetch (and returns the
//                        browser's fetch): the page must read it 0 times (three's ImageBitmapLoader used to run
//                        `typeof fetch` while the GLTF parser was built)
//   getter-throws        early non-configurable accessor whose every read throws (a host with network switched off):
//                        a page that so much as reads window.fetch fails here
//
// Options:
//   --view top     after the board is shown, drag the camera to look straight down (the view silkscreen is read on)
//                  before the screenshot (--shot), as a person would
//   --dpr N        after the screenshot, change only the device pixel ratio to N (same CSS size, so no resize) and
//                  report whether the canvas followed (pixelRatio; part of `ok` when given)
//
// Usage:
//   node tools/review_host_harness.mjs PAGE.html --host shim-early [--shot crop.png] [--timeout 90] [--chrome PATH]
// Prints one JSON object; exit status 0 when ALL of these hold:
//   * the 3D box reached data-state="ready" and a screenshot of it differs from its background (board pixels),
//   * the page logged no console error, exception or CSP report,
//   * the page made NO request: DevTools saw no network request from the review frame other than its own document
//     (fetch / XHR / import() / Worker / image / blob: all show there), the host fetch was never called, and the parent
//     received no message from the frame,
//   * window.fetch is exactly what the host (or the browser) left there: same property descriptor before the page ran
//     as when the host installed it, unchanged afterwards, never re-installed by the re-installing host, and (the
//     getter-* hosts) never even READ by the page,
//   * with --dpr, the canvas follows the new device pixel ratio,
//   * and (host modes) a request for another URL made after the board was shown still reaches the host's fetch (one
//     message to the parent) or, with no host, the browser's.
//
// Needs: Node >= 22 (global WebSocket) and Google Chrome (or --chrome / $CHROME). No npm packages.

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { inflateSync } from "node:zlib";

const BG = [0x0b, 0x0e, 0x0c]; // the 3D box background in review.py
const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; " +
  "font-src data:; media-src 'none'; connect-src 'none'; worker-src 'none'";

// The modelled host fetch: counts its calls, forwards its arguments to the parent and rejects (the parent cannot read
// the frame's blob: URLs anyway). A non-clonable argument throws DataCloneError out of postMessage (no message reaches
// the parent; the call count still shows it), which rejects the promise with it.
const HOST_FETCH = `
  window.__hostCalls = 0;
  window.__reinstalls = 0;
  const __desc = () => { const d = Object.getOwnPropertyDescriptor(window, "fetch");
    return d ? { value: d.value, get: d.get, set: d.set, writable: d.writable, enumerable: d.enumerable, configurable: d.configurable } : null; };
  const __hostFetch = function fetch(...args) {
    window.__hostCalls++;
    return new Promise(function (resolve, reject) {
      parent.postMessage({ args: args }, "*");
      reject(new TypeError("modelled host: the parent does not answer fetch"));
    });
  };
  window.__hostFetch = __hostFetch;`;

// How each host installs it. `window.__preHost` / `window.__hostDesc`: fetch's descriptor just before / after.
const INSTALL = {
  assign: `window.__preHost = __desc(); window.fetch = __hostFetch; window.__hostDesc = __desc();`,
  fallback: `window.__preHost = __desc(); const __orig = window.fetch;
    const __fb = function fetch(...args) { return __hostFetch(...args).catch(function () { return __orig.apply(window, args); }); };
    window.__hostFetch = __fb; window.fetch = __fb; window.__hostDesc = __desc();`,
  proxy: `window.__preHost = __desc();
    const __px = new Proxy(window.fetch, { apply(t, th, a) { return __hostFetch(...a); } });
    window.__hostFetch = __px; window.fetch = __px; window.__hostDesc = __desc();`,
  onload: `addEventListener("load", function () { window.__preHost = __desc(); window.fetch = __hostFetch; window.__hostDesc = __desc(); });`,
  reinstall: `window.__preHost = __desc(); window.fetch = __hostFetch; window.__hostDesc = __desc();
    setInterval(function () { if (window.fetch !== __hostFetch) { window.__reinstalls++; window.fetch = __hostFetch; } }, 20);`,
  getter: `window.__preHost = __desc(); Object.defineProperty(window, "fetch", { configurable: true, enumerable: true,
    get() { return __hostFetch; }, set(v) {} }); window.__hostDesc = __desc();`,
  getterNonconfig: `window.__preHost = __desc(); Object.defineProperty(window, "fetch", { configurable: false, enumerable: true,
    get() { return __hostFetch; }, set(v) {} }); window.__hostDesc = __desc();`,
  frozen: `window.__preHost = __desc(); Object.defineProperty(window, "fetch", { value: __hostFetch, writable: false,
    configurable: false }); window.__hostDesc = __desc();`,
  defineOnload: `addEventListener("load", function () { window.__preHost = __desc(); Object.defineProperty(window, "fetch",
    { value: __hostFetch, writable: true, configurable: true, enumerable: true }); window.__hostDesc = __desc(); });`,
  define: `window.__preHost = __desc(); Object.defineProperty(window, "fetch", { value: __hostFetch, writable: true,
    configurable: true, enumerable: true }); window.__hostDesc = __desc();`,
  // read-watching hosts: window.__fetchReads counts reads of window.fetch (window.__fetchReadStacks says from where)
  countReads: `window.__preHost = __desc(); window.__readMode = "counts"; window.__fetchReads = 0; window.__fetchReadStacks = [];
    const __native = window.fetch;
    Object.defineProperty(window, "fetch", { configurable: false, enumerable: true, get() { window.__fetchReads++;
      window.__fetchReadStacks.push(String(new Error().stack).split("\\n").slice(2, 5).join(" | ")); return __native; } });
    window.__hostDesc = __desc();`,
  throwOnRead: `window.__preHost = __desc(); window.__readMode = "throws"; window.__fetchReads = 0; window.__fetchReadStacks = [];
    Object.defineProperty(window, "fetch", { configurable: false, enumerable: true, get() { window.__fetchReads++;
      window.__fetchReadStacks.push(String(new Error().stack).split("\\n").slice(2, 5).join(" | "));
      throw new TypeError("modelled host: network is disabled in this preview"); } });
    window.__hostDesc = __desc();`,
};

// host -> [where ("early": first in <head>, "late": a module script appended last), installer]
const HOSTS = {
  file: null, http: null, noshim: null,
  "shim-early": ["early", "assign"], "shim-late": ["late", "assign"], "shim-late-fallback": ["late", "fallback"],
  "shim-late-proxy": ["late", "proxy"], "shim-onload": ["early", "onload"], "shim-reinstall": ["early", "reinstall"],
  "shim-getter": ["early", "getter"], "shim-getter-nonconfig": ["early", "getterNonconfig"],
  "shim-frozen": ["early", "frozen"], "shim-define-onload": ["early", "defineOnload"], "shim-define-late": ["late", "define"],
  "getter-counts": ["early", "countReads"], "getter-throws": ["early", "throwOnRead"],
};

// Observer only (first script in the frame; never touches fetch): fetch's descriptor before anything else ran,
// errors the page reports, CSP violations.
const PROBE = `
(function () {
  const d = Object.getOwnPropertyDescriptor(window, "fetch");
  const rec = (window.__harness = { startDesc: d ? { value: d.value, get: d.get, set: d.set, writable: d.writable,
    enumerable: d.enumerable, configurable: d.configurable } : null, csp: [], errors: [] });
  document.addEventListener("securitypolicyviolation", function (e) { rec.csp.push(e.violatedDirective + " " + e.blockedURI); });
  addEventListener("error", function (e) { rec.errors.push(String(e.error || e.message)); });
  addEventListener("unhandledrejection", function (e) { rec.errors.push("unhandled rejection: " + String(e.reason)); });
})();`;

// --dpr only: keep every MediaQueryList the page makes, so the harness can deliver the `change` a real browser sends
// when the window moves to a display with another pixel ratio (headless Chrome's emulated ratio change sends none).
const MQ_RECORDER = `
(function () {
  const mm = window.matchMedia, lists = (window.__harnessMediaLists = []);
  window.matchMedia = function (q) { const l = mm.call(window, q); lists.push({ l, was: l.matches }); return l; };
})();`;

function parseArgs(argv) {
  const a = { page: null, host: "shim-early", shot: null, timeout: 90, chrome: process.env.CHROME || null, view: "default",
    dpr: null };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--host") a.host = argv[++i];
    else if (k === "--shot") a.shot = argv[++i];
    else if (k === "--timeout") a.timeout = Number(argv[++i]);
    else if (k === "--chrome") a.chrome = argv[++i];
    else if (k === "--view") a.view = argv[++i];
    else if (k === "--dpr") a.dpr = Number(argv[++i]);
    else if (!a.page) a.page = k;
    else throw new Error(`unexpected argument ${k}`);
  }
  if (!a.page) throw new Error(`usage: review_host_harness.mjs PAGE.html --host ${Object.keys(HOSTS).join("|")}`);
  if (!(a.host in HOSTS)) throw new Error(`unknown host ${a.host}`);
  if (a.view !== "default" && a.view !== "top") throw new Error(`unknown view ${a.view}`);
  if (a.dpr !== null && !(a.dpr > 0)) throw new Error("--dpr wants a positive number");
  if (a.dpr !== null && a.host !== "file" && a.host !== "http") throw new Error("--dpr needs --host file or http");
  return a;
}

function findChrome(explicit) {
  const c = [explicit, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium", "/usr/bin/google-chrome", "/usr/bin/chromium",
    "/usr/bin/chromium-browser"].filter(Boolean);
  for (const p of c) if (existsSync(p)) return p;
  throw new Error("no Chrome found (pass --chrome or set $CHROME)");
}

function insertHead(page, tag) {
  const m = /<head[^>]*>/i.exec(page);
  return m ? page.slice(0, m.index + m[0].length) + tag + page.slice(m.index + m[0].length) : tag + page;
}

function insertShim(page, where, installer) {
  const body = `(function () {${HOST_FETCH}\n${INSTALL[installer]}\n})();`;
  if (where === "early") return insertHead(page, `<script>/* modelled host fetch (early, ${installer}) */${body}</script>`);
  const tag = `<script type="module">/* modelled host fetch (late, ${installer}) */${body}</script>`;
  const i = page.toLowerCase().lastIndexOf("</body>");
  return i >= 0 ? page.slice(0, i) + tag + page.slice(i) : page + tag;
}

function hostPage(srcdoc) {
  const attr = srcdoc.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${CSP}">
<style>html,body{margin:0;height:100%;background:#fff}iframe{border:0;display:block;width:100%;height:100%}</style>
<script>window.__received = []; addEventListener("message", function (e) {
  try { window.__received.push(JSON.stringify(e.data)); } catch (err) { window.__received.push(String(err)); } });</script>
</head><body><iframe id="pane" sandbox="allow-scripts" srcdoc="${attr}"></iframe></body></html>`;
}

// Minimal PNG decoder: 8-bit, non-interlaced RGB / RGBA (what Chrome's screenshots are).
function decodePng(buf) {
  let p = 8, w = 0, h = 0, ct = 0;
  const idat = [];
  while (p < buf.length) {
    const len = buf.readUInt32BE(p), type = buf.toString("ascii", p + 4, p + 8), data = buf.subarray(p + 8, p + 8 + len);
    if (type === "IHDR") {
      w = data.readUInt32BE(0); h = data.readUInt32BE(4); ct = data[9];
      if (data[8] !== 8 || data[12] !== 0 || (ct !== 2 && ct !== 6)) throw new Error("unsupported PNG");
    } else if (type === "IDAT") idat.push(data);
    p += 12 + len;
  }
  const bpp = ct === 6 ? 4 : 3, stride = w * bpp, raw = inflateSync(Buffer.concat(idat));
  const out = Buffer.alloc(h * stride);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)], src = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? out[y * stride + x - bpp] : 0, b = y ? out[(y - 1) * stride + x] : 0;
      const c = x >= bpp && y ? out[(y - 1) * stride + x - bpp] : 0;
      let v = src[x];
      if (f === 1) v += a; else if (f === 2) v += b; else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) { const pp = a + b - c, pa = Math.abs(pp - a), pb = Math.abs(pp - b), pc = Math.abs(pp - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c; }
      out[y * stride + x] = v & 255;
    }
  }
  return { w, h, bpp, px: out };
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
  send(method, params = {}, sessionId = undefined) {
    const id = ++this.id;
    this.ws.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
  on(h) { this.handlers.push(h); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const a = parseArgs(process.argv.slice(2));
  const page = resolve(a.page);
  const work = mkdtempSync(join(tmpdir(), "pcbc-host-"));
  const hosted = HOSTS[a.host];
  const plain = a.host === "file" || a.host === "http";
  let url, server = null;
  if (a.host === "file") url = pathToFileURL(page).href;
  else if (a.host === "http") {
    const body = readFileSync(page);
    server = createServer((req, res) => {
      if (req.url === "/" || req.url === "/review.html") { res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }); res.end(body); }
      else { res.writeHead(404); res.end(); }
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    url = `http://localhost:${server.address().port}/review.html`;
  } else {
    let doc = readFileSync(page, "utf8");
    if (hosted) doc = insertShim(doc, hosted[0], hosted[1]);
    // The observer goes first (an out-of-process srcdoc frame commits before DevTools can add a new-document script)
    doc = insertHead(doc, `<script>/* harness observer */${PROBE}</script>`);
    writeFileSync(join(work, "host.html"), hostPage(doc));
    url = pathToFileURL(join(work, "host.html")).href;
  }
  const prof = join(work, "profile");
  const chrome = spawn(findChrome(a.chrome), ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${prof}`,
    "--no-first-run", "--no-default-browser-check", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
    "--hide-scrollbars", "--window-size=1280,900", "about:blank"], { stdio: "ignore" });
  const result = { page, host: a.host, state: null, statusText: null, consoleErrors: [], pageRequests: [], boardPixels: null,
    ok: false };
  const requests = []; // every Network.requestWillBeSent: { frameId, url, type }
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
    // A sandboxed (opaque-origin) srcdoc frame is its own out-of-process frame in current Chrome: attach to it as a
    // child target (flattened sessions) and set it up (network log included) before its scripts run.
    const contexts = new Map(); // frameId -> { ctx, session }
    let loadFired = false;
    let childFrame = null; // an out-of-process frame's target id is its frame id
    const setup = async (session) => {
      await cdp.send("Network.enable", {}, session);
      await cdp.send("Runtime.enable", {}, session);
      await cdp.send("Log.enable", {}, session);
      await cdp.send("Page.enable", {}, session);
      await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: PROBE }, session);
      if (a.dpr !== null) await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: MQ_RECORDER }, session);
      await cdp.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }, session);
    };
    cdp.on((m) => {
      const p = m.params;
      if (process.env.HARNESS_DEBUG) process.stderr.write(`${m.sessionId || "-"} ${m.method} ${JSON.stringify(p).slice(0, 160)}\n`);
      if (m.method === "Target.attachedToTarget") {
        const sid = p.sessionId;
        if (p.targetInfo.type === "iframe" && !childFrame) childFrame = p.targetInfo.targetId;
        setup(sid).then(() => cdp.send("Runtime.runIfWaitingForDebugger", {}, sid)).catch(() => {});
      } else if (m.method === "Runtime.executionContextCreated" && p.context.auxData && p.context.auxData.isDefault)
        contexts.set(p.context.auxData.frameId, { ctx: p.context.id, session: m.sessionId });
      else if (m.method === "Runtime.consoleAPICalled" && (p.type === "error" || p.type === "assert"))
        result.consoleErrors.push("console.error: " + p.args.map((x) => x.value ?? x.description ?? "").join(" "));
      else if (m.method === "Runtime.exceptionThrown") {
        const d = p.exceptionDetails;
        result.consoleErrors.push("exception: " + ((d.exception && d.exception.description) || d.text));
      } else if (m.method === "Log.entryAdded" && p.entry.level === "error")
        result.consoleErrors.push(`log(${p.entry.source}): ${p.entry.text}`);
      else if (m.method === "Network.requestWillBeSent")
        requests.push({ frameId: p.frameId, url: String(p.request.url).slice(0, 120), type: p.type });
      else if (m.method === "Page.loadEventFired" && !m.sessionId) loadFired = true;
    });
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    await setup(undefined);
    await cdp.send("Page.navigate", { url });
    const t0 = Date.now(), deadline = t0 + a.timeout * 1000;
    while (!loadFired && Date.now() < deadline) await sleep(100);
    const tree = (await cdp.send("Page.getFrameTree")).frameTree;
    while (!plain && !childFrame && !(tree.childFrames || []).length && Date.now() < deadline) await sleep(50);
    const frameId = plain ? tree.frame.id : childFrame || (tree.childFrames || [])[0]?.frame.id;
    if (!frameId) throw new Error("the review page frame did not appear: load=" + loadFired + " tree=" + JSON.stringify(tree).slice(0, 400));
    while (!contexts.has(frameId) && Date.now() < deadline) await sleep(50);
    if (!contexts.has(frameId)) throw new Error("no script context in the review page frame");
    const { ctx, session } = contexts.get(frameId);
    const ev = async (expression) => {
      const r = await cdp.send("Runtime.evaluate", { expression, contextId: ctx, returnByValue: true, awaitPromise: true },
        session);
      if (r.exceptionDetails) throw new Error("harness evaluate failed: " + JSON.stringify(r.exceptionDetails));
      return r.result.value;
    };
    // the on-load hosts install their fetch at 'load': open the tab after it, like a person would
    await ev(`new Promise(r => document.readyState === "complete" ? r(true) : addEventListener("load", () => r(true)))`);
    await sleep(100);
    await ev(`(document.querySelector('nav [data-tab="three"]') || {click(){}}).click(), true`);
    let state = null;
    while (Date.now() < deadline) {
      state = await ev(`(() => { const v = document.getElementById("board-3d"), s = document.getElementById("board-3d-status");
        return { present: !!v, state: v ? v.dataset.state : null, status: s && !s.hidden ? s.textContent : null }; })()`);
      if (!state.present || state.state === "ready" || state.state === "error") break;
      await sleep(100);
    }
    result.state = state && state.state;
    result.statusText = state && state.status;
    result.seconds = Math.round((Date.now() - t0) / 100) / 10;
    const frames = () => ev(`new Promise(r => { let n = 0; const f = () => (++n > 10 ? r(true) : requestAnimationFrame(f)); f(); })`);
    if (result.state === "ready") {
      await frames();
      await sleep(300);
      if (a.view === "top") {
        // a real left-button drag straight down the box: OrbitControls clamps the camera at the pole
        const r = await ev(`(() => { const b = document.getElementById("board-3d").getBoundingClientRect();
          return { x: b.left + b.width / 2, y: b.top + b.height / 2 }; })()`);
        const mouse = (type, y, buttons) => cdp.send("Input.dispatchMouseEvent", { type, x: r.x, y, button: type === "mouseMoved" && !buttons ? "none" : "left",
          buttons, clickCount: type === "mouseMoved" ? 0 : 1 });
        await mouse("mouseMoved", r.y, 0);
        await mouse("mousePressed", r.y, 1);
        for (let i = 1; i <= 10; i++) await mouse("mouseMoved", r.y + 200 * i, 1);
        await mouse("mouseReleased", r.y + 2000, 0);
        await frames();
        await sleep(300);
      }
    }
    if (state && state.present) {
      const rect = await ev(`(() => { const r = document.getElementById("board-3d").getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height }; })()`);
      // the srcdoc iframe sits at (0, 0) of the host page, so frame coordinates are page coordinates
      const shot = await cdp.send("Page.captureScreenshot", { format: "png",
        clip: { x: rect.x, y: rect.y, width: Math.max(1, rect.w), height: Math.max(1, rect.h), scale: 1 } });
      const png = Buffer.from(shot.data, "base64");
      if (a.shot) writeFileSync(a.shot, png);
      const img = decodePng(png);
      let diff = 0;
      const n = img.w * img.h;
      for (let i = 0; i < n; i++) {
        const o = i * img.bpp;
        const d = Math.max(Math.abs(img.px[o] - BG[0]), Math.abs(img.px[o + 1] - BG[1]), Math.abs(img.px[o + 2] - BG[2]));
        if (d > 24) diff++;
      }
      result.boardPixels = { crop: [img.w, img.h], differFromBackground: diff, fraction: Math.round((diff / n) * 1e4) / 1e4 };
    }
    // Everything the page did until now: no request (network log of the review frame, minus its own document), no
    // call to the host fetch, no message to the parent, and fetch exactly as the host (or the browser) left it.
    result.pageRequests = requests.filter((r) => r.frameId === frameId && r.type !== "Document");
    result.allRequests = requests.length;
    const parentCtx = plain ? null : (contexts.get(tree.frame.id) || {}).ctx;
    const received = async () => parentCtx == null ? [] : (await cdp.send("Runtime.evaluate",
      { expression: "window.__received", contextId: parentCtx, returnByValue: true })).result.value || [];
    const before = await received();
    result.hostMessages = before.length;
    result.fetchCheck = await ev(`(() => {
      const reads = window.__fetchReads, readStacks = window.__fetchReadStacks, readMode = window.__readMode || null;
      const d = Object.getOwnPropertyDescriptor(window, "fetch");
      const now = d ? { value: d.value, get: d.get, set: d.set, writable: d.writable, enumerable: d.enumerable, configurable: d.configurable } : null;
      const same = (x, y) => !!x && !!y && ["value", "get", "set", "writable", "enumerable", "configurable"].every(k => x[k] === y[k]);
      const h = window.__harness || {};
      const hosted = typeof window.__hostFetch !== "undefined";
      // a read-watching host: compare descriptors only (reading window.fetch here would count, or throw)
      return { hosted, readMode, fetchReads: readMode ? reads : null, fetchReadStacks: readMode ? readStacks : null,
        hostCalls: window.__hostCalls || 0, reinstalls: window.__reinstalls || 0,
        untouchedBeforeHost: hosted ? same(h.startDesc, window.__preHost) : null,
        untouchedAfterHost: hosted ? same(window.__hostDesc, now) : same(h.startDesc, now) || !h.startDesc,
        fetchIsHost: hosted && !readMode ? window.fetch === window.__hostFetch : null,
        fetchIsNative: hosted ? null : /\\[native code\\]/.test(Function.prototype.toString.call(window.fetch)),
        csp: h.csp || [], errors: h.errors || [] }; })()`);
    const f = result.fetchCheck;
    if (f.csp.length || f.errors.length) result.consoleErrors.push(...f.csp.map((c) => "csp: " + c), ...f.errors.map((e) => "page: " + e));
    result.noRequests = result.pageRequests.length === 0 && result.hostMessages === 0 && f.hostCalls === 0;
    result.fetchUntouched = f.readMode
      ? f.untouchedBeforeHost && f.untouchedAfterHost && f.fetchReads === 0
      : f.hosted
        ? f.untouchedBeforeHost && f.untouchedAfterHost && f.fetchIsHost && f.reinstalls === 0
        : f.untouchedAfterHost && f.fetchIsNative;
    // The device pixel ratio changes twice and the canvas must follow both times: (1) "zoom": the CSS size and the
    // ratio change together (browser zoom; the box resizes), (2) "display": only the ratio changes, to 3/4 of it (the
    // window moved to another display; nothing resizes) and the page gets the media-query `change` the browser sends.
    if (a.dpr !== null && state && state.present) {
      const canvas = () => ev(`(() => { const c = document.querySelector("#board-3d canvas");
        return c ? { w: c.width, h: c.height, cssW: c.clientWidth, cssH: c.clientHeight, dpr: window.devicePixelRatio } : null; })()`);
      const want = (c, r) => !!c && c.dpr === r && Math.abs(c.w - Math.round(c.cssW * Math.min(r, 2))) <= 1 &&
        Math.abs(c.h - Math.round(c.cssH * Math.min(r, 2))) <= 1;
      const settle = async (r) => { let c = await canvas(); for (let i = 0; i < 30 && !want(c, r); i++) { await sleep(100); c = await canvas(); } return c; };
      const before = await canvas();
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 800, deviceScaleFactor: a.dpr, mobile: false });
      const zoom = await settle(a.dpr);
      const second = a.dpr * 0.75;
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 800, deviceScaleFactor: second, mobile: false });
      await ev(`new Promise(r => requestAnimationFrame(() => r(true)))`);
      const delivered = await ev(`(window.__harnessMediaLists || []).filter(x => x.l.matches !== x.was).map(x => {
        x.was = x.l.matches; x.l.dispatchEvent(new MediaQueryListEvent("change", { media: x.l.media, matches: x.l.matches }));
        return x.l.media; })`);
      const display = await settle(second);
      result.pixelRatio = { before, zoom, display, mediaChanges: delivered,
        followed: want(before, 1) && want(zoom, a.dpr) && want(display, second) };
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    }
    // Every other request still reaches whatever fetch is there: ask the frame's fetch for a different blob: URL. Under
    // a host that must reach the parent (one message) and reject; with no host it must resolve natively (file, http)
    // or be refused by the CSP (noshim).
    if (state && state.present) {
      const errorsBefore = result.consoleErrors.length;
      const outcome = await ev(`(() => { const u = URL.createObjectURL(new Blob(["other"]));
        try { return fetch(u).then(r => r.text().then(t => "resolved:" + t), e => "rejected:" + String(e)); }
        catch (e) { return "threw:" + String(e); } })()`);
      await sleep(200);
      const after = await received();
      result.otherRequest = { outcome, hostMessages: after.length - before.length };
      // a request the CSP refuses (noshim, and the fallback host's second hop) logs CSP errors: they belong to this
      // extra request, not to the 3D tab
      result.otherRequest.consoleErrors = result.consoleErrors.splice(errorsBefore);
      if (a.host === "noshim" || a.host === "getter-counts") result.otherRequestOk = outcome === "rejected:TypeError: Failed to fetch";
      else if (a.host === "getter-throws") result.otherRequestOk = outcome === "threw:TypeError: modelled host: network is disabled in this preview";
      else if (plain) result.otherRequestOk = outcome === "resolved:other";
      else if (a.host === "shim-late-fallback") result.otherRequestOk = after.length - before.length === 1 && outcome === "rejected:TypeError: Failed to fetch";
      else result.otherRequestOk = after.length - before.length === 1 && outcome.startsWith("rejected:TypeError: modelled host");
    }
    // What the host pane would show: the first error the frame reports, else the page's own status text.
    const first = result.consoleErrors[0];
    result.paneText = first ? "Error: " + String(first).replace(/^(console\.error|exception|log\([a-z]+\)|page|csp): /, "").split("\n")[0]
      : result.statusText;
    result.ok = result.state === "ready" && result.consoleErrors.length === 0 && !!result.boardPixels &&
      result.boardPixels.fraction > 0.01 && result.noRequests && result.fetchUntouched && result.otherRequestOk === true &&
      (a.dpr === null || (!!result.pixelRatio && result.pixelRatio.followed));
  } catch (e) {
    result.harnessError = String(e && e.stack || e);
  } finally {
    chrome.kill("SIGKILL");
    if (server) server.close();
    await sleep(200);
    try { rmSync(work, { recursive: true, force: true }); } catch { /* Chrome may still hold files */ }
  }
  process.stdout.write(JSON.stringify(result, null, 1) + "\n");
  process.exit(result.ok ? 0 : 1);
}

main().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(2); });
