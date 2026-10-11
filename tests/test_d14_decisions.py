"""The owner's decisions of 2026-10-01 (*"ok do 1-4"*, `docs/direction.md` §5b) and buck's declared currents.

- decision 2: a switch-node path may change layer through a via group sized for its current, and its copper
  area is measured on the emitted board;
- decision 1: the pad neck (`constraints.PAD_NECK_MAX_MM`) — drawn by the router, accepted by the emitted-board
  checks only under its limits, judged by the same rule in the fab ampacity gate, listed in the report;
- decision 4: placement keeps a declared path's exit corridor clear of the parts it attaches with `Place(to=)`;
- decision 5: buck declares the currents of TI's Table 4-1 roles.

Decision 3 (the 2 oz stackups) is pinned in `test_stackup.py`.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from conftest import copy_board, declared_examples, edit_board, write_split
from pcbc.constraints import compile_constraints
from pcbc.language import load_board

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def _buck(tmp_path: Path, edits=(), name: str = "buck") -> Path:
    work = tmp_path / name
    # buck as shipped with a declared outline (a vehicle: conftest.declared_examples)
    shutil.copytree(declared_examples() / "buck", work, ignore=shutil.ignore_patterns("layout"))
    edit_board(work / "buck.py", edits)
    return work / "buck.py"


# --- decision 2: switch-node vias -----------------------------------------------------------------


def test_a_switch_node_path_changes_layer_through_a_via_group_sized_for_its_current(tmp_path: Path):
    """The switch_node preset no longer forbids vias or pins the net to F.Cu: buck's 2 A SW path may use
    F.Cu and B.Cu, and a layer change places the via group its current needs (the power nets' model)."""
    cs = compile_constraints(load_board(_buck(tmp_path)))
    c = cs.by_net("SW")
    assert c.via.allowed and c.layers == ("F.Cu", "B.Cu"), (c.via, c.layers)
    lk = c.paths[0].links[0]
    assert lk.via.allowed and lk.layers == ("F.Cu", "B.Cu")
    g = lk.group
    assert g is not None and (g.amps, g.count, g.drill_mm) == (2.0, 4, 0.3), g
    assert g.worst_a <= g.rating_a, "the group's worst barrel is within one barrel's rating"
    line = next(x for x in cs.lines if x.startswith("SW: path U1.SW -> L1.1"))
    assert "4 vias per layer change" in line and "no vias" not in line, line
    assert "SW: layers F.Cu, B.Cu (preset switch_node)" in cs.lines


def test_the_switch_node_copper_area_is_the_union_of_its_tracks_and_vias_per_layer():
    """Decision 2: the area stays measured. A 10 mm x 1 mm F.Cu run and a 5 mm x 1 mm B.Cu run meeting at a
    0.6 mm via: each layer's area is its stadium (the via land is inside it), within the raster's error."""
    from pcbc.route_verify import switch_copper, switch_copper_lines

    text = """(kicad_pcb (version 20250000) (generator "pcbnew")
  (segment (start 0 0) (end 10 0) (width 1) (layer "F.Cu") (net "SW") (uuid "a1"))
  (segment (start 10 0) (end 10 5) (width 1) (layer "B.Cu") (net "SW") (uuid "a2"))
  (via (at 10 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "SW") (uuid "v1"))
  (via (at 20 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "SW") (uuid "v2"))
  (segment (start 0 3) (end 5 3) (width 1) (layer "F.Cu") (net "X") (uuid "a3"))
)"""
    cs = N(constraints=[N(kind="switch_node", net="SW"), N(kind="power", net="X")])
    (row,) = switch_copper(text, cs)
    land = math.pi * 0.3**2
    assert abs(row["areas_mm2"]["F.Cu"] - (10 + math.pi * 0.25 + land)) < 0.05, row
    assert abs(row["areas_mm2"]["B.Cu"] - (5 + math.pi * 0.25 + land)) < 0.05, row
    assert (row["vias"], row["length_mm"]) == (2, 15.0)
    assert switch_copper_lines([row])[0].startswith("SW: switch-node copper 17.2 mm2 (B.Cu 6.1 mm2, F.Cu 11.1 mm2), 2 vias, 15 mm of track")


# --- decision 1: the pad neck ---------------------------------------------------------------------

NECK = ROOT / "tests" / "fixtures" / "d14" / "neck.py"


def _neck_board(tmp_path: Path, edits=()) -> Path:
    return copy_board(NECK, tmp_path / "neck", edits)


def _route(board: Path):
    from pcbc.layout_emit import stamp
    from pcbc.layout_job import load_core
    from pcbc.place_native import place
    from pcbc.route_native import route_stage

    design = load_board(board)
    pl = place(design, name=board.stem, base=board.parent / "layout" / board.stem / "routed")
    assert pl.error is None, pl.error
    raw, _gr = load_core(design, board, pl, name=board.stem)
    core = stamp(design, raw) if raw else []
    return design, route_stage(design, pl.job, pl, name=board.stem, core_cu=core)


def test_the_router_leaves_a_pad_narrower_than_its_path_through_a_neck(tmp_path: Path):
    """A 3 A path (2.276 mm with no pour) leaves R1.1, an 0402 pad 0.48 mm from R1.2 [GND]: no full-width stub
    starts at its centre on any side. The router leaves through a neck at the pad's own 0.64 mm across the exit,
    for 0.4 mm past its edge (within PAD_NECK_MAX_MM), then runs at the path's full width."""
    from pcbc.constraints import PAD_NECK_MAX_MM

    _design, rt = _route(_neck_board(tmp_path))
    assert rt.unrouted == [], [(f.a, f.b, f.why) for f in rt.failed]
    mine = [p for p in rt.pieces if p.link == "R1.1~R2.1" and p.kind == "seg"]
    narrow = [p for p in mine if p.w < 2.276 - 1e-6]
    assert [(p.a, p.b, p.w) for p in narrow] == [((7.49, 10.0), (6.82, 10.0), 0.64)], [(p.a, p.b, p.w) for p in mine]
    assert 7.22 - 6.82 <= PAD_NECK_MAX_MM, "R1.1's left edge is x = 7.22"
    assert any(p.w == 2.276 and (6.82, 10.0) in (p.a, p.b) for p in mine), "the neck ends in full-width copper"


def test_a_path_no_wider_than_its_pads_gets_no_neck(tmp_path: Path):
    """At 2 A (1.09 mm) the same link starts at full width from R1.1's centre: nothing on the link is narrower."""
    _design, rt = _route(_neck_board(tmp_path, [('Path("PWR", "R1.1", "R2.1", amps=3)', 'Path("PWR", "R1.1", "R2.1", amps=2)')]))
    assert rt.unrouted == []
    mine = [p for p in rt.pieces if p.link == "R1.1~R2.1" and p.kind == "seg"]
    assert mine and all(abs(p.w - 1.09) < 1e-9 for p in mine), [(p.a, p.b, p.w) for p in mine]


@pytest.fixture(scope="module")
def neck_built(tmp_path_factory):
    """The neck fixture built once (KiCad): its emitted board, design and job."""
    from pcbc.build import build_job
    from pcbc.compile import compile_design

    d = tmp_path_factory.mktemp("neckb")
    shutil.copy(NECK, d / "neck.py")
    shutil.copy(NECK.parent / "layout.core.py", d / "layout.core.py")  # its placement and outline
    shutil.copy(NECK.parent / "schematic.core.py", d / "schematic.core.py")  # its schematic layout
    r = build_job(d / "neck.py", force=True)
    assert r.get("ok"), r.get("error")
    design = load_board(d / "neck.py")
    text = (d / "layout" / "neck" / "routed" / "layout.kicad_pcb").read_text()
    return r, text, design, compile_design(design)


_NECK_SEG = "\t\t(start 7.49 10)\n\t\t(end 6.82 10)\n\t\t(width 0.64)\n"


def _judge(text: str, job):
    from pcbc.copper import power_ampacity_failures
    from pcbc.fab import _owned_copper
    from pcbc.route_verify import gate_roles, pad_necks

    roles = gate_roles(text, job.constraints)
    necks = pad_necks(text, job.constraints, roles)
    return necks, power_ampacity_failures(job, text, owned=_owned_copper(roles), necks=necks)


@pytest.mark.kicad
def test_the_built_board_lists_its_neck_and_passes_the_gates(neck_built):
    """The emitted board's one neck is listed by the route stage and in FAB_NOTES, and the fab gates pass it."""
    r, text, _design, job = neck_built
    route = next(s for s in r["steps"] if s["stage"] == "route")
    assert route["necks"] == [
        "PWR: neck at R1.1 on F.Cu: 0.64 mm wide (R1.1 is 0.64 mm across) for 0.4 mm past its edge, into full-width copper; "
        "Path line 13's link R1.1~R2.1 (limit 1 mm)"
    ], route["necks"]
    assert route["neck_refusals"] == []
    assert _NECK_SEG in text
    (necks, refused), fails = _judge(text, job)
    assert len(necks) == 1 and refused == [] and fails == [], (necks, refused, fails)


@pytest.mark.kicad
@pytest.mark.parametrize(
    "name, old, new, why",
    [
        ("too long", _NECK_SEG + "\t\t(layer", "\t\t(start 7.49 10)\n\t\t(end 5.62 10)\n\t\t(width 0.64)\n\t\t(layer", "it runs 1.6 mm past R1.1's edge, over the 1 mm a pad neck may (constraints.PAD_NECK_MAX_MM)"),
        # Refuter round of 2026-10-02 (defect 12): the limit was tested only at 1.6 mm, so a tolerance of up to
        # +0.5 mm survived. 1.05 mm, just over the limit, is refused; 1.0 mm is accepted (the test below).
        ("a hundredth over the limit", _NECK_SEG, "\t\t(start 7.49 10)\n\t\t(end 6.21 10)\n\t\t(width 0.64)\n", "it runs 1.01 mm past R1.1's edge, over the 1 mm a pad neck may (constraints.PAD_NECK_MAX_MM)"),
        ("just over the limit", _NECK_SEG, "\t\t(start 7.49 10)\n\t\t(end 6.17 10)\n\t\t(width 0.64)\n", "it runs 1.05 mm past R1.1's edge, over the 1 mm a pad neck may (constraints.PAD_NECK_MAX_MM)"),
        ("too wide", _NECK_SEG, "\t\t(start 7.49 10)\n\t\t(end 6.82 10)\n\t\t(width 0.7)\n", "0.7 mm is wider than R1.1's 0.64 mm of copper across the segment"),
        ("into nothing", _NECK_SEG, "\t\t(start 7.49 10)\n\t\t(end 7.49 8.7)\n\t\t(width 0.5)\n", "its far end (7.49,8.7) lies in no 2.276 mm copper of the link, no via of it and no PWR pour on F.Cu"),
    ],
)
def test_a_narrow_link_piece_that_is_not_a_neck_fails_the_ampacity_gate(neck_built, name, old, new, why):
    """The emitted-board check accepts a narrow piece only under the neck's limits; each mutation of the one neck
    fails `power_ampacity_failures` with the fact that refused it."""
    _r, text, _design, job = neck_built
    assert text.count(old) == 1, name
    (necks, refused), fails = _judge(text.replace(old, new), job)
    assert necks == [] and [x["why"] for x in refused] == [why], (name, refused)
    assert len(fails) == 1 and fails[0].endswith("and it is not a pad neck: " + why), fails


@pytest.mark.kicad
def test_the_ampacity_measurement_treats_a_neck_by_the_same_rule(neck_built):
    """`power_bottlenecks` rates an accepted neck like the pad it leaves: it is not counted as rail narrower than
    the path (`under_mm`), and the path reads ok. Measured with the neck as a plain track, its 0.67 mm of 0.64 mm
    copper is counted under the path's 2.276 mm. (Here the full-width run's 1.138 mm end cap overlaps R1.1 itself,
    so the walk's best path does not pass through the neck either way.)"""
    from pcbc.ampacity import power_bottlenecks
    from pcbc.route_verify import gate_roles, pad_necks

    _r, text, _design, job = neck_built
    necks, _bad = pad_necks(text, job.constraints, gate_roles(text, job.constraints))
    keys = frozenset(n["key"] for n in necks)
    row = power_bottlenecks(job, text, necks=keys)["PWR (Path line 13)"]
    bare = power_bottlenecks(job, text)["PWR (Path line 13)"]
    assert row["verdict"] == "ok" and row["width_mm"] == 2.276, row
    assert round(bare["under_mm"] - row["under_mm"], 3) == 0.67, (bare["under_mm"], row["under_mm"])


@pytest.mark.kicad
def test_a_neck_at_exactly_the_limit_is_accepted(neck_built):
    """The other side of the length limit (refuter round, 2026-10-02, defect 12): the same neck run to exactly
    1.0 mm past R1.1's edge (x = 6.22; the edge is x = 7.22) is a neck, ending inside the link's full-width copper."""
    _r, text, _design, job = neck_built
    (necks, refused), fails = _judge(text.replace(_NECK_SEG, "\t\t(start 7.49 10)\n\t\t(end 6.22 10)\n\t\t(width 0.64)\n"), job)
    assert refused == [] and fails == [], (refused, fails)
    assert [(n["pad"], n["width_mm"], n["length_mm"], n["into"]) for n in necks] == [("R1.1", 0.64, 1.0, "full-width copper")], necks


def _split_at_r2(text: str) -> str:
    """The neck board with the last 2.276 mm run into R2.1 cut short at (19.5375, 11), and a 0.6 mm piece from
    there to R2.1's centre: 0.6187 mm of it lies past R2.1's edge, and R2.1 is 1.8258 mm across that diagonal."""
    old = "\t\t(start 18.8075 11.73)\n\t\t(end 20.5375 10)\n\t\t(width 2.276)\n"
    assert text.count(old) == 1
    i = text.index(old)
    s = text.rindex("\t(segment\n", 0, i)
    e = text.index("\n\t)\n", i) + 4
    blk = text[s:e]
    wide = blk.replace(old, "\t\t(start 18.8075 11.73)\n\t\t(end 19.5375 11)\n\t\t(width 2.276)\n")
    thin = blk.replace(old, "\t\t(start 19.5375 11)\n\t\t(end 20.5375 10)\n\t\t(width 0.6)\n").replace('(uuid "', '(uuid "f')
    return text[:s] + wide + thin + text[e:]


@pytest.mark.kicad
def test_a_narrow_piece_at_a_pad_no_narrower_than_the_path_is_not_a_neck(neck_built, tmp_path: Path):
    """Decision 1 allows a neck only where the path meets a pad narrower than the path (refuter round, 2026-10-02,
    defect 1: a 0.6 mm piece at a 1.75 mm pad on a 1.09 mm path was accepted and built through fab). The same
    0.6 mm piece into R2.1 is a neck on the 3 A path (2.276 mm, wider than R2.1's 1.8258 mm across it) and is
    refused, with the facts, when the path is 2 A (1.09 mm, narrower than R2.1)."""
    from pcbc.compile import compile_design

    _r, text, _design, job = neck_built
    cut = _split_at_r2(text)
    (necks, refused), fails = _judge(cut, job)
    assert refused == [] and fails == [], (refused, fails)
    assert ("R2.1", 0.6, 1.8258, 0.6187) in [(n["pad"], n["width_mm"], n["across_mm"], n["length_mm"]) for n in necks], necks
    job2 = compile_design(load_board(_neck_board(tmp_path, [('Path("PWR", "R1.1", "R2.1", amps=3)', 'Path("PWR", "R1.1", "R2.1", amps=2)')])))
    (necks, refused), fails = _judge(cut, job2)
    why = "R2.1 is 1.8258 mm across the segment, not narrower than the link's 1.09 mm on F.Cu"
    assert [x["why"] for x in refused] == [why], refused
    assert "R2.1" not in [n["pad"] for n in necks]
    assert len(fails) == 1 and fails[0].endswith("and it is not a pad neck: " + why), fails


_UP = "\t\t(start 7.49 10)\n\t\t(end 7.49 8.9)\n\t\t(width 0.5)\n"
_FAR = [7.49, 8.9]


def _role(net, owner, kind, layer, w, reason="route"):
    """One role item at the up-neck's far end (7.49, 8.9), in `roles_doc`'s shape."""
    if kind == "via":
        return {"key": ["via", list(_FAR), ["F.Cu", "B.Cu"]], "uuid": "t-via", "reason": reason, "net": net, "owner": owner, "w": w, "drill": 0.3}
    return {"key": ["seg", layer, [7.49, 8.9], [7.49, 7.5], w], "uuid": "t-seg", "reason": reason, "net": net, "owner": owner, "w": w, "drill": 0.0}


def _pour(net, layer):
    return f'\n\t(zone (net "{net}") (layer "{layer}") (filled_polygon (layer "{layer}") (pts (xy 7 8.5) (xy 8 8.5) (xy 8 9.2) (xy 7 9.2))))'


@pytest.mark.kicad
@pytest.mark.parametrize(
    "name, role, pour, into",
    [
        ("full-width copper of the link", ("PWR", "R1.1~R2.1", "seg", "F.Cu", 2.276), None, "full-width copper"),
        ("a via of the link", ("PWR", "R1.1~R2.1", "via", "", 0.6), None, "a via group"),
        ("the net's pour on the neck's layer", None, ("PWR", "F.Cu"), "the PWR pour"),
        ("narrower copper of the link", ("PWR", "R1.1~R2.1", "seg", "F.Cu", 1.0), None, None),
        ("full-width copper of another net", ("GND", "GND", "seg", "F.Cu", 2.276), None, None),
        ("full-width copper of the net no link owns", ("PWR", "PWR", "seg", "F.Cu", 2.276), None, None),
        ("a via of another net", ("GND", "GND", "via", "", 0.6), None, None),
        ("full-width copper of the link on the other layer", ("PWR", "R1.1~R2.1", "seg", "B.Cu", 2.276), None, None),
        ("another net's pour", None, ("GND", "F.Cu"), None),
        ("the net's pour on the other layer", None, ("PWR", "B.Cu"), None),
    ],
)
def test_each_way_a_neck_may_end_is_judged_on_its_own(neck_built, name, role, pour, into):
    """Refuter round, 2026-10-02 (defect 13): every ending clause survived a mutation alone, because the only ending
    test ended in nothing at all. Here the neck leaves R1.1 upward to (7.49, 8.9), 0.78 mm past its edge, 0.5 mm wide
    (R1.1 is 0.54 mm across it), where no copper of the board lies; one piece of copper is then put at that far end.
    Exactly one clause decides each case: full width (2.276 mm), the link's own copper, the same net, the same layer."""
    from pcbc.route_verify import gate_roles, pad_necks, zones
    from pcbc.route_emit import Sidecar

    _r, text, _design, job = neck_built
    t = text.replace(_NECK_SEG, _UP)
    roles = gate_roles(t, job.constraints)
    if pour:  # a filled zone as `route_verify.zones` reads it; the roles are read before it is added
        t = t.rstrip().rstrip(")") + _pour(*pour) + "\n)\n"
        assert any(z.net == pour[0] and z.layer == pour[1] for z in zones(t))
    items = list(roles.items) + ([_role(*role)] if role else [])
    necks, refused = pad_necks(t, job.constraints, Sidecar(items=items))
    mine = [n for n in necks if n["pad"] == "R1.1"]
    if into:
        assert [(n["width_mm"], n["length_mm"], n["into"]) for n in mine] == [(0.5, 0.78, into)], (name, necks, refused)
        assert [r for r in refused if r["a"] == [7.49, 10.0]] == [], refused
    else:
        assert mine == [], (name, mine)
        assert [r["why"] for r in refused if r["a"] == [7.49, 10.0]] == [
            "its far end (7.49,8.9) lies in no 2.276 mm copper of the link, no via of it and no PWR pour on F.Cu"
        ], (name, refused)


@pytest.mark.kicad
def test_a_fanout_or_tap_piece_of_a_link_narrower_than_the_link_fails_the_gate(neck_built):
    """Decision 1 ended the exemption a link's fanout and tap stubs had (refuter round, 2026-10-02, defect 14: nothing
    tested it). One of the link's 2.276 mm pieces, carried as `fanout` (then `tap`) at 1.0 mm, fails
    `power_ampacity_failures` as copper narrower than its link that is not a pad neck; as `guard` it is not judged."""
    from pcbc.copper import power_ampacity_failures
    from pcbc.fab import _owned_copper
    from pcbc.route_emit import Sidecar
    from pcbc.route_verify import gate_roles, pad_necks

    _r, text, _design, job = neck_built
    base = list(gate_roles(text, job.constraints).items)
    (piece,) = [i for i in base if i["key"][2] == [6.65, 10.17] and i["key"][3] == [6.65, 10.714]]
    for reason, failing in (("fanout", True), ("tap", True), ("guard", False)):
        thin = {**piece, "reason": reason, "w": 1.0, "key": ["seg", "F.Cu", [6.65, 10.17], [6.65, 10.714], 1.0]}
        roles = Sidecar(items=[thin if i is piece else i for i in base])
        necks = pad_necks(text, job.constraints, roles)
        fails = power_ampacity_failures(job, text, owned=_owned_copper(roles), necks=necks)
        hits = [f for f in fails if "(6.65,10.17)" in f and "1 mm" in f]
        assert bool(hits) is failing, (reason, fails)
        if failing:
            assert hits[0].startswith("PWR Path line 13's link R1.1~R2.1: copper 1 mm on F.Cu from (6.65,10.17) to (6.65,10.714) < the 2.276 mm"), hits


# --- the pad-gap check reads a board file's pad angle as KiCad writes it -----------------------------


def test_the_pad_gap_check_does_not_turn_a_turned_parts_pads_twice():
    """Refuter round, 2026-10-02 (defects 3 and 6): `copper.pad_clearance_failures` turned each pad by the footprint's
    angle plus the pad's own, but a board file's pad angle already holds the footprint's. The 2 oz buck copy stopped
    on `L1 (SW) vs C_IN1 (GND) pad gap 0.000 mm`; with its two pads exactly as that board wrote them, L1.1 spans
    y 13.606-16.03 and C_IN1.2 y 11.611-13.061 over a shared x range, so the gap is 0.545 mm."""
    from pcbc.copper import pad_clearance_failures

    text = """(kicad_pcb (version 20250000) (generator "pcbnew")
\t(footprint "L" (layer "F.Cu") (at 14.71 17.948 90) (property "Reference" "L1")
\t\t(pad "1" smd rect (at 3.13 0 90) (size 2.424 3.62) (layers "F.Cu") (net "SW"))
\t)
\t(footprint "C" (layer "F.Cu") (at 16.46 12.336 180) (property "Reference" "C_IN1")
\t\t(pad "2" smd rect (at 0.95 0 180) (size 1 1.45) (layers "F.Cu") (net "GND"))
\t)
)"""
    assert pad_clearance_failures(text, clearance=0.16) == []
    assert pad_clearance_failures(text, clearance=0.6) == ["L1 (SW) vs C_IN1 (GND) pad gap 0.545 mm < 0.6 mm"]


def test_a_track_whose_free_end_overhangs_what_it_joins_is_cut_back_not_removed():
    """Found on the same build: the VIN trunk (1.6,8.81)->(20.135,8.81) overhangs its outermost ribs (at x 2.45 and
    19.41) by the pads' half size, so its ends touch nothing; `prune_dangling` removed it whole, then the five ribs
    whose ends it held, and the route stage stopped on VIN left open. A track that joins two or more items is cut
    back to the outermost points it joins at, and still touches all of them; one that joins a single item is not cut
    (the caller removes it, as before)."""
    from types import SimpleNamespace

    from pcbc.route_emit import Piece
    from pcbc.route_geom import circle_shape, track_shape
    from pcbc.route_native import _trim_overhang

    def track(a, b, w=0.4):
        return SimpleNamespace(kind="track", layers=frozenset({"F.Cu"}), copper=track_shape(a, b, w), at=lambda: a)

    trunk = Piece(kind="seg", net="VIN", reason="spine", layer="F.Cu", a=(1.6, 8.81), b=(20.135, 8.81), w=0.4)
    it = SimpleNamespace(copper=track_shape(trunk.a, trunk.b, trunk.w))
    ribs = [track((2.45, 11.975), (2.45, 8.81)), track((17.41, 12.336), (17.41, 8.81)), track((19.41, 12.106), (19.41, 8.81))]
    via = SimpleNamespace(kind="via", layers=frozenset({"F.Cu", "B.Cu"}), copper=circle_shape(10.0, 8.81, 0.6), at=lambda: (10.0, 8.81))
    cut = _trim_overhang(trunk, it, [*ribs, via])
    assert (cut.a, cut.b, cut.w, cut.net, cut.reason) == ((2.45, 8.81), (19.41, 8.81), 0.4, "VIN", "spine"), cut
    assert _trim_overhang(trunk, it, ribs[:1]) is None, "one item joined: removed as before"
    far = track((30.0, 30.0), (31.0, 30.0))
    assert _trim_overhang(trunk, it, [ribs[0], far]) is None, "the far track does not touch the trunk"
    along = track((2.45, 8.81), (5.0, 8.81))
    assert _trim_overhang(trunk, it, [ribs[0], along]) is None, "the two it touches already touch each other: it joins nothing"


# --- decision 4: placement keeps a path's exit corridor clear -------------------------------------


def _placed(board: Path):
    from pcbc.place_native import place

    design = load_board(board)
    return design, place(design, name=board.stem, base=board.parent / "layout" / board.stem / "routed")


def _corridors(pl, ref):
    from pcbc.layout import content_rect
    from pcbc.pcb_place import _board_of, pad_corridors, path_ends, posed_feet_of

    feet = posed_feet_of(pl.job, pl.feet)
    content = content_rect(_board_of(pl.job))
    return feet, [c for num, path, lk in path_ends(pl.job)[ref] for c in pad_corridors(pl.job, feet[ref], num, path, lk, content, feet=feet)]


def test_the_exit_corridor_of_a_paths_end_pad_runs_to_where_the_path_can_turn(tmp_path: Path):
    """Buck's U1.2 (SW, where Path line 46 starts) is 0.532 mm across, under the path's 0.781 mm. Its down exit's
    corridor is the 0.532 mm neck + 2 x 0.2 mm for 1 mm past the pad's edge, then 0.781 + 2 x 0.2 mm on to 1.5905 mm
    past it: the neck's end, then half the path's width and its clearance, the room a turn takes (refuter round,
    2026-10-02, defect 4: the corridor ran on to the board's content edge, y 25). Its up exit stops at U1.5 [EN]'s
    clearance 1.028 mm past the edge, short of the 1.5905 mm, so it is dead (defect 11: it counted as open); U1.1 and
    U1.3 close the sides."""
    _design, pl = _placed(_buck(tmp_path))
    assert pl.error is None, pl.error
    _feet, cors = _corridors(pl, "U1")
    # U1.3 is VIN's Path's end (docs/physics-subjects.md §8) and has corridors of its own; these are U1.2's.
    cors = {c.side: c for c in cors if c.pad == "U1.2"}
    assert {s: (c.closed, c.dead) for s, c in cors.items()} == {
        "right": ("U1.3 [VIN]", True), "left": ("U1.1 [GND]", True), "down": ("", False), "up": ("", True)}
    down, up = cors["down"], cors["up"]
    assert down.rects == ((15.994, 10.496, 16.926, 11.496), (15.8695, 11.496, 17.0505, 12.0865)), down.rects
    assert (down.reach, down.length, down.end, down.layers) == (1.5905, 1.5905, "", frozenset({"F.Cu"}))
    assert (up.reach, up.length, up.end) == (1.5905, 1.028, "U1.5 [EN]"), up
    assert (down.source, down.net) == ("Path line 46", "SW")


def test_place_to_keeps_an_attached_parts_copper_out_of_the_targets_path_corridor(tmp_path: Path):
    """C_IN1 is attached to U1.VIN with Place(to=); before decision 4 it sat at (16.46, 11.676), its VIN pad across
    U1.2's down exit. It now sits where none of its pads enters U1.2's live corridors (to the right, at (17.91, 12.396)
    turned 270), and the declared Place("U1") is not moved."""
    from pcbc.pcb_place import _pad_rect, _rects_overlap

    _design, pl = _placed(_buck(tmp_path))
    feet, cors = _corridors(pl, "U1")
    assert (feet["C_IN1"].at, feet["C_IN1"].rot) == ((17.91, 12.396), 270.0)
    assert feet["U1"].at == (16.46, 8.81)
    for ref in ("C_IN1", "C_IN2", "C_BOOT", "R_EN", "R_FB_TOP", "R_FB_BOT"):
        f = feet[ref]
        assert not any(_rects_overlap(_pad_rect(f, q), r) for q in f.pads for c in cors if not c.dead for r in c.rects), ref


_RB = ('Resistor("R3",', 'Resistor("RB1", "0R", package="0402", mpn="X", lcsc="C1", p1=G, p2=G)\nResistor("RB2", "0R", package="0402", mpn="X", lcsc="C1", p1=G, p2=G)\n'
       'Resistor("RB3", "0R", package="0402", mpn="X", lcsc="C1", p1=G, p2=G)\nResistor("R3",')


def _blocked(left="(5.5, 10)", up="(7.5, 7.5)", down="(7.5, 12.5)"):
    """The neck fixture with three declared 0402 GND resistors, by default one in each live exit of R1.1."""
    place = f'Place("R1", at=(8, 10))\nPlace("RB1", at={left})\nPlace("RB2", at={up})\nPlace("RB3", at={down})\n'
    return [('Place("R1", at=(8, 10))\n', place), _RB]


_SHAPE_LEFT = "inside its 0.64 mm neck + 2 x 0.2 mm for 1 mm, then 2.276 mm + 2 x 0.2 mm to 2.338 mm past R1.1's edge"
_SHAPE_UPDOWN = "inside its 0.54 mm neck + 2 x 0.2 mm for 1 mm, then 2.276 mm + 2 x 0.2 mm to 2.338 mm past R1.1's edge"


def test_a_declared_placement_that_blocks_every_exit_is_an_error_before_routing(tmp_path: Path):
    """R1.1 (the 3 A path's start) has three live exits, each running 2.338 mm past its edge (the 1 mm neck, then
    1.138 + 0.2 mm). Three declared resistors stand in all three, 0.94-1.96 mm past the edge. Declared parts are never
    moved, so placement stops with the facts: each exit, the pads in it, their Place lines and distances, and the
    side R1's own pad closes. (The same board with the error switched off does not route: PWR "did not route at
    2.276 mm", RB1.2 and R1.2 in the way.)"""
    design, pl = _placed(_neck_board(tmp_path, _blocked()))
    assert pl.error == (
        "a path's exit is blocked by declared placement: PWR: every exit of R1.1, where Path line 16 starts, is closed: "
        "right: no stub leaves past R1.2 [GND], R1's own pad; "
        f"left: RB1.1 [GND] (layout.core.py:3) 1.96 mm past R1.1's edge, RB1.2 [GND] (layout.core.py:3) 0.94 mm past R1.1's edge, {_SHAPE_LEFT}; "
        f"down: RB3.1 [GND] (layout.core.py:5) 1.86 mm past R1.1's edge, RB3.2 [GND] (layout.core.py:5) 1.86 mm past R1.1's edge, {_SHAPE_UPDOWN}; "
        f"up: RB2.1 [GND] (layout.core.py:4) 1.86 mm past R1.1's edge, RB2.2 [GND] (layout.core.py:4) 1.86 mm past R1.1's edge, {_SHAPE_UPDOWN}"
    ), pl.error
    # One exit left open (RB3 moved off the down corridor): no error.
    design, pl = _placed(_neck_board(tmp_path / "open", _blocked(down="(12, 14)")))
    assert pl.error is None, pl.error


def test_declared_copper_past_the_corridor_does_not_close_an_exit(tmp_path: Path):
    """Refuter round, 2026-10-02 (defect 4): the corridor ran to the board edge, so three resistors 5.7-7.7 mm from
    R1.1 stopped the build, and the board routes (ok, DRC 0) with the error off. They lie past the 2.338 mm corridor
    now and nothing is reported."""
    _design, pl = _placed(_neck_board(tmp_path, _blocked(left="(1.5, 10)", up="(7.5, 2)", down="(7.5, 18.5)")))
    assert pl.error is None, pl.error


def test_copper_on_the_other_side_of_the_board_does_not_close_an_exit(tmp_path: Path):
    """Refuter round, 2026-10-02 (defect 5): the corridor carried no layer, so three declared bottom-side resistors under
    R1.1's three exits stopped the build, and the board routes with the error off. R1.1 is a surface pad on F.Cu: its
    corridors are on F.Cu, and B.Cu pads are not in them. With one of the three on the top side, the other two still
    leave their exits open."""
    b = _blocked(left='(5.5, 10), layer="B.Cu"', up='(7.5, 7.5), layer="B.Cu"', down='(7.5, 12.5), layer="B.Cu"')
    _design, pl = _placed(_neck_board(tmp_path, b))
    assert pl.error is None, pl.error
    mixed = _blocked(left='(5.5, 10), layer="B.Cu"', down='(7.5, 12.5), layer="B.Cu"')
    _design, pl = _placed(_neck_board(tmp_path / "mixed", mixed))
    assert pl.error is None, pl.error


def test_buck_with_c_in1_declared_across_u1_2s_one_live_exit_is_an_error_before_routing(tmp_path: Path):
    """Refuter round, 2026-10-02 (defect 11): C_IN1 declared at left=16.0, top=11.2 puts C_IN1.1 [VIN] in U1.2's down
    exit, and U1.2's up exit ends at U1.5 1.028 mm past the edge; that dead exit counted as open, so the error never
    fired and routing failed instead. Now the build stops at placement with all four sides."""
    _design, pl = _placed(_buck(tmp_path, [('Place("C_IN1", to="U1.VIN")', 'Place("C_IN1", position="absolute", left=16.0, top=11.2)')]))
    assert pl.error == (
        "a path's exit is blocked by declared placement: SW: every exit of U1.2, where Path line 46 starts, is closed: "
        "right: no stub leaves past U1.3 [VIN], U1's own pad; left: no stub leaves past U1.1 [GND], U1's own pad; "
        "down: C_IN1.1 [VIN] (layout.core.py:8) 0.959 mm past U1.2's edge, inside its 0.532 mm neck + 2 x 0.2 mm for 1 mm, then "
        "0.781 mm + 2 x 0.2 mm to 1.5905 mm past U1.2's edge; "
        "up: it stops at U1.5 [EN] 1.028 mm past U1.2's edge, short of the 1.5905 mm the corridor needs"
    ), pl.error


def test_a_corridor_ends_at_a_station_pad_of_its_own_path(tmp_path: Path):
    """Refuter round, 2026-10-02 (defect 2): the corridor ran on past the path's own far station. Here the path runs
    R1.1 -> R3.1 and R3 is declared turned 180 at (5.4, 10), R3.1 1.04 mm past R1.1's left edge and R3.2 [GND] behind
    it, inside the 2.338 mm the corridor would otherwise run. The left corridor ends at R3.1, so R3.2 does not close
    it, and with RB2 and RB3 in the up and down exits there is still no error."""
    edits = _blocked() + [('Path("PWR", "R1.1", "R2.1", amps=3)', 'Path("PWR", "R1.1", "R3.1", amps=3)'), ('Place("R3", at=(15, 16))', 'Place("R3", at=(5.4, 10), rot=180)')]
    edits[0] = (edits[0][0], edits[0][1].replace('Place("RB1", at=(5.5, 10))\n', ""))
    _design, pl = _placed(_neck_board(tmp_path, edits))
    assert pl.error is None, pl.error
    _feet, cors = _corridors(pl, "R1")
    left = next(c for c in cors if c.side == "left")
    assert (left.length, left.end, left.dead) == (1.04, "R3.1, a station of this path", False), left


_RX = ('Resistor("R3",', 'Resistor("RX", "0R", package="0402", mpn="X", lcsc="C1", p1=G, p2=G)\nResistor("R3",')
_RX_UP = ('Place("R3", at=(15, 16))', 'Place("R3", at=(15, 16))\nPlace("RX", to="R1.2", toward="up")')


@pytest.mark.parametrize("reverse", [False, True])
def test_place_to_keeps_clear_the_corridor_of_a_pad_a_path_starts_or_ends_on(tmp_path: Path, reverse):
    """RX is attached above R1.2 with Place(to=); R1.1, beside it, is where the 3 A path starts (and, reversed, where it
    ends: decision 4 says "starts or ends on", and the refuter's mutant that kept only start pads put RX at (8.0, 8.86),
    in R1.1's up exit). RX sits at (9.26, 8.4) turned 90 either way, out of every live corridor of R1.1 (a mutant that
    kept only the first corridor of each pad, R1.1's closed right side, put it in the up exit too)."""
    from pcbc.pcb_place import _pad_rect, _rects_overlap

    edits = [_RX, _RX_UP] + ([('Path("PWR", "R1.1", "R2.1", amps=3)', 'Path("PWR", "R2.1", "R1.1", amps=3)')] if reverse else [])
    _design, pl = _placed(_neck_board(tmp_path, edits))
    assert pl.error is None, pl.error
    feet, cors = _corridors(pl, "R1")
    assert [c.side for c in cors if not c.dead] == ["left", "down", "up"]
    f = feet["RX"]
    assert (f.at, f.rot) == ((9.26, 8.4), 90.0), (f.at, f.rot)
    assert not any(_rects_overlap(_pad_rect(f, q), r) for q in f.pads for c in cors for r in c.rects)


def test_a_corridor_is_kept_only_on_its_own_layers():
    """Defect 5's other half: `corridor_hits` (the Place(to=) side) counts a pad only on a layer the corridor is on. A
    surface pad on F.Cu over a B.Cu corridor is not in it; the same pad over an F.Cu corridor is; a drilled pad is on
    both."""
    from pcbc.pcb_place import Corridor, Foot, Pad, corridor_hits

    rect = ((0.0, 0.0, 2.0, 2.0),)
    base = dict(pad="R1.1", side="up", rects=rect, source="Path line 1", net="PWR", width=1.0, neck=None, clearance=0.2, stations=frozenset({"R1.1", "R2.1"}))
    on_b = Corridor(layers=frozenset({"B.Cu"}), **base)
    on_f = Corridor(layers=frozenset({"F.Cu"}), **base)
    smd = Foot("RX", (-1, -1, 1, 1), [Pad("1", 0.0, 0.0, 0.5, 0.5, "GND")], at=(1.0, 1.0), layer="F.Cu")
    tht = Foot("JX", (-1, -1, 1, 1), [Pad("1", 0.0, 0.0, 0.5, 0.5, "GND", through=True)], at=(1.0, 1.0), layer="F.Cu")
    assert corridor_hits(smd, (1.0, 1.0), 0.0, [on_b]) == []
    assert corridor_hits(smd, (1.0, 1.0), 0.0, [on_f]) == ["R1.1's up exit corridor (PWR, Path line 1)"]
    assert corridor_hits(tht, (1.0, 1.0), 0.0, [on_b]) == ["R1.1's up exit corridor (PWR, Path line 1)"]
    assert corridor_hits(smd, (1.0, 1.0), 0.0, [Corridor(layers=frozenset({"F.Cu"}), dead=True, **base)]) == [], "a dead exit is not kept"


def test_a_station_pad_of_the_path_may_sit_in_its_corridor(tmp_path: Path):
    """A part attached with Place(to=) whose pad is a station of the same path is the path's own copper, never an
    intrusion: with the path R1.1 -> R3.1 -> R2.1 (R4 a PWR load off it, so the path is not the whole net), R3 attached
    above R1.2 sits at (8.75, 8.86) with R3.1 [PWR] in R1.1's up exit, whose corridor then ends at it; when R3.1 is not
    on the path, R3 is kept out, at (9.26, 8.4) turned 270."""
    from pcbc.pcb_place import posed_feet_of

    load = [('Resistor("R3",', 'Resistor("R4", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=G)\nResistor("R3",'), ('SchRegion(', 'Place("R4", at=(20, 16))\nSchRegion(')]
    up = ('Place("R3", at=(15, 16))', 'Place("R3", to="R1.2", toward="up")')
    _design, pl = _placed(_neck_board(tmp_path, load + [up, ('Path("PWR", "R1.1", "R2.1", amps=3)', 'Path("PWR", "R1.1", "R3.1", "R2.1", amps=3)')]))
    assert pl.error is None and pl.job.constraints.refusals == (), (pl.error, pl.job.constraints.refusals)
    f = posed_feet_of(pl.job, pl.feet)["R3"]
    assert (f.at, f.rot) == ((8.75, 8.86), 0.0), (f.at, f.rot)
    _feet, cors = _corridors(pl, "R1")
    up_exit = next(c for c in cors if c.side == "up")
    assert (up_exit.end, up_exit.length) == ("R3.1, a station of this path", 0.5), up_exit
    _design, pl = _placed(_neck_board(tmp_path / "off", load + [up]))
    f = posed_feet_of(pl.job, pl.feet)["R3"]
    assert (f.at, f.rot) == ((9.26, 8.4), 270.0), (f.at, f.rot)


def test_a_place_to_part_in_an_exit_does_not_count_toward_the_error(tmp_path: Path):
    """Only a declared CSS/absolute Place() is never moved; a Place(to=) part is not a declared blocker. RB2 attached to
    RB1.1 lands at (5.5, 8.86) with RB2.2 inside R1.1's up exit; RB1 and RB3 hold the other two; no error."""
    from pcbc.pcb_place import _pad_rect, _rects_overlap

    edits = _blocked()
    edits[0] = (edits[0][0], edits[0][1].replace('Place("RB2", at=(7.5, 7.5))', 'Place("RB2", to="RB1.1", toward="up")'))
    _design, pl = _placed(_neck_board(tmp_path, edits))
    assert pl.error is None, pl.error
    feet, cors = _corridors(pl, "R1")
    up = next(c for c in cors if c.side == "up")
    f = feet["RB2"]
    assert f.at == (5.5, 8.86) and any(_rects_overlap(_pad_rect(f, q), r) for q in f.pads for r in up.rects)


def test_every_path_end_of_a_part_is_checked(tmp_path: Path):
    """R1 carries two path ends: R1.2 (a 1 A path on AAA, first in net order, its exits open) and R1.1 (PWR). R1.1's
    exits are all held by declared parts; the error names it though R1.2's are open. (A GND path cannot stand in for
    AAA here: amps= on GND is refused with no power NetReq, and with one GND is poured.)"""
    edits = _blocked() + [
        ('G = Ground("GND")', 'G = Ground("GND")\nA = Power("AAA")'),
        ('Resistor("R1", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=G)', 'Resistor("R1", "10k", package="0402", mpn="X", lcsc="C1", p1=P, p2=A)'),
        ('Resistor("R2", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=G)', 'Resistor("R2", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=A)'),
        ('Path("PWR", "R1.1", "R2.1", amps=3)', 'Path("PWR", "R1.1", "R2.1", amps=3)\nNetReq("AAA", kind="power", volts=5)\nPath("AAA", "R1.2", "R2.2", amps=1)'),
        ('Place("R3", at=(15, 16))', 'Place("R3", at=(15, 16))\nPlace("R4", at=(20, 16))'),
    ]
    edits[1] = ('Resistor("R3",', edits[1][1].replace('Resistor("R3",', 'Resistor("R4", "10k", package="0402", mpn="X", lcsc="C1", p1=A, p2=G)\nResistor("R3",'))
    _design, pl = _placed(_neck_board(tmp_path, edits))
    from pcbc.pcb_place import path_ends

    assert [(n, p.net) for n, p, _lk in path_ends(pl.job)["R1"]] == [("2", "AAA"), ("1", "PWR")]
    assert pl.error is not None and pl.error.startswith("a path's exit is blocked by declared placement: PWR: every exit of R1.1, where Path line 18 starts"), pl.error


def test_an_end_pad_on_a_part_placed_by_to_is_checked(tmp_path: Path):
    """R2 (where the path ends) is attached to RY.1 with Place(to=) and lands at (18.59, 14); three declared resistors
    stand in R2.1's three live exits (its right exit stops at R2.2 1.6 mm past the edge). R2 is placed by relation, but
    the blockers are declared, so the error stands."""
    edits = [
        ('Resistor("R3",', 'Resistor("RY", "0R", package="0402", mpn="X", lcsc="C1", p1=G, p2=G)\nResistor("R3",'),
        _RB,
        ('Place("R2", at=(22, 10))', 'Place("RY", at=(22, 14))\nPlace("RB1", at=(15.0, 14))\nPlace("RB2", at=(17.1, 11.5))\nPlace("RB3", at=(17.1, 16.5))\nPlace("R2", to="RY.1")'),
    ]
    _design, pl = _placed(_neck_board(tmp_path, edits))
    assert pl.error is not None and pl.error.startswith(
        "a path's exit is blocked by declared placement: PWR: every exit of R2.1, where Path line 17 ends, is closed: "
        "right: it stops at R2.2 [GND] 1.6 mm past R2.1's edge, short of the 2.413 mm the corridor needs; "
        "left: RB1.1 [GND] (layout.core.py:4) 1.805 mm past R2.1's edge"), pl.error


def test_blockers_inside_only_the_neck_close_an_exit(tmp_path: Path):
    """Three declared resistors 0.35-0.36 mm past R1.1's edge, each wholly inside the first millimetre of an exit (the
    neck's band) and clear of the full-width band past it: every exit is closed."""
    _design, pl = _placed(_neck_board(tmp_path, _blocked(left="(6.55, 10), rot=90", up="(7.5, 9.0)", down="(7.5, 11.0)")))
    assert pl.error is not None and "left: RB1.1 [GND] (layout.core.py:3) 0.35 mm past R1.1's edge" in pl.error, pl.error
    assert "up: RB2.1 [GND] (layout.core.py:4) 0.36 mm past R1.1's edge" in pl.error and "down: RB3.1 [GND] (layout.core.py:5) 0.36 mm" in pl.error


def test_the_corridor_of_the_pad_a_part_attaches_to_is_not_its_to_keep(tmp_path: Path):
    """Decision 4 reads "another pad of that part": a part attached to the path's own end pad (R3 to R1.1) is placed
    as before, here at (6.4, 9.49) turned 90, across R1.1's left exit; only the target's other path pads' corridors
    are kept clear."""
    from pcbc.pcb_place import posed_feet_of

    _design, pl = _placed(_neck_board(tmp_path, [('Place("R3", at=(15, 16))', 'Place("R3", to="R1.1")')]))
    assert pl.error is None, pl.error
    f = posed_feet_of(pl.job, pl.feet)["R3"]
    assert (f.at, f.rot) == ((6.4, 9.49), 90.0)


@pytest.mark.parametrize(
    "to, error",
    [
        ("U9.VIN", "layout.core.py:9: the to= of Place('C_IN2') names U9.VIN, and there is no part U9 on the board"),
        ("U1.NOPE", "layout.core.py:9: the to= of Place('C_IN2') names U1.NOPE, and U1 has no pin NOPE (its pins: GND, SW, VIN, FB, EN, BOOT)"),
    ],
)
def test_a_place_to_naming_a_part_or_pin_the_board_lacks_fails_the_check_with_its_line(tmp_path: Path, to, error):
    """Refuter round, 2026-10-02 (defects 7 and 8): `build_job`'s check stage never ran `pcb_place.validate`, so a
    Place(to=) naming no part surfaced as `check: J_IN (GND) vs C_IN2 (VIN) pad gap 0.000 mm`, and one naming no pin
    as a raw `no pad on net '' on both parts`. The check stage now names the line, the part and the pin."""
    from pcbc.build import build_job

    r = build_job(_buck(tmp_path, [('Place("C_IN2", to="U1.VIN")', f'Place("C_IN2", to="{to}")')]), force=True)
    assert r["error"] == error, r["error"]
    assert r["steps"][-1]["stage"] == "check" and r["steps"][-1]["fails"] == [error]


_PU = 'Place("U1", position="absolute", left=15, top=8, locked=True, reason="buck IC in the middle")'


@pytest.mark.parametrize(
    "old, new, error",
    [
        ('Place("C_IN2", to="U1.VIN")', 'Place("C_IN2", to="U1.VIN")\nPlace("C_IN2", to="U1.GND")',
         "C_IN2: Place() 2 times (layout.core.py lines 9, 10); a part takes one Place(), and pcbc does not pick between them"),
        (_PU, _PU[:-1] + ", z_index=1)", "layout.core.py:3: Place('U1'): z-index is not a copper layer; a part's copper side is read from layer= ('F.Cu' or 'B.Cu')."),
        ('Place("J_IN", edge="left", reason="input connector")', 'Place("J_IN", edge="left", reason="input connector", flex=1)',
         "layout.core.py:6: Place('J_IN'): flex is not a placer; pcbc places a part by its Place() position or relation, and packs a Region's unlocked parts itself."),
        (_PU, _PU.replace('"absolute"', '"sticky"'), "layout.core.py:3: Place('U1'): position:sticky needs a scrollport, which a board has not; position is 'static' or 'absolute'."),
        (_PU, _PU[:-1] + ", padding=1)",
         "layout.core.py:3: Place('U1'): padding belongs on Region, not a footprint (a footprint's size is its courtyard); a part's inset is read from margin."),
        (_PU, _PU + '\nfrom pcbc import Region\nRegion("rx", position="absolute", left=2)',
         "Region('rx'): cannot determine width: it has no width, not both left and right, and no footprint's intrinsic courtyard to size it."),
    ],
)
def test_a_refused_line_states_facts_and_proposes_no_edit(tmp_path: Path, old, new, error):
    """§7b, refuter round 2026-10-02 (defect 9): these refusals told the author what to write ("Use side='F' or
    side='B'", "Put unlocked parts in a Region", "Use position='absolute'", "Use margin.", "Set width/height", "keep
    one"), and the Region one escaped `build_job` as a raw ValueError. Each states what was written and what pcbc reads,
    with its board.py line where the loader has one, and comes back as the check stage's error."""
    import re

    from pcbc.build import build_job

    r = build_job(_buck(tmp_path, [(old, new)]), force=True)
    assert r["error"] == error, r["error"]
    assert not re.search(r"(^|[.;:]\s+)(Use|Put|Set|Pass|Keep|keep one|Try|Add|Move|Remove|Lower|Raise)\b", r["error"]), r["error"]


def test_fab_puts_no_fiducial_on_copper_and_says_which_spots_it_refused():
    """Refuter round, 2026-10-02 (defect 10): with fewer than three free corner spots, `insert_fiducials` put all three
    at fixed corners anyway, on a 16 x 7 mm board's routed PWR track, and fab failed on its own fiducials' shorts. A
    spot whose 2 mm mask opening takes in copper is refused, with the fact; free spots are still taken."""
    from pcbc.fab import insert_fiducials

    text = """(kicad_pcb (version 20250000) (generator "pcbnew")
\t(segment (start 2 4) (end 14 4) (width 1) (layer "F.Cu") (net "PWR") (uuid "a1"))
)"""
    refused: list[str] = []
    out, spots = insert_fiducials(text, (0, 0, 16, 7), refused=refused)
    assert spots == [] and out == text
    assert refused == [f"({x},{y}): its 2 mm mask opening takes in the PWR track (2,4)->(14,4) on F.Cu" for x, y in ((12, 4), (12, 3), (4, 3), (4, 4))]
    refused = []
    _out, spots = insert_fiducials(text, (0, 0, 16, 30), refused=refused)
    assert spots == [("FID1", 12.0, 26.0), ("FID2", 4.0, 26.0)], spots
    assert refused == ["(12,4): its 2 mm mask opening takes in the PWR track (2,4)->(14,4) on F.Cu", "(4,4): its 2 mm mask opening takes in the PWR track (2,4)->(14,4) on F.Cu"]


def test_a_drc_copper_error_names_every_item_and_where_it_is():
    """Refuter round, 2026-10-02 (defect 10): fab failed with a bare `kicad-cli DRC 26 copper error(s)`. The error lists
    the violations by type, each item with its position, and says which items are fiducials fab itself inserted."""
    from pcbc.fab import drc_error_lines

    short = {"type": "shorting_items", "severity": "error", "description": "Items shorting two nets (nets PWR and )", "items": [
        {"description": "Track [PWR] on F.Cu, length 2.3702 mm", "pos": {"x": 9.8615, "y": 1.824}},
        {"description": "Pad [<no net>] of FID1 on F.Cu", "pos": {"x": 12.0, "y": 4.0}}]}
    gap = {"type": "clearance", "severity": "error", "description": "Clearance violation", "items": [
        {"description": "Pad 1 [PWR] of R1 on F.Cu", "pos": {"x": 4.51, "y": 3.5}}, {"description": "Pad 2 [GND] of R3 on F.Cu", "pos": {"x": 6.4, "y": 3.5}}]}
    assert drc_error_lines([short, gap, short], ["FID1", "FID2", "FID3"]) == (
        "shorting_items x2: Track [PWR] on F.Cu, length 2.3702 mm at (9.8615,1.824) with Pad [<no net>] of FID1 on F.Cu at (12,4) "
        "(FID1: a fiducial fab inserted); Track [PWR] on F.Cu, length 2.3702 mm at (9.8615,1.824) with Pad [<no net>] of FID1 on F.Cu "
        "at (12,4) (FID1: a fiducial fab inserted) | clearance x1: Pad 1 [PWR] of R1 on F.Cu at (4.51,3.5) with Pad 2 [GND] of R3 on F.Cu at (6.4,3.5)"
    )
    assert "a fiducial fab inserted" not in drc_error_lines([short], [])


def test_a_branch_whose_legs_other_items_refused_also_names_its_own_path_copper(tmp_path: Path):
    """Decision 2 lets a switch node's off-path branch try B.Cu, and its vias are then refused by the pads around
    its own pad, so the search records hits; the lattice is walled by the net's own path copper, which no leg is
    refused by. The error names that copper with its link and the junction rule beside the pads (it named the pads
    alone). The refuters' buck as they placed it (`tests/fixtures/r2_b1`), SW routed first (a sensitive kind; `autoroute=` is refused since physics-subjects PR 7)."""
    from pcbc.compile import compile_design
    from pcbc.route_native import move_lines

    work = tmp_path / "b1"
    shutil.copytree(ROOT / "tests" / "fixtures" / "r2_b1", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "r2_b1.py"
    edit_board(board, [
        ('\nPlace("C1", to="U1.VIN")', '\nPlace("C1", at=(15.46, 12.676), rot=180)'),
        ('\nPlace("L1", to="U1.SW")', '\nPlace("L1", at=(21.84, 14.996), rot=180)'),
        ('\nPlace("C3", to="L1.2")', '\nPlace("C3", at=(27.362, 14.046), rot=90)'),
        ('\nPlace("C4", to="L1.2")', '\nPlace("C4", at=(28.082, 16.996), rot=0)'),
    ])
    design, rt = _route(board)
    # docs/physics-subjects.md PR 3 (measured 2026-10-02): with VIN and GND at the 0.16 mm class floor (no net
    # current) and no spine, the SW branch C5.2 -> U1.2 routes here; the board no longer reproduces the wall.
    assert "SW" not in rt.unrouted, rt.unrouted
    assert not any(x.startswith("SW: C5.2 ") for x in move_lines(design, compile_design(design), rt))


P2_SIBLING = """from pcbc import *
Board(width=30, height=24, layers=2, stackup="jlcpcb_2l_1oz")
V = Power("3V3"); GND = Ground("GND"); T = Net("T"); A = Net("A")
OPA = load("components/TI/LMV321IDBVR")
OPA("U1", **{"+": A, "-": T, "OUT": T, "V+": V, "GND": GND})
Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=T, p2=GND)
Resistor("R4", "10k", package="0805", mpn="X", lcsc="C1", p1=T, p2=GND)
Resistor("R2", "10k", package="0603", mpn="X", lcsc="C1", p1=A, p2=GND)
NetReq("3V3", kind="power", volts=3.3)
NetReq("T", kind="power", volts=3.3)
Path("T", "U1.OUT", "R1.1", amps=1.5)
Place("U1", at=(12, 12), rot=90)
Place("R1", at=(25, 20))
Place("R4", to="U1.-", toward="left")
Place("R2", to="U1.+")
"""


def _hits(pl, ref: str, part: str) -> list[str]:
    from pcbc.pcb_place import _pad_layers, _pad_rect, _rects_overlap

    feet, cors = _corridors(pl, ref)
    f = feet[part]
    return [f"{c.pad} {c.side} {part}.{q.num}" for c in cors if not c.dead for q in f.pads
            if f"{part}.{q.num}" not in c.stations and (_pad_layers(f, q) & c.layers) and any(_rects_overlap(_pad_rect(f, q), r) for r in c.rects)]


def test_place_to_a_pin_with_a_same_net_sibling_keeps_the_siblings_corridor(tmp_path: Path):
    """Postfix B11 (refutation placement r2, p2): U1.- (pin 3) and U1.OUT (pin 4) are both on T, and U1.OUT ends a 1.5 A
    Path. `Place("R4", to="U1.-")` searched from every same-net pad of U1 and dropped the corridors of whichever it
    anchored on, so anchored on pin 4 it put R4.1 in U1.4's left exit at (9.4, 12.3875). The exempt pads are the ones
    the to= names (pin 3), whatever the anchor."""
    import shutil

    work = tmp_path / "p2"
    work.mkdir()
    shutil.copytree(EXAMPLES / "node" / "components", work / "components")
    write_split(work / "p2.py", P2_SIBLING)
    _design, pl = _placed(work / "p2.py")
    assert pl.error is None, pl.error
    assert _hits(pl, "U1", "R4") == [], _hits(pl, "U1", "R4")


S2_STUCK = """from pcbc import *
Board(width=30, height=24, layers=2, stackup="jlcpcb_2l_1oz")
P = Power("PWR"); G = Ground("GND")
Resistor("RT", "0R01", package="0805", mpn="X", lcsc="C1", p1=P, p2=G)
Resistor("R1", "0R", package="1206", mpn="X", lcsc="C1", p1=P, p2=G)
Capacitor("C1", "1uF", package="0805", mpn="X", lcsc="C1", p1=G, p2=Net("X1"))
NetReq("PWR", kind="power", volts=5)
Path("PWR", "RT.1", "R1.1", amps=3)
Keepout("KL", box=(0, 0, 10.6, 11.0))
Keepout("KR", box=(15.0, 0, 30, 11.0))
Keepout("KT", box=(0, 0, 30, 7.6))
Place("RT", at=(12, 12), rot=180)
Place("R1", at=(25, 20))
Place("C1", to="RT.2", toward="up")
"""


def test_a_part_left_in_a_corridor_with_no_clear_spot_is_said_to_be_there(tmp_path: Path):
    """Postfix B12 (refutation placement r2, s2): with no clear spot within reach the fallback pose was never tested
    against the corridors, so C1.1 sat in RT.1's up exit and the note named only keepouts. The note names the
    corridor and the pose."""
    import json

    work = tmp_path / "s2"
    work.mkdir()
    write_split(work / "s2.py", S2_STUCK)
    _design, pl = _placed(work / "s2.py")
    assert _hits(pl, "RT", "C1"), "the fixture still leaves C1 in RT.1's corridor"
    notes = [x for x in json.dumps(pl.report, default=str).split('"') if x.startswith("C1: no clear spot")]
    assert notes and "RT.1's up exit corridor (PWR, Path line 8)" in notes[0] and "it stands at (" in notes[0], notes


def test_a_link_the_neck_limit_stops_says_so_with_the_room_each_exit_has(tmp_path: Path):
    """Postfix B13 (refutation neck r2): a 3 A Path (2.276 mm) from QFN-16 pin 10 (0.25 mm across, 0.5 mm pitch) cannot
    leave within the 1 mm neck limit: full width plus clearance first has room 1.3 mm past the pad's edge (the refuter's
    geometry: 1.284 mm). The error named only pads and never the neck; it now states the limit and each exit's room."""
    shutil.copytree(ROOT / "tests" / "fixtures" / "d14" / "qfn16", tmp_path / "qfn16")
    _design, rt = _route(tmp_path / "qfn16" / "qfn16.py")
    assert rt.unrouted == ["PWR"], rt.unrouted
    (why,) = [f.why for f in rt.failed if f.net == "PWR"]
    assert (
        "U1.10 is narrower than the link's 2.276 mm on F.Cu on every exit, and a pad neck may run at most 1 mm past the pad's "
        "edge before full width (direction.md §5b); full width plus 0.2 mm clearance has room: right 0.25 mm across, room first "
        "1.3 mm past its edge"
    ) in why, why
