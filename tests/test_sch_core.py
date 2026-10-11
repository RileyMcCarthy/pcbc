"""docs/schematic-layout.md §1 and §5: schematic layout lives in `schematic.core.py` beside `board.py`.

`board.py` is the circuit and its physics only (docs/direction.md §2). A `Sch*` call made while `board.py` or
`layout.core.py` runs is refused, by any import path, naming its line and `schematic.core.py`; a board or PCB object
made while `schematic.core.py` runs is refused naming the file it belongs in. `schematic.core.py` loads under its own
trace, so editing it redraws the sheet and leaves the placed and routed board current. Each test fails when the
behaviour it pins is removed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbc.language import check_board, load_board

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

CIRCUIT = """from pcbc import Board, Ground, Net, NetReq, Power, Resistor
VCC = Power("VCC")
GND = Ground("GND")
MID = Net("MID")
Resistor("R1", "1k", package="0402", mpn="X", lcsc="C1", p1=VCC, p2=MID)
Resistor("R2", "1k", package="0402", mpn="X", lcsc="C1", p1=MID, p2=GND)
Board(layers=2, stackup="jlcpcb_2l_1oz")
NetReq("VCC", kind="power", volts=3.3)
NetReq("GND", kind="power")
"""
CORE = """Outline(width=30, height=20)
Place("R1", at=(10, 10))
Place("R2", at=(20, 10))
"""
SCH = """SchRegion("pair", title="Pair", members=("R1", "R2"))
SchPlace("R2", to="R1.2")
"""


def _board(tmp_path: Path, board: str = CIRCUIT, *, core: str | None = CORE, sch: str | None = SCH) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "two.py"
    p.write_text(board)
    for name, text in (("layout.core.py", core), ("schematic.core.py", sch)):
        f = tmp_path / name
        if text is None:
            if f.exists():
                f.unlink()
        else:
            f.write_text(text)
    return p


def _refusal(path: Path, **kw) -> str:
    with pytest.raises(ValueError) as got:
        load_board(path, **kw)
    return str(got.value)


# ------------------------------------------------------------------ §1.1: one loader, its own trace


def test_schematic_core_loads_after_board_py_under_its_own_trace(tmp_path: Path):
    d = load_board(_board(tmp_path))
    assert [(p.ref, p.line, p.source) for p in d.sch_places] == [("R2", 2, "core")]
    assert [(r.name, r.members, r.line) for r in d.sch_regions] == [("pair", ("R1", "R2"), 1)]
    assert d.sch_core is not None and len(d.sch_core["sha"]) == 64
    assert "schematic.core.py" in d.sch_core["reads"], "its own trace holds the file"
    assert "schematic.core.py" not in d.reads, "board.py's trace does not: the place, route and fab stamps never see it"
    assert "schematic.core.py" not in d.core_reads
    assert check_board(tmp_path / "two.py") == []


def test_the_bench_alone_loads_without_the_schematic_core(tmp_path: Path):
    d = load_board(_board(tmp_path), schematic_core=False)
    assert d.sch_places == [] and d.sch_core is None


def test_no_schematic_core_is_a_design_with_no_schematic_locks(tmp_path: Path):
    d = load_board(_board(tmp_path, sch=None))
    assert d.sch_places == [] and d.sch_regions == [] and d.sch_core is None


# ------------------------------------------------------------------ §1.2 / §5: board.py holds no schematic layout


@pytest.mark.parametrize(
    "line, want",
    [
        ('SchPlace("R1", left=20, top=20)', "two.py:10: SchPlace('R1') is schematic layout; two.py holds the circuit and its physics only, and schematic layout lives in schematic.core.py beside two.py"),
        ('SchRegion("a", title="A")', "two.py:10: SchRegion('a') is schematic layout; two.py holds the circuit and its physics only, and schematic layout lives in schematic.core.py beside two.py"),
        ('SchSheet(wire_mm=0)', "two.py:10: SchSheet() is schematic layout"),
        ('SchNet("MID", wire_mm=0)', "two.py:10: SchNet('MID') is schematic layout"),
        ('from pcbc import SchPlace\nSchPlace("R1", left=20, top=20)', "two.py:11: SchPlace('R1') is schematic layout"),
        ('import pcbc.language as L\nL.SchPlace("R1", left=20, top=20)', "two.py:11: SchPlace('R1') is schematic layout"),
        ('import pcbc.sch_core as S\nS.SchRegion("a")', "two.py:11: SchRegion('a') is schematic layout"),
    ],
    ids=["SchPlace", "SchRegion", "SchSheet", "SchNet", "from-pcbc-import", "language.SchPlace", "sch_core.SchRegion"],
)
def test_schematic_layout_in_board_py_is_refused_by_any_path_naming_its_line_and_the_core_file(tmp_path: Path, line: str, want: str):
    assert want in _refusal(_board(tmp_path, CIRCUIT + line + "\n", sch=None))


def test_the_import_line_alone_is_not_refused(tmp_path: Path):
    d = load_board(_board(tmp_path, "from pcbc import SchPlace, SchRegion\n" + CIRCUIT))
    assert [p.ref for p in d.sch_places] == ["R2"]


def test_many_schematic_lines_in_board_py_are_one_refusal_with_their_lines(tmp_path: Path):
    lines = 'SchRegion("a")\nSchPlace("R1")\nSchPlace("R2", to="R1.2")\n'
    msg = _refusal(_board(tmp_path, CIRCUIT + lines, sch=None))
    assert "two.py:10: SchRegion('a') is schematic layout" in msg
    assert "two.py:11: SchPlace('R1') is schematic layout" in msg and "(and 1 more: line 12)" in msg


def test_a_refused_board_py_puts_no_schematic_layout_into_the_design(tmp_path: Path):
    import pcbc.language as L

    _refusal(_board(tmp_path, CIRCUIT + 'SchPlace("R1", left=20, top=20)\n', sch=None))
    assert L._doc().sch_places == []


def test_net_wire_mm_in_board_py_is_refused_naming_schnet(tmp_path: Path):
    text = CIRCUIT.replace('MID = Net("MID")', 'MID = Net("MID", wire_mm=0)')
    msg = _refusal(_board(tmp_path, text))
    assert "two.py:4: Net('MID', wire_mm=0): the schematic wire limit is schematic layout; it is SchNet() in schematic.core.py beside two.py" in msg


@pytest.mark.parametrize("where", ["board", "core", "sch"])
def test_schstyle_is_refused_in_every_file_naming_schsheet(tmp_path: Path, where: str):
    board, core, sch = CIRCUIT, CORE, SCH
    if where == "board":
        board += "SchStyle(wire_mm=0)\n"
    elif where == "core":
        core += "SchStyle(wire_mm=0)\n"
    else:
        sch += "SchStyle(wire_mm=0)\n"
    assert "SchStyle() is now SchSheet(), in schematic.core.py" in _refusal(_board(tmp_path, board, core=core, sch=sch))


# ------------------------------------------------------------------ §1.2: layout.core.py holds no schematic layout


@pytest.mark.parametrize(
    "line, want",
    [
        ('SchPlace("R1", left=20, top=20)', "layout.core.py:4: SchPlace('R1') is schematic layout; it lives in schematic.core.py beside board.py"),
        ('SchRegion("a")', "layout.core.py:4: SchRegion('a') is schematic layout; it lives in schematic.core.py beside board.py"),
        ('import pcbc.language as L\nL.SchPlace("R1", left=20, top=20)', "layout.core.py:5: SchPlace('R1') is schematic layout; it lives in schematic.core.py"),
    ],
    ids=["SchPlace", "SchRegion", "language.SchPlace"],
)
def test_schematic_layout_in_layout_core_is_refused_naming_the_schematic_core(tmp_path: Path, line: str, want: str):
    assert want in _refusal(_board(tmp_path, core=CORE + line + "\n"))


# ------------------------------------------------------------------ §5: schematic.core.py holds schematic layout only


@pytest.mark.parametrize(
    "line, want",
    [
        ('Place("R1", at=(1, 1))', "schematic.core.py:3: Place() is layout.core.py's, not schematic.core.py's"),
        ('NetReq("MID", kind="digital")', "schematic.core.py:3: NetReq() is board.py's, not schematic.core.py's"),
        ('Seg((0, 0), (1, 0), width=0.2, layer="F.Cu", net="MID")', "schematic.core.py:3: Seg() is layout.core.py's, not schematic.core.py's"),
        ('import pcbc.language as L\nL.NetReq("MID", kind="digital")', "schematic.core.py:4: NetReq() is board.py's, not schematic.core.py's"),
        ('import pcbc.language as L\nL.Net_("NEW")', "schematic.core.py:4: Net() is board.py's, not schematic.core.py's"),
        ('import pcbc.language as L\nL._current.netreqs.append(None)', "schematic.core.py: this file changed board.py's part of the design (netreqs), however it was imported"),
        ('import pcbc.language as L\nL._current.places.clear()', "schematic.core.py: this file changed layout.core.py's part of the design (places), however it was imported"),
    ],
    ids=["Place", "NetReq", "Seg", "language.NetReq", "language.Net", "design.netreqs", "design.places"],
)
def test_a_board_or_pcb_object_in_the_schematic_core_is_refused_naming_its_file(tmp_path: Path, line: str, want: str):
    assert want in _refusal(_board(tmp_path, sch=SCH + line + "\n"))


@pytest.mark.parametrize(
    "line, want",
    [
        ('SchPlace("R9", to="R1.2")', "schematic.core.py:3: SchPlace('R9'): two.py has no part R9"),
        ('SchPlace("R2", to="R1.1")', "schematic.core.py:3: SchPlace('R2') is already written at line 2; a part takes one SchPlace()"),
        ('SchPlace("R2", to="R1.2", locked=True)', "schematic.core.py:3: SchPlace('R2') takes no keyword locked=; it takes at=, to=, pin=, side=, gap=, along="),
        ('SchRegion("a", members=("R1",), rows=2)', "schematic.core.py:3: SchRegion('a') takes no keyword rows="),
        ('SchNet("NOPE", wire_mm=0)', "schematic.core.py:3: SchNet('NOPE'): two.py has no net NOPE"),
        ('SchNet("MID")', "schematic.core.py:3: SchNet('MID'): wire_mm= is the one thing a SchNet() says"),
        ('SchSheet(paper="A1")', "schematic.core.py:3: SchSheet(): paper='A1'; the sheet is A4, A3 or A2"),
        ('SchSheet()\nSchSheet()', "schematic.core.py:4: SchSheet(): the sheet is already declared at schematic.core.py:3"),
    ],
    ids=["unknown-ref", "duplicate", "unknown-kw-place", "unknown-kw-region", "unknown-net", "schnet-no-limit", "paper", "two-sheets"],
)
def test_a_wrong_schematic_core_line_is_refused_naming_its_line(tmp_path: Path, line: str, want: str):
    assert want in _refusal(_board(tmp_path, sch=SCH + line + "\n"))


def test_schsheet_and_schnet_set_the_wire_limits_the_emitter_reads(tmp_path: Path):
    from pcbc.sch_emit import emit_from_design

    plain = emit_from_design(load_board(_board(tmp_path)), title="two")
    assert plain.count('(label "MID"') == 1, "R2 hangs off R1.2: one wire joins MID, named once"
    for sch in (SCH + "SchNet('MID', wire_mm=0)\n", SCH + "SchSheet(wire_mm=0)\n"):
        text = emit_from_design(load_board(_board(tmp_path, sch=sch)), title="two")
        assert '(label "MID"' in text and text.count('(label "MID"') == 2, "labels only: MID is named at both pins"


# ------------------------------------------------------------------ §1.1: the sch stage is a side branch


def _copy_example(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    dst.mkdir(parents=True)
    for f in (f"{name}.py", "layout.core.py", "schematic.core.py"):
        if (EXAMPLES / name / f).exists():
            shutil.copy(EXAMPLES / name / f, dst / f)
    if (EXAMPLES / name / "components").exists():
        shutil.copytree(EXAMPLES / name / "components", dst / "components")
    return dst / f"{name}.py"


def _sha(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.kicad
def test_editing_the_schematic_core_redraws_only_the_sheet_and_editing_layout_core_never_redraws_it(tmp_path: Path):
    from pcbc.build import build_job, sch_reason, stale_reason
    from pcbc.project import layout_dir

    board = _copy_example(tmp_path, "blinky")
    (board.parent / "schematic.core.py").write_text('SchPlace("D1", pin="A", to="R1.2", gap=2.54)\n')
    first = build_job(board, upto="route")
    assert first.get("error") in (None,) or str(first["error"]).startswith("unrouted:"), first.get("error")
    layout = layout_dir(board)
    routed, stamp, sheet = layout / "routed" / "layout.kicad_pcb", layout / "inputs.json", layout / "schematic.kicad_sch"
    before = (_sha(routed), stamp.read_text(), _sha(sheet))

    core = board.parent / "schematic.core.py"
    core.write_text(core.read_text().replace('to="R1.2", gap=2.54', 'to="R1.2", gap=5.08'))
    assert stale_reason(layout, board)[0] == "route", "the board side is current"
    assert "schematic.core.py changed since the schematic was drawn (edited)" == sch_reason(layout, board)
    again = build_job(board, upto="route")
    assert again["plan"] == ["sch"], again["plan"]
    assert (_sha(routed), stamp.read_text()) == before[:2], "the routed board and its record are untouched"
    assert _sha(sheet) != before[2], "the sheet is drawn again"
    assert sch_reason(layout, board) is None

    drawn = _sha(sheet)
    lc = board.parent / "layout.core.py"
    lc.write_text(lc.read_text() + "\n# a comment: the file's bytes change\n")
    third = build_job(board, upto="route")
    assert third["plan"] == ["route"], third["plan"]  # stale from place: the route stage places again, the sheet is not drawn
    assert _sha(sheet) == drawn, "a layout.core.py edit never redraws the sheet"


@pytest.mark.kicad
def test_a_hand_edited_sheet_or_a_deleted_schematic_core_redraws_only_the_sheet(tmp_path: Path):
    from pcbc.build import build_job, sch_reason
    from pcbc.project import layout_dir

    board = _copy_example(tmp_path, "blinky")
    (board.parent / "schematic.core.py").write_text('SchPlace("D1", pin="A", to="R1.2", gap=2.54)\n')
    build_job(board, upto="route")
    layout = layout_dir(board)
    sheet = layout / "schematic.kicad_sch"
    sheet.write_text(sheet.read_text() + "\n")
    assert sch_reason(layout, board).startswith("schematic.kicad_sch is not the file the sch stage wrote")
    assert build_job(board, upto="route")["plan"] == ["sch"]
    (board.parent / "schematic.core.py").unlink()
    assert sch_reason(layout, board) == "schematic.core.py changed since the schematic was drawn (deleted)"
    assert build_job(board, upto="route")["plan"] == ["sch"]


def test_the_schematic_stamp_records_the_schematic_core(tmp_path: Path):
    from pcbc.build import _sch_core_record

    board = _board(tmp_path)
    d = load_board(board)
    rec = _sch_core_record(board, d)
    assert rec["sha"] == _sha(tmp_path / "schematic.core.py") and "schematic.core.py" in rec["reads"]
    json.dumps(rec)


@pytest.mark.parametrize(
    "tail, want",
    [
        ("L._doc().sch_places.append(SchPlaceSpec('R1'))", "design.sch_places holds 1 entry (SchPlaceSpec) written by two.py without a constructor; it is layout, and its file is schematic.core.py beside two.py"),
        ("L._doc().sch_regions.append(SchRegionSpec('x', members=('R1',)))", "design.sch_regions holds 1 entry (SchRegionSpec)"),
        ("L._doc().sch_sheet = SchSheetSpec(paper='A2', wire_mm=0.0)", "design.sch_sheet holds 1 entry (SchSheetSpec)"),
        ("L._doc().keepouts.append(KeepoutSpec('K', (0, 0, 1, 1)))", "design.keepouts holds 1 entry (KeepoutSpec) written by two.py without a constructor; it is layout, and its file is layout.core.py beside two.py"),
        ("L._doc().outline = OutlineSpec(width=30.0, height=30.0)", "design.outline holds 1 entry (OutlineSpec)"),
        ("L._doc().board.size_mm = (30.0, 30.0)", "design.board carries an outline (size (30.0, 30.0), origin (0.0, 0.0)) written by two.py; a declared outline is Outline() in layout.core.py"),
    ],
    ids=["sch_places", "sch_regions", "sch_sheet", "keepouts", "outline", "board-size"],
)
def test_layout_written_into_the_design_directly_by_board_py_is_refused(tmp_path: Path, tail: str, want: str):
    """refute:split 2026-10-08: the named constructors were refused, but `_doc()` written directly was accepted and
    the sheet and the PCB stage used it. The design is inspected after board.py runs (§1.2: every import path)."""
    head = "import pcbc.language as L\nfrom pcbc.model import *\n"
    assert want in _refusal(_board(tmp_path, CIRCUIT + head + tail + "\n", sch=None, core=None))


_SPEC_CHANGES = {
    "size_mm": ("(30.0, 30.0)", "design.board carries an outline (size (30.0, 30.0), origin (0.0, 0.0)) written by two.py"),
    "origin": ("(5.0, 5.0)", "design.board carries an outline (size None, origin (5.0, 5.0)) written by two.py"),
    "layers": ("4", "design.board.layers was set to 4 by two.py after Board() made the board with layers=2"),
    "stackup": ("'jlcpcb_4l_1oz'", "design.board.stackup was set to 'jlcpcb_4l_1oz' by two.py after Board() made the board with stackup='jlcpcb_2l_1oz'"),
    "planes": ("(('GND', 'B.Cu'),)", "design.board.planes was set to (('GND', 'B.Cu'),) by two.py after Board() made the board with planes=()"),
    "pcb": ("'other.kicad_pcb'", "design.board.pcb was set to 'other.kicad_pcb' by two.py after Board() made the board with pcb=None"),
}


def test_every_board_spec_field_is_named_by_the_after_board_refusal():
    """The class is closed, not one attribute: every BoardSpec field has an entry in what the refusal compares, so a
    field added later is compared from the day it exists (refute:split 2026-10-08, major: `.padding` set after Board()).
    padding itself is gone (docs/direction.md §2, 2026-10-09): no layout file could set it, and the box parts are fitted
    into is the outline."""
    import dataclasses

    from pcbc.language import _BOARD_SPEC_REACH
    from pcbc.model import BoardSpec

    assert set(_BOARD_SPEC_REACH) == {f.name for f in dataclasses.fields(BoardSpec)} == set(_SPEC_CHANGES)


@pytest.mark.parametrize("field", sorted(_SPEC_CHANGES))
def test_a_board_spec_field_board_py_sets_after_board_is_refused(tmp_path: Path, field: str):
    """`Board(...).padding = (0, 0, 0, 15)` moved blinky's outline and R1 by 15 mm and `pcbc pcb` exited 0 (refute:split
    2026-10-08). Every BoardSpec field reaches placement, the outline or the router, so each one board.py changes after
    Board() made the board is refused at load, naming it; `pcbc pcb` stops on it."""
    from pcbc.cli import main

    value, want = _SPEC_CHANGES[field]
    board = _board(tmp_path, CIRCUIT + f"import pcbc.language as L\nL._doc().board.{field} = {value}\n", sch=None)
    assert want in _refusal(board)
    assert main(["pcb", str(board)]) != 0


def test_a_board_spec_board_py_builds_without_board_is_refused(tmp_path: Path):
    head = CIRCUIT + "import pcbc.language as L\nfrom pcbc.model import BoardSpec\n"
    tail = "L._doc().board = BoardSpec(layers=2, stackup='jlcpcb_2l_1oz', origin=(5.0, 5.0))\n"
    msg = _refusal(_board(tmp_path, head + tail, sch=None))
    assert "design.board is a BoardSpec two.py set without Board() (size None, origin (5.0, 5.0), layers 2, stackup 'jlcpcb_2l_1oz'" in msg, msg
    assert load_board(_board(tmp_path / "ok", sch=None)).board.origin == (0.0, 0.0)


def test_board_spec_has_no_padding_and_the_content_box_is_the_outline():
    """`BoardSpec.padding` fed `layout.content_rect` but no layout file could set it: Board(padding=) is refused and
    Outline() takes width, height and at. It is removed (docs/direction.md §2, 2026-10-09): the box placement and
    routing fit the parts into is the outline itself."""
    import dataclasses

    from pcbc.layout import board_rect, content_rect
    from pcbc.model import BoardSpec

    assert "padding" not in {f.name for f in dataclasses.fields(BoardSpec)}
    b = BoardSpec(size_mm=(30.0, 20.0), origin=(5.0, 7.0))
    r = content_rect(b)
    assert r == board_rect(b) and (r.x0, r.y0, r.x1, r.y1) == (5.0, 7.0, 35.0, 27.0)


def test_an_attribute_board_py_adds_to_the_board_spec_is_refused(tmp_path: Path):
    """`Board(...).padding = (0, 0, 0, 15)` names a field BoardSpec no longer has. Nothing reads such an attribute,
    so it is refused as a fact naming it and the fields there are, rather than accepted and ignored."""
    from pcbc.cli import main

    board = _board(tmp_path, CIRCUIT + "import pcbc.language as L\nL._doc().board.padding = (0.0, 0.0, 0.0, 15.0)\n", sch=None)
    msg = _refusal(board)
    assert "design.board.padding was set to (0.0, 0.0, 0.0, 15.0) by two.py after Board() made the board; BoardSpec has no field padding and nothing reads it" in msg, msg
    assert main(["pcb", str(board)]) != 0


@pytest.mark.kicad
def test_pcbc_sch_exits_non_zero_and_states_the_refusal_on_a_sheet_with_a_hard_finding(tmp_path: Path, capsys):
    """refute:tests (major, 2026-10-08): `pcbc sch` threw the stage's error away and exited 0 on a sheet the build
    refuses (DS2 with no core: 2 hard findings). It now prints the same error `pcbc build` stops on and exits 1."""
    from pcbc.cli import main

    sch = 'SchRegion("a", title="A", members=("R1",), at=(12.7, 12.7))\nSchRegion("b", title="B", members=("R2",), at=(27.94, 12.7))\n'
    board = _board(tmp_path, sch=sch)
    assert main(["sch", str(board)]) == 1
    err = capsys.readouterr().err
    assert "schematic is not readable (" in err and "region_frames: region a overlaps region b" in err, err
    assert main(["sch", str(board), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"].startswith("schematic is not readable ("), out["error"]
    assert main(["sch", str(_board(tmp_path / "ok"))]) == 0


@pytest.mark.kicad
def test_a_pose_lock_off_the_paper_is_a_fact_naming_its_line_the_paper_and_the_distance(tmp_path: Path, capsys):
    """§5: a pose-locked part outside the paper is a build error naming the line, the paper and the distance outside
    (refute:split: it named only the part and the paper)."""
    from pcbc.cli import main

    sch = 'SchRegion("a", title="A", members=("R1", "R2"), at=(12.7, 12.7))\nSchPlace("R1", at=(635, 508))\n'
    assert main(["sch", str(_board(tmp_path, sch=sch))]) == 1
    err = capsys.readouterr().err
    assert "parts_off_paper: schematic.core.py:2: the at= of R1's SchPlace puts it there: R1's body is " in err, err
    assert "mm outside the A2 sheet's border (570 x 396 mm inside it)" in err, err


# ------------------------------------------------------------------ refusals state the facts (refute:split 2026-10-08)


def _node_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "node"
    shutil.copytree(EXAMPLES / "node", dst, ignore=shutil.ignore_patterns("layout"))
    return dst / "node.py"


def _gen_lines(board: Path) -> list[str]:
    from pcbc.sch_emit import emit_from_design
    from pcbc.sch_gen import gen_text

    rep: dict = {}
    emit_from_design(load_board(board), title="node", report=rep)
    return gen_text(load_board(board), rep, "node").splitlines()


def test_a_label_lock_off_its_wire_is_refused_with_the_group_as_drawn(tmp_path: Path):
    """The label-lock refusal states what the sheet holds: the group's wires and pin ends, how far the nearest is, and
    whether this sheet applies other locks (it said only "on no wire", refute:split 2026-10-08)."""
    from pcbc.sch_emit import emit_from_design

    board = _node_copy(tmp_path)
    line = next(ln for ln in _gen_lines(board) if ln.startswith("SchLabel(") and ", at=(" in ln).split("  #")[0]
    dx = float(line.split("at=(")[1].split(",")[0])
    far = line.replace(f"at=({line.split('at=(')[1].split(',')[0]},", f"at=({dx + 30.48:g},")
    core = board.parent / "schematic.core.py"
    core.write_text(core.read_text() + far + "\n")
    n = len(core.read_text().splitlines())
    with pytest.raises(ValueError) as got:
        emit_from_design(load_board(board), title="node")
    msg = str(got.value)
    assert msg.startswith(f"schematic.core.py:{n}: SchLabel(") and "is on no wire of" in msg, msg
    assert "; on this sheet the group's wires are (" in msg and " mm from (" in msg, msg
    assert msg.endswith("; this sheet applies no other lock of schematic.core.py (the layout without its locks draws the group so)"), msg


def test_a_lock_refusal_names_the_locks_its_sheet_applies_and_where_the_wire_was_without_them():
    """`_lock_error_fact`: when the sheet applies other locks, the refusal names their lines, and the sheet laid out with
    fewer of them that had the point on the net's wire (the tool's own redraw moved it, not the label)."""
    import types

    from pcbc.sch_emit import _lock_error_fact

    err = "schematic.core.py:5: SchLabel('AIN0', pin='R6.2', at=(-4.58, 8.89)): (50.03, 52.07) is on no wire of R6.2's group"
    tag = "label:AIN0:R6.2"
    spec = lambda line: types.SimpleNamespace(line=line)  # noqa: E731
    sheet = lambda segs, recs: types.SimpleNamespace(segs=segs, records=recs)  # noqa: E731
    relaxed = {"sheet": sheet([(45.0, 52.07, 55.0, 52.07, "AIN0")], {}), "applied": {}}
    posed = {"sheet": sheet([(45.0, 49.53, 55.0, 49.53, "AIN0")], {tag: {"line": 5}}), "applied": {("SchPlace", "R6"): spec(3), ("SchLabel", "AIN0", "R6", "2"): spec(5)}}
    out = _lock_error_fact(err, tag, [relaxed, posed], posed["applied"])
    assert out == err + (
        "; this sheet applies the locks at schematic.core.py:3, which differ from the sheet laid out without them"
        "; laid out with none of them, (50.03, 52.07) is on the AIN0 wire (45, 52.07)-(55, 52.07)"
    ), out


def test_name_applied_poses_names_the_line_of_a_posed_part_in_every_finding():
    from pcbc.build import _name_applied_poses

    step = {
        "vector_detail": {"body_overlaps": ["R_BOOT overlaps U1 by 19.81 mm2", "R_EN overlaps C_EN by 1.00 mm2"], "text_collisions": ["pin U1.11 overlaps GND symbol at U10 by 1.0 mm2"]},
        "readability": ["GND symbol at U1 overlaps symbol U1 - no clear spot", "U10 is fine"],
    }
    step["vector_detail"]["region_frames"] = ["region mcu overlaps region analog", "region usb overlaps region pwr"]
    _name_applied_poses(step, {"U1": 16}, {"U1": "mcu", "J1": "usb"})
    assert step["vector_detail"]["region_frames"] == ["region mcu overlaps region analog (region mcu holds U1, posed by schematic.core.py:16)", "region usb overlaps region pwr"]
    assert step["vector_detail"]["body_overlaps"] == ["R_BOOT overlaps U1 by 19.81 mm2 (U1 is posed by schematic.core.py:16)", "R_EN overlaps C_EN by 1.00 mm2"]
    assert step["vector_detail"]["text_collisions"] == ["pin U1.11 overlaps GND symbol at U10 by 1.0 mm2 (U1 is posed by schematic.core.py:16)"]
    assert step["readability"] == ["GND symbol at U1 overlaps symbol U1 - no clear spot (U1 is posed by schematic.core.py:16)", "U10 is fine"]


@pytest.mark.kicad
def test_overlaps_a_pose_lock_makes_are_refused_naming_its_line(tmp_path: Path, capsys):
    """refute:split 2026-10-08: a pose lock that put U1 on R_EN and R_BOOT gave 17 overlap facts and none named the line.
    (Since 2026-10-09 node lays its other parts out round any U1 pose; buck's L1 posed onto U1 is the case now.) `pcbc
    sch` exits 1, every fact naming L1 names its schematic.core.py line, and the error names the locks the sheet
    applies and the hard count without them."""
    from pcbc.cli import main

    dst = tmp_path / "buck"
    shutil.copytree(EXAMPLES / "buck", dst, ignore=shutil.ignore_patterns("layout"))
    board = dst / "buck.py"
    gen = _gen_lines(board)
    reg = next(ln for ln in gen if ln.startswith("SchRegion(")).split("  #")[0]
    (dst / "schematic.core.py").write_text(reg + '\nSchPlace("L1", at=(5.08, 20.32), rotate=270, mirror=None)\n')
    assert main(["sch", str(board)]) == 1
    err = capsys.readouterr().err
    assert "schematic is not readable (" in err, err
    facts = err.split("): ", 1)[1].strip().split("; ")
    with_l1 = [f for f in facts if f.startswith(("body_overlaps:", "text_collisions:")) and "L1" in f]
    assert with_l1 and all(f.endswith("(L1 is posed by schematic.core.py:2)") for f in with_l1), err
    assert "this sheet applies the locks at schematic.core.py:1 (SchRegion('auto-U1')), schematic.core.py:2 (SchPlace('L1'))" in err, err