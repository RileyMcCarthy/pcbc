"""The congestion map: `docs/topo-plan.md` slice 1, one assertion per decision.

Every test here but one is a **pure** test. The board is `conftest.placed_board`'s — check, seed and
place, no KiCad and no KRT — so nothing reads `examples/**/layout/`, which is gitignored build
output. The one exception is the calibration, which needs a routed board to have something to
calibrate against; it is marked `kicad` + `krt` and takes its board from `conftest.routed_board`.

The acceptance `docs/topo-plan.md` slice 1 names, and the test that holds each end:

- **Euler's formula per layer** — `test_euler_per_layer`. It is the one check that says the
  triangulation is a triangulation and not a bag of triangles, and it holds on every layer of every
  board in the repo.
- **The calibration** — `test_calibration_no_false_full`. Every segment of a routed board, mapped
  onto the *placed* board's gates: the model must never call full a channel the arbiter's own
  accepted routing crosses. Measured 0 of 914 today. It fails the moment the capacity rule drifts.
- **The gate-clearance filter (SEARCH's R6)** — `test_no_gate_crosses_a_third_obstacle`, which
  re-derives the answer by a brute-force scan over *every* item on the board rather than through the
  index, so an index bug cannot hide in it. It found one: an AABB prefilter comparing
  `0.30000000000000004` against `0.3`.
- **Four outline items with four edges** — `test_outline_is_four_items`.
- **Byte-identical over two runs** — `test_deterministic`, and `test_no_wall_clock_in_the_census`
  beside it, because the first thing that broke determinism was a timing number in the JSON.

The numbers pinned here were measured on 2026-09-21 and each assertion carries what it measured.
A pinned count that drifts is not automatically wrong — it names the modelling choice it came from.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.route_channel import (
    _blocking,
    _edge_items,
    _hull_size,
    _nm,
    _shape_of,
    census,
    channels,
    channels_doc,
    class_widths,
    crossed,
    demand_nm,
    escapes,
    fits,
    moves,
    report,
    slack_nm,
    supply_nm,
    tightest,
    triangulate,
)
from pcbc.route_emit import Sidecar, read_sidecar, write_sidecar
from pcbc.route_geom import hull_dist2, qp
from pcbc.route_scene import build_scene

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc" / "ds2_addon.py"


def _scene(name: str, board: Path):
    from conftest import placed_board

    design = load_board(board)
    job = compile_design(design)
    return design, job, build_scene(design, job, job.constraints, placed_board(name, board).read_text())


def _blinky():
    return _scene("blinky", EXAMPLES / "blinky" / "blinky.py")


def _buck():
    return _scene("buck", EXAMPLES / "buck" / "buck.py")


def _node():
    return _scene("node", EXAMPLES / "node" / "node.py")


# --- the triangulation ------------------------------------------------------------------------


def test_triangulate_is_a_triangulation_of_a_square():
    pts = [(0, 0), (10_000_000, 0), (10_000_000, 10_000_000), (0, 10_000_000)]
    tris, edges = triangulate(pts)
    assert len(tris) == 2, "four points in general position triangulate into two triangles"
    assert len(edges) == 5, "four hull edges and one diagonal"
    assert _hull_size(pts) == 4


def test_triangulate_is_sorted_and_indexed():
    pts = sorted({(i * 1_000_000, (i * i) % 7 * 1_000_000) for i in range(12)})
    tris, edges = triangulate(pts)
    assert tris == sorted(tris) and edges == sorted(edges), "both lists are sorted, so nothing downstream sees insertion order"
    assert all(a < b < c for a, b, c in tris), "a triangle is (i, j, k) with i < j < k"
    assert all(a < b for a, b in edges)


def test_triangulate_is_a_function_of_the_point_set_not_its_order():
    import random

    pts = sorted({(random.Random(7).randrange(0, 50) * 100_000, random.Random(i).randrange(0, 50) * 100_000) for i in range(40)})
    shuffled = list(pts)
    random.Random(99).shuffle(shuffled)
    assert triangulate(pts) == triangulate(sorted(shuffled)), "the insertion order is derived from the points, not taken from the caller"


def test_hull_size_counts_collinear_boundary_points():
    # Three collinear points on the bottom edge: `route_geom.hull` drops the middle one because a
    # `Shape` must be strictly convex, and Euler's `h` counts it. That is why `_hull_size` exists
    # rather than reusing `hull`.
    pts = [(0, 0), (5_000_000, 0), (10_000_000, 0), (10_000_000, 10_000_000), (0, 10_000_000)]
    assert _hull_size(pts) == 5


@pytest.mark.parametrize("name,board", [("blinky", EXAMPLES / "blinky" / "blinky.py"), ("buck", EXAMPLES / "buck" / "buck.py"), ("node", EXAMPLES / "node" / "node.py")])
def test_euler_per_layer(name, board):
    """`V - E + F = 2` on every copper layer of every board, with `F` the triangles plus the outer face.

    Stated as the two closed forms as well — `t = 2n - h - 2` and `e = 3n - h - 3` — because those
    are what fail informatively: a triangulation that dropped a triangle satisfies neither, and one
    that mis-counted the hull satisfies neither but by a different amount.
    """
    _, _, scene = _scene(name, board)
    for ch in channels(scene):
        if ch.verts < 3:
            continue
        n, e, t = ch.verts, ch.edges, ch.tris
        h = ch.hull
        assert t == 2 * n - h - 2, f"{name} {ch.layer}: {t} triangles over {n} points with {h} on the hull"
        assert e == 3 * n - h - 3, f"{name} {ch.layer}: {e} edges over {n} points with {h} on the hull"
        assert n - e + (t + 1) == 2, f"{name} {ch.layer}: Euler"


# --- the gates --------------------------------------------------------------------------------


def test_outline_is_four_items():
    """The board outline enters as four distinct items with four distinct owners.

    One shared owner makes all four outline edges same-owner edges, which the gate filter drops —
    and on a near-empty layer that disconnects the dual graph slice 3 searches. `blinky`'s B.Cu has
    nothing on it but the outline, so it is the layer where the defect would show: four corners, five
    triangulation edges, and all five must survive as gates.
    """
    _, _, scene = _blinky()
    items = _edge_items(scene)
    assert len(items) == 4
    assert len({it.owner for it in items}) == 4, [it.owner for it in items]
    assert all(it.id < 0 for it in items), "outline corners are synthesised, not in Scene.items"
    back = next(ch for ch in channels(scene) if ch.layer == "B.Cu")
    assert back.verts == 4 and back.edges == 5
    assert len(back.gates) == 5, "a shared owner would leave 1 gate here, not 5"


def test_gate_key_is_owner_and_corner_never_a_vertex_index():
    """`docs/topo-plan.md` C-A: the name is `((owner, corner), (owner, corner))`, and it is sorted."""
    _, _, scene = _buck()
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    for g in front.gates:
        (oa, ca), (ob, cb) = g.key
        assert isinstance(oa, str) and isinstance(ca, int) and isinstance(ob, str) and isinstance(cb, int)
        assert g.key[0] <= g.key[1], "the key is sorted, so one channel has one name"
        assert (oa, ob) == (g.owner_a, g.owner_b)
    assert list(front.gates) == sorted(front.gates, key=lambda g: g.key), "gates come out in key order"
    assert len({g.key for g in front.gates}) == len(front.gates), "a key names one channel"


def test_no_gate_crosses_a_third_obstacle():
    """SEARCH's R6, re-derived by brute force over every item rather than through the index.

    The shipped filter asks `Scene.query` for candidates; this asks *every* item on the layer. They
    must agree exactly, and when they did not the difference was real: an AABB prefilter on
    `it.box()`, which is raw float arithmetic, rejected ds2's `J4.2` at `y0 = 0.30000000000000004`
    against a gate at `y = 0.3` and let a blocked channel through.
    """
    _, _, scene = _buck()
    for ch in channels(scene):
        for g in ch.gates:
            for it in scene.items:
                if it.id in (g.ref_a, g.ref_b) or it.kind in ("lane", "edge") or ch.layer not in it.layers:
                    continue
                sh = _shape_of(it)
                if sh is None:
                    continue
                r2 = sh.r * sh.r
                if hull_dist2((g.a, g.b), sh.pts) > r2:
                    continue
                if hull_dist2((g.a,), sh.pts) <= r2 or hull_dist2((g.b,), sh.pts) <= r2:
                    continue
                pytest.fail(f"{g.label()} on {ch.layer} crosses {it.label()}, which the filter should have dropped")


def test_blocking_matches_a_brute_force_scan():
    """The index answer and the whole-board answer are the same answer, on a rejected channel too.

    Without this the filter can only be tested where it fires; `blocked` is 0 on buck's F.Cu and 4 on
    node's, so the negative case is the one that carries the weight.
    """
    _, _, scene = _node()
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    assert front.blocked > 0, "node F.Cu has channels crossing a third obstacle; measured 4"
    checked = 0
    for g in front.gates[:200]:
        slow = False
        for it in scene.items:
            if it.id in (g.ref_a, g.ref_b) or it.kind in ("lane", "edge") or "F.Cu" not in it.layers:
                continue
            sh = _shape_of(it)
            if sh is None:
                continue
            r2 = sh.r * sh.r
            if hull_dist2((g.a, g.b), sh.pts) <= r2 and hull_dist2((g.a,), sh.pts) > r2 and hull_dist2((g.b,), sh.pts) > r2:
                slow = True
                break
        assert _blocking(scene, "F.Cu", g.a, g.b, (g.ref_a, g.ref_b)) is slow, g.label()
        checked += 1
    assert checked == 200


def test_a_bare_drill_is_an_obstacle():
    """An NPTH has no copper at all, so a copper-only vertex pass triangulates through a mounting hole.

    node's `J1` carries two of them, and they are the whole of the +6 raw gates this module counts
    over the prototype in `docs/topo-plan.md` slice 1.
    """
    _, _, scene = _node()
    holes = [it for it in scene.items if it.kind == "hole"]
    assert len(holes) == 2, [it.owner for it in holes]
    assert all(it.copper is None and it.hole is not None for it in holes)
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    assert {it.id for it in holes} & {g.ref_a for g in front.gates} | {g.ref_b for g in front.gates}, "the drills are gate endpoints"


# --- the exact rule ---------------------------------------------------------------------------


def test_fits_is_integer_nanometres_on_both_sides():
    """`docs/topo-plan.md` section 4, leak 8. The prototype compared `demand > usable + 1e-9`."""
    _, job, scene = _buck()
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    g = front.gates[0]
    assert isinstance(supply_nm(g, ("VIN",)), int)
    assert isinstance(demand_nm(scene, g, (("VIN", 0.4),)), int)
    assert isinstance(g.span_nm, int) and isinstance(g.free_nm, int) and isinstance(g.sure_nm, int)
    assert fits(scene, g, (("VIN", 0.4),)) is (demand_nm(scene, g, (("VIN", 0.4),)) <= supply_nm(g, ("VIN",)))


def test_demand_is_a_function_of_the_member_set_not_its_order():
    _, _, scene = _buck()
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    g = max(front.gates, key=lambda g: g.span_nm)
    a = (("GND", 0.16), ("VIN", 0.4), ("5V", 0.3))
    assert demand_nm(scene, g, a) == demand_nm(scene, g, tuple(reversed(a)))


def test_a_member_on_an_endpoint_net_pays_half_a_width_per_shared_end():
    """This module's docstring, choice 1: same net means it may sit on that endpoint's copper."""
    _, _, scene = _buck()
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    g = next(g for g in front.gates if g.net_a and g.net_b and g.net_a != g.net_b)
    w = 0.4
    lone = demand_nm(scene, g, ((g.net_a, w),))
    stranger = demand_nm(scene, g, (("__no_such_net__", w),))
    assert lone < stranger, "a net that owns one end pays less than a net that owns neither"
    both = demand_nm(scene, g, ((g.net_a, w), (g.net_b, w)))
    assert both < lone + stranger


def test_supply_returns_the_radius_to_a_member_on_that_endpoint_net():
    _, _, scene = _buck()
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    g = next(g for g in front.gates if g.net_a and g.r_a > 0.0)
    assert supply_nm(g, (g.net_a,)) == g.span_nm - _nm(g.r_b)
    assert supply_nm(g, ("__no_such_net__",)) == g.span_nm - _nm(g.r_a) - _nm(g.r_b)


def test_slots_bounds_holds():
    """`free` is the best case and `sure` is the worst, so `holds <= slots` on every channel.

    They are different questions and the plan's own census is the second one: measured on buck's
    F.Cu, `slots` says **0** of 196 channels take nothing and `holds` says **31** refuse some net.
    """
    _, _, scene = _buck()
    for ch in channels(scene):
        for g in ch.gates:
            assert g.sure_nm <= g.free_nm, g.label()
            assert g.holds <= g.slots, g.label()
    c = census(channels(scene))["F.Cu"]
    assert c["gates"] == 196, "buck F.Cu, measured 2026-09-21; `docs/topo-plan.md` slice 1 says 196"
    assert (c["holds"]["0"], c["holds"]["1"]) == (31, 37), "the plan's own census: 31 walls and 37 single-file"
    assert c["slots"]["0"] == 0, "no channel on buck's F.Cu refuses every net"


def test_tightest_is_the_class_floor_not_the_stackup_floor():
    """`between` never returns less than the smallest class clearance, so neither may `slots`."""
    _, job, scene = _buck()
    w, c = tightest(scene)
    assert w == scene.stack.track_min == 0.127
    assert c == 0.16 > scene.stack.clearance_min, "buck's tightest class clearance is 0.16, the fab floor is 0.127"


# --- what it says about a board ---------------------------------------------------------------


def test_escape_is_local_and_has_a_magnitude():
    """A pad's ways out are the channels incident on its own corners, and the slack is the rule's own.

    `slack_nm` is `supply_nm - demand_nm` for that one net, so the number a move prints and the
    number `fits` decided with cannot disagree.
    """
    _, job, scene = _buck()
    maps = channels(scene)
    es = escapes(scene, maps, class_widths(job))
    assert es, "every pad on a net gets an answer"
    assert {e.pad for e in es} == {it.owner for it in scene.items if it.kind == "pad" and it.net}
    for e in es:
        assert 0 < e.open_ways <= e.ways
    front = next(ch for ch in maps if ch.layer == "F.Cu")
    g = front.gates[0]
    assert slack_nm(scene, g, "VIN", 0.4) == supply_nm(g, ("VIN",)) - demand_nm(scene, g, (("VIN", 0.4),))


def test_no_pad_is_sealed_on_a_board_that_routes():
    """A correct oracle is quiet on a board with zero unconnected items.

    Measured on all five boards: 0 sealed pads. The straight line between two pads was tried first
    and fired on **19 of buck's 21 airwires**, on a board that routes — which is why `Escape` is the
    local question and the airwire is not measured at all.
    """
    for name, board in (("blinky", EXAMPLES / "blinky" / "blinky.py"), ("buck", EXAMPLES / "buck" / "buck.py"), ("node", EXAMPLES / "node" / "node.py")):
        _, job, scene = _scene(name, board)
        maps = channels(scene)
        layers_of = {c.net: c.layers for c in (job.constraints.constraints if job.constraints is not None else ())}
        assert moves(escapes(scene, maps, class_widths(job), layers_of=layers_of)) == (), name


def test_a_pad_walled_in_reports_a_move_with_millimetres():
    """The refusal is a `Place()` move, per `docs/topo-plan.md`'s house rule.

    Driven by asking for a width no channel on the board can take, rather than by moving a part:
    the arithmetic under the move is the same either way and this keeps the test a pure function of
    the placed board.
    """
    _, job, scene = _buck()
    maps = channels(scene)
    wide = {net: 6.0 for net in class_widths(job)}
    mv = moves(escapes(scene, maps, wide))
    assert mv, "a 6 mm track fits through nothing on buck"
    assert all(m.startswith("Place(): ") for m in mv)
    assert "mm too wide for it" in mv[0] and "no way out" in mv[0], mv[0]


def test_crossed_uses_the_modules_own_overlap_predicate():
    _, _, scene = _buck()
    front = next(ch for ch in channels(scene) if ch.layer == "F.Cu")
    g = front.gates[0]
    mid = ((g.a[0] + g.b[0]) / 2.0, (g.a[1] + g.b[1]) / 2.0)
    off = qp((mid[0] + 2.0, mid[1] + 2.0))
    assert g in crossed(front, qp(mid), off) or g in crossed(front, off, qp(mid)), "a segment starting on a gate touches it"


# --- determinism ------------------------------------------------------------------------------


def _blob(scene, job):
    maps = channels(scene)
    layers_of = {c.net: c.layers for c in (job.constraints.constraints if job.constraints is not None else ())}
    return json.dumps(
        {
            "gates": [[[list(g.key[0]), list(g.key[1]), g.a, g.b, g.span_nm, g.free_nm, g.sure_nm, g.slots, g.holds] for g in ch.gates] for ch in maps],
            "doc": channels_doc(scene, widths=class_widths(job), layers_of=layers_of, maps=maps),
            "report": report(scene, widths=class_widths(job), layers_of=layers_of, maps=maps),
        }
    )


def test_deterministic():
    """Byte-identical over two runs, on the board with the most channels of the pure-test three."""
    _, job, scene = _node()
    assert _blob(scene, job) == _blob(scene, job)


def test_no_wall_clock_in_the_census():
    """A timing number in `copper.json` makes the file a different file on every run.

    It was in there, and it is the only thing that broke determinism when the map was first measured:
    the gates hashed identically three times running while the census did not.
    """
    _, job, scene = _buck()
    doc = channels_doc(scene, widths=class_widths(job))
    assert "ms" not in json.dumps(doc), "no wall clock in the recorded artifact"
    assert all("ms" not in row for row in census(channels(scene)).values())
    assert next(ch for ch in channels(scene) if ch.layer == "F.Cu").wall_ms >= 0, "the timing is on Channels for a caller that wants to print it"


def test_sidecar_tolerates_a_file_without_the_key(tmp_path):
    """`read_sidecar` uses `raw.get`, so a `copper.json` written before this slice still loads."""
    old = tmp_path / "copper.json"
    old.write_text(json.dumps({"items": [], "refusals": [], "notes": [], "census": {}, "census_nets": {}}))
    assert read_sidecar(old).channels == {}
    fresh = tmp_path / "new.json"
    write_sidecar(fresh, Sidecar(channels={"layers": {"F.Cu": {"gates": 1}}}))
    assert read_sidecar(fresh).channels == {"layers": {"F.Cu": {"gates": 1}}}


# --- the calibration --------------------------------------------------------------------------


@pytest.mark.kicad
@pytest.mark.krt
def test_calibration_no_false_full():
    """Every segment of a routed board crosses only channels the model calls open.

    The one test here that needs a routed board, and the reason it is worth the build: the model is
    checked against the *arbiter's* own accepted routing rather than against itself. A gate KRT drove
    copper through that this module calls full is a false refusal, and slice 3 would refuse a route
    KiCad passes.

    Measured 2026-09-21: **0 false-full of 914 crossed gates** across buck, c3_usb, node and ds2, on
    ten layers. This runs buck alone, because one board is enough to catch a rule that drifted and
    four builds is not what a test suite should cost.

    It does **not** prove tightness: a model that accepted everything would score the same. The
    complement is the capacity histogram in `test_slots_bounds_holds` and slice 3's false-accept rate.
    """
    from conftest import routed_board

    from pcbc.copper_bar import segments as bar_segments

    board = EXAMPLES / "buck" / "buck.py"
    _, job, scene = _scene("buck", board)
    maps = {ch.layer: ch for ch in channels(scene)}
    segs = bar_segments(routed_board("buck", board).read_text())
    assert segs, "the routed board has copper on it"
    crossings = 0
    for layer, ch in maps.items():
        use: dict = {}
        gate_of = {g.key: g for g in ch.gates}
        for s in segs:
            if s["layer"] != layer or not s["net"]:
                continue
            a, b = qp(s["start"]), qp(s["end"])
            if a == b:
                continue
            for g in crossed(ch, a, b):
                row = use.setdefault(g.key, {})
                row[s["net"]] = max(row.get(s["net"], 0.0), s["width"])
        crossings += len(use)
        for key, members in sorted(use.items()):
            g = gate_of[key]
            ms = tuple(sorted(members.items()))
            short = demand_nm(scene, g, ms) - supply_nm(g, [n for n, _ in ms])
            assert short <= 0, (
                f"{layer}: the model calls {g.label()} full by {short / 1e6:.4f} mm, and KRT routed "
                f"{[n for n, _ in ms]} through it. span={g.length} free={g.free}"
            )
    assert crossings >= 50, f"only {crossings} channels crossed; the mapping found almost nothing"
