"""The native router's own arithmetic (`route_cost`, `route_pair`, `route_native.net_order`), asked
without KiCad: the cost terms are the closed forms docs/native-plan.md §3 names, and one in-memory
route of buck is complete and deterministic.

Every number below is the formula evaluated, not a recording, except where a sentence says it was
measured (2026-09-25, the native router at N0).
"""

from __future__ import annotations

import math
from pathlib import Path

from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.route_cost import BUDGET, TAN_HALF, NetCost, net_cost, run_cost
from pcbc.route_native import ORDER, constrained_nets, net_order, route_stage
from pcbc.route_pair import offset

ROOT = Path(__file__).resolve().parent.parent
BUCK = ROOT / "examples" / "buck" / "buck.py"


def _nc(width=0.2, clearance=0.15, via_dia=0.6, per_change=1, via_allowed=True) -> NetCost:
    return NetCost(
        net="N", width_mm=width, clearance_mm=clearance, layers=("F.Cu", "B.Cu"), via_allowed=via_allowed,
        via_count_max=None, via_dia=via_dia, via_drill=0.3, via_per_change=per_change, length_max_mm=None,
    )


def test_the_bend_term_is_pitch_times_tan_half_theta_exactly():
    """cost = length + sum(pitch * tan(theta/2)) + vias * via_mm: a 45-degree turn costs pitch*(sqrt2-1),
    a right angle one pitch, 135 degrees pitch*(sqrt2+1), and a U-turn is refused by price."""
    assert TAN_HALF == {0: 0.0, 1: math.sqrt(2) - 1, 2: 1.0, 3: math.sqrt(2) + 1}
    nc = _nc()
    assert nc.pitch_mm == 0.2 + 0.15
    assert [nc.turn_mm(k) for k in range(4)] == [nc.pitch_mm * TAN_HALF[k] for k in range(4)]
    assert nc.turn_mm(4) >= 1e6, "a U-turn is never cheaper than any detour"
    assert nc.bend_mm(90.0) == nc.pitch_mm and nc.bend_mm(0.2) == 0.0


def test_a_via_costs_the_area_it_takes_in_track_millimetres_and_is_infinite_when_refused():
    """via_mm = per_change * pi * (dia/2 + clearance)^2 / pitch: the lattice area a via's keepout takes,
    as the length of track that would take the same area; per_change multiplies it (a 2 A rail's
    three barrels cost three), and a net that may not carry a via cannot buy one."""
    nc = _nc(via_dia=0.6, per_change=1)
    assert math.isclose(nc.via_mm, math.pi * (0.3 + 0.15) ** 2 / 0.35)
    assert math.isclose(_nc(per_change=3).via_mm, 3 * nc.via_mm)
    assert _nc(via_allowed=False).via_mm == math.inf


def test_a_run_costs_its_length_plus_its_bends():
    """An L of 2 mm + 1 mm with one right angle costs 3 mm plus one pitch; a straight run its length."""
    nc = _nc()
    assert math.isclose(run_cost(((0, 0), (2, 0), (2, 1)), nc), 3.0 + nc.pitch_mm)
    assert math.isclose(run_cost(((0, 0), (1, 1), (3, 1)), nc), math.sqrt(2) + 2 + nc.pitch_mm * TAN_HALF[1])
    assert run_cost(((0, 0), (5, 0)), nc) == 5.0


def test_a_pair_half_is_the_centreline_offset_and_keeps_every_leg_direction():
    """`route_pair.offset` moves each leg along its normal and re-intersects the corners, so the two
    halves of a pair are parallel leg for leg at exactly the offset, and a 45 stays a 45."""
    centre = ((0.0, 0.0), (4.0, 0.0), (6.0, 2.0))
    left, right = offset(centre, 0.2), offset(centre, -0.2)
    assert left[0] == (0.0, -0.2) and right[0] == (0.0, 0.2)
    for half in (left, right):
        assert len(half) == 3
        dirs = [(round(b[0] - a[0], 6) != 0, round(b[1] - a[1], 6) != 0) for a, b in zip(half, half[1:])]
        assert dirs == [(True, False), (True, True)], half
        (x0, y0), (x1, y1) = half[1], half[2]
        # To the 1 nm grid every corner is put on (`qp`): each end may move half a nanometre.
        assert math.isclose(abs(x1 - x0), abs(y1 - y0), abs_tol=2e-6), "the diagonal leg stays at 45 degrees"


def test_the_order_is_constrained_nets_first_then_widest_then_by_name():
    """`ORDER = "constrained"`: a net with a constraint the router must honour goes before any free net,
    each group widest first, ties by name — a count, never a clock."""
    assert ORDER == "constrained"
    design = load_board(BUCK)
    job = compile_design(design)
    cs = job.constraints
    nets = sorted(design.nets)
    con = constrained_nets(job, design)
    got = net_order(None, nets, cs, constrained=con)
    firsts = [n for n in got if n in con]
    assert got[: len(firsts)] == firsts, (got, con)
    for group in (firsts, got[len(firsts):]):
        keys = [(-net_cost(cs, n).width_mm, n) for n in group]
        assert keys == sorted(keys), group


def test_buck_routes_every_link_in_memory_and_the_same_twice():
    """Measured 2026-09-25: the router makes all 11 of buck's router links (5V 1, BOOT 1, EN 1, FB 2,
    GND 2, SW 2, VIN 2) without KiCad and without a board file, and a second run in the same process
    gives the same pieces in the same order. BUDGET is an expansion count (200000), so this cannot
    depend on how fast the machine is."""
    from pcbc.place_native import place

    assert BUDGET == 200000
    runs = []
    for _ in range(2):
        design = load_board(BUCK)
        pl = place(design, name="buck")
        out = route_stage(design, pl.job, pl, name="buck")
        runs.append(out)
    a, b = runs
    assert a.failed == [] and a.unrouted == [], [(f.net, f.a, f.b) for f in a.failed]
    assert sum(t for _d, t in a.links.values()) == sum(d for d, _t in a.links.values()) == 11, a.links
    assert [repr(p) for p in a.pieces] == [repr(p) for p in b.pieces]
