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


def _copper_note(design, pcb: Path, stage: str | None = None, stale: str | None = None) -> str:
    if stale and "unfinished" in stale:
        return "Copper: the route stopped unfinished and wrote no stamp. This picture is the board it emitted; it is not a finished build."
    if stale:
        return f"Copper: not judged — the build's outputs are stale ({stale}); run pcbc build."
    if stage not in ("route", "fab"):
        return "Copper: not routed yet (pcbc build routes it and judges it)."
    text = pcb.read_text()
    if "\n\t(segment" not in text and "\n\t(zone" not in text:
        return "Copper: not routed yet (pcbc build routes it and judges it)."
    try:
        from .netcheck import KicadMissing, check_copper

        gate = check_copper(design, pcb, refill=False)
    except KicadMissing as exc:
        return f"Copper: unchecked ({exc})."
    n_seg = text.count("\n\t(segment")
    n_via = text.count("\n\t(via")
    if gate["ok"]:
        return f"Copper: {n_seg} tracks, {n_via} vias; KiCad DRC clean, nothing unconnected, pads bound as board.py says."
    return f"COPPER FAILS the gate ({len(gate['fails'])}): " + "; ".join(gate["fails"][:5])


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
    def panel_svg(svg: str | None, empty: str) -> str:
        if svg:
            return f'<div class="plot">{svg}</div>'
        return f'<p class="empty">{html.escape(empty)}</p>'

    src_block = (
        f'<pre class="src">{html.escape(source_text)}</pre>'
        if source_text
        else '<p class="empty">No board.py</p>'
    )
    sch_block = (
        '<div class="plot sch" id="sch-view">'
        '<div class="sch-tools">'
        '<button type="button" id="sch-out" title="Zoom out">−</button>'
        '<button type="button" id="sch-fit" title="Show the whole sheet">Fit</button>'
        '<button type="button" id="sch-in" title="Zoom in">+</button>'
        "</div>"
        f'<div class="sch-stage" id="sch-stage">{sch_svg}</div>'
        '<p class="sch-hint">Scroll to zoom. Drag to move.</p>'
        "</div>"
        if sch_svg
        else '<p class="hint">No schematic SVG. kicad-cli sch export svg failed.</p>'
    )
    glb_block = (
        f"""<model-viewer src="data:model/gltf-binary;base64,{glb_b64}"
          camera-controls touch-action="pan-y" shadow-intensity="1"
          environment-image="neutral" exposure="0.9"
          style="width:100%;height:min(72vh,720px);background:#0b0e0c">
        </model-viewer>
        <p class="hint">Drag to orbit. Chip footprints here have no STEP — pads and copper still show.</p>"""
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
        bom_html = f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    else:
        bom_html = '<p class="empty">No fab/bom.csv yet. Run pcbc build first.</p>'
    notes_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes) or "<li>No extra notes.</li>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)} — pcbc review</title>
<script type="module" src="https://ajax.googleapis.com/ajax/libs/model-viewer/4.0.0/model-viewer.min.js"></script>
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
  header {{
    padding: 18px 22px 10px;
    border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  }}
  header h1 {{ margin: 0; font-size: 20px; letter-spacing: .02em; }}
  header p {{ margin: 4px 0 0; color: var(--muted); }}
  .meta {{ color: var(--muted); font-family: ui-monospace, Menlo, monospace; font-size: 12px; }}
  nav {{
    display: flex; gap: 6px; padding: 10px 22px; position: sticky; top: 0;
    background: #10140fee; border-bottom: 1px solid var(--line); z-index: 2; flex-wrap: wrap;
  }}
  nav button {{
    background: transparent; color: var(--muted); border: 1px solid var(--line);
    border-radius: 999px; padding: 6px 14px; cursor: pointer; font: inherit;
  }}
  nav button[aria-selected="true"] {{
    color: var(--bg); background: var(--accent); border-color: var(--accent);
  }}
  section {{ display: none; padding: 18px 22px 40px; }}
  section.active {{ display: block; }}
  .plot {{
    background: #0b0e0c; border: 1px solid var(--line); border-radius: 10px;
    overflow: auto; max-height: 78vh; padding: 8px;
  }}
  .plot svg {{ display: block; width: 100%; height: auto; }}
  .plot.sch {{
    background: #f4f0e4; height: 78vh; overflow: auto; padding: 0;
    position: relative; cursor: grab; touch-action: none;
  }}
  .plot.sch.grabbing {{ cursor: grabbing; }}
  .sch-stage {{
    min-width: 100%; min-height: 100%;
    display: flex; align-items: center; justify-content: center;
  }}
  .plot.sch svg {{ width: 100%; height: 100%; max-width: none; flex: none; }}
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
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border-bottom: 1px solid var(--line); text-align: left; padding: 6px 8px;
    font-family: ui-monospace, Menlo, monospace; }}
  th {{ color: var(--accent); font-weight: 600; }}
  ul {{ color: var(--muted); }}
</style>
</head>
<body>
<header>
  <div>
    <h1>{html.escape(title)}</h1>
    <p>Schematic · layout · 3D — KiCad exports, one page.</p>
  </div>
  <div class="meta">
    {board_mm[0]:g}×{board_mm[1]:g} mm · {layers}L · {html.escape(stackup)}<br/>
    {html.escape(pcb_name)}
  </div>
</header>
<nav>
  <button data-tab="sch" aria-selected="true">Schematic</button>
  <button data-tab="front">Front copper</button>
  <button data-tab="silk">Silkscreen</button>
  <button data-tab="back">Back copper</button>
  <button data-tab="both">Both layers</button>
  <button data-tab="three">3D</button>
  <button data-tab="bom">BOM</button>
  <button data-tab="src">board.py</button>
</nav>
<section id="sch" class="active">{sch_block}</section>
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
<script>
  const tabs = document.querySelectorAll("nav button");
  const sections = document.querySelectorAll("section");
  tabs.forEach(btn => btn.addEventListener("click", () => {{
    tabs.forEach(b => b.setAttribute("aria-selected", b === btn));
    sections.forEach(s => s.classList.toggle("active", s.id === btn.dataset.tab));
  }}));
  const schView = document.getElementById("sch-view");
  const schStage = document.getElementById("sch-stage");
  const schSvg = schView && schView.querySelector("svg");
  if (schView && schStage && schSvg) {{
    let scale = 1;
    const vb = schSvg.viewBox.baseVal;
    const aspect = vb.width / vb.height || 1;
    function fitted() {{
      const bw = schView.clientWidth || 1;
      const bh = schView.clientHeight || 1;
      let w = bw, h = w / aspect;
      if (h > bh) {{ h = bh; w = h * aspect; }}
      return {{ w, h }};
    }}
    function layout() {{
      const f = fitted();
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
    window.addEventListener("resize", () => {{ if (scale === 1) layout(); }});
    schView.addEventListener("wheel", (e) => {{
      e.preventDefault();
      zoomAt(scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12), e.clientX, e.clientY);
    }}, {{ passive: false }});
    let drag = null;
    schView.addEventListener("pointerdown", (e) => {{
      if (e.target.closest(".sch-tools")) return;
      drag = {{ x: e.clientX, y: e.clientY, sl: schView.scrollLeft, st: schView.scrollTop }};
      schView.setPointerCapture(e.pointerId);
      schView.classList.add("grabbing");
    }});
    schView.addEventListener("pointermove", (e) => {{
      if (!drag) return;
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
        "html": str(out_dir / "index.html"),
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

    if not cli.exists() and shutil.which(str(cli)) is None:
        result["error"] = f"kicad-cli not found ({cli})"
    else:
        # The board as the build left it: its silk references were placed by the place stage and are
        # what ships (fab writes none), so review plots them as they are.
        plot_pcb = pcb
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

    def _svg(p: Path) -> str | None:
        return p.read_text(errors="replace") if p.exists() and p.stat().st_size > 80 else None

    glb_b64 = None
    if glb.exists() and glb.stat().st_size > 100:
        glb_b64 = base64.b64encode(glb.read_bytes()).decode("ascii")
        result["glb_bytes"] = glb.stat().st_size

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
            else f"Schematic readability, {len(result['readability'])} to fix by moving parts: "
            + "; ".join(result["readability"])
        ),
        _copper_note(design, pcb, stage, stale),
        *([f"BOM: not shown — the fab package is not fresh ({stale or 'the build stopped before fab'})."] if stage != "fab" else []),
        "3D is kicad-cli pcb export glb (tracks, pads, silk, mask).",
    ]
    if any(s.get("returncode") not in (0, None) for s in steps):
        notes.append("One or more kicad-cli export steps returned non-zero — see report.json.")

    html_text = render_html(
        title=board.stem,
        board_mm=job.board_size_mm,
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
    (out_dir / "report.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    if open_html:
        webbrowser.open(html_path.as_uri())
        result["opened"] = True
    return result
