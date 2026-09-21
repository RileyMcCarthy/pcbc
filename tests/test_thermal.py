"""S6 of `docs/stitch-plan.md`: `Thermal()`, the derived pitch, and the carrier `thermal`.

Everything here runs on a **placed** board — `conftest.placed_board`, pure Python, no KiCad and no
KRT — or on nothing at all, because half of this slice is a compiler that never opens a PCB file.
The numbers that need a routed board (`VIAS_PATTERN`, `OWNS`, `PLANES`, `THERMAL`, the FAB_NOTES
paragraph) live in `test_examples_fab.py`, which builds one.

Measured 2026-09-21. Nothing under `~/Documents/MaD` is read or written by this file.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from conftest import placed_board
from pcbc.compile import compile_design
from pcbc.constraints import compile_constraints
from pcbc.fab import via_in_pad, via_in_pad_blockers
from pcbc.language import load_board
from pcbc.patterns import PatternCtx, pattern_copper
from pcbc.patterns import stitch as st
from pcbc.route_geom import EPS_MM, _nm
from pcbc.route_scene import build_scene
from pcbc.stackup import get_stackup, via_theta_c_per_w, vias_for_theta

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

LANDS = ("c3_usb", "node")
"""The two boards that carry a `pad_prop_heatsink` land, and they carry the same one: an
ESP32-C3-MINI-1-N4 whose exposed pad KiCad writes as **nine** 1.45 x 1.45 mm blocks all numbered 49,
all on GND. blinky, buck and the DS2 Addon have none — measured below, because "no board declares
one" and "no board can" are different facts and only the second makes the empty rows structural."""


def _board(name: str) -> Path:
    return EXAMPLES / name / f"{name}.py"


def _ctx(name: str) -> PatternCtx:
    """The `final` stage's context over a **placed** board: the land exists the moment the parts do."""
    design = load_board(_board(name))
    job = compile_design(design)
    text = placed_board(name, _board(name)).read_text()
    scene = build_scene(design, job, job.constraints, text)
    return PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board=name, stage="final")


def _variant(tmp_path: Path, name: str, *edits: tuple[str, str]) -> Path:
    """A copy of one example with its `Thermal()` line rewritten — for the refusals no board reaches."""
    src = _board(name).read_text()
    for old, new in edits:
        assert old in src, old
        src = src.replace(old, new)
    (tmp_path / f"{name}.py").write_text(src)
    if (_board(name).parent / "components").exists() and not (tmp_path / "components").exists():
        shutil.copytree(_board(name).parent / "components", tmp_path / "components")
    return tmp_path / f"{name}.py"


# --- the barrel as a heat path ---------------------------------------------------------------------


def test_one_barrels_kelvin_per_watt_is_the_stackups_own_two_numbers():
    """`L / (k * A)` on the **plating annulus**, and the hole counts for nothing.

    The area is `pi * ((drill/2 + plating)^2 - (drill/2)^2)` — the same annulus `via_barrel_mil2`
    uses for the current rating, because the current and the heat are the same piece of metal asked
    two questions. The hole is air (or, on a filled board, an epoxy whose conductivity is a fiftieth
    of copper's) and is deliberately not counted, which is the conservative direction.
    """
    two, four = get_stackup("jlcpcb_2l_1oz"), get_stackup("jlcpcb_4l_1oz")
    assert (two.board_mm, two.via_drill) == (1.6, 0.3) and (four.board_mm, four.via_drill) == (1.5862, 0.2)
    assert via_theta_c_per_w(two.board_mm, two.via_drill, two.via_plating_mm) == 231.1
    assert via_theta_c_per_w(four.board_mm, four.via_drill, four.via_plating_mm) == 334.2
    # Longhand, so the formula is asserted rather than the function's own output compared to itself.
    r = 0.15
    area = math.pi * ((r + 0.018) ** 2 - r * r)
    assert round((1.6e-3) / (385.0 * area * 1e-6), 1) == 231.1


def test_the_four_layer_barrel_is_worse_than_the_two_layer_one_on_a_thinner_board():
    """The one number in this statement a reader is most likely to assume goes the other way.

    node's board is 1.5862 mm against c3_usb's 1.6 — 0.9 % thinner — and its barrel is **45 % worse**,
    because the standard 4-layer drill is 0.2 mm against 2 layers' 0.3 and the plating annulus goes
    with the circumference. It is why node needs twelve barrels where c3_usb needs nine for the same
    watt and the same budget, and it is arithmetic rather than a property of either board.
    """
    two, four = get_stackup("jlcpcb_2l_1oz"), get_stackup("jlcpcb_4l_1oz")
    t2 = via_theta_c_per_w(two.board_mm, two.via_drill, two.via_plating_mm)
    t4 = via_theta_c_per_w(four.board_mm, four.via_drill, four.via_plating_mm)
    assert four.board_mm < two.board_mm and t4 > t2
    assert vias_for_theta(two.board_mm, two.via_drill, 0.35, 10.0, two.via_plating_mm) == 9
    assert vias_for_theta(four.board_mm, four.via_drill, 0.35, 10.0, four.via_plating_mm) == 12
    assert round(t2 / 9, 2) == 25.68 and round(t4 / 12, 2) == 27.85
    assert round(t2 / 9 * 0.35, 2) == 8.99 and round(t4 / 12 * 0.35, 2) == 9.75


def test_the_count_is_at_least_one_and_a_bigger_budget_asks_for_fewer():
    """`ceil(theta / (rise/W))`, floored at 1 for `vias_per_change`'s reason: a land that needs no
    help still gets the barrel that says the model was asked."""
    two = get_stackup("jlcpcb_2l_1oz")
    assert vias_for_theta(two.board_mm, two.via_drill, 0.001, 10.0, two.via_plating_mm) == 1
    assert vias_for_theta(two.board_mm, two.via_drill, 0.35, 20.0, two.via_plating_mm) == 5
    assert vias_for_theta(two.board_mm, two.via_drill, 0.7, 10.0, two.via_plating_mm) == 17
    assert vias_for_theta(two.board_mm, two.via_drill, 0.0, 10.0, two.via_plating_mm) == 1


# --- the compiled budget, with no PCB file at all --------------------------------------------------


def test_the_budget_is_computed_from_board_py_alone():
    """R-Z4's lesson one statement over: the half that costs nothing is the half that arrives first.

    Everything a `Thermal()` needs to say — the net, the barrel's K/W, the count, the plane it has to
    reach, the foreign pours it will cut — is a function of `board.py` and the stackup. No `layout/`
    directory is touched anywhere in this test, and `pcbc check --constraints` prints the line.
    """
    want = {
        "c3_usb": ("GND", (0.5, 0.3), 231.1, 9, "B.Cu", ()),
        "node": ("GND", (0.35, 0.2), 334.2, 12, "In1.Cu", (("3V3", "In2.Cu"),)),
    }
    for name in LANDS:
        cs = compile_constraints(load_board(_board(name)))
        assert cs.refusals == (), (name, cs.refusals)
        (t,) = cs.thermals
        assert (t.pad, t.ref, t.nums, t.ids) == ("U1.49", "U1", ("49",), ("U1.49",)), name
        assert (t.net, t.via, t.theta_via.value, t.need, t.plane, t.planes) == want[name], (name, t)
        assert (t.watts, t.rise_c, t.fill) == (0.35, 10.0, False), name
        assert t.across_planes is (name == "node"), name
        assert t.line() in cs.lines, name
        assert t.to_dict()["need"] == t.need and t.to_dict()["pad"] == "U1.49"


def test_the_three_boards_with_no_land_declare_nothing_and_could_not():
    """Structural, not a choice: `pad_prop_heatsink` appears on **eighteen** pads in this repo and
    every one of them is one of the two ESP32 lands.

    buck is the case that proves the refusal rather than the absence: its largest SMD pad is `L1.1`,
    1.2 x 1.45 mm — wider in one axis than a block of node's heatsink — and it is an inductor
    terminal, where a via wicks the joint whatever the fab fills it with.
    """
    from pcbc.fab import board_pads, passive_refs

    for name in ("blinky", "buck", "c3_usb", "node"):
        design = load_board(_board(name))
        assert compile_constraints(design).thermals == () or name in LANDS, name
        pads = board_pads(placed_board(name, _board(name)).read_text())
        heat = sorted({f"{g.ref}.{g.num}" for g in pads if "pad_prop_heatsink" in g.prop})
        assert heat == (["U1.49"] if name in LANDS else []), (name, heat)
        if name == "buck":
            widest = max(pads, key=lambda g: min(g.box()[2] - g.box()[0], g.box()[3] - g.box()[1]))
            assert widest.ref in passive_refs(design), (widest.id, "buck's biggest land is a passive's")


# --- what `Thermal()` refuses ----------------------------------------------------------------------


def test_a_passive_is_refused_and_via_in_pad_blockers_is_not_asked_to_change(tmp_path: Path):
    """R-M4, and the one refusal in this statement that no amount of fab money buys off.

    `fab.via_in_pad_blockers` refuses a via overlapping a two-terminal passive's pad on **any**
    overlap, filled or not, because the barrel wicks the joint. `Thermal()` refuses the statement at
    compile so the author reads it from `pcbc check` rather than from a fab gate three stages later —
    and the blocker list itself has a **zero diff** in this slice, which is what keeps it the judge.
    """
    board = _variant(tmp_path, "c3_usb", ('Thermal("U1.49", watts=0.35)', 'Thermal("C_MCU.1", watts=0.35)'))
    (refusal,) = compile_constraints(load_board(board)).refusals
    assert "C_MCU is a two-terminal passive" in refusal, refusal
    assert "wicks the joint whatever the fab fills it with" in refusal and "Drop the line" in refusal, refusal
    assert "fab.via_in_pad_blockers" in refusal, "the refusal names the gate it is agreeing with"


def test_a_net_with_no_pour_is_refused_and_the_move_is_the_one_that_writes_the_pour(tmp_path: Path):
    """The blinky trap: on two layers `krt_plan` writes the back pour only when `GND` is a power net,
    so an array on a net without one is watts of vias into bare laminate."""
    board = _variant(
        tmp_path,
        "c3_usb",
        ('Thermal("U1.49", watts=0.35)', 'Thermal("U1.3", watts=0.35)'),
    )
    (refusal,) = compile_constraints(load_board(board)).refusals
    assert "3V3 has no plane or pour to reach" in refusal, refusal
    assert 'NetReq("3V3", kind="power") is what makes krt_plan write the back pour' in refusal, refusal


def test_a_foreign_plane_needs_consent_and_the_refusal_carries_the_arithmetic(tmp_path: Path):
    """Finding 10 at nine times the scale, and the default is to refuse.

    Every via here is a through via, so twelve GND barrels under node's land punch twelve antipads in
    the 3V3 plane below whether anybody meant to or not. `across_planes=True` consents; the pitch the
    array is then placed at is derived so the webs survive rather than chosen.
    """
    board = _variant(
        tmp_path,
        "node",
        ('Thermal("U1.49", watts=0.35, across_planes=True)', 'Thermal("U1.49", watts=0.35)'),
    )
    (refusal,) = compile_constraints(load_board(board)).refusals
    assert "12 of them punch 12 antipads in 3V3 on In2.Cu" in refusal, refusal
    assert "turned a 1.80 mm path across it into 8.09 mm" in refusal, refusal
    assert 'across_planes=True' in refusal, refusal


def test_a_stackup_that_cannot_plug_a_barrel_is_refused_and_tenting_is_not_a_plug():
    """`Stackup.via_tenting` closes the mask over a barrel's mouth; `Stackup.via_fill` says whether
    the fab will put anything **inside** it. The two are different facts about the same hole and the
    whole fab story of this statement is in the difference.

    No board here reaches this: both JLC presets carry `via_fill="epoxy_capped"`, which is
    `docs/router-plan.md` section 3's own row read as a field. It is pinned with a synthetic stackup
    rather than left as dead code, because the day pcbc gains a fab that will not fill is the day the
    array must stop being written.
    """
    from dataclasses import replace

    for name in ("jlcpcb_2l_1oz", "jlcpcb_4l_1oz"):
        stack = get_stackup(name)
        assert stack.via_tenting is True and stack.via_fill == "epoxy_capped", name
        assert stack.via_fill_min_drill <= stack.via_drill <= stack.via_fill_max_drill, name
    bare = replace(get_stackup("jlcpcb_2l_1oz"), name="nofill", via_fill="none")
    narrow = replace(get_stackup("jlcpcb_2l_1oz"), name="coarse", via_fill_min_drill=0.45)
    assert bare.via_fill == "none" and not (narrow.via_fill_min_drill <= narrow.via_drill)


def test_two_thermals_on_one_land_are_refused(tmp_path: Path):
    """One land, one array. Two statements would compile two budgets for one piece of copper and the
    pattern would place the first one twice."""
    board = _variant(
        tmp_path,
        "c3_usb",
        ('Thermal("U1.49", watts=0.35)', 'Thermal("U1.49", watts=0.35)\nThermal("U1.49", watts=0.7)'),
    )
    (refusal,) = compile_constraints(load_board(board)).refusals
    assert "already has a Thermal()" in refusal and "one land, one array" in refusal, refusal


def test_the_language_refuses_a_bad_number_before_the_compiler_sees_it():
    """`watts <= 0` and a `pad` with no dot are wrong in the statement itself, not on this board."""
    from pcbc.language import Thermal, reset

    reset()
    with pytest.raises(ValueError, match="write REF.PAD or REF.PIN"):
        Thermal("U1", watts=0.35)
    with pytest.raises(ValueError, match="watts"):
        Thermal("U1.49", watts=0)
    with pytest.raises(ValueError, match="rise_c"):
        Thermal("U1.49", watts=0.35, rise_c=-1)


def test_a_pad_that_is_not_a_land_is_the_patterns_refusal_and_it_names_the_land_next_door():
    """The fifth refusal, and it lives where its measurement lives.

    `constraints.py` has never opened a footprint, so "carries no `pad_prop_heatsink` **and** is
    narrower than `pitch + dia`" cannot be asked there. Here the primitives are `Item`s with
    `pads.PadGeom.prop` carried on them. The `and` is load-bearing: a land that declares the property
    is a land whatever its size, and a pad that declares nothing and cannot hold two barrels at the
    derived pitch is a **pin** — measured, `U1.3` is 0.4 x 0.8 mm against the 1.3001 mm two 0.5 mm
    rings need at c3_usb's derived pitch.
    """
    ctx = _ctx("c3_usb")
    budget = ctx.cs.thermals[0]
    spec = st._thermal_specs(ctx)[0]
    pin = st.StitchSpec(**{**spec.__dict__, "owner": "U1.3", "budget": _rebudget(budget, ("3",))})
    res = st.run(ctx, pin)
    assert res.pieces == () and res.refusal is not None
    assert res.refusal.rule == "not_a_land" and res.refusal.pattern == "thermal", res.refusal
    assert "none of them carries pad_prop_heatsink" in res.refusal.move, res.refusal.move
    assert 'did you mean Thermal("U1.49")?' in res.refusal.move, res.refusal.move
    assert "U1.3 draws 1 primitive(s), the widest 0.4 x 0.8 mm" in res.refusal.move, res.refusal.move
    assert "0.4 mm across will not hold two 0.5 mm rings at the 0.800101 mm this board's antipads allow, which needs 1.3001 mm" in res.refusal.move, res.refusal.move
    # And the land itself is not refused, which is the other half of the `and`.
    assert st.run(ctx, spec).refusal is None


def _rebudget(budget, nums):
    from dataclasses import replace

    return replace(budget, nums=nums, pad=f"{budget.ref}.{nums[0]}")


# --- the derived pitch -----------------------------------------------------------------------------


def test_the_pitch_is_antipad_clashs_own_arithmetic_and_r_t1s_number_deletes_the_plane():
    """`docs/stitch-plan.md` section 2(h), and it is the correction the slice turns on.

    R-T1 says "a grid of vias at the stackup's hole-to-hole" — 0.700 mm on node. At that pitch two
    0.35 mm rings cut antipads of radius `0.175 + 0.2` = 0.375 in the 3V3 plane, so the web between
    them is `0.700 - 0.750` = **-0.050 mm** and KiCad deletes the copper. The derived pitch leaves
    **+0.100101 mm**, a tenth of a micron over the zone's own `min_thickness` — and it places the
    **same number of vias**, because a 1.45 mm block admits two per axis at either number.
    """
    ctx = _ctx("node")
    scene, budget = ctx.scene, ctx.cs.thermals[0]
    # **A placed board has no zones yet**, so the antipad term is zero on one and 0.85 on the other —
    # which is the pitch's own point: it is derived from the board the array is being placed on, and
    # `final` runs after KRT's `planes` step so the real one has them. The zone is injected here with
    # the numbers `route_scene.zone_rules` reads off node's own routed file: `(connect_pads yes
    # (clearance 0.18))` and `(min_thickness 0.1)`, against a class clearance of 0.2 between GND and
    # 3V3, and it is the **larger** of the two that KiCad's fill honours.
    from pcbc.route_scene import ZoneRule

    assert scene.zone_rules == (), "a placed board has no fill to clash with, and the pitch says so"
    assert st.thermal_pitch(scene, "GND", budget.via)[0] == 0.700101, "hole_to_hole alone, before the planes exist"
    scene.zone_rules = (ZoneRule(net="3V3", layer="In2.Cu", pad_clearance=0.18, min_thickness=0.1),)
    assert scene.table.between("GND", "3V3")[0] == 0.2, "the class number, which is the larger of the two"
    dia = budget.via[0]
    hole_to_hole = scene.table.via_pitch("GND", "GND", budget.via[1], dia, budget.via[1], dia)
    assert hole_to_hole == 0.7, "R-T1's number: the fab's 0.5 mm hole-to-hole plus the 0.2 mm drill"
    anti, why = st.antipad_pitch(scene, "GND", dia)
    assert (anti, why) == (0.85, "the 3V3 plane on In2.Cu, whose min_thickness is 0.1 mm"), (anti, why)
    assert anti == 2 * 0.2 + 0.1 + 0.35, "2 * max(zone clearance, class clearance) + min_thickness + ring"
    pitch, pitch_why = st.thermal_pitch(scene, "GND", budget.via)
    assert (pitch, pitch_why) == (0.850101, why), (pitch, pitch_why)
    # R-T1's web against the derived one, on the plane the array is cutting.
    antipad_dia = dia + 2 * 0.2
    assert round(0.7 - antipad_dia, 4) == -0.05, "R-T1's number overlaps the antipads and deletes the plane"
    assert round(pitch - antipad_dia, 6) == 0.100101, "the derived one leaves the zone's own min_thickness"
    # And it writes identical copper: `1 + floor((1.45 - 0.35) / p)` is 2 at both.
    assert [1 + math.floor(1.1 / p) for p in (0.7, pitch)] == [2, 2]


def test_on_two_layers_there_is_no_foreign_pour_and_the_pitch_is_the_fabs_alone():
    """c3_usb's pour is `GND`'s own, so an array via on `GND` cuts no antipad anywhere and the
    antipad term is **zero**. The pitch is then `hole_to_hole + drill` and nothing else."""
    ctx = _ctx("c3_usb")
    budget = ctx.cs.thermals[0]
    assert st.antipad_pitch(ctx.scene, "GND", budget.via[0]) == (0.0, "")
    pitch, why = st.thermal_pitch(ctx.scene, "GND", budget.via)
    assert pitch == 0.800101 and "hole_to_hole" in why, (pitch, why)
    assert ctx.scene.table.via_pitch("GND", "GND", 0.3, 0.5, 0.3, 0.5) == 0.8
    assert ctx.scene.table.between("GND", "GND") == (0.0, "same net")


def test_the_pitch_is_one_nanometre_past_the_clearance_and_not_exactly_on_it():
    """`docs/stitch-plan.md` section 2(i), measured here rather than quoted.

    `clears` compares `hull_dist2 >= (need + radii + EPS_MM)**2`, and `EPS_MM` arrives there through a
    different sequence of roundings than it does through a pitch — so a lattice laid out at exactly
    `need + EPS_MM` clears on one board and does not on the next. It is not hypothetical: with
    `q(pitch + EPS_MM)` the two-layer array placed nine barrels and passed `verify_copper`, and the
    four-layer one placed twelve and **failed its own self-check** six times with `hole_to_hole`
    reporting "0.5001 mm of 0.5 mm".
    """
    assert st.next_nm(0.8) == 0.800001 and st.next_nm(0.850101) == 0.850102
    assert _nm(st.next_nm(1.234567)) == _nm(1.234567) + 1
    # On a **placed** board neither has a zone yet, so both pitches are `hole_to_hole + drill`: the
    # antipad term is a property of the board the array lands on, not of the statement.
    for name, floor in (("c3_usb", 0.8), ("node", 0.7)):
        ctx = _ctx(name)
        pitch, _why = st.thermal_pitch(ctx.scene, "GND", ctx.cs.thermals[0].via)
        assert _nm(pitch) == _nm(floor) + _nm(EPS_MM) + 1, (name, pitch)


# --- the lattice -----------------------------------------------------------------------------------


def test_the_lattice_covers_every_primitive_of_the_land_and_not_its_bounding_box():
    """A module's exposed pad is not one rectangle: KiCad writes the ESP32's as nine blocks with
    0.525 mm of bare laminate between them. A generator taking the owner's bounding box would offer
    sites in those gaps, where there is no copper for a via to be inside of."""
    for name, pitch, dia in (("c3_usb", 0.800101, 0.5), ("node", 0.850101, 0.35)):
        ctx = _ctx(name)
        prims = st._land(ctx.scene, ctx.cs.thermals[0].ids)
        assert len(prims) == 9, name
        boxes = [it.box() for it in prims]
        assert all(round(b[2] - b[0], 4) == round(b[3] - b[1], 4) == 1.45 for b in boxes), name
        sites = st.sites_lattice(ctx, tuple(boxes), pitch, dia)
        assert len(sites) == 36, (name, len(sites))
        assert 1 + math.floor((1.45 - dia) / pitch) == 2, (name, "two per axis, nine blocks")
        # Every site inside one block, with its whole ring: the array is via-in-pad by construction.
        for at in sites:
            assert any(b[0] <= at[0] - dia / 2 and at[0] + dia / 2 <= b[2] and b[1] <= at[1] - dia / 2 and at[1] + dia / 2 <= b[3] for b in boxes), (name, at)


def test_the_order_is_rank_major_so_the_first_nine_are_one_per_block():
    """The whole difference between an array and a clump, and it is what a hand layout of a nine-block
    QFN land does. Block-major would put the first four barrels in the first block."""
    ctx = _ctx("c3_usb")
    spec = st._thermal_specs(ctx)[0]
    prims = st._land(ctx.scene, ctx.cs.thermals[0].ids)
    boxes = [it.box() for it in prims]

    def which(at):
        return next(i for i, b in enumerate(boxes) if b[0] <= at[0] <= b[2] and b[1] <= at[1] <= b[3])

    assert sorted(which(at) for at in spec.sites[:9]) == list(range(9)), "rank 0 is one site per block"
    assert sorted(which(at) for at in spec.sites[9:18]) == list(range(9)), "then rank 1, in the same block order"
    assert spec.need == 9 and spec.bound == 36 and spec.required is False


def test_the_sites_clear_each_other_by_construction_within_one_call():
    """`PatternCtx` promises the scene is mutated only between patterns, so a spec placing nine vias
    cannot ask `blocked` about its own earlier ones — they are not on the board yet. It does not have
    to: the pitch **is** the two rules one array via could break against another, so every pair of
    sites in one block is at least the pitch apart and every pair across blocks is further."""
    for name in LANDS:
        ctx = _ctx(name)
        spec = st._thermal_specs(ctx)[0]
        pitch, _why = st.thermal_pitch(ctx.scene, "GND", spec.via)
        pairs = sorted(round(math.dist(a, b), 6) for i, a in enumerate(spec.sites) for b in spec.sites[i + 1 :])
        assert pairs[0] == pitch, (name, pairs[:3], pitch)
        hole_gap = pairs[0] - spec.via[1]
        assert hole_gap > ctx.scene.table.hole_to_hole() + EPS_MM, (name, hole_gap)


def test_the_array_places_its_whole_budget_on_both_boards_with_room_to_spare():
    """The acceptance, asked of a **placed** board so it needs no KiCad: c3_usb 9 of 9 and node 12 of
    12, with no refusal, no note and no link segment anywhere in the copper."""
    want = {"c3_usb": 9, "node": 12}
    for name in LANDS:
        ctx = _ctx(name)
        spec = st._thermal_specs(ctx)[0]
        res = st.run(ctx, spec)
        assert res.refusal is None and res.notes == (), (name, res.notes)
        assert len(res.pieces) == want[name], (name, len(res.pieces))
        assert {(p.kind, p.reason, p.net, p.owner, p.w, p.drill) for p in res.pieces} == {
            ("via", "thermal", "GND", "U1.49", spec.via[0], spec.via[1])
        }, name
        assert res.reason == "thermal", name


def test_a_short_array_is_a_note_and_never_a_refusal_and_the_note_names_the_blocker():
    """R-S3: a thermal array's count is a **target**, so half of it is half the conductance and the
    honest answer is the copper that fitted plus one sentence. A rail is the opposite — half the rungs
    is a rail that still cannot carry its current — and that is why `parallel` is all-or-nothing."""
    ctx = _ctx("node")
    spec = st._thermal_specs(ctx)[0]
    greedy = st.StitchSpec(**{**spec.__dict__, "need": 40})
    res = st.run(ctx, greedy)
    assert res.refusal is None, res.refusal
    assert len(res.pieces) == 36 and len(res.notes) == 1, (len(res.pieces), res.notes)
    note = res.notes[0]
    assert note.startswith("style: stitch U1.49: 36 of 40 placed, ") and "C of 10 C (inside its budget)" in note, note
    assert "all 36 sites tried" in note and "the land has no more room" in note, note


def test_fill_places_every_site_that_fits_instead_of_the_budgets_count(tmp_path: Path):
    """`fill=True` is the switch for a land whose real dissipation is not known: the budget is a floor,
    so more copper cannot make the array worse, only larger."""
    board = _variant(tmp_path, "c3_usb", ('Thermal("U1.49", watts=0.35)', 'Thermal("U1.49", watts=0.35, fill=True)'))
    design = load_board(board)
    job = compile_design(design)
    text = placed_board("c3_usb", _board("c3_usb")).read_text()
    scene = build_scene(design, job, job.constraints, text)
    ctx = PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board="c3_usb", stage="final")
    spec = st._thermal_specs(ctx)[0]
    assert job.constraints.thermals[0].need == 9 and spec.need == 36 == spec.bound
    assert len(st.run(ctx, spec).pieces) == 36


# --- via-in-pad, reconciled ------------------------------------------------------------------------


def test_the_array_is_via_in_pad_by_definition_and_the_blocker_list_does_not_grow():
    """`docs/stitch-plan.md` section 6 row 4, asked of a placed board with the array written into it.

    Two claims that pull in opposite directions and both have to hold. pcbc's **own** detector must
    find every via pcbc deliberately put in a pad, and find it wholly `inside` — a barrel breaking a
    land's edge would be the silent failure the whole check exists for. And
    `fab.via_in_pad_blockers` must not grow by one row: the array's barrels are IC / heatsink lands
    and the passive rule is untouched.
    """
    from pcbc.route_emit import write_pieces

    for name, n in (("c3_usb", 9), ("node", 12)):
        design = load_board(_board(name))
        job = compile_design(design)
        text = placed_board(name, _board(name)).read_text()
        plan = pattern_copper(design, job, job.constraints, text, name, stage="final")
        vias = [p for p in plan.pieces if p.reason == "thermal"]
        assert len(vias) == n, name
        before = via_in_pad(text)
        after = via_in_pad(write_pieces(text, vias))
        mine = {(round(p.a[0], 4), round(p.a[1], 4)) for p in vias}
        ours = [h for h in after if (round(h["via"][0], 4), round(h["via"][1], 4)) in mine]
        assert len(ours) == n and all(h["inside"] for h in ours), (name, ours)
        assert all(h["pad"] == "U1.49" and h["net"] == "GND" for h in ours), (name, ours)
        assert len(after) == len(before) + n, (name, len(before), len(after))
        assert via_in_pad_blockers(after, design) == via_in_pad_blockers(before, design) == [], name


def test_plane_checks_is_untouched_and_the_arrays_containment_is_thermal_budgets():
    """A correction to `docs/stitch-plan.md` section 6, which asked for `plane_checks` to be
    generalised to a reason set and made `joins`-aware. Measured: it cannot be.

    `plane_checks` walks `Piece`s rebuilt from the sidecar and a `Piece` carries no `joins` — the
    field is on `StitchSpec` and does not survive the round trip. Adding `"stitch"` to its reason set
    without that would ask node's **parallel** rung, whose net is `VBUS` and which is on an unpoured
    rail by construction, to be inside a `VBUS` zone that does not exist, and fail the build for it.
    """
    import inspect

    from pcbc.route_verify import plane_checks, thermal_budget

    src = inspect.getsource(plane_checks)
    assert 'p.reason != "tap"' in src, "the tap branch keeps its body; the array's containment is not asked here"
    assert "stitch" not in src and "thermal" not in src, src
    assert "in_pad" in inspect.getsource(thermal_budget) and "in_plane" in inspect.getsource(thermal_budget)


def test_tap_has_a_zero_diff_and_the_land_keeps_the_via_that_connects_it():
    """`docs/stitch-plan.md` section 2(g). The obvious design puts the array in `POST` before `tap`
    and teaches `tap` to skip the land — and if the array then refuses every site the pad is connected
    to **nothing**, an unconnected item and a hard build failure, on the one carrier allowed to apply
    partially. So the land keeps its tap via and the array is redundant conductance on top of a
    connection that already exists.

    **The cost is one drilled hole's worth of room, and the plan's arithmetic for it is wrong in two
    places.** The plan says the tap "sits about 0.99 mm from a block centre and the nearest array site
    at about 0.43 mm, 0.56 mm apart against a 0.7 mm requirement, so `blocked()` drops that one site".
    Measured from `tap._sites`' own formula and the derived pitch:

    - the array's sites are at the block's **corners** — a 2 x 2 grid has no centre site — so the
      nearest pair is a **diagonal** at 0.7062 mm, not the 0.564 mm the plan got by taking the
      y component alone;
    - 0.7062 mm between two 0.2 mm drills is 0.5062 mm of hole gap, which **clears** the fab's 0.5 mm
      `hole_to_hole`. What takes the site is `antipad_clash`: the tap is a GND via in the 3V3 plane
      just like the array is, and two 0.35 mm rings need `0.2 + 0.2 + 0.1` = 0.5 mm between them
      there. They have 0.3562 mm;
    - and it takes **two** sites, not one, because the block has two corners on that side.

    On c3_usb the tap is 1.1021 mm out from a block centre and the nearest site is a diagonal 0.8080
    mm away — 0.5080 mm of hole gap against 0.5 — so it costs **nothing**. Either way the sites lost
    are the ranks an array needing 9 of 36 or 12 of 36 never reaches, so the cost in **placed barrels
    is zero on both boards**, which is what `test_the_array_places_its_whole_budget_on_both_boards`
    asserts against the real thing.
    """
    import inspect

    from pcbc.patterns import tap

    # `tap.py` has a zero diff in this slice, and the behavioural half of that claim is asserted
    # below: the land still gets its own via. The textual half is that nothing in `tap` reads the new
    # statement — no import of it, no skip keyed on it, no mention of the carrier.
    src = inspect.getsource(tap)
    assert "ThermalSpec" not in src and "cs.thermals" not in src and "THERMAL_REASON" not in src, "tap.py reads nothing S6 added"
    assert "from ..constraints" not in src and "patterns.stitch" not in src, src
    for name, clear, anti in (("c3_usb", 0.5, 0.0), ("node", 0.5, 0.5)):
        ctx = _ctx(name)
        budget = ctx.cs.thermals[0]
        dia, drill = budget.via
        stack = ctx.scene.stack
        # The land still gets exactly one tap, which is the whole of 2(g): `tap.specs` is unchanged.
        taps = [t for t in tap.specs(ctx) if t.pad.owner == "U1.49"]
        assert len(taps) == 1 and not taps[0].skip, (name, taps)
        # `tap._sites`' own distance out: half the pad, the fab's clearance, EPS, half the via.
        out = 0.725 + stack.clearance_min + EPS_MM + dia / 2.0
        pitch, _why = st.thermal_pitch(ctx.scene, "GND", budget.via)
        if name == "node":  # the real `final` board has the 3V3 zone; a placed one does not.
            pitch = 0.850101
        half = pitch / 2.0
        centres = math.hypot(half, out - half)
        assert round(centres, 4) == (0.8080 if name == "c3_usb" else 0.7062), (name, centres)
        assert round(centres - drill, 4) > clear, (name, "the diagonal clears the fab's hole_to_hole either way")
        rings = round(centres - dia, 4)
        assert (rings < anti) is (name == "node"), (name, rings, anti, "the 3V3 plane's neck is what takes node's two")
        assert ctx.scene.table.hole_to_hole() == 0.5


def test_the_gate_can_fail_and_it_fails_on_the_thing_nothing_else_can_see():
    """A gate that cannot fail is not a gate (S7 review, finding 20), so here it is firing twice.

    A barrel moved out of its land is invisible to every other check on the board: KiCad's
    unconnected-items is pad to pad and the via still reaches the pour, `netcheck.check_copper` reads
    pad bindings inside footprint blocks and never a via, and `verify_copper` asks about clearance,
    angle and size — all of which a misplaced barrel passes. It moves none of the heat it was placed
    for, and `build._thermal_gate` is the only thing that says so.

    **The array on a placed board is `adrift` too, and that is the right answer rather than a false
    positive.** A placed board has no filled zone at all, so `in_plane` is 0: nine barrels welded to
    a land on one layer and to nothing on the other. It is why the gate runs on the file KiCad has
    **refilled and saved**, which is the only moment both halves of the containment exist, and it is
    the same reason `plane_checks` calls a missing zone a sentence rather than a `continue`.
    """
    from pcbc.route_emit import via_piece, write_pieces
    from pcbc.route_verify import thermal_budget

    design = load_board(_board("c3_usb"))
    job = compile_design(design)
    ctx = _ctx("c3_usb")
    good = st.run(ctx, st._thermal_specs(ctx)[0]).pieces
    text = write_pieces(placed_board("c3_usb", _board("c3_usb")).read_text(), good)
    (ok,) = thermal_budget(text, list(good), job.constraints)
    assert (ok.got, ok.in_pad, ok.in_plane, ok.plane) == (9, 9, 0, "B.Cu"), ok
    assert ok.verdict == "adrift", "no filled zone yet, so nine barrels reach the land and nothing else"
    # **One per block, which is what a hand layout does** — and it is true here because nothing is in
    # the way. The closest pair is the *block* pitch, 1.975 mm, not the lattice's 0.800101: rank 0 is
    # one site in each of the nine blocks and an array needing nine never reaches rank 1. On the real
    # routed board three of the nine step to a second site in their neighbour's block, because KRT
    # runs the USB pair under the module on B.Cu and takes one column's rank-0 sites
    # (`test_examples_fab.py::THERMAL` pins the 0.800101 that results).
    assert ok.pitch_mm == 1.975, ok.pitch_mm
    # And the half nothing else can see: a barrel that is not under the land at all.
    adrift = list(good[:-1]) + [via_piece("GND", "thermal", (2.0, 2.0), 0.5, 0.3, owner="U1.49")]
    (bad,) = thermal_budget(write_pieces(text, adrift[-1:]), adrift, job.constraints)
    assert (bad.got, bad.in_pad, bad.verdict) == (9, 8, "adrift"), bad
    assert "8 inside the land" in bad.line() and "(adrift)" in bad.line(), bad.line()


# --- determinism ------------------------------------------------------------------------------------


def test_the_array_is_a_function_of_the_board_and_not_of_the_run():
    """Same placed board, same bytes, three times over — uuids included, because they are
    `stable_uuid` over the board name, the reason, the net and the coordinate."""
    for name in LANDS:
        design = load_board(_board(name))
        job = compile_design(design)
        text = placed_board(name, _board(name)).read_text()
        runs = [pattern_copper(design, job, job.constraints, text, name, stage="final").text for _ in range(3)]
        assert runs[0] == runs[1] == runs[2], name
