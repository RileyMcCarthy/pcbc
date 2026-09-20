"""The hop pattern and the pattern stage: `docs/r2-design.md` B.0, B.1 and the parts of C.1 to C.6
the hop needs.

One assertion per rule, every pinned number carrying its reference in the message, and every report
line asserted as an exact string. The numbers were measured against the five placed boards on
2026-09-20 and are recorded in `docs/r2-measurements.md` (S4); a drift names the rule it left.

Two of B.1's own sentences do not survive contact with the boards, and the tests that show why are
here rather than in a comment: blinky's straight `LED` line **shorts** `D1.1 [GND]`, and buck's `EN`
is refused by the shape of its pads' exits rather than by `U1.4`'s copper. Both claims came from
measurements taken before pads had exits; the refutations are pinned so the design's numbers cannot
quietly come back.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.fanout import fanout_pieces
from pcbc.language import load_board
from pcbc.patterns import (
    ALL_SHAPES,
    MITRE_MM,
    PatternCtx,
    legs_of,
    lane_ok,
    link_candidates,
    mitre,
    pattern_copper,
    pieces_of,
    terminals,
)
from pcbc.patterns.hop import DETOUR_MAX, HOP_MM, specs as hop_specs
from pcbc.route import bar_key, krt_plan, pin_copper_ids, write_fab_overrides
from pcbc.route_emit import piece_key
from pcbc.route_geom import MICRO_MM, is_octilinear, legs_ok, q, seg_lengths, turn_ok
from pcbc.route_scene import Exit, blocked, build_scene, pad_exits
from pcbc.route_verify import paths_of, verify_copper

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
NAMES = ("blinky", "buck", "c3_usb", "node")
ALL = NAMES + (("ds2",) if (DS2 / "ds2_addon.py").exists() else ())


def _board(name: str) -> Path:
    return DS2 / "ds2_addon.py" if name == "ds2" else EXAMPLES / name / f"{name}.py"


def _placed(name: str) -> Path:
    if name == "ds2":
        return DS2 / "layout" / "ds2_addon" / "placed" / "layout.kicad_pcb"
    return EXAMPLES / name / "layout" / name / "placed" / "layout.kicad_pcb"


def _uuid_name(name: str) -> str:
    return "ds2_addon" if name == "ds2" else name


def _plan(name: str):
    design = load_board(_board(name))
    job = compile_design(design)
    text = _placed(name).read_text()
    return pattern_copper(design, job, job.constraints, text, _uuid_name(name)), design, job, text


def _scene(name: str):
    design = load_board(_board(name))
    job = compile_design(design)
    text = _placed(name).read_text()
    return build_scene(design, job, job.constraints, text), design, job, text


def _exit(at, d, side, w=0.16, layer="F.Cu"):
    return Exit(at=at, dir=d, stub=(at, at), side=side, width=w, layer=layer)


# --- B.0: the shared link builder -----------------------------------------------------------------


def test_a_link_is_one_of_b0s_seven_shapes_in_b0s_order():
    """B.0 names seven link shapes and walks them in one fixed order. There is no scoring anywhere in
    a pattern: the order **is** the answer, so it is pinned."""
    got = link_candidates(_exit((0.0, 0.0), (1, 0), "right"), _exit((10.0, 3.0), (-1, 0), "left"))
    assert [n for n, _ in got] == ["Z-far", "Z-near", "S-h"], (
        "B.0's order is direct, Z-far, Z-near, L-h, L-v, S-h, S-v; here dx != dy so `direct` is not "
        "octilinear and the two L shapes are right angles"
    )
    for name, pts in got:
        assert is_octilinear(pts), (name, pts, "B.0: a candidate that is not 0/45/90 is dropped")
        assert turn_ok(pts), (name, pts, "B.0: a candidate that fails turn_ok is dropped")
    assert [n for n, _ in link_candidates(_exit((0.0, 0.0), (1, 0), "right"), _exit((10.0, 0.0), (-1, 0), "left"))] == ["direct"], (
        "with dy == 0 every shape is the straight line — S-h keeps a midpoint on it, which is not a "
        "turn and so not a vertex — and the duplicates are dropped"
    )


def test_the_two_l_shapes_never_survive_their_own_turn_check():
    """`L-h` and `L-v` are a right angle, and A.5's rule is an octant difference of at most one — so
    they are dropped by the filter, never by being left out of the list. B.0 names them, and a later
    slice that loosens `turn_ok` gets them back without editing the enumeration (`MAX_LINKS`)."""
    for dx, dy in ((10.0, 3.0), (3.0, 10.0), (-7.0, 2.5), (1.0, 9.0)):
        names = [n for n, _ in link_candidates(_exit((0.0, 0.0), (1, 0), "right"), _exit((dx, dy), (-1, 0), "left"))]
        assert "L-h" not in names and "L-v" not in names, (dx, dy, names)
    corner = ((0.0, 0.0), (10.0, 0.0), (10.0, 3.0))
    assert is_octilinear(corner) and not turn_ok(corner), "a right angle is an octant difference of 2, and the rule is 1 (A.5)"


def test_every_corner_is_computed_and_never_snapped():
    """A.5's second construction rule, on the two pairs the design measured: snapping a corner to the
    router's grid while keeping both endpoints produces neither an axis nor a 45 — node's `J1 -> U1`
    comes out with a far leg of dx 1.65, dy -0.04, and buck's with dx 11.31, dy -0.01 (B.0)."""
    for a, b in (((28.75, 37.44), (31.80, 36.00)), ((2.45, 11.975), (19.56, 6.16))):
        for name, pts in link_candidates(_exit(a, (1, 0), "right"), _exit(b, (-1, 0), "left")):
            for p, nxt in zip(pts, pts[1:]):
                dx, dy = round((nxt[0] - p[0]) * 1e6), round((nxt[1] - p[1]) * 1e6)
                assert dx == 0 or dy == 0 or abs(dx) == abs(dy), (name, p, nxt, "exact in nanometres, not 'within half a degree'")


# --- the mitre: a pad exit meets a link at a right angle ------------------------------------------


def test_a_right_angle_where_copper_leaves_a_pad_is_mitred_not_written():
    """A.7 makes every stub axis-aligned, so a link that runs off at 90 degrees to it is the common
    case — and `dru.py` writes `(constraint track_angle (min 135))` as `pcbc_geometry_angles` on every
    board, so writing that corner raises the count of the warning pcbc itself wrote to catch it.

    The corner is cut `MITRE_MM` back along each leg and joined by a 45, exactly."""
    got = mitre(((0.0, 0.0), (0.0, 1.0), (5.0, 1.0)))
    assert got == ((0.0, 0.0), (0.0, q(1.0 - MITRE_MM)), (q(MITRE_MM), 1.0), (5.0, 1.0)), got
    assert turn_ok(got) and is_octilinear(got), "every turn is 135 degrees or better after the cut"
    assert abs(seg_lengths(got)[1] - MITRE_MM * 2**0.5) < 1e-9, "the new leg is MITRE_MM * sqrt(2) = 0.2828 mm, over MICRO_MM"
    diagonals = mitre(((0.0, 0.0), (2.0, 2.0), (4.0, 0.0)))
    assert diagonals[1] == (q(2.0 - MITRE_MM), q(2.0 - MITRE_MM)) and diagonals[2] == (q(2.0 + MITRE_MM), q(2.0 - MITRE_MM)), (
        "a right angle between two diagonals is cut into an axis leg, which is the same construction seen from 45 degrees"
    )


def test_a_hairpin_is_not_mitred_into_a_route():
    """Three octants or more is copper doubling back on itself; no mitre makes that a route, and
    silently straightening it would move an endpoint, which A.5 forbids."""
    assert mitre(((0.0, 0.0), (5.0, 0.0), (5.0, 0.1))) is not None  # a right angle: cut
    assert mitre(((0.0, 0.0), (5.0, 0.0), (1.0, 1.0))) is None, "up-left after east is 3 octants"
    assert mitre(((0.0, 0.0), (5.0, 0.0), (0.0, 0.0))) is None, "straight back is 4 octants"


def test_no_board_writes_a_corner_kicads_own_angle_rule_would_count():
    """The three judges, on every piece of pattern copper on every board: 0/45/90, no turn under 135
    degrees, and no leg under `MICRO_MM` — which is exactly what `pcbc_geometry_angles` and
    `pcbc_geometry_segments` count, so the rules pcbc hands KiCad cannot fire on pcbc's own copper."""
    for name in ALL:
        plan, _d, _j, _t = _plan(name)
        for path in paths_of(plan.pieces):
            assert is_octilinear(path), (name, path)
            assert turn_ok(path), (name, path, "dru.py: (constraint track_angle (min 135))")
            assert legs_ok(path), (name, path, "dru.py: (constraint track_segment_length (min 0.2mm))")
        for p in plan.pieces:
            for v in (p.a[0], p.a[1], p.b[0], p.b[1]):
                assert q(v) == v and float(f"{v:.6f}") == v, (name, v, "A.5: quantise once, at construction")


# --- B.1: what a hop is ---------------------------------------------------------------------------


def test_blinky_is_one_hop_and_nothing_is_left_over():
    """B.1's own worked example, with the number it got wrong corrected by the board.

    B.1 says blinky's `LED` is 21.8675 mm of straight copper from `R1.2` to `D1.2`. It is not:
    `D1.1 [GND]` sits **on** that line. The hop steps around it in five legs at 22.8182 mm, blinky's
    only multi-pad net is pcbc's own and locked, and the board is 0 % leftover."""
    plan, _d, _j, _t = _plan("blinky")
    assert sorted(plan.done) == ["LED"] and plan.refused == {}, (plan.done, plan.refused)
    assert [(p.a, p.b, p.w) for p in plan.pieces] == [
        ((31.3075, 12.5), (31.3075, 13.0551), 0.16),
        ((31.3075, 13.0551), (31.1075, 13.2551), 0.16),
        ((31.1075, 13.2551), (10.7452, 13.2551), 0.16),
        ((10.7452, 13.2551), (9.9901, 12.5), 0.16),
        ((9.9901, 12.5), (9.44, 12.5), 0.16),
    ], "measured 2026-09-20 on blinky's placed board; docs/r2-measurements.md S4"
    assert round(sum(p.mm for p in plan.pieces), 4) == 22.8182, "against B.1's claimed 21.8675 for the straight line"
    assert plan.census == {"hop": {"segments": 5, "vias": 0, "mm": 22.8182}}, plan.census


def test_the_straight_line_b1_wanted_across_blinky_shorts_d1_1():
    """Why the number above is not B.1's. The straight `R1.2 -> D1.2` line overlaps `D1.1 [GND]` by
    0.2988 mm against the 0.2 mm the Default/Power pair needs — a short, not a clearance error.

    It is the same short S3's agreement test found in the checked-in routed artifact
    (`docs/r2-measurements.md` S3), which is where B.1's 21.8675 mm came from."""
    scene, _d, _j, _t = _scene("blinky")
    a, b = terminals(scene, "LED")
    line = pieces_of((a.at, b.at), [0.16], net="LED", reason="hop", layer="F.Cu", owner=a.owner, board="blinky")
    clash = blocked(scene, line, "LED", ignore=frozenset(i.id for i in scene.items if i.owner in (a.owner, b.owner)))
    assert clash is not None and clash.item.owner == "D1.1" and clash.item.net == "GND", clash
    assert (round(clash.have, 4), clash.need, clash.rule) == (-0.2988, 0.2, "copper"), (clash.have, clash.need, clash.rule)


def test_a_hop_leaves_its_pad_toward_its_partner_first():
    """A.7 orders a pad's exits **outward**, because that is what a fanout escape needs. A link wants
    the opposite first, and taking A.7's order literally sent c3_usb's `LED_A` round the outside at
    1.93x its 1.336 mm airwire. The re-ordering is an ordering of the enumeration, like B.0's own
    nesting, derived from the pads' geometry and nothing else."""
    from pcbc.patterns.hop import _toward_first

    scene, _d, _j, _t = _scene("c3_usb")
    a, b = terminals(scene, "CC1")
    outward = [e.side for e in pad_exits(scene, b.item, 0.16, "F.Cu")]
    toward = [e.side for e in _toward_first(pad_exits(scene, b.item, 0.16, "F.Cu"), b, a)]
    assert outward == ["right", "down", "up"] and toward == ["down", "right", "up"], (outward, toward)
    assert b.at[1] < a.at[1], "`R_CC1.1` sits above `J1.A5`, so the exit that points at it is `down`"
    assert [e.side for e in _toward_first(pad_exits(scene, b.item, 0.16, "F.Cu"), b, a)] == [
        e.side for e in _toward_first(pad_exits(scene, b.item, 0.16, "F.Cu"), b, a)
    ], "an ordering, not a score: the same pads give the same order"


def test_a_hop_that_walks_is_not_a_hop():
    """B.0: a pattern never degrades, and R2's claim is that pcbc's structured copper is *better*.

    c3_usb's `D1.2` and `R_LED.2` sit 1.336 mm apart with their exits 0.08 mm from crossing, so the
    only shape that clears goes round the outside. Taking it moved c3_usb's worst detour from 1.92 to
    2.02 — a pinned bar number — for 1.36 mm of copper, so the hop refuses and KRT has it."""
    plan, _d, _j, _t = _plan("c3_usb")
    assert sorted(plan.refused) == ["LED_A"], plan.refused
    assert plan.refused["LED_A"][0].move == (
        "hop LED_A: D1.2 at (21.0525,11.45) cannot reach R_LED.2 at (19.74,11.2) on F.Cu.\n"
        "  In the way: the shortest candidate that fits runs 1.929x the 1.336 mm between the pads, "
        "past the 1.5x a hop may take (rule: detour)\n"
        "  Tried 4 x 3 exits x 3 link shapes = 36 candidates, 2 of them the shape of copper pcbc writes.\n"
        '  Moves: Place("D1", toward="right") puts it on the side R_LED.2\'s right exit faces; '
        'or NetReq("LED_A", layers=["B.Cu"]) puts the net on B.Cu, where this row is not.'
    ), plan.refused["LED_A"][0].move
    assert DETOUR_MAX == 1.5, "every hop that fits on the five boards is 1.00 to 1.24x; docs/r2-measurements.md S4"


def test_bucks_en_is_refused_and_the_refusal_is_a_move():
    """B.1 predicts `EN` refused, and it is — but not by `U1.4 [FB]`'s copper at 0.089 of 0.200 mm.
    That number is A.7's measurement of the **centre-to-centre** line, which a pattern never draws.
    `U1.5`'s only exit points down, away from `R_EN.2`, so no candidate has a shape at all; the
    refusal says which exits exist, which is the fact the AI can act on."""
    plan, _d, _j, _t = _plan("buck")
    assert sorted(plan.refused) == ["BOOT", "EN"] and not plan.pieces, (plan.refused, len(plan.pieces))
    assert plan.refused["EN"][0].move == (
        "hop EN: R_EN.2 at (17.71,6.454) cannot reach U1.5 at (16.46,7.66) on F.Cu.\n"
        "  In the way: no link fits between the exits these pads have on F.Cu (R_EN.2: left, up; U1.5: down) — "
        "every candidate doubled back on itself or left a leg under 0.2 mm (rule: no candidate)\n"
        "  Tried 2 x 1 exits x 3 link shapes = 6 candidates, 0 of them the shape of copper pcbc writes.\n"
        '  Moves: Place("R_EN", toward="down") puts it on the side U1.5\'s down exit faces; '
        'or NetReq("EN", layers=["B.Cu"]) puts the net on B.Cu, where this row is not.'
    ), plan.refused["EN"][0].move
    assert all(not r.hard for r in plan.refusals()), "C.6: a hop refusal is never hard — KRT's constrained step honours the same intent"


def test_past_hop_mm_only_the_straight_line_counts():
    """B.1's second clause and the one reading of it the boards force.

    node's `DRV` is 32.27 mm apart with no straight line between its exits, so it is a route and not a
    hop. ds2's `GPIO0` (17.34 mm) is the same, and letting it through locked 33 mm of copper across
    the board before anything else was routed — after which `GND` could not reach five of its pads."""
    plan, _d, _j, _t = _plan("node")
    assert sorted(plan.refused) == ["DRV"], plan.refused
    assert plan.refused["DRV"][0].rule == "not local" and "only the straight line between them counts" in plan.refused["DRV"][0].move
    assert HOP_MM == 6.0, "route.LOCAL_MM is 5.0; 6.0 catches node's CC2 at 4.13 mm with headroom (B.1)"
    scene, _d, _j, _t = _scene("node")
    a, b = terminals(scene, "CC2")
    assert round(math.dist(a.at, b.at), 2) == 4.13, "B.1's own example of what HOP_MM has to catch"


@pytest.mark.skipif("ds2" not in ALL, reason="the DS2 Addon is not checked out here")
def test_the_ds2_addon_hops_exactly_its_ten_local_two_pad_nets():
    """S4's acceptance, verbatim: A0-A3, REFP, REFN, UART_RX, UART_TX, DRDY and nRESET are single-path
    hops. Every one of the five it refuses is refused because no straight line exists between the
    pads' exits and the pads are further apart than `HOP_MM`."""
    plan, _d, _j, _t = _plan("ds2")
    assert sorted(plan.done) == ["A0", "A1", "A2", "A3", "DRDY", "REFN", "REFP", "UART_RX", "UART_TX", "nRESET"], sorted(plan.done)
    assert sorted(plan.refused) == ["ADC_DRDY", "ADC_RX", "ADC_TX", "GPIO0", "GPIO1"], sorted(plan.refused)
    assert {r.rule for r in plan.refusals()} == {"not local"}, [(r.net, r.rule) for r in plan.refusals()]
    assert round(sum(p.mm for p in plan.pieces), 4) == 23.7139, "docs/r2-measurements.md S4"
    singles = [n for n in plan.done if len([p for p in plan.pieces if p.net == n]) == 1]
    assert sorted(singles) == ["A0", "A1", "A2", "A3", "DRDY", "REFN", "REFP", "UART_RX", "UART_TX", "nRESET"], (
        "each is one segment: the stub, the link and the far stub are collinear and merge (B.0)"
    )


def test_a_pattern_never_touches_a_plane_net_or_a_pair():
    """B.1 routes neither: a plane net is the tap pattern's (B.3) and a differential pair is R4's, and
    R2 measures nothing about pairs."""
    for name in ALL:
        scene, design, job, _t = _scene(name)
        ctx = PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board=name, stage="pre")
        nets = set(hop_specs(ctx))
        assert not (nets & set(scene.plane_of)), (name, nets & set(scene.plane_of))
        pairs = {c.net for c in job.constraints.constraints if c.pair is not None}
        assert not (nets & pairs), (name, nets & pairs)


# --- C.1, C.4: the stage --------------------------------------------------------------------------


def test_the_stage_checks_its_own_copper_and_raises_rather_than_writes():
    """D.1: `verify_copper` runs unconditionally at the end of the stage and a non-empty result raises.
    It is the reason the gate can be expected to find nothing."""
    for name in ALL:
        plan, _d, _j, _t = _plan(name)
        assert verify_copper(plan.scene, plan.pieces, _j_of(name), ids=plan.ids) == [], name
    plan, design, job, text = _plan("blinky")
    a, b = terminals(plan.scene, "LED")
    bad = pieces_of((a.at, b.at), [0.16], net="LED", reason="hop", layer="F.Cu", owner=a.owner, board="blinky")
    assert verify_copper(plan.scene, bad, job.constraints) == [
        "hop LED: F.Cu (31.3075,12.5)-(9.44,12.5) breaks copper against D1.1 [GND] (-0.2988 mm of 0.2 mm, class Power)"
    ], "B.1's own straight line, which shorts D1.1"


def _j_of(name: str):
    return compile_design(load_board(_board(name))).constraints


def test_each_patterns_copper_is_an_obstacle_to_the_next_one():
    """C.1: the stage is a single left-to-right pass. Each pattern's copper enters the scene before the
    next runs, with ids above every existing id, in emission order (A.6)."""
    plan, _d, _j, _t = _plan("ds2")
    assert list(plan.ids) == sorted(plan.ids), "Scene.add appends; an item's id never changes under a pattern that already asked about it"
    assert plan.ids and min(plan.ids) > 0
    for n, pid in enumerate(plan.ids):
        assert plan.scene.items[pid].net == plan.pieces[n].net and plan.scene.items[pid].locked, (n, pid)


@pytest.mark.skipif("ds2" not in ALL, reason="the DS2 Addon is not checked out here")
def test_the_fanout_skips_the_nets_the_hops_claimed():
    """C.1: hops run FIRST and `fanout._excluded` gains what they took, so a closed row's lane is spent
    on the hop that needed it rather than on a via the hop then has to start from."""
    plan, design, job, text = _plan("ds2")
    plain, _n = fanout_pieces(design, job, text, "ds2_addon")
    after, _n = fanout_pieces(design, job, plan.text, "ds2_addon", plan.scene, claimed=plan.claimed)
    assert len([p for p in plain if p.kind == "via"]) == 10 and len([p for p in after if p.kind == "via"]) == 9, (
        "ds2's `nRESET` is a hop now, so `R10.2`'s escape is not spent; docs/r2-measurements.md S4"
    )
    assert {p.net for p in plain} - {p.net for p in after} == {"nRESET"}, "a claimed net keeps its pads bare"
    assert {p.net for p in after} & plan.claimed == set()


def test_krt_plan_drops_what_the_patterns_finished():
    """C.4: `local_hops` is built from what the pattern refused and disappears when there is nothing
    left; `signals` carries a `!NET` for every finished net, belt and braces."""
    design = load_board(_board("blinky"))
    job = compile_design(design)
    placed = _placed("blinky")
    plan, _d, _j, _t = _plan("blinky")
    names = [n for n, _ in krt_plan(job, design, placed, Path("work"), Path("/krt"), plan)]
    assert "local_hops" not in names, "blinky's one local net is a hop now, so KRT's hop step has nothing to do"
    sig = dict(krt_plan(job, design, placed, Path("work"), Path("/krt"), plan))["signals"]
    assert "!LED" in sig, sig
    plain = [n for n, _ in krt_plan(job, design, placed, Path("work"), Path("/krt"))]
    assert plain == [n for n, _ in krt_plan(job, design, placed, Path("work"), Path("/krt"), None)], "no plan is the pre-R2 plan"


def test_write_fab_overrides_never_escalates_a_class(tmp_path: Path):
    """C.5, scoped so it cannot raise a floor: the smallest class clearance, never the largest.

    `max(class clearance)` on node is 0.2 and would raise the floor above the USB class's own 0.18 and
    above the 0.15 mm gap the pair is dimensioned for; that is a different edit from the one the
    docstring promises."""
    want = {"blinky": 0.16, "buck": 0.16, "c3_usb": 0.155, "node": 0.18, "ds2": 0.16}
    for name in ALL:
        job = compile_design(load_board(_board(name)))
        d = tmp_path / name
        d.mkdir()
        line = next(x for x in write_fab_overrides(job, d).read_text().splitlines() if x.startswith("clearance"))
        got = float(line.split("=")[1])
        assert got == want[name], (name, got, want[name], "docs/r2-design.md A.3's compiled numbers")
        assert all(got <= c.clearance_mm + 1e-12 for c in job.classes), (name, [(c.name, c.clearance_mm) for c in job.classes])


def test_pin_copper_ids_keeps_pcbcs_own_ids_and_re_keys_everything_else():
    """C.3's second guard: pcbc's uuids derive from the same geometry key `copper.json` is keyed by, so
    re-keying them would leave the sidecar pointing at ids that no longer exist."""
    text = (
        '(kicad_pcb\n\t(segment\n\t\t(start 0 0)\n\t\t(end 1 1)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "A")\n\t\t(uuid "mine")\n\t)\n'
        '\t(segment\n\t\t(start 1 1)\n\t\t(end 2 2)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "A")\n\t\t(uuid "krt")\n\t)\n)\n'
    )
    out = pin_copper_ids(text, "b", frozenset({"mine"}))
    assert '(uuid "mine")' in out and '(uuid "krt")' not in out, out
    plain = pin_copper_ids(text, "b")
    assert plain.count('(uuid "') == 2 and '(uuid "mine")' not in plain, "without keep, nothing is preserved: the pre-R2 behaviour"
    assert out.split('(uuid "')[2] == plain.split('(uuid "')[2], "a kept id still consumes its position, so nothing else moves"


def test_patterns_off_claims_nothing_and_is_a_rollback():
    """C.6's escape hatch: `PCBC_PATTERNS=off` restores the pre-R2 plan exactly — one env check around
    one call. Measured with the boards: every copper-bar number on all five is the S1b row."""
    from pcbc.patterns import empty_plan, patterns_off

    plan = empty_plan("(kicad_pcb)\n")
    assert plan.pieces == () and plan.claimed == frozenset() and plan.done == frozenset() and plan.text == "(kicad_pcb)\n"
    assert not patterns_off(), "the default is on; the test for the env var itself is the measurement in docs/r2-measurements.md S4"


def test_the_board_is_a_pure_function_of_the_placed_board():
    """F.3 item 2. Two runs give byte-identical copper and identical keys, with no `set` iteration and
    no dict-over-floats anywhere in a decision path (A.6)."""
    for name in ALL:
        one, _d, _j, _t = _plan(name)
        two, _d2, _j2, _t2 = _plan(name)
        assert one.text == two.text, name
        assert [piece_key(p) for p in one.pieces] == [piece_key(p) for p in two.pieces], name
        assert [p.uuid for p in one.pieces] == [p.uuid for p in two.pieces], name


def test_a_pieces_key_survives_the_two_rewrites_the_board_gets():
    """D.4: the reason cannot live in the file — `pin_copper_ids` re-keys uuids and KiCad invents its
    own during the gate's refill-and-save — so the census is keyed by geometry, with the two ends of a
    segment in a fixed order because nothing says which end KiCad writes first."""
    from pcbc.copper_bar import bar_key as read_key
    from pcbc.route_emit import seg_piece

    p = seg_piece("GND", "hop", "F.Cu", (2.0, 3.0), (1.0, 3.0), 0.2)
    assert bar_key(p) == read_key("seg", "F.Cu", (1.0, 3.0), (2.0, 3.0), 0.2) == read_key("seg", "F.Cu", (2.0, 3.0), (1.0, 3.0), 0.2)


# --- A.7: the lane rule a pattern has to obey ------------------------------------------------------


def test_a_piece_may_cross_a_fanout_lane_and_may_not_run_along_it():
    """A.7, and the DS2 Addon's own lesson (`docs/copper-plan.md` line 191): analog copper ran *along*
    a lane and walled AVDD, DVDD and the UART pins in. Forbidding a crossing outright would cut
    c3_usb's board in half along `J1`'s 1.4515 mm lane."""
    scene, _d, _j, _t = _scene("c3_usb")
    lane = next(it for it in scene.items if it.kind == "lane")
    x0, y0, x1, y1 = lane.box()
    mid = (y0 + y1) / 2.0
    across = pieces_of(((q((x0 + x1) / 2.0), q(y0 - 1.0)), (q((x0 + x1) / 2.0), q(y1 + 1.0))), [0.16], net="GND", reason="hop", layer="F.Cu", owner="X1.1", board="t")
    along = pieces_of(((q(x0 + 0.1), q(mid)), (q(x1 - 0.1), q(mid))), [0.16], net="GND", reason="hop", layer="F.Cu", owner="X1.1", board="t")
    assert lane_ok(scene, across, ("X1",))[0], "crossing a lane is what a lane is there to allow"
    assert not lane_ok(scene, along, ("X1",))[0], (x0, x1, "running the length of a lane is what walls a footprint in")
    assert lane_ok(scene, along, (lane.owner.split(" ")[0],))[0], "the lane's owner is exempt in its own lane"


def test_the_stage_and_the_self_check_exempt_the_same_footprints():
    """The two must agree or a candidate the pattern accepts fails the self-check. Both ask which
    footprints the run **serves** — the ones whose pads it lands on — which is `check_corridors`'
    question ("unless the net owns an escape in it")."""
    from pcbc.route_verify import served_refs

    plan, _d, _j, _t = _plan("ds2")
    for net in sorted(plan.claimed):
        a, b = terminals(plan.scene, net)
        assert served_refs(plan.scene, plan.pieces, net, "hop") == frozenset({a.ref, b.ref}), net


# --- B.0: the neck, and the widths a hop writes ---------------------------------------------------


def test_a_hop_carries_the_class_width_and_necks_only_at_the_pad():
    """B.1: the width is `Constraint.width_mm`, necked at the entry stub only to the pad's across
    dimension — `fanout.py`'s own neck, which necks *to* the pad and not to twice it — and never below
    the fab's `track_min`. A hop never degrades a track to make it fit (B.0)."""
    for name in ALL:
        plan, _d, job, _t = _plan(name)
        for net in sorted(plan.claimed):
            c = job.constraints.by_net(net)
            want = float(c.width_mm.value) if c is not None else plan.scene.stack.track_min
            widths = {p.w for p in plan.pieces if p.net == net}
            assert max(widths) == want, (name, net, widths, want)
            assert min(widths) >= plan.scene.stack.track_min, (name, net, widths)


def test_a_run_of_collinear_legs_is_one_segment():
    """Why ds2's ten hops are ten segments and not thirty: the stub out, the link and the stub in are
    one straight run of copper, and writing it as three touching segments is three items in the file
    for one piece of geometry."""
    pieces = pieces_of(((0.0, 0.0), (1.0, 0.0), (5.0, 0.0), (6.0, 1.0)), [0.2, 0.2, 0.2], net="A", reason="hop", layer="F.Cu", owner="R1.1", board="t")
    assert [(p.a, p.b) for p in pieces] == [((0.0, 0.0), (5.0, 0.0)), ((5.0, 0.0), (6.0, 1.0))]
    assert legs_of(pieces) == ((0.0, 0.0), (5.0, 0.0), (6.0, 1.0))
    split = pieces_of(((0.0, 0.0), (1.0, 0.0), (5.0, 0.0)), [0.1, 0.2], net="A", reason="hop", layer="F.Cu", owner="R1.1", board="t")
    assert len(split) == 2, "a width change is a new segment even when the heading does not turn"


def test_the_length_judge_runs_on_the_merged_path():
    """`legs_ok` asks the merged path, never the raw one: two collinear legs that each fall under
    `MICRO_MM` are one legal segment once merged, and refusing them first refuses copper KiCad
    accepts. `route_geom.legs_ok` counts exactly what `pcbc_geometry_segments` counts."""
    raw = ((0.0, 0.0), (0.1, 0.0), (0.3, 0.0))
    assert not legs_ok(raw), "0.1 mm is under MICRO_MM"
    merged = legs_of(pieces_of(raw, [0.2, 0.2], net="A", reason="hop", layer="F.Cu", owner="R1.1", board="t"))
    assert merged == ((0.0, 0.0), (0.3, 0.0)) and legs_ok(merged) and MICRO_MM == 0.2


def test_every_shape_b0_names_is_in_the_enumeration():
    """`ALL_SHAPES` is B.0's list and nothing else; a pattern that adds a shape adds it here, where the
    bound on the candidate list is declared."""
    assert ALL_SHAPES == frozenset({"direct", "Z-far", "Z-near", "L-h", "L-v", "S-h", "S-v"})


def test_a_hop_refuses_rather_than_reordering_or_narrowing():
    """B.0: a pattern emits nothing at all when nothing clears. A refused net has no pieces, and every
    refusal names a rule of A.4 (or `no candidate`, `detour`, `not local`, `lane`) plus a move ending
    in a `board.py` edit."""
    known = {"copper", "hole_to_copper", "hole_to_hole", "edge", "no candidate", "detour", "not local", "lane", "no layer"}
    for name in ALL:
        plan, _d, _j, _t = _plan(name)
        for r in plan.refusals():
            assert r.net not in plan.claimed, (name, r.net, "a refused net gets no copper at all")
            assert r.rule in known, (name, r.net, r.rule)
            assert "Moves: " in r.move and ("Place(" in r.move or "NetReq(" in r.move), (name, r.move)


def test_the_stage_is_fast_enough_to_run_on_every_build():
    """F.3 item 8: the two pattern stages together at most 2.0 s on node and ds2, 0.3 s on blinky."""
    budget = {"blinky": 300, "buck": 2000, "c3_usb": 2000, "node": 2000, "ds2": 2000}
    for name in ALL:
        plan, _d, _j, _t = _plan(name)
        assert plan.wall_ms <= budget[name], (name, plan.wall_ms, budget[name])


# --- the DS2 Addon's own bar, recorded for the first time -----------------------------------------

DS2_BAR = {"segments": 340, "vias": 28, "off45": 11, "micro": 118, "mm": 509.9, "detour": 3.23}
"""The only board here drawn for a real order, and the only one `test_examples_fab.py` cannot hold:
it lives outside this repo. S1 said its ceilings would be recorded and they never were, so they are
recorded here — measured 2026-09-20 from a fresh build, `docs/r2-measurements.md` S4. Every number is
a ceiling except the census and the refusals, which are exact."""


@pytest.mark.kicad
@pytest.mark.krt
@pytest.mark.skipif("ds2" not in ALL, reason="the DS2 Addon is not checked out here")
def test_the_ds2_addon_builds_to_fab_with_its_hops_in_it(tmp_path: Path):
    """The DS2 Addon end to end: the gate verified, the hops locked and still there after every KRT
    step, and the bar no worse than S1b's (`docs/r2-measurements.md`). Built into a temp copy — the
    MaD checkout is never written to."""
    import shutil

    from pcbc.build import build_job

    shutil.copytree(DS2, tmp_path / "ds2", ignore=shutil.ignore_patterns("layout"))
    result = build_job(tmp_path / "ds2" / "ds2_addon.py", upto="fab", force=True)
    assert result.get("error") is None, result.get("error")
    route = next(s for s in result["steps"] if s.get("stage") == "route")
    assert route["unrouted"] == [] and route["copper"] == "verified", route["unrouted"]
    t = route["copper_bar"]["totals"]
    for key, want in (("segments", "segments"), ("vias", "vias"), ("off45", "off45"), ("micro", "micro")):
        assert t[key] <= DS2_BAR[want], (key, t[key], DS2_BAR[want], route["copper_bar"]["lines"])
    assert t["routed_mm"] <= DS2_BAR["mm"] and t["worst_detour"][1] <= DS2_BAR["detour"] + 0.05, (t["worst_detour"], t["routed_mm"])
    assert route["refused"] == {"hop": 5}, route["refused"]
    owns = {r: (v["segments"], v["vias"]) for r, v in t["by_reason"].items() if r != "leftover"}
    assert owns == {"fanout": (9, 9), "hop": (10, 0)}, (owns, route["copper_bar"]["lines"])


def test_strict_patterns_makes_every_refusal_fatal(monkeypatch):
    """C.6's other escape hatch. In R2 a refusal is a printed move and a fall-through to KRT — the
    count is pinned per board so it cannot drift into being ignored — and `--strict-patterns` (or
    `PCBC_STRICT_PATTERNS=1`) says "I want the move to stop the build". R3 flips the default."""
    from pcbc.patterns import hard_refusals

    plan, _d, _j, _t = _plan("buck")
    assert plan.refusals() and hard_refusals(plan) == (), "a hop refusal is soft: KRT's constrained step honours the same intent"
    monkeypatch.setenv("PCBC_STRICT_PATTERNS", "1")
    assert hard_refusals(plan) == plan.refusals(), "under --strict-patterns every refusal is fatal"
    assert plan.counts() == {"hop": 2}, plan.counts()
