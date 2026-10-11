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
    MAX_LINKS,
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
from pcbc.patterns.tap import TAP_REACH_MM, anchor, specs as tap_specs, tap_via
from boardtext import bar_key
from pcbc.route_emit import piece_key, seg_piece, via_piece
from boardtext import write_pieces
from pcbc.route_geom import EPS_MM, MICRO_MM, is_octilinear, legs_ok, q, seg_lengths, turn_ok
from pcbc.route_geom import track_shape
from pcbc.route_scene import Exit, Item, blocked, pad_exits
from pcbc.route_verify import POUR_CELL_MM, in_zone, paths_of, plane_area, plane_checks, plane_islands, pour_raster, verify_copper, zones
from boardtext import scene_from_text
from conftest import declared_examples, ds2_dir

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = declared_examples()  # the examples as shipped with a declared outline (vehicles): conftest.declared_examples
DS2 = ds2_dir()
NAMES = ("blinky", "buck", "c3_usb", "node")
ALL = NAMES + (("ds2",) if (DS2 / "ds2_addon.py").exists() else ())


BUCK_NETREQ = ROOT / "tests" / "fixtures" / "buck_netreq" / "buck.py"
"""buck as it shipped before docs/net-path-physics.md PR 10 (one NetReq for VIN, 5V and GND, no Path):
the board every pattern measurement in this file was taken on. The example itself migrated to Paths,
and a spine never runs on a net whose paths carry their own numbers, so the pattern pins keep their
board here rather than lose it."""


def _board(name: str) -> Path:
    if name == "buck":
        return BUCK_NETREQ
    return DS2 / "ds2_addon.py" if name == "ds2" else EXAMPLES / name / f"{name}.py"


def _placed(name: str) -> Path:
    # Generated from `board.py`, never read out of `examples/**/layout/` — that directory is
    # gitignored build output, so reading it made 45 tests in this file raise
    # `FileNotFoundError` on a clean checkout, and the artifacts were stale besides
    # (`conftest.placed_board`). Placing is pure Python: check + seed + place, no KiCad.
    from conftest import placed_board

    return placed_board("buck_netreq" if name == "buck" else name, _board(name))


def _uuid_name(name: str) -> str:
    return "ds2_addon" if name == "ds2" else name


def _plan(name: str):
    design = load_board(_board(name))
    job = compile_design(design)
    text = _placed(name).read_text()
    return pattern_copper(design, job, job.constraints, _uuid_name(name), scene=scene_from_text(design, job, job.constraints, text)), design, job, text


def _post(name: str):
    """The post stage over the **placed** board.

    On a real build the post stage reads the board KRT has routed as far as its `planes` step (C.1),
    which is why the tap counts here and the ones `test_examples_fab.py` pins are not the same number:
    on node 64 pads are tappable on the bare placed board and 62 once the pair and the constrained
    nets are down. The placed board is the deterministic input a unit test can pin, and the build
    numbers are pinned where they are measured, each with its own reference.
    """
    design = load_board(_board(name))
    job = compile_design(design)
    text = _placed(name).read_text()
    return pattern_copper(design, job, job.constraints, _uuid_name(name), stage="post", scene=scene_from_text(design, job, job.constraints, text)), design, job, text


def _ctx(name: str, stage: str = "post"):
    scene, design, job, _t = _scene(name)
    return PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board=name, stage=stage)


def _scene(name: str):
    design = load_board(_board(name))
    job = compile_design(design)
    text = _placed(name).read_text()
    return scene_from_text(design, job, job.constraints, text), design, job, text


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
    from pcbc.patterns import toward_first

    scene, _d, _j, _t = _scene("c3_usb")
    a, b = terminals(scene, "CC1")
    outward = [e.side for e in pad_exits(scene, b.item, 0.16, "F.Cu")]
    toward = [e.side for e in toward_first(pad_exits(scene, b.item, 0.16, "F.Cu"), b, a)]
    assert outward == ["right", "down", "up"] and toward == ["down", "right", "up"], (outward, toward)
    assert b.at[1] < a.at[1], "`R_CC1.1` sits above `J1.A5`, so the exit that points at it is `down`"
    assert [e.side for e in toward_first(pad_exits(scene, b.item, 0.16, "F.Cu"), b, a)] == [
        e.side for e in toward_first(pad_exits(scene, b.item, 0.16, "F.Cu"), b, a)
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
        "  Tried 4 x 3 exits x 3 link shapes = 36 candidates, 2 of them the shape of copper pcbc writes."
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
        "  Tried 2 x 1 exits x 3 link shapes = 6 candidates, 0 of them the shape of copper pcbc writes."
    ), plan.refused["EN"][0].move
    assert all(not r.hard for r in plan.refusals()), "C.6: a hop refusal is never hard — KRT's constrained step honours the same intent"


def test_past_hop_mm_only_the_straight_line_counts():
    """B.1's second clause and the one reading of it the boards force.

    node's `DRV` is 32.27 mm apart with no straight line between its exits, so it is a route and not a
    hop. ds2's `GPIO0` (17.34 mm) is the same, and letting it through locked 33 mm of copper across
    the board before anything else was routed — after which `GND` could not reach five of its pads."""
    plan, _d, _j, _t = _plan("node")
    # LED_A refuses on its detour (D1.2 to R_LED.2: 1.885x the 1.336 mm between them) since 2026-10-03: the silkscreen no longer re-places a part whose reference has no room (node's C_EN, R_EN and R_LED went 0.6 mm further out before).
    assert sorted(plan.refused) == ["DRV", "LED_A"], plan.refused
    assert plan.refused["LED_A"][0].rule == "no candidate" and "1.885x" in plan.refused["LED_A"][0].move
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
    plain, _n = fanout_pieces(design, job, "ds2_addon", scene=scene_from_text(design, job, job.constraints, text))
    after, _n = fanout_pieces(design, job, "ds2_addon", scene=plan.scene, claimed=plan.claimed)
    # 16 / 15 since docs/physics-subjects.md PR 5 (was 10 / 9): the six analog nets (AIN0..3, REFN_F, REFP_F) no
    # longer forbid vias, so `fanout._excluded` lets their pads escape too.
    assert len([p for p in plain if p.kind == "via"]) == 16 and len([p for p in after if p.kind == "via"]) == 15, (
        "ds2's `nRESET` is a hop now, so `R10.2`'s escape is not spent; docs/r2-measurements.md S4"
    )
    assert {p.net for p in plain} - {p.net for p in after} == {"nRESET"}, "a claimed net keeps its pads bare"
    assert {p.net for p in after} & plan.claimed == set()


def test_the_board_is_a_pure_function_of_the_placed_board():
    """F.3 item 2. Two runs give byte-identical copper and identical keys, with no `set` iteration and
    no dict-over-floats anywhere in a decision path (A.6)."""
    for name in ALL:
        one, _d, _j, _t = _plan(name)
        two, _d2, _j2, _t2 = _plan(name)
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


def test_a_via_may_not_sit_in_a_fanout_lane_at_all():
    """A via has no run to measure: it occupies (finding 6).

    `lane_ok` and D.1 both looped `if p.kind != "seg": continue`, so no via was ever put to the lane
    rule — and half of every tap is a via. Measured on the DS2 Addon before the fix: `C5`'s tap via
    sat 0.1221 mm inside `U1`'s top lane, in the column `U1.11` (AIN0) would escape through.
    """
    from pcbc.route_emit import via_piece
    from pcbc.route_verify import via_in_lane

    scene, _d, _j, _t = _scene("c3_usb")
    lane = next(it for it in scene.items if it.kind == "lane")
    x0, y0, x1, y1 = lane.box()
    owner = lane.owner.split(" ")[0]
    inside = via_piece("GND", "tap", (q((x0 + x1) / 2.0), q((y0 + y1) / 2.0)), 0.5, 0.3, owner="X1.1")
    clear = via_piece("GND", "tap", (q((x0 + x1) / 2.0), q(y1 + 1.0)), 0.5, 0.3, owner="X1.1")
    assert not lane_ok(scene, (inside,), ("X1",))[0], "a via in a lane is the one thing a lane cannot survive"
    assert lane_ok(scene, (inside,), (owner,))[0], "the lane's owner is exempt in its own lane, as it is for a segment"
    assert lane_ok(scene, (clear,), ("X1",))[0], "a via outside every lane is nobody's business"
    assert via_in_lane(scene, inside.a, inside.w, frozenset()) == lane.owner
    assert via_in_lane(scene, clear.a, clear.w, frozenset()) == ""


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
            assert "Moves: " not in r.move and "Place(" not in r.move and "NetReq(" not in r.move, (name, r.move)


def test_the_stage_is_fast_enough_to_run_on_every_build():
    """F.3 item 8: the two pattern stages together at most 2.0 s on node and ds2, 0.3 s on blinky."""
    budget = {"blinky": 300, "buck": 2000, "c3_usb": 2000, "node": 2000, "ds2": 2000}
    for name in ALL:
        plan, _d, _j, _t = _plan(name)
        assert plan.wall_ms <= budget[name], (name, plan.wall_ms, budget[name])


# --- the DS2 Addon's own bar, recorded for the first time -----------------------------------------

# 2026-10-02: 143 segments (and DS2_OWNS, the pruned VSS stub, the merges and the plane area below) were re-recorded
# on 2026-10-01 while decision 4's exit corridor ran to the board edge and moved C4-C6; with the corridor ending
# where the path can turn, ds2 is placed and routed byte-identical to the checkpoint and these are its numbers again.
DS2_BAR = {"segments": 158, "vias": 0, "off45": 5, "micro": 1, "mm": 421.4, "detour": 2.48}  # 142 / 0 / 411.9 before physics-subjects PR 5 (analog may fan out and change layer; DS2 completes); re-measured 2026-10-02
"""The DS2 Addon's copper bar on the native router's emitted board (`copper_bar` totals), re-recorded
2026-09-25 (`scratchpad/native/fix2/measure_r.py`; the KRT-era numbers, 315 segments / 17 leftover vias /
508.3 mm, are the old router's and say nothing about this one). Ceilings, like `test_examples_fab.BAR`.
The board stops at `REFN_F` (unrouted; its generated copper is dropped, `route_native.drop_unfinished`), so
routing `REFN_F` will raise `segments` and `mm`, and that move should be read. `vias` is `vias_leftover`:
copper no `pcbc:` group names, none natively. The worst detour is `ADC_TX`, 2.48. Re-recorded 2026-09-26
(`scratchpad/native/fix3/acc/table.md`): 144 -> 142 segments and 415.3 -> 411.9 mm, exactly the 3.487 mm the
third refutation round measured laid over same-net copper (two segments dropped, one cut back;
`route_native.merge_overlaps`)."""

DS2_OWNS = {"fanout": (7, 4), "hop": (10, 0), "route": (139, 22), "tap": (2, 2)}  # (6, 4) / (124, 18) before physics-subjects PR 5; re-measured 2026-10-02
"""(segments, vias) per `pcbc:` role on the emitted board, exact. `fanout` is 6 stubs and 4 vias of the
9 + 9 the fanout wrote on `U1`'s closed rows (`route["fanout"]` has 9 rows: pads 1, 2, 4, 5, 16, 15, 14,
13, 12): the dangling sweep removes the other 3 stubs and 5 vias (`DS2_PRUNED`; 6 + 3 = 9, 4 + 5 = 9).
Corrected by the third refutation round, which counted 9 where this said 10. `route` 126 -> 124
(2026-09-26, `scratchpad/native/fix3/acc`): the two route segments that lay wholly on copper of their own
net — VSS's B.Cu run along itself, 3V3's run over the fanout stub — are dropped (`merge_overlaps`)."""

DS2_PRUNED = [
    # The route stage's dangling sweep (`route_native.prune_dangling`) on ds2, exact, in removal order
    # (the second refutation round, major 7: ds2 lost five fanout vias silently). Each is copper pcbc
    # wrote that joins its net on one side only, so removing it opens nothing, and KiCad would report it
    # (`via_dangling`, `track_dangling`). Re-measured 2026-10-02: since docs/physics-subjects.md PR 5 the analog
    # nets (AIN0-3, REFP_F, REFN_F) may use vias, so U1's analog pins fan out too (15 escapes, was 9), and the
    # router reaches their pads on F.Cu: the via goes, and where the whole escape hangs off, the stub after it.
    ("via", "GPIO1", "fanout", "U1.1", "joined on F.Cu only"),
    ("via", "VSS", "fanout", "U1.5", "joined on F.Cu only"),
    ("via", "AIN3", "fanout", "U1.6", "joined on F.Cu only"),
    ("via", "AIN2", "fanout", "U1.7", "joined on F.Cu only"),
    ("via", "REFN_F", "fanout", "U1.8", "joined on F.Cu only"),
    ("via", "ADC_RX", "fanout", "U1.16", "joined on F.Cu only"),
    ("via", "ADC_TX", "fanout", "U1.15", "joined on F.Cu only"),
    ("via", "3V3", "fanout", "U1.13", "joined on F.Cu only"),
    ("via", "AIN0", "fanout", "U1.11", "joined on F.Cu only"),
    ("via", "AIN1", "fanout", "U1.10", "joined on F.Cu only"),
    ("via", "REFP_F", "fanout", "U1.9", "joined on F.Cu only"),
    ("seg", "VSS", "fanout", "U1.5", "its end at (22.83,16.35) touches nothing once the pass before removed what it met"),
    ("seg", "AIN2", "fanout", "U1.7", "its end at (24.13,16.35) touches nothing once the pass before removed what it met"),
    ("seg", "REFN_F", "fanout", "U1.8", "its end at (24.78,16.85) touches nothing once the pass before removed what it met"),
    ("seg", "ADC_RX", "fanout", "U1.16", "its end at (20.22,8.05) touches nothing once the pass before removed what it met"),
    ("seg", "ADC_TX", "fanout", "U1.15", "its end at (20.87,7.55) touches nothing once the pass before removed what it met"),
    ("seg", "AIN0", "fanout", "U1.11", "its end at (23.48,7.55) touches nothing once the pass before removed what it met"),
    ("seg", "AIN1", "fanout", "U1.10", "its end at (24.13,8.05) touches nothing once the pass before removed what it met"),
]

DS2_PLANE_MM2 = 1009.0
"""The GND pour's filled copper on B.Cu, in mm2 (KiCad 10.0.6, re-recorded 2026-09-20).

Pinned beside the island count because the count cannot see what this catches: under
`island_removal_mode 0` a fragment the fill cannot reach is **deleted**, never written as a second
`filled_polygon`, so the count stays at 1 while the copper goes (`route_verify.plane_area`, and
`docs/r2-measurements.md` S5's review, finding 9). ds2 is the board that does it — five orphans,
8.52 mm2 — and it does it with the patterns off as well, so this pins a condition rather than a
regression.

**Re-recorded 1008.42 -> 1009.0 on 2026-09-24** (fourth review, C4): the route stage sweeps the
router's dangling copper before emit, and on ds2 that is `U1.2`'s fanout via at (20.87, 16.85),
joined on B.Cu by nothing; its antipad in this pour goes with it (+0.58 mm2), the island count stays 1
and KiCad's DRC loses exactly its one `via_dangling` warning (`scratchpad/step1b/fix5/measure_moved.py`,
a build without the sweep against this tree's)."""


@pytest.mark.kicad
@pytest.mark.skipif("ds2" not in ALL, reason="the DS2 Addon is not checked out here")
def test_the_ds2_addons_hops_are_in_gen():
    """The DS2 Addon end to end, natively: its five refused hops are still five, its ten local two-pad
    nets are hops in `layout.gen.py` (each a `pcbc:hop:` group), the pour is one island with every tap
    via inside it and no via in a pad. N0 does not require the board to complete (docs/native-plan.md
    §5), so this reads the emitted board whether or not a net is left open. Built in a temp copy — the
    MaD checkout is never written to."""
    from conftest import routed_result
    from pcbc.fab import via_in_pad, via_in_pad_blockers
    from pcbc.layout_job import roles_doc
    from pcbc.route_emit import via_piece
    from pcbc.route_verify import plane_area, plane_checks, plane_islands

    out, result = routed_result("ds2", DS2 / "ds2_addon.py")
    route = next(s for s in result["steps"] if s.get("stage") == "route")
    # Restored exact after the refuters found it loosened to `.get("hop") == 5` for no N0 reason: no other
    # pattern refuses anything on ds2 natively either.
    assert route["refused"] == {"hop": 5}, route["refused"]
    gen = (out.parent / "layout.gen.py").read_text()
    hops = sorted({line.split('"pcbc:hop:')[1].split('"')[0] for line in gen.splitlines() if line.startswith('Group("pcbc:hop:')})
    assert len(hops) == 10, hops
    routed = out.read_text()
    doc = roles_doc(routed)
    vias = [via_piece(i["net"], i["reason"], tuple(i["key"][1]), float(i["w"]), float(i["drill"]), owner=i["owner"]) for i in doc.items if i["key"][0] == "via" and i["reason"] == "tap"]
    assert plane_islands(routed) == {("GND", "B.Cu"): 1}, plane_islands(routed)
    assert plane_checks(routed, vias) == [] and via_in_pad_blockers(via_in_pad(routed), load_board(DS2 / "ds2_addon.py")) == []
    # Restored from the KRT-era test (the refuters: dropped with no N0 reason): the two taps are still
    # `C5.2` and `C6.2`, and the pour's filled area is pinned beside the island count (`DS2_PLANE_MM2`).
    assert sorted(i["owner"] for i in doc.items if i["reason"] == "tap" and i["key"][0] == "via") == ["C5.2", "C6.2"], doc.items
    assert abs(plane_area(routed)[("GND", "B.Cu")] - DS2_PLANE_MM2_NATIVE) <= 0.05, (plane_area(routed), DS2_PLANE_MM2_NATIVE)
    # The census and the bar, on the emitted board (`DS2_BAR`, `DS2_OWNS`), and the dangling sweep's
    # removals, each with its reason (`DS2_PRUNED`); every removal is also a `prune:` note.
    t = route["copper_bar"]["totals"]
    for key, got in (("segments", t["segments"]), ("vias", t["vias_leftover"]), ("off45", t["off45"]), ("micro", t["micro"])):
        assert got <= DS2_BAR[key], (key, got, DS2_BAR[key], route["copper_bar"]["lines"])
    assert t["routed_mm"] <= DS2_BAR["mm"] and t["worst_detour"][1] <= DS2_BAR["detour"] + 0.05, (t["worst_detour"], t["routed_mm"])
    assert {r: (v["segments"], v["vias"]) for r, v in t["by_reason"].items() if r != "leftover"} == DS2_OWNS, t["by_reason"]
    assert t["vias_pattern"] == {r: v for r, (_s, v) in DS2_OWNS.items() if v}, t["vias_pattern"]
    pruned = route["route_stats"]["dangling_removed"]
    assert [(r["kind"], r["net"], r["reason"], r["owner"], r["why"]) for r in pruned] == DS2_PRUNED, pruned
    # The arithmetic of `DS2_OWNS`' sentence: 9 escapes written, and what the census keeps plus what the
    # sweep removed is all of them, stubs and vias each.
    assert len(route["fanout"]) == 15, route["fanout"]  # 9 before physics-subjects PR 5 (the analog pins fan out now)
    # Same-net copper laid over itself (`route_native.merge_overlaps`), 3.487 mm on ds2 before the third
    # refutation round: VSS's B.Cu route along itself and 3V3's route over the locked-in fanout stub are
    # dropped, and a 3V3 run overlapping another is cut back to it; 0 mm is left.
    assert route["route_stats"]["overlap_mm"] == 0.0
    assert sorted((r["net"], "dropped" if "dropped" in r else "trimmed") for r in route["route_stats"]["overlaps_merged"]) == [("3V3", "dropped"), ("3V3", "trimmed"), ("VSS", "dropped")], route["route_stats"]["overlaps_merged"]
    gone = {k: sum(1 for r in pruned if r["reason"] == "fanout" and r["kind"] == k) for k in ("seg", "via")}
    # 15 vias written = 4 kept + 11 removed. Stubs: 7 kept + 7 removed = 14 of the 15 escapes (one escape's stub is not
    # a separate piece on the emitted board; measured 2026-10-02, not explained here).
    assert DS2_OWNS["fanout"][0] + gone["seg"] == 14 and DS2_OWNS["fanout"][1] + gone["via"] == 15, (DS2_OWNS["fanout"], gone)
    assert len([n for n in route["notes"] if n.startswith("prune: removed ")]) == len(DS2_PRUNED), route["notes"]


DS2_PLANE_MM2_NATIVE = 975.29  # 980.41 before physics-subjects PR 5 (DS2 completes: REFN_F routes); re-measured 2026-10-02
"""`DS2_PLANE_MM2` on the native router's copper, re-recorded 2026-09-25 (KiCad 10.0.6; the ds2 build of
`scratchpad/native/fix1/acc/a`, `route_verify.plane_area`): 1009.0 -> 980.41 mm2, one island. The
copper that shares B.Cu with the pour is the native router's, not KRT's, so the fill differs; it is
pinned on an unfinished board (`REFN_F` unrouted), so routing `REFN_F` will move it, and that move
should be read."""


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


# --- B.3: the tap ---------------------------------------------------------------------------------
#
# S5. Every number below was measured on 2026-09-20 and is recorded in `docs/r2-measurements.md`
# (S5); each assertion carries its reference, so a drift names the rule it left rather than a bare
# number that moved.

TAPS = {"blinky": 1, "buck": 6, "c3_usb": 38, "node": 64, "ds2": 3}
JOINT_EXTRA = {"node": {"U2.5": 2}}
"""Pads whose joint to the pour is sized for a station's draw (PR 8): node's U2.VOUT supplies Path line 160's 1 A."""
"""How many SMD plane pads each board's post stage taps on the **placed** board (`_post`'s note on
why that is not the build's number). node's 64 is the corrected census: `docs/r2-design.md` B.3 says
72, counted per `(pad ...)` block, and KiCad writes `U1`'s QFN thermal pad as nine separate blocks
all numbered 49 and each USB-C shield as several `gr_poly` primitives — so the board has 69 plane
**pads** (64 SMD, 5 through-hole), not 77."""

SKIPPED = {"blinky": 0, "buck": 2, "c3_usb": 4, "node": 5, "ds2": 2}
"""Through-hole plane pads, skipped with a printed reason (B.3). The barrel already reaches every
copper layer, so the zone connects it and a tap would be a second hole for nothing."""


def test_every_smd_pad_on_a_plane_net_gets_a_via_of_its_own():
    """B.3, and H.2's decision: one via per pad, not per component and not clustered.

    It is the electrical answer a four-layer board exists for — every SMD ground pad over a ground
    plane gets its own via — and the via *count* objection is answered by splitting the copper bar
    (D.4), not by holding forty-seven ground pads off the ground plane. `TAP_JOIN_MM` clustering is a
    named non-goal with no measurement behind its constant (H.2).
    """
    for name in ALL:
        plan, _d, _j, _t = _post(name)
        vias = [p for p in plan.pieces if p.kind == "via" and p.reason == "tap"]
        stubs = [p for p in plan.pieces if p.kind == "seg" and p.reason == "tap"]
        # docs/physics-subjects.md PR 8: a station of a Path on the pour draws through its pad's joint, which is sized
        # for that draw. node's U2.VOUT (U2.5) supplies the 1 A of Path line 160: ceil(1 / 0.527) = 2 vias.
        extra = JOINT_EXTRA.get(name, {})
        assert len(vias) == len(stubs) == TAPS[name] + sum(n - 1 for n in extra.values()), (name, len(vias), TAPS[name], "docs/r2-measurements.md S5")
        per = {}
        for v in vias:
            per[v.owner] = per.get(v.owner, 0) + 1
        assert {o: n for o, n in per.items() if n > 1} == extra, (name, "one via per pad unless a draw on the pour needs more")


def test_a_through_hole_plane_pad_is_skipped_and_the_report_says_why():
    """B.3: a through-hole pad's barrel already reaches every copper layer, so the zone connects it.
    It is a printed note and not an omission — "there is no via on J1.1" is the kind of silence the
    gate turns into an unconnected item nobody expected."""
    plan, _d, _j, _t = _post("c3_usb")
    notes = [n for n in plan.notes if "through-hole" in n]
    assert len(notes) == SKIPPED["c3_usb"], (notes, "docs/r2-measurements.md S5")
    assert notes[0] == (
        "style: tap GND: J1.1 has no via of its own - a through-hole pad: its barrel already reaches "
        "B.Cu, so the zone connects it and a tap would be a second hole for nothing"
    ), notes[0]
    for name in ALL:
        plan, _d, _j, _t = _post(name)
        assert len([n for n in plan.notes if "through-hole" in n]) == SKIPPED[name], (name, plan.notes)


def test_a_pad_already_welded_to_the_plane_is_not_tapped_twice():
    """B.3: a closed row's escape via is a tap that has already happened — it spans F.Cu to B.Cu, so
    it lands in the plane on the way. Asked of the copper (`_welded` is `route_scene.components`'
    union-find turned round), never declared, so it is true of whatever copper is down."""
    design = load_board(_board("c3_usb"))
    job = compile_design(design)
    text = _placed("c3_usb").read_text()
    bare = pattern_copper(design, job, job.constraints, "c3_usb", stage="post", scene=scene_from_text(design, job, job.constraints, text))
    pre = pattern_copper(design, job, job.constraints, "c3_usb", stage="pre", scene=scene_from_text(design, job, job.constraints, text))
    fan, _n = fanout_pieces(design, job, "c3_usb", scene=pre.scene, claimed=pre.claimed)
    pre.scene.add(pre.scene.item_of(p) for p in fan)
    withfan = pattern_copper(design, job, job.constraints, "c3_usb", stage="post", scene=pre.scene)
    tapped = {p.owner for p in withfan.pieces if p.kind == "via"}
    assert {p.owner for p in bare.pieces if p.kind == "via"} - tapped == {"U2.2", "U3.2"}, (
        "U2.2 and U3.2 carry an escape via once the fanout has run, and a second hole beside it welds nothing new"
    )
    assert [n for n in withfan.notes if "already welded" in n] == [
        "style: tap GND: U2.2 has no via of its own - already welded to B.Cu by copper that is down (an escape via is a tap that has happened)",
        "style: tap GND: U3.2 has no via of its own - already welded to B.Cu by copper that is down (an escape via is a tap that has happened)",
    ], withfan.notes


def test_a_tap_via_is_the_smallest_via_that_carries_one_pads_share():
    """The owner's stand-in overruled B.0's branch rule and H.1 with the arithmetic R1 compiled: the
    smallest via whose current rating covers **one pad's share** of the net's current, capped at the
    net's class via.

    Measured, and the margin is why it matters: node's `GND` asks 1 A across 47 SMD pads, so a pad's
    share is 0.0213 A against the 0.527 A one 0.2 mm barrel carries at 10 C — a factor of 24. The cap
    is what keeps node routable: at the Power class's 0.8 mm ring two taps need 1.0 mm between
    centres and node's pads are not 1.0 mm apart.

    The middle branch — the class via chosen because it *covers* where the stackup via does not — is
    unreachable on all five boards, and that is arithmetic rather than luck: it needs a share in
    (0.527, 0.871] A on four layers or (0.707, 0.871] on two, and no board's amps divided by a whole
    number of pads lands there (buck's 2 A over 2 pads is 1.0, over 3 is 0.667). Swept below so a
    board that does reach it is not a surprise.
    """
    from pcbc.stackup import via_amps

    want = {"blinky": (0.5, 0.3), "buck": (0.5, 0.3), "c3_usb": (0.5, 0.3), "node": (0.35, 0.2), "ds2": (0.5, 0.3)}
    for name in ALL:
        ctx = _ctx(name)
        stack = ctx.scene.stack
        for net in sorted(ctx.scene.plane_of):
            c = ctx.cs.by_net(net)
            cap = (c.via.diameter_mm, c.via.drill_mm)
            pads = len([s for s in tap_specs(ctx) if not s.skip and s.pad.net == net])
            size, why, share = tap_via(ctx.scene, ctx.cs, net, max(pads, 1))
            assert size == want[name], (name, net, size, want[name], "docs/r2-measurements.md S5")
            assert size[1] <= cap[1], (name, net, size, cap, "the class via is the cap, never exceeded")
            assert why.startswith("the fab's standard via"), (name, net, why)
            # The rule itself, over every pad count these nets could have: the smallest covering size,
            # and the cap when nothing covers.
            for n in range(1, 120):
                got, _w, sh = tap_via(ctx.scene, ctx.cs, net, n)
                smaller = [s for s in ((stack.via_diameter, stack.via_drill), cap) if s[1] < got[1]]
                assert all(via_amps(s[1], stack.via_plating_mm) + 1e-9 < sh for s in smaller), (name, net, n, got)
                assert via_amps(got[1], stack.via_plating_mm) + 1e-9 >= sh or got == cap, (name, net, n, got, sh)


def test_a_tap_leaves_its_pad_by_fanouts_own_distance_and_is_exactly_axis_aligned():
    """B.3's geometry is `fanout.py`'s, to the digit: half the pad, the fab's `clearance_min`, half
    the via, **plus `EPS_MM`**. The two have to agree — an escape via and a tap are the same via in
    the same place for the same reason — and the stub keeps the pad centre's other coordinate
    **exactly**, which is what A.5 rule 1 is about and what S1 fixed for the escapes.

    The 0.1 um is `route_scene.pad_exits`' own, with its own reason: "a stub placed at exactly `need`
    is copper this module's own judge refuses". Without it 88 of the 105 taps on these boards sat on
    the fab floor to the last bit, and the pattern only got away with emitting copper `clears` would
    reject because `_in_a_pad` exempts the primitive it leaves from (finding 8)."""
    for name in ALL:
        plan, _d, _j, _t = _post(name)
        scene = plan.scene
        specs = {s.pad.owner: s for s in tap_specs(_ctx(name))}
        for stub in [p for p in plan.pieces if p.kind == "seg" and p.reason == "tap"]:
            assert stub.a[0] == stub.b[0] or stub.a[1] == stub.b[1], (name, stub, "A.5: a tap stub is an axis and nothing else")
            spec = specs[stub.owner]
            box = spec.pad.item.box()
            horizontal = stub.a[1] == stub.b[1]
            half = (box[2] - box[0]) / 2.0 if horizontal else (box[3] - box[1]) / 2.0
            via = next(p for p in plan.pieces if p.kind == "via" and p.owner == stub.owner)
            d = abs((stub.b[0] - stub.a[0]) if horizontal else (stub.b[1] - stub.a[1]))
            first = half + scene.stack.clearance_min + EPS_MM + via.w / 2.0
            steps = round((d - first) / scene.grid, 6)
            assert d + 1e-9 >= first and abs(steps - round(steps)) < 1e-6 and 0 <= round(steps) <= TAP_REACH_MM / scene.grid + 1e-9, (
                name, stub.owner, d, first, "B.3: the first site is fanout's distance and every other is a whole grid step further out"
            )
            # The stub starts at the centre of the ONE primitive it leaves from, which is copper, and
            # the half extent above is that primitive's. `Terminal.at` — the centre of everything the
            # owner draws — is a different point for the two pads that draw more than one shape, and
            # taking it emitted `(10.275,10.8139) -> (12.25,7.85)` on node: a diagonal, which is what
            # the self-check refused. c3_usb's `U1.49` is the same case, nine blocks 3.95 mm apart.
            assert stub.a == anchor(spec) == spec.pad.item.at(), (name, stub.owner, anchor(spec), spec.pad.at)
            inside = spec.pad.item.box()
            assert inside[0] <= stub.a[0] <= inside[2] and inside[1] <= stub.a[1] <= inside[3], (name, stub.owner, "and it is on that primitive")


def test_a_tap_is_tried_outward_first_and_one_step_further_only_when_it_has_to_be():
    """B.3's candidate order, and it is not cosmetic: with distance first the first candidate for a
    passive's pad sits **between** the passive's own two pads (`C_VBUS.2` blocked by `C_VBUS.1`,
    `R_CC1.2` by `R_CC1.1`), so the cheap site is the one that cannot work. The count is bounded and
    declared: `4 x (1 + floor(TAP_REACH_MM / grid))` — 64 on four layers, 124 on two."""
    from pcbc.patterns.tap import _sites

    for name, want in (("node", 64), ("c3_usb", 124)):
        ctx = _ctx(name)
        spec = next(s for s in tap_specs(ctx) if not s.skip)
        sites = _sites(ctx, spec)
        assert len(sites) == want, (name, len(sites), want, "B.3: 4 sides x (1 + floor(TAP_REACH_MM / grid)) sites")
        sides = [s[0] for s in sites]
        assert sides[: want // 4] == [sides[0]] * (want // 4), "every step of one side before the next side: outward first, distance second"
        assert [s[2] for s in sites[: want // 4]] == list(range(want // 4)), "and the steps out in order"
        assert sorted(set(sides)) == ["down", "left", "right", "up"], sides


def test_no_tap_via_sits_inside_a_pad():
    """The fab stage refuses a via whose copper sits inside a passive's pad outright — it wicks the
    joint — and A.4 rule 1 does not ask the question, because it skips a same-net pair. So the tap
    asks it itself, of every pad of its own net, at the number `route.py` already hands KRT as
    `--same-net-pad-clearance` (`docs/copper-plan.md`, and `fab.via_in_pad_blockers`)."""
    from pcbc.fab import via_in_pad, via_in_pad_blockers
    from pcbc.route_geom import clears

    for name in ALL:
        plan, _d, _j, _t = _post(name)
        text = write_pieces(_placed(name).read_text(), plan.pieces)
        assert via_in_pad(text) == [] and via_in_pad_blockers(via_in_pad(text)) == [], (name, via_in_pad(text))
        need = plan.scene.table.via_to_same_net_smd_pad()
        for via in [p for p in plan.pieces if p.kind == "via"]:
            ring = plan.scene.item_of(via).copper
            for it in plan.scene.items:
                if it.kind == "pad" and it.net == via.net and it.owner != via.owner and it.copper is not None:
                    assert clears(ring, it.copper, need), (name, via.owner, it.owner, "a tap clears every other pad of its own net")


def test_a_zone_header_is_read_for_the_numbers_a_fill_will_use():
    """`route_scene.zone_rules`: the `(net, layer, connect_pads clearance, min_thickness)` of every
    pour on the board, read from the header and never from the fill.

    On four layers that is the point: KRT's `planes` step writes the zone and no `filled_polygon` at
    all, so when the post stage runs the numbers exist and the copper does not.
    """
    from boardtext import zone_rules

    text = (
        '(kicad_pcb\n\t(zone\n\t\t(net "GND")\n\t\t(layer "In1.Cu")\n\t\t(connect_pads yes\n\t\t\t(clearance 0.18)\n\t\t)\n'
        "\t\t(min_thickness 0.1)\n\t)\n"
        '\t(zone\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(name "ANTENNA")\n\t\t(min_thickness 0.25)\n\t\t(keepout\n\t\t\t(tracks not_allowed)\n\t\t)\n\t)\n)\n'
    )
    rules = zone_rules(text)
    assert [(z.net, z.layer, z.pad_clearance, z.min_thickness) for z in rules] == [("GND", "In1.Cu", 0.18, 0.1)], rules
    assert len(rules) == 1, "a keepout pours no copper, so it has no antipads to merge"


def test_a_tap_never_merges_its_antipad_into_a_neighbours_in_a_plane_it_does_not_join():
    """The plane a through via does **not** join is the one it can quietly cut (finding 10).

    A GND tap punches `via + 2 * clearance` out of the 3V3 plane on In2.Cu. Two of those closer than
    the zone's `min_thickness` leave a neck KiCad deletes, and the two antipads become one slot: on
    node, eleven taps placed one per pad down `U1`'s own 0.8 mm pitch cut a continuous **8.74 mm**
    void through the 3V3 plane, and the in-copper path from (6.2,7) to (8,7) went 1.80 -> 8.09 mm.
    B.3's "one via per pad, never clustered" and the zone's arithmetic had never been introduced.

    The clearance is the larger of the zone's own `connect_pads` number and the clearance table's,
    measured rather than assumed: node writes `(clearance 0.18)` and the GND-to-3V3 class clearance is
    0.2, and the antipad on the built board comes back at radius 0.375-0.380 mm around a 0.175 mm
    ring — the class number. Taking the zone's alone leaves a neck that does not survive the fill.
    """
    from pcbc.route_scene import antipad_clash

    scene, _d, _j, _t = _scene("node")
    assert [(z.net, z.layer, z.min_thickness) for z in scene.zone_rules] == [], (
        "the placed board has no pours yet; the post stage reads them off KRT's `planes` step"
    )
    from pcbc.route_scene import ZoneRule

    scene.zone_rules = (ZoneRule(net="3V3", layer="In2.Cu", pad_clearance=0.18, min_thickness=0.1),)
    # 0.35 ring, 0.2 clearance a side, 0.1 of neck: two taps need 0.85 mm between centres, which is
    # more than U1's 0.8 mm pad pitch — so a row tapped one-per-pad in a straight line merges.
    at = (10.0, 10.0)
    ids = scene.add([scene.item_of(via_piece("GND", "tap", at, 0.35, 0.2, owner="U1.37"))])
    for d, merged in ((0.80, True), (0.8499, True), (0.8502, False), (1.0, False)):
        clash = antipad_clash(scene, (10.0, 10.0 + d), 0.35, "GND")
        assert (clash is not None) == merged, (d, clash, "0.35 + 2 * 0.2 + 0.1 = 0.85 mm between centres")
        if clash is not None:
            assert clash.rule == "plane_neck" and "3V3 plane on In2.Cu" in clash.why, clash
    # Its own net's plane is not its business: a via in the plane it joins is a connection, not a void.
    scene.zone_rules = (ZoneRule(net="GND", layer="In1.Cu", pad_clearance=0.18, min_thickness=0.1),)
    assert antipad_clash(scene, (10.0, 10.8), 0.35, "GND") is None, "a tap does not carve an antipad in the plane it welds to"
    assert antipad_clash(scene, (10.0, 10.8), 0.35, "GND", ignore=frozenset(i.id for i in ids)) is None


def test_two_taps_keep_the_fabs_hole_to_hole_and_so_does_a_mounting_hole():
    """A.4 rule 3, which does **not** skip a same-net pair: two holes are a drilling constraint
    whatever they are connected to, and `rule_severities.hole_to_hole` is `error` in every example
    `.kicad_pro` where KiCad's own default is a warning. Edge to edge, so two 0.2 mm drills need
    0.7 mm between centres on four layers and two 0.3 mm drills need 0.8 mm on two.

    With 64 taps on node this is the binding constraint, and `J1`'s four NPTH mounting holes on the
    USB-C boards are in it: they have no pad number and no copper at all, so every caller that walks
    `foot.pads` drops them — the scene carries them as `hole` items precisely so this check sees them.
    """
    import math

    for name in ALL:
        plan, _d, _j, _t = _post(name)
        scene = plan.scene
        h2h = scene.table.hole_to_hole()
        vias = [p for p in plan.pieces if p.kind == "via"]
        for i, a in enumerate(vias):
            for b in vias[i + 1 :]:
                gap = math.dist(a.a, b.a) - a.drill / 2 - b.drill / 2
                assert gap >= h2h, (name, a.owner, b.owner, round(gap, 4), h2h, "A.4 rule 3, edge to edge")
        holes = [it for it in scene.items if it.kind == "hole" and it.hole is not None]
        assert not holes or name in ("c3_usb", "node", "ds2", "buck"), name
        for via in vias:
            for it in holes:
                b = it.box()
                r = (b[2] - b[0]) / 2.0
                gap = math.dist(via.a, ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)) - r - via.drill / 2
                assert gap >= h2h - 1e-9, (name, via.owner, it.owner, round(gap, 4), h2h)


def test_the_final_stage_writes_only_what_a_declaration_asked_for_on_a_placed_board():
    """`patterns.FINAL` is `("stitch",)`, and on a **placed** board it writes only the thermal arrays.

    S2 asserted "nothing at all" with an empty `FINAL` and it was the architecture's own test: the
    stage runs for real — `pattern_copper` builds the scene over the finished copper and runs its own
    self-check — and a fresh build of all five boards came back byte-identical to the same build
    without it. S4 put the `parallel` carrier in the stage and the assertion survived unchanged,
    because its **population** is empty on a placed board: the only vias on one are pcbc's own taps
    and fanouts, and every net that carries them is either poured (and so exempt) or already rated by
    `vias_per_change`.

    **S6 is the first slice that moves this test, and the reason is the difference between a
    population and a declaration.** A `thermal` spec is not discovered from the copper; it is
    `Thermal("U1.49")` in `board.py`, and the land it names is a footprint pad that exists the moment
    the board is placed. So the two boards that declare one write their arrays here, on a board with
    no route on it at all, and the three that do not still write nothing. That is the honest split and
    it is asserted as one: `blinky`, `buck` and the DS2 Addon are empty for want of a
    `pad_prop_heatsink` land, not for want of a tuned bound.

    What this does **not** prove is what the stage writes on a routed board, where node also gains a
    rung and the arrays have KRT's copper to fit around. That is `test_examples_fab.py`'s to pin, and
    it pins the rest of the bar unchanged beside it (`docs/stitch-plan.md` §1.2's list).
    `tests/test_stitch.py` and `tests/test_thermal.py` are where the carriers' arithmetic lives.
    """
    from pcbc.patterns import FINAL, _STAGES

    assert FINAL == ("stitch",) and _STAGES["final"] is FINAL
    want = {"c3_usb": 9, "node": 12}
    for name in ALL:
        design = load_board(_board(name))
        job = compile_design(design)
        text = _placed(name).read_text()
        plan = pattern_copper(design, job, job.constraints, _uuid_name(name), stage="final", scene=scene_from_text(design, job, job.constraints, text))
        n = want.get(name, 0)
        assert len(plan.pieces) == n, (name, len(plan.pieces))
        assert plan.census == ({"thermal": {"segments": 0, "vias": n, "mm": 0.0}} if n else {}), (name, plan.census)
        assert plan.moves == () and plan.refused == {}, name
        # `CONNECTS` is False on this module, so an array claims nothing however much copper it wrote.
        assert plan.claimed == frozenset() and plan.done == frozenset() and plan.partial == frozenset(), name
        assert plan.scene is not None, f"{name}: the scene is built, not skipped"
        if not n:
            assert plan.ids == () and plan.notes == (), name


def test_every_pattern_module_says_whether_its_copper_connects_the_net_it_claims():
    """`docs/stitch-plan.md` §2k. `pattern_copper` reads `CONNECTS` before `claimed.add`, and reads
    it as an attribute so a module that forgets to declare it raises here rather than defaulting.

    The four route-writing modules say True. **`stitch` says False**, which is what the flag was put
    there for: a guard (S7) writes `GND` copper to serve an analog net, and a parallel rung (S4) is on
    its own net and still connects no pad to anything — the anchor was already joined to the rail and
    the twin is a second path beside it. Either way `claimed.add` would make `PatternPlan.done` say a
    net was routed that this module did not route, and `krt_plan` drops `done` nets from every step
    and writes `!NET` for them in `signals`."""
    from pcbc.patterns import _modules

    mods = _modules()
    assert sorted(mods) == ["chain", "hop", "stitch", "tap"], "the spine pattern was deleted (docs/physics-subjects.md KD 14)"
    for reason, mod in sorted(mods.items()):
        assert mod.CONNECTS is (reason != "stitch"), reason
        assert mod.REASON == reason, reason


def test_a_tap_the_pour_cannot_reach_is_refused_before_it_is_written():
    """D.5, and it is what makes the two-layer order safe at all: `krt_plan` writes the back pour as
    `gnd_pour`, **after** the signals, so a tap is placed into a board whose pour has not been poured.
    A via the fill retreats from connects nothing, and under `island_removal_mode 0` the fragment is
    deleted rather than kept, so the pad it was welding becomes an unconnected item the gate fails on.

    The raster is built from the copper already down, with every foreign item dilated by what the
    pour owes it, and the pour is the largest free region. A cell counts as blocked when an obstacle
    touches any part of it, so a coarser cell blocks more and the error refuses a tap that would have
    worked rather than accepting one that would not.
    """
    scene, _d, _j, _t = _scene("blinky")
    outer = pour_raster(scene, "GND", "B.Cu")
    assert outer.cell == POUR_CELL_MM and outer.free, "the board is mostly free copper before anything is routed"
    assert outer.reaches((20.0, 12.5), 0.25), "the middle of an empty board is reachable"
    ring = []
    for a, b in (((18.0, 10.0), (22.0, 10.0)), ((22.0, 10.0), (22.0, 15.0)), ((22.0, 15.0), (18.0, 15.0)), ((18.0, 15.0), (18.0, 10.0))):
        ring.append(Item(0, "track", "VCC", frozenset({"B.Cu"}), track_shape(a, b, 0.5), None, None, "VCC fence"))
    scene.add(ring)
    delattr(scene, "_pour_cache")
    fenced = pour_raster(scene, "GND", "B.Cu")
    assert not fenced.reaches((20.0, 12.5), 0.25), "a closed fence of foreign copper is a pocket the pour never floods"
    assert fenced.reaches((5.0, 5.0), 0.25), "and the rest of the board is untouched"


def test_the_plane_a_tap_lands_in_is_still_one_island():
    """D.5's first half, asked of the arbiter's own answer. KiCad writes one `(filled_polygon ...)`
    per island, so the count is the number that matters: a plane that was one island and is now two
    has had a fragment cut off, and `island_removal_mode 0` deletes it rather than keeping it — which
    turns a pad whose only connection was that fragment into an unconnected item.

    The check runs once per plane layer a via **crosses**, not once for the tapped net: every R2 via
    is a through via, so a GND tap punches a clearance hole in the 3V3 plane on In2.Cu as well as
    landing in the GND plane on In1.Cu, and it is the foreign plane a tap can quietly cut in two.
    """
    text = (
        '(kicad_pcb\n\t(zone\n\t\t(net "GND")\n\t\t(layer "In1.Cu")\n'
        "\t\t(filled_polygon\n\t\t\t(layer \"In1.Cu\")\n\t\t\t(pts\n\t\t\t\t(xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)\n\t\t\t)\n\t\t)\n"
        "\t\t(filled_polygon\n\t\t\t(layer \"In1.Cu\")\n\t\t\t(pts\n\t\t\t\t(xy 20 0) (xy 30 0) (xy 30 10) (xy 20 10)\n\t\t\t)\n\t\t)\n\t)\n)\n"
    )
    assert plane_islands(text) == {("GND", "In1.Cu"): 2}, "two filled polygons are two islands"
    z = zones(text)[0]
    assert in_zone(z, (5.0, 5.0)) and in_zone(z, (25.0, 5.0)) and not in_zone(z, (15.0, 5.0))
    via = seg_piece("GND", "tap", "F.Cu", (0.0, 0.0), (1.0, 0.0), 0.2)  # a seg is never checked
    assert plane_checks(text, [via]) == []
    assert plane_checks(text, [via_piece("GND", "tap", (15.0, 5.0), 0.35, 0.2, owner="U1.1")]) == [
        "tap GND: the via at (15,5) for U1.1 has 0/17 of its ring inside the GND plane on In1.Cu"
    ]
    assert plane_checks(text, [via_piece("GND", "tap", (5.0, 5.0), 0.35, 0.2, owner="U1.1")]) == []
    # The ring, not the centre (finding 12): a via 0.1 mm inside the island's edge has its centre in
    # the copper and a third of its ring out of it, and the check used to call that a weld.
    assert plane_checks(text, [via_piece("GND", "tap", (9.9, 5.0), 0.35, 0.2, owner="U1.2")]) == [
        "tap GND: the via at (9.9,5) for U1.2 has 12/17 of its ring inside the GND plane on In1.Cu"
    ]
    # And a net whose zone came back unfilled is a sentence, not a silent pass (finding 17): the tap
    # pattern ran, so the plane was supposed to be there.
    assert plane_checks(text, [via_piece("3V3", "tap", (5.0, 5.0), 0.35, 0.2, owner="U1.3")]) == [
        "tap 3V3: 3V3 has no filled zone on this board, so the via at (5,5) for U1.3 welds nothing"
    ]
    # The area is the number a deleted fragment moves; the island count is not (finding 9). Both
    # islands here are 100 mm2.
    assert plane_area(text) == {("GND", "In1.Cu"): 200.0}, plane_area(text)


def test_a_tap_refusal_is_a_move():
    """C.6 and B.3: soft, because the fall-through is exact — `route.py` runs KRT's own `plane_taps`
    for the nets a pad of which was refused. The sentence names the blockers worst first (the second
    is usually the reason the first cannot simply be moved), the rule of A.4 that decided each, the
    number it measured against the number it needed, what it tried, and two `board.py` edits.

    Pinned verbatim from the build, not from the placed board: on the placed board every pad of node
    still has a site, and this refusal exists because the USB pair and the constrained nets are down
    by the time the post stage runs (`docs/r2-measurements.md` S5).
    """
    ctx = _ctx("node")
    spec = next(s for s in tap_specs(ctx) if s.pad.owner == "C_VBUS.2")
    # A fixture, and it says so: the walls are wide enough to cover every one of the 64 sites, which
    # is what the real board does to this pad by the time the post stage runs. The build's own
    # sentence for the same pad is pinned in `test_node_builds_with_its_taps_in_the_plane`.
    cx, cy = spec.pad.at
    fence = [
        Item(0, "track", "USB_DN", frozenset({"F.Cu"}), track_shape((cx - 1.3, cy - 3.0), (cx - 1.3, cy + 3.0), 2.0), None, None, "USB_DN track on F.Cu"),
        Item(0, "track", "USB_DN", frozenset({"F.Cu"}), track_shape((cx + 1.3, cy - 3.0), (cx + 1.3, cy + 3.0), 2.0), None, None, "USB_DN track on F.Cu"),
        Item(0, "track", "USB_DP", frozenset({"F.Cu"}), track_shape((cx - 3.0, cy - 1.3), (cx + 3.0, cy - 1.3), 2.0), None, None, "USB_DP track on F.Cu"),
        Item(0, "track", "USB_DP", frozenset({"F.Cu"}), track_shape((cx - 3.0, cy + 1.3), (cx + 3.0, cy + 1.3), 2.0), None, None, "USB_DP track on F.Cu"),
    ]
    ctx.scene.add(fence)
    from pcbc.patterns import tap as tap_mod

    res = tap_mod.run(ctx, spec)
    assert res.pieces == () and res.refusal is not None and not res.refusal.hard, res
    assert res.refusal.move.splitlines()[0] == (
        "tap GND: C_VBUS.2 at (23.545,29.85) cannot reach the GND plane on In1.Cu with a 0.35/0.2 via "
        "(the fab's standard via: no Path declares a current into this pad of GND, and 0.527 A is what it carries)."
    ), res.refusal.move
    assert "Moves:" not in res.refusal.move and "Place(" not in res.refusal.move, res.refusal.move
    assert "Tried 64 sites (4 sides, 16 distances 0.1 mm apart)" in res.refusal.move, res.refusal.move
    assert "(rule: copper)" in res.refusal.move and "worst first" in res.refusal.move, res.refusal.move


def test_a_tap_of_the_wrong_size_fails_the_self_check():
    """D.1 item 5, now that the rule is the arithmetic and not "the stackup's": the checker asks
    `patterns.tap.tap_via` the same question the pattern asked, so the two cannot drift apart. The
    divisor is the vias actually emitted, which can only be fewer than the pads the pattern set out
    to tap — so a via that passes here would have passed there too."""
    plan, _d, job, _t = _post("node")
    assert verify_copper(plan.scene, plan.pieces, job.constraints, ids=plan.ids) == [], "node's own taps"
    bad = via_piece("GND", "tap", (30.0, 22.0), 0.8, 0.4, owner="U1.1")
    out = verify_copper(plan.scene, list(plan.pieces) + [bad], job.constraints, ids=plan.ids)
    # docs/physics-subjects.md PR 3: GND declares no current (a poured return path is not built), so no tap
    # has a share to size for and the fab's standard via is the answer whatever the count.
    want = (
        "tap GND: a 0.8/0.4 via where B.0's branch rule says 0.35/0.2 (the fab's standard via: no Path declares a "
        "current into this pad of GND, and 0.527 A is what it carries)"
    )
    assert [line for line in out if "branch rule" in line] == [want], out
    alone = verify_copper(plan.scene, [bad], job.constraints)
    assert [line for line in alone if "branch rule" in line] == [want], "one via alone is no different: no current is declared into it"


def test_the_post_stage_checks_its_own_copper_and_is_fast_enough():
    """D.1 runs unconditionally at the end of **each** stage, and F.3 item 8's budget is the two
    stages together: at most 2.0 s on node and ds2, 0.3 s on blinky. The two-layer boards pay for the
    pour raster and are the ones to watch."""
    budget = {"blinky": 300, "buck": 2000, "c3_usb": 2000, "node": 2000, "ds2": 2000}
    for name in ALL:
        plan, _d, job, _t = _post(name)
        assert verify_copper(plan.scene, plan.pieces, job.constraints, ids=plan.ids) == [], name
        assert plan.wall_ms <= budget[name], (name, plan.wall_ms, budget[name])


# --- B.4: the spine pattern was deleted (docs/physics-subjects.md PR 3, KD 14, O4): with no net carrying a
# current of its own, its population (a wide power net with no fielded path) was empty on every board.


def test_the_native_stage_order_is_pre_fanout_pours_post_router_final_relax(monkeypatch):
    """docs/native-plan.md §3.4: one scene, one pass, least freedom first — `hop` (pre), the fanout,
    the plane `Pour` objects (fields only), `tap` (post), the router, `stitch` (final), and the relaxer
    last (the `mid` stage went with the spine, docs/physics-subjects.md PR 3). Asserted by recording the
    calls `route_native.route_stage` makes on blinky."""
    import pcbc.fanout as fanout_mod
    import pcbc.route_native as rn
    import pcbc.route_relax as rr
    from pcbc.place_native import place

    seen: list[str] = []
    real_stage, real_fan, real_nets, real_pours, real_relax = rn._pattern_stage, fanout_mod.fanout_pieces, rn.route_nets, rn.plane_pours, rr.relax_pieces
    monkeypatch.setattr(rn, "_pattern_stage", lambda a, stage, *r, **k: (seen.append(stage), real_stage(a, stage, *r, **k))[1])
    monkeypatch.setattr(fanout_mod, "fanout_pieces", lambda *a, **k: (seen.append("fanout"), real_fan(*a, **k))[1])
    monkeypatch.setattr(rn, "route_nets", lambda *a, **k: (seen.append("router"), real_nets(*a, **k))[1])
    monkeypatch.setattr(rn, "plane_pours", lambda *a, **k: (seen.append("pours"), real_pours(*a, **k))[1])
    monkeypatch.setattr(rr, "relax_pieces", lambda *a, **k: (seen.append("relax"), real_relax(*a, **k))[1])
    design = load_board(_board("blinky"))
    pl = place(design, name="blinky")
    rn.route_stage(design, pl.job, pl, name="blinky")
    assert seen == ["pre", "fanout", "pours", "post", "router", "final", "relax"], seen
