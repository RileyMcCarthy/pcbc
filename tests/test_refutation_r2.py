"""Refutation round 2 of net-vs-path v1, and the owner's rule that came with it (2026-10-01).

Owner: *"Remove suggestions in tool, just throw errors if unable to complete."* (docs/direction.md §7b).
Every test here pins a fact a defect got wrong, and each fails under the mutation that disables the fix
(the mutation run is recorded in docs/net-path-physics.md, "Refutation round 2"). The fixture
`tests/fixtures/r2/fxa.py` is the physics refuters' board: a 2 A path R1 -> R2 -> R3 on PWR, a feedback
tap R3 -> R5, an off-path load R4, an F.Cu wall W at x = 20 (`layout.core.py`), and a GND that no power
NetReq names, so the two-layer board has no back pour.
"""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

import pytest

from conftest import copy_board, edit_board, put_core, write_split
from pcbc.compile import compile_design
from pcbc.constraints import compile_constraints
from pcbc.language import load_board
from pcbc.route_emit import Sidecar
from pcbc.route_geom import contact_outside, track_shape, via_shape
from pcbc.route_verify import path_copper_failures

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FX = ROOT / "tests" / "fixtures" / "r2"
SRC = ROOT / "src" / "pcbc"


def _fx(tmp_path: Path, edits=(), *, core: bool = True, name: str = "fxa") -> Path:
    work = tmp_path / name
    work.mkdir()
    board = copy_board(FX / "fxa.py", work, edits)
    if not core:
        # The fixture's core minus its copper line(s): its placement and outline stay (docs/direction.md §2).
        core_file = work / "layout.core.py"
        core_file.write_text("".join(ln for ln in core_file.read_text().splitlines(keepends=True) if not ln.startswith(("Seg(", "Via(", "Arc(", "Pour("))))
    return board


# --- the owner's rule: no message proposes an edit ---------------------------------------------------

# Each of these is an edit offered to the author, in the shapes the tool used to print them. A string
# literal of the tool (docstrings and comments are not messages) matching one fails the test.
PROPOSALS = re.compile(
    r"\bwrite:|[;:] write\b|\bMoves?:|did you mean|\btry (vias|layers|amps|a |the )|\bdrop (line|it|the|at=|kind|amps|vias|solder|z_se)\b"
    r"|\bgive (it|the link|them|one of)\b|\bor drop\b|\bor raise\b|\bor lower\b|\braise (amps|max_mm|keep)|\blower (keep|amps)"
    r"|\bmove (it|one|the|that|this)\b|\bmove [A-Z][A-Z_]*\d|\brename (the|it)\b|\bleave (it|the \w+) out\b|\bremove it\b"
    r"|\bdelete (it|the line)\b|\bunlock (it|the)\b|is the edit\b|\bto fix by\b|\bname it with a Path\b"
    r"|[;:] (Sch)?Place\(\{[^}]*\}, (to|edge|at|toward)=|\bPass pin=|\bbring them together\b|\bleave (it|the line|the \w+) out\b"
    # Refuter round, 2026-10-02 (defect 9): sentence-start imperatives the patterns above missed.
    r"|(^|[.;:]\s+)(Use|Put|Set|Pass|Keep) \w|\bkeep one\b",
)


def _messages(path: Path):
    tree = ast.parse(path.read_text())
    doc = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            doc.add(id(n.value))
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for v in n.values:
                doc.add(id(v))
            yield n.lineno, "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc:
            yield n.lineno, n.value


def test_no_message_of_the_tool_proposes_an_edit():
    """docs/direction.md §7b: an error states what failed and why; it never proposes an edit. Scanned
    over every string literal of `src/pcbc` (a docstring or a comment is not a message)."""
    hits = []
    for f in sorted(SRC.rglob("*.py")):
        for line, text in _messages(f):
            m = PROPOSALS.search(text)
            if m:
                hits.append(f"{f.relative_to(SRC)}:{line}: [{m.group(0)}] {text[:120]!r}")
    assert hits == [], "\n".join(hits)


def test_move_line_has_no_edit_sentence():
    from pcbc.moves import move_line

    assert move_line("SW", "U1.2", "L1.1", "VIN track") == "SW: U1.2 cannot reach L1.1. In the way: VIN track."
    with pytest.raises(TypeError):
        move_line("SW", "U1.2", "L1.1", "VIN track", "Moves: Place(...)")  # type: ignore[call-arg]


# --- physics: the plane under a layer is the board's, not assumed ------------------------------------


def test_f_cu_is_sized_for_the_pour_the_board_has(tmp_path: Path):
    """Major (physics): a 2 A path's F.Cu width was sized over a B.Cu pour a board with no power GND
    does not have (the fab gate passed 1.999 A where the repo's own model gives 1.665 A). With no pour,
    F.Cu is sized with no plane, like B.Cu; naming GND in a power NetReq gives the pour back."""
    bare = compile_constraints(load_board(_fx(tmp_path, core=False)))
    lk = bare.link_by_key("R1.1~R2.1")
    assert dict((lay, d.value) for lay, d in lk.width) == {"F.Cu": 1.09, "B.Cu": 1.09}
    assert bare.planes == ()
    poured = compile_constraints(load_board(_fx(tmp_path, [('NetReq("PWR", kind="power", volts=5)', 'NetReq("PWR", kind="power", volts=5)\nNetReq("GND", kind="power")')], core=False, name="poured")))
    assert dict((lay, d.value) for lay, d in poured.link_by_key("R1.1~R2.1").width) == {"F.Cu": 0.781, "B.Cu": 1.09}
    assert poured.planes == (("GND", "B.Cu"),)


# --- physics: a multi-pad station shared by two paths -------------------------------------------------


def test_a_shared_multi_pad_station_is_joined_once_at_its_throughput(tmp_path: Path):
    """Major (physics + tests): J1.VBUS (two pads) is the first station of a 0.3 A and a 2 A path. Each
    path used to build its own join at the larger single current while labelling it with its own, so
    one join read 2 A wide and 0.3 A. The join is built once, at the 2.3 A the station's pads pass."""
    work = tmp_path / "node"
    shutil.copytree(EXAMPLES / "node", work, ignore=shutil.ignore_patterns("layout"))
    text = (work / "node.py").read_text()
    anchor = 'Path("VBUS", "J1.VBUS", "U2.VIN", amps=1)'  # node's own VBUS Path (docs/physics-subjects.md §8)
    assert anchor in text
    text = text.replace(anchor, 'Path("VBUS", "J1.VBUS", "U3.VBUS", amps=0.3)\nPath("VBUS", "J1.VBUS", "U2.VIN", amps=2)', 1)
    write_split(work / "node.py", text)
    cs = compile_constraints(load_board(work / "node.py"))
    assert cs.refusals == ()
    joins = [lk for lk in cs.links_of("VBUS") if lk.join]
    assert [(j.key, j.current.amps, j.amps_written) for j in joins] == [("J1.A4B9~J1.B4A9", 2.3, True)]
    two = cs.link("VBUS", "J1.A4B9", "U2.1")
    assert two is not None and two.current.amps == 2
    assert joins[0].width_on("F.Cu") > two.width_on("F.Cu"), "the join carries more than either path"
    assert joins[0].group is not None and joins[0].group.amps == 2.3


# --- physics: the path gates judge copper no role group owns -----------------------------------------


def _job(tmp_path: Path):
    design = load_board(_fx(tmp_path, core=False))
    return design, compile_design(design)


def _seg(owner: str, layer: str, a, b, w: float, reason: str = "route") -> dict:
    return {"key": ["seg", layer, list(a), list(b), w], "uuid": f"u-{owner}-{layer}-{a}-{b}", "reason": reason, "net": "PWR", "owner": owner, "w": w, "drill": 0.0}


def _via(owner: str, at, reason: str = "route") -> dict:
    return {"key": ["via", list(at), ["F.Cu", "B.Cu"]], "uuid": f"v-{owner}-{at}", "reason": reason, "net": "PWR", "owner": owner, "w": 0.8, "drill": 0.4}


def test_a_sense_link_on_a_layer_it_does_not_allow_is_named(tmp_path: Path):
    """Major (tests): a path's layers= was enforced by nothing a test exercised. The feedback tap is F.Cu
    only (its preset); copper of it on B.Cu, or a via on it, is named."""
    _design, job = _job(tmp_path)
    got = path_copper_failures(job, Sidecar(items=[_seg("R3.1~R5.1", "B.Cu", (30.0, 8.0), (30.0, 12.0), 0.2)]))
    assert got == ["PWR Path line 20's link R3.1~R5.1: copper on B.Cu, which the link does not allow (layers F.Cu, preset feedback)"], got
    got = path_copper_failures(job, Sidecar(items=[_via("R3.1~R5.1", (30.0, 10.0))]))
    assert got == ["PWR Path line 20's link R3.1~R5.1: a via at (30,10) on a link with no vias (preset feedback)"], got


def test_a_via_group_is_rated_by_the_drill_on_the_board(tmp_path: Path):
    """Postfix B1 (refutation stackup r2): the gate rated every barrel by the drill the compiler planned
    (`group.drill_mm`, 0.4 mm), so three 0.15 mm barrels carrying 2 A passed. Each barrel is rated by the drill its
    role item carries: at 0.4 mm three barrels at 1.016 mm pitch hold 2 A; at 0.15 mm the same three do not."""
    _design, job = _job(tmp_path)

    def group(drill: float) -> list[dict]:
        vias = [{**_via("R1.1~R2.1", (10.0 + 1.016 * k, 14.0), "route_group"), "drill": drill} for k in range(3)]
        land = [_seg("R1.1~R2.1", lay, (10.0, 14.0), (12.032, 14.0), 1.09, "route_group") for lay in ("F.Cu", "B.Cu")]
        return vias + land

    assert path_copper_failures(job, Sidecar(items=group(0.4))) == []
    (small,) = path_copper_failures(job, Sidecar(items=group(0.15)))
    assert small.startswith("PWR Path line 19's link R1.1~R2.1: the via group at (10,14) is 3 barrels of 0.15 mm drill at 1.016 mm pitch") and "over the 0.428 A one holds" in small, small
    (lone,) = path_copper_failures(job, Sidecar(items=[{**_via("R1.1~R2.1", (10.0, 14.0)), "drill": 0.2}]))
    assert "a lone via at (10,14) (0.2 mm drill) carries 2 A, over the 0.527 A one barrel holds" in lone, lone


def test_a_lone_via_the_compiler_planned_alone_is_rated_by_its_drill(tmp_path: Path):
    """Round 3 (refute-twooz fx06): fxa at amps=0.6. One 0.4 mm via holds 0.871 A, so the compiler plans no group, and
    the gate skipped every link without one (`if amps is None or group is None: continue`): the routed PWR via changed
    to a 0.15 mm drill (0.428 A) passed every gate, and only the census (not fatal) said so. A lone via is rated by
    the drill on the board, whatever the compiler planned."""
    design = load_board(_fx(tmp_path, [('Path("PWR", "R1.1", "R2.1", "R3.1", amps=2)', 'Path("PWR", "R1.1", "R2.1", "R3.1", amps=0.6)')], core=False))
    job = compile_design(design)
    lk = job.constraints.link_by_key("R1.1~R2.1")
    assert lk.group is None and lk.via.drill_mm == 0.4
    assert path_copper_failures(job, Sidecar(items=[_via("R1.1~R2.1", (31.288, 13.0))])) == []
    (small,) = path_copper_failures(job, Sidecar(items=[{**_via("R1.1~R2.1", (31.288, 13.0)), "drill": 0.15}]))
    assert small == (
        "PWR Path line 19's link R1.1~R2.1: a lone via at (31.288,13) (0.15 mm drill) carries 0.6 A, over the 0.428 A one barrel holds "
        "(jlcpcb_2l_1oz); the compiler planned one 0.4 mm drill via for this layer change"
    ), small


def test_a_path_links_via_groups_are_checked(tmp_path: Path):
    """Major (tests): the via-group checks on a path link, and the both-layers landing check, had no
    test. A lone router via on the 2 A link, a group of two where three are needed, and a group whose
    landing runs on one layer only are each named."""
    _design, job = _job(tmp_path)
    lone = path_copper_failures(job, Sidecar(items=[_via("R1.1~R2.1", (10.0, 14.0))]))
    assert lone == ["PWR Path line 19's link R1.1~R2.1: a lone via at (10,14) (0.4 mm drill) carries 2 A, over the 0.871 A one barrel holds; the layer change needs a 3-via group"], lone
    two = [_via("R1.1~R2.1", (10.0, 14.0), "route_group"), _via("R1.1~R2.1", (11.016, 14.0), "route_group")]
    land = [_seg("R1.1~R2.1", lay, (10.0, 14.0), (11.016, 14.0), 1.09, "route_group") for lay in ("F.Cu", "B.Cu")]
    short = path_copper_failures(job, Sidecar(items=two + land))
    assert len(short) == 1 and short[0].startswith("PWR Path line 19's link R1.1~R2.1: the via group at (10,14) is 2 barrels of 0.4 mm drill at 1.016 mm pitch") and "over the 0.871 A one holds" in short[0], short
    one_layer = path_copper_failures(job, Sidecar(items=two + land[:1]))
    assert one_layer == ["PWR Path line 19's link R1.1~R2.1: the via group at (10,14) has a landing on F.Cu only; both layers' copper must run over every barrel"], one_layer


@pytest.mark.kicad
def test_the_path_gates_judge_copper_no_role_group_owns(tmp_path: Path):
    """The blocker: path gates skipped locked (`layout.core.py`) copper, so width, vias, layers, the
    Kelvin junction and the off-path width went unchecked on it and the build reported ok. The routed
    fixture with every `pcbc:` group removed is that board (all its copper the author's): unmutated it
    passes every gate, and each refuter mutation of round 2 is named."""
    from pcbc.build import build_job
    from pcbc.copper import power_ampacity_failures
    from pcbc.fab import _owned_copper
    from pcbc.route_verify import gate_roles, junction_lines, path_junctions

    board = _fx(tmp_path)  # with layout.core.py: the wall W makes the 2 A link change layers
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result.get("error")
    text = (board.parent / "layout" / board.stem / "routed" / "layout.kicad_pcb").read_text()
    core = re.sub(r'\n\t\(group "pcbc:[^\n]*(?:\n\t\t[^\n]*)*\n\t\)', "", text)
    assert '(group "pcbc:' not in core
    design = load_board(board)
    job = compile_design(design)

    def gates(t: str) -> list[str]:
        roles = gate_roles(t, job.constraints)
        return power_ampacity_failures(job, t, owned=_owned_copper(roles)) + path_copper_failures(job, roles) + junction_lines(path_junctions(t, design, job.constraints))

    assert gates(core) == []
    segs = list(re.finditer(r'\(segment\s*\(start ([-0-9.]+) ([-0-9.]+)\)\s*\(end ([-0-9.]+) ([-0-9.]+)\)\s*\(width ([-0-9.]+)\)\s*(?:\(locked yes\)\s*)?\(layer "([^"]+)"\)\s*\(net "?PWR"?\)', core))
    # a link narrowed: the widest F.Cu PWR segment to 0.5 mm
    wide = max((m for m in segs if m.group(6) == "F.Cu"), key=lambda m: float(m.group(5)))
    got = gates(core[: wide.start(5)] + "0.5" + core[wide.end(5) :])
    assert any(g.startswith("PWR core copper 0.5 mm on F.Cu < the 1.09 mm Path line 19's link") for g in got), got
    # the off-path branch to R4 narrowed (its width is the 0.16 mm class floor since docs/physics-subjects.md PR 3)
    off = next(m for m in segs if abs(float(m.group(5)) - 0.16) < 1e-9)
    got = gates(core[: off.start(5)] + "0.12" + core[off.end(5) :])
    assert any(g.startswith(f"PWR core copper 0.12 mm on {off.group(6)} < the 0.16 mm PWR needs on {off.group(6)} off its paths") for g in got), got
    # a barrel of a group dropped
    vias = list(re.finditer(r'\n\t\(via\s*\(at ([-0-9.]+) ([-0-9.]+)\)(?:.|\n)*?\(net "?PWR"?\)(?:.|\n)*?\n\t\)', core))
    assert len(vias) >= 6
    got = gates(core[: vias[1].start()] + core[vias[1].end() :])
    assert any("the via group at" in g and "2 barrels" in g and "over the 0.871 A one holds" in g for g in got), got
    # the feedback tap's first segment moved to leave the rail mid-trace (a Kelvin junction off its pad)
    tap = next(m for m in segs if abs(float(m.group(5)) - 0.2) < 1e-9 and (m.group(1), m.group(2)) == ("32.5375", "6"))
    mid = core[: tap.start(1)] + "32.5375 10" + core[tap.end(2) :]
    got = gates(mid)
    assert any(g.startswith("PWR: Path line 20's copper") and "outside its first station R3.1's pad" in g for g in got), got
    # Postfix B5: the line names the two pieces of copper and where they are, not only that they meet.
    assert any(g.startswith("PWR: Path line 20's copper") and ": its core track (32.5375,10)-(" in g and " meets the core track (" in g for g in got), got

    # The same Kelvin mutation as a `layout.core.py` an author wrote, through the whole build: every PWR
    # line of the generated file locked, the tap's first segment starting mid-rail. The junction gate is
    # fatal since round 2, so the build stops on it (it used to build ok).
    gen = (board.parent / "layout" / board.stem / "routed" / "layout.gen.py").read_text().splitlines()
    pwr = [ln for ln in gen if ln.startswith(('Seg("PWR"', 'Via("PWR"'))]
    tap_line = next(ln for ln in pwr if ln.startswith('Seg("PWR", (32.5375, 6.0)') and "width=0.2," in ln)
    pwr[pwr.index(tap_line)] = tap_line.replace('Seg("PWR", (32.5375, 6.0)', 'Seg("PWR", (32.5375, 10.0)', 1)
    put_core(board.parent / "layout.core.py", "\n".join(pwr) + "\n")
    locked = build_job(board, upto="route", force=True)
    err = str(locked.get("error") or "")
    assert err.startswith("a path's copper meets its net outside its stations: PWR: ") and "PWR: Path line 20's copper overlaps core copper of R2.1~R3.1 on F.Cu" in err, err
    assert "outside its first station R3.1's pad" in err and not PROPOSALS.search(err), err


# --- the router: same-net junctions judged by the copper outside the pads ------------------------------


def test_two_wide_tracks_meeting_inside_a_pad_do_not_touch_outside_it():
    """Major (router): a 1.367 mm link leaving a 1206 pad that its 1.367 mm feed enters at a right angle
    was refused, because the clipped feed kept its round end, which reaches back into the pad. Contact
    that is inside the pad (its witness) counts only if the copper wholly outside the pad touches too.
    A tap meeting a rail mid-trace, outside every pad, still touches."""
    from pcbc.route_geom import Shape, hull

    pad = Shape(hull([(31.975, 12.125), (33.1, 12.125), (33.1, 13.875), (31.975, 13.875)]), 0.0)
    feed = ("track", (30.78, 13.0), (32.5375, 13.0), 1.367)
    leave = ("track", (32.5375, 13.0), (32.5375, 6.0), 1.367)
    from pcbc.route_geom import clip_outside

    f_parts = [track_shape(a, b, 1.367) for a, b in clip_outside(feed[1], feed[2], [pad], 0.2)]
    l_parts = [track_shape(a, b, 1.367) for a, b in clip_outside(leave[1], leave[2], [pad], 0.2)]
    assert f_parts and l_parts
    assert not any(contact_outside(x, feed, y, leave, [pad], 0.2, 0.2) for x in f_parts for y in l_parts)
    rail = ("track", (32.5375, 13.0), (32.5375, 6.0), 1.367)
    tap = ("track", (32.5375, 10.0), (27.0, 10.0), 0.2)
    r_parts = [track_shape(a, b, 1.367) for a, b in clip_outside(rail[1], rail[2], [pad], 0.2)]
    t_parts = [track_shape(a, b, 0.2) for a, b in clip_outside(tap[1], tap[2], [pad], 0.2)]
    assert any(contact_outside(x, tap, y, rail, [pad], 0.2, 0.2) for x in t_parts for y in r_parts)
    assert contact_outside(via_shape((40.0, 0.0), 0.8), ("via", (40.0, 0.0), 0.8), via_shape((40.5, 0.0), 0.8), ("via", (40.5, 0.0), 0.8), [pad], 0.2, 0.2)


def _route(board: Path):
    from pcbc.place_native import place
    from pcbc.route_native import route_stage

    design = load_board(board)
    pl = place(design, name=board.stem, base=board.parent / "layout" / board.stem / "routed")
    assert pl.error is None, pl.error
    from pcbc.layout_emit import stamp
    from pcbc.layout_job import load_core

    raw, _gr = load_core(design, board, pl, name=board.stem)
    core = stamp(design, raw) if raw else []
    return design, route_stage(design, pl.job, pl, name=board.stem, core_cu=core)


def test_a_3a_link_with_a_straight_via_free_route_is_routed(tmp_path: Path):
    """Major (router): R2.1 -> R3.1 carries 3 A (2.276 mm with no pour under F.Cu), and a straight
    via-free run between them meets every criterion; the router left it unrouted and blamed placement.
    It routes, on F.Cu, with no via of its own."""
    board = _fx(tmp_path, [('Path("PWR", "R1.1", "R2.1", "R3.1", amps=2)', 'Path("PWR", "R1.1", "R2.1", "R3.1", amps=3)')])
    _design, rt = _route(board)
    assert rt.unrouted == [], [(f.a, f.b, f.why) for f in rt.failed]
    mine = [p for p in rt.pieces if p.link == "R2.1~R3.1"]
    assert mine and all(p.kind == "seg" and p.layer == "F.Cu" for p in mine), [(p.kind, p.layer) for p in mine]


def test_a_link_s_layers_bound_the_router(tmp_path: Path):
    """Major (tests): `Path(layers=)` was enforced by no tested router behaviour. With layers=("F.Cu",)
    the 2 A link R1.1 -> R2.1 cannot cross the F.Cu wall W, and the router leaves it open rather than
    take B.Cu; the error states the rule and its line, and proposes nothing."""
    from pcbc.route_native import move_lines

    board = _fx(tmp_path, [('Path("PWR", "R1.1", "R2.1", "R3.1", amps=2)', 'Path("PWR", "R1.1", "R2.1", "R3.1", amps=2, layers=("F.Cu",))')])
    design, rt = _route(board)
    assert not [p for p in rt.pieces if p.link and p.layer == "B.Cu"], "no link copper on a layer its path does not allow"
    assert any(f.link is not None and f.link.key == "R1.1~R2.1" for f in rt.failed), [(f.a, f.b) for f in rt.failed]
    text = "\n".join(move_lines(design, compile_design(design), rt))
    assert "Rules: layers F.Cu (Path line 19)" in text, text
    assert not PROPOSALS.search(text), text


# --- messages that must be true ---------------------------------------------------------------------


def _buck(tmp_path: Path, edits) -> Path:
    work = tmp_path / "buck"
    shutil.copytree(EXAMPLES / "buck", work, ignore=shutil.ignore_patterns("layout"))
    edit_board(work / "buck.py", edits)
    return work / "buck.py"


FB = 'Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")'


def test_a_sense_path_ending_at_a_connector_is_refused(tmp_path: Path):
    """Major (messages): a backwards sense path whose far station is a connector's pad was accepted, and
    the connector's current ran in the 0.2 mm sense trace. node: a sense path from the LDO's input to
    the USB-C connector's VBUS pins (two pads, so the two-pad-part rule cannot see it)."""
    work = tmp_path / "node"
    shutil.copytree(EXAMPLES / "node", work, ignore=shutil.ignore_patterns("layout"))
    text = (work / "node.py").read_text()
    anchor = 'Path("VBUS", "J1.VBUS", "U2.VIN", amps=1)'  # node's own VBUS Path, replaced: one link carries one width
    assert anchor in text
    text = text.replace(anchor, 'Path("VBUS", "U2.VIN", "J1.VBUS", kind="sense")', 1)
    write_split(work / "node.py", text)
    cs = compile_constraints(load_board(work / "node.py"))
    assert cs.refusals == (
        'Path("VBUS") line 66, kind sense: its far station J1.VBUS is a pad of connector J1, where its current comes onto or off the board; '
        "a sense Path's copper beyond its first station U2.VIN reaches the rest of VBUS only inside U2.VIN, so J1.VBUS's current would run in the sense trace",
    ), cs.refusals


def test_a_net_kind_on_a_path_is_refused_with_the_nets_real_kind(tmp_path: Path):
    """Major (messages): `kind="analog"` on a power net's Path was refused as "the net's" kind."""
    cs = compile_constraints(load_board(_buck(tmp_path, [(FB, 'Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="analog")')])))
    assert cs.refusals == (
        'Path("5V") line 50: kind="analog" is a net kind, and 5V is kind power (NetReq line 42); on a Path, kind= names a sense branch and takes "feedback" or "sense"',
    ), cs.refusals


def test_a_path_on_every_pad_with_a_path_kind_is_the_whole_node(tmp_path: Path):
    """Major (messages): this was refused with two NetReq lines to choose from, each of them refused; then with one
    true error that still left the property homeless (refutation one-home r1, defect 6). A Path on every pad is the
    whole node, so its kind is accepted there."""
    cs = compile_constraints(load_board(_buck(tmp_path, [(FB, FB + '\nPath("FB", "R_FB_TOP.2", "U1.FB", "R_FB_BOT.1", kind="sense")')])))
    assert cs.refusals == (), cs.refusals


def test_the_chain_line_is_an_error_that_states_the_fact(tmp_path: Path):
    """direction.md §5b: a Chain() line is an error stating that Chain() is now Path(), with no rewritten line."""
    board = _buck(tmp_path, [('Path("SW", "U1.SW", "L1.1", amps=2)', 'Chain("SW", "U1.SW", "L1.1")'), ("from pcbc import ", "from pcbc import Chain, ")])
    with pytest.raises(ValueError) as exc:
        load_board(board)
    assert str(exc.value) == "buck.py line 46: Chain() is now Path(), with the same arguments\nbuck.py line 4: the import names Chain, which pcbc no longer exports", str(exc.value)


def test_a_branch_walled_in_by_its_own_nets_path_copper_names_it(tmp_path: Path):
    """Major (messages): the language refuters' buck, as written. The off-path SW branch C5.2 -> U1.2
    is walled in by SW's own 2 A path copper (the junction rule makes it foreign there), and the error
    said "nothing near them holds a cell". It names the link that holds the cells, and the rule."""
    from pcbc.route_native import move_lines

    work = tmp_path / "b1"
    shutil.copytree(ROOT / "tests" / "fixtures" / "r2_b1", work, ignore=shutil.ignore_patterns("layout"))
    # As the refuters placed it and constrained it: decision 4 (2026-10-01) now keeps C1 out of U1.SW's exit corridor
    # and decision 2 lets the switch node change layer, and with either the board routes SW. So the four parts
    # decision 4 moved are declared where the relation placer put them before, and SW is held to F.Cu with no vias
    # as the switch_node preset held it then.
    board = work / "r2_b1.py"
    edit_board(board, (
        ('\nPlace("C1", to="U1.VIN")', '\nPlace("C1", at=(15.46, 12.676), rot=180)'),
        ('\nPlace("L1", to="U1.SW")', '\nPlace("L1", at=(21.84, 14.996), rot=180)'),
        ('\nPlace("C3", to="L1.2")', '\nPlace("C3", at=(27.362, 14.046), rot=90)'),
        ('\nPlace("C4", to="L1.2")', '\nPlace("C4", at=(28.082, 16.996), rot=0)'),
        # PR 5: SW held to F.Cu with no vias on its path (a net writes no layers=/vias=).
        ('Path("SW", "U1.SW", "L1.1", amps=1.5)', 'Path("SW", "U1.SW", "L1.1", amps=1.5, layers=["F.Cu"], vias=False)'),
    ))
    design, rt = _route(board)
    # docs/physics-subjects.md PR 3 (measured 2026-10-02): VIN and GND declare no current, so their copper is the
    # 0.16 mm class floor (it was 0.5 mm at the NetReq's 1.5 A), the spine is deleted, and the SW branch
    # C5.2 -> U1.2 now routes on this placement: the wall this test was written for is gone from the fixture.
    # Every remaining refusal still states facts only.
    assert "SW" not in rt.unrouted, rt.unrouted
    for text in move_lines(design, compile_design(design), rt):
        assert not PROPOSALS.search(text), text


def test_a_station_whose_own_part_blocks_it_names_that_pad_and_no_mover(tmp_path: Path):
    """Major (messages): the station-escape warning named the part whose own pads block it as the
    thing to move. It names the pad in the way, the rule, that the part carries both nets, and the
    Place lines; nothing to move."""
    from pcbc.build import pcb_job

    # docs/physics-subjects.md PR 7: feedback is a Path kind; FB keeps its kind analog and writes the keep-away
    board = _buck(tmp_path, [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", keep_clear_of="SW")')])
    (m,) = [m for m in pcb_job(board)["layout_report"] if "can leave" in m]
    assert "U1.FB (FB) is 1.30 mm from it and needs 3 mm (keep_clear_of SW on NetReq line 44)" in m and "U1 carries both SW and FB" in m, m
    assert "move" not in m and not PROPOSALS.search(m), m


def test_a_loop_over_budget_names_the_place_lines_that_put_its_parts_there(tmp_path: Path):
    """Major (messages): Loop and Path-order warnings named the anchor ("no Place() attaches U1")
    instead of the Place line the author wrote. Both parts' lines are named."""
    from pcbc.build import pcb_job

    board = _buck(tmp_path, [('Place("C_IN1", to="U1.VIN")\nPlace("C_IN2", to="U1.VIN")\nPlace("L1", to="U1.SW")',
                              'Place("C_IN1", at=(16.46, 22.0), rot=180)\nPlace("C_IN2", at=(6.0, 20.0), rot=180)\nPlace("L1", at=(22.84, 13.996), rot=180)')])
    hit = [m for m in pcb_job(board)["layout_report"] if m.startswith("loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52)")]
    assert hit == [
        "loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52) encloses 22.9 mm2 over the 20 mm2 budget (pcbc default; U1 carries SW); "
        "C_IN1.1 is 12.0 mm from U1.VIN (layout.core.py:8 places C_IN1; layout.core.py:3 places U1)"
    ], hit
    assert not any("no Place() attaches" in m for m in pcb_job(board)["layout_report"])


def test_a_junction_line_names_both_pieces_and_where_they_are():
    """Postfix B5 (refutation nosuggest r2): "PWR: Path line 13's copper overlaps route copper of PWR on F.Cu by 0.5611 mm,
    outside its station pads" gave no coordinates and no item; both pieces were pcbc's own route copper. A row carries the
    two pieces (`_piece_where`) and the line states them."""
    from pcbc.route_verify import _piece_where, junction_lines

    mine = _piece_where(("track", (18.8075, 11.73), (20.5375, 10.0), 2.276), {"reason": "route"})
    theirs = _piece_where(("via", (19.35, 9.952), 0.8), {"reason": "route"})
    row = {"net": "PWR", "path": "Path line 13", "station": "R1.1", "touched": "route copper of PWR on F.Cu", "distance": -0.5611, "rule": "amps", "mine": mine, "theirs": theirs}
    assert junction_lines([row]) == [
        "PWR: Path line 13's copper overlaps route copper of PWR on F.Cu by 0.5611 mm, outside its station pads: "
        "its route track (18.8075,11.73)-(20.5375,10) 2.276 mm meets the route via at (19.35,9.952)"
    ]


PROPOSAL_TEXT = re.compile(r"\b(leave \w+= out|name this group|attach \S+ to a pin|try |instead|something else)\b")


def test_core_and_schematic_refusals_state_facts_and_propose_no_edit(tmp_path: Path):
    """Postfix B8-B10 (refutation nosuggest r2, direction.md §7b): three refusals ended in an edit to make —
    "leave tenting= out", "name this group something else", "attach J_OUT to a pin on this side, or to the far end of
    the run". Each now ends at its facts."""
    import shutil

    from pcbc.language import load_board as _load
    from pcbc.layout_job import load_core

    work = tmp_path / "blinky"
    shutil.copytree(ROOT / "examples" / "blinky", work, ignore=shutil.ignore_patterns("layout"))
    board = work / "blinky.py"
    design = _load(board)
    core = work / "layout.core.py"
    put_core(core, 'Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width=0.16, id="s1")\nGroup("pcbc:mine", ["s1"], id="g1")\n')
    with pytest.raises(ValueError) as e:
        load_core(_load(board), board)
    assert str(e.value).endswith("group names starting 'pcbc:' are the build's own (a piece's role), and a core line's group is the author's"), str(e.value)
    put_core(core, 'Via("GND", (3, 3), tenting={"front": "none", "back": "none"}, id="v1")\n')
    with pytest.raises(ValueError) as e:
        load_core(_load(board), board)
    assert "what KiCad writes nothing for: its save drops the field" in str(e.value) and not PROPOSAL_TEXT.search(str(e.value)), str(e.value)
    # the schematic issue: tests/test_buck.py::test_a_taken_lane_is_reported
