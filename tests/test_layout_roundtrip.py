"""The layout language is KiCad's primitives, field for field, and the build's round trip is lossless.

Step 1 of the layout pipeline (`docs/layout-properties.md`): the router routes exactly as before, the
routed board is decompiled into `layout.gen.py`, and the final board is emitted from the Python. What
these tests hold:

- **The census.** Every token KiCad 10 wrote under a segment, arc, via, zone, footprint pose or board
  drawing (all sixteen kinds) has a Python field (`gen.CENSUS`), on KiCad's own saves: three probe
  boards (`fixtures/kicad10/make_kicad10_primitives*.py`) and the routed examples.
- **KiCad -> Python -> KiCad** gives back the same primitive; **Python -> KiCad -> Python** gives back
  the same bytes of `layout.gen.py`.
- **The build.** The emitted board carries the router's copper exactly (every field, the fills
  KiCad regenerates included), KiCad's DRC counts it the same, and two builds are byte-identical.
- **Core.** A `Place` in `layout.core.py` is refused naming `board.py`; a copied gen line locks that
  object; an illegal one is refused naming its line.
- **"verified"** is KiCad's word only.
"""

from __future__ import annotations

import base64
import shutil
from collections import Counter
from pathlib import Path

import pytest

from conftest import routed_board, routed_result
from pcbc.gen import CENSUS, Unmapped, census, decompile, gen_source, line_of, unmapped
from pcbc.language import load_board, load_layout
from pcbc.layout_emit import render_graphics, render_one
from pcbc.model import Copper, Design, Graphic, Pose
from pcbc.sexp import Q, parse_tree

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIX = Path(__file__).resolve().parent / "fixtures" / "kicad10"
PROBES = sorted(FIX.glob("kicad10_primitives*.kicad_pcb"))
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"


def _canon(node):
    """A primitive as a comparable value: numbers compared **exactly** (a ratio or an angle KiCad keeps
    as a double must not come back cut to six decimals), quoted and bare atoms kept apart, children
    order-free (KiCad's save orders them itself), and the fill and the render cache left out."""
    if isinstance(node, list):
        kids = [_canon(x) for x in node[1:] if not (isinstance(x, list) and x and x[0] in ("filled_polygon", "render_cache"))]
        return (_canon(node[0]), tuple(sorted(map(repr, kids))))
    if isinstance(node, Q):
        return ("Q", str(node))
    try:
        return ("n", float(node))
    except ValueError:
        return ("a", str(node))


def _by_uuid(text: str) -> dict[str, list]:
    from pcbc.layout_prims import HEADS

    out = {}
    for n in parse_tree(text)[1:]:
        if isinstance(n, list) and (n[0] in ("segment", "arc", "via", "zone") or n[0] in HEADS):
            uid = next((str(x[1]) for x in n[1:] if isinstance(x, list) and x[0] == "uuid"), None)
            if uid is None:
                continue  # KiCad's own teardrop zone (`gen.is_teardrop_zone`): no uuid, not a layout object
            out[uid] = n
    return out


def _members_as_uuids(board) -> dict[str, str]:
    """Straight out of the decompiler a group's members are uuids; render them as themselves."""
    return {m: m for g in board.graphics for m in g.f.get("members", ())}


# ------------------------------------------------------------------------------------ the census


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_every_field_kicad_wrote_on_a_probe_board_has_a_python_field(probe: Path):
    """pcbnew set every via, track and zone option it exposes and saved; nothing it wrote is unmapped."""
    assert unmapped(census(probe.read_text())) == []


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_a_kicad_primitive_to_python_and_back_is_the_same_primitive(probe: Path):
    text = probe.read_text()
    orig = _by_uuid(text)
    board = decompile(text)
    assert len(board.copper) + len(board.graphics) == len(orig)
    for c in board.copper:
        assert _canon(parse_tree(render_one(c, board="x"))) == _canon(orig[c.uuid]), c
    ident = _members_as_uuids(board)
    for g in board.graphics:
        assert _canon(parse_tree(render_graphics([g], board="x", uuids=ident))) == _canon(orig[g.uuid]), g


def test_a_token_no_field_carries_is_refused_not_dropped():
    text = PROBES[0].read_text().replace("\t\t(width 0.25)\n", "\t\t(width 0.25)\n\t\t(frobnicate 1)\n", 1)
    with pytest.raises(Unmapped, match="frobnicate"):
        decompile(text)
    assert unmapped({"segment": {"frobnicate": 1}}) == ["segment/frobnicate"]


def test_a_gen_line_loads_back_to_the_object_it_was_written_from(tmp_path: Path):
    """Python -> Python: every object of the probe boards, written as a gen line and executed."""
    objs = [c for p in PROBES for c in decompile(p.read_text()).copper]
    for i, c in enumerate(objs):
        c.id = f"{c.kind}-{i}"
    poses = [Pose("R1", (1.5, -2.25), 90.0, "B.Cu", True)]
    gen = tmp_path / "layout.gen.py"
    gen.write_text(gen_source(poses, objs, board="x"))
    design = Design()
    load_layout(gen, design, source="gen")
    got = {c.id: c for c in design.copper}
    fields = [f for f in Copper.__dataclass_fields__ if f not in ("source", "where", "comment")]
    for c in objs:
        assert {f: getattr(got[c.id], f) for f in fields} == {f: getattr(c, f) for f in fields}, line_of(c)
    assert [(p.ref, p.at, p.rot, p.layer, p.locked) for p in design.poses] == [("R1", (1.5, -2.25), 90.0, "B.Cu", True)]
    assert gen_source(design.poses, [got[c.id] for c in objs], board="x") == gen.read_text()


def test_the_docs_table_is_the_census_map():
    doc = (ROOT / "docs" / "layout-properties.md").read_text()
    for prim, paths in CENSUS.items():
        for path in paths:
            assert f"| `{prim}` | `{path}` |" in doc, f"{prim}/{path} is not in docs/layout-properties.md"


# ------------------------------------------------------------------------------------ core rules


def _blinky(tmp_path: Path) -> tuple[Path, Design]:
    board = tmp_path / "blinky.py"
    board.write_text((EXAMPLES / "blinky" / "blinky.py").read_text())
    return board, load_board(board)


def test_a_place_in_layout_core_is_refused_naming_board_py(tmp_path: Path):
    board, design = _blinky(tmp_path)
    core = tmp_path / "layout.core.py"
    core.write_text('Seg("LED", (1, 2), (3, 2), width=0.25)\nPlace("R1", at=(10, 10))\n')
    with pytest.raises(ValueError, match=r"layout\.core\.py:2: Place\('R1'\).*board\.py"):
        load_layout(core, design, source="core")


def test_a_place_in_layout_gen_is_a_pose_and_leaves_board_py_s_placement_alone(tmp_path: Path):
    board, design = _blinky(tmp_path)
    before = [(p.ref, p.at) for p in design.places]
    gen = tmp_path / "layout.gen.py"
    gen.write_text('Place("R1", at=(8.93, 12.5), rot=0.0, layer="F.Cu", locked=True)\n')
    load_layout(gen, design, source="gen")
    assert [(p.ref, p.at) for p in design.places] == before
    assert [(p.ref, p.at, p.layer, p.locked, p.where) for p in design.poses] == [("R1", (8.93, 12.5), "F.Cu", True, "layout.gen.py:1")]


def test_a_via_joins_only_what_its_copper_touches(tmp_path: Path):
    """Grok's check unioned every pad of a net once one via of it existed anywhere on the board."""
    from pcbc.layout_check import check_layout
    from pcbc.layout_job import board_pads

    from conftest import placed_board

    placed = placed_board("blinky", EXAMPLES / "blinky" / "blinky.py")
    design = load_board(EXAMPLES / "blinky" / "blinky.py")
    pads, layers = board_pads(design, placed.read_text())
    led = [p for p in pads if p.net == "LED"]
    assert len(led) == 2
    far = Copper("via", "stray", "LED", layer=None, at=(1.0, 1.0), size=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), source="gen")
    design.copper = [far]
    fails, notes = check_layout(design, pads, layers=layers)
    assert fails == [] and any(n.startswith("LED: its pads are in 2 pieces") for n in notes), notes
    a, b = (p.at for p in led)
    design.copper = [Copper("seg", "join", "LED", layer="F.Cu", a=a, b=b, width=0.2, source="gen"), far]
    fails, notes = check_layout(design, pads, layers=layers)
    assert not any(n.startswith("LED:") for n in notes), notes


def test_the_property_check_never_says_verified(tmp_path: Path):
    """Only `netcheck.check_copper` (KiCad) may call copper verified; `layout_check` reports lists."""
    import ast
    import inspect

    import pcbc.layout_check as lc
    import pcbc.layout_job as lj

    for mod in (lc, lj):
        said = [n for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.Constant) and n.value == "verified"]
        assert said == [], f"{mod.__name__} writes the word only KiCad's gate may"


def test_gen_writes_no_todo_any_more():
    import pcbc.gen as gen

    assert not hasattr(gen, "write_todo") and not hasattr(gen, "GEN_TODO")


# ------------------------------------------------------------------------------------ the build


def _drc_types(pcb: Path, tmp: Path) -> dict:
    from pcbc.netcheck import _by_type, kicad_drc

    copy = tmp / pcb.name
    shutil.copy(pcb, copy)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = pcb.with_suffix(ext)
        if side.exists():
            shutil.copy(side, copy.with_suffix(ext))
    return _by_type(kicad_drc(copy, refill=False))


def _boards():
    out = [(n, EXAMPLES / n / f"{n}.py") for n in ("blinky", "buck", "c3_usb", "node")]
    if (DS2 / "ds2_addon.py").exists():
        out.append(("ds2_addon", DS2 / "ds2_addon.py"))
    return out


@pytest.mark.kicad
@pytest.mark.parametrize("name,board", _boards(), ids=[n for n, _ in _boards()])
def test_the_emitted_boards_have_no_unmapped_field(name: str, board: Path):
    """Every token KiCad wrote on the emitted board (after its refill and save) has a Python field —
    on all five boards, complete or not."""
    final = routed_board(name, board)
    assert unmapped(census(final.read_text())) == [], final


@pytest.mark.kicad
@pytest.mark.parametrize("name", ["blinky", "buck"])
def test_the_emitted_board_decompiles_to_the_very_bytes_of_layout_gen(name: str):
    """Python -> KiCad -> Python: the emitted board, as KiCad saved it after the gates' refill, gives
    back `layout.gen.py` byte for byte."""
    from pcbc.gen import assign_ids

    final = routed_board(name, EXAMPLES / name / f"{name}.py")
    back = decompile(final.read_text())
    assign_ids(back.copper, set(), back.graphics)
    assert gen_source(back.poses, back.copper, board=name, graphics=back.graphics) == (final.parent / "layout.gen.py").read_text()


@pytest.mark.kicad
def test_two_builds_are_byte_identical(tmp_path: Path):
    from pcbc.build import build_job

    board = tmp_path / "blinky.py"
    board.write_text((EXAMPLES / "blinky" / "blinky.py").read_text())
    routed = tmp_path / "layout" / "blinky" / "routed"
    got = []
    for _ in range(2):
        result = build_job(board, upto="route", force=True)
        assert result.get("error") is None, result
        assert result["gen"] == str(routed / "layout.gen.py")
        got.append(((routed / "layout.gen.py").read_bytes(), (routed / "layout.kicad_pcb").read_bytes()))
    assert got[0] == got[1]


def _buck(tmp_path: Path) -> Path:
    shutil.copytree(EXAMPLES / "buck", tmp_path / "buck", ignore=shutil.ignore_patterns("layout"))
    return tmp_path / "buck" / "buck.py"


@pytest.mark.kicad
def test_a_gen_line_copied_into_core_locks_that_segment(tmp_path: Path):
    from pcbc.build import build_job

    board = _buck(tmp_path)
    first = build_job(board, upto="route", force=True)
    assert first.get("error") is None, first
    gen = Path(first["gen"]).read_text().splitlines()
    line = next(x for x in gen if x.startswith('Seg("FB"'))
    uid = line.split('uuid="')[1].split('"')[0]
    (board.parent / "layout.core.py").write_text(line + "\n")
    again = build_job(board, upto="route", force=True)
    assert again.get("error") is None, again
    route = next(s for s in again["steps"] if s["stage"] == "route")
    assert route["copper"] == "verified" and route["core_objects"] == 1
    assert uid not in Path(again["gen"]).read_text()
    assert (board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb").read_text().count(f'(uuid "{uid}")') == 1


@pytest.mark.kicad
def test_an_illegal_core_segment_is_refused_naming_its_line(tmp_path: Path):
    """A core segment drawn across a pad of another net is a short: refused before routing."""
    from pcbc.build import build_job
    from pcbc.layout_job import board_pads

    board = _buck(tmp_path)
    first = build_job(board, upto="place", force=True)
    assert first.get("error") is None, first
    placed = board.parent / "layout" / "buck" / "placed" / "layout.kicad_pcb"
    pads, _layers = board_pads(load_board(board), placed.read_text())
    victim = next(p for p in pads if p.net and p.net != "FB" and "F.Cu" in p.cu_layers and p.kind == "smd")
    x, y = victim.at
    (board.parent / "layout.core.py").write_text(f'\nSeg("FB", ({x - 1.0}, {y}), ({x + 1.0}, {y}), width=0.25, layer="F.Cu")\n')
    result = build_job(board, upto="route", force=True)
    assert result["error"] and "layout.core.py:2" in result["error"] and "short" in result["error"], result["error"]
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert "copper" not in route and "verified" not in repr(route), route  # the schematic's KiCad netlist may say it; the copper may not


def test_without_kicad_nothing_is_called_verified(tmp_path: Path, monkeypatch):
    """KiCad is the arbiter: with no kicad-cli the native route stage still places, routes and emits
    (none of that needs KiCad), but nothing can judge the emitted board, so the build stops, says why,
    writes no stamp — and nothing calls the copper verified."""
    from pcbc.build import build_job

    monkeypatch.setattr("pcbc.netcheck.kicad_cli", lambda: Path("/nonexistent/kicad-cli"))
    board, _design = _blinky(tmp_path)
    result = build_job(board, upto="route", force=True)
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert result["error"] and "kicad-cli" in result["error"], result["error"]
    assert str(route["copper"]).startswith("unchecked") and "verified" not in repr(result), route
    assert not (tmp_path / "layout" / "blinky" / "inputs.json").exists(), "an unjudged route is not done"


# ------------------------------------------------------------------------------------ the fixes of the review
#
# Each test below reproduces one finding of the independent review of step 1 and pins the fix.


def test_the_third_probe_carries_every_board_drawing_kicad_has():
    """`make_kicad10_primitives3.py`: all sixteen drawing kinds, saved by KiCad itself."""
    from pcbc.layout_prims import HEADS

    heads = {n[0] for n in parse_tree((FIX / "kicad10_primitives3.kicad_pcb").read_text())[1:] if isinstance(n, list)}
    assert set(HEADS) <= heads, sorted(set(HEADS) - heads)


def test_numbers_kicad_keeps_as_doubles_are_not_cut_to_six_decimals():
    """Finding 1: `fmt_num` cut teardrop ratios, a hatch angle and a hole area to six decimals. They are
    written exactly now; lengths still go to KiCad's 1 nm."""
    board = decompile((FIX / "kicad10_primitives3.kicad_pcb").read_text())
    text = "".join(render_one(c, board="x") for c in board.copper)
    for exact in ("(best_length_ratio 0.1234567891)", "(best_width_ratio 0.9876543219)", "(hatch_orientation 12.3456789)", "(hatch_min_hole_area 0.3333333333)"):
        assert exact in text, exact


def _layout_file(tmp_path: Path, body: str) -> tuple[Path, Design]:
    board, design = _blinky(tmp_path)
    core = tmp_path / "layout.core.py"
    core.write_text(body)
    return core, design


@pytest.mark.parametrize(
    "body,line,says",
    [
        ('Seg("LED", (1, 2), (3, 2), width=0.25)\nfrom pcbc import Place as BoardPlace\nBoardPlace("R1", at=(12.0, 12.5))\n', 3, "board.py"),
        ('import pcbc.language as L\nL.Place("R1", at=(12.0, 12.5))\n', 2, "board.py"),
        ('from pcbc import NetReq\nNetReq("LED", kind="power", volts=3.3, amps=1.5)\n', 2, "board.py"),
        ('Keepout("KO1", box=(18, 10, 22, 15))\n', 1, "Pour(None, keepout="),
        ('Region("R", left=1, top=1, width=2, height=2)\n', 1, "Pour(None, keepout="),
        ('Seg("VIN", (18.85, 11.25), (18.85, 9.45), widht=0.781)\n', 1, "widht"),
        ('Seg("LED", (1, 2), (3, 2), width=0.2, id="dup")\nSeg("LED", (1, 3), (3, 3), width=0.2, id="dup")\n', 2, "layout.core.py:1"),
    ],
    ids=["from-pcbc-import-Place", "pcbc.language.Place", "NetReq", "Keepout", "Region", "typo", "duplicate-id"],
)
def test_a_layout_file_says_layout_primitives_only_and_names_the_line(tmp_path: Path, body: str, line: int, says: str):
    """Findings 6 and 7 and the typo/duplicate-id minors: a `board.py` object reached by any import path,
    `Keepout`/`Region`, a misspelt keyword and two objects with one id are each refused naming
    `layout.core.py:<line>` — never merged over `board.py`, never half-applied, never a traceback."""
    core, design = _layout_file(tmp_path, body)
    places = [(p.ref, getattr(p, "left", None)) for p in design.places]
    with pytest.raises(ValueError) as got:
        load_layout(core, design, source="core")
    assert f"layout.core.py:{line}" in str(got.value) and says in str(got.value), str(got.value)
    assert [(p.ref, getattr(p, "left", None)) for p in design.places] == places


def test_copper_and_drawings_in_board_py_are_refused_naming_the_line(tmp_path: Path):
    """Finding 10: a `Seg` in `board.py` was accepted and silently dropped by the build."""
    board = tmp_path / "blinky.py"
    board.write_text((EXAMPLES / "blinky" / "blinky.py").read_text() + '\nfrom pcbc import Seg\nSeg("LED", (10.0, 16.0), (30.0, 16.0), width=0.16, id="inboard")\n')
    n = len(board.read_text().splitlines())
    with pytest.raises(ValueError, match=rf"blinky\.py:{n}: .*belong in layout\.core\.py"):
        load_board(board)


def test_a_hand_written_pour_takes_compiled_numbers_and_a_low_clearance_is_refused(tmp_path: Path):
    """Minor: a core `Pour` that leaves its clearance and minimum thickness out gets the net's class
    clearance and the board's minimum track (not KiCad's 0.5 / 0.25), and one written below the
    board's minimum clearance is refused naming its line."""
    from pcbc.compile import compile_design
    from pcbc.layout_check import check_layout
    from pcbc.layout_emit import stamp
    from pcbc.stackup import get_stackup

    core, design = _layout_file(tmp_path, 'Pour("GND", layer="B.Cu", id="p1")\nPour("GND", layer="F.Cu", clearance=0.01, id="p2")\n')
    load_layout(core, design, source="core")
    job = compile_design(design)
    p1, p2 = stamp(design, design.copper)
    assert p1.clearance == job.constraints.by_net("GND").clearance_mm.value and p1.min_thickness == get_stackup(job.stackup).track_min and p1.filled is True
    design.copper = [p1, p2]
    fails, _notes = check_layout(design, [], layers=("F.Cu", "B.Cu"))
    assert any(f.startswith("layout.core.py:2: clearance 0.01 mm") for f in fails), fails


def test_a_pour_joins_only_what_its_outline_minus_its_holes_covers(tmp_path: Path):
    """Minor: the pour connectivity used the convex hull and ignored holes, joining pads no copper reaches."""
    from pcbc.layout_check import check_layout
    from pcbc.pads import PadGeom
    from pcbc.route_geom import rect_shape

    def pad(ref, at):
        return PadGeom(ref, "1", "N", "smd", "rect", at, 0.0, (rect_shape(at[0], at[1], 0.5, 0.5),), None, frozenset({"F.Cu"}), frozenset({"F.Mask"}), 0.0)

    pads = [pad("P", (1.0, 1.0)), pad("Q", (8.0, 8.0))]
    cases = {
        "square": (((0, 0), (10, 0), (10, 10), (0, 10)), (), True),
        "hole around Q": (((0, 0), (10, 0), (10, 10), (0, 10)), (((7, 7), (9, 7), (9, 9), (7, 9)),), False),
        "L, Q in the notch": (((0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)), (), False),
    }
    _board, d = _blinky(tmp_path)
    for name, (pts, holes, joined) in cases.items():
        d.copper = [Copper("pour", "p", "N", layer="F.Cu", points=tuple(pts), holes=tuple(holes), source="gen")]
        _fails, notes = check_layout(d, pads, layers=("F.Cu", "B.Cu"))
        assert (not any(n.startswith("N:") for n in notes)) == joined, (name, notes)


def test_the_decompiler_refuses_a_top_level_node_nobody_reads():
    """Minor: an unknown top-level node was skipped, and emit would then have dropped it."""
    text = PROBES[0].read_text().rstrip()[:-1] + "\t(frobnicate 1)\n)\n"
    with pytest.raises(Unmapped, match="frobnicate"):
        decompile(text)


def test_a_place_that_moves_a_part_moves_its_own_zones_as_kicad_does():
    """Minor: KiCad stores a footprint's zones in board coordinates; a move leaves them behind unless
    `set_poses` moves them (measured against pcbnew moving c3_usb's U1 by 1 mm: identical outlines)."""
    from pcbc.layout_emit import set_poses

    fp = (
        '(kicad_pcb\n\t(footprint "X"\n\t\t(layer "F.Cu")\n\t\t(uuid "u")\n\t\t(at 10 20)\n'
        '\t\t(property "Reference" "U1"\n\t\t\t(at 0 0 0)\n\t\t)\n'
        '\t\t(zone\n\t\t\t(net 0)\n\t\t\t(polygon\n\t\t\t\t(pts\n\t\t\t\t\t(xy 9 19) (xy 11 19) (xy 11 21)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n)\n'
    )
    got = set_poses(fp, [Pose("U1", (11.5, 20.0), 0.0, "F.Cu", False)])
    assert "(at 11.5 20)" in got and "(xy 10.5 19) (xy 12.5 19) (xy 12.5 21)" in got, got


@pytest.mark.kicad
@pytest.mark.parametrize("probe", PROBES, ids=lambda p: p.name)
def test_python_to_kicad_to_python_is_the_same_bytes_on_the_probes(probe: Path, tmp_path: Path):
    """Both directions through KiCad itself, for every copper field and every drawing kind: the probe's
    objects as `layout.gen.py`, emitted, loaded and saved by `kicad-cli pcb upgrade`, decompiled and
    written again give the same bytes. `kicad-cli` loading the emitted board is also the proof that
    every token the emitter writes (a via's `backdrill`, post-machining, ...) is one KiCad accepts."""
    import subprocess

    from pcbc.gen import assign_ids
    from pcbc.layout_emit import render, render_graphics, splice, strip_layout, uuid_of

    def gen_of(text: str) -> str:
        b = decompile(text)
        assign_ids(b.copper, set(), b.graphics)
        return gen_source(b.poses, b.copper, board="x", graphics=b.graphics)

    first = gen_of(probe.read_text())
    b = decompile(probe.read_text())
    assign_ids(b.copper, set(), b.graphics)
    ids = {o.id: uuid_of(o, "x") for o in [*b.copper, *b.graphics]}
    emitted = splice(strip_layout(probe.read_text()), render(Design(), b.copper, board="x") + render_graphics(b.graphics, board="x", uuids=ids))
    pcb = tmp_path / "rt.kicad_pcb"
    pcb.write_text(emitted)
    run = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", str(pcb)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert gen_of(pcb.read_text()) == first
    # `render_cache` is not a field: emitted without it, KiCad's save writes it back identical.
    caches = lambda text: [repr(n) for top in parse_tree(text)[1:] if isinstance(top, list) for n in top if isinstance(n, list) and n and n[0] == "render_cache"]  # noqa: E731
    assert "render_cache" not in emitted and caches(pcb.read_text()) == caches(probe.read_text())


@pytest.mark.kicad
def test_a_line_of_layout_gen_changed_before_emit_never_reaches_the_board(tmp_path: Path, monkeypatch):
    """Finding 4: narrowing a 5V track, narrowing SW below its class, or setting the GND pour to
    connect none in `layout.gen.py` built ok. The loaded gen must be the router's board field for field."""
    import pcbc.layout_job as lj
    from pcbc.build import build_job

    orig = lj.write_gen

    def narrowed(path, poses, copper, *, board, graphics=()):
        text = orig(path, poses, copper, board=board, graphics=graphics)
        new = text.replace('connect="solid"', 'connect="none"', 1)
        assert new != text
        Path(path).write_text(new)
        return new

    monkeypatch.setattr(lj, "write_gen", narrowed)
    board = _buck(tmp_path)
    result = build_job(board, upto="fab", force=True)
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert result["error"] and "layout.gen.py" in result["error"] and "connect" in result["error"], result["error"]
    assert "copper" not in route and not (board.parent / "layout" / "buck" / "fab").exists()


@pytest.mark.kicad
def test_the_gates_read_each_piece_s_role_and_geometry_off_the_emitted_board(tmp_path: Path):
    """Findings 3 and 8: the gates built their subjects from `copper.json`'s coordinates. The role
    lives on the board as a KiCad group (`pcbc:<reason>:<owner>`), the geometry is the board's, and
    the pieces the board reports are exactly the groups `layout.gen.py` wrote."""
    from pcbc.build import _thermal_gate
    from pcbc.layout_job import roles_doc

    final = routed_board("c3_usb", EXAMPLES / "c3_usb" / "c3_usb.py")
    text = final.read_text()
    doc = roles_doc(text)
    gen = Design()
    load_layout(final.parent / "layout.gen.py", gen, source="gen")
    by_id = {c.id: c for c in gen.copper}
    want = Counter()
    for g in gen.graphics:
        if g.kind == "group" and str(g.f.get("name", "")).startswith("pcbc:"):
            _p, reason, owner = g.f["name"].split(":", 2)
            for m in g.f["members"]:
                want[(by_id[m].uuid, reason, owner, by_id[m].net)] += 1
    got = Counter((i["uuid"], i["reason"], i["owner"], i["net"]) for i in doc.items)
    assert got == want and len(doc.items) > 50
    # Move one thermal via off its land in the board itself: the gate sees the board, not the sidecar.
    design = load_board(EXAMPLES / "c3_usb" / "c3_usb.py")
    assert _thermal_gate(text, doc, design)["fails"] == []
    via = next(i for i in doc.items if i["reason"] == "thermal")
    at = text.index(f'(uuid "{via["uuid"]}")')
    start = text.rindex("\n\t(via", 0, at)
    block = text[start:at]
    old_at = block[block.index("(at ") : block.index(")", block.index("(at ")) + 1]
    moved = text[:start] + block.replace(old_at, "(at 8.3 7.45)", 1) + text[at:]
    assert moved != text
    gate = _thermal_gate(moved, roles_doc(moved), design)
    assert gate["fails"] and "adrift" in gate["fails"][0], gate


@pytest.mark.kicad
def test_locking_a_piece_a_pattern_writes_again_puts_it_on_the_board_once(tmp_path: Path):
    """Finding 5: locking the LED hop pcbc's patterns write put that segment on the board twice (the
    relax step re-emitted it), and the build called it verified."""
    from pcbc.build import build_job
    from pcbc.gen import signature

    board, _design = _blinky(tmp_path)
    first = build_job(board, upto="route", force=True)
    assert first.get("error") is None, first
    line = next(x for x in Path(first["gen"]).read_text().splitlines() if x.startswith('Seg("LED", (10.1951, 13.2551), (30.5524, 13.2551)'))
    (tmp_path / "layout.core.py").write_text(line + "\n")
    again = build_job(board, upto="route", force=True)
    assert again.get("error") is None, again
    back = decompile((tmp_path / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_text())
    sigs = Counter(signature(c) for c in back.copper)
    assert max(sigs.values()) == 1 and sum(1 for c in back.copper if c.kind == "seg") == 4


@pytest.mark.kicad
def test_a_lock_renumbers_nothing_else(tmp_path: Path):
    """Finding 16: the router's uuids were positional, so one lock swapped the uuids and ids of most
    other objects. They are derived from geometry now: every object that did not move keeps both."""
    from pcbc.build import build_job
    from pcbc.gen import signature

    board = _buck(tmp_path)
    first = build_job(board, upto="route", force=True)
    assert first.get("error") is None, first

    def ids(gen: str) -> dict:
        d = Design()
        load_layout(Path(gen), d, source="gen")
        return {repr(signature(c)): (c.uuid, c.id) for c in d.copper}

    before = ids(first["gen"])
    line = next(x for x in Path(first["gen"]).read_text().splitlines() if x.startswith('Via("GND"'))
    (board.parent / "layout.core.py").write_text(line + "\n")
    again = build_job(board, upto="route", force=True)
    assert again.get("error") is None, again
    after = ids(again["gen"])
    same = set(before) & set(after)
    # The locked via is a core object now (not in gen); every other object is where it was, with the
    # uuid and id it had: the line was adopted (pass 1 reproduced it), so the route did not move.
    assert len(same) == len(before) - 1 and [k for k in same if before[k] != after[k]] == []


@pytest.mark.kicad
def test_a_refused_core_leaves_nothing_behind_and_an_edit_to_core_is_rebuilt(tmp_path: Path):
    """Finding 9: a refused core left the last build's gen, board and Gerbers, and a plain build then
    reported them done; an edit to `layout.core.py` was never seen without `--force`."""
    from pcbc.build import build_job

    board, _design = _blinky(tmp_path)
    layout = tmp_path / "layout" / "blinky"
    assert build_job(board, upto="fab", force=True).get("ok")
    (tmp_path / "layout.core.py").write_text('Place("R1", at=(10, 10))\n')
    refused = build_job(board, upto="fab", force=True)
    assert refused["error"] and "layout.core.py:1" in refused["error"]
    assert not (layout / "routed" / "layout.kicad_pcb").exists() and not (layout / "routed" / "layout.gen.py").exists() and not (layout / "fab").exists()
    assert build_job(board, upto="fab").get("ok") is None, "a plain build after a refusal is not 'done'"
    gen_line = 'Seg("LED", (10.1951, 13.2551), (30.5524, 13.2551), width=0.16, layer="F.Cu", id="hop")\n'
    (tmp_path / "layout.core.py").write_text(gen_line)
    again = build_job(board, upto="fab")
    assert again.get("ok") and "route" in again["plan"], again.get("error")
    assert next(s for s in again["steps"] if s["stage"] == "route")["core_objects"] == 1
    assert build_job(board, upto="fab") == {**build_job(board, upto="fab"), "skipped": "fab"}


@pytest.mark.kicad
def test_a_core_via_too_close_to_another_net_is_refused_before_routing(tmp_path: Path):
    """Finding 17 (a): a via 0.05 mm from a GND pad passed the overlap-only check, routed the whole board
    and failed KiCad DRC naming no line. It is refused before routing, with the air it has and needs."""
    from pcbc.build import build_job

    board = _buck(tmp_path)
    (board.parent / "layout.core.py").write_text('Via("VIN", (21.72, 9.46), size=0.8, drill=0.4, id="vin-near-gnd")\n')
    result = build_job(board, upto="route", force=True)
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert "layout.core.py:1: via on VIN against C_IN2.2" in (result["error"] or "") and "0.05 mm of air where 0.2 mm is needed" in result["error"], result["error"]
    assert "links" not in route and "gen" not in route, "refused before the router ran"


@pytest.mark.kicad
def test_a_drc_failure_on_a_core_object_names_its_line(tmp_path: Path):
    """Finding 17 (b): KiCad reports the uuid of the item it faults, and the emitted board carries each
    core object's own uuid, so the build names the line."""
    from pcbc.build import build_job

    board = _buck(tmp_path)
    (board.parent / "layout.core.py").write_text('\n\nVia("FB", (5.0, 17.0), size=0.8, drill=0.4, id="fb-via")\n')
    result = build_job(board, upto="route", force=True)
    assert result["error"] and "layout.core.py:3:" in result["error"], result["error"]


@pytest.mark.kicad
def test_a_net_a_core_rule_area_cuts_off_is_traced_to_that_line(tmp_path: Path):
    """Finding 17 (c): a core rule area across a net's path left the router's message pointing at the
    parts ("move them closer together"). A core rule area is an obstacle to the native router and to
    the patterns; on blinky a full-height wall leaves LED with no path, and the move names the core
    line first among what the failed search hit."""
    from pcbc.build import build_job

    board, _design = _blinky(tmp_path)
    ko = '{"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed", "copperpour": "not_allowed", "footprints": "allowed"}'
    (tmp_path / "layout.core.py").write_text(f'Pour(None, layer=None, layers=("F.Cu", "B.Cu"), name="wall", keepout={ko}, points=[(19.5, 0.3), (20.0, 0.3), (20.0, 24.7), (19.5, 24.7)], id="wall")\n')
    result = build_job(board, upto="route", force=True)
    err = result["error"] or ""
    assert err.startswith("unrouted: LED") and "In the way: layout.core.py:1 (rule area 'wall')" in err, err
    assert "canary" not in err, "the canary net is the one left open: its silence is not a rule-file fault"


@pytest.mark.kicad
def test_the_outline_is_a_python_object_and_the_emitted_board_s_only_drawings_are_python(tmp_path: Path):
    """Finding 11: the outline `gr_rect` was copied from the placed board, not emitted from Python. It
    is a `Rect` line of `layout.gen.py`, and the base the emitter starts from (the native placer's
    in-memory footprint board) carries no layout primitive at all."""
    from pcbc.layout_prims import HEADS
    from pcbc.place_native import place

    final = routed_board("blinky", EXAMPLES / "blinky" / "blinky.py")
    gen = (final.parent / "layout.gen.py").read_text()
    rect = next(ln for ln in gen.splitlines() if ln.startswith("Rect("))
    uid = rect.split('uuid="')[1].split('"')[0]
    assert 'layer="Edge.Cuts"' in rect and final.read_text().count(f'(uuid "{uid}")') == 1
    base = place(load_board(EXAMPLES / "blinky" / "blinky.py"), name="blinky").text
    left = {n[0] for n in parse_tree(base)[1:] if isinstance(n, list)}
    assert not (left & (set(HEADS) | {"segment", "arc", "via", "zone"})), left


@pytest.mark.kicad
def test_every_drawing_kind_written_by_hand_in_core_survives_kicad_s_save(tmp_path: Path):
    """Finding 14's other half: a hand-written core drawing of every kind — its left-out fields filled
    before emit with what KiCad writes for a new one — goes through the build with KiCad keeping every
    field as written (the build refuses otherwise), the emitted board equal to the Python, and the
    copper verified. What KiCad itself recomputes (a dimension's text, a generator's `last_*`) is
    documented in docs/layout-properties.md and compared as KiCad's function of the authored fields."""
    from pcbc.build import build_job

    board, _design = _blinky(tmp_path)
    png = base64.b64encode(_png_1x1()).decode()
    (tmp_path / "layout.core.py").write_text(
        "\n".join(
            [
                'Rect((3, 2), (1, 1), layer="F.SilkS", radius=0.4, id="box")',
                'Circle((5, 5), 1, layer="F.SilkS", fill="hatch", id="dot")',
                'DrawArc((6, 20), (7, 21), (8, 20), layer="F.SilkS", id="bite")',
                'Poly([(10, 20), (11, 20), (11, 21)], layer="F.Fab", id="mark")',
                'Curve([(12, 20), (13, 22), (14, 22), (15, 20)], id="bend")',
                'TextBox("note", (16, 19), (22, 22), id="note")',
                f'Image((25, 3), "{png}", id="pic")',
                'Table((26, 16), [["A", "B"], ["1", "2"]], col_mm=4.0, id="bom")',
                'Barcode("PCBC", (5, 10), kind="qr", size=3, id="code")',
                'Target((33, 21), shape="x", size=2, id="fid")',
                'Point((2, 16), size=1, id="pt")',
                # A GND track inside blinky's B.Cu GND pour: both ends land in the fill, so it is
                # joined and not dangling. Until the fourth review this was the verbatim copy of the
                # hop's first LED segment; since C4 a verbatim copy **is** the hop's piece (adopted
                # with its `pcbc:hop:D1.2` group), and a KiCad item sits in one group, so a core
                # `Generated` over it is refused naming both (`test_a_core_group_over_a_pattern_s_piece_
                # is_refused_naming_both_groups`). This test is about drawings surviving KiCad's save,
                # so the generator's member is copper no pattern owns.
                'Seg("GND", (3, 5), (6, 5), width=0.25, layer="B.Cu", id="s1")',
                'Generated("tuning_pattern", "tune", ["s1"], layer="B.Cu", id="tune")',
                'Group("pair", ["box", "dot"], id="gp")',
                *(f'Dimension((1, 24), (39, 23), kind="{k}", layer="Dwgs.User", id="dim-{k}")' for k in ("aligned", "orthogonal", "radial", "leader", "center")),
                'Text("REV A", (20, 3), layer="F.SilkS", size=1.0, id="rev")',
                'Line((2, 23), (8, 23), layer="F.SilkS", width=0.15, id="bar")',
                "",
            ]
        )
    )
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result.get("error")
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert route["copper"] == "verified" and route["core_objects"] == 21


def _png_1x1() -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


# ------------------------------------------------------------------------------------ the fixes of the second review
#
# Each test below reproduces one defect the second adversarial round found (M1-M7, the minors) and
# pins the fix. The harnesses that found them are under the session's scratch `step1b/refute-*-r2`.


def _seg_block(text: str, a: tuple, b: tuple) -> tuple[int, int]:
    """The span of the one top-level `(segment)` from `a` to `b` (either way round)."""
    import re

    from pcbc.sexp import matching_paren

    def _blocks(text: str):
        out, pos = [], 0
        while True:
            j = text.find("\n\t(", pos)
            if j < 0:
                return out
            start = j + 2
            end = matching_paren(text, start) + 1
            out.append((start, end, text[start + 1 : start + 41].split(None, 1)[0].rstrip(")")))
            pos = end

    def close(p, q):
        return p is not None and abs(p[0] - q[0]) < 2e-3 and abs(p[1] - q[1]) < 2e-3

    def num(block, tok):
        m = re.search(r"\(" + tok + r"\s+([-0-9.]+)\s+([-0-9.]+)", block)
        return (float(m.group(1)), float(m.group(2))) if m else None

    hits = [(s, e) for s, e, h in _blocks(text) if h == "segment" and ((close(num(text[s:e], "start"), a) and close(num(text[s:e], "end"), b)) or (close(num(text[s:e], "start"), b) and close(num(text[s:e], "end"), a)))]
    assert len(hits) == 1, (a, b, len(hits))
    return hits[0]


@pytest.mark.kicad
def test_fab_judges_the_copper_of_the_board_it_fabricates(tmp_path: Path):
    """M1: fab's hard ampacity gate read the router's sidecar (`copper.json`), so buck's 5V spine
    narrowed to 0.2 mm on the shipped board passed as 0.781 mm and the Gerbers carried the 0.2 mm
    aperture. The gate reads the widths off the board's own `pcbc:` groups now, and the sidecar is
    is gone: the native route stage never writes one."""
    import re

    from pcbc.compile import compile_design
    from pcbc.fab import fab_job

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    assert not list(final.parent.glob("copper*.json")), "the native route stage writes no sidecar"
    work = tmp_path / "routed"
    shutil.copytree(final.parent, work)
    text = (work / "layout.kicad_pcb").read_text()
    gen = Design()
    load_layout(work / "layout.gen.py", gen, source="gen")
    by_id = {c.id: c for c in gen.copper}
    spine = next(by_id[m] for g in gen.graphics if g.kind == "group" and g.f["name"].startswith("pcbc:spine:") for m in g.f["members"] if by_id[m].net == "5V" and by_id[m].kind == "seg")
    s, e = _seg_block(text, spine.a, spine.b)  # pcbc's 5V spine, class 0.781
    narrowed = text[:s] + re.sub(r"\(width\s+[0-9.]+\)", "(width 0.2)", text[s:e], count=1) + text[e:]
    assert narrowed != text
    (work / "layout.kicad_pcb").write_text(narrowed)
    design = load_board(EXAMPLES / "buck" / "buck.py")
    result = fab_job(compile_design(design), work / "layout.kicad_pcb", out_dir=tmp_path / "fab", design=design)
    assert result["error"] and "5V spine copper 0.2 mm < the 0.781 mm 5V class" in result["error"], result["error"]
    assert not list((tmp_path / "fab" / "gerbers").glob("*.g*")), "no Gerber of a board the gate refused"


def test_core_copper_names_a_net_of_board_py(tmp_path: Path):
    """M2: `Seg("PHANTOM", ...)` and `Seg(None, ...)` (which became `(net "None")`) both shipped
    verified. Core copper names a net of board.py, or no net: `net=None` is KiCad's own no-net
    (`(net "")` on a track or via, no token on a zone; `docs/direction.md` §2), never the string."""
    from pcbc.layout_emit import render
    from pcbc.layout_job import load_core
    from pcbc.netcheck import primitive_nets

    for body, says in (
        ('Seg("PHANTOM", (15.0, 5.0), (20.0, 5.0), width=0.2, layer="F.Cu")\n', "'PHANTOM', which is not a net of board.py (GND, LED, VCC)"),
        ('Via("VDD", (15.0, 5.0), size=0.6, drill=0.3)\n', "'VDD', which is not a net of board.py"),
        ('Pour("VDD", layer="B.Cu", points=[(1, 1), (5, 1), (5, 5), (1, 5)])\n', "'VDD', which is not a net of board.py"),
        ('Line((1, 1), (5, 1), layer="F.Cu", net="NOPE")\n', "net 'NOPE', which is not a net of board.py"),
    ):
        board, design = _blinky(tmp_path)
        (tmp_path / "layout.core.py").write_text(body)
        with pytest.raises(ValueError) as got:
            load_core(design, board)
        assert "layout.core.py:1" in str(got.value) and says in str(got.value), str(got.value)
    board, design = _blinky(tmp_path)
    (tmp_path / "layout.core.py").write_text(
        'Pour(None, layer="F.Cu", keepout={"tracks": "not_allowed"}, points=[(1, 1), (5, 1), (5, 5), (1, 5)])\n'
        'Seg(None, (15.0, 5.0), (20.0, 5.0), width=0.2, layer="F.Cu", id="spreader")\n'
        'Via(None, (15.0, 5.0), size=0.6, drill=0.3, id="coupon")\n'
    )
    copper, _gr = load_core(design, board)
    assert [c.net for c in copper] == [None, "", ""], "no net: a zone has no token, a track or via KiCad's (net \"\")"
    text = render(design, copper, board="blinky")
    assert text.count('(net "")') == 2 and '"None"' not in text and text.count("(net") == 2
    back = decompile("(kicad_pcb\n" + text + ")\n")
    assert [c.net for c in back.copper] == [None, "", ""]
    # The netlist check reads every primitive's net token, not only the pads' bindings.
    text = '(kicad_pcb\n\t(segment\n\t\t(start 1 1)\n\t\t(end 2 2)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "PHANTOM")\n\t\t(uuid "x")\n\t)\n\t(via\n\t\t(at 1 1)\n\t\t(size 0.5)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "GND")\n\t\t(uuid "y")\n\t)\n)\n'
    assert primitive_nets(text) == {"PHANTOM", "GND"}


@pytest.mark.kicad
def test_the_copper_gate_refuses_a_net_board_py_does_not_have(tmp_path: Path):
    """M2's other half: KiCad's unconnected check compares pad bindings, so a track on a net no pad
    has is invisible to it; `check_copper` fails on it."""
    from pcbc.netcheck import check_copper

    final = routed_board("blinky", EXAMPLES / "blinky" / "blinky.py")
    work = tmp_path / "routed"
    shutil.copytree(final.parent, work)
    text = (work / "layout.kicad_pcb").read_text()
    phantom = '\t(segment\n\t\t(start 15 5)\n\t\t(end 20 5)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "PHANTOM")\n\t\t(uuid "aaaaaaaa-0000-4000-8000-000000000001")\n\t)\n'
    (work / "layout.kicad_pcb").write_text(text.rstrip()[:-1].rstrip() + "\n" + phantom + ")\n")
    gate = check_copper(load_board(EXAMPLES / "blinky" / "blinky.py"), work / "layout.kicad_pcb", refill=False)
    assert not gate["ok"] and any("PHANTOM" in f for f in gate["fails"]), gate["fails"]


def test_a_zone_s_hatch_offset_and_an_arc_in_a_polygon_have_python_fields(tmp_path: Path):
    """M4: two KiCad 10.0.6 fields no Python field carried — a zone's per-layer `(property (layer)
    (hatch_position (xy)))` and an `(arc (start) (mid) (end))` entry inside a `(pts ...)` (a zone
    outline or hole, a `gr_poly`). Probe 4 (`make_kicad10_primitives4.py`, saved by KiCad) carries
    both and runs through every census and round-trip test above; here a hand-written core line."""
    from pcbc.layout_prims import flat_points, is_arc

    probe = FIX / "kicad10_primitives4.kicad_pcb"
    assert probe in PROBES and probe.read_text().count("(hatch_position") == 2 and probe.read_text().count("(arc") == 3
    design, text = _core_blinky(
        tmp_path,
        'Pour("GND", layer="B.Cu", points=[(1, 1), ((10, 1), (12, 5), (10, 9)), (1, 9)], holes=[[(3, 3), ((5, 3), (6, 4), (5, 5)), (3, 5)]], hatch_position={"B.Cu": (0.3, 0.4)}, fill_mode="hatch", hatch_thickness=0.3, hatch_gap=0.4, hatch_orientation=0.0, id="hatched")\n'
        'Poly([(20, 1), ((25, 1), (26, 3), (25, 5)), (20, 5)], layer="F.SilkS", id="bite")\n',
    )
    pour = design.copper[0]
    assert is_arc(pour.points[1]) and pour.hatch_position == {"B.Cu": (0.3, 0.4)} and flat_points(pour.points)[1:4] == ((10.0, 1.0), (12.0, 5.0), (10.0, 9.0))
    assert '(property (layer "B.Cu") (hatch_position (xy 0.3 0.4)))' in text and "(arc (start 10 1) (mid 12 5) (end 10 9))" in text and "(arc (start 5 3) (mid 6 4) (end 5 5))" in text
    assert "(arc (start 25 1) (mid 26 3) (end 25 5))" in text
    back = decompile("(kicad_pcb\n" + text + ")\n")
    assert back.copper[0].hatch_position == {"B.Cu": (0.3, 0.4)} and back.copper[0].points == pour.points and back.copper[0].holes == pour.holes
    # A vertex written before the arc that is the arc's own start is one KiCad's load drops (third
    # review R1, measured): the emitter writes the outline as KiCad will hold it.
    from pcbc.layout_emit import normalise_outline

    assert normalise_outline([(1, 1), (10, 1), ((10, 1), (12, 5), (10, 9)), (1, 9), (1, 1)]) == pour.points
    assert back.graphics[0].f["pts"] == design.graphics[0].f["pts"]
    assert "hatch_position" in [kw for kw, _a, _t in __import__("pcbc.gen", fromlist=["FIELDS"]).FIELDS["Pour"]]
    for path in ("property", "property/layer", "property/hatch_position", "property/hatch_position/xy", "polygon/pts/arc", "polygon/pts/arc/start", "polygon/pts/arc/mid", "polygon/pts/arc/end"):
        assert path in CENSUS["zone"], path
    for path in ("pts/arc", "pts/arc/start", "pts/arc/mid", "pts/arc/end"):
        assert path in CENSUS["gr_poly"], path


def _core_blinky(tmp_path: Path, body: str):
    from pcbc.layout_emit import render, render_graphics, uuid_of

    board, design = _blinky(tmp_path)
    (tmp_path / "layout.core.py").write_text(body)
    load_layout(tmp_path / "layout.core.py", design, source="core")
    ids = {o.id: uuid_of(o, "blinky") for o in [*design.copper, *design.graphics]}
    return design, render(design, design.copper, board="blinky") + render_graphics(design.graphics, board="blinky", uuids=ids)


@pytest.mark.kicad
def test_a_locked_core_pour_on_the_plane_net_is_kept_and_the_router_pours_nothing_over_it(tmp_path: Path):
    """M5: the old router's pour step deleted the locked core pour and wrote its own. Natively a core
    `Pour` on a plane target's (net, layer) **replaces** the generated one (`layout_job`): the core pour
    is the plane, once on the board. m10: the numbers the build filled in on that hand-written line are
    in the build's own output."""
    from pcbc.build import build_job
    from pcbc.gen import signature
    from pcbc.sexp import stable_uuid

    board = _buck(tmp_path)
    (board.parent / "layout.core.py").write_text('Pour("GND", layer="B.Cu", points=[(0.3, 0.3), (39.7, 0.3), (39.7, 24.7), (20.0, 24.7), (0.3, 24.7)], id="gndplane")\n')
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result.get("error")
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert route["copper"] == "verified" and route["core_objects"] == 1
    final = decompile((board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb").read_text())
    pours = [c for c in final.copper if c.kind == "pour" and c.net == "GND"]
    assert len(pours) == 1 and pours[0].uuid == stable_uuid("buck", "layout", "gndplane") and len(pours[0].points) == 5
    assert max(Counter(signature(c) for c in final.copper).values()) == 1
    filled = route["core_filled"]["layout.core.py:1"]
    from pcbc.compile import compile_design
    from pcbc.stackup import get_stackup

    job = compile_design(load_board(board))
    assert filled["clearance"] == job.constraints.by_net("GND").clearance_mm.value and filled["min_thickness"] == get_stackup(job.stackup).track_min, filled
    assert filled["filled"] is True and filled["thermal_gap"] == 0.5 and "points" not in filled, filled


@pytest.mark.kicad
@pytest.mark.parametrize("name", ["blinky", "buck", "c3_usb"])
def test_every_line_of_layout_gen_can_be_locked_verbatim(name: str, tmp_path: Path):
    """Findings 2 and 15 of the first review, and M5 of the second: the gen header says copy a line
    verbatim to lock it. The first round held every copper line to the pre-route core checks only;
    the second found that locking all of them was refused by the build ("the router dropped or moved
    locked core copper": KRT's pour step deleted the locked pour). A **full build** with every copper
    line of `layout.gen.py` in core: verified, every core object once on the board, nothing
    duplicated beyond what the baseline board already carried (c3_usb's router writes one pair
    segment twice), and KiCad's DRC the same by type as the baseline board's."""
    import re

    from pcbc.build import build_job
    from pcbc.gen import signature

    final, base_result = routed_result(name, EXAMPLES / name / f"{name}.py")
    base = decompile(final.read_text())
    cu = [ln for ln in (final.parent / "layout.gen.py").read_text().splitlines() if re.match(r"(Seg|Arc|Via|Pour)\(", ln)]
    shutil.copytree(EXAMPLES / name, tmp_path / name, ignore=shutil.ignore_patterns("layout"))
    board = tmp_path / name / f"{name}.py"
    (board.parent / "layout.core.py").write_text("\n".join(cu) + "\n")
    # c3_usb does not finish under N0 (VBUS at the USB-C connector): it is built to the route stage and
    # must stop on the **same** unrouted nets as its baseline, with every other property below intact.
    # It is here because it is the one board with a `Thermal()` (the D2 property below); the refuters
    # found the test had been narrowed to blinky/buck, where D2 cannot happen.
    unfinished = bool(base_result.get("unrouted"))
    result = build_job(board, upto="route" if unfinished else "fab", force=True)
    route = next(s for s in result["steps"] if s["stage"] == "route")
    if unfinished:
        assert str(result.get("error") or "").startswith("unrouted: "), result.get("error")
        assert result.get("unrouted") == base_result["unrouted"], (result.get("unrouted"), base_result["unrouted"])
        assert route["copper"] == f"not verified: {len(base_result['unrouted'])} net(s) unrouted", route["copper"]
    else:
        assert result.get("error") is None, result.get("error")
        assert route["copper"] == "verified"
    assert route["core_objects"] == len(cu) >= {"blinky": 5, "buck": 50, "c3_usb": 150}[name]
    got = decompile((board.parent / "layout" / name / "routed" / "layout.kicad_pcb").read_text())
    dup_base = {s for s, n in Counter(signature(c) for c in base.copper).items() if n > 1}
    dup_got = {s for s, n in Counter(signature(c) for c in got.copper).items() if n > 1}
    assert dup_got <= dup_base, dup_got - dup_base
    # The native route stage deduplicates same-net copper before it becomes objects, so the baseline
    # carries no exact duplicate at all (`LOCK_RESIDUAL`).
    assert len(dup_base) == LOCK_RESIDUAL[name], (name, dup_base)
    assert {signature(c) for c in base.copper} <= {signature(c) for c in got.copper}
    # D2 (third review): locking every line added a second nine-via thermal array on c3_usb (68 drills
    # for 59) because the pattern placed its array without seeing the core vias already in the land.
    # The vias and the drill file are the baseline's. What is **not** the baseline's is pinned and
    # must fall, not hidden: with every path locked, the pour step's own pad-centre weld stubs
    # (route_planes' "#107" corridors) survive, because the relaxer, which absorbs them into the
    # redrawn paths on a build that owns its copper, cannot redraw across locked copper. Every one is
    # a segment under 1 mm on the plane net or beside a pour via, a KiCad warning at most.
    assert sum(1 for c in got.copper if c.kind == "via") == sum(1 for c in base.copper if c.kind == "via")
    if name == "c3_usb":
        # D2 itself: the thermal land under the ESP32's exposed pad keeps one array, not two.
        land = [c for c in got.copper if c.kind == "via" and c.net == "GND"]
        assert len(land) == len([c for c in base.copper if c.kind == "via" and c.net == "GND"]), len(land)
    drills = lambda d: sum(1 for ln in (d / "layout-PTH.drl").read_text().splitlines() if ln.startswith("X"))  # noqa: E731
    # The drill file against the baseline's own holes: every via and every plated pad hole of the
    # baseline board is one hit in the locked build's PTH file (sixth review: this line was
    # `assert (A == B) if <no fab dir> else True`, which is `assert True`). Only a finished board has
    # a fab package.
    pth = sum(1 for c in base.copper if c.kind == "via") + len(re.findall(r'\(pad "[^"]*" thru_hole ', final.read_text()))
    if not unfinished:
        assert drills(board.parent / "layout" / name / "fab") == pth, (drills(board.parent / "layout" / name / "fab"), pth)
    extra = list((Counter(signature(c) for c in got.copper) - Counter(signature(c) for c in base.copper)).elements())
    assert len(extra) == EXTRA_WELD_STUBS[name], (len(extra), extra)
    assert all(s[0] == "seg" and ((s[3][0][0] - s[3][1][0]) ** 2 + (s[3][0][1] - s[3][1][1]) ** 2) ** 0.5 < 1.0 for s in extra), extra
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, b = _drc_types(final, tmp_path / "a"), _drc_types(board.parent / "layout" / name / "routed" / "layout.kicad_pcb", tmp_path / "b")
    assert {k: v for k, v in a.items() if not k.startswith("warning:")} == {k: v for k, v in b.items() if not k.startswith("warning:")}, (a, b)
    if EXTRA_WELD_STUBS[name] == 0:
        assert a == b


LOCK_RESIDUAL = {"blinky": 0, "buck": 0, "c3_usb": 0}
"""Exact-duplicate copper objects KRT writes on the baseline board: c3_usb's two 0.127 mm pair stubs,
`USB_DN` (19.25, 22.44)-(19.25, 22.45) and `USB_DP` (19.7, 22.4)-(19.75, 22.45), each written twice
under two uuids (the same on every c3_usb build of 2026-09-24: `scratchpad/step1b/fix5/{A,T1,T2}` and
the fourth review's own baseline). Re-recorded 1 -> 2: the earlier docstring named only the `USB_DN`
one, while its own log (`after/L_c3_usb_each.log`) shows four lines differing, two per duplicate.
Measured with one build per copper line of `layout.gen.py` locked alone (`lockrun.py`, `after/L_*_each.log`):
every one of buck's 84 and blinky's 9 lines is the baseline board object for object, uuid for uuid and
DRC for DRC, and of c3_usb's 362 all but the **four** lines that are these duplicates' copies, which
each ship their duplicate once (normalise replaces every copy of a core object with the one core line;
`base_only=1`, one warning fewer). The residual is KRT's writing and is pinned by its cause in the test
above; the build does not deduplicate KRT's copper (docs/layout-properties.md, "Lock idempotence")."""

EXTRA_WELD_STUBS = {"blinky": 0, "buck": 0, "c3_usb": 0}
"""Copper an every-line-verbatim lock adds beyond the baseline board's, per board. The third review
measured 1 and 5 (the pour step's pad-centre weld stubs, `scratchpad/step1b/fix3/after/D2_allcu_*.log`),
a ledger that had to fall; the fourth review's adoption by reproduction (`layout_job`, C4) routes the
first pass with nothing locked, finds every line reproduced, and adopts it — so the board **is** the
baseline's, object for object, and the ledger is 0 (measured 2026-09-24,
`scratchpad/step1b/fix5/after/L_*_all.log`, re-run on this tree as `fix5/after2/L_*_all.log`)."""


@pytest.mark.kicad
def test_a_board_py_edit_after_a_refused_route_is_placed_again_and_build_output_is_never_trusted(tmp_path: Path):
    """M6: after any refused route a later edit to board.py was ignored — place was skipped, the new
    hash stamped onto the stale placed board, and every plain rebuild failed `U1 moved` until
    `--force`, which is exactly the path the `Place()` refusal tells an author to take. The place
    stage records what it consumed (`placed/inputs.json`); a plain build after the edit places
    again (the native route stage places again, in memory, whenever it runs). m3/m4: the route stamp
    holds what it wrote too, so a hand-edited or deleted `layout.gen.py`, or a routed board changed by
    hand, is stale from place and never fabbed."""
    from pcbc.build import build_job, stale_reason

    board, _design = _blinky(tmp_path)
    layout = tmp_path / "layout" / "blinky"
    first = build_job(board, upto="fab", force=True)
    assert first.get("ok"), first.get("error")
    assert (layout / "inputs.json").exists()

    def r1_x() -> float:
        placed = (layout / "routed" / "layout.kicad_pcb").read_text()
        at = placed.index('(property "Reference" "R1"')
        block = placed[placed.rindex("(footprint", 0, at) : at]
        return float(block[block.index("(at ") + 4 :].split()[0])

    before = r1_x()
    (tmp_path / "layout.core.py").write_text('Place("R1", at=(10, 12.5))\n')
    refused = build_job(board, upto="fab")
    assert refused["error"] and "layout.core.py:1" in refused["error"]
    (tmp_path / "layout.core.py").unlink()
    src = board.read_text()
    board.write_text(src.replace("left=8,", "left=10,", 1))
    assert board.read_text() != src
    assert stale_reason(layout, board) == (None, "board.py, a module it imports or a part file changed since the schematic was drawn")  # the schematic's record answers first
    again = build_job(board, upto="fab")
    assert again.get("ok") and again["plan"][0] == "check" and "place" in again["plan"], (again.get("error"), again["plan"])
    assert abs(r1_x() - (before + 2.0)) < 1e-6, (before, r1_x())
    assert build_job(board, upto="fab") == {**build_job(board, upto="fab"), "skipped": "fab"}
    # m3: gen is build output; an edit or a deletion is stale from place, never "done"
    gen = layout / "routed" / "layout.gen.py"
    text = gen.read_text()
    gen.write_text(text + "# edited by hand\n")
    assert stale_reason(layout, board)[0] == "place" and "layout.gen.py" in stale_reason(layout, board)[1]
    gen.unlink()
    assert stale_reason(layout, board)[0] == "place"
    rebuilt = build_job(board, upto="fab")
    assert rebuilt.get("ok") and rebuilt["plan"] == ["route", "fab"] and gen.read_text() == text, rebuilt.get("error")
    # m4: a routed board changed by hand is re-judged (rerouted), never exported as it is
    routed = layout / "routed" / "layout.kicad_pcb"
    routed.write_text(routed.read_text().replace("(width 0.16)", "(width 0.15)", 1))
    assert stale_reason(layout, board)[0] == "place" and "routed/layout.kicad_pcb" in stale_reason(layout, board)[1]
    assert build_job(board, upto="fab")["plan"] == ["route", "fab"]


@pytest.mark.kicad
def test_a_changed_part_file_makes_the_placed_board_stale(tmp_path: Path):
    """m2: `inputs.json` hashed board.py and core only; a changed `components/` part file gave a stale
    BOM as ok. Every file the board load reads is hashed."""
    from pcbc.build import build_job, stale_reason

    board = _buck(tmp_path)
    layout = board.parent / "layout" / "buck"
    result = build_job(board, upto="place", force=True)
    assert result.get("ok"), result.get("error")
    assert stale_reason(layout, board) == ("place", None)
    part = board.parent / "components" / "TI" / "TPS54202DDCR" / "part.py"
    part.write_text(part.read_text() + "\n# a changed part file\n")
    stage, why = stale_reason(layout, board)
    # The schematic's record is asked first (it is the earliest stage with an output), and it hashes
    # the same part files; the placed board's own record would say the same one line later.
    assert stage is None and why == "board.py, a module it imports or a part file changed since the schematic was drawn", (stage, why)


@pytest.mark.kicad
def test_a_dangling_core_via_is_a_finding_naming_its_line(tmp_path: Path):
    """M7: after a pour lock the locked GND via was left dangling; KiCad reported `via_dangling` at its
    uuid and the build said verified, naming no line. Every DRC item on a core object names its line;
    a dangling core via or track is a finding with a move. (The second round's case, buck's own GND
    via locked with the pour, builds verified since D1 — it is the baseline's copper; this via is a
    hand-written one in an empty corner, welded to the pour on B.Cu and to nothing on F.Cu.)

    Since the third refutation round the via's free layer is a routing terminal (`route_native.EndTerminal`):
    the router tries to join its F.Cu and finds no path past FID3's mask keepout. That is the lock's
    finding, not an unfinished GND — the via reaches no pad, so GND's pads are all joined and its copper
    stays — and the refusal quotes the router's note and names the moves."""
    from pcbc.build import build_job

    board = _buck(tmp_path)
    (board.parent / "layout.core.py").write_text('Seg("VIN", (18.85, 11.25), (18.85, 9.45), layer="F.Cu", id="myvin")\nVia("GND", (2.5, 22.5), size=0.5, drill=0.3, id="lonely")\n')
    result = build_job(board, upto="route", force=True)
    err = result["error"] or ""
    assert not result.get("unrouted"), result.get("unrouted")
    assert "layout.core.py:2: via_dangling" in err and "the router could not join this core via's free end" in err, err
    assert "move that end onto the copper it should join, or unlock the line (delete it)" in err and "pcbc bug" not in err, err
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert route["copper"] != "verified" and not (board.parent / "layout" / "buck" / "fab").exists()


@pytest.mark.kicad
def test_a_tap_via_must_be_joined_to_its_owner_pad_by_a_track(tmp_path: Path):
    """m6: `_plane_gate` passed a tap via moved 12 mm from its owner pad (still inside the plane, and
    the pad welded through other copper). A `pcbc:tap:<REF.PAD>` via must be joined to that pad by
    tracks and vias alone — the stub the tap pattern wrote."""
    from pcbc.build import _plane_gate, tap_stubs
    from pcbc.layout_job import roles_doc

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    text = final.read_text()
    design = load_board(EXAMPLES / "buck" / "buck.py")
    doc = roles_doc(text)
    assert tap_stubs(text, doc, design) == [] and _plane_gate(text, doc, design)["fails"] == []
    tap = next(i for i in doc.items if i["reason"] == "tap" and i["owner"] == "U1.1" and i["key"][0] == "via")
    at = text.index(f'(uuid "{tap["uuid"]}")')
    start = text.rindex("\n\t(via", 0, at)
    block = text[start:at]
    old_at = block[block.index("(at ") : block.index(")", block.index("(at ")) + 1]
    moved = text[:start] + block.replace(old_at, "(at 10 22)", 1) + text[at:]
    fails = tap_stubs(moved, roles_doc(moved), design)
    assert len(fails) == 1 and "not joined to U1.1 by any track" in fails[0], fails
    assert _plane_gate(moved, roles_doc(moved), design)["fails"] == fails


def test_a_pour_joins_a_pad_only_as_its_connect_mode_allows(tmp_path: Path):
    """m7: the connectivity note joined pads through `connect="none"` / `thru_hole_only` pours."""
    from pcbc.layout_check import check_layout
    from pcbc.pads import PadGeom
    from pcbc.route_geom import via_shape

    def pad(ref, at, kind="smd"):
        return PadGeom(ref, "1", "GND", kind, "rect", at, 0.0, (via_shape(at, 1.0),), None, frozenset({"B.Cu"}), frozenset(), 0.0)

    pads = [pad("A", (2.0, 2.0)), pad("B", (8.0, 8.0))]
    for connect, pieces in (("solid", 1), ("thermal", 1), ("none", 2), ("thru_hole_only", 2)):
        board, design = _blinky(tmp_path)
        (tmp_path / "layout.core.py").write_text(f'Pour("GND", layer="B.Cu", connect="{connect}", points=[(0, 0), (10, 0), (10, 10), (0, 10)], id="p")\n')
        load_layout(tmp_path / "layout.core.py", design, source="core")
        _fails, notes = check_layout(design, pads, layers=("F.Cu", "B.Cu"))
        assert (pieces == 1) == (not any("GND: its pads are in 2 pieces" in n for n in notes)), (connect, notes)
    board, design = _blinky(tmp_path)
    (tmp_path / "layout.core.py").write_text('Pour("GND", layer="B.Cu", connect="thru_hole_only", points=[(0, 0), (10, 0), (10, 10), (0, 10)], id="p")\n')
    load_layout(tmp_path / "layout.core.py", design, source="core")
    _fails, notes = check_layout(design, [pad("A", (2.0, 2.0), "thru_hole"), pad("B", (8.0, 8.0), "thru_hole")], layers=("F.Cu", "B.Cu"))
    assert not any("2 pieces" in n for n in notes), notes


def test_a_core_id_that_takes_a_gen_id_is_refused_not_renamed_around():
    """m8: a core id colliding with a gen id silently lengthened the gen object's id."""
    from pcbc.gen import IdTaken, assign_ids

    c = Copper("via", "", "GND", at=(1, 1), uuid="16b12fbb-ecd7-5573-b4f1-181fdf8de6df")
    with pytest.raises(IdTaken) as got:
        assign_ids([c], {"via-16b12fbb"})
    assert got.value.id == "via-16b12fbb" and got.value.uuid == c.uuid
    assign_ids([c], set())
    assert c.id == "via-16b12fbb"


@pytest.mark.kicad
def test_a_core_rule_area_over_an_unrouted_pad_names_the_pad_and_the_move(tmp_path: Path):
    """m9: an impossible lock gave no concrete move and the router's list blamed the parts. A core rule
    area is an obstacle to the native router, and when it covers a pad of the link that failed, the move
    names the line and the pad."""
    from pcbc.build import build_job
    from pcbc.layout_job import board_pads
    from pcbc.place_native import place

    board, design = _blinky(tmp_path)
    pads, _layers = board_pads(design, place(design, name="blinky").text)
    p = next(p for p in pads if p.net == "LED" and p.ref == "R1")
    x, y = p.at
    ko = '{"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed", "copperpour": "not_allowed", "footprints": "allowed"}'
    (tmp_path / "layout.core.py").write_text(f'Pour(None, layer=None, layers=("F.Cu", "B.Cu"), name="wall", keepout={ko}, points=[({x - 1}, {y - 1}), ({x + 1}, {y - 1}), ({x + 1}, {y + 1}), ({x - 1}, {y + 1})], id="wall")\n')
    result = build_job(board, upto="route", force=True)
    assert str(result["error"]).startswith("unrouted: LED"), result["error"]
    assert f"layout.core.py:1 (rule area 'wall') covers R1.{p.num}: clear it off that pad or delete it" in result["error"], result["error"]


def test_the_gen_header_says_which_lines_cannot_be_locked():
    """m11: the header invited locking any line verbatim; a `pcbc:` group line is refused. Sixth
    review: so is the Edge.Cuts outline, which the header did not name."""
    from pcbc.gen import HEADER

    assert 'Group named "pcbc:<reason>:<owner>"' in HEADER and "a Place (a part is placed in" in HEADER
    assert "Three kinds of line cannot be locked" in HEADER and "the Edge.Cuts outline (Board(width, height) in board.py)" in HEADER


@pytest.mark.kicad
def test_every_line_of_layout_gen_the_header_does_not_name_can_be_locked(tmp_path: Path):
    """Sixth review: the gen header said two kinds of line cannot be locked, and every board's
    Edge.Cuts `Rect` was refused too. Each line of blinky's gen alone in core: a line is refused at load
    only if its kind is one the header names."""
    import re

    from pcbc.gen import HEADER
    from pcbc.layout_job import load_core

    from pcbc.place_native import place

    final = routed_board("blinky", EXAMPLES / "blinky" / "blinky.py")
    placed = place(load_board(EXAMPLES / "blinky" / "blinky.py"), name="blinky").text
    lines = [ln for ln in (final.parent / "layout.gen.py").read_text().splitlines() if re.match(r"[A-Z][A-Za-z]*\(", ln)]
    named = {"Place": "a Place (a part is placed in", "pcbc group": 'Group named "pcbc:<reason>:<owner>"', "outline": "the Edge.Cuts outline"}
    refused = set()
    for ln in lines:
        (tmp_path / "layout.core.py").write_text(ln + "\n")
        board = tmp_path / "blinky.py"
        board.write_text((EXAMPLES / "blinky" / "blinky.py").read_text())
        try:
            load_core(load_board(board), board, placed, name="blinky")
        except ValueError:
            kind = "Place" if ln.startswith("Place(") else "pcbc group" if ln.startswith('Group("pcbc:') else "outline" if 'layer="Edge.Cuts"' in ln else ln
            refused.add(kind)
    assert refused == {"Place", "pcbc group", "outline"}, refused
    assert all(named[k] in HEADER for k in refused)


def test_a_core_load_may_not_add_a_pose_by_any_path(tmp_path: Path):
    """m12: `pcbc.language._gen_place` (or a `Pose` appended by hand) in core bypassed the `Place()`
    refusal and gave a part a second home."""
    for body in (
        'import pcbc.language as L\nL._gen_place("R1", at=(12.0, 12.5))\n',
        'import pcbc.language as L\nL._layout_source = "gen"\nL._gen_place("R1", at=(12.0, 12.5))\n',
        'import pcbc.language as L\nfrom pcbc.model import Pose\nL._current.poses.append(Pose("R1", (12.0, 12.5), 0.0, "F.Cu", True, source="gen"))\n',
    ):
        core, design = _layout_file(tmp_path, body)
        with pytest.raises(ValueError, match=r"a pose for 'R1' was made in layout\.core\.py.*board\.py only"):
            load_layout(core, design, source="core")
        assert design.poses == []


def test_the_outline_has_one_home(tmp_path: Path):
    """m1: core accepted Edge.Cuts drawings on top of Board(width, height); the place stage never
    saw a core cutout. Refused naming board.py (default for this round, reversible)."""
    from pcbc.layout_job import load_core

    board, design = _blinky(tmp_path)
    (tmp_path / "layout.core.py").write_text('Rect((14.0, 18.0), (18.0, 22.0), width=0.05, layer="Edge.Cuts")\n')
    with pytest.raises(ValueError, match=r"layout\.core\.py:1: gr_rect on Edge\.Cuts; the outline is Board\(width, height\) in board\.py"):
        load_core(design, board)


# --------------------------------------------------------------------- third review, round 1


def _blinky_with_helper(tmp_path: Path) -> Path:
    """blinky whose `board.py` says `from helper import LEFT`: a second source file beside it."""
    src = (EXAMPLES / "blinky" / "blinky.py").read_text()
    src = src.replace("from pcbc import Board", "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\nfrom helper import LEFT\nfrom pcbc import Board", 1)
    src = src.replace("    left=8,\n", "    left=LEFT,\n", 1)
    assert "left=LEFT" in src
    board = tmp_path / "blinky.py"
    board.write_text(src)
    (tmp_path / "helper.py").write_text("LEFT = 8\n")
    return board


def _r1_at(placed: Path) -> str:
    import re

    text = placed.read_text()
    at = text.index('(property "Reference" "R1"')
    return re.search(r"\(at ([-0-9.]+) ([-0-9.]+)( [-0-9.]+)?\)", text[text.rindex("(footprint", 0, at) : at]).group(0)


def test_the_placed_board_is_stamped_and_a_hand_edit_is_stale_from_the_place_stage(tmp_path: Path):
    """B1: a footprint moved by hand in `placed/layout.kicad_pcb` was routed and fabbed "copper:
    verified" with the hand pose on the shipped board — `placed/inputs.json` never recorded the
    placed board's own bytes and `stale_reason` named the change and then called place done. (S2):
    the place stage stamps what it wrote; a mismatch is stale **from the stage that wrote it**
    (fifth review: it was stale from check, which re-seeded and redrew the schematic for nothing), so
    place and the later stages run, never `("place", None)`. And `pcbc pcb` (no sch stage) never
    vouches for a schematic: the schematic has its own record, written by the sch stage alone."""
    import hashlib
    import json

    from pcbc.build import INPUTS, NEVER_EDITED, pcb_job, planned, sch_stage, stale_reason

    board, design = _blinky(tmp_path)
    assert not pcb_job(board).get("error")
    layout = tmp_path / "layout" / "blinky"
    placed = layout / "placed" / "layout.kicad_pcb"
    stamp = json.loads((layout / "placed" / INPUTS).read_text())
    assert stamp["placed"] == hashlib.sha256(placed.read_bytes()).hexdigest() and "modules" in stamp and "sch" not in stamp
    stage, why = stale_reason(layout, board)
    assert stage is None and why.startswith("the schematic has no record of the board.py it was drawn from"), (stage, why)
    assert planned(stage, upto="fab", force=False) == ["check", "sch", "place", "route", "fab"]
    _step, err = sch_stage(design, board, layout, name="blinky")
    assert err is None, err
    assert stale_reason(layout, board) == ("place", None)
    at = _r1_at(placed)
    x, y = float(at.split()[1]), float(at.split()[2].rstrip(")"))
    placed.write_text(placed.read_text().replace(at, f"(at {x + 0.04:.4f} {y:.4f})", 1))
    stage, why = stale_reason(layout, board)
    assert stage == "sch" and why == f"placed/layout.kicad_pcb is not the board the place stage wrote (edited or deleted): {NEVER_EDITED}"
    assert planned(stage, upto="fab", force=False) == ["place", "route", "fab"]
    placed.unlink()
    assert stale_reason(layout, board) == ("sch", None)


def test_a_module_board_py_imports_is_a_hashed_input(tmp_path: Path):
    """T2: `from helper import LEFT` in board.py was a source the stamps never hashed: edit helper.py,
    plain build, the stale board ships ok=True. The choice: **hash** it (a board's helpers are its
    author's to organise), never refuse it. `load_board` records every module the exec imported from
    the board's own directory, takes it back out of `sys.modules` so the next load reads the file,
    and both stamps carry its hash."""
    import json
    import sys

    from pcbc.build import INPUTS, pcb_job, stale_reason

    board = _blinky_with_helper(tmp_path)
    design = load_board(board)
    assert design.modules == ["helper.py"] and "helper" not in sys.modules
    assert not pcb_job(board).get("error")
    layout = tmp_path / "layout" / "blinky"
    assert sorted(json.loads((layout / "placed" / INPUTS).read_text())["modules"]) == ["helper.py"]
    assert stale_reason(layout, board)[0] is None  # `pcbc pcb` never draws the schematic (fifth review)
    from pcbc.build import sch_stage

    assert sch_stage(load_board(board), board, layout, name="blinky")[1] is None
    assert stale_reason(layout, board) == ("place", None)
    before = _r1_at(layout / "placed" / "layout.kicad_pcb")
    (tmp_path / "helper.py").write_text("LEFT = 14\n")
    assert stale_reason(layout, board) == (None, "board.py, a module it imports or a part file changed since the schematic was drawn")  # the schematic's record answers first, and it hashes helper.py
    # and the same process reads the new value: the module was not left in Python's cache
    assert not pcb_job(board).get("error")
    assert _r1_at(layout / "placed" / "layout.kicad_pcb") != before


def test_core_copper_lives_on_a_copper_layer_and_a_via_with_no_size_is_refused(tmp_path: Path):
    """T1: `Pour(None, layer="Edge.Cuts", ...)` in core built verified and put a filled region into
    the outline Gerber. T3: `Via(None, (x, y))` with no size or drill escaped as a traceback from
    `layout_emit.render_one`. Both are refused at load, naming the line."""
    from pcbc.layout_job import load_core

    board, design = _blinky(tmp_path)
    core = tmp_path / "layout.core.py"
    core.write_text('Pour(None, layer="Edge.Cuts", points=[(5, 5), (15, 5), (15, 15), (5, 15)], id="edgepour")\n')
    with pytest.raises(ValueError, match=r"layout\.core\.py:1: pour on 'Edge\.Cuts', which is not a copper layer of this stackup \(F\.Cu, B\.Cu\).*Board\(40, 25\) in board\.py"):
        load_core(design, board)
    core.write_text('Seg("LED", (1, 2), (3, 2), width=0.25, layer="F.SilkS")\n')
    with pytest.raises(ValueError, match=r"layout\.core\.py:1: seg on 'F\.SilkS', which is not a copper layer"):
        load_core(load_board(board), board)
    core.write_text('Via(None, (3, 3), id="v")\n')
    with pytest.raises(ValueError, match=r"layout\.core\.py:1: a via on no net needs size= and drill="):
        load_core(load_board(board), board)
    core.write_text('Seg(None, (1, 2), (3, 2), id="s")\n')
    with pytest.raises(ValueError, match=r"layout\.core\.py:1: a seg on no net needs width="):
        load_core(load_board(board), board)


def test_kicad_string_escapes_read_and_write_both_ways():
    """P2: `sexp.parse_tree` decoded only `\\"` and `\\\\`, so a newline KiCad wrote as `\\n` read back
    as a backslash and an n, the emitted board carried that, and gen1 == gen2 could not see it; a real
    newline written raw crashed KiCad's parser. Probe 5 is KiCad's own save of every escape."""
    from pcbc.sexp import parse_tree, quote

    s = 'a\nb\rc\ttab "q" back\\slash'
    assert parse_tree(f"(gr_text {quote(s)})")[1] == s
    assert quote("x\ny") == '"x\\ny"' and "\n" not in quote("x\ny")
    probe = FIX / "kicad10_primitives5.kicad_pcb"
    assert probe in PROBES
    board = decompile(probe.read_text())
    assert next(g.f["text"] for g in board.graphics if g.kind == "gr_text") == s
    assert next(g.f["text"] for g in board.graphics if g.kind == "gr_text_box") == "line1\nline2"
    assert board.copper[0].name == 'zone\nname "q"'
    # the bytes KiCad wrote for the text come back from the emitter exactly
    line = next(ln for ln in probe.read_text().splitlines() if ln.startswith("\t(gr_text "))
    text = next(g for g in board.graphics if g.kind == "gr_text")
    assert render_graphics([text], board="x").splitlines()[0] == line


def test_every_gen_line_of_every_probe_loads_back_alone_equal():
    """P3: a `Dimension` line was written positional (`pts`) while the constructor took `a, b`, a
    `Table` line's positional `column_count` was read as `at`, and a `Generated` loaded back with a
    list where the decompiler held a tuple. Every line of every probe, one at a time, in a fresh
    design, must give back the object it was written from (`layout_job.comparable`)."""
    from pcbc.gen import assign_ids
    from pcbc.layout_job import comparable

    seen: Counter = Counter()
    for probe in PROBES:
        board = decompile(probe.read_text())
        assign_ids(board.copper, set(), board.graphics)
        for o in [*board.copper, *board.graphics]:
            path = Path(__file__).resolve().parent / "fixtures" / "kicad10" / "_line.py"
            design = Design()
            try:
                path.write_text(line_of(o) + "\n")
                load_layout(path, design, source="gen")
            finally:
                path.unlink(missing_ok=True)
            got = [*design.copper, *design.graphics]
            assert len(got) == 1 and comparable(got[0]) == comparable(o), (probe.name, line_of(o)[:200], [str(x) for x, y in zip(comparable(o), comparable(got[0])) if x != y] if got else got)
            seen[o.kind] += 1
    assert {"dimension", "table", "generated", "pour", "via"} <= set(seen), seen


def test_every_kicad_saved_probe_decompiles_and_a_stroke_less_table_reads():
    """P4: a table saved with border and separators off has no `(stroke)` under either, and the schema
    required its width, so the decompiler raised. The census proved no *extra* token; this is the
    reverse: every probe KiCad saved decompiles without raising."""
    for probe in PROBES:
        decompile(probe.read_text())
    board = decompile((FIX / "kicad10_primitives5.kicad_pcb").read_text())
    table = next(g for g in board.graphics if g.kind == "table")
    assert table.f["border"] == {"external": False, "header": False, "width": None, "type": None, "color": None}
    assert table.f["separators"]["rows"] is False and table.f["separators"]["width"] is None
    text = render_graphics([table], board="x")
    assert "(border (external no) (header no))" in text and "(stroke" not in text.split("(cells", 1)[0]


def test_a_teardrop_zone_is_kicads_own_and_never_a_layout_object():
    """P1: a zone carrying `(attr (teardrop (type ...)))` — KiCad's teardrop generator's, with no
    `(uuid)` and no `(name)` — made the decompiler raise at `(uuid)`, and a core `Via(teardrops=...)`
    killed the build with a traceback. It is KiCad-derived like `filled_polygon`: dropped by the
    decompiler, skipped by every zone reader, regenerated by KiCad's refill."""
    from pcbc.gen import is_teardrop_zone
    from boardtext import zone_rules
    from pcbc.route_verify import zones

    text = (FIX / "kicad10_primitives5.kicad_pcb").read_text()
    nodes = [n for n in parse_tree(text)[1:] if isinstance(n, list) and n[0] == "zone"]
    assert sorted(is_teardrop_zone(n) for n in nodes) == [False, True]
    board = decompile(text)
    assert board.teardrops == 1 and len(board.copper) == 1 and board.copper[0].teardrop_type is None
    assert unmapped(census(text)) == []
    assert [z.layer for z in zones(text)] == ["B.Cu"] and [r.layer for r in zone_rules(text)] == ["B.Cu"]


def test_a_core_pour_outline_and_a_hatch_or_teardrop_default_are_what_kicad_will_keep(tmp_path: Path):
    """R1: a core `Pour` whose outline KiCad normalises on load (a consecutive duplicate vertex, a
    repeated closing vertex, a vertex equal to an adjacent arc's endpoint) was refused as "the router
    dropped or moved locked core copper", naming no field. P5: a `fill_mode="hatch"` pour was refused
    for the hatch defaults KiCad fills in. The emitter writes both as KiCad will hold them and the
    change is recorded (`core_filled`); a via's partial `teardrops` is completed the same way."""
    from pcbc.layout_emit import HATCH_DEFAULTS, TEARDROP_DEFAULTS, normalise_outline, stamp

    sq = ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0))
    assert normalise_outline([(0, 0), (0, 0), (20, 0), (20, 20), (0, 20)]) == sq
    assert normalise_outline([(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]) == sq
    assert normalise_outline([(0, 30), (20, 30), ((20, 30), (25, 40), (20, 50)), (0, 50)]) == ((0.0, 30.0), ((20.0, 30.0), (25.0, 40.0), (20.0, 50.0)), (0.0, 50.0))
    assert normalise_outline([(30, 30), ((50, 30), (55, 40), (50, 50)), (50, 50), (30, 50)]) == ((30.0, 30.0), ((50.0, 30.0), (55.0, 40.0), (50.0, 50.0)), (30.0, 50.0))
    assert normalise_outline([(60, 30), (80.1234567, 30), (80, 50), (60, 50)])[1] == (80.123457, 30.0)
    kept = [(30, 0), (40, 0), (50, 0), (50, 20), (30, 20)]  # a collinear midpoint stays, as KiCad keeps it
    assert normalise_outline(kept) == tuple((float(x), float(y)) for x, y in kept)
    board, design = _blinky(tmp_path)
    (tmp_path / "layout.core.py").write_text(
        'Pour(None, layer="F.Cu", points=[(30, 18), (30, 18), (36, 18), (36, 23), (30, 23), (30, 18)], id="dup")\n'
        'Pour(None, layer="F.Cu", fill_mode="hatch", points=[(30, 18), (36, 18), (36, 23), (30, 23)], id="hatchy")\n'
        'Via("GND", (6, 20), size=0.5, drill=0.3, teardrops={"enabled": True}, id="td")\n'
    )
    load_layout(tmp_path / "layout.core.py", design, source="core")
    dup, hatchy, td = stamp(design, design.copper)
    assert dup.points == ((30.0, 18.0), (36.0, 18.0), (36.0, 23.0), (30.0, 23.0))
    assert {k: getattr(hatchy, k) for k in HATCH_DEFAULTS} == HATCH_DEFAULTS
    assert td.teardrops == TEARDROP_DEFAULTS and list(td.teardrops) == list(TEARDROP_DEFAULTS)
    with pytest.raises(ValueError, match="teardrops has no field"):
        stamp(design, [Copper("via", "x", "GND", at=(1, 1), size=0.5, drill=0.3, teardrops={"bogus": 1})])


def test_a_refused_field_names_what_kicad_kept_down_to_the_key():
    """R2: a core line written past KiCad's precision is refused with the value KiCad's save has, so
    the message says what to write; a dict-valued field (`teardrops`) names the key that differs,
    `teardrops.filter_ratio`, and not the nine-entry dict around it (measured: `1/3` comes back
    `0.3333333333`, ten significant digits; `30.123456789` comes back `30.12345679`)."""
    from pcbc.layout_job import diff_objects

    td = dict(TEARDROP_DEFAULTS_FOR_TEST)
    wrote = Copper("via", "td", "GND", at=(1, 1), size=0.5, drill=0.3, teardrops={**td, "filter_ratio": 1 / 3})
    kept = Copper("via", "td", "GND", at=(1, 1), size=0.5, drill=0.3, teardrops={**td, "filter_ratio": 0.3333333333})
    (msg,) = diff_objects([wrote], [kept], what="KiCad's save")
    assert "teardrops.filter_ratio (written 0.3333333333333333, KiCad's save has 0.3333333333: write that)" in msg, msg
    assert "best_length_ratio" not in msg
    sq = [(30.0, 18.0), (36.0, 18.0), (36.0, 23.0), (30.0, 23.0)]
    wrote = Copper("pour", "h", None, layer="F.Cu", points=sq, fill_mode="hatch", hatch_orientation=30.123456789)
    kept = Copper("pour", "h", None, layer="F.Cu", points=sq, fill_mode="hatch", hatch_orientation=30.12345679)
    (msg,) = diff_objects([wrote], [kept], what="KiCad's save")
    assert "hatch_orientation (written 30.123456789, KiCad's save has 30.12345679: write that)" in msg, msg


TEARDROP_DEFAULTS_FOR_TEST = {"best_length_ratio": 0.5, "max_length": 1.0, "best_width_ratio": 1.0, "max_width": 2.0, "curved_edges": False, "filter_ratio": 0.9, "enabled": True, "allow_two_segments": False, "prefer_zone_connections": False}


def test_the_place_only_board_has_relative_model_paths(tmp_path: Path):
    """P6/D3: the board files the build writes sit one directory below `layout/<name>/`, with every
    `(model ...)` path relative to the board file, so two builds from two paths write the same bytes.
    The native placer writes no seed board at all; the place-only board `pcbc pcb` emits is the one
    board file of the place stage."""
    from pcbc.build import pcb_job

    board = _buck(tmp_path)
    assert not pcb_job(board).get("error")
    layout = tmp_path / "buck" / "layout" / "buck"
    assert not (layout / "seed").exists() and not (layout / "layout.kicad_pcb").exists()
    pcb = layout / "placed" / "layout.kicad_pcb"
    text = pcb.read_text()
    assert '(model "../../../components/' in text and str(tmp_path) not in text, pcb
    assert (pcb.parent / "../../../components/TI/TPS54202DDCR").resolve().is_dir()


def test_fab_report_paths_are_relative_and_a_missing_export_is_named(tmp_path: Path):
    """D3: `report.json` carried absolute paths (and KiCad's own stdout carried one in the middle of a
    sentence). A3: a drill export with returncode 1, or a gerber export that died after F.Cu, was
    `ok=True`; every requested layer's Gerber and both drill files are now required by name."""
    from pcbc.fab import _relative, export_missing, gerber_stems
    from pcbc.seed import emit_pcb

    root = tmp_path / "layout" / "buck"
    root.mkdir(parents=True)
    got = _relative({"fab": str(root / "fab"), "steps": [{"stdout": f"Saved DRC Report to {root}/fab/drc.json\n"}], "kicad_cli": "/Applications/kicad-cli"}, root)
    assert got == {"fab": "fab", "steps": [{"stdout": "Saved DRC Report to fab/drc.json\n"}], "kicad_cli": "/Applications/kicad-cli"}
    text = emit_pcb(load_board(EXAMPLES / "blinky" / "blinky.py"), name="blinky")
    layers = ["F.Cu", "B.Cu", "F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
    assert gerber_stems(text, layers) == {"F.Cu": "F_Cu", "B.Cu": "B_Cu", "F.Paste": "F_Paste", "B.Paste": "B_Paste", "F.SilkS": "F_Silkscreen", "B.SilkS": "B_Silkscreen", "F.Mask": "F_Mask", "B.Mask": "B_Mask", "Edge.Cuts": "Edge_Cuts"}
    (tmp_path / "gerbers").mkdir()
    assert export_missing(text, layers, tmp_path / "gerbers", tmp_path) == [f"gerber {la}" for la in layers] + ["gerber job file", "drill PTH", "drill NPTH"]
    for name in ("layout-F_Cu.gtl", "layout-job.gbrjob"):
        (tmp_path / "gerbers" / name).write_text("")
    (tmp_path / "layout-PTH.drl").write_text("")
    assert export_missing(text, layers, tmp_path / "gerbers", tmp_path) == [f"gerber {la}" for la in layers[1:]] + ["drill NPTH"]


def _mutate_in_fab(monkeypatch, fn):
    """Corrupt `fab/layout.kicad_pcb` inside `fab_job`, right after fab's own edits (the fiducials,
    when it inserts any) and before its DRC: at `check_job`, fab's first read of its board. The
    third and fifth reviews' arbiter harnesses patched `silk_job` there; fab no longer runs one
    (the silk references are the place stage's), so the mutation point moved to the next call."""
    import pcbc.fab as FAB

    orig = FAB.check_job

    def patched(job, work, *a, **k):
        text = Path(work).read_text()
        new = fn(text)
        assert new != text, "the mutation changed nothing"
        Path(work).write_text(new)
        return orig(job, work, *a, **k)

    monkeypatch.setattr(FAB, "check_job", patched)


@pytest.mark.kicad
def test_fab_ships_only_the_copper_the_gates_judged(tmp_path: Path, monkeypatch):
    """(S1), for A1, A2 and A5: fab was a second, weaker judge — its DRC ran errors-only and never
    read `unconnected_items`, its ampacity gate was vacuous once the `pcbc:` groups were gone — so a
    via deleted, a spine narrowed with its group dropped, a pour shrunk or a track end moved inside
    fab shipped ten Gerbers `ok=True`. The fab board's copper and role groups must equal the routed
    board's, by uuid and field, or fab refuses naming the object; no fab-side blind spot can matter."""
    import re

    from pcbc.compile import compile_design
    from pcbc.fab import fab_copper_diff, fab_job
    from boardtext import _blocks

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    design = load_board(EXAMPLES / "buck" / "buck.py")
    job = compile_design(design)
    gen = Design()
    load_layout(final.parent / "layout.gen.py", gen, source="gen")
    by_id = {c.id: c for c in gen.copper}
    gnd_via = next(c for c in gen.copper if c.kind == "via" and c.net == "GND")
    spine = next(by_id[m] for g in gen.graphics if g.kind == "group" and g.f["name"].startswith("pcbc:spine:") for m in g.f["members"] if by_id[m].net == "5V" and by_id[m].kind == "seg")
    boot = next(c for c in gen.copper if c.kind == "seg" and c.net == "BOOT")

    def via_block(text, at):
        for s, e, h in _blocks(text):
            if h == "via" and re.search(rf"\(at {at[0]:g} {at[1]:g}\)", text[s:e]):
                return s, e
        raise AssertionError(at)

    def delete_via(text):
        s, e = via_block(text, gnd_via.at)
        return text[: s - 2] + text[e:]

    def narrow_spine_and_drop_groups(text):
        s, e = _seg_block(text, spine.a, spine.b)
        text = text[:s] + re.sub(r"\(width\s+[0-9.]+\)", "(width 0.2)", text[s:e], count=1) + text[e:]
        out, pos = [], 0
        for s, e, h in _blocks(text):
            if h == "group" and '"pcbc:' in text[s:e]:
                out.append(text[pos : s - 2])
                pos = e
        out.append(text[pos:])
        return "".join(out)

    def shrink_pour(text):
        from pcbc.sexp import matching_paren

        for s, e, h in _blocks(text):
            b = text[s:e]
            if h == "zone" and '"GND"' in b and '"B.Cu"' in b and "keepout" not in b:
                i = b.index("(polygon")
                j = matching_paren(b, i)
                return text[:s] + b[:i] + "(polygon (pts (xy 0.3 0.3) (xy 3.3 0.3) (xy 3.3 3.3) (xy 0.3 3.3)))" + b[j + 1 :] + text[e:]
        raise AssertionError("no B.Cu GND zone")

    def move_boot_end(text):
        s, e = _seg_block(text, boot.a, boot.b)
        block = text[s:e]
        m = re.search(r"\(end\s+([-0-9.]+)\s+([-0-9.]+)\)", block)
        moved = block[: m.start()] + f"(end {float(m.group(1)) + 0.3:g} {m.group(2)})" + block[m.end() :]
        return text[:s] + moved + text[e:]

    cases = {"delete_via": (delete_via, "via .* on GND is on the routed board and not on the fab board"), "narrow_spine_drop_groups": (narrow_spine_and_drop_groups, "group .* is on the routed board and not on the fab board"), "shrink_pour": (shrink_pour, "pour .* differs on the fab board in points"), "move_boot_end": (move_boot_end, "seg .* differs on the fab board in b")}
    for name, (fn, says) in cases.items():
        _mutate_in_fab(monkeypatch, fn)
        result = fab_job(job, final, out_dir=tmp_path / name, design=design)
        assert result["error"] and result["error"].startswith("the fab board is not the routed board: "), (name, result["error"])
        # A refused fab keeps its board only as `refused.kicad_pcb`, never where a package would be (fifth review).
        assert not (tmp_path / name / "layout.kicad_pcb").exists(), name
        moved = fab_copper_diff(final.read_text(), (tmp_path / name / "refused.kicad_pcb").read_text())
        assert any(re.search(says, m) for m in moved), (name, moved)
        assert result.get("copper_equal") is False and not list((tmp_path / name / "gerbers").glob("*")), name
    assert fab_copper_diff(final.read_text(), final.read_text()) == []
    monkeypatch.undo()
    clean = fab_job(job, final, out_dir=tmp_path / "clean", design=design)
    assert clean["error"] is None and clean["copper_equal"] is True and clean["unconnected"] == 0 and clean["gerbers"] == 10


@pytest.mark.kicad
def test_fab_outputs_are_stamped_and_an_export_failure_is_an_error(tmp_path: Path, monkeypatch):
    """A4/T4: an edited or deleted fab board, nine deleted Gerbers, deleted drill files were "skipped:
    fab, ok". A3: a failed or partial `kicad-cli` export was `ok=True`. (S2): `fab/inputs.json`
    stamps every file the fab stage wrote and the routed board it read; any mismatch is stale from
    fab, and the fab stage stamps only when its exports all succeeded."""
    import pcbc.fab as FAB
    from pcbc.build import NEVER_EDITED, build_job, stale_reason

    board, _design = _blinky(tmp_path)
    layout = tmp_path / "layout" / "blinky"
    first = build_job(board, upto="fab", force=True)
    assert first.get("ok"), first.get("error")
    assert stale_reason(layout, board) == ("fab", None)
    fab_board = layout / "fab" / "layout.kicad_pcb"
    fab_board.write_text(fab_board.read_text().replace("(width 0.25)", "(width 0.2)", 1))
    assert stale_reason(layout, board) == ("route", f"fab/layout.kicad_pcb is not the file the fab stage wrote (edited or deleted): {NEVER_EDITED}")
    again = build_job(board, upto="fab")
    assert again.get("ok") and again["plan"] == ["fab"] and "(width 0.2)" not in fab_board.read_text()
    for victim in sorted((layout / "fab" / "gerbers").glob("*"))[1:]:
        victim.unlink()
    stage, why = stale_reason(layout, board)
    assert stage == "route" and why.startswith("fab/gerbers/") and "8 more" in why and why.endswith(NEVER_EDITED)
    assert build_job(board, upto="fab")["plan"] == ["fab"] and stale_reason(layout, board) == ("fab", None)
    (layout / "fab" / "layout-PTH.drl").unlink()
    assert stale_reason(layout, board)[1].startswith("fab/layout-PTH.drl is not the file")
    # A3: the drill export fails
    orig = FAB._run

    def failing(cmd):
        if "export" in cmd and "drill" in cmd:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "simulated kicad-cli failure"}
        return orig(cmd)

    monkeypatch.setattr(FAB, "_run", failing)
    failed = build_job(board, upto="fab")
    assert failed["plan"] == ["fab"] and failed.get("ok") is None and "kicad-cli drill export failed (1)" in failed["error"]
    # no stamp was written, so the fab package does not count: route is done and fab is to do
    assert stale_reason(layout, board) == ("route", None) and not (layout / "fab" / "inputs.json").exists()
    monkeypatch.undo()
    assert build_job(board, upto="fab")["plan"] == ["fab"] and stale_reason(layout, board) == ("fab", None)


@pytest.mark.kicad
def test_a_pour_locked_verbatim_changes_nothing_else(tmp_path: Path):
    """D1: buck's GND pour copied verbatim into core dropped the whole `gnd_pour` step, so GND and VIN
    were rerouted (25 objects gone, 9 new) and a seg+via+pour lock was refused blaming the via line
    (`via_dangling`). Now (fourth review, C4) a line equal to what the build draws is adopted, not
    locked, so the rest routes as in the build the line was copied from: copper identical to the
    baseline by signature, DRC identical by type."""
    from pcbc.build import build_job
    from pcbc.gen import signature

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    base = Counter(signature(c) for c in decompile(final.read_text()).copper)
    gen = (final.parent / "layout.gen.py").read_text().splitlines()
    pour = next(ln for ln in gen if ln.startswith('Pour("GND"'))
    seg = next(ln for ln in gen if ln.startswith('Seg("5V"'))
    via = next(ln for ln in gen if ln.startswith('Via("GND"'))
    for tag, core in (("pour", pour), ("segviapour", "\n".join((seg, via, pour)))):
        board = _buck(tmp_path / tag)
        (board.parent / "layout.core.py").write_text(core + "\n")
        result = build_job(board, upto="route", force=True)
        assert result.get("error") is None, (tag, result.get("error"))
        route = next(s for s in result["steps"] if s["stage"] == "route")
        assert route["copper"] == "verified" and route["core_objects"] == core.count("\n") + 1
        # A verbatim line is not a lock at all: the first pass routes with nothing locked, reproduces
        # every line, and adopts them (`lock_passes`), so the route is exactly the baseline's.
        ids = sorted(ln.rsplit('id="', 1)[1].split('"')[0] for ln in core.splitlines())
        assert route["lock_passes"] == [{"locked": [], "reproduced": ids, "missing": []}] and route["adopted"] == ids and route["locked"] == [], route["lock_passes"]
        got = Counter(signature(c) for c in decompile((board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb").read_text()).copper)
        assert got == base, (tag, list((base - got).elements())[:3], list((got - base).elements())[:3])
        (tmp_path / tag / "a").mkdir()
        (tmp_path / tag / "b").mkdir()
        assert _drc_types(final, tmp_path / tag / "a") == _drc_types(board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb", tmp_path / tag / "b")


@pytest.mark.kicad
def test_two_builds_from_two_absolute_paths_are_byte_identical_with_no_normalisation(tmp_path: Path):
    """D3: two builds from different absolute paths differed in the boards' `(model ...)` paths and in
    `report.json`; and (fifth review) in KiCad's wall-clock dates inside every Gerber, drill file,
    drill map and `drc.json`, and the fab stamp that hashes them. Fab pins every date
    (`fab.pin_dates`), so the **whole** `layout/<name>/` tree is compared byte for byte, with no file
    left out: the native route stage writes no intermediate board at all."""
    import hashlib
    import re

    from pcbc.build import build_job

    dirs = [tmp_path / "a" / "blinky", tmp_path / "some" / "deeper dir" / "b" / "blinky"]
    for d in dirs:
        d.mkdir(parents=True)
        (d / "blinky.py").write_text((EXAMPLES / "blinky" / "blinky.py").read_text())
        assert build_job(d / "blinky.py", upto="fab", force=True).get("ok")

    def tree(d: Path) -> dict:
        return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in d.rglob("*") if p.is_file()}

    a, b = tree(dirs[0] / "layout" / "blinky"), tree(dirs[1] / "layout" / "blinky")
    assert set(a) == set(b)
    differ = sorted(k for k in a if a[k] != b[k])
    assert differ == [], differ
    # 33 files (2026-09-25, native): the schematic and its record, the routed board, its gen file and
    # three sidecars, the route record, and the fab package.
    assert len(a) > 25 and any(k.startswith("fab/gerbers/") for k in a) and "fab/layout-PTH.drl" in a and "fab/drc.json" in a and "fab/inputs.json" in a
    assert str(tmp_path) not in (dirs[0] / "layout" / "blinky" / "fab" / "report.json").read_text()


@pytest.mark.kicad
def test_a_multi_line_text_and_a_teardrop_via_survive_kicads_save(tmp_path: Path):
    """P2 and P1 through KiCad itself: a core `Text` with a real newline, and a via with teardrops,
    emitted, saved by KiCad and decompiled, give back the same primitive bytes on a second pass (K1 ==
    K2, not only P1 == P2); KiCad's refill of the via writes no layout object the decompiler keeps."""
    import subprocess

    from pcbc.gen import assign_ids
    from pcbc.layout_emit import render, render_graphics, splice, strip_layout, uuid_of
    from pcbc.netcheck import kicad_drc

    board, design = _blinky(tmp_path)
    (tmp_path / "layout.core.py").write_text('Text("a\\nb \\"q\\"", (10, 10), layer="F.SilkS", id="t")\nVia("GND", (6, 20), size=0.5, drill=0.3, teardrops={"enabled": True}, id="td")\nSeg("GND", (6, 20), (9, 20), width=0.25, id="s")\n')
    load_layout(tmp_path / "layout.core.py", design, source="core")
    assert design.graphics[0].f["text"] == 'a\nb "q"'
    ids = {o.id: uuid_of(o, "blinky") for o in [*design.copper, *design.graphics]}
    base = strip_layout((FIX / "kicad10_primitives3.kicad_pcb").read_text())

    def passes(objs_cu, objs_gr, name: str) -> tuple[str, str]:
        pcb = tmp_path / f"{name}.kicad_pcb"
        pcb.write_text(splice(base, render(design, objs_cu, board="blinky") + render_graphics(objs_gr, board="blinky", uuids=ids)))
        run = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", str(pcb)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
        text = pcb.read_text()
        prims = [ln for ln in text.splitlines() if ln.startswith(("\t(gr_text", "\t(via", "\t(segment", "\t(zone"))]
        return text, "\n".join(prims)

    k1, p1 = passes(design.copper, design.graphics, "k1")
    b1 = decompile(k1)
    assert unmapped(census(k1)) == [] and next(g.f["text"] for g in b1.graphics if g.kind == "gr_text") == 'a\nb "q"'
    assign_ids(b1.copper, set(), b1.graphics)
    k2, p2 = passes(b1.copper, b1.graphics, "k2")
    assert p1 == p2
    # and through KiCad's refill (the copper gate's save): whatever teardrop zones it writes are dropped
    pcb = tmp_path / "k1.kicad_pcb"
    kicad_drc(pcb, refill=True)
    b3 = decompile(pcb.read_text())
    assert unmapped(census(pcb.read_text())) == [] and [c.kind for c in b3.copper] == [c.kind for c in b1.copper]


# ------------------------------------------------------------------------------ the fourth review
#
# Four structural fixes (`docs/layout-properties.md`, "Fourth review"): every file a stage reads is
# stamped and the sidecars are regenerated (C1); fab equality is judged on the whole board (C2); a
# core line is refused at load or it is legal (C3); a lock is idempotent (C4).


def _stamp_of(layout: Path, rel: str) -> dict:
    import json

    return json.loads((layout / rel).read_text())


@pytest.mark.kicad
def test_every_file_a_stage_reads_is_stamped_and_every_sidecar_is_regenerated(tmp_path: Path):
    """C1 (BLOCKER + MAJOR): the `.kicad_pro`/`.kicad_dru`/`.kicad_prl` beside every board were build
    outputs `kicad-cli pcb drc` and fab read, and none was stamped: a `placed/layout.kicad_pro` with its
    clearances at 0.02 and every severity `ignore` silenced the arbiter and shipped in the fab
    package. The sidecar a stage writes is regenerated from the compiled job (`project.write_sidecars`)
    and the copy on disk is stamped, so an edit is stale from the stage that wrote it, worded "build
    output, never edited". (The native build has no seed board: the placer builds its footprints in
    memory, and the place stage writes a board only with `--upto place`.)"""
    import json

    from pcbc.build import NEVER_EDITED, build_job, stale_reason
    from pcbc.seed import emit_pro

    board, design = _blinky(tmp_path)
    layout = tmp_path / "layout" / "blinky"
    # the place stamp: `build --upto place`, edit board.py, plain build places again
    assert build_job(board, upto="place", force=True).get("ok")
    assert stale_reason(layout, board) == ("place", None)
    stamp = _stamp_of(layout, "placed/inputs.json")
    assert set(stamp) == {"board", "parts", "modules", "reads", "placed", "gen", "sidecars", "traced"} and set(stamp["sidecars"]) == {"layout.kicad_pro", "layout.kicad_dru"}
    assert "blinky.py" in stamp["reads"] and set(stamp["traced"]) == {"reads", "dirs", "env", "child_env", "tools", "argv"} and "pcbc" in stamp["traced"]["tools"]
    pro = emit_pro(design, name="blinky")
    p = layout / "placed" / "layout.kicad_pro"
    assert p.read_text() == pro
    doc = json.loads(p.read_text())
    doc["board"]["design_settings"]["rule_severities"] = {"clearance": "ignore"}
    p.write_text(json.dumps(doc, indent=2) + "\n")
    assert stale_reason(layout, board) == ("sch", f"placed/layout.kicad_pro is not the file the place stage wrote (edited or deleted): {NEVER_EDITED}")
    again = build_job(board, upto="place")
    assert again.get("ok") and again["plan"] == ["place"] and p.read_text() == pro
    src = board.read_text()
    board.write_text(src.replace('"1k"', '"4k7"', 1))
    why = "board.py, a module it imports or a part file changed since the schematic was drawn"
    assert stale_reason(layout, board) == (None, why)
    full = build_job(board, upto="fab")
    assert full.get("ok") and full["plan"][0] == "check" and full["stale"] == why, full
    assert "4k7,R1" in (layout / "fab" / "bom.csv").read_text()
    assert stale_reason(layout, board) == ("fab", None)
    assert not (layout / "placed").exists(), "a full build leaves no place-only board of an earlier run"
    assert set(_stamp_of(layout, "inputs.json")["sidecars"]) == {"layout.kicad_pro", "layout.kicad_dru", "layout.kicad_prl"}
    # every sidecar the build wrote is the compiled job's, byte for byte, at every stage
    for stage in ("routed", "fab"):
        assert (layout / stage / "layout.kicad_pro").read_text() == pro, stage
    dru = (layout / "routed" / "layout.kicad_dru").read_text()
    assert dru and (layout / "fab" / "layout.kicad_dru").read_text() == dru
    d = layout / "routed" / "layout.kicad_dru"
    d.write_text("(version 1)\n")
    assert stale_reason(layout, board) == ("place", f"routed/layout.kicad_dru is not the file the route stage wrote (edited or deleted): {NEVER_EDITED}")
    assert build_job(board, upto="fab")["plan"] == ["route", "fab"] and d.read_text() == dru
    sch = layout / "schematic.kicad_sch"
    sch.write_text(sch.read_text() + "\n")
    assert stale_reason(layout, board) == (None, f"schematic.kicad_sch is not the file the sch stage wrote (edited or deleted): {NEVER_EDITED}")
    assert build_job(board, upto="fab")["plan"][0] == "check" and stale_reason(layout, board) == ("fab", None)


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_decompiler_refuses_a_uuid_two_objects_carry():
    """C2: a second via carrying an existing uuid was invisible to a dict keyed by uuid and shipped."""
    from pcbc.gen import DuplicateUuid

    via = '\t(via\n\t\t(at {x} 4)\n\t\t(size 0.5)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "")\n\t\t(uuid "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1")\n\t)\n'
    text = "(kicad_pcb\n\t(version 1)\n" + via.format(x=1) + via.format(x=2) + ")\n"
    with pytest.raises(DuplicateUuid, match=r"aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1 is carried by 2 layout objects \(via, via\)"):
        decompile(text)
    assert len(decompile(text, unique=False).copper) == 2


@pytest.mark.kicad
def test_fab_refuses_every_change_outside_its_own_allowlist(tmp_path: Path, monkeypatch):
    """C2 (MAJOR): `gr_rect`/`gr_poly`/`gr_line` on a copper layer, a footprint added or re-valued, a
    pad's paste layer, the tenting, a duplicated uuid — all shipped from inside fab with
    `copper_equal=True`. `fab_board_diff` compares every top-level node of both boards as a multiset
    with `FAB_WRITES` as the only allowed differences."""
    import re

    from pcbc.compile import compile_design
    from pcbc.fab import fab_board_diff, fab_job
    from boardtext import _blocks
    from pcbc.sexp import board_footprint_spans, footprint_reference

    final = routed_board("buck", EXAMPLES / "buck" / "buck.py")
    design = load_board(EXAMPLES / "buck" / "buck.py")
    job = compile_design(design)
    assert fab_board_diff(final.read_text(), final.read_text()) == []
    # A zone's fill is compared exactly, vertex for vertex (M3, seventh round): a nanometre on one
    # vertex is refused as surely as a millimetre. The route stage hands fab the fill's fixed point
    # (`build.py` fills the emitted board once before the gates refill it), so fab's refill of the same
    # board from the same compiled sidecars comes back the same bytes.
    txt = final.read_text()
    m = re.search(r"\(filled_polygon[^(]*\(layer \"[^\"]+\"\)\s*\(pts\s*\(xy ([-0-9.]+) ([-0-9.]+)\)", txt)
    assert m, "buck's routed board carries a filled pour"

    def moved(dx: float) -> str:
        return txt[: m.start(1)] + f"{float(m.group(1)) + dx:.6f}" + txt[m.end(1) :]

    for dx in (1e-6, 1.0):
        said = fab_board_diff(txt, moved(dx))
        assert any("fill on" in x and "is not the routed board's vertex for vertex" in x for x in said), (dx, said)

    def top(text, node):
        st = text.rstrip()
        return st[:-1] + node + ")\n"

    def fp(text, ref):
        for s, e in board_footprint_spans(text):
            if footprint_reference(text[s:e]) == ref:
                return s, e
        raise AssertionError(ref)

    def dup_uuid_via_before(text):
        for s, e, h in _blocks(text):
            if h == "via":
                uid = re.search(r'\(uuid "([^"]+)"\)', text[s:e]).group(1)
                return text[:s] + f'(via\n\t\t(at 31 4)\n\t\t(size 0.5)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "")\n\t\t(uuid "{uid}")\n\t)\n\t' + text[s:]
        raise AssertionError("via")

    def gr_poly_fcu(text):
        return top(text, '\t(gr_poly\n\t\t(pts\n\t\t\t(xy 30 3) (xy 32 3) (xy 32 5) (xy 30 5)\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type solid)\n\t\t)\n\t\t(fill yes)\n\t\t(layer "F.Cu")\n\t\t(uuid "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa3")\n\t)\n')

    def add_footprint(text):
        s, e = fp(text, "C_IN2")
        b = text[s:e].replace('(property "Reference" "C_IN2"', '(property "Reference" "C_X"', 1)
        b = re.sub(r"\n\t\t\(at [-0-9.]+ [-0-9.]+(?: [-0-9.]+)?\)", "\n\t\t(at 31 4)", b, count=1)
        b = re.sub(r'\(net "[^"]*"\)', '(net "")', b)
        return text[:e] + "\n\t" + b + text[e:]

    def value_change(text):
        s, e = fp(text, "C_IN2")
        return text[:s] + text[s:e].replace('(property "Value" "22uF"', '(property "Value" "1uF"', 1) + text[e:]

    def pad_paste(text):
        s, e = fp(text, "R_FB_TOP")
        b = text[s:e]
        i = b.find('(pad "1"')
        return text[:s] + b[:i] + b[i:].replace('(layers "F.Cu" "F.Mask" "F.Paste")', '(layers "F.Cu" "F.Mask")', 1) + text[e:]

    def tenting_off(text):
        return text.replace("(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)", "(tenting\n\t\t\t(front no)\n\t\t\t(back no)", 1)

    cases = {
        "dup_uuid_via_before": (dup_uuid_via_before, r"carries a uuid twice"),
        "gr_poly_fcu": (gr_poly_fcu, r"gr_poly aaaaaaaa-1111-4111-8111-aaaaaaaaaaa3 is on the fab board and nothing routed it"),
        # a copied footprint carries its original's uuids too: refused as a uuid carried twice, naming both
        "add_footprint": (add_footprint, r"footprint C_X is on the fab board and nothing routed it|carries the uuid .* 2 times \(footprint C_IN2, footprint C_X\)"),
        "value_change": (value_change, r"footprint C_IN2 is on the routed board and not on the fab board as it was"),
        "pad_paste": (pad_paste, r"footprint R_FB_TOP is on the routed board and not on the fab board as it was"),
        "tenting_off": (tenting_off, r"\(setup\) is on the routed board and not on the fab board as it was"),
    }
    for name, (fn, says) in cases.items():
        _mutate_in_fab(monkeypatch, fn)
        result = fab_job(job, final, out_dir=tmp_path / name, design=design)
        assert result["error"] and result["error"].startswith("the fab board is not the routed board"), (name, result["error"])
        assert re.search(says, result["error"]), (name, result["error"])
        assert result.get("copper_equal") is False and not list((tmp_path / name / "gerbers").glob("*")), name
        monkeypatch.undo()
    clean = fab_job(job, final, out_dir=tmp_path / "clean", design=design)
    assert clean["error"] is None and clean["copper_equal"] is True and clean["gerbers"] == 10


WORDS_FIXTURE = FIX / "kicad10_words.kicad_pcb"


def test_kicad_s_save_keeps_what_the_loader_writes_for_every_word_and_layer_list():
    """C3: the loader normalises what KiCad's save normalises (measured on `kicad10_words.kicad_pcb`,
    `make_kicad10_words.py`): a via's layers as (top, bottom), a zone's layers in layer-id order, a
    `fill` of `solid`/`none` as yes/no, an uppercase uuid lowercased. So a core line KiCad would
    rewrite is refused (the uuid) or already written the way KiCad keeps it (the rest)."""
    from pcbc.language import Pour, Rect, Via, reset
    from pcbc.layout_prims import WORDS, lset_order, stack_order

    board = decompile(WORDS_FIXTURE.read_text())
    vias = [c for c in board.copper if c.kind == "via"]
    assert all(v.layers == ("F.Cu", "B.Cu") for v in vias) and vias[2].uuid == "abcdef01-1111-4111-8111-111111111116"
    zone = next(c for c in board.copper if c.kind == "pour" and c.layers)
    assert zone.layers == ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu") == lset_order(("In2.Cu", "F.Cu", "In1.Cu", "B.Cu"))
    assert stack_order("B.Cu", "F.Cu") == ("F.Cu", "B.Cu") and stack_order("B.Cu", "In2.Cu") == ("In2.Cu", "B.Cu")
    assert [g.f["fill"] for g in board.graphics if g.kind == "gr_rect"] == [True, "hatch", False]
    # tenting none is dropped by the save; covering/plugging none is kept: the word lists say so
    assert vias[0].tenting is None and vias[0].covering == {"front": "none", "back": "yes"} and vias[0].plugging == {"front": "no", "back": "none"}
    assert WORDS["via.tenting"] == ("yes", "no", "none") and "none" in WORDS["via.covering"]  # one side none beside a set side is kept (probe 2); both none is dropped
    with pytest.raises(ValueError, match=r"tenting with every side 'none' is what KiCad writes nothing for"):
        Via("GND", (1, 1), tenting={"front": "none", "back": "none"})
    # smoothing none and mode polygon are dropped: not words
    assert [c.smoothing for c in board.copper if c.kind == "pour"] == [None, "chamfer"] and all(c.fill_mode is None for c in board.copper if c.kind == "pour")
    assert WORDS["pour.smoothing"] == ("chamfer", "fillet") and WORDS["pour.fill_mode"] == ("hatch",)
    reset()
    assert Via("GND", (1, 1), layers=("B.Cu", "F.Cu")).layers == ("F.Cu", "B.Cu")
    assert Pour(None, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), points=[(1, 1), (4, 1), (4, 4), (1, 4)], keepout={"tracks": "not_allowed"}).layers == ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
    assert [Rect((1, 1), (2, 2), fill=f).f["fill"] for f in ("solid", "none", "hatch", True)] == [True, False, "hatch", True]


@pytest.mark.parametrize(
    "line, says",
    [
        ('Pour("LED", layer="F.Cu", points=[(1, 16), (12, 16), (12, 24), (1, 24)], fill_mode="solid", id="p")', r"fill_mode must be one of 'hatch' \(or leave it out\)"),
        ('Pour("LED", layer="F.Cu", points=[(1, 16), (12, 16), (12, 24), (1, 24)], hatch=("bogus", 0.5), id="p")', r"hatch style must be one of 'none', 'edge', 'full'"),
        ('Pour("LED", layer="F.Cu", points=[(1, 16), (12, 16), (12, 24), (1, 24)], smoothing="bogus", id="p")', r"smoothing must be one of 'chamfer', 'fillet'"),
        ('Pour("LED", layer="F.Cu", points=[(1, 16), (12, 16), (12, 24), (1, 24)], fill_mode="hatch", hatch_border_algorithm="bogus", id="p")', r"hatch_border_algorithm must be one of"),
        ('Pour(None, layer="F.Cu", points=[(31, 16), (34, 16), (34, 20), (31, 20)], keepout={"tracks": "maybe", "vias": "not_allowed"}, id="k1")', r"keepout\['tracks'\] must be one of 'allowed', 'not_allowed'"),
        ('Via("GND", (20, 5), tenting={"front": "maybe", "back": "yes"}, id="v1")', r"tenting\['front'\] must be one of 'yes', 'no'"),
        ('Via("GND", (20, 5), tenting={"front": "none", "back": "none"}, id="v1")', r"tenting with every side 'none' is what KiCad writes nothing for; leave tenting= out"),
        ('Via("GND", (20, 5), kind="bogus", id="v1")', r"kind must be one of 'through', 'blind', 'buried', 'micro'"),
        ('Rect((26, 2), (30, 8), layer="F.SilkS", stroke_type="bogus", id="r1")', r"stroke_type must be one of 'solid', 'dash'"),
        ('Text("j", (5, 3), layer="F.SilkS", justify=("middle",), id="t1")', r"justify holds 'middle'; KiCad's words are 'left', 'right', 'top', 'bottom', 'mirror'"),
        ('Dimension([(0, 70), (10, 70)], kind="bogus", id="d1")', r"kind must be one of 'aligned', 'orthogonal', 'radial', 'leader', 'center', got 'bogus'"),
        ('Via("GND", (20, 5), uuid="ABCDEF01-1111-4111-8111-111111111116", id="v1")', r"uuid 'ABCDEF01-1111-4111-8111-111111111116' is not what KiCad keeps: its save writes it lowercase, 'abcdef01-1111-4111-8111-111111111116'"),
        ('Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width=0.16, uuid="12345678", id="s1")', r"uuid '12345678' is not a uuid KiCad keeps"),
        ('Rect((26, 2), (30, 8), layer="F.SilkS", uuid="12345678", id="r1")', r"uuid '12345678' is not a uuid KiCad keeps"),
        # values KiCad's save rewrites, refused at load rather than after routing (coreprobe5
        # `seg_width_string`, coreprobe6 `free_seg_locked_group`: "written 0.05 / False, KiCad's save has
        # None / True")
        ('Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width=0.16, solder_mask_margin="0.05", id="s1")', r"solder_mask_margin='0.05' is not a number"),
        ('Seg("LED", (9.44, 12.5), (10.1951, 13.2551), width=0.16, solder_mask_margin=0.05, id="s1")', r"solder_mask_margin=0.05 on a track with solder_mask=False is what KiCad's save drops"),
        ('Seg("LED", (5, 10), (9, 10), width=0.16, id="s1")\nGroup("g", ["s1"], locked=True, id="g1")', r"'s1' is a member of the locked group 'g' .*KiCad's save locks it"),
    ],
)
def test_a_word_kicad_has_no_token_for_is_refused_at_load_naming_the_line(tmp_path: Path, line: str, says: str):
    """C3 (MAJOR): eight of fifteen such lines routed a whole board and then died in a `RuntimeError`
    traceback out of `kicad-cli` ("Expecting none, edge, or full. Got 'bogus'"). Every word-valued
    field is checked at load against `layout_prims.WORDS` (KiCad's own lists, narrowed to what its
    save keeps), and a uuid KiCad would rewrite is refused naming what it keeps."""
    board, design = _blinky(tmp_path)
    core = tmp_path / "layout.core.py"
    core.write_text("# a comment\n" + line + "\n")
    with pytest.raises(ValueError, match=r"layout\.core\.py:2: .*" + says):
        load_layout(core, design, source="core")


def test_an_astral_character_in_a_string_round_trips_through_layout_gen(tmp_path: Path):
    """C3 (MAJOR): a net named `LED😀` was written by `json.dumps` as a UTF-16 surrogate pair,
    `\\ud83d\\ude00`, which Python reads as two lone surrogates; `layout.gen.py` did not load back
    equal to the board and emit refused its own output. Strings are written as literals Python
    reads back (`\\U0001f600`)."""
    from pcbc.gen import _py

    net = "LED\U0001f600"
    seg = Copper("seg", "seg-1", net, a=(1.0, 2.0), b=(3.0, 2.0), width=0.16, uuid="aaaaaaaa-1111-4111-8111-aaaaaaaaaaa1", source="gen")
    text = gen_source([], [seg], board="b", graphics=[Graphic("gr_text", "t-1", {"text": "x\U0001f600y", "locked": False, "at": (1.0, 1.0), "angle": 0.0, "layer": "F.SilkS", "knockout": False, "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaa2", "face": None, "size": (1.0, 1.0), "line_spacing": None, "thickness": 0.15, "bold": False, "italic": False, "justify": (), "hide": False}, source="gen")])
    assert "\\U0001f600" in text and "\\ud83d" not in text and _py("é") == '"\\u00e9"'
    gen = tmp_path / "layout.gen.py"
    gen.write_text(text)
    d = Design()
    d.nets = {net: object()}  # type: ignore[assignment]
    load_layout(gen, d, source="gen")
    assert d.copper[0].net == net and d.graphics[0].f["text"] == "x\U0001f600y"


def test_the_pour_step_pins_the_plane_inset_to_the_stackup():
    """C4: the plane inset depended on which sibling `.kicad_pro` a router step happened to leave
    (blinky shipped 0.5 mm against a declared 0.3). The native route stage's `Pour` objects are inset by
    the stackup's `edge_clearance`, a field every emitted plane carries."""
    from pcbc.compile import compile_design
    from pcbc.route_native import plane_pours
    from pcbc.stackup import get_stackup

    for name in ("blinky", "node"):
        design = load_board(EXAMPLES / name / f"{name}.py")
        job = compile_design(design)
        e = get_stackup(job.stackup).edge_clearance
        w, h = job.board_size_mm
        pours = plane_pours(design, job, name)
        assert pours, name
        for p in pours:
            assert p.points == ((e, e), (w - e, e), (w - e, h - e), (e, h - e)), (name, p.points)


@pytest.mark.kicad
def test_a_gen_line_locked_alone_changes_nothing_else(tmp_path: Path):
    """C4 (MAJOR): locking ONE gen line changed unrelated copper on 51 of buck's 84 lines and on
    blinky's tap via (the lock sat in D1's exit lane before the hop pattern ran; the hop refused and
    KRT routed LED at 0.127 mm). Adoption by reproduction: the first pass routes with nothing
    locked, the line is a piece the build draws anyway, and it is adopted with its role group. The
    board is the baseline's, object for object, uuid for uuid, DRC for DRC."""
    from pcbc.build import build_job
    from pcbc.gen import signature

    board, _design = _blinky(tmp_path)
    first = build_job(board, upto="route", force=True)
    assert first.get("error") is None, first
    base = decompile((tmp_path / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_text())
    base_drc = next(s for s in first["steps"] if s["stage"] == "route")["drc_by_type"]
    line = next(x for x in Path(first["gen"]).read_text().splitlines() if x.startswith('Via("GND", (28.9179, 12.5)'))
    (tmp_path / "layout.core.py").write_text(line + "\n")
    again = build_job(board, upto="route", force=True)
    assert again.get("error") is None, again
    route = next(s for s in again["steps"] if s["stage"] == "route")
    assert route["lock_passes"] == [{"locked": [], "reproduced": ["via-987d5a93"], "missing": []}] and route["adopted"] == ["via-987d5a93"] and route["locked"] == []
    got = decompile((tmp_path / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_text())
    assert Counter(signature(c) for c in got.copper) == Counter(signature(c) for c in base.copper)
    assert {c.uuid for c in got.copper} == {c.uuid for c in base.copper} and route["drc_by_type"] == base_drc
    assert sorted(g.f["name"] for g in got.graphics if g.kind == "group") == ["pcbc:hop:D1.2", "pcbc:tap:D1.1"]
    # a line the build would not draw is locked on the second pass, and the record says so
    (tmp_path / "layout.core.py").write_text('Seg("LED", (5, 10), (9, 10), width=0.16, id="s1")\n')
    hand = build_job(board, upto="route", force=True)
    route = next(s for s in hand["steps"] if s["stage"] == "route")
    assert route["lock_passes"] == [{"locked": [], "reproduced": [], "missing": ["s1"]}, {"locked": ["s1"], "reproduced": [], "missing": []}] and route["locked"] == ["s1"]
    # Re-recorded 2026-09-25 (the second refutation round, major 2). This line used to assert that the
    # build refused the lock as `track_dangling` — which was the relaxer trimming the router's link to
    # one of the lock's two free ends, not the lock. Both free ends are routing terminals
    # (`route_native.EndTerminal`), the relaxer now keeps a free end only its run holds
    # (`route_relax.free_ends_held`), and the valid lock builds with both ends joined.
    assert hand["error"] is None, hand["error"]
    assert not [k for k in route["drc_by_type"] if k.endswith(("track_dangling", "via_dangling"))], route["drc_by_type"]
    gen = Path(hand["gen"]).read_text()
    for end in ("(5.0, 10.0)", "(9.0, 10.0)"):
        assert any(x.startswith('Seg("LED"') and end in x for x in gen.splitlines()), (end, "the router's link to this free end")


@pytest.mark.kicad
def test_a_core_group_over_a_pattern_s_piece_is_refused_naming_both_groups(tmp_path: Path):
    """C3/C4: a core `Group` over a piece a pattern owns was silently dropped by KiCad (an item is in
    one group) and the two-groups guard compared ids against uuids and never fired."""
    from pcbc.build import build_job

    board, _design = _blinky(tmp_path)
    first = build_job(board, upto="route", force=True)
    line = next(x for x in Path(first["gen"]).read_text().splitlines() if x.startswith('Seg("LED", (9.44, 12.5)'))
    (tmp_path / "layout.core.py").write_text(line + '\nGroup("mine", ["seg-f9491dcd"], id="g1")\n')
    again = build_job(board, upto="route", force=True)
    assert again["error"] and "layout.core.py:2" in again["error"] and "pcbc:hop:D1.2" in again["error"] and "KiCad keeps an item in one group" in again["error"], again["error"]
