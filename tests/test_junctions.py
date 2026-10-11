"""Sense paths, the current rule, and path keep-aways (`docs/net-path-physics.md` §3.2, PR 5).

Two legality rules where branches join: a sense path touches the rest of its net only inside its first
station's pad; a path that writes `amps=` is met by the rest of its net only inside its station pads.
Contact inside an allowed pad (dilated by the net's clearance, one number for the router and the
emitted-board check) is legal for both sides.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from pcbc.constraints import compile_constraints
from pcbc.language import load_board
from pcbc.place_native import place
from pcbc.route_cost import net_cost
from pcbc.route_emit import seg_piece
from pcbc.route_native import net_links, route_stage
from pcbc.route_scene import build_scene, junction_rule
from conftest import declared_examples, write_split

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = declared_examples()  # the examples as shipped with a declared outline (vehicles): conftest.declared_examples

SHIPPED = 'NetReq("VIN", "5V", kind="power", volts=12); NetReq("GND", kind="power")\nNetReq("SW", kind="switch_node", max_mm=6)\nNetReq("FB", kind="analog")\n'
MINIMUM_TAP = 'Path("5V", "C_OUT1.1", "R_FB_TOP.1", kind="feedback")\n'


def _minimum_text() -> str:
    """The backwards-compatible minimum (§5): buck with line 55 as it shipped and one line added at 58."""
    text = (EXAMPLES / "buck" / "buck.py").read_text()
    if "Path(" in text:
        # From buck's currents comment (decision 5, 2026-10-01) when it has one: the minimum is the shipped file.
        start = text.index("# Currents at") if "# Currents at" in text else text.index('NetReq("VIN"')
        end = text.index('Loop("C_IN1.1"')
        end = text.index("\n", end) + 1
        if text.startswith('Path("VIN"', end):  # the input current's Path (docs/physics-subjects.md §8) is not the minimum
            end = text.index("\n", end) + 1
        text = text[:start] + SHIPPED + text[end:]
    text = text.replace(SHIPPED, SHIPPED + MINIMUM_TAP)
    return text.replace("from pcbc import Board, Capacitor, Ground, Loop, Net, NetReq, Path,", "from pcbc import Board, Capacitor, Ground, Net, NetReq, Path,").replace(
        "from pcbc import Board, Capacitor, Ground, Net, NetReq, Place,", "from pcbc import Board, Capacitor, Ground, Net, NetReq, Path, Place,"
    )


def _work(tmp_path: Path, text: str, name: str = "buck") -> Path:
    work = tmp_path / name
    shutil.copytree(EXAMPLES / "buck", work, ignore=shutil.ignore_patterns("layout"))
    write_split(work / "buck.py", text)
    return work / "buck.py"


def _routed(board: Path):
    design = load_board(board)
    pl = place(design, name=board.stem, base=board.parent / "layout" / board.stem / "routed")
    assert not pl.error, pl.error
    return design, pl, route_stage(design, pl.job, pl, name=board.stem)


def test_the_minimum_file_compiles_the_tap_alone(tmp_path: Path):
    board = _work(tmp_path, _minimum_text())
    lines = board.read_text().splitlines()
    assert lines[41] == MINIMUM_TAP.strip(), "line 42"
    cs = compile_constraints(load_board(board))
    assert cs.refusals == ()
    c = cs.by_net("5V")
    assert c.off_path == ("L1.2", "J_OUT.1", "C_OUT2.1"), c.off_path
    tap = cs.link("5V", "C_OUT1.1", "R_FB_TOP.1")
    assert tap.sense and tap.width_on("F.Cu") == 0.2 and not tap.via.allowed
    assert cs.pad_width("5V", "C_OUT1.1", "F.Cu") == 0.2, "the tap's 0.2 mm at its junction pad; 5V's own copper is the 0.16 mm class floor (PR 3: no net current)"
    assert "5V: path C_OUT1.1 -> R_FB_TOP.1 (Path line 42, kind feedback): width 0.2 mm (preset feedback), no vias, layers F.Cu, length 15 mm; meets 5V only inside C_OUT1.1, its first station" in cs.lines


def test_the_minimum_file_written_backwards_is_refused_once_a_path_carries_the_load(tmp_path: Path):
    """docs/physics-subjects.md PR 1: a net takes current only where a Path with amps= says so. With no such
    Path, C_OUT1's GND pad makes no load of C_OUT1.1 (GND is the B.Cu pour, whose return path is not
    built), so the backwards tap is accepted; with the load Path on 5V it is refused, by (c)."""
    back = 'Path("5V", "R_FB_TOP.1", "C_OUT1.1", kind="feedback")\n'
    cs = compile_constraints(load_board(_work(tmp_path, _minimum_text().replace(MINIMUM_TAP, back), name="bare")))
    assert cs.refusals == (), cs.refusals
    load = 'Path("5V", "L1.2", "C_OUT1.1", "J_OUT.1", amps=2)\n'
    cs = compile_constraints(load_board(_work(tmp_path, _minimum_text().replace(MINIMUM_TAP, back + load))))
    assert cs.refusals == (
        "Path(\"5V\") line 42, kind feedback: Path line 43 names C_OUT1.1, this Path's last station, and not R_FB_TOP.1, its first; a sense Path starts at the station it shares",
    ), cs.refusals


SHUNT = '''from pcbc import *
VBAT = Power("VBAT"); VLOAD = Power("VLOAD"); GND = Ground("GND")
Resistor("R_S", "0R01", package="1206", mpn="X", lcsc="C1", p1=VBAT, p2=VLOAD)
Resistor("R_A", "1k", package="0603", mpn="X", lcsc="C1", p1=VBAT, p2="AMP_P")
Resistor("R_B", "1k", package="0603", mpn="X", lcsc="C1", p1=VLOAD, p2="AMP_N")
Resistor("R_L", "1", package="1206", mpn="X", lcsc="C1", p1=VLOAD, p2=GND)
Resistor("R_IN", "0R", package="1206", mpn="X", lcsc="C1", p1=VBAT, p2=GND)
Board(width=30, height=20, layers=2, stackup="jlcpcb_2l_1oz")
NetReq("VBAT", "VLOAD", kind="power", volts=12)
Path("VLOAD", "R_S.2", "R_L.1", amps=5)
{paths}
for i, r in enumerate(["R_S", "R_A", "R_B", "R_L", "R_IN"]):
    Place(r, at=(5 + 5 * i, 10))
'''


@pytest.mark.parametrize(
    "paths,refused",
    [
        ('Path("VBAT", "R_S.1", "R_A.1", kind="sense")\nPath("VLOAD", "R_S.2", "R_B.1", kind="sense")', False),
        ('Path("VBAT", "R_A.1", "R_S.1", kind="sense")\nPath("VLOAD", "R_S.2", "R_B.1", kind="sense")', True),
    ],
    ids=["kelvin_from_the_shunt", "written_backwards"],
)
def test_a_shunt_is_sensed_from_its_own_pads(tmp_path: Path, paths: str, refused: bool):
    """Language v1-1's blocker: the reversed shunt routed the 5 A load through 0.2 mm sense copper with
    every other check passing. Refusal (a): its far station is a two-pad part whose other pad is a station
    of a Path that carries current (the load Path from R_S.2; docs/physics-subjects.md PR 1)."""
    board = tmp_path / "shunt.py"
    write_split(board, SHUNT.replace("{paths}", paths))
    cs = compile_constraints(load_board(board))
    hit = [r for r in cs.refusals if "kind sense: its far station R_S.1 is a pad of R_S, whose other pad R_S.2 is a station of Path line 10, which carries current on VLOAD" in r]
    assert bool(hit) == refused, cs.refusals
    if refused:
        assert hit[0].endswith("so it sits on the load; a sense Path starts where it leaves the rail"), hit[0]


def test_kruskal_never_takes_the_far_station(tmp_path: Path):
    """No off-path pad is ever paired with a sense path's far station: R_FB_TOP.1 is reached by the
    tap's own link and by nothing else (§4.2)."""
    board = _work(tmp_path, _minimum_text())
    design = load_board(board)
    pl = place(design, name="buck", base=board.parent / "layout" / "buck" / "routed")
    cs = pl.job.constraints
    scene = build_scene(design, pl.job, cs, pl.feet)
    terms, links, _uf = net_links(design, scene, "5V", (), cs)
    with_far = [(terms[i].owner, terms[j].owner, lk) for i, j, lk in links if "R_FB_TOP.1" in (terms[i].owner, terms[j].owner)]
    assert with_far == [("C_OUT1.1", "R_FB_TOP.1", cs.link("5V", "C_OUT1.1", "R_FB_TOP.1"))], with_far


def _pieces(out, net):
    return [p for p in out.pieces if p.net == net]


def test_the_routed_tap_meets_the_rail_only_at_its_junction(tmp_path: Path):
    """The minimum file routed: the tap is 0.2 mm, leaves the rail inside C_OUT1.1's pad, and no other
    5V copper comes within the net's clearance of it outside that pad (the router's own predicate,
    asked of the copper it handed on)."""
    design, pl, out = _routed(_work(tmp_path, _minimum_text()))
    assert "5V" not in out.unrouted, out.unrouted
    cs = pl.job.constraints
    tap = [p for p in _pieces(out, "5V") if p.link == "C_OUT1.1~R_FB_TOP.1"]
    assert tap and all(p.w == 0.2 and p.kind == "seg" and p.layer == "F.Cu" for p in tap), tap
    rest = [p for p in _pieces(out, "5V") if p.link != "C_OUT1.1~R_FB_TOP.1"]
    assert rest and all(p.link == "" for p in rest), "the rail's own copper is off-path"
    scene = build_scene(design, pl.job, cs, pl.feet)
    scene.add(scene.item_of(p) for p in out.pieces)
    nc = net_cost(cs, "5V")
    rule = junction_rule(scene, cs, "5V", cs.link_by_key("C_OUT1.1~R_FB_TOP.1"), nc)
    assert rule is not None and rule.clearance == cs.by_net("5V").clearance_mm.value, "one number: the net's clearance"
    assert rule.clashes(scene, tap) is None
    off = junction_rule(scene, cs, "5V", None, nc)
    assert off.clashes(scene, rest) is None


def test_contact_just_outside_the_junction_pad_is_foreign(tmp_path: Path):
    """Code v1-R4: a probe of off-path 5V copper 0.15 mm from the tap and outside the junction pad's
    clearance band clashes; the same probe 0.25 mm away does not. The router and the emitted-board
    check clip at the same number (`route_geom.clip_outside` at the net's clearance)."""
    design, pl, out = _routed(_work(tmp_path, _minimum_text()))
    cs = pl.job.constraints
    scene = build_scene(design, pl.job, cs, pl.feet)
    scene.add(scene.item_of(p) for p in out.pieces)
    tap = [p for p in _pieces(out, "5V") if p.link == "C_OUT1.1~R_FB_TOP.1"]
    seg = max(tap, key=lambda p: p.mm)
    (ax, ay), (bx, by) = seg.a, seg.b
    length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    nx, ny = -(by - ay) / length, (bx - ax) / length
    mx, my = (ax + bx) / 2, (ay + by) / 2
    w = cs.by_net("5V").width_mm.value
    rule = junction_rule(scene, cs, "5V", None, net_cost(cs, "5V"))
    got = {}
    for air in (0.15, 0.25):
        off = seg.w / 2 + w / 2 + air
        for sgn in (1, -1):
            px, py = mx + sgn * nx * off, my + sgn * ny * off
            probe = seg_piece("5V", "route", "F.Cu", (px - 0.1 * (bx - ax) / length, py - 0.1 * (by - ay) / length), (px + 0.1 * (bx - ax) / length, py + 0.1 * (by - ay) / length), w)
            if rule.in_allowed(probe.a) or rule.in_allowed(probe.b):
                continue
            got.setdefault(air, []).append(rule.clashes(scene, [probe]))
    assert got[0.15] and all(x is not None for x in got[0.15]), got
    assert got[0.25] and any(x is None for x in got[0.25]), got


def test_an_off_path_load_never_tees_onto_a_current_path(tmp_path: Path):
    """The §5 file routed: C_OUT2.1's branch (off-path, the 0.16 mm class floor since docs/physics-subjects.md
    PR 3) meets the 2 A trunk only inside a station pad of Path line 48, never mid-link (rule 2)."""
    board = _work(tmp_path, (EXAMPLES / "buck" / "buck.py").read_text())
    design, pl, out = _routed(board)
    cs = pl.job.constraints
    scene = build_scene(design, pl.job, cs, pl.feet)
    scene.add(scene.item_of(p) for p in out.pieces)
    off = [p for p in _pieces(out, "5V") if p.link == ""]
    assert off and {p.w for p in off if p.kind == "seg"} == {0.16}, off
    rule = junction_rule(scene, cs, "5V", None, net_cost(cs, "5V"))
    assert rule.clashes(scene, off) is None


@pytest.mark.kicad
def test_a_junction_moved_to_the_inductor_is_named_on_the_emitted_board(tmp_path: Path):
    """The mutation of PR 5: the tap's first segment moved to start inside L1.2 on the emitted board;
    `route_verify.path_junctions` names the pad it touches."""
    from conftest import routed_result
    from pcbc.ampacity import board_pad_geoms
    from pcbc.route_verify import path_junctions

    board = _work(tmp_path, _minimum_text())
    out, _result = routed_result("buck_minimum", board)
    text = out.read_text()
    design = load_board(board)
    cs = compile_constraints(design)
    assert path_junctions(text, design, cs) == [], "the router held the rule"
    pads = {p.id: p for p in board_pad_geoms(text) if p.net == "5V"}
    c1, l1 = pads["C_OUT1.1"].at, pads["L1.2"].at
    # the tap segment that starts (or ends) at C_OUT1.1's centre: move that end to L1.2's centre
    pat = re.compile(r"\(segment\s*\(start ([-0-9.]+) ([-0-9.]+)\)\s*\(end ([-0-9.]+) ([-0-9.]+)\)\s*\(width 0\.2\)")
    for m in pat.finditer(text):
        sx, sy, ex, ey = (float(m.group(k)) for k in range(1, 5))
        if abs(sx - c1[0]) < 1e-3 and abs(sy - c1[1]) < 1e-3:
            text = text[: m.start(1)] + f"{l1[0]:g} {l1[1]:g}" + text[m.end(2) :]
            break
        if abs(ex - c1[0]) < 1e-3 and abs(ey - c1[1]) < 1e-3:
            text = text[: m.start(3)] + f"{l1[0]:g} {l1[1]:g}" + text[m.end(4) :]
            break
    else:
        pytest.fail("no 0.2 mm tap segment ends at C_OUT1.1's centre")
    got = path_junctions(text, design, cs)
    assert any(r["touched"] == "pad L1.2" and r["path"] == "Path line 42" for r in got), got
