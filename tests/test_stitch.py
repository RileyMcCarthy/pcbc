"""S4 of `docs/stitch-plan.md`: `patterns/stitch.py`, carrier `parallel`.

The first copper pcbc writes **after** the router, and the slice that tests the `final` stage's
central claim instead of arguing it. Everything here runs on a **placed** board — `conftest.
placed_board`, pure Python, no KiCad and no KRT — with the copper a candidate has to clear written
into it by `route_emit.write_pieces`, so every number below is reproducible in a tree with no build
output in it. The numbers that need a routed board (`VIAS_PATTERN`, `OWNS`, `PLANES`, `BAR`,
`REFUSED`, the rung itself) live in `test_examples_fab.py`, which builds one.

Measured 2026-09-21. Nothing under `~/Documents/MaD` is read or written by this file.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from conftest import placed_board
from pcbc.ampacity import VIA_PARALLEL_MM, _via_clusters
from pcbc.build import _barrel_gate
from pcbc.compile import compile_design
from pcbc.copper_bar import REDUNDANT, copper_bar
from pcbc.language import load_board
from pcbc.patterns import FINAL, PatternCtx, _STAGES, pattern_copper
from pcbc.patterns import stitch as st
from boardtext import bar_key
from pcbc.route_emit import REASONS, piece_key, seg_piece, via_piece
from boardtext import write_pieces
from pcbc.route_geom import EPS_MM, is_octilinear, q
from pcbc.route_scene import ZoneRule
from boardtext import zone_rules
from pcbc.route_verify import BRANCH_REASONS, parallel_joined, verify_copper
from pcbc.stackup import via_amps, vias_per_change
from boardtext import scene_from_text

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

# One open millimetre of node's placed board, chosen once and measured: of the 32 ring sites around
# it, **24 clear** and the 8 that do not are the whole of the first radius, refused by `EPS_MM`
# against the fab's `hole_to_hole` (see `test_the_rung_is_a_via_and_one_link...`). 24 is the maximum
# any point on this board reaches, so the ring here is judged by the two limits and by nothing else.
OPEN = (44.0, 36.0)
TWIN = (44.8, 36.0)  # the first site that clears: `right`, at the second rung of the ladder


def _board(name: str) -> Path:
    return EXAMPLES / name / f"{name}.py"


def _scene(name: str, extra=()):
    """A scene over `name`'s placed board, plus whatever copper the test needs on it."""
    b = _board(name)
    design = load_board(b)
    job = compile_design(design)
    text = placed_board(name, b).read_text()
    if extra:
        text = write_pieces(text, extra)
    return design, job, scene_from_text(design, job, job.constraints, text), text


def _ctx(name: str, extra=()):
    design, job, scene, text = _scene(name, extra)
    return PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board=name, stage="final"), text


def _anchor(at=OPEN, net: str = "VBUS"):
    """One under-rated `VBUS` barrel: node's rail is 1 A and one 0.2 mm barrel carries 0.527 A."""
    return via_piece(net, "leftover", at, 0.35, 0.2, owner=f"{net} via at ({at[0]:g},{at[1]:g})")


# --- the stage ---------------------------------------------------------------------------------


def test_the_final_stage_runs_exactly_this_module():
    """`FINAL` is the registration and `_modules` is the lookup; a stage with no module is a KeyError
    at import time rather than a pattern that silently never runs."""
    assert FINAL == ("stitch",) and _STAGES["final"] == FINAL
    from pcbc.patterns import _modules

    assert _modules()["stitch"] is st
    # S8's `"plane"` is the fourth carrier and it goes **last**, which is C.1's rule and not a
    # preference: `parallel` has a ring of eight sites within 1 mm of one fixed point, `thermal` owns a
    # pad land nothing else wants, `guard` owns a corridor beside one run — and `plane` owns the whole
    # board, a lattice and an edge ring over every square millimetre two pours of one net face each
    # other across. Most freedom, so it steps around the other three rather than the other way about.
    assert st.REASON == "stitch" and st.CONNECTS is False and st.CARRIERS == ("parallel", "thermal", "guard", "plane")
    # S6: two carriers, **two reasons**, and the second is not decoration. `parallel_joined` walks
    # every `reason == "stitch"` via and asks what anchor it is a twin of; an array barrel has none,
    # so under one shared reason c3_usb's first S6 build put nine "a parallel via is not parallel"
    # failures into `build._barrel_gate` and stopped. `patterns.FINAL` registers the module and
    # `route_emit._census` keys on the piece, which is why the two can differ at all.
    assert st.THERMAL_REASON == "thermal" and "thermal" in REASONS and "stitch" in REASONS
    # S7's third carrier gets a third reason for the same reason again: a guard barrel has no anchor
    # either, and `route_verify.BRANCH_REASONS` has to judge it as the fab's standard via rather than
    # as the ground net's class one. `route_emit.REASONS` has carried the name since R1.
    assert st.GUARD_REASON == "guard" and "guard" in REASONS


def test_a_carrier_this_module_does_not_ship_raises_instead_of_writing_nothing():
    """Silently emitting nothing is how a pattern comes to pass its own tests by doing nothing, so
    `run` refuses to be asked about a carrier with no site generator.

    S4 asserted this with `thermal`, S6 shipped that, S7 shipped `guard` — so `CARRIERS` is now the
    whole set and the assertion has to be made with a name that is not in it. The **rule** is what is
    being pinned, not the name of whichever carrier had not landed yet: the next one that is designed
    before it is built must fail here rather than write silence."""
    ctx, _ = _ctx("node")
    assert st.CARRIERS == ("parallel", "thermal", "guard", "plane"), "C.1's least free first, and `run` dispatches on it"
    spec = st.StitchSpec(carrier="edge", net="VBUS", sites=(), need=1, required=True, via=(0.35, 0.2), joins=("F.Cu", "B.Cu"), owner="x", why="", bound=0)
    with pytest.raises(ValueError, match="not a carrier this module ships"):
        st.run(ctx, spec)


# --- sites_ring ---------------------------------------------------------------------------------


def test_the_ring_is_eight_directions_and_the_two_limits_are_the_compilers():
    """The candidate list is `8 x len(radii)`, and neither end of the ladder is invented.

    Measured on node: `via_pitch("VBUS","VBUS",0.2,0.35,0.2,0.35)` is **0.700** — `hole_to_hole` 0.5
    between two 0.2 mm drills — and the far end is `ampacity.VIA_PARALLEL_MM`, 1.0, so at the 0.1 mm
    router grid the ladder is 0.7/0.8/0.9/1.0 and **32 sites**. That is the same 32
    `docs/stitch-plan.md` §2(q) enumerated by hand.
    """
    ctx, _ = _ctx("node")
    assert ctx.scene.table.via_pitch("VBUS", "VBUS", 0.2, 0.35, 0.2, 0.35) == 0.7
    assert ctx.scene.grid == 0.1 and VIA_PARALLEL_MM == 1.0
    sites = st.sites_ring(ctx, OPEN, "VBUS", (0.35, 0.2), VIA_PARALLEL_MM)
    assert len(sites) == 32, sites
    dists = sorted({round(math.dist(OPEN, s), 3) for s in sites})
    assert dists == [0.7, 0.8, 0.9, 1.0], dists
    assert len(set(sites)) == len(sites), "a candidate list with a repeat in it is not 32 candidates"


def test_the_ring_is_ordered_nearest_first_and_the_order_is_the_answer():
    """Distance first, direction second — the opposite of `tap._sites`, for the opposite reason: a
    twin has no sides, and the only thing that separates two sites is how certainly
    `_via_clusters` will call the result parallel."""
    ctx, _ = _ctx("node")
    sites = st.sites_ring(ctx, OPEN, "VBUS", (0.35, 0.2), VIA_PARALLEL_MM)
    d = [math.dist(OPEN, s) for s in sites]
    # Monotone by **radius**, to within the nanometre the 45 degree offset is floored by: a diagonal
    # site is up to 1.1e-6 mm nearer than the axis site of the same radius, and never further.
    assert all(d[i] <= d[i + 1] + 2e-6 for i in range(len(d) - 1)), d
    assert sites[0] == (q(OPEN[0] + 0.7), OPEN[1]), "the first candidate is the nearest one, to the right"
    assert st._site_name(st.StitchSpec(carrier="parallel", net="VBUS", sites=sites, need=1, required=True, via=(0.35, 0.2), joins=("F.Cu", "B.Cu"), owner="", why="", bound=32, at=OPEN), sites[0]) == "right 0.7"


def test_no_site_is_outside_the_constant_the_measurement_counts_with():
    """A twin beyond `VIA_PARALLEL_MM` is copper `_via_clusters` will not count, so the pattern may
    not place one. The 45 degree offset is **floored** to the nanometre for exactly this: rounding
    `1.0 * sqrt(0.5)` to 0.707107 puts the twin 1.0000003 mm out, a third of a nanometre past the
    constant and therefore a via the rail would never be credited with.
    """
    ctx, _ = _ctx("node")
    for at in st.sites_ring(ctx, OPEN, "VBUS", (0.35, 0.2), VIA_PARALLEL_MM):
        assert math.dist(OPEN, at) <= VIA_PARALLEL_MM + 1e-12, at
        assert _via_clusters([{"at": OPEN}, {"at": at}]) == {0: 2, 1: 2}, at
    rounded = q(1.0 * math.sqrt(0.5))
    assert math.dist((0.0, 0.0), (rounded, rounded)) > VIA_PARALLEL_MM, "the rounded diagonal is the bug this floors away"


def test_every_link_a_ring_site_makes_is_octilinear_to_the_nanometre():
    """The link runs from the anchor to the twin and `verify_copper` asks `is_octilinear` of every
    path pcbc writes, so eight directions is **forced** and not chosen. Built in integer nanometres
    because `q(x + d)` and `q(y + d)` can land a nanometre apart when x and y have different
    fractional parts, and a 45 degree leg off by a nanometre is not a 45 degree leg (A.5)."""
    ctx, _ = _ctx("node")
    for start in (OPEN, (26.799936, 36.475912), (13.3335, 7.1117)):
        for at in st.sites_ring(ctx, start, "VBUS", (0.35, 0.2), VIA_PARALLEL_MM):
            assert is_octilinear((start, at)), (start, at)


# --- specs --------------------------------------------------------------------------------------


def test_the_population_is_an_under_rated_group_on_an_unpoured_rail():
    """One spec per under-rated **group**, with `vias_per_change`'s own arithmetic in it."""
    ctx, _ = _ctx("node", (_anchor(),))
    specs = st.specs(ctx)
    # S6: node declares one `Thermal()` too, and `specs` returns the carriers in `CARRIERS`' order —
    # C.1's least-free-first, which is the stage's order because `pattern_copper` adds each spec's
    # copper to the scene before the next one runs.
    assert [x.carrier for x in specs] == ["parallel", "thermal"], [x.owner for x in specs]
    s = specs[0]
    assert (s.carrier, s.net, s.need, s.required) == ("parallel", "VBUS", 1, True)
    assert s.via == (0.35, 0.2) and s.at == OPEN and s.joins == ("F.Cu", "B.Cu")
    assert s.width == 0.4, "the class width, never narrowed"
    assert s.bound == len(s.sites) == 32
    assert vias_per_change(1.0, 0.2, ctx.scene.stack.via_plating_mm, 10.0) == 2 and via_amps(0.2) == 0.527
    assert s.why == "1 x 0.2 mm barrel carries 0.527 A of VBUS's 1 A at 10 C, and vias_per_change asks 2", s.why


def test_a_poured_net_is_exempt_and_the_exemption_is_read_from_the_compiled_job():
    """The largest single number in the slice.

    At `patterns_final` the plane zones are **written but not yet filled** — KiCad's
    `--refill-zones` runs in the gate, after — so `route_verify.poured_planes` returns `()` on node's
    own final-stage board and using it would have handed this pattern every `GND` and `3V3` via group
    the router placed. `scene.plane_of` is `route_scene.plane_targets`, the compiled answer, which
    `poured_planes`' own docstring names as the right source for a pattern deciding where to put
    copper. Measured: with one injected barrel on each of the three, only the unpoured one gets a
    spec, and `GND`/`3V3` get none however many barrels they are given.
    """
    ctx, _ = _ctx("node", (_anchor(), _anchor((9.0, 9.0), "GND"), _anchor((12.0, 12.0), "3V3")))
    assert ctx.scene.plane_of == {"GND": "In1.Cu", "3V3": "In2.Cu"}
    assert [s.net for s in st.specs(ctx) if s.carrier == "parallel"] == ["VBUS"]
    # And the exemption is the parallel carrier's alone: a `thermal` spec's net **must** be poured
    # (`constraints._thermal_refusals` refuses the statement otherwise), so the two populations
    # cannot overlap on any board — which is why `specs`' order is arithmetic rather than a tuning.
    assert [s.net for s in st.specs(ctx) if s.carrier == "thermal"] == ["GND"]


def test_a_group_that_is_already_rated_gets_no_spec_at_all():
    """`add = vias_per_change - len(group)`, floored at zero. Two barrels 0.9 mm apart are one
    cluster carrying 1.054 A of a 1 A rail, so there is nothing to ask for."""
    ctx, _ = _ctx("node", (_anchor(), _anchor((OPEN[0] + 0.9, OPEN[1]))))
    assert [s.carrier for s in st.specs(ctx)] == ["thermal"], "node's declared array is the only spec left"


def test_four_of_the_five_boards_have_no_population_and_it_is_arithmetic_not_luck():
    """blinky and buck carry no via on an unpoured power net at all; c3_usb's 0.5 A and the DS2
    Addon's 0.1 A both sit under one 0.3 mm barrel's 0.707 A, so `vias_per_change` is 1 and a
    singleton is rated. Asked here of the **placed** boards, where the only vias are pcbc's own."""
    for name in ("blinky", "buck", "c3_usb"):
        ctx, _ = _ctx(name)
        assert [s for s in st.specs(ctx) if s.carrier == "parallel"] == [], name
    ctx, _ = _ctx("c3_usb", (via_piece("VBUS", "leftover", OPEN, 0.5, 0.3, owner="VBUS via"),))
    assert vias_per_change(0.5, 0.3, ctx.scene.stack.via_plating_mm, 10.0) == 1 and via_amps(0.3) == 0.707
    assert [s for s in st.specs(ctx) if s.carrier == "parallel"] == [], "a 0.5 A rail through one 0.3 mm barrel is rated, so there is nothing to stitch"


# --- the rung -------------------------------------------------------------------------------------


def test_the_rung_is_a_via_and_one_link_on_each_layer_at_the_class_width():
    """`docs/stitch-plan.md` §2(e), and the first radius is refused by `EPS_MM` on the way past.

    0.700 mm between two 0.2 mm drills leaves exactly the fab's 0.5 mm `hole_to_hole`, and `clears`
    demands `need + EPS_MM` — so the nearest rung on this board is the **second** rung of the ladder,
    0.8 mm to the right. That is the boundary failing in the safe direction: a site refused too
    strictly costs a rung and prints a move, one accepted too loosely costs a board.
    """
    ctx, _ = _ctx("node", (_anchor(),))
    res = st.run(ctx, st.specs(ctx)[0])
    assert res.refusal is None and res.candidate == "right 0.8", res.candidate
    kinds = [(p.kind, p.layer, p.w, p.drill) for p in res.pieces]
    assert kinds == [("via", ("F.Cu", "B.Cu"), 0.35, 0.2), ("seg", "F.Cu", 0.4, None), ("seg", "B.Cu", 0.4, None)], kinds
    assert res.pieces[0].a == TWIN
    for seg in res.pieces[1:]:
        assert (seg.a, seg.b) == (OPEN, TWIN), seg
    # Nine sites judged: all eight of the 0.7 mm radius, every one of them refused by `EPS_MM`
    # against `hole_to_hole`, and then the first of the 0.8 mm radius.
    assert res.net == "VBUS" and res.tried == 9, res.tried
    assert EPS_MM == 1e-4 and ctx.scene.table.hole_to_hole() == 0.5


def test_the_rungs_own_copper_passes_pcbcs_self_check():
    """`verify_copper` is what `pattern_copper` raises on, and it asks the via-size rule, the angle
    rule, the leg-length rule and every clearance of the copper just written."""
    ctx, text = _ctx("node", (_anchor(),))
    plan = pattern_copper(load_board(_board("node")), ctx.job, ctx.cs, "node", stage="final", scene=scene_from_text(load_board(_board("node")), ctx.job, ctx.cs, text))
    # Three pieces for the rung and twelve barrels for node's declared array: the whole `final` stage
    # on this board, judged in one pass by the check `pattern_copper` raises on.
    assert plan.moves == () and len(plan.pieces) == 15, plan.moves
    assert [p.reason for p in plan.pieces].count("thermal") == 12, plan.census
    assert verify_copper(plan.scene, plan.pieces, ctx.cs, ids=plan.ids) == []


def test_the_twin_takes_the_anchors_size_and_a_class_via_fails_the_check():
    """§2(f): one via must mean one thing. node's `Power` class via is 0.8/0.4 and the anchor is the
    fab's 0.35/0.2 — at the class ring two vias need 1.080 mm between centres against a
    `VIA_PARALLEL_MM` of 1.0, so a class-size partner cannot be placed at all. `_via_rules` reads the
    anchor off the board rather than off the spec, so a check handed the pattern's own numbers cannot
    bless the pattern using the wrong ones."""
    ctx, _ = _ctx("node", (_anchor(),))
    assert "stitch" in BRANCH_REASONS, "the fallback for a twin with no anchor in reach"
    # The plan's §2(f) number does not reproduce and its argument does. `via_pitch` at the class
    # ring is **0.900**, inside the window, not the 1.080 the plan quotes. What puts a class-size
    # partner out of reach is the plane antipad: node's two inner planes are cut at
    # `2 * max(0.18, between) + min_thickness` around the ring, so two 0.8 mm rings need **1.300 mm**
    # between centres against a `VIA_PARALLEL_MM` of 1.0 — while two 0.35 mm rings need 0.850 and fit.
    zones = (ZoneRule(net="GND", layer="In1.Cu", pad_clearance=0.18, min_thickness=0.1), ZoneRule(net="3V3", layer="In2.Cu", pad_clearance=0.18, min_thickness=0.1))
    ctx.scene.zone_rules = zones
    assert ctx.scene.table.via_pitch("VBUS", "VBUS", 0.4, 0.8, 0.4, 0.8) == 0.9
    assert st.antipad_pitch(ctx.scene, "VBUS", 0.8)[0] == 1.3 > VIA_PARALLEL_MM
    assert st.antipad_pitch(ctx.scene, "VBUS", 0.35)[0] == 0.85 < VIA_PARALLEL_MM
    ctx.scene.zone_rules = ()
    good = via_piece("VBUS", "stitch", TWIN, 0.35, 0.2, owner="t")
    bad = via_piece("VBUS", "stitch", TWIN, 0.8, 0.4, owner="t")
    assert verify_copper(ctx.scene, [good], ctx.cs) == []
    out = verify_copper(ctx.scene, [bad], ctx.cs)
    assert any("0.35/0.2" in line and "anchor's own size" in line for line in out), out


def test_a_required_spec_that_falls_short_writes_nothing_and_says_so_once():
    """R-S3: a rail that needs two more barrels and gets one is a rail that still cannot carry its
    current, so the answer is no copper and **one** refusal — never one per site, which is what keeps
    `REFUSED` an exact pinned number instead of a flood of 32 lines per via."""
    ctx, _ = _ctx("node", (_anchor(),))
    spec = st.specs(ctx)[0]
    hungry = st.StitchSpec(**{**spec.__dict__, "need": 7})
    res = st.run(ctx, hungry)
    assert res.pieces == () and res.refusal is not None and res.notes == ()
    assert res.refusal.pattern == "stitch" and res.refusal.hard is False
    assert "placed 2 of 7" in res.refusal.move, res.refusal.move
    assert res.refusal.move.count("cannot reach") == 1


def test_a_target_spec_applies_partially_and_says_so_in_one_style_line():
    """R-S3's other half. `parallel` sets `required=True` on every spec it makes, so this is the
    branch S6's thermal array and S7's guard will use — exercised here with a hand-made spec rather
    than left as untested code waiting for them."""
    ctx, _ = _ctx("node", (_anchor(),))
    spec = st.specs(ctx)[0]
    target = st.StitchSpec(**{**spec.__dict__, "need": 7, "required": False})
    res = st.run(ctx, target)
    assert res.refusal is None and len(res.pieces) == 6, len(res.pieces)
    assert len(res.notes) == 1, res.notes
    # S6 adds the clause after the semicolon, and it is the half that makes the note actionable: a
    # target emits **one** note and no refusal, so without it "2 of 7 placed" is a number nobody can
    # act on. It is the same measurement `_refuse` would have printed — `Clash.have`, `Clash.need`
    # and the rule of A.4 that decided it — compressed to the single worst blocker.
    assert res.notes[0] == (
        "style: stitch VBUS via at (44,36): 2 of 7 placed, 1.581 A of 1 A (above its floor); "
        "all 32 sites tried, worst blocker VBUS via at (44,36) [VBUS] at 44,36 leaving 0.500 mm of 0.500 mm (rule: hole_to_hole)"
    ), res.notes[0]
    cover = st.Coverage(
        want=7, got=2, measure=1.581, target=1.0, unit="A", floor_ok=True, why="VBUS via at (44,36)",
        blocked="all 32 sites tried, worst blocker VBUS via at (44,36) [VBUS] at 44,36 leaving 0.500 mm of 0.500 mm (rule: hole_to_hole)",
    )
    assert cover.line() == res.notes[0]
    # And the verdict word follows the **unit**, because the two carriers measure in opposite
    # directions: a rail wants more amperes than its target, a land wants fewer degrees than its
    # budget, and one sentence for both prints "above its floor" on an array that is overheating.
    hot = st.Coverage(want=9, got=4, measure=57.78, target=10.0, unit="C", floor_ok=False, why="U1.49")
    assert hot.line() == "style: stitch U1.49: 4 of 9 placed, 57.78 C of 10 C (over its budget)", hot.line()


def test_two_rungs_of_one_spec_are_judged_against_each_other_and_must_make_one_line():
    """`PatternCtx` promises the scene does not move under a candidate, so a spec placing more than
    one rung cannot ask `blocked` about its own earlier ones — they are not on the board yet. The two
    rules are asked directly instead: the via pitch, and the **turn**, because two links on one layer
    meet at the anchor and `route_verify.paths_of` walks them as one path through it, so anything but
    a straight line is copper pcbc's own self-check refuses.

    Nothing in this repo needs a second rung — every shortfall on every board is exactly one — so the
    rule is arithmetic here rather than a measurement.
    """
    ctx, _ = _ctx("node", (_anchor(),))
    spec = st.specs(ctx)[0]
    pair = st.StitchSpec(**{**spec.__dict__, "need": 2})
    res = st.run(ctx, pair)
    assert res.refusal is None, res.refusal
    twins = [p.a for p in res.pieces if p.kind == "via"]
    assert twins == [TWIN, (43.2, 36.0)], twins
    assert all(t[1] == OPEN[1] for t in twins), "collinear through the anchor, so the two links are one straight run"
    assert math.dist(twins[0], twins[1]) >= ctx.scene.table.via_pitch("VBUS", "VBUS", 0.2, 0.35, 0.2, 0.35)
    diag = (q(OPEN[0] + 0.8 * math.sqrt(0.5)), q(OPEN[1] + 0.8 * math.sqrt(0.5)))
    assert st._crowds(ctx, spec, TWIN, [diag]).startswith("0."), "0.61 mm apart: the pitch rule speaks first"
    assert st._crowds(ctx, spec, TWIN, [(OPEN[0], OPEN[1] - 0.9)]).startswith("a VBUS link"), "a right angle at the anchor"
    assert st._crowds(ctx, spec, TWIN, [(OPEN[0] - 0.9, OPEN[1])]) == "", "the exact opposite: one straight run"
    assert verify_copper(ctx.scene, res.pieces, ctx.cs) == []


def test_a_site_that_would_merge_two_groups_is_not_a_site():
    """`specs` derives every group's shortfall from the board as it stands, once, before a millimetre
    of this copper exists — that is what makes the list fixed. A twin within `VIA_PARALLEL_MM` of a
    via outside its own group merges the two under single linkage, and the *other* group's spec,
    already computed and still to run, becomes a statement about a board that no longer exists."""
    other = (OPEN[0] + 1.6, OPEN[1])
    ctx, _ = _ctx("node", (_anchor(), _anchor(other)))
    specs = tuple(sp for sp in st.specs(ctx) if sp.carrier == "parallel")
    assert [sp.at for sp in specs] == [OPEN, other], [sp.at for sp in specs]
    assert st._merges(ctx.scene, specs[0], TWIN) is True, "0.8 mm from its own anchor and 0.8 mm from the other group's"
    assert st._merges(ctx.scene, specs[0], (43.2, 36.0)) is False
    res = st.run(ctx, specs[0])
    assert [p.a for p in res.pieces if p.kind == "via"] == [(43.2, 36.0)], "it steps to the far side rather than joining them"


# --- the two layer window --------------------------------------------------------------------------


def test_the_window_is_empty_on_two_layers_and_the_refusal_names_the_stackup():
    """`docs/stitch-plan.md` §2(q): "on 2L it closes to a point that `EPS_MM` then shuts", derived
    rather than asserted.

    Measured on c3_usb with the `GND` pour `krt_plan`'s `gnd_pour` step writes — `(connect_pads
    (clearance 0.16))`, `(min_thickness 0.1)` — and the fab's 0.5/0.3 via: two rings may not sit
    closer than `2 * max(0.16, between("GND","VBUS") = 0.2) + 0.1 + 0.5` = **1.000 mm** without
    merging their antipads in the only pour the board has, and `VIA_PARALLEL_MM` is **1.000** exactly.
    The window is the single point 1.0, and `clears` wants `need + EPS_MM`, so it is shut. On node it
    is `2 * 0.2 + 0.1 + 0.35` = 0.850 against the same 1.0 — a window 0.15 mm wide, non-empty only
    because node is four layers with a 0.35 mm via.
    """
    b = _board("c3_usb")
    design = load_board(b)
    job = compile_design(design)
    anchor = via_piece("VBUS", "leftover", OPEN, 0.5, 0.3, owner="VBUS via at (44,36)")
    scene = scene_from_text(design, job, job.constraints, write_pieces(placed_board("c3_usb", b).read_text(), [anchor]))
    scene.zone_rules = (ZoneRule(net="GND", layer="B.Cu", pad_clearance=0.16, min_thickness=0.1),)
    pitch, why = st.antipad_pitch(scene, "VBUS", scene.stack.via_diameter)
    assert (pitch, why) == (1.0, "the GND plane on B.Cu, whose min_thickness is 0.1 mm"), (pitch, why)
    assert pitch >= VIA_PARALLEL_MM, "no distance is both close enough to count and far enough to keep the web"
    ctx = PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board="c3_usb", stage="final")
    spec = st.StitchSpec(
        carrier="parallel", net="VBUS", sites=st.sites_ring(ctx, OPEN, "VBUS", (0.5, 0.3), VIA_PARALLEL_MM),
        need=1, required=True, via=(0.5, 0.3), joins=("F.Cu", "B.Cu"), owner="VBUS via at (44,36)",
        why="on two layers", bound=40, at=OPEN, width=0.4,
    )
    res = st.run(ctx, spec)
    assert res.refusal is not None and res.pieces == ()
    assert "Board(layers=4)" in res.refusal.move, res.refusal.move
    assert "1 mm between two 0.5 mm rings" in res.refusal.move, res.refusal.move


def test_the_real_two_layer_boards_never_reach_that_refusal():
    """And they do not reach it because they have no **parallel** population at all, which is the
    honest reason: `REFUSED` gains no `stitch` entry on blinky, buck or c3_usb because `specs` yields
    no parallel spec on them, not because a bound was tuned until the refusal stopped firing.

    c3_usb writes copper here from S6 on, and it is the other carrier's: nine barrels under the land
    `Thermal("U1.49")` names. The two are asserted apart rather than together, because "this board
    writes nothing" and "this board has no under-rated barrel" stopped being the same sentence when a
    second carrier joined the module."""
    for name in ("blinky", "buck", "c3_usb"):
        ctx, text = _ctx(name)
        plan = pattern_copper(load_board(_board(name)), ctx.job, ctx.cs, name, stage="final", scene=scene_from_text(load_board(_board(name)), ctx.job, ctx.cs, text))
        assert plan.moves == () and plan.refused == {}, name
        assert all(p.reason == "thermal" for p in plan.pieces), (name, plan.census)
        if name != "c3_usb":
            assert (plan.pieces, plan.notes) == ((), ()), f"{name}: the final stage wrote copper on a board it had nothing to write on"


# --- parallel_joined -------------------------------------------------------------------------------


def _rung(anchor, twin, *, links=True):
    out = [via_piece("VBUS", "stitch", twin, 0.35, 0.2, owner=f"VBUS via at ({anchor[0]:g},{anchor[1]:g})")]
    if links:
        out += [seg_piece("VBUS", "stitch", L, anchor, twin, 0.4, owner=out[0].owner) for L in ("F.Cu", "B.Cu")]
    return out


def test_a_twin_with_no_links_is_not_parallel_and_the_gate_says_so():
    """**The fixture the slice turns on**, and the one check nothing else in the codebase can make.

    `ampacity._via_clusters` is single-linkage on distance with no connectivity test, so the same two
    barrels 0.8 mm apart read as a cluster of two — `2 x via_amps(0.2)` = 1.054 A of a 1 A rail —
    whether or not any copper joins them. KiCad cannot see the difference either: its unconnected
    check is pad to pad, so a via on a named net welded to nothing passes DRC; `netcheck.check_copper`
    reads pad bindings inside footprint blocks and never a segment or a via. So this is the board
    where pcbc would report that its own copper worked when it did not.
    """
    _d, _j, _s, text = _scene("node", (_anchor(),))
    joined = _rung(OPEN, TWIN)
    lone = _rung(OPEN, TWIN, links=False)
    assert _via_clusters([{"at": OPEN}, {"at": TWIN}]) == {0: 2, 1: 2}, "the measurement counts both either way"

    good = parallel_joined(write_pieces(text, joined), joined)
    assert len(good) == 1 and good[0].ok
    assert (good[0].at, good[0].anchor, good[0].mm) == (TWIN, OPEN, 0.8)
    assert good[0].joined == good[0].spans == good[0].linked == ("B.Cu", "F.Cu")
    assert good[0].line() == "VBUS rung at (44.8,36): joined to its anchor 0.8 mm away on B.Cu, F.Cu"

    bad = parallel_joined(write_pieces(text, lone), lone)
    assert len(bad) == 1 and not bad[0].ok
    assert bad[0].spans == ("B.Cu", "F.Cu") and bad[0].linked == () and bad[0].joined == ()
    assert "not on B.Cu, F.Cu" in bad[0].line() and "counts it anyway" in bad[0].line(), bad[0].line()


def test_a_twin_joined_on_one_layer_only_is_still_not_parallel():
    """Per layer, because a via spans the stack: what makes two barrels parallel is that both ends of
    both of them are the same two nodes, and a component walk that may leave through a *different*
    layer would call one link on one side "joined"."""
    _d, _j, _s, text = _scene("node", (_anchor(),))
    half = _rung(OPEN, TWIN)[:2]  # the via and the F.Cu link only
    rows = parallel_joined(write_pieces(text, half), half)
    assert len(rows) == 1 and not rows[0].ok
    assert rows[0].spans == ("B.Cu", "F.Cu") and rows[0].linked == ("F.Cu",) and rows[0].joined == ("F.Cu",), rows[0]
    none = [half[0], seg_piece("VBUS", "stitch", "B.Cu", (20.0, 20.0), (21.0, 20.0), 0.4, owner=half[0].owner)]
    rows = parallel_joined(write_pieces(text, none), none)
    assert len(rows) == 1 and not rows[0].ok and rows[0].joined == (), rows[0]


def test_the_gate_is_fatal_for_a_rung_that_is_not_one_and_for_nothing_else():
    """`build._barrel_gate`. A rail that is still short is a **note** — the pattern refused, the
    refusal is printed with its blockers, and stopping the build there would stop a board that builds
    today on a fault the tool has just been unable to repair (C.6). A rung that is not joined is
    copper pcbc itself wrote doing the opposite of what pcbc reports about it."""

    class _Doc:
        def __init__(self, items):
            self.items = items
            self.refusals = []

    _d, _j, _s, text = _scene("node", (_anchor(),))
    for links, want in ((True, 0), (False, 1)):
        pieces = _rung(OPEN, TWIN, links=links)
        items = [{"key": list(piece_key(p)), "reason": "stitch", "net": p.net, "owner": p.owner, "w": p.w, "drill": p.drill or 0.0} for p in pieces]
        gate = _barrel_gate(write_pieces(text, pieces), _Doc(items), load_board(_board("node")))
        assert len(gate["fails"]) == want, (links, gate["fails"])
        assert len(gate["rungs"]) == 1
        if want:
            assert "not parallel" in gate["fails"][0] and "pcbc bug, not a board move" in gate["fails"][0]


def test_a_rail_whose_rungs_all_went_in_and_did_not_move_is_the_one_other_fatal_case(monkeypatch):
    """The second half of `docs/stitch-plan.md` §6 row 1, narrowed so it can only say something true.

    §6 asks the gate to re-run `power_bottlenecks` on the finished board and require `kind != "via"`
    or `carries >= amps * (1 - CURVE_EPS)` for every stitched net. Taken literally that fails node
    today: two of its three groups refused, so the rail is still one barrel wide and the build would
    stop on a fault the pattern has just printed a move for — C.6's argument exactly. So the gate
    asks it only of a net where **every** spec was served: pcbc wrote every rung its own arithmetic
    asked for, the measurement did not move, and one of the two is wrong. A net with a `stitch`
    refusal in the sidecar is a board fact and stays a note.
    """
    import pcbc.ampacity as amp

    class _Doc:
        def __init__(self, items, refusals):
            self.items = items
            self.refusals = refusals

    _d, _j, _s, text = _scene("node", (_anchor(),))
    pieces = _rung(OPEN, TWIN)
    items = [{"key": list(piece_key(p)), "reason": "stitch", "net": p.net, "owner": p.owner, "w": p.w, "drill": p.drill or 0.0} for p in pieces]
    stuck = {"VBUS": {"net": "VBUS", "amps": 1.0, "carries": 0.527, "kind": "via", "zoned": False, "at_mm": "44,36 on F.Cu"}}
    monkeypatch.setattr(amp, "power_bottlenecks", lambda job, t: stuck)
    board = load_board(_board("node"))
    stitched = write_pieces(text, pieces)

    served = _barrel_gate(stitched, _Doc(items, []), board)
    assert len(served["fails"]) == 1 and "the measurement did not move" in served["fails"][0], served["fails"]

    refused = _barrel_gate(stitched, _Doc(items, [{"pattern": "stitch", "net": "VBUS"}]), board)
    assert refused["fails"] == [], refused["fails"]


# --- the census -------------------------------------------------------------------------------------


def test_redundant_copper_is_counted_everywhere_except_in_the_route_it_is_not_part_of():
    """`copper_bar.REDUNDANT`. A rung's 1.6 mm of links is copper on the board and the census must
    see all of it — `segments`, `routed_mm`, `by_reason`, `vias_pattern` — and it is **not** part of
    the net's route, so counting it against the airwire says the route got worse when not one
    nanometre of it moved. Measured on node's placed board: the detour is identical with and without
    the rung, and `pattern_mm + leftover_mm + stitch_mm == routed_mm` on every net."""
    # S6 adds `"thermal"` for the same reason and it is the same rule: an array barrel is redundant
    # thermal conductance on top of an electrical connection the tap already made, so its copper is on
    # the board, in `by_reason` and in `vias_pattern`, and not in the net's route. S7 adds `"guard"`,
    # which is the case the tuple was named for and the only one of the three that is not even on the
    # net it serves: a shield is `GND` copper placed beside a *different* net.
    # S8 adds `"plane"`, the clearest of the four: a lattice barrel's two ends are the **same
    # conductor** — two pours of one net, already joined by every through via on the board — so its
    # absence leaves nothing unconnected by construction rather than by measurement. Re-pinned here
    # with that sentence rather than left to drift.
    assert REDUNDANT == ("guard", "plane", "stitch", "thermal")
    _d, _j, _s, text = _scene("node", (_anchor(),))
    pieces = _rung(OPEN, TWIN)
    reasons = {bar_key(p): "stitch" for p in pieces}
    before = copper_bar(text, {}, {})
    after = copper_bar(write_pieces(text, pieces), reasons, {})
    assert after["totals"]["detours"] == before["totals"]["detours"], "a rung is not a detour"
    assert after["totals"]["by_reason"]["stitch"] == {"segments": 2, "vias": 1, "mm": 1.6}
    assert after["totals"]["vias_pattern"] == {"stitch": 1}
    row = after["nets"]["VBUS"]
    assert row["stitch_mm"] == 1.6 and row["pattern_mm"] == 0.0
    for net, r in after["nets"].items():
        assert abs(r["pattern_mm"] + r["leftover_mm"] + r["stitch_mm"] - r["routed_mm"]) < 2e-3, net


def test_the_answer_is_a_function_of_the_board_and_not_of_the_run():
    """Determinism, the way every other pattern is asserted: same placed board, same bytes, three
    times over — and the uuids are `stable_uuid`, so even the ids repeat."""
    once = None
    for _ in range(3):
        ctx, _ = _ctx("node", (_anchor(),))
        res = st.run(ctx, st.specs(ctx)[0])
        now = [(p.kind, p.net, p.reason, str(p.layer), p.a, p.b, p.w, p.drill, p.owner, p.uuid) for p in res.pieces]
        assert once is None or now == once
        once = now
