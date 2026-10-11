"""The front silkscreen: references placed to the fab's limits and never printed over anything, the
library's own drawings trimmed to the board and the pads, and KiCad's silk checks gating the build."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from pcbc.silk import INK_H, INK_W, _bbox_on_layer, drawings, hidden_references, legalize_silk, silk_plan, silk_parts_of_text
from pcbc.stackup import get_stackup, silk_limits

LIMITS = silk_limits(get_stackup("jlcpcb_2l_1oz"))
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _fp(
    ref: str,
    x: float,
    y: float,
    w: float,
    h: float,
    pads: list[tuple[float, float, float, float]] | None = None,
    *,
    rot: float = 0.0,
    silk: str = "",
    hidden: bool = False,
) -> str:
    """A footprint block: courtyard w x h centred on (x, y), optional pads (px, py, pw, ph), optional
    silk drawings (footprint frame)."""
    pad_text = "".join(
        f'\t\t(pad "{i + 1}" smd rect\n\t\t\t(at {px} {py} {rot:g})\n\t\t\t(size {pw} {ph})\n\t\t\t(layers "F.Cu" "F.Mask")\n\t\t)\n'
        for i, (px, py, pw, ph) in enumerate(pads or [])
    )
    hide = "\t\t\t(hide yes)\n" if hidden else ""
    return (
        f'\t(footprint "X"\n\t\t(layer "F.Cu")\n\t\t(at {x} {y} {rot:g})\n'
        f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.SilkS")\n{hide}\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
        f'\t\t(fp_rect\n\t\t\t(start {-w / 2} {-h / 2})\n\t\t\t(end {w / 2} {h / 2})\n\t\t\t(stroke (width 0.05) (type solid))\n\t\t\t(fill no)\n\t\t\t(layer "F.CrtYd")\n\t\t)\n'
        f"{silk}{pad_text}\t)\n"
    )


def _line(x0, y0, x1, y1, w=0.12) -> str:
    return f'\t\t(fp_line\n\t\t\t(start {x0} {y0})\n\t\t\t(end {x1} {y1})\n\t\t\t(stroke (width {w}) (type solid))\n\t\t\t(layer "F.SilkS")\n\t\t\t(uuid "u-{x0}-{y0}-{x1}-{y1}")\n\t\t)\n'


def _board(*fps: str) -> str:
    return "(kicad_pcb\n" + "".join(fps) + ")\n"


def _ref(text: str, ref: str) -> tuple[float, float, float, float, float, bool]:
    """(x, y, angle, size, thickness, hidden) of a reference as written."""
    j = text.index(f'(property "Reference" "{ref}"')
    prop = text[j : text.index("\n\t\t)\n", j)]
    at = re.search(r"\(at ([-0-9.]+) ([-0-9.]+) ([-0-9.]+)\)", prop)
    size = re.search(r"\(size ([0-9.]+) [0-9.]+\)", prop)
    th = re.search(r"\(thickness ([0-9.]+)\)", prop)
    return float(at[1]), float(at[2]), float(at[3]), float(size[1]), float(th[1]) if th else 0.0, "(hide yes)" in prop


def _box(text: str, ref: str, fx: float, fy: float, frot: float = 0.0) -> tuple[float, float, float, float]:
    """The world box the plan reserves for a printed reference."""
    import math

    lx, ly, ang, size, _t, _h = _ref(text, ref)
    a = math.radians(-frot)
    wx, wy = fx + lx * math.cos(a) - ly * math.sin(a), fy + lx * math.sin(a) + ly * math.cos(a)
    w, h = INK_W * size * len(ref), INK_H * size
    if ang % 180:
        w, h = h, w
    return wx - w / 2, wy - h / 2, wx + w / 2, wy + h / 2


def _gap(a, b) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return max(dx, dy) if (dx == 0 or dy == 0) else (dx * dx + dy * dy) ** 0.5


def test_references_are_the_fabs_minimum_size():
    """JLCPCB: 1.0 mm characters, 0.15 mm lines and a 1:6 stroke. Until 2026-10-03 every reference on
    the five boards was 0.4-0.8 mm with a 0.08-0.12 mm stroke."""
    text = _board(_fp("R1", 10, 10, 1.6, 0.9, [(-0.5, 0, 0.5, 0.6), (0.5, 0, 0.5, 0.6)]))
    new, report = legalize_silk(text, (20, 20))
    _x, _y, _a, size, thick, hidden = _ref(new, "R1")
    assert (size, thick, hidden) == (1.0, 0.167, False), (size, thick, hidden)
    assert report["hidden"] == []


def test_the_angle_written_is_the_texts_angle_on_the_board():
    """KiCad reads a footprint field's angle as the text's angle on the board, not relative to the part
    (pcbnew: C_BME at 90 with a file angle of 270 drew vertical text where the planner had reserved a
    horizontal box). A part turned 90 with room above it prints horizontal text: angle 0, not 270."""
    text = _board(_fp("C1", 10, 10, 1.6, 0.9, [(-0.5, 0, 0.5, 0.6), (0.5, 0, 0.5, 0.6)], rot=90))
    new, _report = legalize_silk(text, (20, 20))
    _x, _y, ang, _s, _t, _h = _ref(new, "C1")
    assert ang == 0.0, ang


def test_a_reference_turns_to_fit_a_narrow_slot():
    """A tall part, neighbours 2.2 mm off both sides, wide parts above and below: "C9" fits only
    turned, in the slot beside it."""
    text = _board(
        _fp("C9", 10, 10, 1.0, 3.2),
        _fp("L1", 7.2, 10, 1.0, 3.2),
        _fp("R1", 12.8, 10, 1.0, 3.2),
        _fp("U9", 10, 6.4, 12.0, 2.4),
        _fp("U8", 10, 13.6, 12.0, 2.4),
    )
    new, report = legalize_silk(text, (30, 30))
    assert "C9" not in report["hidden"], report
    _x, _y, rot, _s, _t, _h = _ref(new, "C9")
    assert rot in (90.0, 270.0), "the slot beside C9 only fits turned text"


def test_a_big_part_carries_its_reference_on_its_body_clear_of_its_pads():
    """A module in the board corner, a neighbour flush on each free side; its pads are on the edges."""
    pads = [(-5.5, y, 0.8, 0.4) for y in (-4, -3.2, -2.4)] + [(5.5, y, 0.8, 0.4) for y in (-4, -3.2, -2.4)]
    text = _board(
        _fp("U1", 7.0, 7.0, 13.0, 13.0, pads),
        _fp("C1", 14.4, 7.0, 1.6, 13.0),
        _fp("C2", 7.0, 14.4, 13.0, 1.6),
        _fp("C3", 14.4, 14.4, 1.6, 1.6),
    )
    new, report = legalize_silk(text, (15.6, 15.6))
    assert "U1" not in report["hidden"], report
    box = _box(new, "U1", 7.0, 7.0)
    assert abs((box[0] + box[2]) / 2 - 7) < 4 and abs((box[1] + box[3]) / 2 - 7) < 4, box
    for px, py, pw, ph in pads:
        assert _gap(box, (7 + px - pw / 2, 7 + py - ph / 2, 7 + px + pw / 2, 7 + py + ph / 2)) >= 0.15


def test_pads_outside_the_courtyard_still_keep_the_reference_off():
    """EasyEDA courtyards hug the body and the pads stick out (buck U1, the USB-C, the DS2's TSSOP): the
    part's outline is courtyard, silk and pads. The pads here reach 1.5 mm past a 1 x 1 courtyard."""
    pads = [(-2.0, 0, 1.0, 0.6), (2.0, 0, 1.0, 0.6)]
    text = _board(_fp("Q1", 10, 10, 1.0, 1.0, pads))
    new, _report = legalize_silk(text, (20, 20))
    box = _box(new, "Q1", 10, 10)
    for px, py, pw, ph in pads:
        assert _gap(box, (10 + px - pw / 2, 10 + py - ph / 2, 10 + px + pw / 2, 10 + py + ph / 2)) >= 0.15 - 1e-9, box


def test_every_pad_on_the_board_keeps_a_reference_off_not_only_the_parts_own():
    """A small part beside a neighbour whose pads stick out of its courtyard into the spots above the
    small part: the reference keeps 0.15 mm from them too (buck R_EN printed on U1's pads 4-6)."""
    text = _board(
        _fp("R9", 10, 10, 1.6, 0.9, [(-0.5, 0, 0.5, 0.6), (0.5, 0, 0.5, 0.6)]),
        _fp("U2", 10, 6.0, 1.0, 1.0, [(0, 2.4, 3.0, 1.0)]),  # its pad reaches down to y 8.9
    )
    new, report = legalize_silk(text, (20, 20))
    assert "R9" not in report["hidden"]
    box = _box(new, "R9", 10, 10)
    assert _gap(box, (8.5, 7.9, 11.5, 8.9)) >= 0.15 - 1e-9, box


def test_no_room_hides_the_reference_and_says_so():
    """No spot anywhere: the reference is not printed (never 0.4 mm over a neighbour, as before), and
    the note states the fact with the numbers (`docs/direction.md` §7b)."""
    text = _board(
        _fp("C1", 1.2, 1.0, 1.6, 1.0),
        _fp("U9", 3.0, 2.5, 6.0, 2.4),
        _fp("U8", 3.0, 1.0, 2.4, 1.0),
    )
    new, report = legalize_silk(text, (8, 8))
    assert "C1" in report["hidden"]
    assert _ref(new, "C1")[5] is True
    assert any(
        n.startswith("C1: silkscreen reference not printed: no spot for 1 mm text (0.15 mm from every pad opening") for n in report["notes"]
    ), report["notes"]
    assert not any("try" in n or "move" in n.lower() for n in report["notes"])
    assert hidden_references(new) == [r for r in ("C1", "U9", "U8") if r in report["hidden"]]


def test_a_librarys_own_hidden_reference_is_the_plans_to_show():
    """The plan is the one writer of a reference's visibility: a library that hides its own is shown
    when there is room, so a hidden reference on the board is always one the plan found no spot for."""
    text = _board(_fp("R1", 10, 10, 1.6, 0.9, hidden=True))
    new, report = legalize_silk(text, (20, 20))
    assert report["hidden"] == [] and _ref(new, "R1")[5] is False
    assert hidden_references(new) == []


def test_the_drawing_reader_keeps_polygons_circles_and_line_width():
    """`fp_poly` points, a circle's radius and the stroke all count (the fiducial's courtyard was
    1.25 x 0 mm; the ESP32's silk polygon was not read at all)."""
    block = (
        '(footprint "X"\n'
        '\t(fp_poly (pts (xy 0 0) (xy 4 0) (xy 4 3)) (stroke (width 0.2) (type solid)) (fill no) (layer "F.SilkS"))\n'
        '\t(fp_circle (center 10 10) (end 11.25 10) (stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))\n'
        ")"
    )
    assert _bbox_on_layer(block, "F.SilkS") == pytest.approx((-0.1, -0.1, 4.1, 3.1))
    assert _bbox_on_layer(block, "F.CrtYd") == pytest.approx((8.725, 8.725, 11.275, 11.275), abs=1e-3)
    assert [d["tag"] for d in drawings(block, "F.SilkS")] == ["fp_poly"]


def test_a_reference_never_prints_over_another_parts_silk():
    """A neighbour's silk polygon in the first spot above the part: the reference goes elsewhere (node
    C_MCU_HF printed over the ESP32's silk polygon, which the reader dropped)."""
    poly = '\t\t(fp_poly (pts (xy -3 -1.5) (xy 3 -1.5) (xy 3 1.5) (xy -3 1.5)) (stroke (width 0.12) (type solid)) (fill no) (layer "F.SilkS"))\n'
    text = _board(
        _fp("R1", 10, 10, 1.6, 0.9, [(-0.5, 0, 0.5, 0.6), (0.5, 0, 0.5, 0.6)]),
        _fp("U7", 10, 7.2, 0.5, 0.5, silk=poly),
    )
    new, report = legalize_silk(text, (20, 20))
    assert "R1" not in report["hidden"]
    box = _box(new, "R1", 10, 10)
    assert _gap(box, (7 - 0.06, 5.7 - 0.06, 13 + 0.06, 8.7 + 0.06)) >= 0.15 - 1e-9 or box[1] > 8.7, box


def test_library_silk_is_trimmed_to_the_edge_and_off_the_pads():
    """A connector at the board edge whose library outline runs 0.1 mm past it, and a silk line across a
    neighbour's pad: each line is cut to the board inset by the fab's clearance and around every pad's
    opening plus 0.15 mm (36 library items crossed the edge on the five boards, none was trimmed)."""
    silk = _line(-3, -1, 3, -1) + _line(-3, 0.4, 3, 0.4)
    text = _board(
        _fp("J1", 3.0, 1.1, 6.0, 2.0, [(-1.25, 0, 1.0, 1.0), (1.25, 0, 1.0, 1.0)], silk=silk),
    )
    new, _report = legalize_silk(text, (20, 20))
    j = new.index('(property "Reference" "J1"')
    lines = [tuple(map(float, m)) for m in re.findall(r"\(fp_line \(start ([-0-9.]+) ([-0-9.]+)\) \(end ([-0-9.]+) ([-0-9.]+)\)", new)]
    lines += [tuple(map(float, m)) for m in re.findall(r"\(fp_line\n\t\t\t\(start ([-0-9.]+) ([-0-9.]+)\)\n\t\t\t\(end ([-0-9.]+) ([-0-9.]+)\)", new)]
    assert j > 0 and lines, new
    for x0, y0, x1, y1 in lines:
        wy0 = 1.1 + y0
        assert wy0 - 0.06 >= 0.15 - 1e-6, ("the top line is cut back inside the edge: it is gone", y0)
        assert min(x0, x1) + 3.0 - 0.06 >= 0.15 - 1e-6, ("cut back inside the left edge", x0, x1)
        for px in (-1.25, 1.25):  # the line through the pads keeps 0.15 mm off each opening (0.5 + 0.15 + 0.06)
            lo, hi = min(x0, x1), max(x0, x1)
            assert hi <= px - 0.71 + 1e-6 or lo >= px + 0.71 - 1e-6, (x0, x1, px)
    assert len(lines) == 3, ("three pieces of the line through the pads", lines)
    assert '"u--3--1-3--1"' not in new, "the line past the edge is left out"


def test_two_parts_library_silk_keeps_the_fabs_clearance():
    """The smaller part's silk yields to the bigger part's: node's U5 and D1 outlines sat 0.005 mm
    apart, a KiCad `silk_overlap` once the 0.15 mm rule is set."""
    big = _line(-3, -1, 3, -1) + _line(-3, 1, 3, 1)
    small = _line(-1, 0, 1, 0)
    text = _board(_fp("U1", 10, 10, 6.2, 2.2, silk=big), _fp("D1", 10, 11.05, 0.5, 0.5, silk=small))
    new, _report = legalize_silk(text, (20, 20))
    assert '"u--3-1-3-1"' in new, "the bigger part keeps its line"
    assert '"u--1-0-1-0"' not in new, "the smaller part's line within 0.15 mm of it is cut"


def test_the_project_carries_the_fabs_silk_rules_as_errors():
    """The `.kicad_pro` holds JLCPCB's silk minimums as KiCad's own rules, and KiCad's five silk checks as
    errors (KiCad's default is a warning: 162 on the five boards, nothing gated)."""
    from pcbc.fab import _IGNORE_DRC, copper_drc_errors
    from pcbc.language import load_board
    from pcbc.seed import emit_pro

    design = load_board(EXAMPLES / "blinky" / "blinky.py")
    ds = json.loads(emit_pro(design, name="blinky"))["board"]["design_settings"]
    assert {k: ds["rules"][k] for k in ("min_text_height", "min_text_thickness", "min_silk_clearance")} == {
        "min_text_height": 1.0,
        "min_text_thickness": 0.15,
        "min_silk_clearance": 0.15,
    }
    for kind in ("silk_overlap", "silk_over_copper", "silk_edge_clearance", "text_height", "text_thickness"):
        assert ds["rule_severities"][kind] == "error", kind
        assert kind not in _IGNORE_DRC
        assert copper_drc_errors({"violations": [{"type": kind, "severity": "error", "items": []}]}), kind


def test_fab_notes_list_every_reference_not_printed(tmp_path: Path):
    from pcbc.compile import compile_design
    from pcbc.fab import _write_notes
    from pcbc.language import load_board

    job = compile_design(load_board(EXAMPLES / "blinky" / "blinky.py"))
    line = "R_EN: silkscreen reference not printed: no spot for 1 mm text"
    _write_notes(tmp_path, job, {"silk": {"hidden": [line]}})
    notes = (tmp_path / "FAB_NOTES.md").read_text()
    assert "## Silkscreen\n\n- " + line in notes
    _write_notes(tmp_path, job, {"silk": {"hidden": []}})
    notes = (tmp_path / "FAB_NOTES.md").read_text()
    assert "## Silkscreen\n\n- Every reference is printed (a fiducial carries none).\n- Every pin-1 and polarity mark of the libraries is printed." in notes
    mark = "U5: silkscreen mark not printed: the library's filled polygon 0.35 x 0.00 mm mark has no area and a line under 0.15 mm"
    _write_notes(tmp_path, job, {"silk": {"hidden": [], "marks": [mark]}})
    assert "- Every reference is printed (a fiducial carries none).\n- " + mark in (tmp_path / "FAB_NOTES.md").read_text()


@pytest.mark.kicad
@pytest.mark.parametrize("name", ["blinky", "buck", "c3_usb", "node"])
def test_kicad_finds_no_silk_fault_on_the_placed_examples(name: str):
    """KiCad's own silk checks, with the fab's rules as errors, on each example's placed board: no
    `silk_overlap`, `silk_over_copper`, `silk_edge_clearance`, `text_height` or `text_thickness`
    (2026-10-03 before the fix, on the routed boards: blinky 2, buck 24, c3_usb 36, node 59 warnings)."""
    from conftest import placed_board

    from pcbc.fab import kicad_cli

    pcb = placed_board(name, EXAMPLES / name / f"{name}.py")
    out = pcb.parent / "silk_drc.json"
    subprocess.run([str(kicad_cli()), "pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(pcb)], capture_output=True, timeout=600)
    doc = json.loads(out.read_text())
    silk = [v for v in doc.get("violations", []) if v["type"].startswith(("silk", "text_"))]
    assert silk == [], json.dumps(silk, indent=1)[:3000]
    # Every printed reference is the fab's size; every hidden one is in the plan's notes.
    text = pcb.read_text()
    for ref in re.findall(r'\(property "Reference" "([^"]+)"', text):
        if ref.startswith("FID") or ref in hidden_references(text):
            continue
        assert _ref(text, ref)[3] >= 1.0, ref


def test_a_modules_exposed_pad_keeps_its_reference_off_its_body_centre():
    """A big part may carry its reference on its body, but never on a pad: a module with an exposed pad
    under its centre prints its reference beside the pad, 0.15 mm clear of its opening."""
    pads = [(0, 0, 4.0, 4.0)] + [(-5.5, y, 0.8, 0.4) for y in (-4, -3.2, -2.4)]
    text = _board(
        _fp("U1", 7.0, 7.0, 13.0, 13.0, pads),
        _fp("C1", 14.4, 7.0, 1.6, 13.0),
        _fp("C2", 7.0, 14.4, 13.0, 1.6),
        _fp("C3", 14.4, 14.4, 1.6, 1.6),
    )
    new, report = legalize_silk(text, (15.6, 15.6))
    assert "U1" not in report["hidden"], report
    box = _box(new, "U1", 7.0, 7.0)
    for px, py, pw, ph in pads:
        assert _gap(box, (7 + px - pw / 2, 7 + py - ph / 2, 7 + px + pw / 2, 7 + py + ph / 2)) >= 0.15 - 1e-9, (box, px, py)


def test_a_reference_stays_off_a_neighbours_outline_between_its_pads():
    """A part's outline is its courtyard, its silk and its pads together: a connector whose pads stand
    3 mm out from a 1 mm courtyard covers the space between them, and a reference does not print
    there even though no pad is within 0.15 mm (the USB-C and the XH on the examples)."""
    text = _board(
        _fp("R1", 10, 1.2, 1.6, 0.9, [(-0.5, 0, 0.5, 0.6), (0.5, 0, 0.5, 0.6)]),  # above it is the board edge
        _fp("J1", 10, 4.0, 1.0, 1.0, [(-3.0, -1.2, 1.0, 1.0), (3.0, -1.2, 1.0, 1.0)]),
    )
    new, report = legalize_silk(text, (20, 20))
    assert "R1" not in report["hidden"], report
    box = _box(new, "R1", 10, 1.2)
    outline = (10 - 3.5, 4.0 - 1.7, 10 + 3.5, 4.0 + 0.5)
    assert _gap(box, outline) >= 0.05 - 1e-9, (box, outline)


def test_library_silk_lines_print_at_the_fabs_minimum_width():
    """KiCad's stock 0402/0603/0805 outlines draw 0.12 mm lines; JLCPCB prints 0.15 mm and up. The copy on
    the board is widened (and kept the fab's clearance at its new width); a wider line stays as drawn."""
    silk = _line(-3, -1, 3, -1, 0.12) + _line(-3, 1, 3, 1, 0.2)
    text = _board(_fp("U1", 10, 10, 6.4, 2.4, silk=silk))
    new, _report = legalize_silk(text, (20, 20))
    widths = [float(w) for w in re.findall(r'\(fp_line[^"]*?\(width ([0-9.]+)\)', new, re.S)]
    assert sorted(widths) == [0.15, 0.2], widths


def test_every_trimmed_piece_has_a_uuid_of_its_own():
    """A fetched part's library file carries no uuids (`source.upgrade_footprint` strips them): two of
    its lines cut by the same pads still give every piece its own uuid on the board."""
    silk = (
        '\t\t(fp_line (start -3 0.3) (end 3 0.3) (stroke (width 0.15) (type solid)) (layer "F.SilkS"))\n'
        '\t\t(fp_line (start -3 -0.3) (end 3 -0.3) (stroke (width 0.15) (type solid)) (layer "F.SilkS"))\n'
    )
    text = _board(_fp("J1", 10, 10, 6.4, 2.0, [(0, 0, 1.0, 1.0)], silk=silk))
    new, _report = legalize_silk(text, (20, 20))
    uuids = re.findall(r'\(fp_line [^\n]*\(uuid "([^"]+)"\)\)', new)
    assert len(uuids) == 4 and len(set(uuids)) == 4, uuids


def test_a_filled_polygon_with_no_area_is_left_out():
    """A zero-area filled polygon at width 0 prints nothing the fab can make (node U5's EasyEDA SOT-23-5,
    0.35 x 0 mm); a real filled pin-1 mark is kept."""
    silk = (
        '\t\t(fp_poly (pts (xy 0.2 1) (xy 0.55 1) (xy 0.2 1)) (stroke (width 0) (type solid)) (fill yes) (layer "F.SilkS") (uuid "flat"))\n'
        '\t\t(fp_poly (pts (xy -0.3 -1) (xy 0.3 -1) (xy 0 -0.55)) (stroke (width 0.12) (type solid)) (fill yes) (layer "F.SilkS") (uuid "mark"))\n'
    )
    text = _board(_fp("U5", 10, 10, 3.0, 3.0, silk=silk))
    new, _report = legalize_silk(text, (20, 20))
    assert '"flat"' not in new and '"mark"' in new


def _circle(cx, cy, r, w=0.3, uid="pin1") -> str:
    return f'\t\t(fp_circle (center {cx} {cy}) (end {cx + r} {cy}) (stroke (width {w}) (type solid)) (fill no) (layer "F.SilkS") (uuid "{uid}"))\n'


def test_a_pin1_mark_touching_a_pad_opening_moves_clear_and_is_never_dropped():
    """refute:silk (major): buck U1's pin-1 circle (0.6 mm, 0.3 wide) sat 0.44 mm from pad 1, inside the pad's
    clearance zone, and the trim left it out whole with no word. A mark moves, whole, to the nearest legal spot within
    MARK_MOVE_MM and keeps its uuid; every point moves by the same offset."""
    from pcbc.silk import MARK_MOVE_MM

    pads = [(-1.0, 0.0, 0.6, 1.2), (1.0, 0.0, 0.6, 1.2)]
    text = _board(_fp("U1", 10, 10, 4.0, 4.0, pads, silk=_circle(-1.0, -1.1, 0.3)))
    new, report = legalize_silk(text, (20, 20))
    m = re.search(r'\(fp_circle \(center ([-0-9.]+) ([-0-9.]+)\) \(end ([-0-9.]+) ([-0-9.]+)\)[^\n]*\(uuid "pin1"\)', new)
    assert m, new
    cx, cy, ex, ey = map(float, m.groups())
    assert (round(ex - cx, 4), round(ey - cy, 4)) == (0.3, 0.0), "moved whole, not resized"
    moved = ((cx + 1.0) ** 2 + (cy + 1.1) ** 2) ** 0.5
    assert 0 < moved <= MARK_MOVE_MM + 1e-9, moved
    # the ring's ink (radius 0.3 + half its 0.3 width) keeps 0.15 mm from pad 1's opening (x -1.3..-0.7, y -0.6..0.6)
    ink = (cx - 0.45, cy - 0.45, cx + 0.45, cy + 0.45)
    for pad in ((-1.3, -0.6, -0.7, 0.6), (0.7, -0.6, 1.3, 0.6)):
        assert _gap(ink, pad) >= 0.15 - 1e-6, (cx, cy, pad)
    assert [r["ref"] for r in report["marks_moved"]] == ["U1"] and report["marks_dropped"] == []


def test_a_mark_with_no_legal_spot_is_left_out_as_a_fact_that_fab_reads_off_the_board():
    """With pads all round it (none of 16 directions clears within MARK_MOVE_MM), the mark is left out, the plan's notes
    say so, and fab's reading of the board (`unprinted_marks`, against the part's library drawings) says the same."""
    from pcbc.silk import drawings, unprinted_marks

    pads = [(x, y, 1.4, 1.4) for x in (-1.0, 0.0, 1.0) for y in (-1.0, 0.0, 1.0) if (x, y) != (0.0, 0.0)]
    lib = _circle(0.0, 0.0, 0.1, w=0.15, uid="dot")
    text = _board(_fp("U9", 10, 10, 3.0, 3.0, pads, silk=lib))
    new, report = legalize_silk(text, (20, 20))
    assert '"dot"' not in new
    want = ("U9: silkscreen mark not printed: the library's circle 0.20 x 0.20 mm mark has no spot within 1 mm of where the library draws it "
            "that is 0.15 mm from every pad opening and other silk and inside the board edge")
    assert want in report["notes"], report["notes"]
    shapes = drawings(text[text.index("(footprint"):], "F.SilkS")
    assert unprinted_marks(new, {"U9": shapes}, LIMITS) == [want]
    assert unprinted_marks(text, {"U9": shapes}, LIMITS) == [], "the library's own board carries it"


@pytest.mark.parametrize("name, dropped", [("buck", ["U1", "U1", "L1"]), ("c3_usb", []), ("node", ["U4", "U4", "U5"])])
def test_the_examples_print_every_pin1_mark_their_libraries_draw(tmp_path: Path, name: str, dropped: list):
    """refute:silk on the examples: buck U1's circle and dot, the ESP32's triangle (c3_usb, node U1) and node U4/U5/Q1's
    dots were left out, and nothing said why. Every mark is printed, moved where it touched a pad opening, unless it
    would mislead (docs/direction.md §4c): node U5's zero-area polygon; a dot nearer another part's pad than any of its
    own, or as near — buck U1's two (by L1's pad), L1's (by J_IN's housing), node U4's outside dot at pad 5's corner and
    its vent dot (by the I2C pull-ups') — each a fact, and fab's reading of the board states the same fact, not "no spot"."""
    import shutil

    from pcbc.language import load_board
    from pcbc.place_native import emitted_text, place, seed_feet

    dst = tmp_path / name
    shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns("layout"))
    design = load_board(dst / f"{name}.py")
    pl = place(design, name=name)
    assert pl.error is None, pl.error
    from pcbc.silk import unprinted_marks

    from pcbc.orient import library_mark_reason

    feet = seed_feet(design)
    text = emitted_text(design, pl, name=name)
    lines = unprinted_marks(text, {pf.ref: pf.lib.silk_shapes for pf in feet}, LIMITS, reason=library_mark_reason(design, feet, LIMITS, text))
    assert [ln.split(":")[0] for ln in lines] == dropped, lines
    # Fab's reading of the board states the place stage's own facts, word for word (docs/direction.md §7b).
    assert lines == [n for n in pl.notes if "silkscreen mark not printed" in n], (lines, pl.notes)
    for ln in lines:
        assert "no spot" not in ln, ln
        if ln.startswith(("U4", "L1")) or ln.startswith("U1"):
            # A dot nearer another part's pad, or as near, names that part or none (refute:fab 2026-10-08).
            assert ln.endswith("(G5)") and ("another part's pin" in ln or "names no part" in ln), ln
