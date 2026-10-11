"""One-page HTML: schematic, copper SVGs, 3D GLB. No Zener, no default.net."""

from __future__ import annotations

import base64
import csv
import html
import json
import os
import re
import shutil
import subprocess
import webbrowser
from pathlib import Path

from .compile import compile_design
from .fab import kicad_cli
from .netcheck import KicadMissing, check_schematic
from .language import load_board
from .project import layout_dir
from .sch_emit import emit_schematic_file

# The 3D viewer: three.js 0.179.1 (MIT), the subset the tab uses bundled by tools/build_three_bundle.mjs into one classic
# script that sets `window.PCBCThree`, inlined in the page (pinned, sha256-checked; its licence sits beside it).
THREE_BUNDLE = Path(__file__).resolve().parent / "share" / "vendor" / "three-0.179.1-viewer.min.js"


def three_js() -> str:
    """The vendored three.js bundle, safe inside an inline `<script>` (no `</script` in it)."""
    return THREE_BUNDLE.read_text(encoding="utf-8").replace("</script", "<\\/script")


# The 3D tab makes NO request of any kind: the model's bytes are base64 text in a non-executed `<script
# type="application/octet-stream">`, and GLTFLoader.parse() builds the scene straight from them. Nothing calls fetch,
# XHR, import() or a Worker, and window.fetch is never read or replaced: not by the viewer, and not by the bundled
# three.js either, whose loaders' network globals (fetch, createImageBitmap, Request, Headers, Response) the build binds
# to bundle-local stand-ins that throw (tools/build_three_bundle.mjs; three's ImageBitmapLoader, which the GLTF parser
# builds, used to run `typeof fetch` and so broke under a host whose fetch getter throws). Why: Claude's file pane swaps
# the page's fetch for one that posts its arguments to the parent window, and the owner's 3D tab (model-viewer, which
# loads its model with fetch(new Request(url))) showed "DataCloneError: Failed to execute 'postMessage' on 'Window':
# Request object could not be cloned." Answering that fetch from inside the page held only while the host ASSIGNED its
# shim; a host that pins or redefines fetch got the same error back, and the host's code is not ours to see. So the page
# does not depend on fetch at all. GLTFLoader.parse on a binary glTF whose one buffer is the GLB's own BIN chunk and
# which has no images reads nothing from anywhere (GLTFParser.loadBuffer returns the BIN chunk; only a `uri` buffer or
# an image goes through FileLoader / ImageBitmapLoader, i.e. fetch, which in this bundle is the stand-in that throws;
# Draco/KTX2/meshopt need decoders the page never sets, so they fail instead); KiCad's GLBs are exactly that, and one
# with images or `uri` buffers is refused with a message, not fetched. The view initialises the first time the tab is
# shown (it has no size before), renders only when the camera moves, the box resizes or the pixel ratio changes, and
# shows any failure as text in the page. (The page also names an empty `data:` icon, so a browser that opens it from a
# local web server asks for no /favicon.ico either.)
GLB_VIEWER = """
(function () {
  const glbText = document.getElementById("board-glb");
  const box = document.getElementById("board-3d");
  const status = document.getElementById("board-3d-status");
  const nojs = document.getElementById("board-3d-nojs");
  if (!glbText || !box) return;
  // Scripts run: the view's box and hint show and the note for a viewer without JavaScript goes.
  const hint = document.getElementById("board-3d-hint");
  box.hidden = false;
  if (hint) hint.hidden = false;
  if (nojs) nojs.hidden = true;
  const T = window.PCBCThree;
  let started = false, renderer = null, scene = null, camera = null, pending = 0;
  function fail(err) {
    box.dataset.state = "error";
    if (status) {
      status.textContent = "The 3D view could not be shown. " + String(err);
      status.hidden = false;
    }
  }
  function draw() { pending = 0; renderer.render(scene, camera); }
  function redraw() { if (!pending) pending = requestAnimationFrame(draw); }
  // The canvas follows the device pixel ratio: on every resize (browser zoom resizes the box too) and when the ratio
  // alone changes (the window moved to a display with another one), so the view stays sharp.
  function ratio() { return Math.min(window.devicePixelRatio || 1, 2); }
  function watchRatio() {
    window.matchMedia("(resolution: " + (window.devicePixelRatio || 1) + "dppx)").addEventListener("change", function () {
      if (fit()) redraw();
      watchRatio();
    }, { once: true });
  }
  function fit() {
    const w = box.clientWidth, h = box.clientHeight;
    if (!w || !h) return false;
    if (renderer.getPixelRatio() !== ratio()) renderer.setPixelRatio(ratio());
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    return true;
  }
  function glbBytes() {
    const raw = atob(glbText.textContent.trim());
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const view = new DataView(bytes.buffer);
    if (bytes.length < 20 || view.getUint32(0, true) !== 0x46546C67 || view.getUint32(16, true) !== 0x4E4F534A) {
      throw new Error("the model is not a binary glTF");
    }
    const json = JSON.parse(new TextDecoder().decode(bytes.subarray(20, 20 + view.getUint32(12, true))));
    if ((json.images || []).length || (json.buffers || []).some((b, i) => i > 0 || b.uri !== undefined)) {
      throw new Error("the model refers to images or data outside the file, which this page does not load");
    }
    return bytes.buffer;
  }
  // Lighting. three's RoomEnvironment lights the board, minus its overhead (+y) area light: looking straight down, the
  // camera saw that light mirrored in every metal surface, so KiCad's grey metallic pads came out near-white, the same
  // as the silkscreen, on the top-down view where orientation marks are read. Metals also reflect the room at
  // METAL_REFLECTION of its strength (pads then read mid-grey from above and at the opening angle, as the old viewer
  // drew them), and a soft shadow under the board sets it on the ground.
  const METAL_REFLECTION = 0.65;
  function environment() {
    const room = new T.RoomEnvironment();
    room.traverse(function (o) { if (o.isMesh && o.position.y === 20 && o.scale.y < 0.2) o.visible = false; });
    const pmrem = new T.PMREMGenerator(renderer);
    const texture = pmrem.fromScene(room, 0.04).texture;
    pmrem.dispose();
    room.dispose();
    return texture;
  }
  function softenMetals(model) {
    model.traverse(function (o) {
      for (const m of o.isMesh ? [].concat(o.material) : []) {
        if (m.metalness > 0.5) {
          m.envMap = scene.environment;
          m.envMapIntensity = METAL_REFLECTION;
          m.needsUpdate = true;
        }
      }
    });
  }
  function groundShadow(size) {
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const g = c.getContext("2d");
    g.shadowColor = "rgba(0, 0, 0, 0.75)";
    g.shadowBlur = 14;
    g.shadowOffsetX = 1000;
    g.fillRect(24 - 1000, 24, 80, 80); // only its blurred shadow lands on the canvas
    const plane = new T.Mesh(new T.PlaneGeometry(size.x * 1.6, size.z * 1.6),
      new T.MeshBasicMaterial({ map: new T.CanvasTexture(c), transparent: true, depthWrite: false, toneMapped: false }));
    plane.rotation.x = -Math.PI / 2;
    plane.position.y = -size.y / 2 - size.y * 0.05;
    plane.renderOrder = -1;
    return plane;
  }
  function shown(gltf) {
    try {
      const model = gltf.scene;
      scene.add(model);
      softenMetals(model);
      const bounds = new T.Box3().setFromObject(model);
      if (bounds.isEmpty()) throw new Error("the model is empty");
      model.position.sub(bounds.getCenter(new T.Vector3()));
      scene.add(groundShadow(bounds.getSize(new T.Vector3())));
      const radius = bounds.getSize(new T.Vector3()).length() / 2;
      const half = (camera.fov * Math.PI) / 360;
      const dist = (1.1 * radius) / Math.sin(Math.min(half, Math.atan(Math.tan(half) * camera.aspect)));
      const tilt = (55 * Math.PI) / 180;
      camera.position.set(0, Math.cos(tilt) * dist, Math.sin(tilt) * dist);
      camera.near = dist / 100;
      camera.far = dist * 100;
      camera.updateProjectionMatrix();
      const controls = new T.OrbitControls(camera, renderer.domElement);
      controls.minDistance = radius * 0.3;
      controls.maxDistance = dist * 4;
      controls.update();
      controls.addEventListener("change", redraw);
      box.dataset.state = "ready";
      draw();
    } catch (err) {
      fail(err);
    }
  }
  function start() {
    started = true;
    box.dataset.state = "loading";
    try {
      if (!T) throw new Error("the bundled three.js did not load");
      const data = glbBytes();
      renderer = new T.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(ratio());
      renderer.toneMapping = T.NeutralToneMapping;
      renderer.toneMappingExposure = 0.9;
      box.appendChild(renderer.domElement);
      scene = new T.Scene();
      scene.background = new T.Color(0x0b0e0c);
      scene.environment = environment();
      camera = new T.PerspectiveCamera(30, 1, 0.001, 10);
      fit();
      watchRatio();
      new T.GLTFLoader().parse(data, "", shown, fail);
    } catch (err) {
      fail(err);
    }
  }
  new ResizeObserver(() => {
    if (!started) {
      if (box.clientWidth && box.clientHeight) start();
    } else if (renderer && fit()) {
      redraw();
    }
  }).observe(box);
})();
"""


def _kicad_3d_dir() -> Path | None:
    for key in ("KICAD10_3DMODEL_DIR", "KICAD_3DMODEL_DIR"):
        raw = os.environ.get(key)
        if raw and Path(raw).is_dir():
            return Path(raw)
    for candidate in (
        Path("/usr/share/kicad/3dmodels"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels"),
    ):
        if candidate.is_dir():
            return candidate
    return None


def _kicad_env() -> dict[str, str]:
    env = os.environ.copy()
    models = _kicad_3d_dir()
    if models is not None:
        env.setdefault("KICAD10_3DMODEL_DIR", str(models))
        env.setdefault("KICAD_3DMODEL_DIR", str(models))
    return env


def _run(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_kicad_env())
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-1500:],
        "stderr": (proc.stderr or "")[-1500:],
    }


def _export_svg(cli: Path, pcb: Path, out: Path, layers: str, *, mirror: bool = False) -> dict:
    cmd = [
        str(cli),
        "pcb",
        "export",
        "svg",
        "--layers",
        layers,
        "--page-size-mode",
        "2",
        "--fit-page-to-board",
        "--exclude-drawing-sheet",
        "--check-zones",
        "--mode-single",
    ]
    if mirror:
        cmd.append("--mirror")
    cmd.extend(["-o", str(out), str(pcb)])
    return _run(cmd)


def _export_glb(cli: Path, pcb: Path, out: Path) -> dict:
    cmd = [
        str(cli),
        "pcb",
        "export",
        "glb",
        "--force",
        "--subst-models",
        "--include-tracks",
        "--include-pads",
        "--include-zones",
        "--include-silkscreen",
        "--include-soldermask",
        "--no-dnp",
        "-o",
        str(out),
        str(pcb),
    ]
    models = _kicad_3d_dir()
    if models is not None:
        cmd[cmd.index("-o"):0] = ["-D", f"KICAD10_3DMODEL_DIR={models}"]
    return _run(cmd)


# Still pictures of the board for the static page (static.html), the one a viewer that runs no JavaScript can show (the
# owner's phone app showed the review with scripts off: no tab switched and the interactive view could not start).
# kicad-cli ray-traces them when the review is written; the page carries them as JPEG `data:` URIs, about 100 kB each
# at this size (a PNG of the same render is about 2.4x that, and a render is a photograph, not line art: JPEG).
# (name, caption, view arguments)
STILL_VIEWS = (
    ("top", "Top", ["--side", "top"]),
    ("bottom", "Bottom, seen from below", ["--side", "bottom"]),
    ("front-left", "Angled, from the front left", ["--side", "top", "--rotate", "-50,0,25", "--perspective"]),
    ("back-right", "Angled, from the back right", ["--side", "top", "--rotate", "-50,0,-155", "--perspective"]),
)
STILL_SIZE = (1200, 900)


def _render_still(cli: Path, pcb: Path, out: Path, view: list[str]) -> dict:
    cmd = [
        str(cli),
        "pcb",
        "render",
        *view,
        "--width",
        str(STILL_SIZE[0]),
        "--height",
        str(STILL_SIZE[1]),
        "--quality",
        "high",
        "--background",
        "opaque",
    ]
    models = _kicad_3d_dir()
    if models is not None:
        cmd.extend(["-D", f"KICAD10_3DMODEL_DIR={models}"])
    if out.exists():
        out.unlink()
    cmd.extend(["-o", str(out), str(pcb)])
    return _run(cmd)


def _render_stills(cli: Path, pcb: Path, out_dir: Path) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """The still renders of STILL_VIEWS (run side by side: each is seconds of ray tracing). Returns the kicad-cli steps
    and (caption, mime, base64) for every picture that came out."""
    from concurrent.futures import ThreadPoolExecutor

    outs = [out_dir / f"render-{name}.jpg" for name, _, _ in STILL_VIEWS]
    with ThreadPoolExecutor(max_workers=len(STILL_VIEWS)) as pool:
        steps = list(pool.map(lambda job: _render_still(cli, pcb, job[0], job[1][2]), zip(outs, STILL_VIEWS)))
    stills = []
    for out, (_, caption, _), step in zip(outs, STILL_VIEWS, steps):
        if step["returncode"] == 0 and out.is_file() and out.stat().st_size > 100:
            stills.append((caption, "image/jpeg", base64.b64encode(out.read_bytes()).decode("ascii")))
    return steps, stills


def _export_sch_svg(cli: Path, sch: Path, out_dir: Path) -> dict:
    return _run(
        [
            str(cli),
            "sch",
            "export",
            "svg",
            "--exclude-drawing-sheet",
            "--no-background-color",
            "-o",
            str(out_dir),
            str(sch),
        ]
    )


_PATH_XY = re.compile(r"[ML]\s*([0-9.+-]+)\s+([0-9.+-]+)")
_ATTR_XY = re.compile(r'\bx="([0-9.+-]+)"\s+y="([0-9.+-]+)"')


def crop_svg_to_content(svg: str, *, pad_mm: float = 10.0, px_per_mm: float = 10.0) -> str:
    xs: list[float] = []
    ys: list[float] = []
    for m in _PATH_XY.finditer(svg):
        x, y = float(m.group(1)), float(m.group(2))
        if 2.0 < x < 2000 and 2.0 < y < 2000:
            xs.append(x)
            ys.append(y)
    for m in _ATTR_XY.finditer(svg):
        x, y = float(m.group(1)), float(m.group(2))
        if 2.0 < x < 2000 and 2.0 < y < 2000:
            xs.append(x)
            ys.append(y)
    if len(xs) < 4:
        return svg
    x0, y0 = min(xs) - pad_mm, min(ys) - pad_mm
    x1, y1 = max(xs) + pad_mm, max(ys) + pad_mm
    w, h = max(x1 - x0, 20.0), max(y1 - y0, 20.0)
    out = re.sub(r'viewBox="[^"]+"', f'viewBox="{x0:.3f} {y0:.3f} {w:.3f} {h:.3f}"', svg, count=1)
    out = re.sub(r'\bwidth="[^"]+"', f'width="{w * px_per_mm:.0f}px"', out, count=1)
    out = re.sub(r'\bheight="[^"]+"', f'height="{h * px_per_mm:.0f}px"', out, count=1)
    return out


# The board each fresh stage leaves, most finished first: `build.stale_reason`'s stage -> the file.
_STAGE_PCB = {"fab": "fab/layout.kicad_pcb", "route": "routed/layout.kicad_pcb", "place": "placed/layout.kicad_pcb", "sch": "seed/layout.kicad_pcb", "seed": "seed/layout.kicad_pcb"}


def _best_pcb(layout: Path, stage: str | None) -> Path | None:
    """The board of the last stage whose stamp matches (`build.stale_reason`), never a board by its
    presence alone: until the fifth review a hand-edited `fab/layout.kicad_pcb` the build called stale
    was the one review read, plotted and called "KiCad DRC clean ... as board.py says"."""
    rel = _STAGE_PCB.get(stage or "")
    if rel is None:
        return None
    p = layout / rel
    return p if p.exists() else None


def _has_copper(pcb: Path) -> bool:
    text = pcb.read_text()
    return "\n\t(segment" in text or "\n\t(zone" in text


def _copper_gate(design, pcb: Path, stage: str | None, stale: str | None) -> dict | None:
    """`netcheck.check_copper` on the reviewed board, run once for the copper note and the static page's net and DRC
    counts: for a fresh routed or fab board, and for the board an unfinished route emitted (its open nets are the point).
    None where the review does not judge copper (stale outputs, nothing routed); {"error": ...} without kicad-cli."""
    unfinished = bool(stale and "unfinished" in stale)
    if (stale and not unfinished) or (not unfinished and stage not in ("route", "fab")) or not _has_copper(pcb):
        return None
    from .netcheck import check_copper

    try:
        return check_copper(design, pcb, refill=False)
    except KicadMissing as exc:
        return {"error": str(exc)}


def _copper_note(design, pcb: Path, stage: str | None = None, stale: str | None = None, gate: dict | None = None) -> str:
    if stale and "unfinished" in stale:
        return "Copper: the route stopped unfinished and wrote no stamp. This picture is the board it emitted; it is not a finished build."
    if stale:
        return f"Copper: not judged — the build's outputs are stale ({stale}); run pcbc build."
    if stage not in ("route", "fab"):
        return "Copper: not routed yet (pcbc build routes it and judges it)."
    text = pcb.read_text()
    if "\n\t(segment" not in text and "\n\t(zone" not in text:
        return "Copper: not routed yet (pcbc build routes it and judges it)."
    if gate is None:
        gate = _copper_gate(design, pcb, stage, stale)
    if gate is None or "error" in gate:
        return f"Copper: unchecked ({(gate or {}).get('error', 'kicad-cli')})."
    n_seg = text.count("\n\t(segment")
    n_via = text.count("\n\t(via")
    if gate["ok"]:
        return f"Copper: {n_seg} tracks, {n_via} vias; KiCad DRC clean, nothing unconnected, pads bound as board.py says."
    return f"COPPER FAILS the gate ({len(gate['fails'])}): " + "; ".join(gate["fails"][:5])


def _outline_mm(pcb, job) -> tuple[float, float, str]:
    """(width, height, "declared" | "derived") of the outline the emitted board carries on Edge.Cuts: the review reads the
    board, not a recompile, which cannot know a derived outline (docs/board-outline.md B.4)."""
    from .sexp import edge_outline

    rect = None
    try:
        rect = edge_outline(Path(pcb).read_text())
    except OSError:
        rect = None
    rect = rect or job.outline
    if rect is None:
        return (0.0, 0.0, job.outline_source)
    return (round(rect[2] - rect[0], 4), round(rect[3] - rect[1], 4), job.outline_source)


# The page's views: (section id, tab name). The first is shown when the page opens.
TABS = (
    ("sch", "Schematic"),
    ("front", "Front copper"),
    ("silk", "Silkscreen"),
    ("back", "Back copper"),
    ("both", "Both layers"),
    ("three", "3D"),
    ("bom", "BOM"),
    ("src", "board.py"),
)


def render_html(
    *,
    title: str,
    board_mm: tuple[float, float],
    layers: int,
    stackup: str,
    pcb_name: str,
    source_text: str | None,
    sch_svg: str | None,
    front_svg: str | None,
    back_svg: str | None,
    copper_svg: str | None,
    silk_svg: str | None,
    glb_b64: str | None,
    bom_rows: list[list[str]],
    notes: list[str],
) -> str:
    """The full review page (index.html): tabs, the schematic's zoom and drag, the interactive 3D view. A viewer that
    runs no JavaScript still switches its tabs (CSS) and gets a plain schematic; static.html (`render_static_html`) is
    the page made for such a viewer."""

    def panel_svg(svg: str | None, empty: str) -> str:
        if svg:
            return f'<div class="plot">{svg}</div>'
        return f'<p class="empty">{html.escape(empty)}</p>'

    src_block = (
        f'<pre class="src">{html.escape(source_text)}</pre>'
        if source_text
        else '<p class="empty">No board.py</p>'
    )
    # Without JavaScript the schematic is a plain picture the width of the screen (the phone's own pinch zoom and scroll
    # work on it); the script turns it into the zoom-and-drag view, shows its buttons and its hint.
    sch_block = (
        '<div class="plot sch" id="sch-view">'
        '<div class="sch-tools" hidden>'
        '<button type="button" id="sch-out" title="Zoom out">−</button>'
        '<button type="button" id="sch-fit" title="Show the whole sheet">Fit</button>'
        '<button type="button" id="sch-in" title="Zoom in">+</button>'
        "</div>"
        f'<div class="sch-stage" id="sch-stage">{sch_svg}</div>'
        '<p class="sch-hint" hidden>Scroll to zoom. Drag to move.</p>'
        "</div>"
        if sch_svg
        else '<p class="hint">No schematic SVG. kicad-cli sch export svg failed.</p>'
    )
    # Without JavaScript the 3D tab says where the board's pictures are instead of showing an empty box; the viewer
    # script shows its box and hint and hides this note.
    nojs_3d = (
        '<p class="hint" id="board-3d-nojs">The interactive 3D view needs JavaScript, which this viewer does not run. '
        "static.html, written beside this page, shows the board in still renders.</p>"
    )
    glb_block = (
        f"""<script type="application/octet-stream" id="board-glb">{glb_b64}</script>
        <div id="board-3d" class="viewer3d" data-state="idle" hidden></div>
        <p class="viewer-error" id="board-3d-status" role="status" hidden></p>
        <p class="hint" id="board-3d-hint" hidden>Drag to orbit, right-drag to pan, scroll to zoom. Chip footprints here have no STEP — pads and copper still show.</p>
        {nojs_3d}"""
        if glb_b64
        else '<p class="empty">GLB export failed. 2D copper still reviews routing.</p>'
    )
    if bom_rows:
        head, *body = bom_rows
        th = "".join(f"<th>{html.escape(c)}</th>" for c in head)
        trs = []
        for row in body:
            tds = "".join(f"<td>{html.escape(c)}</td>" for c in row)
            trs.append(f"<tr>{tds}</tr>")
        bom_html = f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'
    else:
        bom_html = '<p class="empty">No fab/bom.csv yet. Run pcbc build first.</p>'
    notes_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes) or "<li>No extra notes.</li>"
    # The tabs work without JavaScript: one radio button per view (visually hidden, still focusable, arrow keys move
    # between them), a <label> for each in the nav, and CSS that shows the section whose radio is checked. Tapping a
    # label checks its radio natively, with no script and no change of URL fragment. The owner's phone app showed the
    # page with scripts off and the old script-only tabs stayed on the schematic.
    tab_state = "".join(
        f'<input type="radio" class="tab-state" name="view" id="tab-{key}" autocomplete="off"{" checked" if i == 0 else ""}/>'
        for i, (key, _) in enumerate(TABS)
    )
    tab_labels = "".join(f'<label for="tab-{key}" data-tab="{key}">{html.escape(name)}</label>' for key, name in TABS)
    tab_css = "\n".join(
        f'  #tab-{key}:checked ~ #{key} {{ display: block; }}\n'
        f'  #tab-{key}:checked ~ nav label[for="tab-{key}"] {{ color: var(--bg); background: var(--accent); border-color: var(--accent); }}\n'
        f'  #tab-{key}:focus-visible ~ nav label[for="tab-{key}"] {{ outline: 2px solid var(--ink); outline-offset: 2px; }}'
        for key, _ in TABS
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<link rel="icon" href="data:,"/>
<title>{html.escape(title)} — pcbc review</title>
<style>
  :root {{
    --bg: #10140f;
    --panel: #181e16;
    --ink: #e7eee2;
    --muted: #8ea382;
    --line: #2a3626;
    --accent: #d4893a;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.45 ui-sans-serif, system-ui, sans-serif; }}
  [hidden] {{ display: none !important; }}
  header {{
    padding: 18px 22px 10px;
    border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  }}
  header > div {{ min-width: 0; max-width: 100%; }}
  header h1 {{ margin: 0; font-size: 20px; letter-spacing: .02em; overflow-wrap: anywhere; }}
  header p {{ margin: 4px 0 0; color: var(--muted); }}
  .meta {{ color: var(--muted); font-family: ui-monospace, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }}
  .tab-state {{
    position: fixed; top: 0; left: 0; width: 1px; height: 1px; margin: 0;
    opacity: 0; pointer-events: none;
  }}
  nav {{
    display: flex; gap: 6px; padding: 10px 22px; position: sticky; top: 0;
    background: #10140fee; border-bottom: 1px solid var(--line); z-index: 2; flex-wrap: wrap;
  }}
  nav label {{
    background: transparent; color: var(--muted); border: 1px solid var(--line);
    border-radius: 999px; padding: 6px 14px; cursor: pointer; font: inherit;
    user-select: none; -webkit-user-select: none;
  }}
  section {{ display: none; padding: 18px 22px 40px; }}
{tab_css}
  .plot {{
    background: #0b0e0c; border: 1px solid var(--line); border-radius: 10px;
    overflow: auto; max-height: 78vh; padding: 8px;
  }}
  .plot svg {{ display: block; width: 100%; height: auto; }}
  .plot.sch {{ background: #f4f0e4; max-height: none; padding: 0; position: relative; }}
  .plot.sch.zoomable {{ height: 78vh; overflow: auto; cursor: grab; touch-action: none; }}
  .plot.sch.grabbing {{ cursor: grabbing; }}
  .plot.sch.zoomable .sch-stage {{
    min-width: 100%; min-height: 100%;
    display: flex; align-items: center; justify-content: center;
  }}
  .plot.sch.zoomable svg {{ width: 100%; height: 100%; max-width: none; flex: none; }}
  .sch-tools {{
    position: absolute; top: 8px; right: 8px; display: flex; gap: 6px; z-index: 1;
  }}
  .sch-tools button {{
    background: #181e16; color: var(--ink); border: 1px solid var(--line);
    border-radius: 8px; padding: 4px 10px; cursor: pointer; font: inherit;
  }}
  .sch-hint {{
    position: absolute; left: 10px; bottom: 8px; margin: 0;
    color: #5c6b56; font-size: 12px; pointer-events: none;
  }}
  .src {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 14px; overflow: auto; max-height: 78vh; font: 12px/1.4 ui-monospace, Menlo, monospace;
    white-space: pre; color: #d5e4cc;
  }}
  .hint, .empty {{ color: var(--muted); }}
  .viewer3d {{ width: 100%; height: min(72vh, 720px); background: #0b0e0c; border: 1px solid var(--line);
    border-radius: 10px; overflow: hidden; }}
  .viewer3d canvas {{ display: block; width: 100%; height: 100%; }}
  .viewer-error {{ color: var(--accent); font-family: ui-monospace, Menlo, monospace; font-size: 13px; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border-bottom: 1px solid var(--line); text-align: left; padding: 6px 8px;
    font-family: ui-monospace, Menlo, monospace; }}
  th {{ color: var(--accent); font-weight: 600; }}
  ul {{ color: var(--muted); }}
  @media (max-width: 600px) {{
    header {{ padding: 14px 16px 8px; }}
    nav {{ padding: 8px 16px; gap: 5px; }}
    nav label {{ padding: 5px 11px; }}
    section {{ padding: 14px 16px 32px; }}
  }}
</style>
</head>
<body>
<header>
  <div>
    <h1>{html.escape(title)}</h1>
    <p>Schematic · layout · 3D — KiCad exports, one page.</p>
  </div>
  <div class="meta">
    {board_mm[0]:g}×{board_mm[1]:g} mm{(" (" + board_mm[2] + ")") if len(board_mm) > 2 else ""} · {layers}L · {html.escape(stackup)}<br/>
    {html.escape(pcb_name)}
  </div>
</header>
{tab_state}
<nav aria-label="Views">{tab_labels}</nav>
<section id="sch">{sch_block}</section>
<section id="front">{panel_svg(front_svg, "Front SVG missing — kicad-cli pcb export svg failed.")}</section>
<section id="silk">{panel_svg(silk_svg, "Silk SVG missing.")}</section>
<section id="back">{panel_svg(back_svg, "Back SVG missing.")}</section>
<section id="both">{panel_svg(copper_svg, "Combined copper SVG missing.")}</section>
<section id="three">{glb_block}</section>
<section id="bom">
  {bom_html}
  <h3>Notes</h3>
  <ul>{notes_html}</ul>
</section>
<section id="src">{src_block}</section>
{("<script>" + three_js() + "</script>" + chr(10) + "<script>" + GLB_VIEWER + "</script>") if glb_b64 else ""}
<script>
  // The tabs need no script (radio buttons and CSS above). The script adds the schematic's zoom and drag.
  const schView = document.getElementById("sch-view");
  const schStage = document.getElementById("sch-stage");
  const schSvg = schView && schView.querySelector("svg");
  if (schView && schStage && schSvg) {{
    schView.classList.add("zoomable");
    schView.querySelectorAll(".sch-tools, .sch-hint").forEach((el) => {{ el.hidden = false; }});
    let scale = 1;
    const vb = schSvg.viewBox.baseVal;
    const aspect = vb.width / vb.height || 1;
    function fitted() {{
      const bw = schView.clientWidth;
      const bh = schView.clientHeight;
      if (!bw || !bh) return null;
      let w = bw, h = w / aspect;
      if (h > bh) {{ h = bh; w = h * aspect; }}
      return {{ w, h }};
    }}
    function layout() {{
      const f = fitted();
      if (!f) return;
      const w = f.w * scale, h = f.h * scale;
      schSvg.style.width = w + "px";
      schSvg.style.height = h + "px";
      if (scale === 1) {{
        schStage.style.width = "100%";
        schStage.style.height = "100%";
      }} else {{
        schStage.style.width = w + "px";
        schStage.style.height = h + "px";
      }}
    }}
    function zoomAt(next, clientX, clientY) {{
      const prev = scale;
      scale = Math.min(8, Math.max(1, next));
      const rect = schView.getBoundingClientRect();
      const ox = schView.scrollLeft + (clientX - rect.left);
      const oy = schView.scrollTop + (clientY - rect.top);
      layout();
      const k = scale / prev;
      schView.scrollLeft = ox * k - (clientX - rect.left);
      schView.scrollTop = oy * k - (clientY - rect.top);
    }}
    layout();
    // A hidden tab has no size, so a resize while it is hidden must not be written down. Showing it
    // again is the radio's change event, and by then the section already has its box: buck came back
    // 1px wide when the fit waited for a later frame that never arrived. Fit on the change itself.
    function relayout() {{
      if (scale !== 1) return;
      if (!document.getElementById("tab-sch").checked) return;
      if (!fitted()) return;
      layout();
    }}
    new ResizeObserver(() => relayout()).observe(schView);
    document.getElementById("tab-sch").addEventListener("change", () => relayout());
    window.addEventListener("resize", () => relayout());
    schView.addEventListener("wheel", (e) => {{
      e.preventDefault();
      zoomAt(scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12), e.clientX, e.clientY);
    }}, {{ passive: false }});
    let drag = null;
    schView.addEventListener("pointerdown", (e) => {{
      if (e.target.closest(".sch-tools")) return;
      e.preventDefault();
      drag = {{ x: e.clientX, y: e.clientY, sl: schView.scrollLeft, st: schView.scrollTop }};
      schView.setPointerCapture(e.pointerId);
      schView.classList.add("grabbing");
    }});
    schView.addEventListener("pointermove", (e) => {{
      if (!drag) return;
      e.preventDefault();
      schView.scrollLeft = drag.sl - (e.clientX - drag.x);
      schView.scrollTop = drag.st - (e.clientY - drag.y);
    }});
    function endDrag() {{ drag = null; schView.classList.remove("grabbing"); }}
    schView.addEventListener("pointerup", endDrag);
    schView.addEventListener("pointercancel", endDrag);
    document.getElementById("sch-in").addEventListener("click", () => zoomAt(scale * 1.25, schView.getBoundingClientRect().left + schView.clientWidth / 2, schView.getBoundingClientRect().top + schView.clientHeight / 2));
    document.getElementById("sch-out").addEventListener("click", () => zoomAt(scale / 1.25, schView.getBoundingClientRect().left + schView.clientWidth / 2, schView.getBoundingClientRect().top + schView.clientHeight / 2));
    document.getElementById("sch-fit").addEventListener("click", () => {{
      scale = 1;
      layout();
      schView.scrollLeft = 0;
      schView.scrollTop = 0;
    }});
  }}
</script>
</body>
</html>
"""


# ------------------------------------------------------------------------------------------------ the static page
# static.html, written beside index.html by every review: the review as pictures, for a viewer that runs no JavaScript
# (the owner's phone app showed index.html with scripts off and stayed on the schematic). It has no <script> at all, makes
# no request (its own Content-Security-Policy is default-src 'none' with img-src data:), and is one file that reads top
# to bottom on a 375 px screen with the phone's own pinch zoom: a status header, then the schematic, the copper, the
# silkscreen, four 3D renders and the BOM. index.html stays the full, interactive page.
#
# Pictures, chosen by measurement (2026-10-09, the five example boards): the schematic and the board plots are KiCad's
# own SVGs, carried as `data:image/svg+xml` in an <img>. They are line art, and an SVG stays sharp at any pinch zoom
# (Chrome redraws it at the zoomed scale); a PNG is sharp only up to the zoom it was drawn for. Against a PNG drawn for
# 4x zoom on a 375 px, DPR 3 phone (4116 px wide): node's front copper is 119 kB as SVG and 432 kB as PNG, its combined
# copper 60 kB against 231 kB; its schematic is 936 kB as SVG and 608 kB as PNG (ds2's 491 kB against 278 kB), and that
# PNG blurs past 4x where a sheet's 1.27 mm text is still small, so the SVG it is. KiCad writes text as strokes: the SVGs
# name no font and refer to nothing outside themselves (`static_page_problems` holds every page to that), and an SVG in
# an <img> runs no script and loads nothing by the browser's own rules. The 3D views are ray-traced photographs
# (`STILL_VIEWS`): JPEG.

STATIC_SECTIONS = (
    ("sch", "Schematic"),
    ("front", "Front copper"),
    ("back", "Back copper"),
    ("silk", "Silkscreen"),
    ("both", "Both layers"),
    ("three", "3D"),
    ("bom", "BOM"),
)
# The pictures each section of a complete static page carries.
STATIC_PICTURES = {"sch": 1, "front": 1, "back": 1, "silk": 1, "both": 1, "three": len(STILL_VIEWS)}
STATIC_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"


def _svg_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def render_static_html(
    *,
    title: str,
    facts: list[tuple[str, str, bool]],
    sch_svg: str | None,
    front_svg: str | None,
    back_svg: str | None,
    silk_svg: str | None,
    copper_svg: str | None,
    stills: list[tuple[str, str, str]],
    bom_rows: list[list[str]],
    notes: list[str],
) -> str:
    """The static review page (see above). `facts` are the header's (label, value, is_bad) lines; `stills` are
    (caption, mime, base64) 3D renders."""

    def pic(svg: str | None, alt: str, missing: str, cls: str = "") -> str:
        if not svg or _svg_refs(svg):
            why = missing if not svg else f"{missing} (its SVG refers to something outside itself: {', '.join(_svg_refs(svg)[:3])})"
            return f'<p class="empty">{html.escape(why)}</p>'
        return f'<figure class="pic{(" " + cls) if cls else ""}"><img src="{_svg_uri(svg)}" alt="{html.escape(alt)}"/></figure>'

    facts_html = "".join(
        ('<div class="bad">' if bad else "<div>") + f"<dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>"
        for k, v, bad in facts
    )
    toc = " · ".join(f'<a href="#{key}">{html.escape(name)}</a>' for key, name in STATIC_SECTIONS)
    shots = "".join(
        f'<figure class="pic shot"><img src="data:{mime};base64,{b64}" alt="{html.escape(caption)}: 3D render of the board"/>'
        f"<figcaption>{html.escape(caption)}</figcaption></figure>"
        for caption, mime, b64 in stills
    )
    three = (
        f'<p class="cap">kicad-cli pcb render, made when this page was written.</p><div class="shots">{shots}</div>'
        if shots
        else '<p class="empty">No 3D renders: kicad-cli pcb render made no picture (see report.json).</p>'
    )
    if bom_rows:
        head, *body = bom_rows
        th = "".join(f"<th>{html.escape(c)}</th>" for c in head)
        trs = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>" for row in body)
        bom = f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'
    else:
        bom = '<p class="empty">No BOM: the fab package is not fresh (pcbc build writes fab/bom.csv).</p>'
    notes_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="Content-Security-Policy" content="{STATIC_CSP}"/>
<title>{html.escape(title)} — pcbc review (static)</title>
<style>
  :root {{ --bg: #10140f; --panel: #181e16; --ink: #e7eee2; --muted: #8ea382; --line: #2a3626; --accent: #d4893a; --bad: #e8735a; }}
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.45 ui-sans-serif, system-ui, sans-serif;
    overflow-wrap: anywhere; }}
  header, section {{ padding: 16px; border-bottom: 1px solid var(--line); max-width: 1100px; margin: 0 auto; }}
  h1 {{ margin: 0; font-size: 21px; }}
  h2 {{ margin: 0 0 10px; font-size: 17px; color: var(--accent); }}
  .sub, .cap, .empty {{ color: var(--muted); margin: 4px 0 10px; }}
  dl {{ margin: 10px 0; display: grid; grid-template-columns: 1fr; gap: 4px; }}
  dl div {{ display: grid; grid-template-columns: 7.5em minmax(0, 1fr); gap: 8px; }}
  dt {{ color: var(--muted); }}
  dd {{ margin: 0; font-family: ui-monospace, Menlo, monospace; font-size: 13px; }}
  dl div.bad dd {{ color: var(--bad); }}
  .toc {{ font-size: 14px; }}
  .toc a {{ color: var(--accent); }}
  figure {{ margin: 0 0 12px; }}
  .pic img {{ display: block; width: 100%; height: auto; max-height: 88vh; object-fit: contain; background: #0b0e0c;
    border: 1px solid var(--line); border-radius: 8px; }}
  .pic.sch img {{ background: #f4f0e4; }}
  .shots {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); gap: 12px; }}
  figcaption {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  th, td {{ border-bottom: 1px solid var(--line); text-align: left; padding: 5px 6px; vertical-align: top;
    font-family: ui-monospace, Menlo, monospace; }}
  th {{ color: var(--accent); font-weight: 600; overflow-wrap: normal; }}
  ul {{ color: var(--muted); padding-left: 20px; }}
  @media (max-width: 480px) {{ dl div {{ grid-template-columns: 6.2em minmax(0, 1fr); gap: 6px; }} dd {{ font-size: 12px; }} }}
</style>
</head>
<body>
<header id="status">
  <h1>{html.escape(title)}</h1>
  <p class="sub">pcbc review, static: the pictures only, no script. index.html beside it is the full page.</p>
  <dl>{facts_html}</dl>
  <p class="toc">{toc}</p>
</header>
<section id="sch"><h2>Schematic</h2>{pic(sch_svg, "Schematic", "No schematic: kicad-cli sch export svg failed.", "sch")}</section>
<section id="front"><h2>Front copper</h2>{pic(front_svg, "Front copper and silkscreen", "No front copper picture: kicad-cli pcb export svg failed.")}</section>
<section id="back"><h2>Back copper</h2><p class="cap">Seen from below (mirrored).</p>{pic(back_svg, "Back copper and silkscreen, mirrored", "No back copper picture: kicad-cli pcb export svg failed.")}</section>
<section id="silk"><h2>Silkscreen</h2>{pic(silk_svg, "Front silkscreen", "No silkscreen picture: kicad-cli pcb export svg failed.")}</section>
<section id="both"><h2>Both layers</h2>{pic(copper_svg, "Front and back copper", "No combined copper picture: kicad-cli pcb export svg failed.")}</section>
<section id="three"><h2>3D</h2>{three}</section>
<section id="bom"><h2>BOM</h2>{bom}</section>
<section id="notes"><h2>Notes</h2><ul>{notes_html or "<li>No extra notes.</li>"}</ul></section>
</body>
</html>
"""


_SVG_EXTERNAL = re.compile(r"""(?:\b(?:href|src)\s*=\s*["'](?!#|data:)([^"']*)["'])|(?:url\(\s*["']?(?!#|data:)([^)"']*))|(@import)""", re.I)


def _svg_refs(svg: str) -> list[str]:
    """What an SVG refers to outside itself (a link, an external image, font or stylesheet): it must be nothing."""
    return [next(g for g in m.groups() if g) for m in _SVG_EXTERNAL.finditer(svg)]


def _decodes(mime: str, data: bytes) -> str | None:
    """Why the bytes of a `data:` picture are not a picture of `mime` (None when they are). A structural check (the
    image's signature, its end, an SVG that parses with an <svg> root and refers to nothing); the browser's decode is
    tools/review_static_check.mjs's."""
    if mime == "image/svg+xml":
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            return f"SVG does not parse ({exc})"
        if not root.tag.endswith("svg"):
            return f"root element is {root.tag}, not svg"
        if not (root.get("viewBox") or (root.get("width") and root.get("height"))):
            return "SVG has no size (no viewBox, width or height)"
        text = data.decode("utf-8", "replace")
        if re.search(r"<script|<foreignObject", text, re.I):
            return "SVG carries a script or foreign content"
        if _svg_refs(text):
            return "SVG refers outside itself: " + ", ".join(_svg_refs(text)[:3])
        return None
    if mime == "image/jpeg":
        return None if data[:3] == b"\xff\xd8\xff" and b"\xff\xd9" in data[-16:] and len(data) > 1000 else "not a whole JPEG"
    if mime == "image/png":
        return None if data[:8] == b"\x89PNG\r\n\x1a\n" and b"IEND" in data[-12:] else "not a whole PNG"
    return f"picture type {mime} is not one the page uses"


def static_page_problems(page: str, pictures: dict[str, int] | None = None) -> list[str]:
    """Everything that keeps `page` from being the static review page: a <script> (or an event handler, frame, object,
    stylesheet link or <base>), anything that names a URL other than a `data:` picture or an in-page `#anchor`, a picture
    whose bytes are not a whole picture, a missing section, and a section with fewer pictures than `pictures` asks
    (default STATIC_PICTURES). Empty when the page is whole."""
    from html.parser import HTMLParser

    pictures = STATIC_PICTURES if pictures is None else pictures
    problems: list[str] = []
    found: dict[str, int] = {}
    seen: list[str] = []

    class Reader(HTMLParser):
        section: str | None = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag in ("script", "iframe", "frame", "object", "embed", "base", "applet", "audio", "video", "source", "form"):
                problems.append(f"has a <{tag}>")
            if tag == "link" and (a.get("rel") or "").lower() != "icon":
                problems.append(f"has a <link rel={a.get('rel')!r}>")
            if tag == "meta" and (a.get("http-equiv") or "").lower() == "refresh":
                problems.append("has a meta refresh")
            for k, v in attrs:
                if k.startswith("on"):
                    problems.append(f"<{tag}> has an event handler ({k})")
                if k in ("src", "href", "srcset", "poster", "action", "data") and v is not None:
                    if not (v.startswith("#") or (k in ("src", "href") and v.startswith("data:"))):
                        problems.append(f"<{tag} {k}> names {v[:80]!r}")
            if tag == "section":
                self.section = a.get("id")
                seen.append(self.section or "")
            if tag == "img":
                m = re.fullmatch(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=]+)", a.get("src") or "")
                if not m:
                    problems.append(f"an <img> in section {self.section} is not a base64 data: picture")
                    return
                try:
                    why = _decodes(m.group(1), base64.b64decode(m.group(2), validate=True))
                except ValueError as exc:
                    why = f"base64 does not decode ({exc})"
                if why:
                    problems.append(f"a picture in section {self.section}: {why}")
                else:
                    found[self.section or ""] = found.get(self.section or "", 0) + 1

        def handle_endtag(self, tag):
            if tag == "section":
                self.section = None

    Reader().feed(page)
    if f'http-equiv="Content-Security-Policy" content="{STATIC_CSP}"' not in page:
        problems.append("does not carry its Content-Security-Policy")
    for key, _ in STATIC_SECTIONS:
        if key not in seen:
            problems.append(f"no section {key}")
    for key, want in pictures.items():
        if found.get(key, 0) < want:
            problems.append(f"section {key} shows {found.get(key, 0)} of {want} pictures")
    return problems


def _static_facts(design, job, pcb: Path, layout: Path, stage: str | None, stale: str | None, gate: dict | None,
                  result: dict, bom_rows: list[list[str]]) -> list[tuple[str, str, bool]]:
    """The static page's status header: (label, value, is_bad)."""
    from .netcheck import expected_nets

    w, h, source = _outline_mm(pcb, job)
    facts: list[tuple[str, str, bool]] = [
        ("Size", f"{w:g} × {h:g} mm ({source} outline)", False),
        ("Layers", f"{job.layers} · stackup {job.stackup}", False),
    ]
    if stale and "unfinished" in stale:
        facts.append(("Build", "the route stopped unfinished; these pictures are the board it emitted", True))
    elif stale:
        facts.append(("Build", f"freshest stage: {stage}; later outputs are stale ({stale})", True))
    else:
        facts.append(("Build", f"fresh through {stage}" + (" (fab package written)" if stage == "fab" else ""), False))
    need = sorted(n for n, pads in expected_nets(design).items() if len(pads) >= 2)
    if gate is None:
        reason = "not judged: outputs are stale" if stale and "unfinished" not in stale else "not routed yet"
        facts.append(("Nets", f"{len(need)} to connect; {reason}", bool(stale)))
        facts.append(("KiCad DRC", "not run on this board", False))
    elif "error" in gate:
        facts.append(("Nets", f"{len(need)} to connect; unchecked ({gate['error']})", True))
        facts.append(("KiCad DRC", f"unchecked ({gate['error']})", True))
    else:
        open_nets = sorted(set(gate.get("unconnected_nets") or []))
        routed = len([n for n in need if n not in open_nets])
        facts.append((
            "Nets",
            f"{routed} of {len(need)} routed · {len(open_nets)} unrouted" + (f": {', '.join(open_nets[:12])}" if open_nets else ""),
            bool(open_nets),
        ))
        n_err = len(gate.get("drc_errors") or [])
        facts.append((
            "KiCad DRC",
            f"{n_err} error(s) · {gate.get('drc_warnings', 0)} warning(s) · {gate.get('unconnected', 0)} unconnected item(s)",
            bool(n_err or gate.get("unconnected")),
        ))
        if gate.get("nets"):
            facts.append(("Pad binding", "; ".join(gate["nets"][:3]), True))
    if stage == "fab":
        try:
            silk = json.loads((layout / "fab" / "report.json").read_text()).get("silk") or {}
        except (OSError, ValueError):
            silk = {}
        marked, failed = silk.get("orientation") or [], silk.get("orientation_fails") or []
        if marked or failed:
            refs = ", ".join(str(m).split(":", 1)[0] for m in marked)
            facts.append((
                "Orientation marks",
                f"{len(marked)} marked" + (f" ({refs})" if refs else "") + f" · {len(failed)} failed"
                + (": " + "; ".join(str(f) for f in failed[:3]) if failed else ""),
                bool(failed),
            ))
    netlist = result.get("netlist")
    facts.append((
        "Netlist",
        "kicad-cli reads the same netlist as board.py" if netlist == "verified"
        else "MISMATCH: " + "; ".join(netlist[:3]) if isinstance(netlist, list) else str(netlist),
        netlist != "verified",
    ))
    facts.append(("Parts", f"{len(design.instances)} parts · {len(design.nets)} nets"
                  + (f" · BOM {max(len(bom_rows) - 1, 0)} lines" if bom_rows else ""), False))
    facts.append(("Board file", str(pcb), False))
    return facts


def review_job(board: Path, *, open_html: bool = True) -> dict:
    board = Path(board).resolve()
    design = load_board(board)
    job = compile_design(design)
    layout = layout_dir(board)
    out_dir = layout / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Review reads a stage's files only when that stage's stamp matches (C1): the freshest stage by
    # `stale_reason`, and why the later ones are not, printed in place of their verdicts.
    from .build import stale_reason

    stage, stale = stale_reason(layout, board, design)
    pcb = _best_pcb(layout, stage)
    # A route that stops with a net unfinished emits the board and deliberately
    # writes no success stamp, so the next build does not skip it. The picture
    # is still that emit. A hand-edited or source-stale board does not get here:
    # those come back as a different stage than "place" with this reason.
    if pcb is None and stage == "place" and stale and "no record of what it was routed from" in stale:
        routed = layout / "routed" / "layout.kicad_pcb"
        if routed.is_file():
            pcb = routed
            stale = "the route stopped unfinished and wrote no stamp"
    steps: list[dict] = []
    result: dict = {
        "review": str(out_dir),
        "html": str(out_dir / "index.html"),  # the full page (tabs, the interactive 3D view)
        "static_html": str(out_dir / "static.html"),  # the same review as pictures, for a viewer that runs no script
        "pcb": str(pcb) if pcb else None,
        "stage": stage,
        "stale": stale,
        "steps": steps,
        "error": None,
    }
    if pcb is None:
        result["error"] = "no board the build stamped as fresh" + (f" ({stale})" if stale else "") + " — run pcbc build first"
        return result

    # Review's own drawing, in its own directory: never over the build's `schematic.kicad_sch`, which
    # is the sch stage's output and stamped (fifth review: review rewrote it and the next build blamed
    # pcbc's own write as a hand edit).
    sch_path = out_dir / "schematic.kicad_sch"
    sch_report: dict = {}
    emit_schematic_file(design, sch_path, title=board.stem, report=sch_report)
    result["schematic"] = str(sch_path)
    result["readability"] = sch_report.get("issues", [])
    try:
        netlist_fails = check_schematic(design, sch_path)
        result["netlist"] = "verified" if not netlist_fails else netlist_fails
    except KicadMissing as exc:
        netlist_fails = []
        result["netlist"] = f"unchecked: {exc}"

    bom_path = layout / "fab" / "bom.csv"
    bom_rows: list[list[str]] = []
    if stage == "fab" and bom_path.exists():
        with bom_path.open(newline="") as f:
            bom_rows = [row for row in csv.reader(f) if row]

    cli = kicad_cli()
    front = out_dir / "front.svg"
    back = out_dir / "back.svg"
    copper = out_dir / "copper.svg"
    silk_svg_path = out_dir / "silk.svg"
    glb = out_dir / "board.glb"
    sch_svg_text = None
    stills: list[tuple[str, str, str]] = []

    if not cli.exists() and shutil.which(str(cli)) is None:
        result["error"] = f"kicad-cli not found ({cli})"
    else:
        # The board as the build left it: its silk references were placed by the place stage and are
        # what ships (fab writes none), so review plots them as they are.
        plot_pcb = pcb
        # The 3D still renders (the static page's) take longest: they run while the rest is exported.
        from concurrent.futures import ThreadPoolExecutor

        still_pool = ThreadPoolExecutor(max_workers=1)
        still_job = still_pool.submit(_render_stills, cli, pcb, out_dir)
        steps.append(_export_svg(cli, plot_pcb, front, "F.Cu,F.SilkS,Edge.Cuts"))
        steps.append(_export_svg(cli, plot_pcb, silk_svg_path, "F.SilkS,Edge.Cuts"))
        steps.append(_export_svg(cli, plot_pcb, back, "B.Cu,B.SilkS,Edge.Cuts", mirror=True))
        steps.append(_export_svg(cli, plot_pcb, copper, "F.Cu,B.Cu,Edge.Cuts"))
        steps.append(_export_glb(cli, pcb, glb))
        sch_dir = out_dir / "sch"
        sch_dir.mkdir(exist_ok=True)
        for old in sch_dir.glob("*.svg"):
            old.unlink()
        steps.append(_export_sch_svg(cli, sch_path, sch_dir))
        svgs = sorted(sch_dir.glob("*.svg"))
        if svgs:
            raw = svgs[0].read_text(errors="replace")
            sch_svg_text = crop_svg_to_content(raw)
            if sch_svg_text != raw:
                svgs[0].write_text(sch_svg_text)
        still_steps, stills = still_job.result()
        still_pool.shutdown()
        steps.extend(still_steps)

    def _svg(p: Path) -> str | None:
        return p.read_text(errors="replace") if p.exists() and p.stat().st_size > 80 else None

    glb_b64 = None
    if glb.exists() and glb.stat().st_size > 100:
        glb_b64 = base64.b64encode(glb.read_bytes()).decode("ascii")
        result["glb_bytes"] = glb.stat().st_size

    gate = _copper_gate(design, pcb, stage, stale)
    notes = [
        f"{len(design.instances)} parts, {len(design.nets)} nets",
        "Schematic is pcbc emit from board.py (no default.net).",
        (
            "kicad-cli reads the same netlist as board.py."
            if result.get("netlist") == "verified"
            else f"NETLIST MISMATCH: {'; '.join(netlist_fails)}"
            if netlist_fails
            else f"Netlist {result.get('netlist')}."
        ),
        (
            "Schematic readability: nothing overlaps."
            if not result["readability"]
            else f"Schematic readability, {len(result['readability'])} warning(s): "
            + "; ".join(result["readability"])
        ),
        _copper_note(design, pcb, stage, stale, gate),
        *([f"BOM: not shown — the fab package is not fresh ({stale or 'the build stopped before fab'})."] if stage != "fab" else []),
        "3D is kicad-cli pcb export glb (tracks, pads, silk, mask).",
        (
            f"3D still renders (static.html): {len(stills)} of {len(STILL_VIEWS)} from kicad-cli pcb render."
            if len(stills) == len(STILL_VIEWS)
            else f"3D still renders (static.html): {len(stills)} of {len(STILL_VIEWS)} — kicad-cli pcb render made no picture for the rest (see report.json)."
        ),
    ]
    if any(s.get("returncode") not in (0, None) for s in steps):
        notes.append("One or more kicad-cli export steps returned non-zero — see report.json.")

    html_text = render_html(
        title=board.stem,
        board_mm=_outline_mm(pcb, job),
        layers=job.layers,
        stackup=job.stackup,
        pcb_name=str(pcb),
        source_text=board.read_text(),
        sch_svg=sch_svg_text,
        front_svg=_svg(front),
        back_svg=_svg(back),
        copper_svg=_svg(copper),
        silk_svg=_svg(silk_svg_path),
        glb_b64=glb_b64,
        bom_rows=bom_rows,
        notes=notes,
    )
    html_path = out_dir / "index.html"
    html_path.write_text(html_text)
    static_text = render_static_html(
        title=board.stem,
        facts=_static_facts(design, job, pcb, layout, stage, stale, gate, result, bom_rows),
        sch_svg=sch_svg_text,
        front_svg=_svg(front),
        back_svg=_svg(back),
        silk_svg=_svg(silk_svg_path),
        copper_svg=_svg(copper),
        stills=stills,
        bom_rows=bom_rows,
        notes=notes,
    )
    static_path = out_dir / "static.html"
    static_path.write_text(static_text, encoding="utf-8")
    result["stills"] = len(stills)
    result["static_bytes"] = static_path.stat().st_size
    # What the static page is missing (a picture kicad-cli did not make, say), checked on the page as written.
    result["static_problems"] = static_page_problems(static_text)
    (out_dir / "report.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    if open_html:
        webbrowser.open(html_path.as_uri())
        result["opened"] = True
    return result
