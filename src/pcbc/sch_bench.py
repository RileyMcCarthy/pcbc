"""Saved pictures of the example schematics.

A pull request that changes how a sheet is drawn updates the picture in
``tests/benchmarks/schematics``. Vibes reads ``vibes.images.json`` and posts
current, new, and the difference; the first time a picture is saved, current
and the difference are empty. The check itself is the drawing (the SVG with
the export date removed), so a different font renderer does not fail a run
whose sheet did not move.
"""

from __future__ import annotations

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


def render_sheet(board: Path, name: str) -> str:
    """Draw ``board`` and return the cropped schematic SVG."""
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        sch = dest / f"{name}.kicad_sch"
        emit_schematic_file(load_board(board), sch, title=name)
        subprocess.run(
            [str(kicad_cli()), "sch", "export", "svg", "--exclude-drawing-sheet", "--no-background-color", "-o", str(dest), str(sch)],
            check=True,
            capture_output=True,
            text=True,
        )
        produced = next(dest.glob("*.svg"))
        return crop_svg_to_content(normalize_svg(produced.read_text()), pad_mm=6.0, px_per_mm=8.0)


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


def check_board(name: str, board: Path, *, update: bool) -> str | None:
    """Return an error string when the saved picture is not what the tool draws."""
    svg = render_sheet(board, name)
    golden_svg = GOLDEN / f"{name}.svg"
    golden_png = GOLDEN / f"{name}.png"
    folder = OUT / name
    if update:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        golden_svg.write_text(svg)
        rasterize(svg, golden_png)
        return None
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
        f"{name}: the schematic picture changed. "
        f"Open {OUT / 'index.html'} for the saved picture, the new one, and the difference. "
        f"If the new picture is right, run PCBC_UPDATE_SCHEMATICS=1 pytest tests/test_schematic_bench.py "
        f"and commit tests/benchmarks/schematics/{name}.svg and {name}.png."
    )


def main() -> None:
    update = os.environ.get("PCBC_UPDATE_SCHEMATICS") == "1"
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    errors: list[str] = []
    rows: list[tuple[str, bool]] = []
    for name, board in BOARDS:
        err = check_board(name, board, update=update)
        rows.append((name, err is not None))
        if err:
            errors.append(err)
    if not update:
        _page(rows)
    if errors:
        raise SystemExit("\n".join(errors))


if __name__ == "__main__":
    main()
