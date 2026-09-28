"""The whole-board string-pull: `docs/quality-plan.md` slice 1, one assertion per decision.

Every test here is a **pure** test. The board is the native placer's in-memory footprint board
(`place_native.place`) — no KiCad, no file — and the copper it relaxes is handed to
`route_relax.relax_pieces` as pieces by the test itself. The two boards the slice was
measured on, buck and the DS2 Addon, are routed boards and their numbers live in the report, not
here.

The three decisions the panel settled by measurement each have a test that fails if it is undone:
the lexicographic accept rule (C2), the contact preserve set (C3), and the reasons the pass may
rewrite (C4). The C3 test is the important one — two independent prototypes shipped the endpoint-only
version, it looks correct in review, and it breaks nets.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.route_emit import Piece, seg_key, seg_piece
from pcbc.route_geom import gap
from pcbc.route_geom import track_shape
from pcbc.route_native import constrained_nets
from pcbc.route_relax import RELAXABLE, _seg_closest, _straighten, chains_of, holds, orphan_copper, quality, relax_pieces
from pcbc.route_scene import components
from boardtext import scene_from_text, feet_of_text

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _blinky():
    from pcbc.place_native import place

    board = EXAMPLES / "blinky" / "blinky.py"
    design = load_board(board)
    return design, compile_design(design), place(design, name="blinky").text


def _seg(net: str, a, b, w: float = 0.2, layer: str = "F.Cu", reason: str = "leftover") -> Piece:
    return seg_piece(net, reason, layer, a, b, w, uuid=f"{net}-{a}-{b}")


def _as_dicts(pieces):
    """Pieces as `copper_bar.segments` rows, which is what `chains_of` reads."""
    return [{"net": p.net, "layer": p.layer, "start": p.a, "end": p.b, "width": p.w, "length": p.mm, "locked": False} for p in pieces]


# --- C2: the accept rule ---------------------------------------------------------------------------


def test_the_staircase_collapse_is_exactly_length_neutral_which_is_why_shorter_is_the_wrong_rule():
    """`docs/quality-plan.md` C2. A monotone staircase and the two legs that replace it have the
    same length to the nanometre, so "strictly shorter and no more corners" accepted **0 of buck's
    21 chains**. Lexicographic Q accepts it on the terms above length."""
    stair = ((0.0, 0.0), (0.4, 0.0), (0.4, 0.4), (0.8, 0.4), (0.8, 0.8), (1.2, 0.8), (1.2, 1.2))
    taut = ((0.0, 0.0), (1.2, 1.2))
    lo, hi = quality(taut), quality(stair)
    assert round(hi[4], 6) == round(sum(0.4 for _ in range(6)), 6) == 2.4, "the staircase is 6 legs of 0.4 mm"
    assert round(lo[4], 6) == round(math.hypot(1.2, 1.2), 6)
    assert lo < hi, "Q must accept the collapse"
    assert lo[3] == 0 and hi[3] == 5, "the win is five corners, and the length term is a tie-break under it"
    assert round(lo[4], 6) < round(hi[4], 6), "here the diagonal is also shorter; on buck's chains it was not"


def test_quality_ranks_off_45_above_a_sharp_turn_above_a_micro_leg_above_a_corner_above_length():
    """The order of the five terms is the rule; each is a thing KiCad counts and a person sees."""
    off45 = ((0.0, 0.0), (1.0, 0.3))
    sharp = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))
    micro = ((0.0, 0.0), (0.1, 0.1), (1.0, 1.0))
    plain = ((0.0, 0.0), (1.0, 1.0))
    assert quality(plain) < quality(micro) < quality(sharp) < quality(off45)
    assert quality(off45)[0] == 1 and quality(sharp)[1] == 1 and quality(micro)[2] == 1


def test_quality_counts_the_turns_at_the_chain_s_two_ends_against_copper_that_does_not_move():
    """Without this ds2's `track_angle` went 10 -> 12 while every chain's own score improved."""
    pts = ((0.0, 0.0), (1.0, 0.0))
    straight_on = quality(pts, ends=(((-1.0, 0.0),), ()))
    diagonal = quality(pts, ends=(((-1.0, -1.0),), ()))
    right_angle = quality(pts, ends=(((0.0, -1.0),), ()))
    assert straight_on[3] == 0, "a neighbour straight across from the end is not a corner"
    assert diagonal[3] == 1 and diagonal[1] == 0, "45 degrees is a corner and not a sharp one"
    assert right_angle[3] == 1 and right_angle[1] == 1, "90 degrees is both"
    assert straight_on < diagonal < right_angle


# --- chains ----------------------------------------------------------------------------------------


def test_a_width_change_and_a_reason_change_are_both_chain_boundaries():
    """Width in the key is how `docs/quality-plan.md` section 6 keeps "class width or nothing"
    structurally; reason in the key is how `RELAXABLE` is enforced by the grouping rather than by a
    test somebody can forget."""
    pieces = [
        _seg("N", (0.0, 0.0), (1.0, 0.0), 0.2),
        _seg("N", (1.0, 0.0), (2.0, 0.0), 0.4),  # a width change
        _seg("N", (2.0, 0.0), (3.0, 0.0), 0.4, reason="spine"),  # a reason change, and not relaxable
    ]
    rows = _as_dicts(pieces)
    reason = {id(r): p.reason for r, p in zip(rows, pieces)}
    chains, left = chains_of(rows, lambda s: reason[id(s)])
    assert sorted((c.width, len(c.keys)) for c in chains) == [(0.2, 1), (0.4, 1)]
    assert [s["width"] for s in left] == [0.4], "the spine is not a chain; it is left exactly where it is"
    assert sum(len(c.keys) for c in chains) + len(left) == len(rows), "the partition is the contract"


def test_two_identical_segments_both_survive_the_walk():
    """A point-keyed walk merges them and the pass then deletes copper it never accounted for."""
    rows = _as_dicts([_seg("N", (0.0, 0.0), (1.0, 0.0)), _seg("N", (0.0, 0.0), (1.0, 0.0))])
    chains, left = chains_of(rows, lambda s: "leftover")
    assert sum(len(c.keys) for c in chains) + len(left) == 2


def test_straighten_drops_a_collinear_vertex_and_keeps_a_reversal():
    assert _straighten(((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))) == ((0.0, 0.0), (2.0, 0.0))
    assert _straighten(((0.0, 0.0), (2.0, 0.0), (1.0, 0.0))) == ((0.0, 0.0), (2.0, 0.0), (1.0, 0.0))
    assert _straighten(((0.0, 0.0), (0.0, 0.0), (1.0, 1.0))) == ((0.0, 0.0), (1.0, 1.0))


# --- C4: what may be relaxed ------------------------------------------------------------------------


def test_the_relaxable_reasons_are_the_five_with_no_gate_reading_their_geometry():
    """`route` is the native router's copper (a shape nobody's gate reads for meaning, like the old
    router's `leftover`, which stays for copper no group names)."""
    assert RELAXABLE == ("fanout", "hop", "leftover", "route", "tap")
    for barred in ("spine", "guard", "stitch", "thermal", "chain", "plane"):
        assert barred not in RELAXABLE, f"{barred}'s gate rebuilds a Piece from the sidecar's geometry"


def test_a_constrained_netreq_s_nets_are_skipped_whatever_their_copper_is_labelled():
    """buck declares `NetReq('SW', kind='switch_node')` and `NetReq('FB', kind='analog')`; both
    route on one layer with no vias, and C4 keeps this pass off. One function names them
    (`route_native.constrained_nets`): the net order and this pass read it, so the two cannot drift."""
    design = load_board(EXAMPLES / "buck" / "buck.py")
    assert constrained_nets(compile_design(design), design) == frozenset({"SW", "FB"})


@pytest.mark.parametrize("name", ["c3_usb", "node"])
def test_a_differential_pair_is_skipped_and_the_reason_is_coupling_not_skew(name: str):
    """The two boards with a pair, and the clause that was removed, measured and put back.

    Removing `autoroute == "diff_pair"` from `_skip_nets` improves **every number the arbiter
    reports**: on a fresh c3_usb build `kicad_drc` stops raising `skew_out_of_range` at all (0.6154
    -> 0.2730 mm against a declared 0.5, read with the rule tightened to `(max 0mm)` so KiCad prints
    the actual on the passing arm), `diff_pair_uncoupled_length_too_long` falls 23.2594 -> 21.0732,
    and the board sheds 66 segments, 5 off-45 legs and 52 micro legs.

    It was refused on the number no rule in this repo reads: walking `USB_DP`'s centreline at 20 um
    and asking how much of it has `USB_DN` within 0.35 mm on the same layer — the pair's own
    `diff_pair_gap` is `(opt 0.127mm)` on 0.127 mm copper — **c3_usb goes 29.722 mm coupled (78.9 %)
    to 6.654 mm (19.1 %)**, and the collapse survives every threshold tried (70.4 -> 16.2 % at 0.30
    mm, 97.0 -> 71.7 % at 1.00 mm). The halves are separate chains in `sorted(net)` order and take
    different answers to the same corner, so the skew that passed was two single-ended traces of
    equal length. c3_usb's `("GND", "B.Cu")` pour says it a second way: 1053.10 -> 1040.57 mm2, one
    island still, because two coupled tracks cut one clearance corridor through a coplanar pour and
    two separated ones cut two.

    This test is pure — `compile_design` only — and it pins the decision, not the measurement; the
    table is in `route_relax`'s history (docs/quality-plan.md).
    """
    design = load_board(EXAMPLES / name / f"{name}.py")
    skip = constrained_nets(compile_design(design), design)
    assert {"USB_DP", "USB_DN"} <= skip, (name, skip)


# --- C3, the half the contact rule could not see ------------------------------------------------------


def test_seg_closest_returns_zero_for_a_proper_crossing_because_gap_already_does():
    """Projecting the four endpoints is exact for every pair **except** a proper crossing, where it
    returns the distance between two segments that are on top of each other.

    `gap` on the same pair of 0.2 mm tracks says -0.2, i.e. contact. `_seg_closest` said 1.0, `holds`
    read that as past `reach` and recorded no anchor at all, and both chains then relaxed as if the
    other were not there."""
    a, b, d = _seg_closest((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0))
    assert d == 0.0 and a == b == (0.0, 0.0), "the witness of a crossing is the crossing"
    assert gap(track_shape((-1.0, 0.0), (1.0, 0.0), 0.2), track_shape((0.0, -1.0), (0.0, 1.0), 0.2)) <= 0.0
    # A T-junction and a collinear overlap are not crossings and the projections already name them.
    assert _seg_closest((0.0, 0.0), (2.0, 0.0), (1.0, 0.0), (1.0, 1.0))[2] == 0.0
    assert _seg_closest((0.0, 0.0), (2.0, 0.0), (1.0, 0.0), (3.0, 0.0))[2] == 0.0
    assert _seg_closest((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0))[2] == 1.0, "parallel, and 1 mm apart"


def test_two_chains_joined_only_by_a_crossing_stay_joined_and_orphan_no_copper():
    """`docs/quality-plan.md` C3's failure mode, reproduced against the shipped rule and pinned.

    Copper connects where it overlaps, and two runs can overlap with no vertex anywhere near the
    contact. With the crossing invisible, both chains came back `shapes=0 anchors=0`, relaxed apart,
    and left **1 orphaned copper item where there had been 0** — while `components(LED)` said one
    group either way, because it groups pads and the orphan has none. That is the `track_dangling
    1 -> 2` both prototypes died of."""
    design, job, text = _blinky()
    run = [(9.305, 12.315), (20.0, 12.315), (20.0, 20.0), (31.08875, 20.0), (31.08875, 12.24375)]
    spur = [(24.0, 16.0), (20.5, 16.0), (16.0, 16.0), (16.0, 8.0)]  # meets the run only at (20, 16)
    pieces = [_seg("LED", p, q) for p, q in zip(run, run[1:])] + [_seg("LED", p, q) for p, q in zip(spur, spur[1:])]
    rows = [{"net": s.net, "layer": s.layer, "start": s.a, "end": s.b, "width": s.w, "length": s.mm, "locked": False} for s in pieces]
    chains, _left = chains_of(rows, lambda s: "leftover")
    movable = frozenset(k for c in chains if len(c.pts) >= 3 for k in c.keys)
    assert all(holds(c, [], [], rows, movable).anchors for c in chains), "the crossing must anchor both sides"

    before = _after(design, job, text, pieces)
    got = _relax(design, job, text, pieces)
    after = _after(design, job, text, got.owned)
    assert orphan_copper(before, "LED") == orphan_copper(after, "LED") == 0
    assert got.stats["orphans_in"] == got.stats["orphans_out"] == 0
    vertical = [{"start": p.a, "end": p.b, "width": p.w} for p in got.owned if p.net == "LED" and p.kind == "seg" and p.a[0] == p.b[0] == 20.0]
    assert any(gap(track_shape(s["start"], s["end"], s["width"]), track_shape((20.5, 16.0), (16.0, 16.0), 0.2)) <= 0.0 for s in vertical), (
        "the spur no longer crosses the run: the contact rule is blind to a crossing again"
    )


def test_orphan_copper_measures_millimetres_because_a_count_gives_the_guard_slack():
    """`components` groups **pads**, so copper adrift from every pad changes nothing it reports —
    which is why the whole-board self-check needs this second question asked of it as well.

    And it is measured in **millimetres, not items**, because the second adversarial pass broke the
    count version: reducing the item count is this pass's whole purpose, so a count comparison gives
    the guard headroom proportional to how well the pass worked. One connected island that touches
    no pad reads 8 before and 1 after, and that slack hid the very crossing defect the guard exists
    for. Length is what tidying does not change and setting a run adrift does.
    """
    design, job, text = _blinky()
    joined = [_seg("LED", (9.305, 12.315), (31.08875, 12.24375))]
    adrift = joined + [_seg("LED", (20.0, 20.0), (24.0, 20.0))]  # touches neither pad nor the run
    for pieces, want in ((joined, 0.0), (adrift, 4.0)):
        scene = _after(design, job, text, pieces)
        assert [sorted(g) for g in components(scene, "LED")] == [["D1.2", "R1.2"]], "the pads are one group either way"
        assert orphan_copper(scene, "LED") == want, "4.0 mm of centreline"
    # The count version's blind spot, made concrete: an island that tidies from many legs to one
    # loses items and keeps its millimetres, so a count leaves slack and a length does not.
    staircase = joined + [_seg("LED", (20.0 + 0.5 * i, 20.0), (20.5 + 0.5 * i, 20.0)) for i in range(8)]
    sc = _after(design, job, text, staircase)
    assert orphan_copper(sc, "LED") == 4.0, "eight legs of the same 4 mm island measure exactly what one leg would"


# --- C3, and the end a trim moves -------------------------------------------------------------------


def test_a_trim_may_not_retract_a_run_until_its_end_cap_merely_grazes_the_pad():
    """`Holds` requires the run's **centreline** to stay inside the copper it joins, not its cap.

    "Still touching" is the weakest true statement about a joint and a rounded end cap satisfies it
    while grazing the pad outline. The `trim` in `_moves` moves a run's end, so that bound was
    reachable and the pass reached it: on buck, `R_EN.2`'s overlap went 0.1985 -> 0.0235 mm, and
    23.5 um is inside JLCPCB's etch tolerance — a joint that can open while pcbc, KiCad DRC and
    `components` all call it connected.

    blinky's `R1.2` is a roundrect whose hull is `x in [9.305, 9.575]` with `r = 0.135`, so its
    copper reaches `x = 9.71` and the rule bites at `x = 9.575 + 0.135 = 9.71` exactly."""
    design, job, text = _blinky()
    scene = scene_from_text(design, job, job.constraints, text)
    pad = next(it for it in scene.items if it.kind == "pad" and it.owner == "R1.2")
    assert pad.copper.r == 0.135 and pad.copper.pts[0] == (9.305, 12.315)
    chain = chains_of(_as_dicts([_seg("LED", (9.40, 12.5), (13.0, 12.5))]), lambda s: "leftover")[0][0]
    hold = holds(chain, [pad], [], [], frozenset())
    assert len(hold.shapes) == 1 and hold.shapes[0][1] == pad.copper.r**2, "the bound is the pad's own radius"

    inside = ((9.70, 12.5), (13.0, 12.5))  # centreline still in the pad's copper, by 10 um
    graze = ((9.80, 12.5), (13.0, 12.5))  # cap still touching, centreline 90 um outside
    assert hold.ok(inside, 0.2, {}) and not hold.ok(graze, 0.2, {})
    assert gap(pad.copper, track_shape(*graze, 0.2)) <= 0.0, (
        "the grazing candidate does still touch — which is exactly why 'still touching' is not the rule"
    )


def test_a_pad_joint_must_keep_the_process_floor_of_centreline_inside_the_pads_copper():
    """The centreline rule on its own lets a trim retract a joint to the copper's outer edge.

    `hold2 = max(shape.r**2, reach_of_the_input**2)` raises the bound only where the input was
    *already* outside the copper, so a run that started across a pad's centre may legally end up
    clipping its outline — and on fresh builds of the five boards it did, on **27 of 302 pads**,
    three of them to less than the board's own `stackup.clearance_min`: c3_usb's `C_EN.2`
    0.2700 -> 0.0760 mm and `U1.23` 0.2 -> 0.1 against a 0.127 mm floor, and ds2's `R5.2`
    0.5075 -> 0.1175. So a pad carries `floor` as well, and the centreline must stay that far inside.

    blinky's `R1.2` is the same roundrect the test above uses: hull `x in [9.305, 9.575]`,
    `r = 0.135`, copper out to `x = 9.71`. With blinky's own floor of 0.127 mm the bound falls to
    `9.575 + 0.008`, so a candidate at `x = 9.70` — legal under the centreline rule, and 10 um inside
    the copper — is refused, and one inside the hull is not.
    """
    from pcbc.stackup import get_stackup

    design, job, text = _blinky()
    floor = get_stackup(job.stackup).clearance_min
    assert floor == 0.127, "blinky's process floor, off its own stackup"
    scene = scene_from_text(design, job, job.constraints, text)
    pad = next(it for it in scene.items if it.kind == "pad" and it.owner == "R1.2")
    chain = chains_of(_as_dicts([_seg("LED", (9.40, 12.5), (13.0, 12.5))]), lambda s: "leftover")[0][0]

    bare = holds(chain, [pad], [], [], frozenset())
    kept = holds(chain, [pad], [], [], frozenset(), floor=floor)
    assert bare.shapes[0][1] == pad.copper.r**2, "without a floor the bound is the pad's own radius"
    assert kept.shapes[0][1] == (pad.copper.r - floor) ** 2, "with one it is that radius less the floor"

    edge = ((9.70, 12.5), (13.0, 12.5))  # centreline in the copper by 10 um: the old rule allowed it
    deep = ((9.575, 12.5), (13.0, 12.5))  # centreline on the hull, so 135 um inside the copper
    assert bare.ok(edge, 0.2, {}) and not kept.ok(edge, 0.2, {}), "the floor is what refuses the shallow one"
    assert bare.ok(deep, 0.2, {}) and kept.ok(deep, 0.2, {})

    # And a pad whose own copper margin is thinner than the floor cannot be asked for more than its
    # hull — c3_usb's `U1.23` and `J1.A7` are r = 0.1 against a 0.127 floor — so the bound clamps at
    # zero rather than going negative and freezing every chain that lands on such a pad.
    tight = holds(chain, [pad], [], [], frozenset(), floor=10.0)
    assert tight.shapes[0][1] == 0.0 and tight.ok(((9.4, 12.5), (13.0, 12.5)), 0.2, {})


def test_the_first_two_moves_are_trims_and_they_do_move_an_end():
    """Pinned because three docstrings used to say a run's ends could not move, and that sentence is
    why nobody asked what the trim was being held to."""
    cur = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (2.0, 1.0))
    from pcbc.route_relax import _moves

    first, second = [m for m, _ in _moves(cur)][:2]
    assert first[0] != cur[0] and first[-1] == cur[-1], "the first trim drops the leading leg"
    assert second[-1] != cur[-1] and second[0] == cur[0], "the second drops the trailing one"
    assert [drawn for _c, drawn in _moves(cur)][:2] == [None, None], "a trim draws nothing, so `blocked` is not asked"


# --- the emitter ------------------------------------------------------------------------------------


def test_seg_key_is_route_bar_key_and_copper_bar_bar_key():
    from pcbc.copper_bar import bar_key as bar
    from boardtext import bar_key as route_bar

    p = _seg("N", (2.0, 1.0), (0.0, 0.0), 0.25)
    assert seg_key("F.Cu", (2.0, 1.0), (0.0, 0.0), 0.25) == bar("seg", "F.Cu", (2.0, 1.0), (0.0, 0.0), 0.25) == route_bar(p)
    assert seg_key("F.Cu", (0.0, 0.0), (2.0, 1.0), 0.25) == seg_key("F.Cu", (2.0, 1.0), (0.0, 0.0), 0.25), "order-free"


# --- end to end, on a placed board -------------------------------------------------------------------


def _relax(design, job, text, pieces):
    return relax_pieces(design, job, job.constraints, feet_of_text(design, text), "blinky", owned=pieces)


def _after(design, job, text, pieces):
    """The scene of the footprint board with these pieces on it: what the checks read."""
    sc = scene_from_text(design, job, job.constraints, text)
    sc.add(sc.item_of(p) for p in pieces)
    return sc


def test_router_copper_is_written_unlocked_and_a_pattern_s_stays_locked():
    """The router's route is a shape the generator chose and a person may drag in KiCad's editor; a
    pattern's copper is written locked, as it always was (`route_native.to_objects`)."""
    from pcbc.route_native import to_objects

    cu, groups = to_objects([_seg("N", (0.0, 0.0), (1.0, 0.0), reason="route"), _seg("N", (1.0, 0.0), (2.0, 0.0), reason="hop")], "t")
    assert [c.locked for c in cu] == [False, True]
    assert sorted(g.f["name"] for g in groups) == ["pcbc:hop:N", "pcbc:route:N"]


def _staircase(a, b, n=8):
    """`a` to `b` as a monotone staircase of `2n` legs: what a grid router leaves behind."""
    pts = [a]
    for i in range(1, n + 1):
        x = a[0] + (b[0] - a[0]) * i / n
        y = a[1] + (b[1] - a[1]) * i / n
        pts += [(round(x, 6), round(pts[-1][1], 6)), (round(x, 6), round(y, 6))]
    return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]


def test_a_staircase_between_two_pads_collapses_and_the_net_stays_one_component():
    design, job, text = _blinky()  # LED runs from R1.2 at (9.44, 12.5) to D1.2 at (31.3075, 12.5)
    pts = _staircase((9.44, 12.5), (31.3075, 16.0)) + [(31.3075, 12.5)]
    pieces = [_seg("LED", p, q) for p, q in zip(pts, pts[1:])]
    got = _relax(design, job, text, pieces)
    assert got.stats["segments_in"] == len(pieces) and got.stats["segments_out"] < len(pieces) / 3
    after = _after(design, job, text, got.owned)
    assert [sorted(g) for g in components(after, "LED")] == [["D1.2", "R1.2"]], "the pads are still one component"
    assert got.stats["skipped_segments"] == 0 and got.stats["moved"] == 1
    assert {p.reason for p in got.owned} == {"leftover"}


def test_two_runs_of_the_same_board_are_byte_identical():
    """The determinism argument of `docs/quality-plan.md` section 6: a fixed chain order, a fixed
    scan order, first-accept, and every coordinate through `route_geom.q` exactly once."""
    design, job, text = _blinky()
    pts = _staircase((9.44, 12.5), (31.3075, 16.0)) + [(31.3075, 12.5)]
    pieces = [_seg("LED", p, q) for p, q in zip(pts, pts[1:])]
    assert _relax(design, job, text, pieces).owned == _relax(design, job, text, pieces).owned


def test_a_mid_span_contact_survives_and_the_endpoint_only_design_would_have_lost_it():
    """`docs/quality-plan.md` C3, and the reason it is written down: freezing the ends and the
    degree != 2 vertices looks correct and is known-wrong. The stub below meets the run **mid-span**,
    where the run has no vertex at all, so nothing an endpoint rule can see holds it — and pulling
    the run straight between its two ends would leave the stub welded to nothing."""
    design, job, text = _blinky()
    pts = _staircase((9.44, 12.5), (20.0, 16.0)) + _staircase((20.0, 16.0), (31.3075, 12.5))[1:]
    run = [_seg("LED", p, q) for p, q in zip(pts, pts[1:])]
    stub = _seg("LED", (20.0, 18.0), (20.0, 16.0))  # lands on the run away from every vertex of it
    got = _relax(design, job, text, run + [stub])
    after = _after(design, job, text, got.owned)
    assert [sorted(g) for g in components(after, "LED")] == [["D1.2", "R1.2"]]
    stub_shape = track_shape((20.0, 18.0), (20.0, 16.0), 0.2)

    kept = [{"start": p.a, "end": p.b, "width": p.w} for p in got.owned if p.net == "LED" and p.kind == "seg"]
    assert any(gap(stub_shape, track_shape(s["start"], s["end"], s["width"])) <= 0.0 for s in kept if s["start"] != (20.0, 18.0)), (
        "the run no longer touches the stub: the preserve set has been reduced to the endpoints"
    )
    assert len([s for s in kept if s["width"] == 0.2]) > 2, "it did not simply pull straight between the two pads"


def test_a_spine_is_not_rewritten_even_when_it_is_the_worst_copper_on_the_board():
    """C4. `build._guard_gate` and friends rebuild a `Piece` from the sidecar's geometry and
    re-check semantics that were true by construction, so a moved one is no longer what it was."""
    design, job, text = _blinky()
    pts = _staircase((9.44, 12.5), (31.3075, 16.0)) + [(31.3075, 12.5)]
    pieces = [_seg("LED", p, q, reason="spine") for p, q in zip(pts, pts[1:])]
    got = _relax(design, job, text, pieces)
    assert got.stats["skipped_segments"] == len(pieces) and got.stats["segments_out"] == len(pieces)
    assert got.owned == tuple(pieces), "not one piece of a spine moves"


def test_a_relaxed_tap_keeps_its_reason():
    """A relaxed piece is still what its pattern wrote it as: its role group (`pcbc:tap:...`) is where
    the gates read it, so pcbc's own copper would read as another's the moment it moved."""
    design, job, text = _blinky()
    pts = _staircase((9.44, 12.5), (31.3075, 16.0)) + [(31.3075, 12.5)]
    pieces = [_seg("LED", p, q, reason="tap") for p, q in zip(pts, pts[1:])]
    got = _relax(design, job, text, pieces)
    assert {p.reason for p in got.owned} == {"tap"}
    assert len(got.owned) == got.stats["segments_out"] < len(pieces)
