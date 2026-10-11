"""docs/schematic-layout.md §2.6: what "readable" means, as a vector, and the text model it is scored with.

The scored text boxes come from KiCad's own SVG of the emitted sheet; pcbc's field model (`sch_emit._text_box`,
`_field_just`, `sch_font`) must agree with it to 0.5 mm for every Reference and Value, on every bench board and for
every rotation, mirror, justification and field angle. Each test fails when the behaviour it pins is removed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pcbc.language import load_board
from pcbc.sch_emit import _field_just, _paper, _text_box, emit_from_design, emit_schematic_file
from pcbc.sch_score import HARD, key, score, seg_through_box, svg_texts, with_svg

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
BENCH = [(b, EXAMPLES / b / f"{b}.py") for b in ("blinky", "buck", "c3_usb", "node")]


def test_a_wire_through_a_small_body_near_its_end_is_through_it():
    """measure.py sampled t = 0.35, 0.5, 0.65 and missed this one; the test is exact (Liang-Barsky)."""
    box = (9.0, -0.5, 9.6, 0.5)
    assert seg_through_box(0.0, 0.0, 10.0, 0.0, box)
    assert not seg_through_box(0.0, 0.0, 8.9, 0.0, box)
    assert not seg_through_box(0.0, 0.5, 10.0, 0.5, box), "along an edge is touching, not through"
    assert seg_through_box(9.3, -5.0, 9.3, 5.0, box)


def test_kicads_invisible_text_is_read_with_its_anchor_and_rotation():
    svg = (
        '<text x="10.0000" y="20.0000"\ntextLength="4.0000" font-size="1.6933" lengthAdjust="spacingAndGlyphs"\n'
        'text-anchor="start" opacity="0" stroke-opacity="0">AB</text>\n'
        '<g transform="rotate(-90.000000 50.0000 50.0000)">\n<text x="50.0000" y="50.0000"\ntextLength="6.0000" '
        'font-size="1.6933" lengthAdjust="spacingAndGlyphs"\ntext-anchor="end" opacity="0" stroke-opacity="0">R&amp;1</text>'
    )
    (a, box_a), (b, box_b) = svg_texts(svg)
    assert a == "AB" and abs(box_a[0] - 10.0) < 1e-6 and abs(box_a[2] - 14.0) < 1e-6 and box_a[3] > 19.9 and box_a[1] < 18.8
    assert b == "R&1" and abs((box_b[3] - box_b[1]) - 6.0) < 1e-6, "rotated: 6 mm tall, running from the anchor"
    assert box_b[1] > 49.9, "text-anchor end, turned -90 degrees: it runs down the sheet from y=50"


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [None, "x", "y"])
def test_the_field_justification_swap_is_its_own_inverse_and_turns_with_the_symbol(rot, mirror):
    for vis in (0, 90):
        for just in (None, "left", "right"):
            assert _field_just(_field_just(just, vis, rot, mirror), vis, rot, mirror) == just
    assert _field_just("left", 0, rot, mirror) == ("right" if (rot in (90, 180)) != (mirror == "y") else "left")
    assert _field_just("left", 90, rot, mirror) == ("right" if (rot in (180, 270)) != (mirror == "x") else "left")
    assert _field_just(None, 0, rot, mirror) is None


def test_a_vertical_field_runs_up_when_left_down_when_right_and_centres_otherwise():
    up, down, mid = (_text_box("R1", 10.0, 20.0, j, 90) for j in ("left", "right", None))
    assert up[3] == 20.0 and up[1] < 20.0
    assert down[1] == 20.0 and down[3] > 20.0
    assert abs((mid[1] + mid[3]) / 2.0 - 20.0) < 1e-9


def test_the_paper_holds_the_sheet_in_width_and_height():
    from types import SimpleNamespace

    from pcbc.model import SchSheetSpec

    class P:
        def __init__(self, y):
            self.x, self.y, self.rot, self.mirror = 50.0, y, 0.0, None
            self.bbox = (-5.0, -5.0, 5.0, 5.0)
            self.hw = self.hh = 5.0

    assert _paper([P(50.0)]) == "A4"
    assert _paper([P(200.0)]) == "A3", "narrow and tall: A4 holds the width, not the height"
    assert _paper([P(300.0)]) == "A2", "A3 by width, which the old rule took, would leave it below the edge"
    assert _paper([P(300.0)], SchSheetSpec(paper="A4")) == "A4"
    assert _paper([P(50.0)], SimpleNamespace(paper=None)) == "A4"


def test_the_order_is_hard_then_lint_then_labels_then_partner_then_area():
    base = {"hard": 0, "lint_issues": 2, "labels": 10, "label_partner_mm": 50.0, "extent_mm": [100.0, 100.0]}
    worse = [
        {**base, "hard": 1, "lint_issues": 0, "labels": 0},
        {**base, "lint_issues": 3, "labels": 0},
        {**base, "labels": 11, "label_partner_mm": 0.0},
        {**base, "label_partner_mm": 51.0, "extent_mm": [1.0, 1.0]},
        {**base, "extent_mm": [100.0, 101.0]},
    ]
    for w in worse:
        assert key(base) < key(w), w
    assert key(base) == key(dict(base)), "a tie: the loop keeps the later pass (<=)"


@pytest.mark.parametrize("name,board", BENCH, ids=[b for b, _ in BENCH])
def test_the_bench_sheets_hold_the_generator_invariants_and_score_every_hard_number(name, board):
    from pcbc.sch_score import invariant_errors

    rep: dict = {}
    emit_from_design(load_board(board), title=name, report=rep)
    vec = rep["readability"]
    assert invariant_errors(rep["_sheet"], rep["_parts"]) == []
    assert set(HARD) <= set(vec) and vec["hard"] == sum(vec[k] for k in HARD)
    assert not any("lengthen the pins" in i or "free side is the other way" in i for i in rep["issues"]), "facts, no advice"


def test_a_library_fact_is_reported_and_not_scored():
    rep: dict = {}
    emit_from_design(load_board(EXAMPLES / "node" / "node.py"), title="node", report=rep)
    lib = [i for i in rep["issues"] if "that is the .kicad_sym" in i]
    assert lib and rep["readability"]["lint_issues"] == len(rep["issues"]) - len(lib)


def test_the_sheet_is_the_same_under_two_hash_seeds():
    code = (
        "import hashlib, sys; from pcbc.language import load_board; from pcbc.sch_emit import emit_from_design; "
        "print(','.join(hashlib.sha256(emit_from_design(load_board(b), title='x').encode()).hexdigest()[:12] for b in sys.argv[1:]))"
    )
    boards = [str(b) for _, b in BENCH]
    got = {
        subprocess.run([sys.executable, "-c", code, *boards], env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True, check=True).stdout
        for seed in ("0", "1")
    }
    assert len(got) == 1, got


@pytest.mark.kicad
@pytest.mark.parametrize("name,board", BENCH, ids=[b for b, _ in BENCH])
def test_every_reference_and_value_is_where_kicad_draws_it(tmp_path: Path, name, board):
    """The fidelity test (§2.6, PR 2): every Reference and Value box of the model within 0.5 mm of KiCad's SVG."""
    import math

    from pcbc.netcheck import sheet_svg

    rep: dict = {}
    sch = emit_schematic_file(load_board(board), tmp_path / f"{name}.kicad_sch", title=name, report=rep)
    texts = svg_texts(sheet_svg(sch))
    parts = {p.ref: p for p in rep["_parts"]}
    off = []
    for o in rep["_sheet"].occupants:
        if o.kind not in ("ref", "value"):
            continue
        s = o.owner if o.kind == "ref" else parts[o.owner].display
        cx, cy = (o.box[0] + o.box[2]) / 2.0, (o.box[1] + o.box[3]) / 2.0
        d = min((math.hypot((b[0] + b[2]) / 2.0 - cx, (b[1] + b[3]) / 2.0 - cy) for t, b in texts if t == s), default=99.0)
        if d > 0.5:
            off.append(f"{o.owner} {o.kind} {d:.2f} mm")
    assert off == []


@pytest.mark.kicad
def test_every_orientation_draws_its_fields_where_the_model_says(tmp_path: Path):
    """Each rotation, mirror, justification and field angle, on a box symbol and a resistor: KiCad's text box
    within 0.5 mm of the model's (the J_LOAD fields that KiCad drew across the body, review-readability B3)."""
    import copy

    from pcbc.sch_prims import write
    import math

    import pcbc.sch_emit as E
    from pcbc.netcheck import sheet_svg

    d = load_board(EXAMPLES / "node" / "node.py")
    kinds = {n.name: n.kind for n in d.nets.values()}
    parts = {p.ref: p for p in E._parts_from_design(d, kinds)}
    E._ids = E._Ids("orient")
    try:
        libs = [E._lib_r(), parts["J_LOAD"].lib_sexp]
        body, cases, i = [], [], 0
        for ref in ("J_LOAD", "R_TDIV"):
            for rot in (0, 90, 180, 270):
                for mirror in (None, "x", "y"):
                    for just in (None, "left", "right"):
                        for vis in (0, 90):
                            p = copy.deepcopy(parts[ref])
                            p.ref, p.display = f"X{i}", "VALUE"
                            p.x, p.y, p.rot, p.mirror = 30.0 + (i % 20) * 25.0, 30.0 + (i // 20) * 25.0, float(rot), mirror
                            seen = (p.x + 3.0, p.y - 4.0, just, vis)
                            p.text_ref = E._file_spot(p, seen)
                            p.text_val = E._file_spot(p, (p.x + 3.0, p.y + 4.0, just, vis))
                            body.append(E._instance(p))
                            cases.append((p.ref, seen, (ref, rot, mirror, just, vis)))
                            i += 1
    finally:
        E._ids = None
    sch = tmp_path / "o.kicad_sch"
    sch.write_text(
        '(kicad_sch (version 20260306) (generator "pcbc") (uuid "00000000-0000-0000-0000-000000000001") (paper "A0")\n'
        " (lib_symbols\n" + "".join(libs) + " )\n" + "".join(write(b) for b in body) + ' (sheet_instances (path "/" (page "1")))\n)\n'
    )
    drawn = dict(svg_texts(sheet_svg(sch)))
    off = []
    for r, seen, what in cases:
        m = _text_box(r, *seen)
        k = drawn[r]
        dist = math.hypot((m[0] + m[2] - k[0] - k[2]) / 2.0, (m[1] + m[3] - k[1] - k[3]) / 2.0)
        if dist > 0.5:
            off.append((what, round(dist, 2)))
    assert off == []


@pytest.mark.kicad
def test_the_scored_text_collisions_come_from_kicads_drawing(tmp_path: Path):
    from pcbc.netcheck import sheet_svg

    rep: dict = {}
    sch = emit_schematic_file(load_board(EXAMPLES / "node" / "node.py"), tmp_path / "node.kicad_sch", title="node", report=rep)
    occ, matched, unmatched = with_svg(rep["_sheet"].occupants, rep["_parts"], sheet_svg(sch))
    assert unmatched == 0 and matched > 200
    moved = [o for o, m in zip(occ, rep["_sheet"].occupants) if o.box != m.box]
    assert moved, "the boxes scored are KiCad's, not the model's"
    vec = score(rep["_sheet"], rep["_parts"], rep, paper="A3", occupants=occ, svg_matched=matched)
    assert vec["text_source"] == "kicad-svg" and vec["text_collisions"] == 0


def test_a_wire_over_a_pin_line_is_anywhere_on_it_but_the_pins_end():
    from pcbc.sch_emit import over_pin_line

    pin = (10.0, 20.0, 10.0, 15.0, "SW.2")  # end (10, 20), body side (10, 15): a vertical pin line
    assert over_pin_line(5.0, 17.5, 15.0, 17.5, pin), "across it"
    assert over_pin_line(5.0, 17.5, 10.0, 17.5, pin), "ending on it"
    assert over_pin_line(10.0, 20.0, 10.0, 18.0, pin), "from the end, inward along it"
    assert over_pin_line(10.0, 16.0, 10.0, 25.0, pin), "along it, past the end"
    assert not over_pin_line(10.0, 20.0, 10.0, 25.0, pin), "from the end, outward: the pin's own wire"
    assert not over_pin_line(5.0, 20.0, 15.0, 20.0, pin), "through the end itself is the pin-end rule's, not this one's"
    assert not over_pin_line(5.0, 21.0, 15.0, 21.0, pin) and not over_pin_line(11.0, 15.0, 11.0, 25.0, pin)


@pytest.mark.parametrize("core", [True, False], ids=["core", "auto"])
def test_node_draws_no_wire_over_a_pin_line_and_the_score_counts_one_as_hard(core: bool):
    """refute:sheet (major, 2026-10-08): on node, auto and with core, the LED wire at y=120.65 ran across SW_RST pin
    2's pin line, so LED read as tied to the switch and GND, and the hard set reported 0. A wire over a pin line is now
    refused by the router and counted as the hard `wire_over_pin`."""
    from pcbc.sch_emit import over_pin_line, pin_lines

    rep: dict = {}
    emit_from_design(load_board(EXAMPLES / "node" / "node.py", schematic_core=core), title="node", report=rep)
    sheet, parts = rep["_sheet"], rep["_parts"]
    lines = pin_lines(parts)
    assert [(s[4], ln[4]) for s in sheet.segs for ln in lines if over_pin_line(*s[:4], ln)] == []
    assert rep["readability"]["wire_over_pin"] == 0
    sw = next(ln for ln in lines if ln[4] == "SW_RST.2")
    y = (sw[1] + sw[3]) / 2.0
    sheet.segs.append((sw[0] - 5.08, y, sw[0] + 5.08, y, "LED"))
    vec = score(sheet, parts, rep, paper=rep["readability"]["paper"], region_boxes=rep["_boxes"])
    assert vec["wire_over_pin"] == 1 and vec["hard"] >= 1
    assert len(vec["detail"]["wire_over_pin"]) == 1 and vec["detail"]["wire_over_pin"][0].endswith("runs over SW_RST.2's pin line")


def test_the_router_refuses_a_wire_over_any_pin_line_and_names_it():
    """`_Sheet.segment_ok` refuses a wire across a pin's drawn line for every part (box and two-pin symbols alike), and a
    refused lock names it (`blocker`)."""
    from pcbc.sch_emit import _Sheet, pin_lines

    rep: dict = {}
    emit_from_design(load_board(EXAMPLES / "node" / "node.py"), title="node", report=rep)
    parts = rep["_parts"]
    fresh = _Sheet(parts)
    for ref_pin in ("SW_RST.2", "SW_BOOT.1"):
        ln = next(ln for ln in pin_lines(parts) if ln[4] == ref_pin)
        if abs(ln[0] - ln[2]) < 1e-6:  # vertical pin line: cross it horizontally at its middle
            y = (ln[1] + ln[3]) / 2.0
            seg = (ln[0] - 1.27, y, ln[0] + 1.27, y)
        else:
            x = (ln[0] + ln[2]) / 2.0
            seg = (x, ln[1] - 1.27, x, ln[1] + 1.27)
        assert not fresh.segment_ok(*seg, "X"), ref_pin
        assert fresh.blocker([seg[:2], seg[2:]], "X") == f"the pin line of {ref_pin}", fresh.blocker([seg[:2], seg[2:]], "X")


# ------------------------------------------------------------------ two wires on one stretch (refute:sheet 2026-10-08)


def _ds2_or_example(tmp_path: Path, name: str, core: bool) -> Path:
    if name != "ds2":
        board = EXAMPLES / name / f"{name}.py"
        return board
    import shutil

    from conftest import ds2_dir

    src = ds2_dir()
    if not (src / "ds2_addon.py").exists():
        pytest.skip("the DS2 Addon is not checked out here")
    dst = tmp_path / "ds2"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("layout"))
    if not core:
        (dst / "schematic.core.py").unlink()
    return dst / "ds2_addon.py"


@pytest.mark.parametrize("name, core", [("c3_usb", True), ("c3_usb", False), ("ds2", True), ("ds2", False)], ids=["c3_usb", "c3_usb-auto", "ds2", "ds2-auto"])
def test_no_wire_is_drawn_along_another_and_no_dot_sits_where_nothing_branches(tmp_path: Path, name: str, core: bool):
    """refute:sheet (major, 2026-10-08): a power symbol's or PWR_FLAG's stub was laid from its pin along a wire of its
    own net already there (c3_usb's GND stub over U1's GND bus bar; DS2's flags at J3.3 and C7.2), so two wires lay on
    one stretch and a junction dot sat on the bare pin end, with 2.54 mm to the real T. The stub now starts where that
    wire turns away, and the hard `wire_overlaps` counts what is left: 0 on all four sheets."""
    from pcbc.sch_emit import wire_overlaps

    rep: dict = {}
    emit_from_design(load_board(_ds2_or_example(tmp_path, name, core), schematic_core=core), title=name, report=rep)
    assert wire_overlaps(rep["_sheet"], rep["_parts"]) == []
    assert rep["readability"]["wire_overlaps"] == 0 and rep["readability"]["hard"] == 0, rep["readability"].get("detail")


def test_the_score_counts_a_wire_along_another_and_the_dot_it_puts_on_a_bare_pin_end():
    rep: dict = {}
    emit_from_design(load_board(EXAMPLES / "c3_usb" / "c3_usb.py"), title="c3_usb", report=rep)
    sheet, parts = rep["_sheet"], rep["_parts"]
    from pcbc.sch_emit import _near, pin_world

    ends = [(pin_world(p, pin), f"{p.ref}.{pin.number}") for p in parts for pin in p.pins]
    # A wire leaving a pin end that no other wire touches: its first half drawn a second time.
    x0, y0, x1, y1, net, pin = next(
        (s[0], s[1], s[2], s[3], s[4], name)
        for s in sheet.segs
        for (ex, ey), name in ends
        if _near(s[0], s[1], ex, ey) and sum(1 for t in sheet.segs if _near(t[0], t[1], ex, ey) or _near(t[2], t[3], ex, ey)) == 1
    )
    sheet.segs.append((x0, y0, (x0 + x1) / 2.0, (y0 + y1) / 2.0, net))
    vec = score(sheet, parts, rep, paper=rep["readability"]["paper"], region_boxes=rep["_boxes"])
    # One overlap, and two dots where only two ways meet: on the pin end, and where the copy ends mid-wire.
    assert vec["wire_overlaps"] == 3 and vec["hard"] == 3, vec["detail"]
    facts = vec["detail"]["wire_overlaps"]
    assert sum(f.startswith(f"wire {net} (") and " runs along wire " in f for f in facts) == 1, facts
    assert any(f.startswith("junction dot at (") and f.endswith(f"where 2 ways meet (the end of {pin})") for f in facts), facts
    assert any(f == f"junction dot at ({(x0 + x1) / 2:g}, {(y0 + y1) / 2:g}) where 2 ways meet" for f in facts), facts


def test_the_router_refuses_a_wire_along_one_of_its_own_net_and_a_stub_starts_where_that_wire_turns():
    from pcbc.sch_emit import _Sheet, _trim_own

    sheet = _Sheet([])
    sheet.add([(1000.0, 1000.0), (1005.08, 1000.0), (1005.08, 1010.16)], "GND")  # a bus bar leaving a pin end, turning down
    assert not sheet.segment_ok(1000.0, 1000.0, 1002.54, 1000.0, "GND"), "along its own net's wire"
    assert sheet.segment_ok(1005.08, 1000.0, 1010.16, 1000.0, "GND"), "on from the bar's corner: touching end to end"
    assert _trim_own(sheet, [(1000.0, 1000.0), (1010.16, 1000.0)], "GND") == [(1005.08, 1000.0), (1010.16, 1000.0)]
    assert _trim_own(sheet, [(1000.0, 1000.0), (1002.54, 1000.0)], "GND") == [(1002.54, 1000.0)], "all of it on the bar"
    assert _trim_own(sheet, [(1000.0, 1000.0), (1010.16, 1000.0)], "VCC") == [(1000.0, 1000.0), (1010.16, 1000.0)]
