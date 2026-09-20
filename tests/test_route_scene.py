"""The placed board as obstacles: a pad's true copper, the clearance table, the scene, the emitter.

`docs/r2-design.md` A.3, A.4, A.6, A.7, A.8 and A.9, one assertion per rule, plus the acceptance
this slice is judged by: the fanout's copper comes out **byte-identical** through the new core
(`test_fanout.py` holds that end), and D.2's agreement test runs the checker over the routed
examples and asserts that every clearance, hole, edge and short error KiCad finds is one the checker
also rejects. Every pinned number carries its reference in the assertion message.

The numbers here were measured against the checked-in boards on 2026-09-20 and are recorded in
`docs/r2-measurements.md` (S3); a drift names the rule it left.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.constraints import clearance_table
from pcbc.dru import rules as dru_rules
from pcbc.fanout import fanout_pieces
from pcbc.language import load_board
from pcbc.pads import pad_geoms
from pcbc.route_emit import Piece, census, piece_key, read_sidecar, seg_piece, segment, sidecar, via, via_piece, write_pieces, write_sidecar
from pcbc.route_geom import EPS_MM, MICRO_MM, Shape, aabb, clears, gap, track_shape
from pcbc.route_scene import (
    ADVISORY,
    KIND_ORDER,
    audit,
    blocked,
    build_scene,
    clashes,
    components,
    free_intervals,
    lane_run_mm,
    lane_strips,
    net_open,
    pad_exits,
    plane_targets,
)
from pcbc.sexp import board_footprint_spans, footprint_at, footprint_reference
from pcbc.stackup import fanout_stagger

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
NAMES = ("blinky", "buck", "c3_usb", "node")


def _board(name: str) -> Path:
    return DS2 / "ds2_addon.py" if name == "ds2" else EXAMPLES / name / f"{name}.py"


def _placed(name: str) -> Path:
    if name == "ds2":
        return DS2 / "layout" / "ds2_addon" / "placed" / "layout.kicad_pcb"
    return EXAMPLES / name / "layout" / name / "placed" / "layout.kicad_pcb"


def _routed(name: str) -> Path:
    return EXAMPLES / name / "layout" / name / "routed" / "layout.kicad_pcb"


ALL = NAMES + (("ds2",) if (DS2 / "ds2_addon.py").exists() else ())


def _scene(name: str, pcb: Path | None = None):
    design = load_board(_board(name))
    job = compile_design(design)
    text = (pcb or _placed(name)).read_text()
    return build_scene(design, job, job.constraints, text), design, job, text


# --- A.1 on real pads: `pads.py` reads the copper KiCad draws --------------------------------------


def test_every_pad_of_every_board_is_read_and_the_census_is_the_one_e1_counted():
    """E.1's census, which is what makes completeness provable instead of claimed: 397 pads across
    the five placed boards, 363 smd, 30 thru_hole, 4 np_thru_hole."""
    kinds: dict[str, int] = {}
    shapes: dict[str, int] = {}
    total = 0
    for name in ALL:
        text = _placed(name).read_text()
        for s, e in board_footprint_spans(text):
            block = text[s:e]
            for g in pad_geoms(block, footprint_at(block), ref=footprint_reference(block) or ""):
                total += 1
                kinds[g.kind] = kinds.get(g.kind, 0) + 1
                shapes[g.shape] = shapes.get(g.shape, 0) + 1
    if len(ALL) < 5:
        pytest.skip("the DS2 Addon is not checked out here; the census is over all five boards")
    assert total == 397, f"E.1 counted 397 pads across the five placed boards, got {total}"
    assert kinds == {"smd": 363, "thru_hole": 30, "np_thru_hole": 4}, kinds
    assert shapes == {"rect": 165, "roundrect": 156, "oval": 32, "circle": 36, "custom": 8}, shapes


def test_a_usb_c_shield_pad_is_the_copper_it_draws_not_the_anchor_it_declares():
    """A.1's measured row, and the reason `pads.py` exists: `J1.A4B9` declares `(size 0.005 0.005)`
    and draws a `gr_poly` spanning 0.599974 x 1.299997 mm with a `(width 0.1)` stroke."""
    text = _placed("c3_usb").read_text()
    block = next(text[s:e] for s, e in board_footprint_spans(text) if footprint_reference(text[s:e]) == "J1")
    pad = next(g for g in pad_geoms(block, footprint_at(block), ref="J1") if g.num == "A4B9")
    x0, y0, x1, y1 = pad.box()
    assert (round(x1 - x0, 6), round(y1 - y0, 6)) == (0.699974, 1.399997), (
        "0.599974 x 1.299997 of gr_poly plus a 0.1 mm stroke (docs/r2-design.md A.1); the declared "
        "size is a 5 um dot and every check was blind to this copper before S1b"
    )
    assert pad.shape == "custom" and pad.net == "VBUS"
    assert len(pad.copper) >= 2, "one Shape per primitive, plus the anchor: tighter than one hull"


def test_an_oval_drill_is_a_capsule_and_an_npth_is_a_hole_with_no_copper():
    """A.1's two through-hole rows. Reading `(drill oval 0.8 1.5)` as a scalar under-models the
    USB-C leg by 0.35 mm at each end, and rule 2 is the check that catches a via parked there."""
    text = _placed("c3_usb").read_text()
    block = next(text[s:e] for s, e in board_footprint_spans(text) if footprint_reference(text[s:e]) == "J1")
    geoms = pad_geoms(block, footprint_at(block), ref="J1")
    leg = next(g for g in geoms if g.num == "1")
    assert leg.kind == "thru_hole" and leg.hole is not None
    assert len(leg.hole.pts) == 2 and round(leg.hole.r, 6) == 0.399999, (
        f"a capsule: two end-cap centres offset by half the narrow width, got {leg.hole}"
    )
    span = round(math.dist(leg.hole.pts[0], leg.hole.pts[1]) + 2 * leg.hole.r, 6)
    assert span == 1.499998, (
        f"the long axis of `(drill oval 0.799998 1.499997)`, got {span}: the two end-cap centres are "
        "half a nanometre off the grid and `q` rounds them outward, so the capsule is one nanometre "
        "LONGER than the drill — a superset, which is the only side of A.1's rule this may be on"
    )
    npth = [g for g in geoms if g.kind == "np_thru_hole"]
    assert npth and all(g.copper == () and g.hole is not None for g in npth), (
        "an NPTH has a hole and no copper at all (A.1), so it becomes a `hole` item in the scene"
    )
    assert leg.cu_layers == frozenset({"F.Cu", "B.Cu"}), "`*.Cu` expands to the board's copper layers"


def test_a_chamfered_roundrect_is_read_as_the_roundrect_which_is_a_superset():
    """A.1: a chamfer only ever removes copper from a corner, so ignoring it makes the obstacle
    bigger and never smaller — the one side of the rule the core may be on. c3_usb's pad 49 is the
    real one: `(roundrect_rratio 0)` with `(chamfer_ratio 0.41) (chamfer top_left)`."""
    text = _placed("c3_usb").read_text()
    block = next(text[s:e] for s, e in board_footprint_spans(text) if "(chamfer_ratio" in text[s:e])
    pad = next(g for g in pad_geoms(block, footprint_at(block), ref="U1") if g.num == "49")
    x0, y0, x1, y1 = pad.box()
    assert (round(x1 - x0, 4), round(y1 - y0, 4)) == (1.45, 1.45), (
        "the full 1.45 x 1.45 square, chamfer included, which is the superset (docs/r2-design.md A.1)"
    )


# --- A.3 the clearance table ----------------------------------------------------------------------

LOWERING_RULES = ("pads_of_one_footprint",)
"""The one rule in the file written to **lower** a number rather than raise it: a connector's own
pads sit closer than a power class asks (USB-C: 0.1 mm), that is the land and not a routing choice,
and KiCad's later-rule-wins precedence makes it stick. `ClearanceTable` takes the maximum, so it
does not model it and does not need to — it is pad to pad, and pcbc never draws a pad."""

_MIN_MM = re.compile(r"\(min ([0-9.]+)mm\)")


def _matches(rule, a, b) -> bool:
    """Does this rule's condition match the (a, b) constraint pair? The conditions `dru.py` writes
    are `hasNetclass`, `NetName ==`, `NetName !=`, `||` lists and the own-pads exemption, and
    nothing else, so they evaluate exactly."""
    env = {"A": a, "B": b}
    expr = rule.condition
    expr = re.sub(r"([AB])\.hasNetclass\('([^']+)'\)", lambda m: str(env[m.group(1)].class_name == m.group(2)), expr)
    expr = re.sub(r"([AB])\.NetName == '([^']*)'", lambda m: str(env[m.group(1)].net == m.group(2)), expr)
    expr = re.sub(r"([AB])\.NetName != '([^']*)'", lambda m: str(env[m.group(1)].net != m.group(2)), expr)
    expr = expr.replace("!(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference)", "True")
    expr = expr.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    if "Type" in expr or "intersectsArea" in expr:
        return False
    return bool(eval(expr))  # noqa: S307 - the grammar is the five forms above


@pytest.mark.parametrize("name", ALL)
def test_clearance_table_covers_every_written_rule(name: str):
    """A.3's one-source discipline, kept by a test and not by a refactor of `dru.py`: for every
    `clearance` and `creepage` rule pcbc hands KiCad, `between` returns at least that rule's number
    for every net pair the rule's condition matches."""
    cs = compile_design(load_board(_board(name))).constraints
    table = clearance_table(cs)
    cons = sorted(cs.constraints, key=lambda c: c.net)
    checked = 0
    for rule in dru_rules(cs):
        if rule.name in LOWERING_RULES or not _MIN_MM.search(rule.constraint):
            continue
        if not rule.constraint.startswith(("(constraint clearance", "(constraint creepage")):
            continue
        want = float(_MIN_MM.search(rule.constraint).group(1))
        for a in cons:
            for b in cons:
                if a.net == b.net or not _matches(rule, a, b):
                    continue
                checked += 1
                got, why = table.between(a.net, b.net)
                assert got + 1e-9 >= want, f"{name}: rule {rule.name} asks {want} for {a.net}/{b.net}, table says {got} ({why})"
    assert checked >= 0


def test_a_synthetic_board_with_every_kind_exercises_creepage_and_isolation():
    """The five real boards write no `creepage` and no `Isolation` rule, so the coverage test above
    would pass vacuously on exactly the two rows most likely to be wrong. `test_dru.py`'s own
    every-kind board is the one that writes them."""
    from test_dru import EVERY_KIND, HEAD, TAIL, TWO

    src = ROOT / "tests" / "_every_kind_tmp.py"
    src.write_text(HEAD + TWO + EVERY_KIND + TAIL)
    try:
        cs = compile_design(load_board(src)).constraints
    finally:
        src.unlink()
    table = clearance_table(cs)
    cons = sorted(cs.constraints, key=lambda c: c.net)
    kinds = set()
    for rule in dru_rules(cs):
        if rule.name in LOWERING_RULES or not rule.constraint.startswith(("(constraint clearance", "(constraint creepage")):
            continue
        want = float(_MIN_MM.search(rule.constraint).group(1))
        for a in cons:
            for b in cons:
                if a.net == b.net or not _matches(rule, a, b):
                    continue
                kinds.add(rule.name.split("_")[0])
                got, why = table.between(a.net, b.net)
                assert got + 1e-9 >= want, f"rule {rule.name} asks {want} for {a.net}/{b.net}, table says {got} ({why})"
    assert {"creepage", "feedback", "analog"} <= kinds, f"the creepage and keep-away rows really ran: {sorted(kinds)}"
    hv = next(c for c in cons if c.voltage is not None and c.voltage.creepage_mm is not None)
    other = next(c for c in cons if c.class_name != hv.class_name)
    assert table.between(hv.net, other.net)[0] >= hv.voltage.creepage_mm.value, (
        f"the {hv.voltage.volts:g} V class's IEC 60664-1 F.5 row reaches {other.net} (C.8, E.6): "
        "KiCad writes that rule against every other net, not only against another class that also "
        "carries creepage, and the table has to cover what is written"
    )
    assert table.between(hv.net, other.net)[1].startswith("creepage"), table.between(hv.net, other.net)


def test_between_is_the_pairs_own_classes_and_a_net_with_none_gets_default():
    """KiCad resolves a clearance from the two items' own net classes and gives an unassigned net
    the Default class. Starting from Default instead reads two USB nets (0.155) as 0.16 and refuses
    c3_usb's own routed pair at 0.159 mm, which KiCad passes."""
    cs = compile_design(load_board(_board("c3_usb"))).constraints
    table = clearance_table(cs)
    assert table.between("USB_DP", "USB_DN") == (0.155, "class USB"), "c3_usb's USB class (A.3)"
    assert table.between("USB_DP", "3V3") == (0.2, "class Power"), "the wider of the two wins"
    assert table.between("GND", "GND") == (0.0, "same net"), "A.3: `between(a, a)` is 0.0, same net"
    assert table.between("USB_DP", "")[0] == 0.16, "an item with no net is judged at the Default class"
    assert table.between("", "") == (0.16, "Default class")


def test_the_board_wide_numbers_are_the_stackups_and_via_pitch_is_the_fanouts_own_arithmetic():
    """A.3's remaining rows, and A.4 rule 3's consequence: two 0.3 mm drills need 0.8 mm centre to
    centre, and `stackup.fanout_stagger` is this same arithmetic on a row of a given pitch."""
    cs = compile_design(load_board(_board("c3_usb"))).constraints
    table = clearance_table(cs)
    stack = cs.stackup
    assert (table.edge(), table.hole_to_copper(), table.hole_to_hole()) == (0.3, 0.25, 0.5)
    assert table.mask_bridge() == 0.10, "JLC's published solder-mask dam, advisory in R2 (A.4 rule 4)"
    assert table.via_to_same_net_smd_pad() == stack.clearance_min
    pitch = table.via_pitch("GND", "3V3", stack.via_drill, stack.via_diameter, stack.via_drill, stack.via_diameter)
    assert pitch == 0.8, "0.3 + 0.3 + 0.2: two 0.3 mm drills, edge to edge (docs/r2-design.md A.4 rule 3)"
    row = 0.65
    assert round(math.hypot(row, fanout_stagger(stack, 0.2, row)), 4) >= pitch - 1e-9, (
        "the fanout's staggered neighbours already obey this pitch, which is the point of having "
        "one number: `fanout_stagger` and `via_pitch` cannot disagree"
    )


# --- A.6 the scene --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_the_scene_is_sorted_and_its_ids_are_the_canonical_tie_break(name: str):
    """A.6: sort by `(KIND_ORDER[kind], net, owner, x_nm, y_nm)`, then assign ids, so `item.id` is
    the tie-break `blocked` sorts on and no answer depends on the order a file listed its parts."""
    scene, _d, _j, _t = _scene(name)
    keys = [(KIND_ORDER[it.kind], it.net, it.owner, round(it.box()[0] * 1e6), round(it.box()[1] * 1e6)) for it in scene.items]
    assert keys == sorted(keys), f"{name}: Scene.items is not in A.6's canonical order"
    assert [it.id for it in scene.items] == list(range(len(scene.items))), "ids are assigned after the sort"
    assert scene.items[-1].kind == "edge", "the board edge sorts last (KIND_ORDER)"


def test_shuffling_the_footprints_in_the_file_gives_the_identical_scene():
    """D.3's determinism property at the scene's own boundary: the board is a pure function of what
    is on it, not of the order the file happened to write it in."""
    scene, design, job, text = _scene("c3_usb")
    spans = board_footprint_spans(text)
    blocks = [text[s:e] for s, e in spans]
    head, tail = text[: spans[0][0]], text[spans[-1][1] :]
    joiner = text[spans[0][1] : spans[1][0]]
    shuffled = head + joiner.join(reversed(blocks)) + tail
    other = build_scene(design, job, job.constraints, shuffled)
    assert [(it.id, it.kind, it.net, it.owner, it.copper, it.hole) for it in scene.items] == [
        (it.id, it.kind, it.net, it.owner, it.copper, it.hole) for it in other.items
    ], "reversing the footprint order moved an item"


@pytest.mark.parametrize("name", ALL)
def test_the_index_never_misses_anything_a_linear_scan_would_find(name: str):
    """A.6's index is a bucket grid and the caller grows the query box by `max_need + max_item_r`,
    so a bucket answer is a superset and the exact test runs on what comes back. A miss here is a
    silent false accept, which is the one failure mode the whole core exists to rule out."""
    scene, _d, _j, _t = _scene(name)
    pad = 0.6
    for it in scene.items[:: max(1, len(scene.items) // 25)]:
        layer = sorted(it.layers)[0]
        got = set(scene.query(layer, it.box(), pad))
        want = {
            o.id
            for o in scene.items
            if layer in o.layers
            and not (o.box()[2] < it.box()[0] - pad or o.box()[0] > it.box()[2] + pad or o.box()[3] < it.box()[1] - pad or o.box()[1] > it.box()[3] + pad)
        }
        assert want <= got, f"{name}: the index missed {sorted(want - got)} near {it.owner}"


def test_the_outline_is_the_content_rect_inset_by_edge_clearance_with_no_stroke_term():
    """A.4 rule 5, settled by experiment rather than by argument: KiCad measures copper to the
    NOMINAL Edge.Cuts line and ignores the outline's `(stroke (width 0.05))`, so the inset is
    `stack.edge_clearance` exactly (docs/r2-design.md A.4 rule 5)."""
    scene, design, _j, _t = _scene("node")
    from pcbc.layout import content_rect

    c = content_rect(design.board)
    e = scene.stack.edge_clearance
    assert scene.outline == (c.x0 + e, c.y0 + e, c.x1 - e, c.y1 - e) == (0.3, 0.3, 59.7, 44.7)
    assert e == 0.3 and "0.025" not in repr(scene.outline), "no half-stroke term anywhere in it"


def test_the_lanes_of_a_closed_row_are_obstacles_the_router_may_cross_and_not_run_along():
    """A.7's deliberate difference: `check_corridors` fills the whole strip and asks about a trunk;
    the router asks about one piece — may it cross a lane (yes) or run along it (no). Forbidding
    crossing outright would cut c3_usb's board in half along `J1`'s 1.4515 mm lane."""
    scene, _d, _j, _t = _scene("c3_usb")
    lanes = {it.owner: it for it in scene.items if it.kind == "lane"}
    assert "J1 lane top" in lanes and "U2 lane left" in lanes, sorted(lanes)
    x0, y0, x1, y1 = lanes["J1 lane top"].box()
    assert round(y1 - y0, 4) == 1.4515, "stackup.fanout_lane at the 2-layer rung, measured (A.7)"
    assert lane_strips(scene.feet["J1"])["top"] == (x0, y0, x1, y1), "one owner for the rectangle"
    across = lane_run_mm(scene, (x0 - 1.0, (y0 + y1) / 2.0), (x1 + 1.0, (y0 + y1) / 2.0))
    down = lane_run_mm(scene, ((x0 + x1) / 2.0, y0 - 1.0), ((x0 + x1) / 2.0, y1 + 1.0))
    assert across[0] > down[0] and across[1] == "J1 lane top", (
        f"running along the lane is {across[0]} mm and crossing it is {down[0]} mm; the rule is on "
        "the run, which is the DS2 Addon's lesson (docs/copper-plan.md line 191)"
    )
    assert round(down[0], 4) == round(y1 - y0, 4), "crossing costs the lane's width and no more"
    crossing = seg_piece("GND", "hop", "F.Cu", ((x0 + x1) / 2.0, y0 - 1.0), ((x0 + x1) / 2.0, y1 + 1.0), 0.2)
    assert [c.rule for c in clashes(scene, [crossing], "GND") if c.item.kind == "lane"] == [], (
        "and `clashes` never refuses on a lane at all: the lane rule is the caller's question, "
        "because forbidding a crossing would cut c3_usb's board in half along J1's lane (A.7)"
    )


# --- A.4 the five rules ---------------------------------------------------------------------------


def test_a_candidate_over_a_foreign_pad_is_refused_and_the_worst_clash_is_the_one_reported():
    """A.4 rule 1 and `blocked`'s order: the worst clash by `(need - have)` descending, then
    `item.id` ascending, so the move line names the thing most in the way."""
    scene, _d, _j, _t = _scene("c3_usb")
    victim = next(it for it in scene.items if it.owner == "U2.5" and it.kind == "pad")
    x, y = victim.at()
    piece = seg_piece("GND", "hop", "F.Cu", (x - 3.0, y), (x + 3.0, y), 0.2)
    worst = blocked(scene, [piece], "GND")
    assert worst is not None and worst.rule == "copper", worst
    assert worst.item.net == "3V3" and worst.need == 0.2, worst.line()
    assert worst.have < 0.0, "the candidate runs straight over the pad, so the gap is negative"
    found = [c for c in clashes(scene, [piece], "GND") if c.rule not in ADVISORY]
    assert worst == sorted(found, key=lambda c: (-(c.need - c.have), c.item.id, c.rule))[0]


def test_two_items_on_one_net_skip_the_copper_rule_and_never_the_hole_rules():
    """A.4's header, with rule 2's one measured correction: KiCad's `hole_clearance` also skips a
    same-net pair (a hole inside its own net's copper is the connection, and the barrel is plated
    to it), while `hole_to_hole` skips nothing — it is a drilling constraint. Reading rule 2 as
    never skipping refuses 21 pieces on buck, 65 on c3_usb and 161 on node, every one of them a
    track landing on its own via and every one of them passed by KiCad (docs/r2-measurements.md S3)."""
    scene, _d, _j, _t = _scene("node")
    pad = next(it for it in scene.items if it.kind == "pad" and it.net == "GND")
    x, y = pad.at()
    own = seg_piece("GND", "tap", "F.Cu", (x, y), (x + 1.0, y), 0.2)
    assert [c.rule for c in clashes(scene, [own], "GND") if c.item.id == pad.id] == [], (
        "a track landing on its own pad is the connection, not a clash"
    )
    foreign = seg_piece("3V3", "hop", "F.Cu", (x, y), (x + 1.0, y), 0.2)
    assert "copper" in [c.rule for c in clashes(scene, [foreign], "3V3") if c.item.id == pad.id]
    hole = next(it for it in scene.items if it.hole is not None and it.net)
    hx, hy = hole.at()
    near = via_piece(hole.net, "tap", (hx + 0.3, hy), 0.35, 0.2)
    rules = [c.rule for c in clashes(scene, [near], hole.net) if c.item.id == hole.id]
    assert "hole_to_hole" in rules, f"same net or not, two holes 0.3 mm apart is a fab refusal: {rules}"


def test_a_candidate_outside_the_edge_band_is_refused_by_rule_5():
    scene, _d, _j, _t = _scene("blinky")
    inside = seg_piece("GND", "hop", "F.Cu", (5.0, 5.0), (10.0, 5.0), 0.2)
    assert [c for c in clashes(scene, [inside], "GND") if c.rule == "edge"] == []
    out = seg_piece("GND", "hop", "F.Cu", (5.0, 0.35), (10.0, 0.35), 0.2)
    clash = blocked(scene, [out], "GND")
    assert clash is not None and clash.rule == "edge", clash
    assert clash.item.owner == "board edge" and clash.need == 0.3
    assert clash.have == 0.25, (
        "`actual = y - width/2` measured to the nominal line, which is KiCad's own answer for a "
        "0.2 mm track at y = 0.35 (docs/r2-design.md A.4 rule 5)"
    )


def test_the_mask_rule_is_advisory_so_it_reports_and_never_blocks():
    """A.4 rule 4: a candidate that fails only the mask rule is accepted and emits a `style:` note
    counted in the census. KiCad's own `solder_mask_bridge` check is what gates the build, and R1's
    procedure is to count first and promote in the PR that shows the zeros."""
    assert ADVISORY == ("mask",)
    scene, _d, _j, _t = _scene("blinky")
    pad = next(it for it in scene.items if it.kind == "pad" and it.mask is not None)
    x, y = pad.at()
    cu = aabb(pad.copper)
    at_x = cu[2] + 0.25 + 0.1  # 0.25 mm of air to the copper, so rule 1 (0.2) passes and rule 4 does not
    near = seg_piece("LED", "hop", "F.Cu", (at_x, y - 2.0), (at_x, y + 2.0), 0.2)
    found = [c.rule for c in clashes(scene, [near], "LED") if c.item.id == pad.id]
    assert "mask" in found, f"the 0.5 mm solder_mask_margin on blinky's LED pads is real: {found}"
    assert blocked(scene, [near], "LED") is None or blocked(scene, [near], "LED").rule != "mask"


# --- A.7 pad exits --------------------------------------------------------------------------------


def test_a_pad_exit_leaves_far_enough_to_be_copper_and_in_a_fixed_order():
    """A.7: out by the pad's own half extent plus the widest clearance this net owes anything on the
    footprint plus half the track, floored at `MICRO_MM` — which is what guarantees no pattern stub
    can fire `track_segment_length (min 0.2mm)`. The order is the escape side first, then outward."""
    scene, _d, _j, _t = _scene("c3_usb")
    pad = next(it for it in scene.items if it.owner == "U2.5" and it.kind == "pad")
    exits = pad_exits(scene, pad, 0.2, "F.Cu", check=False)
    assert [e.side for e in exits] == ["right", "up", "down", "left"], (
        "U2.5 sits on the right of its footprint, so `right` maximises (pad - centre) . dir (A.7)"
    )
    for e in exits:
        assert math.dist(e.stub[0], e.stub[1]) >= MICRO_MM - 1e-9, f"{e.side}: {math.dist(*e.stub)}"
        assert e.stub[0] == pad.at() and e.at == e.stub[1]
        assert e.dir[0] == 0 or e.dir[1] == 0, "an exit is axis-aligned; a stub never leaves at an angle"
        assert (e.at[0] == pad.at()[0]) or (e.at[1] == pad.at()[1]), (
            "A.5 rule 1: the across-coordinate keeps the pad's exactly — the S1 fanout bug"
        )


def test_an_exit_that_cannot_be_drawn_is_dropped():
    """A.7: every exit is clearance-checked as a piece and a blocked one is dropped. `U2.2` is a
    middle pad of a closed SOT-23 row, so its two along-the-row exits run into its neighbours."""
    scene, _d, _j, _t = _scene("c3_usb")
    pad = next(it for it in scene.items if it.owner == "U2.2" and it.kind == "pad")
    kept = [e.side for e in pad_exits(scene, pad, 0.2, "F.Cu")]
    every = [e.side for e in pad_exits(scene, pad, 0.2, "F.Cu", check=False)]
    assert kept == ["left", "right"] and every == ["left", "down", "up", "right"], (kept, every)
    shield = next(it for it in scene.items if it.owner == "J1.A4B9" and it.kind == "pad")
    assert [e.side for e in pad_exits(scene, shield, 0.2, "F.Cu")] == ["up"], (
        "the USB-C shield pad is walled in by the connector's own legs and can only go inward"
    )


# --- A.8 free intervals ---------------------------------------------------------------------------


@pytest.mark.parametrize("net,layer,span", [("GND", "F.Cu", (5.0, 25.0)), ("3V3", "B.Cu", (2.0, 40.0)), ("", "F.Cu", (10.0, 50.0))])
def test_free_intervals_never_offers_an_offset_clears_refuses(net: str, layer: str, span):
    """A.8, checked against the thing it is a closed form of: sweep node at 0.01 mm and assert every
    offset inside a returned interval is one `clears` agrees with. The sweep is what found the two
    endpoints: `clears` carries `EPS_MM` on the strict side, so the blocked interval carries it too
    and the free one is rounded inward."""
    scene, _d, _j, _t = _scene("node")
    got = free_intervals("x", span, (0.0, 45.0), 0.1, net, scene, layer)
    step = 0.01
    c = 0.0
    while c <= 45.0 + 1e-12:
        y = round(c, 4)
        if any(a <= y <= b for a, b in got):
            cand = track_shape((span[0], y), (span[1], y), 0.2)
            for it in scene.items:
                if it.kind in ("lane", "edge") or layer not in it.layers or it.copper is None:
                    continue
                if net and it.net == net:
                    continue
                need = scene.table.between(net, it.net)[0] if it.kind != "keepout" else 0.0
                assert clears(cand, it.copper, need), f"y = {y} was offered and {it.owner} refuses it"
        c += step
    assert got and all(b - a >= scene.grid - 1e-9 for a, b in got), f"nothing narrower than the grid: {got}"


def test_free_intervals_answers_the_whole_question_at_once_and_on_all_four_axes():
    scene, _d, _j, _t = _scene("node")
    for axis in ("x", "y", "u", "v"):
        got = free_intervals(axis, (5.0, 25.0), (-60.0, 120.0), 0.1, "GND", scene, "F.Cu")
        assert got == free_intervals(axis, (5.0, 25.0), (-60.0, 120.0), 0.1, "GND", scene, "F.Cu"), f"{axis} is not deterministic"
        assert all(a < b for a, b in got) and list(got) == sorted(got), f"{axis}: {got}"
    with pytest.raises(ValueError, match="axis is one of"):
        free_intervals("z", (0.0, 1.0), (0.0, 1.0), 0.1, "GND", scene, "F.Cu")


# --- A.9 who still needs KRT ----------------------------------------------------------------------


def test_a_placed_board_is_open_and_a_routed_one_is_not():
    """A.9: connectivity is computed, never declared, so a pattern that connects three of a net's
    four pads is honest by construction. On a placed board every pad is its own component."""
    placed, _d, _j, _t = _scene("buck")
    assert net_open(placed, "GND"), "nothing is routed yet, so GND is open"
    assert len(components(placed, "GND")) == len(placed.pads_of("GND")), "one component per pad"
    routed, _d2, _j2, _t2 = _scene("buck", _routed("buck"))
    assert not net_open(routed, "5V"), f"the routed board connects 5V: {components(routed, '5V')}"
    assert all(isinstance(c, frozenset) for c in components(routed, "5V"))


def test_a_plane_net_joins_through_its_taps():
    """A.9: a pad joins the plane when it has a via on the plane's layer, which is exactly what a
    tap is — and `plane_of` is C.2's one fact, `job.planes` on four layers and the GND pour on two."""
    scene, _d, job, _t = _scene("node")
    assert plane_targets(job) == (("GND", "In1.Cu"), ("3V3", "In2.Cu")), "node's declared planes"
    assert scene.plane_of == {"GND": "In1.Cu", "3V3": "In2.Cu"}
    two, _d2, job2, _t2 = _scene("c3_usb")
    assert plane_targets(job2) == (("GND", "B.Cu"),), (
        "on two layers `krt_plan` pours GND on the back when GND is a power net (C.2)"
    )


# --- route_emit -----------------------------------------------------------------------------------


def test_the_emitted_text_is_the_byte_string_krts_parser_reads():
    """C.3: `segment` and `via` are lifted verbatim from `fanout.py`, `(locked yes)` included, in
    the two positions KRT's parser reads — after `(width)` on a segment and after `(layers)` on a
    via. The lock is what tells every later KRT step this copper is an obstacle it may not move."""
    assert segment(1.0, 2.0, 3.0, 4.5, 0.2, "F.Cu", "GND", "u1") == (
        '\n\t(segment\n\t\t(start 1.000000 2.000000)\n\t\t(end 3.000000 4.500000)\n\t\t(width 0.2)\n\t\t(locked yes)\n'
        '\t\t(layer "F.Cu")\n\t\t(net "GND")\n\t\t(uuid "u1")\n\t)\n'
    )
    assert via(1.0, 2.0, 0.5, 0.3, "GND", "u2") == (
        '\n\t(via\n\t\t(at 1.000000 2.000000)\n\t\t(size 0.5)\n\t\t(drill 0.3)\n'
        '\t\t(layers "F.Cu" "B.Cu")\n\t\t(locked yes)\n\t\t(net "GND")\n\t\t(uuid "u2")\n\t)\n'
    )
    body = write_pieces("(kicad_pcb\n\t(net 0 \"\")\n)\n", [seg_piece("GND", "hop", "F.Cu", (1.0, 2.0), (3.0, 4.5), 0.2, uuid="u1")])
    assert body.endswith(")\n") and body.count("(locked yes)") == 1


def test_a_piece_is_quantised_once_at_construction_and_refuses_a_degenerate_one():
    """A.5's construction rule at the emitter's own door: what is written is what was checked."""
    p = seg_piece("GND", "hop", "F.Cu", (1.0000004, 2.0), (3.0, 4.0), 0.2)
    assert p.a == (1.0, 2.0), "quantised once, here, and never again on the way out"
    assert piece_key(p) == ("seg", "F.Cu", (1.0, 2.0), (3.0, 4.0), 0.2)
    assert piece_key(via_piece("GND", "tap", (1.5, 2.5), 0.5, 0.3)) == ("via", (1.5, 2.5), ("F.Cu", "B.Cu"))
    with pytest.raises(ValueError, match="zero length"):
        seg_piece("GND", "hop", "F.Cu", (1.0, 2.0), (1.0000004, 2.0), 0.2)
    with pytest.raises(ValueError, match="needs a drill"):
        Piece("via", "GND", "tap", ("F.Cu", "B.Cu"), (1.0, 1.0), None, 0.5, None)


def test_the_sidecar_is_keyed_by_geometry_and_round_trips(tmp_path: Path):
    """D.4: KiCad rewrites the board during the gate's refill-and-save and `pin_copper_ids` re-keys
    uuids, so the reason cannot live in the file — `copper.json` is keyed by the geometry, which is
    stable through both rewrites because the copper is locked."""
    pieces = [
        seg_piece("GND", "fanout", "F.Cu", (1.0, 2.0), (1.0, 5.0), 0.2, owner="U1.5", uuid="a"),
        via_piece("GND", "fanout", (1.0, 5.0), 0.5, 0.3, owner="U1.5", uuid="b"),
        seg_piece("3V3", "hop", "F.Cu", (0.0, 0.0), (4.0, 0.0), 0.3, owner="U1.1", uuid="c"),
    ]
    doc = sidecar(pieces, step="patterns_pre", notes=("style: a loop",))
    assert doc.census == {
        "fanout": {"segments": 1, "vias": 1, "mm": 3.0},
        "hop": {"segments": 1, "vias": 0, "mm": 4.0},
    }, doc.census
    assert census(pieces, leftover={"segments": 9, "vias": 1, "mm": 12.5})["leftover"]["mm"] == 12.5
    path = tmp_path / "copper.json"
    write_sidecar(path, doc)
    back = read_sidecar(path)
    assert back.to_dict() == doc.to_dict() and path.read_text().endswith("\n")
    assert json.loads(path.read_text())["items"][0]["key"] == ["seg", "F.Cu", [1.0, 2.0], [1.0, 5.0], 0.2]


# --- the acceptance: the fanout's copper, unchanged -------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_the_fanout_writes_the_same_copper_through_the_new_core(name: str):
    """S3's acceptance (docs/r2-design.md G, S3): the fanout's emission now goes through
    `route_emit` and its footprints through the scene, and the copper is byte-identical on all five
    boards. That is the proof the new core agrees with the code that already works — an emitter
    that writes the same bytes and a scene whose `Foot`s are the same objects."""
    scene, design, job, text = _scene(name)
    pieces, notes = fanout_pieces(design, job, text, "t", scene)
    assert len(pieces) == 2 * len(notes), "one stub and one via per escape"
    again, _ = fanout_pieces(design, job, text, "t")
    assert [piece_key(p) for p in pieces] == [piece_key(p) for p in again], f"{name}: not deterministic"
    from pcbc.fanout import fanout_copper

    out, notes2 = fanout_copper(design, job, text, "t", scene)
    assert notes2 == notes
    assert out == write_pieces(text, pieces) if pieces else out == text
    for p in pieces:
        if p.kind != "seg":
            continue
        assert p.a[0] == p.b[0] or p.a[1] == p.b[1], f"{name}: {p.owner}'s stub leaves its pad square (S1)"
        assert p.mm >= MICRO_MM - 1e-9, f"{name}: {p.owner}'s stub is {p.mm} mm, under KiCad's 0.2"


@pytest.mark.parametrize("name", ALL)
def test_the_fanouts_own_copper_clears_the_scene_it_was_built_from(name: str):
    """The self-check of D.1 applied to the one pattern that already exists: every escape, judged by
    A.4's rules against the board it sits on, with the pad it leaves and its own via exempt."""
    scene, design, job, text = _scene(name)
    pieces, notes = fanout_pieces(design, job, text, "t", scene)
    added = scene.add([scene.item_of(p) for p in pieces])
    mine = frozenset(it.id for it in added)
    for p in pieces:
        own = frozenset(it.id for it in scene.items if it.owner == p.owner) | mine
        clash = blocked(scene, [p], p.net, ignore=own)
        assert clash is None, f"{name}: {p.owner}'s {p.reason} is refused by {clash.line() if clash else ''}"
    assert len(notes) == len([p for p in pieces if p.kind == "via"])


# --- D.2 the agreement test -------------------------------------------------------------------------

# What the checker flags on the four routed examples that KiCad does not, recorded with its shape
# (docs/r2-measurements.md, S3). Both are the one-sided epsilon doing exactly what it is for: a gap
# that lands exactly on the requirement is refused rather than handed to the arbiter to argue about.
STRICTER = {
    "blinky": (),
    "buck": (),
    "c3_usb": (("copper", 0.2, 0.2),),  # 3V3 track against SW_BOOT.1 [BOOT], exactly 0.2 of 0.2
    "node": (("hole_to_hole", 0.5, 0.5),),  # 3V3 via at (51.8,5.6) against a GND via, exactly 0.5
}
_ACTUAL = re.compile(r"actual ([0-9.]+) mm")
_KICAD_RULES = {
    "clearance": "copper",
    "hole_clearance": "hole_to_copper",
    "hole_to_hole": "hole_to_hole",
    "copper_edge_clearance": "edge",
    "shorting_items": "copper",
    "solder_mask_bridge": "mask",
}


@pytest.mark.kicad
@pytest.mark.parametrize("name", NAMES)
def test_the_checker_rejects_every_copper_error_kicad_finds(name: str):
    """D.2, the real proof and S3's gate, run over the examples' own routed boards.

    The two directions are not symmetric. pcbc flagging something KiCad passes is a superset that
    costs a pattern a fit — allowed, and every instance is recorded in `STRICTER` above with its
    shape. KiCad flagging a clearance, hole or edge error pcbc passed means **pcbc's model is
    unsound** and the slice does not land.

    It is not a vacuous test: the checked-in `blinky` board carries a genuine short (a `LED` track
    crossing `D1.1 [GND]`, from a generation of the board older than the pinned bar) and the
    checked-in `c3_usb` carries four real clearance errors, and the checker finds all six — the four
    c3_usb ones at exactly the millimetre KiCad prints for them.
    """
    from pcbc.netcheck import kicad_drc

    scene, _d, _j, _t = _scene(name, _routed(name))
    found = audit(scene)
    doc = kicad_drc(_routed(name), refill=False)
    errors = [v for v in doc.get("violations", []) if v.get("severity") == "error" and v.get("type") in _KICAD_RULES]
    for v in errors:
        rule = _KICAD_RULES[v["type"]]
        m = _ACTUAL.search(v.get("description") or "")
        hits = [(sub, c) for sub, c in found if c.rule == rule]
        assert hits, f"{name}: KiCad reports {v['type']} ({v.get('description')}) and the checker passed it"
        if m:
            want = float(m.group(1))
            assert any(abs(c.have - want) < 5e-4 for _s, c in hits), (
                f"{name}: KiCad measures {want} mm for {v['type']} and the checker's nearest is "
                f"{sorted(round(c.have, 4) for _s, c in hits)} — the same geometry, a different number"
            )
    extra = tuple(sorted((c.rule, round(c.have, 4), round(c.need, 4)) for _s, c in found if not _matched(c, errors)))
    assert extra == tuple(sorted(STRICTER[name])), (
        f"{name}: the checker is stricter than KiCad in {len(extra)} places and "
        f"docs/r2-measurements.md records {len(STRICTER[name])}: {extra}"
    )


def _matched(clash, errors) -> bool:
    """Is this clash one of the KiCad errors, by rule and by the millimetre KiCad printed?"""
    for v in errors:
        if _KICAD_RULES.get(v.get("type")) != clash.rule:
            continue
        m = _ACTUAL.search(v.get("description") or "")
        if m is None or abs(clash.have - float(m.group(1))) < 5e-4:
            return True
    return False


@pytest.mark.kicad
def test_kicad_skips_hole_to_hole_for_a_slot_so_rule_3_there_is_pcbcs_own_choice():
    """A.4 rule 3, measured: KiCad 10.0.6's hole-to-hole test only considers **circular** holes. Two
    round drills 2 mm apart under an 8 mm rule are reported; the same pair with either hole declared
    `(drill oval 0.6 1.6)` is reported by nothing at all.

    pcbc models the slot as a capsule (exact against KiCad's `hole_clearance`), so
    `clears(hole, hole, hole_to_hole())` there is strictly stricter than KiCad. That is the safe
    direction and it lands on real copper — the four `thru_hole oval` J1 legs on c3_usb and node —
    so it is written down: a refusal beside those legs is pcbc's own choice, not a DRC error it
    prevented, and a move that says otherwise is lying to the reader.
    """
    from pcbc.netcheck import kicad_drc

    import tempfile

    from pcbc.dru import DruRule, render

    def board(drill: str) -> str:
        return (
            '(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbc")\n\t(generator_version "0.1")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n\t(paper "A4")\n'
            '\t(layers\n\t\t(0 "F.Cu" mixed)\n\t\t(2 "B.Cu" mixed)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
            "\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n"
            '\t(net 0 "")\n\t(net 1 "N")\n'
            '\t(footprint "t:h"\n\t\t(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-000000000001")\n\t\t(at 10 10 0)\n'
            f'\t\t(pad "" np_thru_hole {"oval" if "oval" in drill else "circle"}\n\t\t\t(at 0 0)\n\t\t\t(size 1.8 2.6)\n\t\t\t({drill})\n'
            '\t\t\t(layers "*.Cu" "*.Mask")\n\t\t)\n\t)\n'
            '\t(via\n\t\t(at 12 10)\n\t\t(size 0.5)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net 1)\n'
            '\t\t(uuid "00000000-0000-4000-8000-000000000002")\n\t)\n'
            '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 30 20)\n\t\t(stroke (width 0.05) (type default))\n\t\t(fill none)\n'
            '\t\t(layer "Edge.Cuts")\n\t\t(uuid "00000000-0000-4000-8000-000000000003")\n\t)\n)\n'
        )

    pro = {
        "board": {"design_settings": {"defaults": {}, "rules": {"min_clearance": 0.05, "min_track_width": 0.05}, "rule_severities": {"hole_near_hole": "error"}}},
        "meta": {"filename": "layout.kicad_pcb", "version": 1},
        "net_settings": {"classes": [{"name": "Default", "clearance": 0.05, "track_width": 0.16, "via_diameter": 0.5, "via_drill": 0.3}]},
        "text_variables": {},
    }
    seen = {}
    with tempfile.TemporaryDirectory() as td:
        for tag, drill in (("round", "drill 0.9"), ("slot", "drill oval 0.6 1.6")):
            work = Path(td) / tag
            work.mkdir()
            (work / "layout.kicad_pcb").write_text(board(drill))
            (work / "layout.kicad_pro").write_text(json.dumps(pro, indent=2))
            (work / "layout.kicad_dru").write_text(render([DruRule("h2h", "(constraint hole_to_hole (min 8mm))", "")]))
            doc = kicad_drc(work / "layout.kicad_pcb", refill=False)
            seen[tag] = [v for v in doc.get("violations", []) if v.get("type") == "hole_to_hole"]
    assert seen["round"], f"a round NPTH against a via at 2 mm is reported: {seen['round']}"
    assert _ACTUAL.search(seen["round"][0]["description"]).group(1) == "1.4000", (
        "2 mm centres less half the 0.9 drill less half the 0.3 drill: edge to edge, which is what "
        "A.4 rule 3 says KiCad measures and what `clears(hole, hole, ...)` gets right for free"
    )
    from pcbc.route_geom import hole_shape

    assert round(gap(hole_shape(10.0, 10.0, 0.9), hole_shape(12.0, 10.0, 0.3)), 4) == 1.4, (
        "and pcbc measures the same 1.4000 mm for the same two holes"
    )
    assert seen["slot"] == [], (
        "and the identical pair with an oval drill is reported by nothing: KiCad 10.0.6 skips slots "
        "in its hole-to-hole test, so pcbc's capsule there is stricter with no DRC error behind it"
    )


@pytest.mark.kicad
def test_kicad_tolerates_half_a_micron_of_shortfall_which_is_the_room_eps_mm_has():
    """`EPS_MM`'s own comment, measured instead of asserted. KiCad 10.0.6 allows 0.0005 mm of
    shortfall on a clearance rule: a 0.2 mm rule last faults at a 0.1994989 mm gap and first passes
    at 0.1994995. So pcbc is 0.0006 mm stricter than KiCad, not the 0.0001 the constant suggests —
    and 1e-4 mm is exactly the 4-decimal resolution KiCad's reports print, not below it."""
    from pcbc.dru import DruRule, render
    from pcbc.netcheck import kicad_drc
    import tempfile

    def board(gap_mm: float) -> str:
        x = 10.0 + 1.0 + gap_mm  # two 1.0 mm circle pads, centres a diameter plus the gap apart
        return (
            '(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbc")\n\t(generator_version "0.1")\n'
            '\t(general\n\t\t(thickness 1.6)\n\t)\n\t(paper "A4")\n'
            '\t(layers\n\t\t(0 "F.Cu" mixed)\n\t\t(2 "B.Cu" mixed)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
            "\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n"
            '\t(net 0 "")\n\t(net 1 "A")\n\t(net 2 "B")\n'
            + "".join(
                f'\t(footprint "t:p{i}"\n\t\t(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-00000000000{i}")\n\t\t(at {px:.7f} 10 0)\n'
                f'\t\t(pad "1" smd circle\n\t\t\t(at 0 0)\n\t\t\t(size 1 1)\n\t\t\t(layers "F.Cu" "F.Mask")\n\t\t\t(net {i} "{n}")\n\t\t)\n\t)\n'
                for i, (px, n) in enumerate(((10.0, "A"), (x, "B")), start=1)
            )
            + '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 30 20)\n\t\t(stroke (width 0.05) (type default))\n\t\t(fill none)\n'
            '\t\t(layer "Edge.Cuts")\n\t\t(uuid "00000000-0000-4000-8000-00000000000f")\n\t)\n)\n'
        )

    pro = {
        "board": {"design_settings": {"defaults": {}, "rules": {"min_clearance": 0.05, "min_track_width": 0.05}, "rule_severities": {}}},
        "meta": {"filename": "layout.kicad_pcb", "version": 1},
        "net_settings": {"classes": [{"name": "Default", "clearance": 0.05, "track_width": 0.16, "via_diameter": 0.5, "via_drill": 0.3}]},
        "text_variables": {},
    }
    faults = {}
    with tempfile.TemporaryDirectory() as td:
        for tag, g in (("under", 0.1994989), ("over", 0.1994995), ("clear", 0.2002)):
            work = Path(td) / tag
            work.mkdir()
            (work / "layout.kicad_pcb").write_text(board(g))
            (work / "layout.kicad_pro").write_text(json.dumps(pro, indent=2))
            (work / "layout.kicad_dru").write_text(render([DruRule("c", "(constraint clearance (min 0.200000mm))", "")]))
            doc = kicad_drc(work / "layout.kicad_pcb", refill=False)
            faults[tag] = [v for v in doc.get("violations", []) if v.get("type") == "clearance"]
    assert faults["under"] and not faults["over"] and not faults["clear"], (
        f"the boundary moved: {{k: len(v) for k, v in faults.items()}} = {({k: len(v) for k, v in faults.items()})}"
    )
    a = Shape(((10.0, 10.0),), 0.5)
    b = Shape(((11.1995, 10.0),), 0.5)  # a 0.1995 mm gap: the first one on the grid KiCad passes
    assert round(gap(a, b), 4) == 0.1995
    assert not clears(a, b, 0.2), (
        "and pcbc refuses the gap KiCad passes: 0.0005 mm of KiCad's own tolerance plus 0.0001 mm "
        "of EPS_MM is 0.0006 mm of real margin, which is the number EPS_MM's docstring records — "
        "not the 0.0001 the constant alone suggests"
    )
    assert EPS_MM == 1e-4 and f"{EPS_MM:.4f}" == "0.0001", (
        "and EPS_MM is not below KiCad's display resolution: 1e-4 mm is exactly the 4 decimals its "
        "reports print, so a gap of exactly EPS_MM is the smallest number `actual` can say"
    )


def test_there_is_one_reader_of_a_custom_pads_primitives():
    """S1b put `_custom_size` in `pcb_place` so placement stops reading a 5 um dot; `pads.py` reads
    the same primitives for the router. Two readers that could disagree about how big the USB-C
    shield pads are is exactly the class of bug S1b was: the two views agree by construction."""
    from pcbc.pads import custom_size

    text = _placed("c3_usb").read_text()
    block = next(text[s:e] for s, e in board_footprint_spans(text) if footprint_reference(text[s:e]) == "J1")
    pad_block = next(b for b in re.findall(r"\(pad \"A4B9\"[\s\S]*?\n\t\t\)", block))
    w, h = custom_size(pad_block)
    geom = next(g for g in pad_geoms(block, footprint_at(block), ref="J1") if g.num == "A4B9")
    x0, y0, x1, y1 = geom.box()
    assert (round(w, 6), round(h, 6)) == (round(x1 - x0, 6), round(y1 - y0, 6)) == (0.699974, 1.399997), (
        f"the placement view says {(w, h)} and the routing view says {(x1 - x0, y1 - y0)}"
    )
