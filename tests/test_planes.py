"""Technique 2: the plane lattice, and the routing change that gave it a population at all.

`docs/router-plan.md` R-E1, `docs/stitch-plan.md` section 8 item 1, S8. R-E1 is one sentence —
*"plane edges stitched every 5 mm when two ground pours face each other across layers"* — and it was
deferred for a measured reason: **no board could pour one net on two facing layers.** `language.Board`
refused `planes=` below three layers outright, the old router read `job.planes` only above two, and
so the five boards were four at `(('GND','B.Cu'),)` and node at `(('GND','In1.Cu'),('3V3','In2.Cu'))`
— two pours of two *different* nets, between which a via is a short. Shipping R-E1 therefore meant
landing a routing change first, which the panel judged "larger and riskier than the stitching feature
it would enable". The four example boards are tests and the user has accepted that risk; this file is
the ledger of what the change moved, which is measured to be nothing on any of them.

Three subjects, in the order the slice built them:

1. **the enabling change** — a declared `planes=` is the board's answer on every stackup, and the
   implicit back pour is what a board that declares nothing gets;
2. **the pitch** — lambda/20 at the knee of the board's own declared fastest edge, in the stackup's
   own series Dk, with the process floor under it, and a **refusal naming the missing keyword** when
   the board has not said how fast it is;
3. **the carrier and its gate** — the edge ring then the interior lattice, and every barrel landing
   in *both* pours on the finished board.

Every board here is read **read-only**, the DS2 Addon's included (it lives outside this repo, and
nothing in `~/Documents/MaD` is written by this suite). Nothing reads `examples/**/layout/`, which is
gitignored build output: a **placed** board comes from `conftest.placed_board` (pure Python, no
KiCad) and the one test that needs a **routed** one is marked `kicad`.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.constraints import COMMON_KWARGS, return_rules
from pcbc.copper_bar import REDUNDANT
from pcbc.fanout import fanout_pieces
from pcbc.language import load_board
from pcbc.patterns import PatternCtx, merge_plans, pattern_copper
from pcbc.route_native import plane_pours
from pcbc.route_emit import REASONS, via_piece
from boardtext import WithText, with_text, write_pieces
from pcbc.route_geom import Pt
from pcbc.route_scene import plane_targets
from pcbc.route_verify import BRANCH_REASONS, plane_stitch, plane_stitch_lines
from pcbc.stackup import C_MM_PER_NS, get_stackup
import pcbc.patterns.stitch as st
from boardtext import scene_from_text

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIXTURE = ROOT / "tests" / "fixtures" / "planes" / "planes.py"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc" / "ds2_addon.py"
HAS_DS2 = DS2.exists()

EXAMPLE_BOARDS = ("blinky", "buck", "c3_usb", "node")


def _board(name: str) -> Path:
    if name == "ds2":
        return DS2
    if name == "planes":
        return FIXTURE
    return EXAMPLES / name / f"{name}.py"


def _placed(name: str) -> Path:
    from conftest import placed_board

    return placed_board(name, _board(name))


def _final_ctx(name: str):
    """The board as the `final` stage really sees it, minus KRT: the pre stage, the fanout, the mid
    stage, and the pieces pcbc owns. `test_guard.py::_routed_to_patterns`'s twin."""
    design = load_board(_board(name))
    job = compile_design(design)
    board = "ds2_addon" if name == "ds2" else name
    base = _placed(name).read_text()
    pre = pattern_copper(design, job, job.constraints, board, stage="pre", scene=scene_from_text(design, job, job.constraints, base))
    fan, _n = fanout_pieces(design, job, board, scene=pre.scene, claimed=pre.claimed)
    pre.scene.add(pre.scene.item_of(p) for p in fan)
    mid = pattern_copper(design, job, job.constraints, board, stage="mid", scene=pre.scene)
    merged = WithText(merge_plans(pre, mid), write_pieces(base, list(pre.pieces) + list(fan) + list(mid.pieces)))
    scene = scene_from_text(design, job, job.constraints, merged.text)
    ctx = PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board=board, stage="final", owned=tuple(merged.pieces) + tuple(fan))
    return ctx, merged


# --- 1. the enabling change ------------------------------------------------------------------------


def test_a_declared_plane_is_the_boards_answer_on_every_stackup_and_the_implicit_pour_is_the_default(tmp_path: Path):
    """S8's composition rule, and the two alternatives it was chosen over.

    `route_scene.plane_targets` used to be "the declared `planes=` **above** two layers, and
    `('GND','B.Cu')` on two when GND is a power net" — a declaration the router did not read below
    three layers, which `language.Board` refused outright and rightly. Making the router read it needs
    one decision: what a declared pour and the implicit one compose to on two layers.

    - **Union** would make `planes=[("GND","F.Cu")]` mean *two* pours on a board whose author asked
      for one, and leave no way at all to write "GND on the front and nothing on the back".
    - **Implicit wins** would make the declaration decoration, which is the lie the refusal named.
    - **Replacement** — this — leaves every board that declares nothing byte-identical and gives a
      2-layer author the one thing R-E1 needs.
    """
    from pcbc.language import check_board

    head = (
        "from pcbc import *\n"
        'VCC = Power("VCC"); GND = Ground("GND")\n'
        'Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz"{planes})\n'
        'NetReq("VCC", "GND", kind="power", volts=5, amps=0.1)\n'
        'Place("R1", at=(5, 5)); SchPlace("R1", left=20, top=20)\n'
    )

    def targets(planes: str) -> tuple:
        b = tmp_path / f"b{abs(hash(planes))}.py"
        b.write_text(head.format(planes=planes))
        assert not [f for f in check_board(b, pcb=False) if "planes" in f], check_board(b, pcb=False)
        return plane_targets(compile_design(load_board(b)))

    assert targets("") == (("GND", "B.Cu"),), "declare nothing and the implicit back pour is unchanged"
    assert targets(', planes=[("GND", "F.Cu")]') == (("GND", "F.Cu"),), "replacement, not union"
    assert targets(', planes=[("GND", "F.Cu"), ("GND", "B.Cu")]') == (("GND", "F.Cu"), ("GND", "B.Cu"))


def test_the_board_line_no_longer_refuses_a_two_layer_plane_and_still_refuses_a_layer_that_is_not_there(tmp_path: Path):
    """The refusal `language.Board` used to make, and the one it keeps.

    "A declaration the router never reads is a lie the board tells its author" was true of the router
    it was written against and is not true of this one, so the 2-layer half is gone — and gone rather
    than relaxed, because it would now forbid the only way to say the thing R-E1 needs. The layer
    check stays: a copper layer the stackup does not have is a lie no router change can make true.
    """
    from pcbc.language import check_board

    head = (
        "from pcbc import *\n"
        'VCC = Power("VCC"); GND = Ground("GND")\n'
        'Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz", planes={planes})\n'
        'Place("R1", at=(5, 5)); SchPlace("R1", left=20, top=20)\n'
    )
    ok = tmp_path / "ok.py"
    ok.write_text(head.format(planes='[("GND", "F.Cu"), ("GND", "B.Cu")]'))
    assert not [f for f in check_board(ok, pcb=False) if "planes" in f]
    ghost = tmp_path / "ghost.py"
    ghost.write_text(head.format(planes='[("GND", "In1.Cu")]'))
    assert check_board(ghost, pcb=False)[:1] == [
        "line 4: Board(planes=[('GND', 'In1.Cu')]): jlcpcb_2l_1oz has no copper layer In1.Cu; it has B.Cu, F.Cu"
    ], check_board(ghost, pcb=False)[:1]


def test_the_route_plan_of_every_example_board_is_unmoved_by_the_enabling_change():
    """**The acceptance of S8's first half, and it is a `pours == what it always was` proof.**

    The change moves copper only where a board declares a pour the old router would have ignored, and
    no board in this repo does: four are two layers with no `planes=` at all and node is four layers,
    where `planes=` was already read. So `plane_targets` is byte-identical on all five and every KRT
    step that consumes it takes the same arguments. Measured 2026-09-21, and pinned here rather than
    argued, because "this routing change is larger and riskier than the feature it enables" is exactly
    the objection `docs/stitch-plan.md` section 8 item 1 made.
    """
    want = {
        "blinky": (("GND", "B.Cu"),),
        "buck": (("GND", "B.Cu"),),
        "c3_usb": (("GND", "B.Cu"),),
        "node": (("GND", "In1.Cu"), ("3V3", "In2.Cu")),
    }
    for name, pours in want.items():
        job = compile_design(load_board(_board(name)))
        assert plane_targets(job) == pours, name
    # The native route stage turns that one rule into `Pour` objects (`route_native.plane_pours`):
    # one per target, clearance the Default class's, connected solid, inset by the stackup's edge
    # clearance — the fields every shipped plane carried (tests/fixtures/native/krt_baseline.json).
    got = {}
    for name in EXAMPLE_BOARDS:
        design = load_board(_board(name))
        job = compile_design(design)
        got[name] = [(p.net, p.layer, p.clearance, p.connect, p.points[0]) for p in plane_pours(design, job, name)]
    assert got == {
        "blinky": [("GND", "B.Cu", 0.16, "solid", (0.3, 0.3))],
        "buck": [("GND", "B.Cu", 0.16, "solid", (0.3, 0.3))],
        "c3_usb": [("GND", "B.Cu", 0.16, "solid", (0.3, 0.3))],
        "node": [("GND", "In1.Cu", 0.18, "solid", (0.3, 0.3)), ("3V3", "In2.Cu", 0.18, "solid", (0.3, 0.3))],
    }, got


def test_a_two_layer_board_that_declares_two_pours_gets_both_as_objects():
    """The branch S8 created: the route stage pours what `plane_targets` says, both layers of one net."""
    design = load_board(FIXTURE)
    job = compile_design(design)
    assert [(p.net, p.layer) for p in plane_pours(design, job, "planes")] == [("GND", "F.Cu"), ("GND", "B.Cu")]


def test_plane_of_is_the_outermost_pour_and_not_the_order_the_author_wrote(tmp_path: Path):
    """`Scene.plane_of` had one answer per net until S8 and now can have two. It takes the **outermost**
    — the first in `Stackup.copper_layers()` — rather than whichever the author wrote last, which is
    what a dict comprehension would have done. It keeps node's taps on In1.Cu if node ever pours GND
    on both inner layers, so an experiment there moves the pour and not the 62 taps."""
    from pcbc.route_scene import _plane_of

    stack = get_stackup("jlcpcb_4l_1oz")
    assert _plane_of(stack, (("GND", "In2.Cu"), ("GND", "In1.Cu"))) == {"GND": "In1.Cu"}
    assert _plane_of(stack, (("GND", "In1.Cu"), ("GND", "In2.Cu"))) == {"GND": "In1.Cu"}
    two = get_stackup("jlcpcb_2l_1oz")
    assert _plane_of(two, (("GND", "B.Cu"), ("GND", "F.Cu"))) == {"GND": "F.Cu"}
    assert _plane_of(two, (("GND", "B.Cu"),)) == {"GND": "B.Cu"}


# --- 2. the pitch ----------------------------------------------------------------------------------


def test_the_pitch_is_derived_and_every_term_in_it_is_somebody_elses_number():
    """R-E1 writes "every 5 mm" and the panel flagged it as unjustified. This is the arithmetic that
    replaces it, term by term, and the point of the test is that only one term is pcbc's own ratio:

        f_knee  = KNEE_OF_RISE / t_rise     Johnson & Graham; t_rise is NetReq(rise_ps=), no default
        v       = C_MM_PER_NS / sqrt(Dk)    Stackup.dielectric_between, the series Dk it measures
        lambda  = v / f_knee
        pitch   = lambda / PITCH_FRACTION   the lambda/20 rule, stated and citable

    Measured 2026-09-21: both stackups here put Dk **4.6** between the layers a facing pair would use,
    so the pitch is a property of the declared edge and barely one of the board — **6.98895 mm** at a
    500 ps edge on either. R-E1's 5 mm approximates that as lambda/28, for no stated reason.
    """
    four, two = get_stackup("jlcpcb_4l_1oz"), get_stackup("jlcpcb_2l_1oz")
    assert st.cavity_pair(four, ("In1.Cu", "In2.Cu")) == ("In1.Cu", "In2.Cu")
    assert st.cavity_pair(two, ("F.Cu", "B.Cu")) == ("F.Cu", "B.Cu")
    # The highest series Dk wins, because a cavity mode is `v / 2L` and `v = c / sqrt(Dk)`: the plate
    # separation does not enter it at all, so the only term a pair contributes is its Dk.
    assert st.cavity_pair(four, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")) == ("F.Cu", "B.Cu")
    assert four.dielectric_between("F.Cu", "B.Cu")[1] == 4.6345 > four.dielectric_between("In1.Cu", "In2.Cu")[1]

    assert four.dielectric_between("In1.Cu", "In2.Cu") == (1.065, 4.6)
    assert two.dielectric_between("F.Cu", "B.Cu") == (1.53, 4.6)
    v = st.cavity_velocity(four, ("In1.Cu", "In2.Cu"))
    assert round(v, 4) == 139.779 and round(C_MM_PER_NS / math.sqrt(4.6), 4) == 139.779
    assert st.knee_ghz(500) == 1.0 and st.KNEE_OF_RISE == 0.5 and st.PITCH_FRACTION == 20.0
    assert round(v / st.knee_ghz(500) / st.PITCH_FRACTION, 5) == 6.98895
    assert round(st.cavity_velocity(two, ("F.Cu", "B.Cu")), 4) == 139.779, "both stackups, one answer"


def test_the_board_supplies_no_edge_rate_and_pcbc_refuses_to_invent_one():
    """**The half of the derivation that is a refusal, and the reason it is not a default.**

    `docs/stitch-plan.md` section 8 item 3 threw out `NetReq(rise_ps=)`'s per-kind defaults — four
    "pcbc default" guesses for `clock`, `spi`, `i2c` and `switch_node` — because a pitch scales
    linearly with them, so four guesses would be four invented numbers reaching the geometry. S8 keeps
    that refusal and adds the keyword with **no default anywhere**: not in `language.NetReq`, not in
    `PRESETS`, not in `constraints._numbers`.

    Measured 2026-09-21: `ConstraintSet.fastest_edge()` is `None` on all five boards, because nothing
    in this repo has ever declared an edge rate. So the answer on a real board is a sentence naming the
    keyword, not a pitch.
    """
    assert "rise_ps" in COMMON_KWARGS, "an edge rate belongs to whichever net has one, not to a kind"
    for name in EXAMPLE_BOARDS + (("ds2",) if HAS_DS2 else ()):
        cs = compile_design(load_board(_board(name))).constraints
        assert cs.fastest_edge() is None, name
        assert all(c.rise_ps is None for c in cs.constraints), name
    from pcbc.constraints import PRESETS

    assert all(not hasattr(p, "rise_ps") for p in PRESETS.values())
    # And the fixture, which declares one, folds the fastest over the whole board rather than per net.
    cs = compile_design(load_board(FIXTURE)).constraints
    assert cs.fastest_edge() == ("CLK", 500.0)
    assert cs.by_net("GND").rise_ps is None, "the cavity's pitch is the board's fastest edge, not GND's"


def test_the_refusal_names_the_missing_keyword_and_is_a_move_a_board_can_act_on(tmp_path: Path):
    """Every refusal in this repo is a move ending in a `board.py` edit, and this one is: the edit is
    `rise_ps=` on the NetReq of the fastest net. Soft, because nothing is unconnected without a plane
    stitch — R-S1's redundant copper, in its purest form (a barrel whose two ends are the same
    conductor).

    Exercised on the fixture with its one `rise_ps=` taken out, so the refusal is measured on a board
    that otherwise gets a lattice rather than on a board that could never have had one.
    """
    work = tmp_path / "noedge.py"
    work.write_text(FIXTURE.read_text().replace(', rise_ps=500', ''))
    from conftest import placed_board

    design = load_board(work)
    job = compile_design(design)
    base = placed_board("planes_noedge", work).read_text()
    pre = pattern_copper(design, job, job.constraints, "noedge", stage="pre", scene=scene_from_text(design, job, job.constraints, base))
    scene = scene_from_text(design, job, job.constraints, write_pieces(base, pre.pieces))
    ctx = PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board="noedge", stage="final", owned=tuple(pre.pieces))
    (spec,) = st._plane_specs(ctx)
    assert spec.sites == () and spec.bound == 0 and spec.pitch == 0.0
    res = st.run(ctx, spec)
    assert res.pieces == () and res.refusal is not None
    assert res.refusal.rule == "no_edge_rate" and res.refusal.hard is False and res.refusal.pattern == "plane"
    assert "no net declares NetReq(rise_ps=)" in res.refusal.move
    assert "will not invent" in res.refusal.move
    assert 'add rise_ps= to NetReq("CLK", ..., rise_ps=...)' in res.refusal.move, "it names the fastest-kind net it can see"


def test_the_process_floor_is_under_the_pitch_and_it_binds_only_below_an_impossible_edge():
    """A lattice at a pitch below `thermal_pitch` is two barrels whose antipads have merged, which
    deletes the plane the stitch exists to tie (section 2(h)'s measured -0.050 mm web). So the floor
    is the process's number and an edge fast enough to ask for less gets the floor plus a sentence.

    Measured 2026-09-21, the floor binds far below anything these boards carry: the two-layer fixture
    at **57.2 ps** and a 4-layer board with no foreign pour at **50.1 ps**. A 500 ps edge asks for
    6.98895 mm, which is nine floors.
    """
    ctx, _m = _final_ctx("planes")
    floor, why = st.thermal_pitch(ctx.scene, "GND", (0.5, 0.3))
    assert floor == 0.800101 and "hole_to_hole" in why
    v = st.cavity_velocity(ctx.scene.stack, ("F.Cu", "B.Cu"))
    binds = st.KNEE_OF_RISE * 1000.0 * floor * st.PITCH_FRACTION / v
    assert round(binds, 1) == 57.2, binds
    got = st.stitch_pitch(ctx.scene, ctx.cs, ("F.Cu", "B.Cu"), "GND", (0.5, 0.3))
    assert isinstance(got, tuple) and got[0] == 6.988949, got
    assert "lambda/20 at the 500 ps edge CLK declares" in got[1] and "Dk 4.6" in got[1]
    # And below the floor the floor wins, with the sentence that says the stitch is coarser than the
    # edge asked for — never a lattice that eats the pour it ties.
    fast = compile_design(load_board(FIXTURE)).constraints
    from dataclasses import replace

    quick = replace(fast, constraints=tuple(replace(c, rise_ps=10.0 if c.net == "CLK" else None) for c in fast.constraints))
    slow = st.stitch_pitch(ctx.scene, quick, ("F.Cu", "B.Cu"), "GND", (0.5, 0.3))
    assert isinstance(slow, tuple) and slow[0] == floor
    assert "the process floor, coarser than the 0.1398 mm" in slow[1], slow[1]


# --- 3. the carrier --------------------------------------------------------------------------------


def test_the_candidate_list_is_the_edge_ring_then_the_interior_lattice():
    """R-E1's two halves in R-E1's own order, and C.1's least-free-first agrees with the letter of the
    requirement for once: a ring site sits on one line, an interior site is one of a grid.

    An unstitched plane pair is a parallel-plate cavity whose **open perimeter** is the aperture it
    radiates from, so the ring is not decoration on a lattice; it is what R-E1 names. Both halves run
    on the one derived pitch, because two pitches would be two numbers where the arithmetic supplies
    one.

    Measured on the fixture: 20 ring sites around a 38.9 x 28.9 mm rectangle and 30 interior, 50 in
    all, with the ring first and no duplicate between them.
    """
    ctx, _m = _final_ctx("planes")
    pitch = 6.988949
    rect = st.plane_rect(ctx.scene, "GND", 0.5)
    assert rect == (0.55, 0.55, 39.45, 29.45), "outline (0.3) inset by the 0.25 ring; no zone yet at the placed stage"
    sites = st.sites_plane(ctx, "GND", pitch, 0.5)
    ring = st.sites_along(ctx, ((rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3]), (rect[0], rect[1])), pitch)
    grid = st.sites_grid(rect, pitch, 0.5)
    assert len(ring) == 20 and len(grid) == 30 and len(sites) == 50
    assert sites[: len(ring)] == ring, "the ring is proposed first"
    assert len(set(sites)) == len(sites), "deduplicated in order"
    assert all(x in (rect[0], rect[2]) or y in (rect[1], rect[3]) for x, y in ring), "every ring site is on the rectangle"


def test_the_lattice_is_integer_nanometres_and_its_neighbours_are_exactly_the_pitch():
    """S6's finding one carrier on, and it matters more here, not less: `q(cx + (i - (nx-1)/2) *
    pitch)` rounds the two sites of a 2-wide row **both inward** to the same even nanometre, and a
    lattice over a 60 x 45 mm board has far more neighbouring pairs than a lattice over a 1.45 mm pad.
    Anchoring the first site and stepping by an exact integer makes every separation the pitch to the
    nanometre."""
    from pcbc.patterns import _nm

    rect = (0.55, 0.55, 39.45, 29.45)
    pitch = 6.988949
    grid = st.sites_grid(rect, pitch, 0.5)
    step = _nm(pitch)
    xs = sorted({_nm(x) for x, _y in grid})
    ys = sorted({_nm(y) for _x, y in grid})
    assert len(xs) == 6 and len(ys) == 5
    assert all(b - a == step for a, b in zip(xs, xs[1:])), "exact integer nanometres across"
    assert all(b - a == step for a, b in zip(ys, ys[1:])), "exact integer nanometres down"
    assert grid == tuple(sorted(grid, key=lambda p: (p[1], p[0]))), "row-major, y then x"
    # Centred: the grid's own centre is the rectangle's, to within the half nanometre an odd count
    # cannot place.
    assert abs((min(xs) + max(xs)) / 2e6 - (rect[0] + rect[2]) / 2) < 1e-6


def test_the_ring_is_inset_by_the_pours_own_min_thickness_and_the_gate_is_what_found_that():
    """**The one thing in this slice only the finished board could have said.**

    Built with the ring at `outline + dia/2` and routed end to end, 2026-09-21: the fill came back at
    0.3005 mm from the board edge and every one of the 20 ring barrels had its outer ring sample at
    exactly 0.3000, so `route_verify.plane_stitch` reported "20 of 41 lattice via(s) do not land in
    every pour they tie" and the build stopped. `route_verify.pour_raster` had said all 20 were
    reachable — it skips `kind in ("lane", "edge")` and rasters `scene.outline` itself, so it cannot
    see the board edge at all.

    The honest inset is the pour's own `min_thickness` off `Scene.zone_rules`: copper thinner than
    that is what KiCad is entitled to remove from a fill, so a ring with less than `min_thickness` of
    pour around it is standing where the fill need not be. A derived number, not a margin: no zone, no
    term, which is what the placed-stage rectangle above shows.
    """
    from pcbc.route_scene import ZoneRule

    ctx, _m = _final_ctx("planes")
    assert ctx.scene.zone_rules == (), "at the placed stage no pour has been written yet"
    assert st.plane_rect(ctx.scene, "GND", 0.5) == (0.55, 0.55, 39.45, 29.45)
    poured = ZoneRule(net="GND", layer="B.Cu", pad_clearance=0.16, min_thickness=0.1)
    ctx.scene.zone_rules = (poured, ZoneRule(net="GND", layer="F.Cu", pad_clearance=0.16, min_thickness=0.1))
    assert st.plane_rect(ctx.scene, "GND", 0.5) == (0.65, 0.65, 39.35, 29.35), "the 0.1 mm the fill may drop"
    assert st.plane_rect(ctx.scene, "3V3", 0.5) == (0.55, 0.55, 39.45, 29.45), "another net's zone is not this one's"


def test_plane_reach_is_the_span_across_the_widest_hole_and_not_a_count():
    """"43 of 50 placed" is compatible with seven sites lost one at a time, which barely changes the
    stitch, and with seven lost in one block, which is a hole several pitches across in the middle of
    the cavity. The `Coverage` measure separates them: the hole **radius** (the furthest a wanted site
    is from a via that exists), doubled, because a hole of radius r is a span of 2r between the copper
    on its two sides — one missing interior site leaves its two row neighbours exactly two pitches
    apart, which is the row's own spacing read off the board."""
    p = 7.0
    grid = tuple((p * i, p * j) for j in range(5) for i in range(5))
    assert st.plane_reach(grid, grid, p) == p, "a complete lattice reports its own pitch"
    one = tuple(s for s in grid if s != (p * 2, p * 2))
    assert st.plane_reach(grid, one, p) == 2 * p, "one interior site lost is a two-pitch span"
    block = tuple(s for s in grid if not (p <= s[0] <= 3 * p and p <= s[1] <= 3 * p))
    assert st.plane_reach(grid, block, p) == 4 * p, "a 3x3 block lost is four pitches"
    assert st.plane_reach(grid, (), p) == math.inf, "nothing placed is not a division by zero"
    assert st.plane_reach((), (), p) == p


def test_the_note_states_the_shortfall_as_the_edge_the_lattice_still_stitches_for():
    """R-S3's target half, in the unit the pitch was derived in. A plane spec's `need` is its **whole**
    candidate list — R-E1 asks for a pitch and not for a count — so every site is tried, "it placed
    them all" is the good outcome rather than the silent one, and the note is emitted either way.

    Measured on the placed fixture: 43 of 50, and the one interior site `C3.1`'s `3V3` pad sits on
    takes the worst span to two pitches, so the lattice is a lambda/20 stitch up to **0.5 GHz** where
    the board's own `rise_ps=500` asks for 1 GHz. The six that lost to `_crowds` cost nothing, because
    the barrel that won is 0.485 mm from where the loser wanted to be.
    """
    ctx, _m = _final_ctx("planes")
    (spec,) = st._plane_specs(ctx)
    assert spec.carrier == "plane" and spec.required is False and spec.need == spec.bound == 50
    assert spec.pours == ("F.Cu", "B.Cu") and spec.joins == ("F.Cu", "B.Cu")
    res = st.run(ctx, spec)
    assert len([p for p in res.pieces if p.kind == "via"]) == 43
    assert all(p.kind == "via" for p in res.pieces), "a lattice barrel carries no link: its two ends are the same conductor"
    assert res.notes == (
        "style: stitch GND plane: 43 of 50 placed, 0.5 GHz of 1 GHz (under its floor); all 50 sites "
        "tried, worst blocker C3.1 [3V3] at 23.49,15 leaving -0.500 mm of 0.200 mm (rule: copper)",
    ), res.notes


def test_a_lattice_barrel_is_a_branch_a_redundancy_and_still_on_the_rail():
    """The three vocabularies, asked of the fourth carrier. `BRANCH_REASONS` is about the **via**: a
    lattice barrel's two ends are the same conductor, so it carries no net current across layers and
    takes the fab's standard via — which also keeps the ring, and therefore the antipad it would cut
    in any foreign plane, as small as the fab allows. `REDUNDANT` is about the **route**: its absence
    leaves nothing unconnected. `NOT_A_RAIL` is about the **current**, and it is not one: a lattice is
    on the very net whose conduction it improves."""
    from pcbc.ampacity import NOT_A_RAIL

    assert "plane" in REASONS and "plane" in BRANCH_REASONS
    assert REDUNDANT == ("guard", "plane", "stitch", "thermal")
    assert NOT_A_RAIL == ("guard",), "a guard is the only one not on the net whose current is in question"


# --- 4. the gate, and the boards that write nothing -------------------------------------------------


def test_plane_stitch_is_the_check_and_it_can_fail():
    """The one claim nothing else on this board can make: **every barrel lands in BOTH pours.**

    A via welded to the front plane and standing in a clearance hole in the back one still reads as
    connected everywhere else — KiCad's unconnected-items check is pad to pad, `netcheck.check_copper`
    reads pad bindings inside footprint blocks and never a via, `verify_copper` asks about clearance,
    angle and size, `copper_bar` counts it like any other via — and it ties the two planes not at all,
    which is the entire technique. So the failing case is constructed here rather than hoped for.

    It is `plane_checks`' containment with one word changed and the word is load-bearing: **every**
    poured layer of the net, not *any* of them. A tap only has to reach its own plane; a lattice
    barrel exists to join two.
    """
    text = (
        '\n\t(zone (net 1) (net_name "GND") (layer "F.Cu")\n'
        "\t\t(filled_polygon (layer \"F.Cu\") (pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20)))\n\t)"
        '\n\t(zone (net 1) (net_name "GND") (layer "B.Cu")\n'
        "\t\t(filled_polygon (layer \"B.Cu\") (pts (xy 0 0) (xy 10 0) (xy 10 20) (xy 0 20)))\n\t)"
    )
    inside = via_piece("GND", "plane", (5.0, 5.0), 0.5, 0.3, owner="GND on F.Cu + B.Cu")
    front_only = via_piece("GND", "plane", (15.0, 5.0), 0.5, 0.3, owner="GND on F.Cu + B.Cu")
    good = plane_stitch(text, [inside])
    assert len(good) == 1 and good[0].ok and good[0].vias == good[0].welded == 1
    assert good[0].layers == ("B.Cu", "F.Cu") and good[0].islands == 1
    bad = plane_stitch(text, [inside, front_only])
    assert not bad[0].ok and bad[0].vias == 2 and bad[0].welded == 1
    assert "1 of 2 lattice via(s) do not land in every pour they tie" in bad[0].line()
    assert "no other check on this board can see it" in bad[0].line()
    assert plane_stitch_lines(bad)[-1] == "planes: 0 of 1 lattice(s) landing in every pour they tie"
    # One pour is not a pair, and a lattice on a board with one pour is not welded to a pair either.
    one = plane_stitch(text.split("\n\t(zone (net 1) (net_name \"GND\") (layer \"B.Cu\")")[0], [inside])
    assert not one[0].zoned and not one[0].ok and "a lattice needs two facing pours" in one[0].line()
    assert plane_stitch(text, []) == () and plane_stitch_lines(()) == ["planes: pcbc wrote no plane-stitch copper on this board"]


@pytest.mark.parametrize("name", EXAMPLE_BOARDS + (("ds2",) if HAS_DS2 else ()))
def test_no_board_in_this_repo_pours_one_net_on_two_layers_so_the_carrier_writes_nothing(name: str):
    """`docs/stitch-plan.md` section 2(l)'s measurement, re-taken after the enabling change: four
    boards at `(('GND','B.Cu'),)` and node at `(('GND','In1.Cu'),('3V3','In2.Cu'))`, which is two pours
    of two **different** nets — a via between them is a short, and what that pair wants is a stitching
    capacitor.

    So `_plane_specs` iterates nothing on every one of them, for a **structural** reason rather than a
    tuned bound: no facing pair exists, and even if one did, no net declares an edge rate. That is the
    same shape of zero `docs/stitch-plan.md` section 4 records for ds2's `final` stage, and it is why
    `VIAS_PATTERN`, `OWNS` and `PLANES` are unmoved on all five.
    """
    ctx, _m = _final_ctx(name)
    by_net: dict[str, list[str]] = {}
    for net, lay in plane_targets(ctx.job):
        by_net.setdefault(net, []).append(lay)
    assert all(len(v) == 1 for v in by_net.values()), (name, by_net)
    assert st._plane_specs(ctx) == (), name
    assert not [p for p in pattern_copper(ctx.design, ctx.job, ctx.cs, ctx.board, stage="final", owned=ctx.owned, scene=scene_from_text(ctx.design, ctx.job, ctx.cs, _m.text)).pieces if p.reason == "plane"]


def test_node_with_gnd_on_both_inner_planes_flips_its_return_verdict_and_pays_for_it():
    """**The experiment the user authorised, and its verdict is: not worth keeping.**

    S5's `return_rules` predicts that node's `net_change` becomes `kept` if `GND` is poured under both
    outer layers, and that prediction is **right** — computed here from `board.py` alone, with no PCB
    file, which is the whole value of R-Z4's **C** half. What the routed build then measured
    (2026-09-21, in a temp copy; `examples/node/node.py` is unchanged):

        returns    8 vias `net_change` -> 7 `far` + 1 `served`, and the rule says `kept`   BETTER
        3V3        loses its plane: 51.311 mm of 0.0889 mm copper on a 1 A rail,
                   `power_moves` goes from 1 entry to 2                                     WORSE
        taps       62 -> 44 (3V3 is no longer a plane net, so its pads get none)
        rungs      1 -> 5  (3V3's antipads are gone, so the ring has room)
        thermal    12 barrels either way, pitch 0.8501 -> 0.700101 (no foreign pour left)
        fab        **the build fails**: KRT puts a GND via inside `C_3V3_HF.2`, a passive's pad,
                   and `fab.via_in_pad_blockers` refuses the board

    And with `rise_ps=500` added it does get its lattice — 74 barrels, all landing in both inner
    planes, both planes still one island — so the technique works on four layers. It is the *board*
    that does not: one rail loses its plane and the fab gate stops it. Recorded here so the next person
    does not have to run it again.
    """
    exp = compile_design(load_board(_board("node"))).constraints
    assert {r.net: r.verdict for r in return_rules(exp)} == {"USB_DN": "net_change", "USB_DP": "net_change"}
    line = next(r.line() for r in return_rules(exp) if r.net == "USB_DP")
    assert 'Board(planes=[("GND", "In1.Cu"), ("GND", "In2.Cu")]) puts one net under both layers' in line
    assert "at the cost of 3V3's plane" in line, "the move names its own cost, and the cost is what settled it"
    # The prediction, made the way S5 makes it: from board.py alone, no PCB file.
    from dataclasses import replace

    both = replace(exp, planes=(("GND", "In1.Cu"), ("GND", "In2.Cu")))
    assert {r.net: r.verdict for r in return_rules(both)} == {"USB_DN": "kept", "USB_DP": "kept"}
    assert plane_targets(compile_design(load_board(_board("node")))) == (("GND", "In1.Cu"), ("3V3", "In2.Cu")), (
        "node ships unchanged: the experiment was run, measured and rejected"
    )


@pytest.mark.kicad
def test_the_fixture_builds_to_fab_with_the_plane_gate_verified(tmp_path: Path):
    """The three things a unit test cannot do: run the `final` stage inside `route_job`, prove a
    lattice barrel lands in a pour that only `gnd_pour` writes, and pin a report string.

    Measured 2026-09-21. **The lattice costs the pours nothing measurable**, and that is worth stating
    because it is the opposite of every other via pcbc places: `PLANES`' own arithmetic is
    `pi*(dia/2 + clearance)^2` = 0.4418 mm2 per via per crossed plane, but that is the antipad a via
    cuts in a **foreign** plane, and a lattice barrel is on the pour's own net — the fill flows right
    up to it and there is no clearance hole at all. Built with and without the lattice, the same
    board's `plane_area` is byte-identical: `GND B.Cu` 1137.97 and `GND F.Cu` 1066.28 both times.

    **`GND F.Cu` is re-recorded 1066.28 -> 1066.22 for `docs/quality-plan.md` slice 1**, and that is
    the string-pull rather than the lattice: built with the relaxer disabled the fixture measures
    1066.28 / 1137.97 to the last digit, and with it 1066.22 / 1137.97. The pass moves 6 of this
    board's 17 chains and takes 1.1842 mm out of them; F.Cu's is the only pour whose area moves at
    all, and a pour flows around copper — a staircase and the taut run that replaces it exclude different slivers of zone. The
    direction is not systematic: on c3_usb the same pass takes `GND B.Cu` **up** 1053.06 -> 1053.10.
    Both boards are still one island per plane and every lattice barrel still lands in its pour.
    """
    from pcbc.build import build_job

    work = tmp_path / "planes.py"
    work.write_text(FIXTURE.read_text())
    result = build_job(work, upto="fab", force=True)
    assert result["error"] is None, result["error"]
    route = next(s for s in result["steps"] if s.get("stage") == "route")
    assert route["planes_stitched"]["fails"] == [], route["planes_stitched"]["lines"]
    assert route["planes_stitched"]["lines"] == [
        "plane GND: 43 lattice via(s), all landing in B.Cu + F.Cu; 2210.61 mm2 of pour across 2 layer(s), 1 island(s) on the worst of them",
        "planes: 1 of 1 lattice(s) landing in every pour they tie",
    ], route["planes_stitched"]["lines"]
    assert route["planes"]["islands"] == {"GND B.Cu": 1, "GND F.Cu": 1}, route["planes"]["islands"]
    # Re-recorded 2026-09-25 for the native router: 41 -> 43 lattice vias, pours 1137.97/1066.22 ->
    # 1139.54/1071.07 mm2. The lattice takes what the route leaves (R-S2), and the native router
    # leaves more of the board free than the old one did (its copper is shorter), so two more of the
    # 50 candidate sites clear and both pours keep more copper; still one island per plane.
    assert route["planes"]["area_mm2"] == {"GND B.Cu": 1139.54, "GND F.Cu": 1071.07}, route["planes"]["area_mm2"]
    assert route["copper_bar"]["totals"]["vias_pattern"] == {"plane": 43, "tap": 5}
    assert route["refusals"] == [], "a board that declares an edge rate has nothing to refuse"
    assert any(n.startswith("style: stitch GND plane: 43 of 50 placed, 0.5 GHz of 1 GHz") for n in route["notes"]), route["notes"]
    # Determinism: a `final` carrier reads the router's finished copper, and the lattice reads the pour
    # objects the route stage wrote. Two builds of this board are compared byte for byte rather than
    # pinned to a digest, because any router change must move the digest.
    import hashlib

    first = hashlib.sha256((tmp_path / "layout" / "planes" / "routed" / "layout.kicad_pcb").read_bytes()).hexdigest()
    again = build_job(work, upto="route", force=True)
    assert again["error"] is None, again["error"]
    second = hashlib.sha256((tmp_path / "layout" / "planes" / "routed" / "layout.kicad_pcb").read_bytes()).hexdigest()
    assert first == second, "same input, byte-identical output"
