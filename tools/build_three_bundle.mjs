#!/usr/bin/env node
// Rebuild src/pcbc/share/vendor/three-0.179.1-viewer.min.js: the part of three.js the review page's 3D tab uses
// (WebGLRenderer, GLTFLoader, OrbitControls, RoomEnvironment + PMREMGenerator, a textured plane for the ground shadow)
// as ONE classic IIFE that sets `window.PCBCThree`. The review page inlines it, so the page loads nothing and the model
// is parsed from bytes already in the page (GLTFLoader.parse): no fetch, no XHR, no import(), no Worker, and three's
// own network globals are stand-ins (NO_NETWORK below; see review.py, GLB_VIEWER).
//
// Reproducible: the source is three 0.179.1 (MIT) from a local node_modules copy, bundled by a pinned esbuild with
// fixed flags and a fixed entry (below). Every input file esbuild reads and the output are checked against the
// sha256 pins in this file; a mismatch writes nothing and exits 1.
//
//   node tools/build_three_bundle.mjs            rebuild, verify the pins, write the bundle + licence
//   node tools/build_three_bundle.mjs --check    rebuild in memory, verify the pins and that the committed bundle matches
//   node tools/build_three_bundle.mjs --print-pins   rebuild and print the pins (to update them on purpose)
//   options: --three DIR (…/node_modules/three)  --esbuild PATH (esbuild binary)  --out DIR
//
// Needs node >= 18 and a local esbuild + three (no network, no npm install). esbuild writes into a temporary directory
// ($TMPDIR) that is removed afterwards; only the two vendor files are kept.

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync, rmSync, mkdtempSync, realpathSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const VERSION = "0.179.1";
const ESBUILD_VERSION = "0.25.12";
const OUT_JS = `three-${VERSION}-viewer.min.js`;
const OUT_LICENSE = `three-${VERSION}.LICENSE`;
const DEFAULTS = {
  three: join(homedir(), "Documents/Zener/boards/emg16-tscircuit/node_modules/three"),
  esbuild: join(homedir(), "Documents/MaD/Software/Control/node_modules/.bin/esbuild"),
  out: resolve(HERE, "../src/pcbc/share/vendor"),
};

// The entry: exactly what the viewer uses, nothing else (esbuild tree-shakes the rest of three away).
const ENTRY = `export { WebGLRenderer, Scene, PerspectiveCamera, PMREMGenerator, Box3, Vector3, Color, NeutralToneMapping,
  SRGBColorSpace, REVISION, Mesh, PlaneGeometry, MeshBasicMaterial, CanvasTexture } from "three";
export { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
export { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
export { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
`;

// The network globals three's loaders name (FileLoader: fetch, Request, Headers, Response; ImageBitmapLoader and the
// GLTFParser's choice of image loader: createImageBitmap, fetch) are bound to these bundle-local stand-ins via esbuild
// --inject, so the bundle never reads the page's globals of those names at all: `typeof fetch` in the bundle sees this
// function, not window.fetch, and a host that pins window.fetch behind a getter (that counts or throws) is never asked.
// Calling one throws; the review page reaches none of them (it parses a GLB with no images and no `uri` buffers).
const NO_NETWORK_FILE = "pcbc-no-network.js";
const NO_NETWORK = `const refuse = (name) => function () {
  throw new Error("the pcbc review page loads nothing over the network (" + name + " is not available in its 3D viewer)");
};
export const fetch = refuse("fetch");
export const createImageBitmap = refuse("createImageBitmap");
export const Request = refuse("Request");
export const Headers = refuse("Headers");
export const Response = refuse("Response");
`;

const BANNER = `/*! three.js ${VERSION} (MIT, Copyright 2010-2025 three.js authors; full text in ${OUT_LICENSE}) - the subset the ` +
  `pcbc review 3D tab uses, bundled by tools/build_three_bundle.mjs (esbuild ${ESBUILD_VERSION}) as window.PCBCThree */`;

const FLAGS = ["--bundle", "--format=iife", "--global-name=PCBCThree", "--minify", "--legal-comments=none",
  "--target=es2020", "--platform=browser", "--log-level=warning", `--banner:js=${BANNER}`];

// sha256 of every file esbuild read (paths relative to the three package) and of the output. Update only on purpose
// (--print-pins), after reading what changed.
const PINS = {
  inputs: {
    "LICENSE": "bfe119ea4fd413f5f7ca3fcd63adb0c4a073ed39daa2fe7d3e6b769e21272601",
    "package.json": "219ef226f588ac2f07b2311a9c58900226cffb17583713649f48bbe78aff2197",
    "build/three.core.js": "3cd45beb4da9ce682bb7f5894dafd8a146379aa4b6b5aa3acd9ef5d47fdaaf92",
    "build/three.module.js": "ce6be0cb5ead0027e1f6094dd82ad43bba0886c03324a3b21fd9a33ac93fc2b4",
    "examples/jsm/controls/OrbitControls.js": "b97879c748170baadeb3fb84cea1ffdf4674e283dc06042f34e2acb95a76042c",
    "examples/jsm/environments/RoomEnvironment.js": "c20f0b4677f6128a138d7152b85cbef9091f4f45cdfa05adca04d40c1697c7ae",
    "examples/jsm/loaders/GLTFLoader.js": "caba6c51cfd8c7d5313bd7705a54b76bc0a7199d9822ecc497c5311eaffe8e5e",
    "examples/jsm/utils/BufferGeometryUtils.js": "cbcfe1864abedcc0122cb893373918fe14469491717a34ca6efcc38551805765",
  },
  output: "ac1c9ca838da1cb6ccef20a9f5b0a621caf0f79b95d5a907d27187effaad48c2",
};

const sha = (buf) => createHash("sha256").update(buf).digest("hex");

function args(argv) {
  const a = { ...DEFAULTS, mode: "write" };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--three") a.three = resolve(argv[++i]);
    else if (k === "--esbuild") a.esbuild = resolve(argv[++i]);
    else if (k === "--out") a.out = resolve(argv[++i]);
    else if (k === "--check") a.mode = "check";
    else if (k === "--print-pins") a.mode = "print";
    else throw new Error(`unknown argument ${k}`);
  }
  return a;
}

function build(a) {
  const pkg = JSON.parse(readFileSync(join(a.three, "package.json"), "utf8"));
  if (pkg.name !== "three" || pkg.version !== VERSION) throw new Error(`${a.three} is ${pkg.name}@${pkg.version}, not three@${VERSION}`);
  if (pkg.license !== "MIT") throw new Error(`three's licence is ${pkg.license}, expected MIT`);
  const ver = spawnSync(a.esbuild, ["--version"], { encoding: "utf8" });
  if (ver.status !== 0 || ver.stdout.trim() !== ESBUILD_VERSION) throw new Error(`esbuild ${ver.stdout.trim()} at ${a.esbuild}, expected ${ESBUILD_VERSION}`);
  // `three` must resolve to a.three: run from the directory that holds its node_modules
  const nodeModules = dirname(a.three);
  if (!nodeModules.endsWith("node_modules")) throw new Error(`${a.three} is not inside a node_modules directory`);
  const work = realpathSync(mkdtempSync(join(tmpdir(), "pcbc-three-"))); // esbuild reports real paths
  try {
    const meta = join(work, "meta.json");
    const noNetwork = join(work, NO_NETWORK_FILE); // the stand-ins, written for this build only
    writeFileSync(noNetwork, NO_NETWORK);
    const out = join(work, "out.js");
    const r = spawnSync(a.esbuild, [...FLAGS, `--inject:${noNetwork}`, "--sourcefile=pcbc-three-entry.js", `--metafile=${meta}`,
      `--outfile=${out}`], { cwd: dirname(nodeModules), input: Buffer.from(ENTRY), maxBuffer: 64 << 20 });
    if (r.status !== 0) throw new Error(`esbuild failed: ${r.stderr.toString()}`);
    // the bundled sources, plus the package.json that names the version and the LICENSE copied beside the bundle
    const inputs = { "LICENSE": sha(readFileSync(join(a.three, "LICENSE"))), "package.json": sha(readFileSync(join(a.three, "package.json"))) };
    const read = Object.keys(JSON.parse(readFileSync(meta, "utf8")).inputs).map((p) => [p, resolve(dirname(nodeModules), p)]);
    if (!read.some(([, abs]) => abs === noNetwork)) throw new Error(`esbuild did not read ${NO_NETWORK_FILE}: the stand-ins are not in the bundle`);
    for (const [p, abs] of read.sort((x, y) => (x[0] < y[0] ? -1 : 1))) {
      if (p === "pcbc-three-entry.js" || p === "<stdin>" || abs === noNetwork) continue;
      if (!abs.startsWith(a.three + "/")) throw new Error(`esbuild read ${abs}, outside ${a.three}`);
      inputs[abs.slice(a.three.length + 1)] = sha(readFileSync(abs));
    }
    return { js: readFileSync(out), inputs, license: readFileSync(join(a.three, "LICENSE")) };
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

function verify(b) {
  const bad = [];
  const want = PINS.inputs, got = b.inputs;
  for (const k of new Set([...Object.keys(want), ...Object.keys(got)])) if (want[k] !== got[k]) bad.push(`input ${k}: pinned ${want[k] || "-"} got ${got[k] || "-"}`);
  if (PINS.output !== sha(b.js)) bad.push(`output: pinned ${PINS.output} got ${sha(b.js)}`);
  return bad;
}

function main() {
  const a = args(process.argv.slice(2));
  const b = build(a);
  if (a.mode === "print") {
    process.stdout.write(JSON.stringify({ inputs: b.inputs, output: sha(b.js), bytes: b.js.length }, null, 2) + "\n");
    return 0;
  }
  const bad = verify(b);
  if (bad.length) {
    process.stderr.write("pins do not match (nothing written):\n  " + bad.join("\n  ") + "\n");
    return 1;
  }
  const outJs = join(a.out, OUT_JS), outLic = join(a.out, OUT_LICENSE);
  const license = Buffer.concat([Buffer.from(`three.js ${VERSION} - https://github.com/mrdoob/three.js - bundled into ${OUT_JS}\n\n`), b.license]);
  if (a.mode === "check") {
    const okJs = existsSync(outJs) && sha(readFileSync(outJs)) === sha(b.js);
    const okLic = existsSync(outLic) && readFileSync(outLic).equals(license);
    process.stdout.write(`${OUT_JS} ${okJs ? "matches" : "DIFFERS"}; ${OUT_LICENSE} ${okLic ? "matches" : "DIFFERS"}; sha256 ${sha(b.js)}\n`);
    return okJs && okLic ? 0 : 1;
  }
  writeFileSync(outJs, b.js);
  writeFileSync(outLic, license);
  process.stdout.write(`wrote ${outJs} (${b.js.length} bytes, sha256 ${sha(b.js)}) and ${outLic}\n`);
  return 0;
}

process.exit(main());
