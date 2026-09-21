"""Technique 3: the guard, the one pattern with no population on any real board.

`docs/stitch-plan.md` S7 and `docs/r2-design.md` B.6. The subject of this file is a shield — two
ground tracks beside a net pcbc routed, stitched to the pour — and the thing that makes it worth a
file of its own is that **its failure is invisible to every other judge on the board**: KiCad's
unconnected-items check is pad to pad, so a floating track on a named net passes DRC;
`netcheck.check_copper` reads pad bindings inside footprint blocks and never a segment or a via;
`verify_copper` asks about clearance, angle and size; the copper bar counts its millimetres like any
other copper. A guard welded to nothing looks exactly like a guard.

Every board here is read **read-only**, the DS2 Addon's included (it lives outside this repo, and
nothing in `~/Documents/MaD` is written by this suite). Nothing reads `examples/**/layout/`, which is
gitignored build output: a **placed** board comes from `conftest.placed_board` (pure Python, no
KiCad) and the one test that needs a **routed** one is marked `kicad` + `krt`.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from pcbc.ampacity import NOT_A_RAIL
from pcbc.compile import compile_design
from pcbc.copper_bar import REDUNDANT
from pcbc.fanout import fanout_pieces
from pcbc.language import load_board
from pcbc.patterns import PatternCtx, merge_plans, pattern_copper
from pcbc.route_emit import REASONS, write_pieces
from pcbc.route_geom import EPS_MM, clears, is_octilinear, track_shape, turn_ok
from pcbc.route_scene import build_scene, clear_runs, net_runs
from pcbc.route_verify import BRANCH_REASONS, guard_cover, guard_lines
import pcbc.patterns.stitch as st

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIXTURE = ROOT / "tests" / "fixtures" / "guard" / "guard.py"
VECTORS = ROOT / "tests" / "fixtures" / "clear_runs_vectors.json"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc" / "ds2_addon.py"
HAS_DS2 = DS2.exists()


def _board(name: str) -> Path:
    if name == "ds2":
        return DS2
    if name == "guard":
        return FIXTURE
    return EXAMPLES / name / f"{name}.py"


def _uuid_name(name: str) -> str:
    return "ds2_addon" if name == "ds2" else name


def _placed(name: str) -> Path:
    from conftest import placed_board

    return placed_board(name, _board(name))


def _routed_to_patterns(name: str):
    """The board as the `final` stage really sees it, minus KRT: the pre stage, the fanout, the mid
    stage, and the list of pieces pcbc owns.

    Not a routed board — that needs KiCad and KRT and is marked as such below — but it is the same
    *shape* of input: a scene with pcbc's own copper in it and `PatternCtx.owned` holding the pieces
    that put it there, which is the only thing the guard's precondition reads.
    """
    design = load_board(_board(name))
    job = compile_design(design)
    text = _placed(name).read_text()
    board = _uuid_name(name)
    pre = pattern_copper(design, job, job.constraints, text, board, stage="pre")
    fan, _n = fanout_pieces(design, job, pre.text, board, pre.scene, claimed=pre.claimed)
    pre.scene.add(pre.scene.item_of(p) for p in fan)
    mid = pattern_copper(design, job, job.constraints, write_pieces(pre.text, fan), board, stage="mid", scene=pre.scene)
    merged = merge_plans(pre, mid)
    owned = tuple(merged.pieces) + tuple(fan)
    scene = build_scene(design, job, job.constraints, merged.text)
    ctx = PatternCtx(scene=scene, design=design, job=job, cs=job.constraints, board=board, stage="final", owned=owned)
    return ctx, merged


def _final(name: str):
    ctx, merged = _routed_to_patterns(name)
    design, job = ctx.design, ctx.job
    return pattern_copper(design, job, job.constraints, merged.text, ctx.board, stage="final", owned=ctx.owned), ctx


# ---------------------------------------------------------------------------------------------
# Finding 1: which nets pcbc routes, and therefore which nets can be guarded at all
# ---------------------------------------------------------------------------------------------

ROUTED_BY_PCBC = {
    "blinky": ["LED"],
    "buck": [],
    "c3_usb": ["CC1", "CC2", "LED"],
    "node": ["CC1", "CC2", "LED", "LED_A", "LOAD"],
    "ds2": ["A0", "A1", "A2", "A3", "DRDY", "REFN", "REFP", "UART_RX", "UART_TX", "nRESET"],
}
"""Every net pcbc routes **end to end** on each board, measured 2026-09-21 over the pre + fanout + mid
stages of a freshly placed board.

This is B.6's precondition turned into a census, and it is the finding S7 exists to report. The plan's
section 7.1 says the intersection of "a net pcbc routed" and "a net worth guarding" is empty on all
five boards, and gives as the reason that pcbc routes only `fanout`, `hop`, `spine` and `tap`. The
first half is right and the reason is not: pcbc routes plenty — ten hops on the DS2 Addon alone. What
is empty is narrower and worse. On the one board here that declares an analog net, **every declared
analog net is KRT's**: `AIN0..AIN3`, `REFP_F` and `REFN_F` are `kind="analog"`, which compiles to one
layer with `via.allowed=False`, and they go to KRT's `analog_nets` step. What pcbc does route there is
`A0..A3` and `REFP`/`REFN` — the *unfiltered* side of the same signals, 2.2 to 3.1 mm of hop between a
connector pin and the resistor beside it.

`buck` is empty because both its spines leave their nets open (`merged.done` is `[]`), which is the
same measurement from the other end: a partially routed net is not a path pcbc owns."""


@pytest.mark.parametrize("name", sorted(ROUTED_BY_PCBC))
def test_which_nets_pcbc_routes_is_the_guards_whole_population(name: str):
    if name == "ds2" and not HAS_DS2:
        pytest.skip("the DS2 Addon lives outside this repo")
    _ctx, merged = _routed_to_patterns(name)
    assert sorted(merged.done) == ROUTED_BY_PCBC[name], (
        f"{name}: the nets pcbc routes end to end. This is the guard's entire population "
        "(docs/r2-design.md B.6: pcbc cannot offset a path it does not own), so a change here is a "
        "change to what can be guarded at all"
    )


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon lives outside this repo")
def test_the_nets_worth_guarding_on_a_real_board_are_all_krts():
    """The finding, stated as the thing an author would actually write.

    `Guard("AIN0")` is the line the DS2 Addon would carry — `AIN0..AIN3`, `REFP_F` and `REFN_F` are
    its analog inputs and the reason it has two grounds at all. Not one of them is pcbc's, and the
    reason is structural rather than incidental: `kind="analog"` compiles to a single layer and no
    vias, which is what sends a net to KRT's constrained step."""
    ctx, merged = _routed_to_patterns("ds2")
    analog = sorted(c.net for c in ctx.cs.constraints if c.kind == "analog")
    assert analog == ["AIN0", "AIN1", "AIN2", "AIN3", "REFN_F", "REFP_F"], analog
    assert not (set(analog) & set(merged.done)), (
        "every declared-analog net on the one board that has any is routed by KRT, so B.6's deferral "
        "is the guard's output on a real board and not its exception"
    )
    for net in analog:
        c = ctx.cs.by_net(net)
        assert c.layers == ("F.Cu",) and not c.via.allowed, (net, c.layers, c.via.allowed)


# ---------------------------------------------------------------------------------------------
# `clear_runs`: the dual of `free_intervals`, and the sweep that calibrates it
# ---------------------------------------------------------------------------------------------


def _sweep(scene, axis, span, offset, half, net, layer, step=0.01):
    """`clears` asked directly, every `step` along the line, over the same items `clear_runs` reads."""
    from pcbc.route_geom import gap

    lo, hi = min(span), max(span)
    out = []
    t = lo
    while t <= hi + 1e-12:
        at = {"x": (t, offset), "y": (offset, t), "u": ((t + offset) / 2.0, (t - offset) / 2.0), "v": ((t + offset) / 2.0, (offset - t) / 2.0)}[axis]
        probe = track_shape(at, (at[0] + 1e-6, at[1]), half * 2.0)
        ok = True
        for it in scene.items:
            if it.kind in ("lane", "edge") or layer not in it.layers or it.copper is None:
                continue
            if bool(net) and it.net == net:
                continue
            need = scene.table.between(net, it.net)[0] if it.kind != "keepout" else 0.0
            if not clears(probe, it.copper, need):
                ok = False
                break
        out.append((round(t, 4), ok))
        t = round(t + step, 6)
    return out


@pytest.mark.skipif(not HAS_DS2, reason="the DS2 Addon lives outside this repo")
def test_clear_runs_agrees_with_clears():
    """A.8's own calibration, applied to the dual: sweep the real board at 0.01 mm and check that
    every point `clear_runs` calls clear really does clear.

    The direction of the claim is the whole point and it is one-sided on purpose. `clear_runs` is a
    **proposer**: it may be more pessimistic than `clears` — `_project` reads an axis-aligned bounding
    box, which on a diagonal is a strict superset — and it may never be more optimistic, because
    `blocked` is what decides and a stretch this offered has to survive it. So the assertion is that
    no point inside a reported interval is one `clears` refuses; points outside them are free to be
    clear and often are.
    """
    design = load_board(_board("ds2"))
    job = compile_design(design)
    scene = build_scene(design, job, job.constraints, _placed("ds2").read_text())
    checked = 0
    for y in (6.0, 9.0, 12.0, 15.0, 18.0):
        ivs = clear_runs("x", (2.0, 40.0), y, 0.0635, "GND", scene, "F.Cu")
        truth = dict(_sweep(scene, "x", (2.0, 40.0), y, 0.0635, "GND", "F.Cu"))
        for lo, hi in ivs:
            t = lo
            while t <= hi + 1e-12:
                key = round(t, 4)
                if key in truth:
                    checked += 1
                    assert truth[key], f"clear_runs offered ({lo},{hi}) at y={y}, and clears refuses x={key}"
                t = round(t + 0.01, 6)
    assert checked > 500, f"the sweep has to actually cover something: {checked} points"


def test_clear_runs_is_the_duals_own_epsilon_and_rounds_inward():
    """Every endpoint is 4 dp and rounded **inward**, `free_intervals`' rule in `free_intervals`'
    words: never wider than the truth. And nothing narrower than `scene.grid` survives, because a
    lane a router cannot use is not a lane."""
    design = load_board(_board("guard"))
    job = compile_design(design)
    scene = build_scene(design, job, job.constraints, _placed("guard").read_text())
    ivs = clear_runs("x", (2.0, 38.0), 8.8199, 0.0635, "GND", scene, "F.Cu")
    assert ivs, "the fixture's north flank has room somewhere"
    for lo, hi in ivs:
        assert round(lo, 4) == lo and round(hi, 4) == hi
        assert hi - lo >= scene.grid - 1e-9


def test_clear_runs_replays_its_golden_vectors():
    """The fixture R3 replays, in `geom_vectors.json`'s own shape and with its own regenerate switch.

    **It is a separate file and `docs/stitch-plan.md` S7 asks for an entry in `geom_vectors.json`.**
    That file is generated by `test_route_geom.py::_vector_doc` from `random.Random(20260920)` and
    `route_geom` alone, and replayed by a test that asserts the whole document byte for byte — so a
    key generated anywhere else fails that assertion, and generating it there would make the pure
    geometry suite import `route_scene` and build a board. The vectors are what R3 needs; which file
    they sit in is not, and this one carries the same note, the same seedless determinism and the same
    `PCBC_WRITE_*` regeneration idiom.
    """
    design = load_board(_board("guard"))
    job = compile_design(design)
    scene = build_scene(design, job, job.constraints, _placed("guard").read_text())
    cases = []
    for axis, span, offset in (
        ("x", (2.0, 38.0), 8.8199),
        ("x", (2.0, 38.0), 9.8801),
        ("y", (2.0, 28.0), 12.0),
        ("u", (24.0, 34.0), -9.2254),
        ("v", (-20.0, -2.0), 27.0),
    ):
        cases.append(
            {
                "axis": axis, "span": list(span), "offset": offset, "half": 0.0635, "net": "GND", "layer": "F.Cu",
                "runs": [list(r) for r in clear_runs(axis, span, offset, 0.0635, "GND", scene, "F.Cu")],
            }
        )
    doc = {
        "note": (
            "Golden vectors for route_scene.clear_runs, the dual of free_intervals (docs/r2-design.md "
            "A.8, docs/stitch-plan.md S7). Taken on tests/fixtures/guard/guard.py as conftest.placed_board "
            "generates it — a pure function of board.py — so R3's port replays this file unchanged. "
            "Intervals are 4 dp millimetres of the axis' own coordinate, rounded INWARD; u = x + y and "
            "v = x - y, so a millimetre along a diagonal is two of its units. Regenerate with "
            "PCBC_WRITE_CLEAR_RUNS=1 pytest tests/test_guard.py -k vectors."
        ),
        "board": "tests/fixtures/guard/guard.py",
        "eps_mm": EPS_MM,
        "grid": scene.grid,
        "cases": cases,
    }
    if os.environ.get("PCBC_WRITE_CLEAR_RUNS"):
        VECTORS.write_text(json.dumps(doc, indent=1) + "\n")
    stored = json.loads(VECTORS.read_text())
    assert stored["eps_mm"] == EPS_MM and stored["grid"] == scene.grid
    assert stored["cases"] == cases, "the fixture is out of date: PCBC_WRITE_CLEAR_RUNS=1 regenerates it"


# ---------------------------------------------------------------------------------------------
# `net_runs`: the board's own copper, walked into runs
# ---------------------------------------------------------------------------------------------


def test_net_runs_walks_a_net_into_runs_and_stops_at_a_junction():
    """`route_verify.paths_of`' walk, asked of the scene instead of of a pattern's pieces: a vertex
    where three tracks meet ends the chains that reach it. Canonical and sorted, so two boards with
    the same copper written in a different order give the same tuple."""
    design = load_board(_board("guard"))
    job = compile_design(design)
    _ctx, merged = _routed_to_patterns("guard")
    scene = build_scene(design, job, job.constraints, merged.text)
    runs = net_runs(scene, "SIG", "F.Cu")
    assert len(runs) == 1 and runs[0].layer == "F.Cu", runs
    assert runs[0].pts == ((7.5925, 9.35), (23.7675, 9.35)), runs[0].pts
    assert round(runs[0].mm, 4) == 16.175 and runs[0].widths == (0.16,)
    assert all(scene.items[i].kind == "track" for i in runs[0].ids)
    # SIG2 is one run of five legs — the two stubs, the mitre chords and the 45 between them.
    sig2 = net_runs(scene, "SIG2", "F.Cu")
    assert len(sig2) == 1 and len(sig2[0].pts) == 6, sig2


# ---------------------------------------------------------------------------------------------
# The offset: B.6's formula, and the three things measurement took out of it
# ---------------------------------------------------------------------------------------------


def test_the_offset_is_not_b6s_formula_and_every_term_is_the_compilers():
    """`docs/stitch-plan.md` section 2(i). B.6 writes `d = w/2 + max(between, spacing_w * w)`; the
    offset here is `next_nm(w/2 + between + max(track_min, via_diameter)/2 + EPS_MM)`.

    The measurement is the argument. On this fixture `w` is 0.16 (the class floor), `between("GND",
    "SIG")` is 0.2, and the stitch via's ring is 0.5 — so **0.25 of the 0.5301 mm offset is the term
    B.6 leaves out**, and B.6's own via could not sit on B.6's own track. `spacing_w` is dropped
    because it is a crosstalk rule between two signal nets and is not even monotone.
    """
    ctx, _merged = _routed_to_patterns("guard")
    d, why = st.guard_offset(ctx.scene, ctx.cs, "SIG", "GND")
    assert d == 0.530101, d
    stack = ctx.scene.stack
    w = round(float(ctx.cs.by_net("SIG").width_mm.value), 4)
    bet = ctx.scene.table.between("GND", "SIG")[0]
    c_spacing = ctx.cs.by_net("SIG").spacing_w
    assert (w, bet, stack.track_min, stack.via_diameter) == (0.16, 0.2, 0.127, 0.5)
    assert d == st.next_nm(w / 2.0 + bet + stack.via_diameter / 2.0 + EPS_MM)
    # B.6's own number on this net is 0.56 — *larger* than the derived one, and the reason it is
    # larger is the accident section 2(i) objects to: `spacing_w * w` is 0.48 here, so the crosstalk
    # term happens to cover a via ring it was never about. Take a net where it does not (a narrow
    # track with a large clearance, which is every high-voltage or fine-pitch case) and B.6's answer
    # is short by exactly the ring's half-width plus the epsilon — which is the term, not a rounding.
    b6 = w / 2.0 + max(bet, c_spacing * w)
    assert round(b6, 6) == 0.56 and c_spacing == 3
    narrow, far = 0.0889, 0.5
    assert max(far, c_spacing * narrow) == far, "the branch where the crosstalk term stops masking it"
    short = (narrow / 2.0 + far + stack.via_diameter / 2.0 + EPS_MM) - (narrow / 2.0 + far)
    assert round(short, 4) == 0.2501, "B.6's offset is a whole via radius short wherever between wins"
    assert "ring a stitch via puts on the guard" in why


def test_the_offset_matches_the_plans_two_measured_numbers():
    """Section 5's table: the DS2 Addon's analog class gives 0.550101 and node's 0.475101. Both are
    0.2 mm nets, and reproducing a number the plan measured by a different route is what says the
    formula is the same formula."""
    for name, want in (("ds2", 0.550101), ("node", 0.475101)):
        if name == "ds2" and not HAS_DS2:
            continue
        design = load_board(_board(name))
        job = compile_design(design)
        scene = build_scene(design, job, job.constraints, _placed(name).read_text())
        net = "AIN0" if name == "ds2" else "USB_DN"
        c = job.constraints.by_net(net)
        d = st.next_nm(0.2 / 2.0 + scene.table.between("GND", net)[0] + max(scene.stack.track_min, scene.stack.via_diameter) / 2.0 + EPS_MM)
        assert d == want, (name, net, d, round(float(c.width_mm.value), 4))


def test_offset_path_is_exact_and_cuts_the_corner_it_cannot_turn():
    """Two constructions and one refusal, all three measured on the fixture's own `SIG2`.

    A corner's offset vertex is the **intersection** of the two offset lines. Translating the vertex
    instead gives two points whose chord is not octilinear — the number below is that chord, and
    `verify_copper` would have raised on it. And a corner the offset cannot turn cuts the flank in
    two, which is not an edge case: `patterns.MITRE_MM` is 0.2, so every quarter turn pcbc writes
    carries a 0.2828 mm chord, and the inside of a 135 degree turn loses `0.828 * d` of it — so at any
    offset over 0.342 mm, which is every offset any board here computes, it folds.
    """
    from pcbc.patterns import MITRE_MM, _nm

    path = ((7.5925, 17.95), (8.1851, 17.95), (8.3851, 18.15), (8.3851, 18.3602), (10.9749, 20.95), (11.7675, 20.95))
    d = 0.530101
    outside = st.offset_path(path, d, -1)
    inside = st.offset_path(path, d, 1)
    assert len(outside) == 1, "the outside of every turn: one continuous mitred flank"
    assert len(inside) == 2, "the inside folds at the mitre chord and the flank is cut in two"
    for flank in outside + inside:
        assert is_octilinear(flank) and turn_ok(flank), flank
        for x, y in flank:
            assert _nm(x) / 1e6 == x and _nm(y) / 1e6 == y, "every vertex a whole nanometre"
    # The arithmetic that makes the cut inevitable rather than incidental.
    assert MITRE_MM == 0.2 and round(MITRE_MM * math.sqrt(2), 4) == 0.2828
    assert round(MITRE_MM * math.sqrt(2) / (2 * math.tan(math.radians(22.5))), 4) == 0.3414
    assert d > 0.3414
    # The naive construction, for the record: translating the corner vertex twice gives two points
    # whose chord is not octilinear, which is what `verify_copper` would have raised on.
    corner, out_leg, in_leg = path[1], (0.0, -d), (-d * math.sqrt(0.5), -d * math.sqrt(0.5))
    chord = ((corner[0] + out_leg[0], corner[1] + out_leg[1]), (corner[0] + in_leg[0], corner[1] + in_leg[1]))
    assert not is_octilinear(chord), chord
    assert abs(abs(chord[1][0] - chord[0][0]) - abs(chord[1][1] - chord[0][1])) > 1e-3


def test_sites_along_is_b6s_rule_read_literally():
    """*"stitch vias every `stitch_mm` along each side starting from the guarded path's first vertex
    and one via at each end"* — arc length 0 is the first end, so the rule is `0, pitch, 2*pitch, ...`
    plus the far end, and both ends fall out of it. Stepped in integer nanometres, because arc length
    along a 45 degree leg is irrational."""
    from pcbc.patterns import _nm

    ctx, _m = _routed_to_patterns("guard")
    flank = ((8.0, 10.0), (18.0, 10.0))
    sites = st.sites_along(ctx, flank, 2.5)
    assert sites == ((8.0, 10.0), (10.5, 10.0), (13.0, 10.0), (15.5, 10.0), (18.0, 10.0)), sites
    diag = ((8.0, 10.0), (11.0, 13.0))
    got = st.sites_along(ctx, diag, 2.5)
    assert got[0] == (8.0, 10.0) and got[-1] == (11.0, 13.0), got
    for x, y in got:
        assert _nm(x) / 1e6 == x and _nm(y) / 1e6 == y
    assert 2.4 < math.dist(got[0], got[1]) <= 2.5, "floored into the leg's own steps, never past the target"


# ---------------------------------------------------------------------------------------------
# The precondition, and its message
# ---------------------------------------------------------------------------------------------


def test_a_net_krt_routed_defers_with_b6s_message_and_writes_nothing():
    """B.6's deferral, verbatim, and it is a **note** and not a `Refusal`.

    Every refusal in this repo is a move ending in a `board.py` edit, and there is no edit that makes
    pcbc route an analog net: the pattern that would is R3's maze router. A refusal would also put an
    entry into `REFUSED`, which is pinned exactly per board.
    """
    plan, _ctx = _final("guard")
    want = (
        "guard AIN: deferred - the net is routed by KRT, not by a pattern, so pcbc cannot offset a "
        "path it does not own. R3 owns this."
    )
    assert want in plan.notes, plan.notes
    assert not [p for p in plan.pieces if p.owner.startswith("AIN")], "no copper for a deferred guard"
    assert not [r for r in plan.refusals() if r.net == "AIN"], "a deferral is a report, not a refusal"


def test_the_guarded_net_is_the_one_pcbc_owns_not_the_one_the_file_locks():
    """The precondition is read off `PatternCtx.owned` and it cannot be read off the board.

    `AIN` on the fixture is KRT's, and after a real route its copper is `(locked yes)` exactly like
    pcbc's — `route.lock_copper` locks every constrained step's output. So the same scene, handed an
    empty `owned`, defers **every** guard: provenance is a fact about the run and the board file does
    not record it."""
    ctx, merged = _routed_to_patterns("guard")
    blind = PatternCtx(scene=ctx.scene, design=ctx.design, job=ctx.job, cs=ctx.cs, board="guard", stage="final", owned=())
    specs = st.specs(blind)
    assert specs and all(s.guard is None for s in specs if s.carrier == "guard"), specs
    assert sum(1 for s in st.specs(ctx) if s.carrier == "guard" and s.guard is not None) == 2, "SIG and SIG2 are pcbc's"


# ---------------------------------------------------------------------------------------------
# The copper, the partial rule, and the note
# ---------------------------------------------------------------------------------------------


def test_the_fixture_guard_is_written_and_stitched_at_the_declared_pitch():
    """The whole technique on a board, at the placed-plus-patterns stage a unit test can pin.

    The pitch is `Guard(stitch_mm=2.5)`'s and nothing else: the vias sit 2.5 mm apart along the flank,
    phased from the guarded path's own first vertex, which is what makes the two flanks' vias line up
    across the run instead of drifting.
    """
    plan, _ctx = _final("guard")
    segs = [p for p in plan.pieces if p.kind == "seg" and p.reason == "guard"]
    vias = [p for p in plan.pieces if p.kind == "via" and p.reason == "guard"]
    assert [p.net for p in segs + vias] == ["GND"] * (len(segs) + len(vias)), "a guard is ground copper"
    assert {p.w for p in segs} == {0.127}, "stack.track_min, B.6's width, never the class"
    assert {(p.w, p.drill) for p in vias} == {(0.5, 0.3)}, "the fab's standard via: a stitch via is a branch"
    sig = sorted(p.a for p in vias if p.owner == "SIG guard")
    xs = sorted({round(x, 4) for x, _y in sig})
    assert all(round(b - a, 4) == 2.5 for a, b in zip(xs, xs[1:])), xs
    assert {round(y, 4) for _x, y in sig} == {8.8199, 9.8801}, "one flank each side, at +-d"
    assert round(8.8199 + 0.530101, 4) == 9.35 and round(9.8801 - 0.530101, 4) == 9.35


def test_a_blocked_stretch_is_dropped_and_the_note_says_how_much_of_the_run_is_guarded():
    """R-S3's target half and B.6's partial rule, pinned verbatim.

    B.6 writes the note as *"AIN0 guarded 18.2 of 24.0 mm; C4 blocks 5.8 mm on the north side (8
    stitch vias placed, 2 dropped)"*. That sentence did not survive S4, which gave every target spec
    one `Coverage` record and one `style:` line built from it — so the shape below is `Coverage.line`'s
    and the content is B.6's. Two things B.6 does not say and this does: which rule dropped the
    stretch, and that a stretch which got no via is deleted again rather than left as ground copper
    welded to nothing.
    """
    plan, _ctx = _final("guard")
    notes = [n for n in plan.notes if n.startswith("style: stitch SIG guard")]
    assert notes == [
        "style: stitch SIG guard: 10 of 16 placed, 24.898 mm of 32.35 mm (above its floor); "
        "a 2.173 mm stretch on the left got no stitch via, so nothing would weld it to the GND pour"
    ], notes
    assert len([p for p in plan.pieces if p.kind == "seg" and p.owner == "SIG guard"]) == 3
    # 32.35 is both flanks of a 16.175 mm run; 24.898 is what fitted. The two 0402s on the north
    # flank are what cut it, and the island between them is too short to hold a via at the declared
    # pitch, so it is not written at all.
    assert round(2 * 16.175, 3) == 32.35


def test_a_stretch_with_no_stitch_via_is_never_written():
    """The step that makes the pattern honest rather than decorative, and the reason
    `route_verify.guard_cover` has nothing to fail on when the pattern has done its job.

    A guard track is ground copper only because a via ties it to the pour. Every judge on this board
    would pass an untied one: KiCad's unconnected check is pad to pad, `netcheck.check_copper` never
    reads a segment, `verify_copper` asks about clearance and angle. So the pattern deletes it.
    """
    plan, _ctx = _final("guard")
    pieces = [p for p in plan.pieces if p.reason == "guard"]
    segs = [p for p in pieces if p.kind == "seg"]
    vias = [p for p in pieces if p.kind == "via"]
    for s in segs:
        shape = track_shape(s.a, s.b, s.w)
        from pcbc.route_geom import gap, via_shape

        assert any(gap(via_shape(v.a, v.w), shape) <= 0.0 for v in vias if v.owner == s.owner), (
            f"the stretch {s.a}-{s.b} has no via on it, so nothing welds it to the pour"
        )


def test_guard_cover_is_the_check_and_it_can_fail():
    """`route_verify.guard_cover` on copper that is right, then on the same copper with its vias
    taken away — which is the only way to show that the check can fail, because the pattern is
    written so that it never writes a stretch a via does not weld.

    The pour is synthesised here rather than routed: `gnd_pour` is KRT's step and this file does not
    need KiCad to ask whether a ring is inside a polygon. The routed answer is
    `test_the_fixture_builds_to_fab_with_the_guard_gate_verified`'s.
    """
    plan, _ctx = _final("guard")
    pieces = [p for p in plan.pieces if p.reason == "guard"]
    text = _with_gnd_pour(write_pieces(_routed_to_patterns("guard")[1].text, plan.pieces))
    rows = guard_cover(text, pieces)
    assert sorted(r.net for r in rows) == ["SIG", "SIG2"]
    assert all(r.ok for r in rows), [r.line() for r in rows]
    assert guard_lines(rows)[-1] == "guards: 2 of 2 shield(s) welded to their pour on every piece"
    # Strip the vias and the same copper is an island on a named net that nothing else here can see.
    broken = guard_cover(text, [p for p in pieces if p.kind == "seg"])
    assert broken and all(not r.ok for r in broken), broken
    assert "shields nothing" in broken[0].line()
    # And the blinky trap, which is the other half of the same check: a ground with no pour at all.
    bare = guard_cover(_routed_to_patterns("guard")[1].text, pieces)
    assert all(not r.zoned and not r.ok for r in bare)
    assert "has no filled zone on this board" in bare[0].line()
    assert guard_lines(())[0] == "guards: pcbc wrote no guard copper on this board"


def _with_gnd_pour(text: str) -> str:
    """A filled `GND` zone over the whole board, appended the way KiCad writes one.

    Only what `route_verify.zones` reads: the net name, the layer and one `filled_polygon`. It is a
    rectangle rather than the real pour's outline, which makes this test about the containment
    arithmetic and not about KiCad's fill."""
    poly = "".join(f"\n\t\t\t\t\t(xy {x} {y})" for x, y in ((0, 0), (40, 0), (40, 30), (0, 30)))
    zone = (
        '\n\t(zone\n\t\t(net 1)\n\t\t(net_name "GND")\n\t\t(layer "B.Cu")\n\t\t(min_thickness 0.25)'
        f"\n\t\t(filled_polygon\n\t\t\t(layer \"B.Cu\")\n\t\t\t(pts{poly}\n\t\t\t)\n\t\t)\n\t)"
    )
    return text.rstrip()[:-1] + zone + "\n)\n"


def test_a_ground_with_no_pour_is_a_pattern_refusal_naming_the_board_edit():
    """The blinky trap, and section 3 keeps it here rather than in the compiler because its move is a
    `Board(...)` edit that reads better beside the geometry. blinky's routed board has **zero**
    `(zone ...)` blocks, so sixteen stitch vias there would weld to nothing."""
    ctx, merged = _routed_to_patterns("guard")
    scene = ctx.scene
    scene.plane_of.pop("GND", None)
    plan = pattern_copper(ctx.design, ctx.job, ctx.cs, merged.text, "guard", stage="final", owned=ctx.owned, scene=scene)
    moves = [m for m in plan.moves if m.startswith("guard")]
    assert len(moves) == 2, moves
    assert "has no plane or pour on this board" in moves[0]
    assert 'NetReq("GND", kind="power")' in moves[0] and 'Board(planes=' in moves[0]
    assert not [p for p in plan.pieces if p.reason == "guard"]


# ---------------------------------------------------------------------------------------------
# What the rest of the tool had to learn about a guard
# ---------------------------------------------------------------------------------------------


def test_a_guard_is_a_branch_a_redundancy_and_not_a_rail():
    """Three vocabularies the guard had to be added to, and they are three different questions.

    `BRANCH_REASONS` is about the **via**: a stitch via carries no net current across layers, so it
    takes the fab's standard via and not the ground class's. `REDUNDANT` is about the **route**: a
    shield's millimetres are not part of any net's path, so counting them makes `detour` report a rail
    that got worse. `NOT_A_RAIL` is about the **current**, and it is deliberately *smaller* than
    `REDUNDANT` — a parallel rung and a thermal barrel are redundant for connectivity and fully
    load-bearing for current, and S4's whole acceptance is that a rung moves
    `ampacity.power_bottlenecks`' answer.
    """
    assert "guard" in REASONS and "guard" in BRANCH_REASONS
    # S8's `"plane"` joined `REDUNDANT` and `BRANCH_REASONS` and stayed out of `NOT_A_RAIL`, by these
    # same three questions: a lattice barrel carries no current across layers (a branch), its
    # millimetres are on no net's path (redundant), and it *is* on the net whose current is in question
    # — two pours of that net, tied — so it is not the guard's case.
    assert REDUNDANT == ("guard", "plane", "stitch", "thermal")
    assert NOT_A_RAIL == ("guard",), "not REDUNDANT: a rung and a barrel carry the rail's current"
    from pcbc.copper import PATTERN_NECK_OK

    assert "guard" in PATTERN_NECK_OK, (
        "a shield's width is B.6's `stack.track_min` and it carries no rail current; measured, the "
        "gate stopped the fixture's build with `GND guard copper 0.127 mm < the 0.4 mm GND class` "
        "when its GND was declared at 0.5 A"
    )


def test_guard_ground_reaches_the_router_and_an_undeclared_one_is_refused(tmp_path: Path):
    """Section 2(r): `Guard(ground=)` had been parsed and then dropped since R1. The refusal is the
    mirror of the one the guarded net already had, and it lives in the compiler because that is the
    one place holding the netlist."""
    body = (
        'from pcbc import Board, Capacitor, Ground, Guard, Net, NetReq, Power\n'
        'V = Power("3V3")\nG = Ground("GND")\nS = Net("SIG")\n'
        'c = dict(package="0805", manufacturer="Yageo", mpn="CC0805KRX7R9BB104", lcsc="C49678")\n'
        'Capacitor("C1", "100nF", p1=V, p2=G, **c)\nCapacitor("C2", "100nF", p1=S, p2=G, **c)\n'
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")\n'
        'NetReq("3V3", "GND", kind="power", volts=3.3, amps=0.1)\n'
    )
    good = tmp_path / "good.py"
    good.write_text(body + 'Guard("SIG", ground="GND")\n')
    cs = compile_design(load_board(good)).constraints
    assert cs.refusals == () and cs.by_net("SIG").guard_ground == "GND"
    bad = tmp_path / "bad.py"
    bad.write_text(body + 'Guard("SIG", ground="GNDD")\n')
    cs2 = compile_design(load_board(bad)).constraints
    assert cs2.refusals and 'no net "GNDD" to make the guard out of' in cs2.refusals[0], cs2.refusals
    assert "did you mean 'GND'?" in cs2.refusals[0]


@pytest.mark.parametrize("name", ["blinky", "buck", "c3_usb", "node"])
def test_the_example_boards_declare_no_guard_and_the_stage_writes_none(name: str):
    """S7's acceptance: the four example boards are **unmoved**. None declares a `Guard()`, so
    `_guard_specs` has nothing to iterate and the carrier cannot write a millimetre on any of them —
    for a structural reason and not a tuned bound, which is the distinction `spine.WIDE_MM` got right
    and the chain got wrong."""
    design = load_board(_board(name))
    job = compile_design(design)
    assert design.guards == [], name
    assert [c.net for c in job.constraints.constraints if c.guard_stitch_mm is not None] == [], name
    plan, _ctx = _final(name)
    assert not [p for p in plan.pieces if p.reason == "guard"], name
    assert not [n for n in plan.notes if "guard" in n], name


# ---------------------------------------------------------------------------------------------
# The whole path, on a routed board
# ---------------------------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.krt
def test_the_fixture_builds_to_fab_with_the_guard_gate_verified(tmp_path: Path):
    """The one thing a unit test cannot do, and section 7.1 is the list of reasons: pin a report
    string, run the `final` stage inside `route_job`, and prove a stitch via lands in a pour that only
    `gnd_pour` writes.

    Measured 2026-09-21: the guard survives every KRT step after it — there are none — and the plane
    comes back as **one** island. The coverage is lower than the placed-board measurement above
    because `final` sees KRT's copper too, which is the honest cost of the stage and the reason the
    note is a `Coverage` and not a pass/fail.
    """
    from pcbc.build import build_job

    work = tmp_path / "guard.py"
    work.write_text(FIXTURE.read_text())
    result = build_job(work, upto="fab", force=True)
    assert result["error"] is None, result["error"]
    route = next(s for s in result["steps"] if s.get("stage") == "route")
    assert route["guards"]["fails"] == [], route["guards"]["lines"]
    assert route["guards"]["lines"][-1] == "guards: 2 of 2 shield(s) welded to their pour on every piece"
    assert route["planes"]["islands"] == {"GND B.Cu": 1}, route["planes"]["islands"]
    assert route["copper_bar"]["totals"]["vias_pattern"]["guard"] == 11
    assert any(n.startswith("guard AIN: deferred") for n in route["notes"])
