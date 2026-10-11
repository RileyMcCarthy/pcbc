"""Saved pictures of the example schematics, two per board (docs/schematic-layout.md §7).

``<board>.png`` is the sheet with the board's ``schematic.core.py`` (the cleaned-up sheet); ``<board>.auto.png`` is the
bare auto-layout, drawn with no core (``load_board(path, schematic_core=False)``), so a lock can never hide a regression
in the auto-layout. A pull request that changes how a sheet is drawn updates its picture in
``tests/benchmarks/schematics``. Vibes reads ``vibes.images.json`` and posts current, new, and the difference; the first
time a picture is saved, current and the difference are empty. The check is the drawing (the SVG with the export date
removed), so a different font renderer does not fail a run whose sheet did not move.

Each picture has its readability vector beside it (``<board>.score.json``, ``<board>.auto.score.json``; text boxes from
KiCad's own SVG, ``sch_score.kicad_vector``). A hard number above 0 fails, and so does a soft number worse than the
saved one, unless the run saves the new ones (``PCBC_UPDATE_SCHEMATICS=1``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .language import load_board
from .netcheck import kicad_cli
from .review import crop_svg_to_content
from .sch_emit import emit_schematic_file

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "benchmarks" / "schematics"
OUT = GOLDEN / "out"

BOARDS: list[tuple[str, Path]] = [
    ("blinky", ROOT / "examples" / "blinky" / "blinky.py"),
    ("buck", ROOT / "examples" / "buck" / "buck.py"),
    ("c3_usb", ROOT / "examples" / "c3_usb" / "c3_usb.py"),
    ("node", ROOT / "examples" / "node" / "node.py"),
]

_TITLE = re.compile(r"<title>.*?</title>", re.S)


def normalize_svg(svg: str) -> str:
    """The drawing, without the export clock KiCad writes into the title."""
    svg = _TITLE.sub("<title>schematic</title>", svg, count=1)
    return svg.replace("\r\n", "\n")


MODES = (("", True), (".auto", False))  # (file suffix, with the board's schematic.core.py)
# The soft numbers a picture may not get worse on, and which way is worse (§7 item 3).
RATCHET = {
    "lint_issues": 1, "labels": 1, "label_partner_mm": 1, "wrong_way_up": 1, "upside_down_power": 1, "text_crowding": 1,
    "ambiguous_names": 1, "tight_pairs": 1, "extent_area": 1,
}


def render_sheet(board: Path, name: str, *, schematic_core: bool = True) -> tuple[str, dict]:
    """Draw ``board`` (with its schematic.core.py, or none) and return the cropped SVG and the readability vector."""
    from .sch_score import kicad_vector

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        sch = dest / f"{name}.kicad_sch"
        report: dict = {}
        emit_schematic_file(load_board(board, core=False, schematic_core=schematic_core), sch, title=name, report=report)
        subprocess.run(
            [str(kicad_cli()), "sch", "export", "svg", "--exclude-drawing-sheet", "--no-background-color", "-o", str(dest), str(sch)],
            check=True,
            capture_output=True,
            text=True,
        )
        raw = next(dest.glob("*.svg")).read_text()
        vec = kicad_vector(sch, report, svg=raw)
        w, h = vec.get("extent_mm") or (0.0, 0.0)
        vec = {k: v for k, v in vec.items() if not k.startswith("_") and k not in ("svg_texts_matched", "svg_texts_unmatched")}
        vec["extent_area"] = round(w * h, 1)
        return crop_svg_to_content(normalize_svg(raw), pad_mm=6.0, px_per_mm=8.0), vec


def rasterize(svg: str, dest: Path, width: int = 1400) -> None:
    tool = shutil.which("rsvg-convert")
    if tool is None:
        raise RuntimeError("rsvg-convert is not installed (librsvg); the picture cannot be drawn")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([tool, "-w", str(width), "-o", str(dest)], input=svg, text=True, check=True, capture_output=True)


# The same reading the Vibes picture report uses: a channel more than this
# far apart has moved, agreement is grey, movement is red.
_MOVED_ABOVE = 16
_AGREE = (236, 236, 236)
_MOVED = (210, 32, 32)


def write_diff(saved: Path, new: Path, dest: Path) -> None:
    """Grey where the two pictures agree, red where a pixel changed."""
    from PIL import Image, ImageChops

    old = Image.open(saved).convert("RGB")
    nxt = Image.open(new).convert("RGB")
    width = max(old.width, nxt.width)
    height = max(old.height, nxt.height)
    base_old = Image.new("RGB", (width, height), (255, 255, 255))
    base_new = Image.new("RGB", (width, height), (255, 255, 255))
    base_old.paste(old, (0, 0))
    base_new.paste(nxt, (0, 0))
    mask = ImageChops.difference(base_old, base_new).convert("L").point(lambda p: 255 if p > _MOVED_ABOVE else 0)
    out = Image.new("RGB", (width, height), _AGREE)
    out.paste(Image.new("RGB", (width, height), _MOVED), mask=mask)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest)


def _page(rows: list[tuple[str, bool]]) -> None:
    parts = [
        "<!DOCTYPE html><meta charset=utf-8><title>Schematic pictures</title>",
        "<style>body{font:16px sans-serif;background:#111;color:#eee;margin:24px} img{max-width:100%;background:#fff} figure{margin:0} .row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin:24px 0}</style>",
        "<h1>Schematic pictures</h1>",
        "<p>Current, the sheet this tool draws now, and the pixels that moved. An empty column is a picture this run does not have.</p>",
    ]
    for name, changed in rows:
        folder = OUT / name
        parts.append(f"<h2>{name}</h2>")
        if not changed:
            parts.append("<p>Unchanged.</p>")
            continue
        parts.append('<div class="row">')
        for label, filename in (("Current", "current.png"), ("New", "new.png"), ("Difference", "diff.png")):
            path = folder / filename
            if path.exists():
                parts.append(f"<figure><figcaption>{label}</figcaption><img src='{name}/{filename}'></figure>")
            else:
                parts.append(f"<figure><figcaption>{label}</figcaption></figure>")
        parts.append("</div>")
    (OUT / "index.html").write_text("\n".join(parts))


def score_errors(name: str, vec: dict, saved: dict | None) -> list[str]:
    """A hard number above 0, or a soft number worse than the saved picture's, as facts."""
    out = [f"{name}: {k} {m}" for k, ms in (vec.get("detail") or {}).items() for m in ms]
    if saved:
        for k in RATCHET:
            if k in vec and k in saved and vec[k] is not None and saved[k] is not None and vec[k] > saved[k] + 0.05:
                out.append(f"{name}: {k} is {vec[k]}, worse than the saved {saved[k]}")
    return out


def check_board(name: str, board: Path, *, update: bool, schematic_core: bool = True) -> str | None:
    """Return an error string when the saved picture is not what the tool draws, or its scores fail."""
    suffix = "" if schematic_core else ".auto"
    svg, vec = render_sheet(board, name, schematic_core=schematic_core)
    stem = f"{name}{suffix}"
    golden_svg = GOLDEN / f"{stem}.svg"
    golden_png = GOLDEN / f"{stem}.png"
    golden_score = GOLDEN / f"{stem}.score.json"
    folder = OUT / stem
    saved = json.loads(golden_score.read_text()) if golden_score.exists() else None
    errors = score_errors(stem, vec, None if update else saved)
    if update:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        golden_svg.write_text(svg)
        rasterize(svg, golden_png)
        golden_score.write_text(json.dumps({k: v for k, v in vec.items() if k != "detail"}, indent=1, sort_keys=True) + "\n")
        return "; ".join(errors) if errors else None
    if errors:
        return "; ".join(errors)
    if not golden_svg.exists():
        return f"{name}: no saved schematic picture. Run with PCBC_UPDATE_SCHEMATICS=1."
    if golden_svg.read_text() == svg:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "new.svg").write_text(svg)
    rasterize(svg, folder / "new.png")
    if golden_png.exists():
        shutil.copyfile(golden_png, folder / "current.png")
        write_diff(golden_png, folder / "new.png", folder / "diff.png")
    return (
        f"{stem}: the schematic picture changed. "
        f"Open {OUT / 'index.html'} for the saved picture, the new one, and the difference. "
        f"If the new picture is right, run PCBC_UPDATE_SCHEMATICS=1 pytest tests/test_schematic_bench.py "
        f"and commit tests/benchmarks/schematics/{stem}.svg, {stem}.png and {stem}.score.json."
    )


def main() -> None:
    update = os.environ.get("PCBC_UPDATE_SCHEMATICS") == "1"
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    errors: list[str] = []
    rows: list[tuple[str, bool]] = []
    for name, board in BOARDS:
        for suffix, core in MODES:
            err = check_board(name, board, update=update, schematic_core=core)
            rows.append((name + suffix, err is not None))
            if err:
                errors.append(err)
    if not update:
        _page(rows)
    if errors:
        raise SystemExit("\n".join(errors))


if __name__ == "__main__":
    main()
