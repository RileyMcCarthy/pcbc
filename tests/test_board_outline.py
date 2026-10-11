"""docs/board-outline.md: placement lives in layout.core.py (Part A), and the outline is optional (Part B).

docs/direction.md §2 (2026-10-03): `board.py` is the circuit and its physics only; every geometric decision the AI
makes (`Place`, `Region`, `Keepout`, a declared `Outline`) is `layout.core.py`'s. Each refusal here is a fact naming
the line and the file the construct belongs in (§7b), and each test fails when the refusal it pins is removed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbc.language import check_board, load_board

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

# A two-part board, its circuit only.
CIRCUIT = """from pcbc import Board, Ground, NetReq, Power, Resistor, SchPlace
VCC = Power("VCC")
GND = Ground("GND")
Resistor("R1", "1k", package="0402", mpn="X", lcsc="C1", p1=VCC, p2=GND)
Resistor("R2", "1k", package="0402", mpn="X", lcsc="C1", p1=VCC, p2=GND)
Board(layers=2, stackup="jlcpcb_2l_1oz")
NetReq("VCC", kind="power", volts=3.3)
NetReq("GND", kind="power")
# The schematic's locks are schematic.core.py's (docs/schematic-layout.md), written beside it by _board.
# (two lines kept here so every later line keeps its number)
"""
SCH = """SchRegion("two", title="Two", members=("R1", "R2"))
"""
CORE = """Outline(width=30, height=20)
Place("R1", at=(10, 10))
Place("R2", at=(20, 10))
"""


def _board(tmp_path: Path, board: str = CIRCUIT, core: str | None = CORE, name: str = "two.py") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_text(board)
    (tmp_path / "schematic.core.py").write_text(SCH)
    if core is not None:
        (tmp_path / "layout.core.py").write_text(core)
    elif (tmp_path / "layout.core.py").exists():
        (tmp_path / "layout.core.py").unlink()
    return p


def _refusal(path: Path) -> str:
    with pytest.raises(ValueError) as got:
        load_board(path)
    return str(got.value)


def test_the_split_board_loads_with_its_placement_and_outline_from_core(tmp_path: Path):
    d = load_board(_board(tmp_path))
    assert [(p.ref, p.at, p.source, p.line) for p in d.places] == [("R1", (10.0, 10.0), "core", 2), ("R2", (20.0, 10.0), "core", 3)]
    assert (d.outline.width, d.outline.height, d.outline.at, d.outline.line) == (30.0, 20.0, (0.0, 0.0), 1)
    assert d.core_loaded is True and "layout.core.py" in d.core_reads
    assert "layout.core.py" not in d.reads, "board.py's own trace does not hold the core file (the sch stamp reads it)"
    assert check_board(tmp_path / "two.py") == []


# ------------------------------------------------------------------ A.3: board.py holds no geometry


@pytest.mark.parametrize(
    "line, want",
    [
        ('Place("R1", at=(10, 10))', "two.py:11: Place('R1') is placement; placement is layout.core.py's (next to two.py), and two.py holds the circuit and its physics only"),
        ('Region("HV", position="absolute", left=0, top=0, width=5, height=5)', "two.py:11: Region('HV') is geometry; Regions are layout.core.py's (next to two.py)"),
        ('Keepout("ANTENNA", box=(0, 0, 4, 4))', "two.py:11: Keepout('ANTENNA') is geometry; keepouts are layout.core.py's (next to two.py)"),
        ('Outline(width=30, height=20)', "two.py:11: Outline() is geometry; a declared outline is layout.core.py's (next to two.py)"),
        ('import pcbc.language as L\nL.Place("R1", at=(10, 10))', "two.py:12: Place('R1') is placement; placement is layout.core.py's"),
        ('import pcbc.language as L\nL.Keepout("K", box=(0, 0, 1, 1))', "two.py:12: Keepout('K') is geometry; keepouts are layout.core.py's"),
    ],
    ids=["Place", "Region", "Keepout", "Outline", "language.Place", "language.Keepout"],
)
def test_geometry_in_board_py_is_refused_by_any_path_naming_its_line_and_its_file(tmp_path: Path, line: str, want: str):
    """A.3: the call records a refusal and nothing else, and `load_board` raises once the file has run."""
    path = _board(tmp_path, CIRCUIT + line + "\n")
    got = _refusal(path)
    assert got.startswith(want), got
    assert check_board(path) == [got]


def test_from_pcbc_import_place_is_refused_on_the_import_line(tmp_path: Path):
    """A.3: `Place`, `Region`, `Keepout` and `Outline` left pcbc's exports; `pcbc.__getattr__` refuses them."""
    import pcbc

    for name in ("Place", "Region", "Keepout", "Outline"):
        assert name not in pcbc.__all__
        path = _board(tmp_path, CIRCUIT.replace("from pcbc import Board,", f"from pcbc import {name}, Board,"))
        assert _refusal(path) == f"two.py:1: pcbc exports no {name}: {name} is layout.core.py's (next to two.py), and two.py holds the circuit and its physics only"
    with pytest.raises(ValueError, match=r"^pcbc exports no Place: Place is layout.core.py's, and board.py holds the circuit and its physics only$"):
        pcbc.Place  # noqa: B018  (outside any load)
    with pytest.raises(AttributeError):
        pcbc.Nope  # noqa: B018


@pytest.mark.parametrize(
    "board_line, label",
    [
        ('Board(width=30, height=20, layers=2, stackup="jlcpcb_2l_1oz")', "Board(width=30, height=20)"),
        ('Board(size_mm=(30, 20), layers=2, stackup="jlcpcb_2l_1oz")', "Board(size_mm=(30, 20))"),
        ('Board((30, 20), layers=2, stackup="jlcpcb_2l_1oz")', "Board(size_mm=(30, 20))"),
        ('Board(layers=2, stackup="jlcpcb_2l_1oz", padding=1)', "Board(padding=1)"),
    ],
)
def test_a_board_size_in_board_py_is_refused_naming_where_the_outline_lives(tmp_path: Path, board_line: str, label: str):
    path = _board(tmp_path, CIRCUIT.replace('Board(layers=2, stackup="jlcpcb_2l_1oz")', board_line))
    assert _refusal(path) == (
        f"two.py:6: {label}: the outline is geometry; a declared outline is Outline() in layout.core.py (next to two.py); "
        "Board() takes layers, stackup, planes, pcb and net_order"
    )


def test_many_places_in_board_py_are_one_refusal_with_their_lines(tmp_path: Path):
    """A.3: the first of each kind with its line, the rest counted with theirs (ranges, each line once)."""
    extra = 'Place("R1", at=(1, 1))\nPlace("R2", at=(2, 1))\nPlace("R1", at=(3, 1))\n\nfor r in ("R1", "R2"):\n    Place(r, at=(4, 1))\n'
    got = _refusal(_board(tmp_path, CIRCUIT + extra))
    assert got.endswith("(and 4 more: lines 12-13, 16)"), got


def test_a_refused_board_py_puts_nothing_into_the_design(tmp_path: Path):
    """`language.Place` records nothing into the design while board.py runs: the code that put a board.py `Place`
    into `design.places` is deleted, not bypassed (A.3)."""
    import pcbc.language as L

    path = _board(tmp_path, CIRCUIT + 'Place("R1", at=(10, 10))\n', core=None)
    with pytest.raises(ValueError):
        load_board(path)
    assert L._current is not None and L._current.places == []


# ------------------------------------------------------------------ A.3/A.4: layout.core.py


@pytest.mark.parametrize(
    "core, want",
    [
        ('Outline(width=30, height=20)\nOutline(width=40, height=20)\n', "layout.core.py:2: Outline() is already declared at layout.core.py:1; a board has one outline"),
        ('Outline(width=30)\n', "layout.core.py:1: Outline() has no height"),
        ('Outline(height=20)\n', "layout.core.py:1: Outline() has no width"),
        (CORE + 'Place("R9", at=(1, 1))\n', "layout.core.py:4: Place('R9'): two.py has no part R9"),
        (CORE + 'Place("FID1", at=(1, 1), rot=0.0, layer="F.Cu", locked=False)\n', "layout.core.py:4: Place('FID1'): FID1 is a fiducial the build places, and two.py has no part FID1"),
        (CORE.replace('Place("R2", at=(20, 10))', 'Place("R2", at=(20, 10), side="B")'), 'layout.core.py:3: Place(\'R2\'): side= is now layer= ("F.Cu" or "B.Cu")'),
        (CORE.replace('Place("R2", at=(20, 10))', 'Place("R2", at=(20, 10), rotate=90)'), "layout.core.py:3: Place('R2'): rotate= is now rot="),
        (CORE.replace('Place("R2", at=(20, 10))', 'Place("R2", at=(20, 10), layer="In1.Cu")'), "layout.core.py:3: Place('R2'): layer='In1.Cu': a footprint's layer is F.Cu or B.Cu"),
        (CORE + 'Board(layers=2)\n', "layout.core.py:4: Board() is board.py's, not layout.core.py's"),
        (CORE + 'NetReq("VCC", kind="power")\n', "layout.core.py:4: NetReq() is board.py's, not layout.core.py's"),
        (CORE + 'Keepout("ANT", box=(0, 0, 4, 4))\nPour(None, layers=("F.Cu", "B.Cu"), name="ANT", keepout={"tracks": "not_allowed"}, points=[(0, 0), (4, 0), (4, 4), (0, 4)], id="k")\n',
         "layout.core.py:5: Pour(None, keepout=...) 'ANT' is the rule area gen draws from Keepout('ANT') at layout.core.py:4"),
        (CORE + 'Rect((1, 1), (5, 5), width=0.05, layer="Edge.Cuts")\n', "layout.core.py:4: Rect on Edge.Cuts; the outline is Outline() in layout.core.py, or derived when there is none"),
    ],
    ids=["two-outlines", "no-height", "no-width", "unknown-ref", "fiducial", "side", "rotate", "layer", "Board", "NetReq", "pasted-keepout-area", "edge-cuts"],
)
def test_a_core_line_that_is_not_geometry_or_is_malformed_is_refused_naming_its_line(tmp_path: Path, core: str, want: str):
    from pcbc.layout_job import load_core

    path = _board(tmp_path, core=core)
    with pytest.raises(ValueError) as got:
        design = load_board(path)
        load_core(design, path)
    assert str(got.value) == want, str(got.value)


def test_a_part_with_two_places_in_core_is_refused_naming_both_lines(tmp_path: Path):
    """A.3/A.5: a pasted gen pose replaces the part's Place line; a second one is refused naming both lines."""
    path = _board(tmp_path, core=CORE + 'Place("R1", at=(12.5, 10), rot=90.0, layer="F.Cu", locked=True)\n')
    assert "R1: Place() 2 times (layout.core.py lines 2, 4); a part takes one Place(), and pcbc does not pick between them" in check_board(path)


def test_the_gen_form_of_a_pose_is_the_core_form(tmp_path: Path):
    """A.4: `Place("C1", at=..., rot=..., layer=..., locked=...)` as gen writes it is a core Place: layer= sets the side,
    rot= the rotation, and locked=False only clears KiCad's flag (the pose is still fixed)."""
    path = _board(tmp_path, core=CORE.replace('Place("R2", at=(20, 10))', 'Place("R2", at=(20.5, 9.25), rot=270.0, layer="B.Cu", locked=False)'))
    d = load_board(path)
    r2 = next(p for p in d.places if p.ref == "R2")
    assert (r2.at, r2.rot, r2.side, r2.locked, r2.rot_set) == ((20.5, 9.25), 270.0, "B", False, True)
    assert check_board(path) == []


def test_board_py_and_core_are_two_traces_and_a_core_edit_is_stale_from_place_never_from_sch(tmp_path: Path):
    """A.2: the schematic stamp records `design.reads` (board.py's load); the place stamp also records the core
    file's sha and `design.core_reads`. A core edit leaves the schematic current and makes place stale."""
    from pcbc.build import pcb_job, sch_stage, stale_reason

    shutil.copytree(EXAMPLES / "blinky", tmp_path / "blinky", ignore=shutil.ignore_patterns("layout"))
    board = tmp_path / "blinky" / "blinky.py"
    layout = board.parent / "layout" / "blinky"
    assert not pcb_job(board).get("error")
    assert sch_stage(load_board(board), board, layout, name="blinky")[1] is None
    assert stale_reason(layout, board) == ("place", None)
    core = board.parent / "layout.core.py"
    core.write_text(core.read_text().replace("left=8,", "left=9,", 1))
    assert stale_reason(layout, board) == ("sch", "layout.core.py changed since the board was placed")


# ------------------------------------------------------------------ A.2: the schematic never loads core


def test_a_broken_core_never_blocks_the_schematic_entry_points(tmp_path: Path):
    """A.2: `check_board(pcb=False)`, `pcbc sch` and the sch stage load board.py with `core=False`; a core error is
    reported by every PCB entry point and by none of the schematic ones."""
    from pcbc.build import build_job, sch_stage
    from pcbc.cli import main

    path = _board(tmp_path, core=CORE + "Outline(width=1)\n")
    assert check_board(path, pcb=False) == []
    d = load_board(path, core=False)
    assert d.core_loaded is False and d.places == [] and d.outline is None and d.core_reads == {}
    step, err = sch_stage(d, path, tmp_path / "layout" / "two", name="two")
    assert err is None, err
    assert main(["sch", str(path)]) == 0
    assert any("layout.core.py:4" in f for f in check_board(path))
    assert "layout.core.py:4" in build_job(path, upto="place", force=True)["error"]


def test_with_core_not_loaded_an_isolation_compiles_nothing_of_cores(tmp_path: Path):
    """A.2: with `core=False` an Isolation's Regions (core's) are not resolved, so nothing is refused for them."""
    from pcbc.constraints import compile_constraints

    board = CIRCUIT + 'Isolation("A", "B", volts=50, across=("R1",))\n'
    path = _board(tmp_path, board, core=None)
    assert not any("Isolation" in r for r in compile_constraints(load_board(path, core=False)).refusals)
    refusals = compile_constraints(load_board(path)).refusals
    assert 'two.py:11: Isolation("A", "B") names Region "A", and there is no layout.core.py next to two.py' in refusals


# ================================================================== Part B: the outline is optional

import re  # noqa: E402


# docs/board-outline.md B.7: the anchor changes a board needs to drop its size (each the pose it resolved to).
B7_ANCHORS = {
    "blinky": [
        (re.compile(r'Place\(\n    "R1",.*?\n\)\n', re.S), 'Place("R1", position="absolute", left=8, top=12.03, locked=True, reason="resistor west of LED")\n'),
        (re.compile(r'Place\(\n    "D1",.*?\n\)\n', re.S), 'Place("D1", to="R1.2", reason="LED beside its resistor")\n'),
    ],
    "c3_usb": [(re.compile(re.escape('Place("U2", position="absolute", right=4, top=10, reason="LDO")')), 'Place("U2", at=(33.95, 11.7), reason="LDO")')],
    "node": [
        (re.compile(re.escape('Place("U2", position="absolute", left=26, bottom=12, reason="LDO between the USB-C and the module")')), 'Place("U2", at=(28.05, 31.3), reason="LDO between the USB-C and the module")'),
        (re.compile(re.escape('Place("U4", position="absolute", right=4, top=4, reason="BME280 in the corner, away from the regulator")')), 'Place("U4", at=(54.75, 5.25), reason="BME280 in the corner, away from the regulator")'),
    ],
}


def derived_copy(tmp_path: Path, name: str, edits=(), *, keep_outline: bool = False) -> Path:
    """examples/<name> copied, with no `Outline()` in its layout.core.py and the B.7 anchors, then `edits` (old, new)
    applied to the core file. The same board whether or not the example has already dropped its size."""
    dst = tmp_path / name
    shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns("layout"))
    core = dst / "layout.core.py"
    text = core.read_text()
    if not keep_outline:
        # as the examples dropped theirs (PR 4): the line kept, as a comment, so every other line keeps its number
        text = re.sub(r"^Outline\(.*\)$", "# No Outline(): the outline is derived from the placement and the routing (docs/board-outline.md B).", text, flags=re.M)
    elif not re.search(r"^Outline\(", text, flags=re.M):
        w, h = {"blinky": (40, 25), "buck": (40, 25), "c3_usb": (40, 30), "node": (60, 45)}[name]  # their sizes before PR 4
        text = f"Outline(width={w}, height={h})\n" + text
    for pat, new in B7_ANCHORS.get(name, ()):
        text = pat.sub(lambda _m: new, text)
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    core.write_text(text)
    return dst / f"{name}.py"


def _place(board: Path):
    from pcbc.place_native import place

    return place(load_board(board), name=board.stem)


def test_derived_buck_places_as_the_design_measured_it(tmp_path: Path):
    """B.2 and B.7 on buck: J_IN and J_OUT dock against the interior and fix the left and right sides; the top and
    bottom are free; the placement-only fit is 26.81 x 18.00 mm at (2.31, 5.43), and L1 stands where the order puts it
    (the evidence recorded in B.2's "What v1 does not do")."""
    from pcbc.outline import line

    pl = _place(derived_copy(tmp_path, "buck"))
    assert pl.error is None, pl.error
    d = pl.job.derived
    assert d.fixed == {"left": 2.31, "right": 29.12}
    assert {k: (v["by"], v["what"]) for k, v in d.fixed_by.items()} == {"left": ("J_IN", "edge face"), "right": ("J_OUT", "edge face")}
    assert d.fit == (2.31, 5.43, 29.12, 23.43) and (d.A, d.m) == (3.18, 0.7)
    assert d.R == (2.31, 2.75, 29.12, 26.16), "R: fixed lines, free sides at P plus A"
    poses = {p.ref: (p.at, p.rot) for p in pl.poses}
    assert poses["J_IN"] == ((6.01, 8.285), 180.0) and poses["J_OUT"] == ((25.42, 9.335), 0.0) and poses["U1"] == ((16.46, 8.81), 0.0)
    assert poses["L1"] == ((13.21, 15.038), 90.0)
    assert [f[0] for f in d.fids] == ["FID1", "FID2", "FID3"]
    assert pl.job.outline == d.R, "placement and routing read R"
    assert line(d, d.fit, d.sides, placement_only=True) == (
        "outline: derived 26.81 x 18.00 mm at (2.31, 5.43); left: J_IN face (edge=, layout.core.py:6); top: R_EN.1 pad at y=6.13 + 0.70 margin; "
        "right: J_OUT face (edge=, layout.core.py:7); bottom: C_OUT2.2 pad at y=22.73 + 0.70 margin; placement-only fit; the free sides are fitted again after routing"
    )


@pytest.mark.parametrize(
    "name, want",
    [
        ("blinky", (10.98, 14.54)),
        # Re-recorded 2026-10-07: c3_usb 28.21 -> 28.89 high, because U1's decoupling caps now stand beside U1.3V3 (1.61
        # and 2.39 mm, as on the declared board) where I0 left them 8.9 and 9.5 mm away; node 56.69 -> 56.70 wide, because
        # a fixed line is on the 0.01 mm grid, outward (U4's pad floor was 56.6935, which froze 0.0035 mm inside it).
        ("c3_usb", (36.45, 28.89)),
        ("node", (56.7, 45.94)),
    ],
)
def test_the_examples_derived_placement_matches_b7(tmp_path: Path, name: str, want):
    """B.7's placement-level outlines (the lower bound: no copper past the pads plus m), with B.7's anchors."""
    pl = _place(derived_copy(tmp_path, name))
    assert pl.error is None, pl.error
    d = pl.job.derived
    x0, y0, x1, y1 = d.fit
    assert (round(x1 - x0, 2), round(y1 - y0, 2)) == want
    if name in ("c3_usb", "node"):
        # B.2 step 6: the antenna keep-out reaches the left and top at least as far as every courtyard, so those sides
        # are fixed at its face, and copper is never routed beside the antenna on the outside (R holds them there)
        assert {s: (d.fixed_by[s]["by"], d.fixed_by[s]["what"], d.fixed[s]) for s in ("left", "top")} == {
            "left": ("ANTENNA", "keepout face", 0.0), "top": ("ANTENNA", "keepout face", 0.0)}
        assert d.R[:2] == (0.0, 0.0)


@pytest.mark.parametrize(
    "ref, dx, moved",
    [
        ("U4", 2.0, {"C_BME": 2.0, "FID1": 2.0, "FID3": 1.0, "J1": 1.0, "J_LOAD": 2.0, "Q1": 2.0, "R_CC1": 1.0, "R_CC2": 1.0, "R_G": 2.0, "R_PD": 2.0, "R_SCL": 2.0, "R_SDA": 2.0, "U3": 1.0, "U4": 2.0}),
        ("U2", 3.0, {"C_3V3": 3.0, "C_3V3_HF": 3.0, "C_VBUS": 3.0, "C_VBUS_HF": 3.0, "U2": 3.0}),
        ("U2", -6.0, {"C_3V3": 6.0, "C_3V3_HF": 6.0, "C_VBUS": 6.0, "C_VBUS_HF": 6.0, "FID1": 2.46, "FID2": 2.46, "J1": 2.46, "R_CC1": 2.46, "R_CC2": 2.46, "U2": 6.0, "U3": 2.46}),
    ],
)
def test_moving_one_anchor_moves_its_own_tree_and_only_the_groups_it_sets(tmp_path: Path, ref: str, dx: float, moved: dict):
    """B.2 "Stability", pinned on node: a group moves rigidly, and only when the anchor sets I0's span (the group's
    centre) or I's side (its docking depth). U4 +2 mm moves J_LOAD's group 2.0 and J1's 1.0 (half the span change); U2
    +3 mm moves its four caps only; U2 -6 mm docks J1's group 2.46 mm higher."""
    import math

    at = {"U4": (54.75, 5.25), "U2": (28.05, 31.3)}[ref]
    a = {p.ref: p.at for p in _place(derived_copy(tmp_path / "a", "node")).poses}
    edit = (f'Place("{ref}", at=({at[0]:g}, {at[1]:g})', f'Place("{ref}", at=({at[0] + dx:g}, {at[1]:g})')
    pb = _place(derived_copy(tmp_path / "b", "node", [edit]))
    b = {p.ref: p.at for p in pb.poses}
    got = {r: round(math.hypot(b[r][0] - a[r][0], b[r][1] - a[r][1]), 2) for r in a if math.hypot(b[r][0] - a[r][0], b[r][1] - a[r][1]) > 0.01}
    assert got == moved
    assert pb.job.derived.fixed["bottom"] == (43.48 if dx == -6.0 else 45.94)


def test_the_derivation_is_deterministic_from_two_directories(tmp_path: Path):
    """B.3 Determinism, at placement: the same board in two working directories places the same, to the digit."""
    a = _place(derived_copy(tmp_path / "one", "node"))
    b = _place(derived_copy(tmp_path / "two" / "deeper", "node"))
    assert [(p.ref, p.at, p.rot) for p in a.poses] == [(p.ref, p.at, p.rot) for p in b.poses]
    assert (a.job.derived.R, a.job.derived.fit, a.job.derived.fids) == (b.job.derived.R, b.job.derived.fit, b.job.derived.fids)


# ------------------------------------------------------------------ B.5: the errors


@pytest.mark.parametrize(
    "old, new, want",
    [
        ('Place("U1", position="absolute", left=15, top=8,', 'Place("U1", position="absolute", right=15, top=8,',
         "layout.core.py:3: Place('U1') right=15 measures from the outline's right edge; this board has no Outline(), and its right edge is derived from the placement"),
        ('Place("U1", position="absolute", left=15, top=8,', 'Place("U1", position="absolute", left=15, bottom=8,',
         "layout.core.py:3: Place('U1') bottom=8 measures from the outline's bottom edge; this board has no Outline(), and its bottom edge is derived from the placement"),
        ('Place("U1", position="absolute", left=15, top=8,', 'Place("U1", position="absolute", top=8,',
         "layout.core.py:3: Place('U1') has no left= or at=: with no Outline() there is no width to centre it in"),
        ('Place("U1", position="absolute", left=15, top=8,', 'Place("U1", position="absolute", left=15,',
         "layout.core.py:3: Place('U1') has no top= or at=: with no Outline() there is no height to centre it in"),
        ('Place("U1", position="absolute", left=15, top=8,', 'Place("U1", position="absolute", left="40%", top=8,',
         "layout.core.py:3: Place('U1') left= is a % of the outline; this board has no Outline(), and its right edge is derived from the placement"),
        ('Place("U1", position="absolute", left=15, top=8,', 'Place("U1", position="absolute", left=15, top=8, margin_left="auto", margin_right="auto",',
         "layout.core.py:3: Place('U1') margin-left: auto centres it in the outline; this board has no Outline(), and its right edge is derived from the placement"),
        ('Place("J_IN", edge="left",', 'Place("J_IN", edge="left", bottom=3,',
         "layout.core.py:6: Place('J_IN') bottom=3 measures from the outline's bottom edge; this board has no Outline(), and its bottom edge is derived from the placement"),
    ],
    ids=["right", "bottom", "no-left", "no-top", "percent", "auto", "edge-along-bottom"],
)
def test_b5_1_an_anchor_that_needs_an_edge_is_refused(tmp_path: Path, old: str, new: str, want: str):
    from pcbc.pcb_place import validate

    board = derived_copy(tmp_path, "buck", [(old, new)])
    assert want in validate(load_board(board))
    assert not validate(load_board(derived_copy(tmp_path / "ok", "buck")))
    # with an Outline() the same line measures from the declared edge and is not refused
    assert not [f for f in validate(load_board(derived_copy(tmp_path / "decl", "buck", [(old, new)], keep_outline=True))) if "no Outline()" in f]


@pytest.mark.parametrize(
    "line, want",
    [
        ('Region("HV", position="absolute", right=2, top=2, width=5, height=5)', "layout.core.py:17: Region('HV') right=2 measures from the outline's right edge"),
        ('Region("HV", position="absolute", left=2, top=2, width=5)', "layout.core.py:17: Region('HV') has no height=: with no Outline() there is no height to stretch it to"),
        ('Keepout("K", position="absolute", left=2, bottom=0, width=4, height=4)', "layout.core.py:17: Keepout('K') bottom=0 measures from the outline's bottom edge"),
    ],
)
def test_b5_1_a_region_or_keepout_needs_frame_numbers_too(tmp_path: Path, line: str, want: str):
    from pcbc.compile import compile_design

    board = derived_copy(tmp_path, "buck")
    core = board.parent / "layout.core.py"
    core.write_text(core.read_text() + line + "\n")
    with pytest.raises(ValueError) as got:
        compile_design(load_board(board))
    assert str(got.value).startswith(want), str(got.value)
    core.write_text(core.read_text().replace(line, 'Keepout("K", box=(2, 2, 4, 4))'))
    compile_design(load_board(board))


def test_b5_2_a_part_with_no_place_has_no_position(tmp_path: Path):
    pl = _place(derived_copy(tmp_path, "buck", [('Place("R_FB_BOT", to="U1.FB")\n', "")]))
    assert pl.error == "R_FB_BOT has no Place() in layout.core.py; with no Outline() the outline is derived from where the parts are, and this has no position"


def test_b5_3_an_edge_part_with_nothing_to_define_its_edge(tmp_path: Path):
    board = derived_copy(tmp_path, "blinky")
    (board.parent / "layout.core.py").write_text('Place("R1", edge="left")\nPlace("D1", to="R1.2")\n')
    pl = _place(board)
    assert pl.error == (
        'layout.core.py:1: R1 (edge="left") has no edge to stand on: this board has no Outline(), and no part placed by at=, left/top or a Region '
        "and no keepout, so nothing places the left edge"
    )
    (board.parent / "layout.core.py").write_text('Place("R1", edge="left")\nPlace("D1", at=(10, 10))\n')
    assert _place(board).error is None


def test_b5_4_a_group_that_crosses_another_sides_fixed_line(tmp_path: Path):
    """C_IN1's bottom group docks first, against an interior of U1 alone; J_OUT's group (L1 attached to it) then hangs
    below the bottom line it set."""
    core = (
        'Place("U1", position="absolute", left=15, top=8, locked=True)\nPlace("C_IN1", edge="bottom")\nPlace("J_IN", edge="left")\n'
        'Place("J_OUT", edge="right")\nPlace("C_IN2", to="J_IN.1")\nPlace("L1", to="J_OUT.1")\nPlace("C_OUT1", to="L1.2")\n'
        'Place("C_OUT2", to="L1.2")\nPlace("C_BOOT", to="U1.BOOT")\nPlace("R_EN", to="U1.EN")\nPlace("R_FB_TOP", to="U1.FB")\nPlace("R_FB_BOT", to="U1.FB")\n'
    )
    board = derived_copy(tmp_path, "buck")
    (board.parent / "layout.core.py").write_text(core)
    assert _place(board).error == 'L1 (to="J_OUT.1", layout.core.py:6) reaches y=20.57, past the bottom edge at y=14.15 that C_IN1 (edge="bottom", layout.core.py:2) sets'
    (board.parent / "layout.core.py").write_text(core.replace('Place("L1", to="J_OUT.1")', 'Place("L1", to="U1.SW")'))
    assert _place(board).error is None


def test_b5_5_core_copper_outside_a_fixed_side_is_refused_and_beyond_a_free_one_grows_it(tmp_path: Path):
    from pcbc.layout_job import layout_job

    board = derived_copy(tmp_path, "buck")
    core = board.parent / "layout.core.py"
    core.write_text('Seg("GND", (1.0, 10.0), (3.0, 10.0), width=0.3, layer="F.Cu")\n' + core.read_text())
    r = layout_job(load_board(board), out=board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb", board=board)
    assert r["error"] == (
        'a core line is refused: layout.core.py:1: Seg on GND reaches x=0.85, outside the left edge at x=2.31 that J_IN (edge="left", layout.core.py:7) sets'
    )


def test_b5_7_a_slot_that_does_not_fit_the_derived_outline(tmp_path: Path):
    from types import SimpleNamespace

    from pcbc.outline import slot_refusal

    req = SimpleNamespace(a="HV", b="LV", line=12, slot=True)
    job = SimpleNamespace(constraints=SimpleNamespace(isolation_specs=(SimpleNamespace(req=req, axis="x"),)))
    design = SimpleNamespace(source=str(tmp_path / "iso.py"))
    assert slot_refusal(design, job, (0.0, 0.0, 20.0, 5.5)) == "Isolation HV/LV (iso.py:12): the slot needs 3.00 mm of board at each end, and the derived outline is 5.50 mm along y"
    assert slot_refusal(design, job, (0.0, 0.0, 20.0, 6.01)) is None
    req.slot = False
    assert slot_refusal(design, job, (0.0, 0.0, 20.0, 5.5)) is None


# ------------------------------------------------------------------ B.1: frame coordinates in both modes


def test_left_and_top_are_frame_coordinates_and_right_measures_from_a_declared_outline_at_its_corner(tmp_path: Path):
    """B.1/B.6: `Outline(at=)` moves the declared rectangle; `at=`, `left=` and `top=` keep their frame numbers, and
    `right=`/`bottom=` measure from the moved edge. Freezing a derived outline (`Outline(width=, height=, at=)` from the
    report line) keeps every anchor's pose."""
    core = 'Outline(width=30, height=20, at=(5, 7))\nPlace("R1", position="absolute", left=10, top=10)\nPlace("R2", position="absolute", right=2, bottom=3)\n'
    board = _board(tmp_path, core=core)
    pl = _place(board)
    assert pl.error is None, pl.error
    from pcbc.pcb_place import posed_feet_of

    feet = posed_feet_of(pl.job, pl.feet)
    assert tuple(round(v, 4) for v in feet["R1"].world_box()[:2]) == (10.0, 10.0)
    assert tuple(round(v, 4) for v in feet["R2"].world_box()[2:]) == (33.0, 24.0), "right/bottom from x1 = 35, y1 = 27"
    assert pl.job.outline == (5.0, 7.0, 35.0, 27.0) and pl.job.outline_source == "declared"
    edge = next(g for g in pl.graphics if g.f.get("layer") == "Edge.Cuts")
    assert (edge.f["start"], edge.f["end"]) == ((5.0, 7.0), (35.0, 27.0))


def test_freezing_the_derived_outline_keeps_every_anchor(tmp_path: Path):
    pl = _place(derived_copy(tmp_path / "d", "buck"))
    x0, y0, x1, y1 = pl.job.derived.fit
    frozen = derived_copy(tmp_path / "f", "buck")
    core = frozen.parent / "layout.core.py"
    core.write_text(f"Outline(width={x1 - x0:.2f}, height={y1 - y0:.2f}, at=({x0:.2f}, {y0:.2f}))\n" + core.read_text())
    pf = _place(frozen)
    assert pf.error is None and tuple(round(v, 6) for v in pf.job.outline) == (x0, y0, x1, y1)
    assert {p.ref: p.at for p in pf.poses}["U1"] == {p.ref: p.at for p in pl.poses}["U1"]


# ------------------------------------------------------------------ B.4: what is written where


def test_the_gen_header_names_the_outline_source():
    from pcbc.gen import gen_source, outline_header

    assert "# Edge.Cuts outline: derived (no Outline() in layout.core.py)." in gen_source([], [], board="b", outline=outline_header("derived"))
    assert "# Edge.Cuts outline: declared, layout.core.py:2." in gen_source([], [], board="b", outline=outline_header("declared", 2))
    assert "Edge.Cuts outline:" not in gen_source([], [], board="b")


def test_fab_and_review_read_the_outline_off_edge_cuts():
    """B.4: the outline fab and review report is the emitted board's Edge.Cuts rect (the largest; a slot is smaller),
    and fab's fiducials go `inset` inside it wherever it sits in the frame."""
    from pcbc.fab import insert_fiducials
    from pcbc.sexp import edge_outline

    rect = '\t(gr_rect\n\t\t(start {} {})\n\t\t(end {} {})\n\t\t(stroke (width 0.05) (type default))\n\t\t(fill none)\n\t\t(layer "Edge.Cuts")\n\t)\n'
    text = "(kicad_pcb\n" + rect.format(10, 8, 10.6, 20) + rect.format(2.31, 4.89, 29.12, 24.68) + rect.format(0, 0, 50, 50).replace("Edge.Cuts", "Dwgs.User") + ")\n"
    assert edge_outline(text) == (2.31, 4.89, 29.12, 24.68)
    assert edge_outline("(kicad_pcb\n)\n") is None
    _out, spots = insert_fiducials("(kicad_pcb\n)\n", (2.31, 4.89, 29.12, 24.68))
    assert [(round(x, 2), round(y, 2)) for _n, x, y in spots] == [(25.12, 8.89), (25.12, 20.68), (6.31, 20.68)]


def test_the_routing_box_note_names_the_side_copper_reached():
    from pcbc.outline import Derived, routing_box_notes

    d = Derived(fixed={"left": 0.0}, A=3.18, pitch=0.36, e=0.3)
    R = (0.0, 0.0, 20.0, 20.0)
    assert routing_box_notes(d, R, [("seg", (5.0, 5.0, 19.5, 6.0))]) == ["routed copper reached the routing box on the right side (x=19.50): the box is the placement plus 3.18 mm"]
    assert routing_box_notes(d, R, [("seg", (5.0, 5.0, 19.0, 6.0))]) == []
    assert routing_box_notes(d, R, [("seg", (-1.0, 5.0, 2.0, 6.0))]) == [], "a fixed side is the route scene's outline, never a note"


def test_pcbc_pcb_reports_the_placement_only_fit(tmp_path: Path):
    from pcbc.build import pcb_job

    r = pcb_job(derived_copy(tmp_path, "buck"))
    assert r["error"] is None or "unrouted" not in r["error"]
    assert r["outline"]["source"] == "derived" and r["outline"]["rect"] == [2.31, 5.43, 29.12, 23.43]
    assert r["outline"]["line"].endswith("placement-only fit; the free sides are fitted again after routing")
    gen = (tmp_path / "buck" / "layout" / "buck" / "placed" / "layout.gen.py").read_text()
    assert "# Edge.Cuts outline: derived (no Outline() in layout.core.py)." in gen
    assert 'Rect((2.31, 5.43), (29.12, 23.43)' in gen


# ------------------------------------------------------------------ B.3 on a routed derived board (KiCad judges it)

_BUILT: dict = {}


def _built(key: str, board: Path) -> tuple[Path, dict]:
    """(the routed board, the build result) of `board`, built once per session to the route stage."""
    from pcbc.build import build_job

    if key not in _BUILT:
        r = build_job(board, upto="route", force=True)
        assert r.get("error") is None or str(r["error"]).startswith("unrouted:"), r.get("error")
        _BUILT[key] = (board.parent / "layout" / board.stem / "routed" / "layout.kicad_pcb", r)
    return _BUILT[key]


def _fills(text: str, net: str, layer: str) -> list[list[tuple[float, float]]]:
    """The filled pieces of `net`'s pour on `layer`, as KiCad filled and saved them."""
    from pcbc.sexp import parse_tree

    out = []
    for node in parse_tree(text):
        if not (isinstance(node, list) and node and node[0] == "zone"):
            continue
        fields = {x[0]: x for x in node[1:] if isinstance(x, list) and x}
        if fields.get("net_name", [None, None])[1] != net and fields.get("net", [None, None])[-1] != net:
            continue
        for fp in (x for x in node[1:] if isinstance(x, list) and x and x[0] == "filled_polygon"):
            lay = next((y[1] for y in fp[1:] if isinstance(y, list) and y[0] == "layer"), None)
            if lay != layer:
                continue
            pts = next(y for y in fp[1:] if isinstance(y, list) and y[0] == "pts")
            out.append([(float(p[1]), float(p[2])) for p in pts[1:] if isinstance(p, list) and p[0] == "xy"])
    return out


def _inside(pt, poly) -> bool:
    x, y = pt
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def _runs_from_edge(poly_list, x, y_edge, sign, depth=3.0, step=0.01):
    """The filled run along x = `x` that starts within `step` of `y_edge` and goes inward (`sign`), in mm; 0 with none."""
    n = 0
    y = y_edge + sign * step / 2
    if not any(_inside((x, y), p) for p in poly_list):
        return 0.0
    while n * step < depth and any(_inside((x, y + sign * n * step), p) for p in poly_list):
        n += 1
    return n * step


@pytest.mark.kicad
def test_a_derived_board_routes_and_fits_deterministically_from_two_directories(tmp_path_factory):
    """B.3 Determinism: the same derived board built from two working directories gives the same gen and the same
    board, byte for byte; and KiCad's DRC finds no copper_edge_clearance on it (B.3 step 5)."""
    one = derived_copy(tmp_path_factory.mktemp("one"), "buck")
    two = derived_copy(tmp_path_factory.mktemp("two") / "deeper", "buck")
    pa, ra = _built("buck-one", one)
    pb, rb = _built("buck-two", two)
    assert (pa.parent / "layout.gen.py").read_bytes() == (pb.parent / "layout.gen.py").read_bytes()
    assert pa.read_bytes() == pb.read_bytes()
    assert ra["outline"] == rb["outline"] and ra["outline"]["source"] == "derived"
    route = next(s for s in ra["steps"] if s["stage"] == "route")
    assert not [k for k in (route.get("drc_by_type") or {}) if "edge_clearance" in k], route.get("drc_by_type")
    from pcbc.sexp import edge_outline

    assert list(edge_outline(pa.read_text())) == ra["outline"]["rect"], "the board's Edge.Cuts is O"
    x0, y0, x1, y1 = ra["outline"]["rect"]
    assert (x0, x1) == (2.31, 29.12), "the fixed sides do not move in the fit"
    gen = (pa.parent / "layout.gen.py").read_text()
    assert "# Edge.Cuts outline: derived (no Outline() in layout.core.py)." in gen and f"Rect(({x0:g}, {y0:g}), ({x1:g}, {y1:g})" in gen
    stamp = __import__("json").loads((pa.parent.parent / "inputs.json").read_text()) if (pa.parent.parent / "inputs.json").exists() else None
    if stamp is not None:
        assert stamp["outline"] == {"source": "derived", "rect": [x0, y0, x1, y1]}


@pytest.mark.kicad
def test_the_pour_fills_the_derived_outline_as_it_fills_a_wider_one(tmp_path_factory):
    """B.3 step 4: the derived board and the same board with `Outline(O + 5 mm)`. For the GND pour (B.Cu) each filled
    piece touches the same pads in both, and along each free side the fill that reaches the plane's edge is at least
    min_thickness wide in KiCad's own fill."""
    from pcbc.stackup import get_stackup

    derived = derived_copy(tmp_path_factory.mktemp("one"), "buck")
    pa, ra = _built("buck-one", derived)
    x0, y0, x1, y1 = ra["outline"]["rect"]
    wide = derived_copy(tmp_path_factory.mktemp("wide"), "buck")
    core = wide.parent / "layout.core.py"
    core.write_text(f"Outline(width={x1 - x0 + 10:.2f}, height={y1 - y0 + 10:.2f}, at=({x0 - 5:.2f}, {y0 - 5:.2f}))\n" + core.read_text())
    pw, rw = _built("buck-wide", wide)
    from pcbc.language import load_board as _lb
    from pcbc.place_native import place

    def pads_by_piece(pcb: Path, board: Path):
        pl = place(_lb(board), name="buck")
        from pcbc.pcb_place import posed_feet_of
        from pcbc.outline import pad_boxes

        feet = posed_feet_of(pl.job, pl.feet)
        gnd = [(f"{r}.{n}", b) for r, f in feet.items() for n, b in pad_boxes(f) if any(p.num == n and p.net == "GND" for p in f.pads) and r.startswith("J")]
        pieces = _fills(pcb.read_text(), "GND", "B.Cu")
        out = []
        for poly in pieces:
            touch = sorted(name for name, b in gnd if any(b[0] - 0.3 <= px <= b[2] + 0.3 and b[1] - 0.3 <= py <= b[3] + 0.3 for px, py in poly))
            if touch:
                out.append(tuple(touch))
        return sorted(out)

    assert pads_by_piece(pa, derived) == pads_by_piece(pw, wide)
    stack = get_stackup("jlcpcb_2l_1oz")
    e, tmin = stack.edge_clearance, stack.pour_min_mm
    pieces = _fills(pa.read_text(), "GND", "B.Cu")
    widths = []
    for side, y_edge, sign in (("top", y0 + e, 1.0), ("bottom", y1 - e, -1.0)):
        x = x0 + 1.0
        while x < x1 - 1.0:
            w = _runs_from_edge(pieces, x, y_edge, sign)
            if w:
                widths.append((w, side, round(x, 2)))
            x += 0.25
    assert widths, "the pour reaches the free sides"
    assert min(widths)[0] >= tmin - 0.011, min(widths)


def test_isolation_corridors_run_to_the_outline_in_use():
    """B.3 step 5: an isolation corridor's across-span is its Regions', its along-span the outline: R while placing and
    routing, O after the fit (`respan_rule_areas`); a declared outline gives compile's boxes back unchanged."""
    from types import SimpleNamespace

    from pcbc.constraints import RuleArea, respan_rule_areas

    spec = SimpleNamespace(req=SimpleNamespace(a="HV", b="LV"), axis="x")
    cs = SimpleNamespace(rule_areas=(RuleArea("ISO_HV_LV", (18.0, 0.0, 21.0, 0.0), ("F&B.Cu",), ("track",), "r"),), isolation_specs=(spec,))
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class CS:
        rule_areas: tuple
        isolation_specs: tuple

    got = respan_rule_areas(CS(cs.rule_areas, cs.isolation_specs), (2.0, 3.0, 40.0, 27.5))
    assert got.rule_areas[0].box == (18.0, 3.0, 21.0, 27.5)
    spec.axis = "y"
    got = respan_rule_areas(CS(cs.rule_areas, (spec,)), (2.0, 3.0, 40.0, 27.5))
    assert got.rule_areas[0].box == (2.0, 0.0, 40.0, 0.0)


def test_a_declared_outline_is_held_to_the_nanometre():
    """`Outline(at=)` + `width=` in floats (-2.69 + 36.81 = 34.120000000000005) is a number KiCad's board text cannot
    hold; the outline is rounded to 1 nm, so layout.gen.py loads back as written (found building B.3's pour test)."""
    from types import SimpleNamespace

    from pcbc.layout import declared_outline
    from pcbc.model import OutlineSpec

    d = SimpleNamespace(outline=OutlineSpec(width=36.81, height=29.79, at=(-2.69, -0.11)), board=None)
    assert declared_outline(d)[0] == (-2.69, -0.11, 34.12, 29.68)


def test_the_edge_clearance_is_the_stackups(monkeypatch):
    """B.3: `pcb_place.EDGE_CLEAR` and the stackup were two homes for one number; placement reads the stackup's."""
    from types import SimpleNamespace

    from pcbc import pcb_place
    from pcbc.stackup import get_stackup

    stack = get_stackup("jlcpcb_2l_1oz")
    assert pcb_place.edge_clear(SimpleNamespace(stackup="jlcpcb_2l_1oz")) == stack.edge_clearance == 0.3
    monkeypatch.setattr(pcb_place, "get_stackup", lambda name: SimpleNamespace(edge_clearance=0.5))
    assert pcb_place.edge_clear(SimpleNamespace(stackup="jlcpcb_2l_1oz")) == 0.5


def test_placement_reads_the_edge_clearance_from_the_stackup(tmp_path: Path, monkeypatch):
    """The copper-to-edge clearance placement keeps (a relation's search box inside the outline, an edge part's step in
    from its edge) is the stackup's number, read through `edge_clear`: another number places the declared buck
    differently."""
    from pcbc import pcb_place

    a = {p.ref: p.at for p in _place(derived_copy(tmp_path / "a", "buck", keep_outline=True)).poses}
    monkeypatch.setattr(pcb_place, "edge_clear", lambda job: 3.0)
    b = {p.ref: p.at for p in _place(derived_copy(tmp_path / "b", "buck", keep_outline=True)).poses}
    assert a != b


def test_a_relation_keeps_the_stackups_edge_clearance(tmp_path: Path, monkeypatch):
    """A part placed `to=` a pin searches inside the outline less the copper-to-edge clearance, the stackup's
    (`edge_clear`); a board with no edge part shows it alone: R2 attaches to R1 in the corner either way, and where
    it stands moves with the number."""
    from pcbc import pcb_place

    core = 'Outline(width=30, height=20)\nPlace("R1", position="absolute", left=0.4, top=0.4)\nPlace("R2", to="R1.1")\n'
    a = {p.ref: p.at for p in _place(_board(tmp_path / "a", core=core)).poses}
    monkeypatch.setattr(pcb_place, "edge_clear", lambda job: 2.5)
    b = {p.ref: p.at for p in _place(_board(tmp_path / "b", core=core)).poses}
    assert a["R1"] == b["R1"] and a["R2"] != b["R2"], (a, b)


def test_core_copper_beyond_a_free_side_grows_it(tmp_path: Path):
    """B.5-5's other half: core copper past a free side is design, and the side grows to hold it (plus m), at placement
    already, so the routing box R holds it too (found when blinky's core Seg at y=2 met the scene's edge)."""
    board = derived_copy(tmp_path, "buck")
    core = board.parent / "layout.core.py"
    core.write_text('Seg("GND", (10.0, 2.0), (20.0, 2.0), width=0.3, layer="F.Cu")\n' + core.read_text())
    d = _place(board).job.derived
    assert d.fit[1] == 1.15 and d.sides["top"]["what"] == "copper", (d.fit, d.sides["top"])  # 2.0 - 0.15 - 0.70
    assert d.R[1] <= d.fit[1]


# ------------------------------------------------------------------ the 2026-10-07 refutations (outline, compat, tests)


def test_a_fixed_line_is_on_the_grid_so_the_frozen_outline_keeps_the_edge_clearance(tmp_path: Path):
    """refute:outline (major): node's right side stood at U4's pad floor, 56.6935 (pads at 56.3935 + 0.3); the report
    printed 56.69, and the outline frozen from it (B.6) put U4's pads 0.2965 mm from the edge. A fixed line is now on
    the 0.01 mm grid, outward, so the printed numbers freeze to an outline that holds the clearance; and the report
    names the pad that set where J_LOAD's group stands."""
    from pcbc.outline import line, pad_boxes
    from pcbc.pcb_place import posed_feet_of
    from pcbc.stackup import get_stackup

    pl = _place(derived_copy(tmp_path / "d", "node"))
    d = pl.job.derived
    assert all(abs(v * 100 - round(v * 100)) < 1e-6 for v in d.fixed.values()), d.fixed
    assert d.fixed["right"] == 56.7
    assert (d.fixed_by["right"]["by"], d.fixed_by["right"].get("pad")) == ("J_LOAD", "U4.1")
    report = line(d, d.fit, d.sides, placement_only=True)
    assert "right: J_LOAD face (edge=, layout.core.py:12), standing at U4.1 pad x=56.39 + 0.30 edge clearance" in report
    m = re.search(r"derived ([\d.]+) x ([\d.]+) mm at \(([\d.-]+), ([\d.-]+)\)", report)
    frozen = derived_copy(tmp_path / "f", "node")
    core = frozen.parent / "layout.core.py"
    core.write_text(f"Outline(width={m[1]}, height={m[2]}, at=({m[3]}, {m[4]}))\n" + core.read_text())
    pf = _place(frozen)
    assert pf.error is None, pf.error
    x0, y0, x1, y1 = pf.job.outline
    e = get_stackup("jlcpcb_2l_1oz").edge_clearance
    feet = posed_feet_of(pf.job, pf.feet)
    worst = min(min(b[0] - x0, b[1] - y0, x1 - b[2], y1 - b[3]) for r, f in feet.items() if not r.startswith("FID") for _n, b in pad_boxes(f))
    assert worst >= e - 1e-9, worst


def test_an_edge_part_stands_on_a_declared_outline_wherever_its_corner_is(tmp_path: Path):
    """refute:compat (major): with `Outline(at=(5, 7))` an edge="left" part was resolved from the frame's x=0, 5 mm off
    the board, with no word; and an edge part's centring moved half as far as the corner. Its inset and its centring
    measure from the outline's own edges."""
    from pcbc.pcb_place import posed_feet_of

    core = 'Outline(width=30, height=20, at=(5, 7))\nPlace("R1", edge="left")\nPlace("R2", edge="top")\n'
    pl = _place(_board(tmp_path, core=core))
    assert pl.error is None, pl.error
    feet = posed_feet_of(pl.job, pl.feet)
    r1, r2 = feet["R1"].world_box(), feet["R2"].world_box()
    assert round(r1[0], 4) >= 5.0 and round(r1[0], 4) < 5.5, r1
    assert round((r1[1] + r1[3]) / 2, 4) == 17.0, "centred on the left edge, y 7..27"
    assert round(r2[1], 4) >= 7.0 and round(r2[1], 4) < 7.5, r2
    assert round((r2[0] + r2[2]) / 2, 4) == 20.0, "centred on the top edge, x 5..35"


REGION_CORE = (
    'Place("U1", position="absolute", left=15, top=8, reason="anchor")\n'
    'Keepout("KO", position="absolute", left=14, top=0, width=6, height=4)\n'
    'Region("PWR", left=0, top=12, width=8, height=14)\n'
    "{c_in1}\n"
    'Place("J_IN", edge="left")\nPlace("J_OUT", edge="right")\nPlace("C_IN2", to="U1.VIN")\nPlace("L1", to="U1.SW")\n'
    'Place("C_OUT1", to="L1.2")\nPlace("C_OUT2", to="L1.2")\nPlace("C_BOOT", to="U1.BOOT")\nPlace("R_EN", to="U1.EN")\n'
    'Place("R_FB_TOP", to="U1.FB")\nPlace("R_FB_BOT", to="U1.FB")\n'
)


def test_a_region_counts_like_a_courtyard_so_no_fixed_side_cuts_it(tmp_path: Path):
    """refute:compat (major): a Region at x 0..8 lay past the left side J_IN docked at (x=6.60) against the courtyards
    alone, and the outline cut 6.6 mm of it with no word. B.1: a Region counts toward the outline like a courtyard, so
    the edge group docks outside it."""
    board = derived_copy(tmp_path, "buck")
    (board.parent / "layout.core.py").write_text(REGION_CORE.format(c_in1='Place("C_IN1", to="U1.VIN")'))
    pl = _place(board)
    assert pl.error is None, pl.error
    from pcbc.pcb_place import posed_feet_of

    d = pl.job.derived
    assert d.fixed["left"] <= 0.0 and d.fit[0] <= 0.0 and d.fit[3] >= 26.0, (d.fixed, d.fit)
    jin = posed_feet_of(pl.job, pl.feet)["J_IN"].world_box()
    assert jin[2] <= 0.0 + 1e-6 or jin[1] >= 26.0 or jin[3] <= 12.0, ("J_IN stands clear of Region PWR", jin)


RULE_AREA = 'Pour(None, layers=("F.Cu", "B.Cu"), name="RA", keepout={{"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed", "copperpour": "not_allowed", "footprints": "allowed"}}, points=[{pts}], id="ra1")\n'


@pytest.mark.parametrize("pts, side", [("(31, 2), (36, 2), (36, 6), (31, 6)", "right"), ("(-6, 10), (-1, 10), (-1, 14), (-6, 14)", "left")])
def test_a_core_rule_area_counts_like_a_keepout_so_no_fixed_side_leaves_it_outside(tmp_path: Path, pts: str, side: str):
    """refute:outline (major, 2026-10-07): a core rule area past a side an edge part fixes was left wholly outside
    Edge.Cuts with no word (c3_usb n3: RA at x 36..41, the right edge at 36.00), where the same box as a Keepout grew the
    outline. A.5: the two count toward a derived outline the same way, so the edge group docks outside it and the board
    holds it."""
    board = derived_copy(tmp_path, "buck")
    core = REGION_CORE.format(c_in1='Place("C_IN1", to="U1.VIN")').replace('Region("PWR", left=0, top=12, width=8, height=14)\n', "")
    (board.parent / "layout.core.py").write_text(core + RULE_AREA.format(pts=pts))
    pl = _place(board)
    assert pl.error is None, pl.error
    d = pl.job.derived
    xs = [float(v.split(",")[0].strip("( ")) for v in pts.split("), ")]
    lo, hi = min(xs), max(xs)
    assert d.fit[0] <= lo + 1e-6 and d.fit[2] >= hi - 1e-6, ("the board holds the rule area", d.fit, (lo, hi))
    assert d.fixed[side] <= lo + 1e-6 if side == "left" else d.fixed[side] >= hi - 1e-6, d.fixed


def test_a_core_rule_area_past_a_fixed_side_is_a_fact():
    """The B.5-5 fact for a core rule area: one that still reaches past a fixed side is refused by name, never left
    outside Edge.Cuts silently."""
    from types import SimpleNamespace

    from pcbc.model import Copper
    from pcbc.outline import Derived, fixed_side_breach

    d = Derived(fixed={"right": 36.0}, m=0.7)
    d.fixed_by = {"right": {"by": "SW_BOOT", "what": "edge face", "line": 25}}
    ra = Copper(kind="pour", id="ra1", net=None, name="RA", keepout={"tracks": "not_allowed"}, points=((36.0, 2.0), (41.0, 2.0), (41.0, 6.0), (36.0, 6.0)), where="layout.core.py:11")
    got = fixed_side_breach(SimpleNamespace(derived=d), [ra])
    assert got == 'layout.core.py:11: rule area Pour(None, keepout=...) \'RA\' reaches x=41.00, outside the right edge at x=36.00 that SW_BOOT (edge="right", layout.core.py:25) sets', got


def test_a_region_that_sets_a_side_is_called_a_region():
    """refute:compat (minor): the fit labelled a Region "keepout" ("bottom: Region PWR keepout at y=26.00")."""
    from pcbc.outline import Derived, fit, line

    d = Derived(fixed={}, m=0.7)
    rect, sides = fit(d, [], extra=[("Region PWR", (0.0, 12.0, 8.0, 26.0)), ("Keepout KO", (14.0, 0.0, 20.0, 4.0))])
    assert (sides["bottom"]["what"], sides["top"]["what"]) == ("region", "keepout")
    assert line(d, rect, sides) == (
        "outline: derived 20.00 x 26.00 mm at (0.00, 0.00); left: Region PWR at x=0.00; top: Keepout KO at y=0.00; "
        "right: Keepout KO at x=20.00; bottom: Region PWR at y=26.00"
    )


def test_a_keepout_fixes_a_side_only_when_it_reaches_as_far_as_the_regions(tmp_path: Path):
    """The other half of a Region counting like a courtyard: a keep-out at the top fixed the top at its face even with a
    Region reaching past it, and the outline cut the Region; the Region now keeps that side free, and the fit holds it."""
    board = derived_copy(tmp_path, "buck")
    (board.parent / "layout.core.py").write_text(
        REGION_CORE.format(c_in1='Place("C_IN1", to="U1.VIN")').replace('Region("PWR", left=0, top=12, width=8, height=14)', 'Region("PWR", left=20, top=-2, width=4, height=5)')
    )
    d = _place(board).job.derived
    assert "top" not in d.fixed and d.fit[1] <= -2.0, (d.fixed, d.fit)


@pytest.mark.parametrize("declared", [False, True])
def test_a_part_placed_in_a_region_lands_in_it_once(tmp_path: Path, declared: bool):
    """refute:compat (major, older than PR 4): Place(parent=Region, left=2, top=2) put C_IN1 at y=26.98, its Region's
    top counted twice, because the settled pose kept `parent=` and was resolved from the Region's corner again."""
    from pcbc.pcb_place import posed_feet_of

    board = derived_copy(tmp_path, "buck")
    core = REGION_CORE.format(c_in1='Place("C_IN1", parent="PWR", left=2, top=2)')
    (board.parent / "layout.core.py").write_text(("Outline(width=40, height=30, at=(-10, 0))\n" if declared else "") + core)
    pl = _place(board)
    assert pl.error is None, pl.error
    box = posed_feet_of(pl.job, pl.feet)["C_IN1"].world_box()
    assert (round(box[0], 4), round(box[1], 4)) == (2.0, 14.0), box


def test_pcbc_pcb_prints_the_outline_line(tmp_path: Path, capsys):
    """refute:compat (major): README and B.4 say `pcbc pcb` prints the outline line; only --json carried it."""
    from pcbc.cli import main

    main(["pcb", str(derived_copy(tmp_path, "buck"))])
    out = capsys.readouterr().out.splitlines()
    assert out[1].startswith("outline: derived 26.81 x 18.00 mm at (2.31, 5.43); left: J_IN face"), out[:3]
    unwritten = [ln for ln in out if "was not written" in ln]
    assert unwritten and all(ln.endswith("rot= was not written") for ln in unwritten), "rotate= is refused; the field is rot="


def test_c3_usb_decoupling_caps_stand_beside_their_pin_in_derived_mode(tmp_path: Path):
    """c3_usb regression of PR 4: limited to I0, U1's caps took a clear spot 8.9 and 9.5 mm from U1.3V3 and EN went
    unrouted. A spot inside an earlier limit wins only while it is within DECAP_NEXT_MM of the pin; past that, a spot
    nearer by more than DECAP_MM outside it does (`pcb_place._relate`)."""
    import math

    from pcbc.pcb_place import posed_feet_of

    pl = _place(derived_copy(tmp_path, "c3_usb"))
    assert pl.error is None, pl.error
    feet = posed_feet_of(pl.job, pl.feet)
    u1 = feet["U1"]
    pin = next(u1.pad_world(p) for p in u1.pads if p.num == "3")

    def dist(ref):
        f = feet[ref]
        return min(math.hypot(a[0] - pin[0], a[1] - pin[1]) for a in (f.pad_world(p) for p in f.pads if p.net == "3V3"))

    assert dist("C_MCU_HF") <= 2.5 and dist("C_MCU") <= 5.0, (dist("C_MCU_HF"), dist("C_MCU"))


@pytest.mark.parametrize("left, top", [(8, 2), (10, 4), (6, 1), (12, 3)])
def test_the_first_decap_on_a_pin_stands_within_decap_mm_in_derived_mode(tmp_path: Path, left: int, top: int):
    """refute:placement r2 (major, 2026-10-07): with R_FB_BOT anchored north-west of buck's U1, I0 is the box around U1
    and R_FB_BOT, and `_relate` took the first clear spot in it up to DECAP_NEXT_MM even for the first cap on U1.VIN:
    C_IN1 3.40 mm and C_IN2 4.34 mm from the pin, against pcbc's own 2.5 mm rule (the declared board: 1.57 and 1.54).
    The first decoupling cap on a pin now takes a spot within DECAP_MM in whichever limit has one."""
    import math

    from pcbc.pcb_place import DECAP_MM, DECAP_NEXT_MM, posed_feet_of

    board = derived_copy(tmp_path, "buck", [('Place("R_FB_BOT", to="U1.FB")', f'Place("R_FB_BOT", position="absolute", left={left}, top={top}, locked=True)')])
    pl = _place(board)
    assert pl.error is None, pl.error
    feet = posed_feet_of(pl.job, pl.feet)
    u1 = feet["U1"]
    pins = [u1.pad_world(p) for p in u1.pads if p.net == "VIN"]

    def dist(ref):
        f = feet[ref]
        return min(math.hypot(a[0] - b[0], a[1] - b[1]) for a in (f.pad_world(p) for p in f.pads if p.net == "VIN") for b in pins)

    near, far = sorted((dist("C_IN1"), dist("C_IN2")))
    assert near <= DECAP_MM and far <= DECAP_NEXT_MM, (dist("C_IN1"), dist("C_IN2"))
    assert not [m for m in pl.report if "a decoupling cap belongs within" in m], pl.report


def test_geometry_in_layout_gen_py_names_core_as_its_home(tmp_path: Path):
    """refute:tests (gen-place-refused survived): an Outline() in layout.gen.py is refused at its own line, naming
    layout.core.py, before the second guard (a gen file that changed core's fields) is reached."""
    from pcbc.language import load_layout
    from pcbc.model import Design

    gen = tmp_path / "layout.gen.py"
    gen.write_text("Outline(width=5, height=5)\n")
    with pytest.raises(ValueError) as got:
        load_layout(gen, Design(), source="gen")
    assert str(got.value) == "layout.gen.py:1: Outline() is layout.core.py's, not layout.gen.py's"


def test_a_routing_scene_of_an_unplaced_derived_board_is_refused_not_spanned(tmp_path: Path):
    """refute:tests (scene-unplaced survived): with no Outline() and no placement the scene's escape grid would span
    the 10 m frame (it ran past 300 s); it is refused with the fact. Run in a thread so a regression fails, not hangs."""
    import threading

    from pcbc.compile import compile_design
    from pcbc.route_scene import build_scene

    design = load_board(derived_copy(tmp_path, "blinky"))
    job = compile_design(design)
    got: list = []

    def run():
        try:
            build_scene(design, job, job.constraints, {})
            got.append("built")
        except ValueError as exc:
            got.append(str(exc))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(60)
    assert got == ["the board has no Outline() and this job was not placed: a derived outline is set by the place stage (docs/board-outline.md B.3)"], got


def test_core_copper_is_judged_in_the_outline_placement_set(tmp_path: Path):
    """refute:tests (clash-outline survived): a core line's clearance is judged in a fresh compile that takes the
    placement's outline (R on a derived board); without it the scene is refused as unplaced and the core line is never
    judged."""
    from pcbc.layout_job import _clearance_clashes

    board = derived_copy(tmp_path, "buck")
    core = board.parent / "layout.core.py"
    core.write_text('Seg(None, (10.0, 2.0), (20.0, 2.0), width=0.3, layer="B.Cu")\n' + core.read_text())
    design = load_board(board)
    from pcbc.place_native import place

    pl = place(design, name="buck")
    assert pl.error is None, pl.error
    cu = [c for c in design.copper if c.source == "core"]
    assert _clearance_clashes(design, pl.feet, cu, job=pl.job) == []


@pytest.mark.kicad
def test_the_fit_after_routing_holds_core_copper_regions_and_rule_areas_and_redraws_the_pours(tmp_path_factory):
    """refute:tests (core-cu-grows-O, O-regions-extra, O-rule-area-extra, core-pour-from-O, plane-pours-from-O all
    survived): on a routed derived board, O's free sides are set by a no-net core Seg (top), a core rule area (right)
    and an empty Region (bottom); the point-less core Pour is O inset 0.5 and the GND plane is O inset the edge clearance,
    both drawn again from O, not left at R."""
    from pcbc.build import build_job
    from pcbc.sexp import parse_tree
    from pcbc.stackup import get_stackup

    board = derived_copy(tmp_path_factory.mktemp("extras"), "buck", [
        ('Place("J_OUT", edge="right", reason="output connector")', 'Place("J_OUT", at=(25.42, 9.335), reason="output connector")'),
    ])
    core = board.parent / "layout.core.py"
    core.write_text(
        'Seg(None, (10.0, 2.0), (20.0, 2.0), width=0.3, layer="B.Cu")\n'
        'Region("SPARE", left=8, top=30, width=10, height=2)\n'
        'Pour(None, layers=("F.Cu", "B.Cu"), name="RA", keepout={"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed", '
        '"copperpour": "allowed", "footprints": "allowed"}, points=[(36, 10), (38, 10), (38, 14), (36, 14)], id="ra")\n'
        'Pour("GND", layer="F.Cu", name="TOPGND", id="tg")\n' + core.read_text()
    )
    r = build_job(board, upto="route", force=True)
    o = r["outline"]
    # The core rule area counts as a Keepout does (A.5; refute:outline 2026-10-07): it is in I0, so the anchors' box and
    # the edge groups dock around it (left 2.31, was 3.56), and its face, reaching past every courtyard, fixes the right.
    assert o["source"] == "derived" and o["rect"] == [2.31, 1.15, 38.0, 32.0], o
    assert (o["sides"]["top"]["by"], o["sides"]["bottom"]["by"]) == ("no net seg (10,2)", "Region SPARE"), o["sides"]
    assert {k: o["sides"]["right"][k] for k in ("by", "what", "kind", "line")} == {"by": "RA", "what": "keepout face", "kind": "rule area", "line": 3}, o["sides"]["right"]
    x0, y0, x1, y1 = o["rect"]
    e = get_stackup("jlcpcb_2l_1oz").edge_clearance
    zones = {}
    for node in parse_tree((board.parent / "layout" / "buck" / "routed" / "layout.kicad_pcb").read_text()):
        if isinstance(node, list) and node and node[0] == "zone":
            f = {x[0]: x for x in node[1:] if isinstance(x, list) and x}
            pts = [(float(p[1]), float(p[2])) for p in f["polygon"][1][1:] if isinstance(p, list) and p[0] == "xy"]
            zones[(f.get("name", [None, None])[1], f.get("layer", [None, None])[1])] = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    assert zones[("TOPGND", "F.Cu")] == tuple(round(v, 6) for v in (x0 + 0.5, y0 + 0.5, x1 - 0.5, y1 - 0.5))
    assert zones[(None, "B.Cu")] == tuple(round(v, 6) for v in (x0 + e, y0 + e, x1 - e, y1 - e)), "the GND plane"


@pytest.mark.kicad
def test_a_finished_derived_board_stamps_and_fabs_its_derived_outline(tmp_path: Path):
    """refute:tests (route-stamp-outline, fab-notes-source survived): blinky with no Outline() routes and fabs; the
    route stamp records O, FAB_NOTES says the outline is derived, and fab reads the same rectangle off Edge.Cuts."""
    import json

    from pcbc.build import build_job

    board = derived_copy(tmp_path, "blinky")
    r = build_job(board, force=True)
    assert r.get("error") is None, r.get("error")
    layout = board.parent / "layout" / "blinky"
    stamp = json.loads((layout / "inputs.json").read_text())
    assert stamp["outline"] == {"source": "derived", "rect": r["outline"]["rect"]}
    x0, y0, x1, y1 = r["outline"]["rect"]
    notes = (layout / "fab" / "FAB_NOTES.md").read_text()
    assert f"Board {round(x1 - x0, 4):g}×{round(y1 - y0, 4):g} mm (derived)" in notes, notes[:400]
    rep = json.loads((layout / "fab" / "report.json").read_text())
    assert rep["outline_rect"] == r["outline"]["rect"] and rep["outline_source"] == "derived"


def test_a_keepout_face_is_fixed_on_the_grid_outward(tmp_path: Path):
    """The keepout half of the 0.01 mm grid rule: a keep-out whose face is at y=0.004 fixes the top at 0.00 (outward),
    not at 0.004, so the frozen outline holds it."""
    board = derived_copy(tmp_path, "buck", [
        ('Place("U1", position="absolute", left=15, top=8, locked=True, reason="buck IC in the middle")',
         'Place("U1", position="absolute", left=15, top=8, locked=True, reason="buck IC in the middle")\nKeepout("KO", left=14, top=0.004, width=6, height=4)'),
    ])
    d = _place(board).job.derived
    assert (d.fixed_by["top"]["by"], d.fixed["top"]) == ("KO", 0.0), (d.fixed, d.fixed_by)


def test_pads_past_the_edge_are_said_to_cross_it(tmp_path: Path):
    """refute:compat: a part past Edge.Cuts was reported as "pads come within 0.3 mm of the board edge"; the report
    says how far they cross it."""
    from pcbc.build import pcb_job

    r = pcb_job(_board(tmp_path, core='Outline(width=30, height=20)\nPlace("R1", at=(0, 10))\nPlace("R2", at=(20, 10))\n'))
    text = "\n".join(r["layout_report"])
    assert re.search(r"R1's pads cross the board edge by \d+\.\d\d mm: copper needs 0\.3 mm inside it", text), r["layout_report"]
    assert "R2's pads" not in text


def test_a_fiducial_mask_in_the_way_is_the_builds_not_a_core_line():
    """refute:compat (minor): router errors named a fiducial's copper keepout "a Keepout or rule area of
    layout.core.py"; the build places it, and no core line declares it."""
    from types import SimpleNamespace

    from pcbc.route_native import _source_of

    design = SimpleNamespace(places=[], netreqs=[])
    assert _source_of(design, SimpleNamespace(kind="keepout", owner="FID2_mask", reason="fid")) == "FID2_mask (the copper keepout of fiducial FID2, placed by the build)"
    assert _source_of(design, SimpleNamespace(kind="keepout", owner="KO", reason="fid")) == "KO (a Keepout or rule area of layout.core.py)"
