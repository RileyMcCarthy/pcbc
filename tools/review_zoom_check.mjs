#!/usr/bin/env node
// The schematic tab fits its sheet to the box. Hiding that tab, resizing the window, and showing it
// again must fit the new box: a hidden box has no size, and laying the sheet out then left buck 1 px
// wide and left c3_usb and node at the width from before the resize.
//
// Usage: node tools/review_zoom_check.mjs PAGE.html [--chrome PATH]
// Prints one JSON object. Exit 0 when the sheet fills the box on open and again after that resize.

import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

function findChrome(explicit) {
  const c = [explicit, process.env.CHROME, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome"].filter(Boolean);
  for (const p of c) if (existsSync(p)) return p;
  throw new Error("no Chrome found");
}

class Cdp {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.id = 0; this.pending = new Map();
    this.ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { res, rej } = this.pending.get(msg.id); this.pending.delete(msg.id);
        msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
      }
    };
  }
  open() { return new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = rej; }); }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, { res, rej }));
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const page = resolve(process.argv[2]);
  const chromeArg = process.argv.includes("--chrome") ? process.argv[process.argv.indexOf("--chrome") + 1] : null;
  const work = mkdtempSync(join(tmpdir(), "pcbc-zoom-"));
  const prof = join(work, "profile");
  const chrome = spawn(findChrome(chromeArg), ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${prof}`,
    "--no-first-run", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--hide-scrollbars", "about:blank"],
    { stdio: "ignore" });
  const result = { page, failures: [], ok: false };
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
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
    await cdp.send("Page.navigate", { url: pathToFileURL(page).href });
    for (let i = 0; i < 100 && !loaded; i++) {
      const s = await cdp.send("Runtime.evaluate", { expression: "document.readyState", returnByValue: true });
      loaded = s.result.value === "complete";
      if (!loaded) await sleep(50);
    }
    await sleep(200);
    const measure = async () => (await cdp.send("Runtime.evaluate", {
      expression: `(()=>{const v=document.getElementById("sch-view"),s=v&&v.querySelector("svg");
        const r=s.getBoundingClientRect(); return {sw:Math.round(r.width),sh:Math.round(r.height),vw:v.clientWidth,vh:v.clientHeight};})()`,
      returnByValue: true,
    })).result.value;
    const fills = (box, where) => {
      if (!box) { fail(where + ": no schematic"); return; }
      if (box.sw > box.vw + 2 || box.sh > box.vh + 2) fail(`${where}: svg ${box.sw}x${box.sh} overflows ${box.vw}x${box.vh}`);
      if (box.sw < box.vw * 0.9 && box.sh < box.vh * 0.9) fail(`${where}: svg ${box.sw}x${box.sh} does not fill ${box.vw}x${box.vh}`);
    };
    const open = await measure();
    result.open = open;
    fills(open, "on open");
    await cdp.send("Runtime.evaluate", { expression: `document.querySelector('nav [data-tab="front"]').click()` });
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 900, height: 900, deviceScaleFactor: 1, mobile: false });
    await sleep(200);
    await cdp.send("Runtime.evaluate", { expression: `document.querySelector('nav [data-tab="sch"]').click()` });
    await sleep(400);
    const back = await measure();
    result.afterHiddenResize = back;
    fills(back, "after resize while hidden");
  } catch (e) {
    fail("crash: " + e.message);
  } finally {
    try { chrome.kill("SIGKILL"); } catch { /* already gone */ }
    // The profile dir can still be busy for a moment after the signal. Cleanup must not
    // swallow the JSON result: a thrown rmSync here is how a real failure came out blank.
    try { rmSync(work, { recursive: true, force: true, maxRetries: 5, retryDelay: 40 }); } catch { /* leftover profile */ }
  }
  result.ok = result.failures.length === 0;
  console.log(JSON.stringify(result));
  process.exit(result.ok ? 0 : 1);
}

main();
