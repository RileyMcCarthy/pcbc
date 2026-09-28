"""Python layout primitives emit KiCad objects. layout.gen.py is the routed board, decompiled."""

import base64
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from pcbc.build import pcb_job
from pcbc.language import load_board, load_layout
from pcbc.layout_emit import render, render_graphics, splice
from pcbc.layout_job import layout_job
from pcbc.layout import footprints_by_ref
from pcbc.seed import emit_pcb
from pcbc.sexp import footprint_at, stable_uuid

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


def _core(tmp_path: Path, body: str):
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    design = load_board(board)
    (tmp_path / "layout.core.py").write_text(body)
    load_layout(tmp_path / "layout.core.py", design, source="core")
    return design, render(design, design.copper, board="blinky")


def test_seg_via_arc_pour_are_the_kicad_objects(tmp_path: Path):
    design, text = _core(
        tmp_path,
        "\n".join(
            [
                'Seg("LED", (1, 2), (3, 2), layer="F.Cu", width=0.25, id="led")',
                'Via("GND", (4, 5), layers=("F.Cu", "B.Cu"), size=0.6, drill=0.3, id="gnd")',
                'Via("GND", (6, 5), layers=("F.Cu", "In1.Cu"), size=0.45, drill=0.2, kind="blind", id="blind")',
                'Arc("LED", (0, 0), (1, 1), (2, 0), layer="F.Cu", width=0.2, id="bend")',
                'Pour("GND", layer="B.Cu", points=[(0.5, 0.5), (10, 0.5), (10, 8), (0.5, 8)], id="gnd-plane")',
                "",
            ]
        ),
    )
    assert design.copper[0].kind == "seg"
    led = stable_uuid("blinky", "layout", "led")
    gnd = stable_uuid("blinky", "layout", "gnd")
    bend = stable_uuid("blinky", "layout", "bend")
    plane = stable_uuid("blinky", "layout", "gnd-plane")
    assert (
        "\t(segment\n"
        "\t\t(start 1 2)\n"
        "\t\t(end 3 2)\n"
        "\t\t(width 0.25)\n"
        '\t\t(layer "F.Cu")\n'
        '\t\t(net "LED")\n'
        f'\t\t(uuid "{led}")\n'
        "\t)\n"
    ) in text
    assert (
        "\t(via\n"
        "\t\t(at 4 5)\n"
        "\t\t(size 0.6)\n"
        "\t\t(drill 0.3)\n"
        '\t\t(layers "F.Cu" "B.Cu")\n'
        '\t\t(net "GND")\n'
        f'\t\t(uuid "{gnd}")\n'
        "\t)\n"
    ) in text
    assert "\t(via blind\n" in text and "(at 6 5)" in text
    assert (
        "\t(arc\n"
        "\t\t(start 0 0)\n"
        "\t\t(mid 1 1)\n"
        "\t\t(end 2 0)\n"
        "\t\t(width 0.2)\n"
        '\t\t(layer "F.Cu")\n'
        '\t\t(net "LED")\n'
        f'\t\t(uuid "{bend}")\n'
        "\t)\n"
    ) in text
    # KiCad 10 writes a zone's net by name, `(net "GND")`, and writes no `(net_name ...)` at all: the
    # census of the five routed boards finds 23 zones and not one `net_name` (docs/layout-properties.md).
    assert "(zone" in text and '(net "GND")' in text and "net_name" not in text and f'(uuid "{plane}")' in text
    assert "(xy 0.5 0.5)" in text and "(xy 10 8)" in text


def test_css_place_is_the_kicad_footprint_at(tmp_path: Path):
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    result = pcb_job(board)
    assert result.get("error") is None, result
    text = Path(result["placed"]).read_text()
    blocks = footprints_by_ref(text)
    for ref, pose in result["poses"].items():
        at = footprint_at(blocks[ref])
        assert at is not None, ref
        assert (at[0], at[1], at[2]) == (pose["at"][0], pose["at"][1], pose["rot"]), ref


def test_board_graphics_are_kicad_objects(tmp_path: Path):
    design, _copper = _core(
        tmp_path,
        "\n".join(
            [
                'Line((0, 0), (4, 0), layer="Edge.Cuts", width=0.05, id="slot")',
                'Rect((1, 1), (3, 2), layer="F.SilkS", radius=0.4, id="box")',
                'Circle((5, 5), 1, layer="F.SilkS", fill="hatch", id="dot")',
                'DrawArc((0, 0), (1, 1), (2, 0), layer="Edge.Cuts", id="bite")',
                'Poly([(0, 0), (1, 0), (1, 1)], layer="F.Fab", id="mark")',
                'Curve([(0, 0), (1, 2), (2, 2), (3, 0)], id="bend")',
                'Text("REV A", (8, 1), layer="F.SilkS", size=1.5, id="rev")',
                'TextBox("note", (0, 3), (6, 5), id="note")',
                'Dimension((0, 0), (10, 0), id="width")',
                'Group("marks", ["slot", "rev"], id="g")',
                'Image((2, 2), "iVBORw0KGgoAAAANSUhEUgAAAAwAAAADCAIAAAAoQXllAAAAUklEQVR4nA3KMQEAIRADwYhABCKuTk29NSIQERGIQAQiEPM/9UiiiS5KDDHFEhFbHHHFE5Jpppsyw0yzTMw2x1zz/KfQQg8VRphhhYQdTrjhhQ/ngSX5Bo9g+gAAAABJRU5ErkJggg==", id="pic")',  # a real PNG: KiCad refuses a board whose image is not one (fifth review)
                'Table((0, 6), [["A", "B"], ["1", "2"]], id="bom")',
                'Barcode("PCBC", (9, 9), kind="qrcode", id="code")',
                'Target((12, 2), shape="x", size=4, id="fid")',
                'Point((12, 4), size=1, id="pt")',
                'Generated("tuning_pattern", "tune", ["slot"], props={"single_sided": True, "origin": (1, 2)}, id="tune")',  # KiCad's own properties (fifth review: an unknown one is dropped by its save)
                "",
            ]
        ),
    )
    text = render_graphics(design.graphics, board="blinky")
    slot = stable_uuid("blinky", "layout", "slot")
    assert (
        "\t(gr_line\n"
        "\t\t(start 0 0)\n"
        "\t\t(end 4 0)\n"
        "\t\t(stroke (width 0.05) (type solid))\n"
        '\t\t(layer "Edge.Cuts")\n'
        f'\t\t(uuid "{slot}")\n'
        "\t)\n"
    ) in text
    assert "(gr_rect" in text and "(radius 0.4)" in text and '(layer "F.SilkS")' in text
    assert "(gr_circle" in text and "(center 5 5)" in text and "(end 6 5)" in text and "(fill hatch)" in text
    assert "(gr_arc" in text and "(gr_poly" in text and "(gr_curve" in text
    assert '(gr_text "REV A"' in text and "(at 8 1 0)" in text
    assert '(gr_text_box "note"' in text
    assert "(dimension" in text and "(type aligned)" in text and '(gr_text "10 mm"' in text
    assert '(group "marks"' in text
    assert f'"{stable_uuid("blinky", "layout", "slot")}"' in text
    assert "(image" in text and '"iVBORw0KGgoAAAANSUhEUgAAAAwAAAADCAIAAAAoQXllAAAAUklEQVR4nA3KMQEAIRADwYhABCKu"' in text
    assert "(table" in text and "(column_count 2)" in text and "(external yes)" in text and '(table_cell "A"' in text
    assert "(barcode" in text and "(type qr)" in text and '(text "PCBC")' in text and "(ecc_level L)" in text
    assert "(target x" in text and "(size 4)" in text
    assert "(point" in text and "(at 12 4)" in text
    assert "(generated" in text and "(type tuning_pattern)" in text and "(single_sided yes)" in text and "(origin (xy 1 2))" in text


@pytest.mark.kicad
def test_gen_is_the_python_the_stage_wrote_and_core_copper_still_emits(tmp_path: Path):
    """Was `test_gen_file_is_a_todo...`: `gen.write_todo` is retired; `layout.gen.py` is the Python the
    native route stage wrote (placer and router), and the job places and routes in memory. The core
    line still emits, once, and is never written into gen."""
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    (tmp_path / "layout.core.py").write_text(
        'Seg("LED", (1, 2), (3, 2), layer="F.Cu", width=0.25, id="led")\n'
    )
    out = tmp_path / "routed" / "layout.kicad_pcb"
    design = load_board(board)
    result = layout_job(design, out=out, board=board)
    assert result["error"] is None, result
    gen = (tmp_path / "routed" / "layout.gen.py").read_text()
    assert "TODO" not in gen
    assert "Place(" in gen and "Seg(" in gen and "Pour(" in gen
    assert 'id="led"' not in gen, "a core object is never written into gen"
    led = stable_uuid("blinky", "layout", "led")
    assert out.read_text().count(f'(uuid "{led}")') == 1
    assert '(net "LED")' in out.read_text()


def _png_1x1() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    scan = b"\x00\xff\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(scan)) + chunk(b"IEND", b"")


@pytest.mark.kicad
def test_kicad_loads_the_board_primitives(tmp_path: Path):
    """kicad-cli accepts every primitive. A string match is not the same thing."""
    png = base64.b64encode(_png_1x1()).decode()
    board = tmp_path / "gfx.py"
    board.write_text("Board((30, 20))\n")
    design = load_board(board)
    (tmp_path / "layout.core.py").write_text(
        "\n".join(
            [
                'Seg("GND", (2, 2), (8, 2), layer="F.Cu", width=0.25, id="gnd-seg")',
                'Arc("GND", (2, 4), (4, 6), (6, 4), layer="F.Cu", width=0.25, id="gnd-arc")',
                'Via("GND", (8, 4), size=0.6, drill=0.3, id="gnd-via")',
                'Via("GND", (10, 4), layers=("F.Cu", "In1.Cu"), size=0.45, drill=0.2, kind="blind", id="gnd-blind")',
                'Pour("GND", layer="B.Cu", points=[(1, 1), (20, 1), (20, 12), (1, 12)], id="gnd-pour")',
                'Line((0, 0), (30, 0), layer="Edge.Cuts", width=0.05, id="edge")',
                'Rect((1, 1), (4, 3), radius=0.4, id="box")',
                'Circle((6, 6), 1.5, fill="hatch", id="dot")',
                'DrawArc((8, 1), (9, 2), (10, 1), layer="Edge.Cuts", id="bite")',
                'Poly([(12, 1), (14, 1), (13, 3)], fill=True, id="mark")',
                'Curve([(1, 8), (2, 10), (4, 10), (5, 8)], id="bend")',
                'Text("REV A", (16, 2), id="rev")',
                'TextBox("note", (16, 4), (24, 7), id="note")',
                'Dimension((1, 14), (12, 14), id="width")',
                'Dimension((14, 8), (14, 14), kind="orthogonal", orientation=1, id="height")',
                'Group("marks", ["edge", "rev"], id="g")',
                f'Image((18, 12), "{png}", id="pic")',
                'Table((1, 16), [["A", "B"], ["1", "2"]], id="bom")',
                'Barcode("PCBC", (22, 14), kind="qr", size=6, id="code")',
                'Target((26, 4), shape="plus", id="fid")',
                'Point((26, 8), id="pt")',
                'Generated("tuning_pattern", "tune", ["gnd-seg"], id="tune")',
                "",
            ]
        )
    )
    load_layout(tmp_path / "layout.core.py", design, source="core")
    block = render(design, design.copper, board="gfx") + render_graphics(design.graphics, board="gfx")
    pcb = tmp_path / "gfx.kicad_pcb"
    pcb.write_text(splice(emit_pcb(design, name="gfx"), block))
    svg = tmp_path / "gfx.svg"
    proc = subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "export",
            "svg",
            "--mode-single",
            "--exclude-drawing-sheet",
            "--page-size-mode",
            "2",
            "--layers",
            "F.Cu,B.Cu,In1.Cu,F.SilkS,B.SilkS,Edge.Cuts,Dwgs.User,F.Fab",
            "--output",
            str(svg),
            str(pcb),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert svg.exists() and svg.stat().st_size > 0
