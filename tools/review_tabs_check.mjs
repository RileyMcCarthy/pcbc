#!/usr/bin/env node
// Open a pcbc review page in headless Chrome as a phone (375x812 CSS px, touch, mobile viewport), optionally with
// JavaScript switched OFF, tap every tab with a real (emulated) touch and report what shows.
//
// Why: the owner opened a review page in Claude's phone app and could not leave the schematic. The page switched tabs
// only with a script, and that viewer runs the page with scripts off. The page now switches tabs with radio buttons
// and CSS; this checks it the way that viewer shows it.
//
// Usage:
//   node tools/review_tabs_check.mjs PAGE.html [--nojs] [--shots DIR] [--prefix NAME] [--width 375] [--height 812]
//        [--timeout 90] [--chrome PATH]
// Prints one JSON object; exit status 0 when ALL of these hold:
//   * on open, exactly the first tab's section shows and its label looks selected,
//   * a tap on each tab label shows exactly that tab's section (every other section is display:none) and marks
//     that label selected (accent background), in nav order and back to the first,
//   * the page never scrolls sideways (content width <= viewport width) on any tab,
//   * every schematic / copper / silk SVG on its tab is no wider than the screen; without JS the schematic box does
//     not block the phone's own pinch zoom and scroll (touch-action is not "none"),
//   * the 3D tab: without JS its note (the 3D view needs JavaScript; static.html has still renders) shows and the empty
//     viewer box does not; with JS the viewer box shows, the note does not, and the box reaches data-state="ready",
//   * the keyboard: focusing the checked tab and pressing ArrowRight shows the next section and outlines its label,
//   * with JS on, the page logs no exception or console error.
// Screenshots: DIR/PREFIX-<tab>.png per tab (viewport, after the tap).
// Needs: Node >= 22 (global WebSocket) and Google Chrome (or --chrome / $CHROME). No npm packages. JavaScript being off
// is Emulation.setScriptExecutionDisabled; all reads go through the DOM / CSS domains, which work without it.

import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

function parseArgs(argv) {
  const a = { page: null, nojs: false, shots: null, prefix: "page", width: 375, height: 812, timeout: 90,
    chrome: process.env.CHROME || null };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--nojs") a.nojs = true;
    else if (k === "--shots") a.shots = argv[++i];
    else if (k === "--prefix") a.prefix = argv[++i];
    else if (k === "--width") a.width = Number(argv[++i]);
    else if (k === "--height") a.height = Number(argv[++i]);
    else if (k === "--timeout") a.timeout = Number(argv[++i]);
    else if (k === "--chrome") a.chrome = argv[++i];
    else if (!a.page) a.page = k;
    else throw new Error(`unexpected argument ${k}`);
  }
  if (!a.page) throw new Error("usage: review_tabs_check.mjs PAGE.html [--nojs] [--shots DIR]");
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
const ACCENT = "rgb(212, 137, 58)";

async function main() {
  const a = parseArgs(process.argv.slice(2));
  const page = resolve(a.page);
  const work = mkdtempSync(join(tmpdir(), "pcbc-tabs-"));
  const prof = join(work, "profile");
  const chrome = spawn(findChrome(a.chrome), ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${prof}`,
    "--no-first-run", "--no-default-browser-check", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
    "--hide-scrollbars", "--window-size=800,900", "about:blank"], { stdio: "ignore" });
  const result = { page, js: !a.nojs, viewport: [a.width, a.height], initial: null, tabs: [], keyboard: null,
    consoleErrors: [], failures: [], ok: false };
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
    let loaded = false;
    cdp.on((m) => {
      const p = m.params;
      if (m.method === "Page.loadEventFired") loaded = true;
      else if (m.method === "Runtime.exceptionThrown") {
        const d = p.exceptionDetails;
        result.consoleErrors.push("exception: " + ((d.exception && d.exception.description) || d.text));
      } else if (m.method === "Runtime.consoleAPICalled" && (p.type === "error" || p.type === "assert"))
        result.consoleErrors.push("console.error: " + p.args.map((x) => x.value ?? x.description ?? "").join(" "));
      else if (m.method === "Log.entryAdded" && p.entry.level === "error" && p.entry.source !== "network")
        result.consoleErrors.push(`log(${p.entry.source}): ${p.entry.text}`);
    });
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");
    await cdp.send("DOM.enable");
    await cdp.send("CSS.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: a.width, height: a.height, deviceScaleFactor: 2,
      mobile: true, screenWidth: a.width, screenHeight: a.height });
    await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
    if (a.nojs) await cdp.send("Emulation.setScriptExecutionDisabled", { value: true });
    await cdp.send("Page.navigate", { url: pathToFileURL(page).href });
    const deadline = Date.now() + a.timeout * 1000;
    while (!loaded && Date.now() < deadline) await sleep(100);
    if (!loaded) throw new Error("the page did not load");
    await sleep(300);

    const root = (await cdp.send("DOM.getDocument", { depth: 0 })).root.nodeId;
    const q = async (sel) => (await cdp.send("DOM.querySelector", { nodeId: root, selector: sel })).nodeId || null;
    const qa = async (sel) => (await cdp.send("DOM.querySelectorAll", { nodeId: root, selector: sel })).nodeIds;
    const style = async (nodeId, ...props) => {
      const cs = (await cdp.send("CSS.getComputedStyleForNode", { nodeId })).computedStyle;
      const out = {};
      for (const p of props) out[p] = (cs.find((c) => c.name === p) || {}).value;
      return out;
    };
    const attr = async (nodeId, name) => {
      const at = (await cdp.send("DOM.getAttributes", { nodeId })).attributes;
      const i = at.indexOf(name);
      return i >= 0 && i % 2 === 0 ? at[i + 1] : null;
    };
    const rect = async (nodeId) => {
      try {
        const quads = (await cdp.send("DOM.getContentQuads", { nodeId })).quads;
        if (!quads.length) return null;
        const xs = quads[0].filter((_, i) => i % 2 === 0), ys = quads[0].filter((_, i) => i % 2 === 1);
        return { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
      } catch { return null; } // display:none: no box
    };
    const metrics = async () => {
      const m = await cdp.send("Page.getLayoutMetrics");
      return { contentWidth: m.cssContentSize.width, viewportWidth: m.cssLayoutViewport.clientWidth };
    };

    const labels = [];
    for (const id of await qa("nav [data-tab]")) labels.push({ id, tab: await attr(id, "data-tab") });
    if (labels.length < 2) throw new Error(`expected tabs ([data-tab]) in <nav>, found ${labels.length}`);
    const sections = [];
    for (const { tab } of labels) {
      const id = await q(`section#${tab}`);
      if (!id) throw new Error(`no section#${tab}`);
      sections.push({ tab, id });
    }
    const shown = async () => {
      const out = [];
      for (const s of sections) if ((await style(s.id, "display")).display !== "none") out.push(s.tab);
      return out;
    };
    const selected = async () => {
      const out = [];
      for (const l of labels) if ((await style(l.id, "background-color"))["background-color"] === ACCENT) out.push(l.tab);
      return out;
    };
    const tap = async (nodeId) => {
      await cdp.send("DOM.scrollIntoViewIfNeeded", { nodeId });
      const r = await rect(nodeId);
      if (!r) throw new Error("tab label has no box");
      const x = r.x + r.w / 2, y = r.y + r.h / 2;
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
      await sleep(40);
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      await sleep(350);
      return { x: Math.round(x), y: Math.round(y) };
    };

    // did the page's scripts run? (the schematic script marks its box zoomable)
    const sv0 = await q("#sch-view");
    if (sv0) {
      result.scriptsRan = /\bzoomable\b/.test((await attr(sv0, "class")) || "");
      if (result.scriptsRan === a.nojs) fail(`scripts ${result.scriptsRan ? "ran" : "did not run"} (JavaScript ${a.nojs ? "off" : "on"})`);
    }
    result.initial = { shown: await shown(), selected: await selected() };
    if (JSON.stringify(result.initial.shown) !== JSON.stringify([labels[0].tab])) fail(`on open shows ${result.initial.shown}`);
    if (JSON.stringify(result.initial.selected) !== JSON.stringify([labels[0].tab])) fail(`on open selects ${result.initial.selected}`);

    if (a.shots) mkdirSync(a.shots, { recursive: true });
    for (const l of [...labels, labels[0]]) {
      const at = await tap(l.id);
      const r = { tab: l.tab, tap: at, shown: await shown(), selected: await selected(), ...(await metrics()) };
      if (JSON.stringify(r.shown) !== JSON.stringify([l.tab])) fail(`tap ${l.tab}: shows ${r.shown}`);
      if (JSON.stringify(r.selected) !== JSON.stringify([l.tab])) fail(`tap ${l.tab}: selects ${r.selected}`);
      if (r.contentWidth > r.viewportWidth + 0.5) fail(`tap ${l.tab}: page ${r.contentWidth}px wide on a ${r.viewportWidth}px screen`);
      // the pictures on this tab fit the screen
      const svgs = await qa(`section#${l.tab} .plot > svg, section#${l.tab} .sch-stage > svg`);
      r.svgWidths = [];
      for (const s of svgs) {
        const b = await rect(s);
        if (b) r.svgWidths.push(Math.round(b.w));
        if (b && (b.x < -0.5 || b.x + b.w > r.viewportWidth + 0.5)) fail(`tap ${l.tab}: an SVG spans ${b.x}..${b.x + b.w}`);
        if (!b || b.h < 20) fail(`tap ${l.tab}: an SVG has no size`);
      }
      if (l.tab === "sch") {
        const sv = await q("#sch-view");
        if (sv) {
          r.schTouchAction = (await style(sv, "touch-action"))["touch-action"];
          if (a.nojs && r.schTouchAction === "none") fail("without JS the schematic box blocks pinch and scroll (touch-action: none)");
        }
      }
      if (l.tab === "three") {
        const box = await q("#board-3d"), note = await q("#board-3d-nojs");
        r.three = { box: box ? !!(await rect(box)) : null, note: note ? !!(await rect(note)) : null };
        if (a.nojs) {
          if (box && !r.three.note) fail("3D without JS: no note shown");
          if (r.three.box) fail("3D without JS: the empty viewer box shows");
        } else if (box) {
          if (!r.three.box) fail("3D with JS: the viewer box is hidden");
          if (r.three.note) fail("3D with JS: the no-JavaScript note still shows");
          let state = null;
          while (Date.now() < deadline && (state = await attr(box, "data-state")) !== "ready" && state !== "error") await sleep(200);
          r.three.state = state;
          if (state !== "ready") fail(`3D with JS: viewer state ${state}`);
        }
      }
      if (a.shots) {
        const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
        r.shot = join(a.shots, `${a.prefix}-${l.tab}.png`);
        writeFileSync(r.shot, Buffer.from(shot.data, "base64"));
      }
      result.tabs.push(r);
    }

    // keyboard: the checked tab's radio, then ArrowRight
    const firstRadio = await q("input.tab-state:checked");
    if (!firstRadio) fail("no checked tab radio");
    else {
      await cdp.send("DOM.focus", { nodeId: firstRadio });
      await cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 });
      await sleep(200);
      const next = labels[1];
      const k = { shown: await shown(), outline: (await style(next.id, "outline-style"))["outline-style"] };
      result.keyboard = k;
      if (JSON.stringify(k.shown) !== JSON.stringify([next.tab])) fail(`keyboard ArrowRight shows ${k.shown}`);
      if (k.outline === "none") fail(`keyboard focus on ${next.tab} has no visible outline`);
    }
    if (!a.nojs && result.consoleErrors.length) fail("console errors: " + result.consoleErrors.join(" | "));
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
