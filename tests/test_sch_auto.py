"""docs/schematic-layout.md §2-§5: the auto-layout, the core vocabulary, schematic.gen.py and its fixed point.

The tool lays the sheet out from the netlist alone; `schematic.core.py` locks what the AI wants kept; every object the
layout resolved is written to `layout/<name>/schematic.gen.py`, and copying every gen line into the core reproduces the
sheet byte for byte. Each test fails when the behaviour it pins is removed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pcbc.language import load_board
from pcbc.sch_emit import emit_from_design
from pcbc.sch_gen import gen_text

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
BOARDS = ("blinky", "buck", "c3_usb", "node")

CIRCUIT = """from pcbc import Board, Ground, Net, NetReq, Power, Resistor, Capacitor
VCC = Power("VCC")
GND = Ground("GND")
MID = Net("MID")
Resistor("R1", "1k", package="0402", mpn="X", lcsc="C1", p1=VCC, p2=MID)
Resistor("R2", "1k", package="0402", mpn="X", lcsc="C1", p1=MID, p2=GND)
Capacitor("C1", "100nF", package="0402", mpn="X", lcsc="C1", p1=MID, p2=GND)
Board(layers=2, stackup="jlcpcb_2l_1oz")
NetReq("VCC", kind="power", volts=3.3)
"""


def _board(tmp_path: Path, sch: str | None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    b = tmp_path / "three.py"
    b.write_text(CIRCUIT)
    if sch is not None:
        (tmp_path / "schematic.core.py").write_text(sch)
    return b


def _copy(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns("layout"))
    return dst / f"{name}.py"


def _emit(board: Path, **kw) -> tuple[str, dict]:
    rep: dict = {}
    return emit_from_design(load_board(board, **kw), title="x", report=rep), rep


# ------------------------------------------------------------------ §2: the sheet from the netlist alone


@pytest.mark.parametrize("name", BOARDS)
def test_every_example_is_drawn_with_no_schematic_core(name: str):
    """§2 / PR 4: the "no SchPlace()" refusal is gone; a board with no schematic.core.py gets a sheet."""
    text, rep = _emit(EXAMPLES / name / f"{name}.py", schematic_core=False)
    assert text.startswith("(kicad_sch") and rep["readability"]["hard"] >= 0
    assert {p["ref"] for p in rep["parts"]} == {i.ref for i in load_board(EXAMPLES / name / f"{name}.py").instances}


def test_regions_are_inferred_with_auto_names_titled_by_their_anchors():
    _text, rep = _emit(EXAMPLES / "node" / "node.py", schematic_core=False)
    regions = {r.name: r for r in rep["_layout"].regions}
    assert set(regions) == {"auto-U2", "auto-J1", "auto-U1", "auto-U4", "auto-U5", "auto-J_LOAD"}
    assert regions["auto-U2"].title == "U2 AP2112K-3.3TRG1" and regions["auto-U2"].row == 0, "the regulator, row 0"
    assert regions["auto-U1"].hub, "the ESP32 is the hub"
    assert {"C_MCU", "C_MCU_HF", "C_BME", "C_OPA", "C_3V3", "C_3V3_HF"} <= set(regions["auto-U2"].members), "one 3.3 V bank"
    assert {"R_SDA", "R_SCL", "C_BME"} & set(regions["auto-U4"].members) == {"R_SDA", "R_SCL"}, "the pull-ups go with the sensor"
    assert set(regions["auto-J_LOAD"].members) == {"Q1", "J_LOAD", "R_G", "R_PD"}


def test_a_sheet_with_one_inferred_region_draws_no_outline():
    text, rep = _emit(EXAMPLES / "buck" / "buck.py", schematic_core=False)
    assert [r.outline for r in rep["_layout"].regions] == [False] and "(type dash)" not in text


def test_the_ds2_rc_filters_stay_with_their_connector():
    from conftest import ds2_dir

    board = ds2_dir() / "ds2_addon.py"
    if not board.exists():
        pytest.skip("the DS2 Addon is not checked out here")
    _text, rep = emit_from_design(load_board(board, schematic_core=False), title="ds2", report=(rep := {})), rep
    home = {m: r.name for r in rep["_layout"].regions for m in r.members}
    assert home["R6"] == home["C7"] == home["C8"] == home["J2"], "a passive unit joins one anchor whole (review-readability M4)"
    assert home["C4"] == home["U1"], "Path('VDDA', 'J2.1', 'C4.1', 'U1.12') puts C4 with the ADC"


@pytest.mark.parametrize("name", ["blinky", "buck"])
def test_the_auto_sheet_is_the_same_under_two_hash_seeds(name: str):
    code = (
        "import hashlib, sys; from pcbc.language import load_board; from pcbc.sch_emit import emit_from_design; "
        "print(hashlib.sha256(emit_from_design(load_board(sys.argv[1], schematic_core=False), title='x').encode()).hexdigest())"
    )
    board = str(EXAMPLES / name / f"{name}.py")
    got = {
        subprocess.run([sys.executable, "-c", code, board], env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True, check=True).stdout
        for seed in ("0", "1")
    }
    assert len(got) == 1


# ------------------------------------------------------------------ §4: gen and its fixed point


def _ds2_copy(tmp_path: Path) -> Path:
    from conftest import ds2_dir

    src = ds2_dir()
    if not (src / "ds2_addon.py").exists():
        pytest.skip("the DS2 Addon is not checked out here")
    dst = tmp_path / "ds2"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("layout"))
    return dst / "ds2_addon.py"


def _locks(gen: str) -> str:
    """Gen's lock lines (every line but the drawn primitives, which are no locks: PR 6)."""
    return "\n".join(ln for ln in gen.splitlines() if not ln.startswith("Sheet")) + "\n"


@pytest.mark.parametrize("name", [*BOARDS, "ds2"])
def test_every_gen_line_copied_into_core_gives_the_same_sheet(tmp_path: Path, name: str):
    """§4's fixed point: copy every gen lock line into schematic.core.py, rebuild, and the .kicad_sch is byte-identical,
    and gen writes the same lock lines again (DS2 included: refute:tests found it unpinned)."""
    board = _ds2_copy(tmp_path) if name == "ds2" else _copy(tmp_path, name)
    first, rep = _emit(board)
    gen = gen_text(load_board(board), rep, name)
    (board.parent / "schematic.core.py").write_text(_locks(gen))
    again, rep2 = _emit(board)
    assert again == first
    body = lambda g: [ln.split("  #")[0] for ln in g.splitlines() if ln.startswith("Sch")]  # noqa: E731
    assert body(gen_text(load_board(board), rep2, name)) == body(gen)


@pytest.mark.parametrize(
    "name, region, ref",
    [("ds2", "adc", "C4"), ("ds2", "inputs", "J5"), ("ds2", "edge", "R4"), ("c3_usb", "mcu", "SW_BOOT"), ("node", "mcu", "SW_RST")],
)
def test_one_gen_pose_copied_over_its_core_lines_in_place_changes_nothing(tmp_path: Path, name: str, region: str, ref: str):
    """refute:split (major, 2026-10-08): one gen pose line plus its region line, pasted over the region's core line in
    its place, broke DS2 (a pose in adc moved J2 in inputs; the owner's SchPower locks no longer fit) and shorted USB_DN to
    GND on c3_usb. The build now draws the sheet (no refusal, no hard finding) with the pose's and the region's gen lines
    unchanged (§4's single-line variant)."""
    board = _ds2_copy(tmp_path) if name == "ds2" else _copy(tmp_path, name)
    first, rep = _emit(board)
    gen = gen_text(load_board(board), rep, name).splitlines()
    reg_line = next(ln for ln in gen if ln.startswith(f'SchRegion("{region}"')).split("  #")[0]
    pose = next(ln for ln in gen if ln.startswith(f'SchPlace("{ref}"')).split("  #")[0]
    core = board.parent / "schematic.core.py"
    lines = core.read_text().splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(f'SchRegion("{region}"'))
    lines[i] = reg_line
    lines = [ln for ln in lines if not ln.startswith(f'SchPlace("{ref}"')]
    lines.insert(i + 1, pose)
    core.write_text("\n".join(lines) + "\n")
    _again, rep2 = _emit(board)
    assert rep2["readability"]["hard"] == 0, rep2["readability"]["detail"]
    after = [ln.split("  #")[0] for ln in gen_text(load_board(board), rep2, name).splitlines()]
    assert pose in after and reg_line in after


@pytest.mark.parametrize(
    "kind",
    ["SchRegion", "SchPlace", "SchText", "SchLabel", "SchPower", "SchSheet"],
)
def test_one_copied_gen_line_keeps_its_object_where_it_was(tmp_path: Path, kind: str):
    """§4's single-line variant: one gen line of each kind (with its region line for a pose) copied into the core, and
    that object's gen line is unchanged."""
    board = _copy(tmp_path, "c3_usb")
    (board.parent / "schematic.core.py").unlink()
    _first, rep = _emit(board)
    lines = gen_text(load_board(board), rep, "c3").splitlines()
    pick = next(ln for ln in lines if ln.startswith(kind + "(") and (kind != "SchPlace" or "# auto:" in ln))
    core = [pick.split("  #")[0]]
    if kind == "SchPlace":
        ref = pick.split('"')[1]
        core.insert(0, next(ln for ln in lines if ln.startswith("SchRegion(") and f'"{ref}"' in ln.split("members=")[1].split(")")[0]).split("  #")[0])
    (board.parent / "schematic.core.py").write_text("\n".join(core) + "\n")
    _again, rep2 = _emit(board)
    after = [ln.split("  #")[0] for ln in gen_text(load_board(board), rep2, "c3").splitlines()]
    assert pick.split("  #")[0] in after


def test_gen_writes_one_line_per_object_and_marks_locks(tmp_path: Path):
    board = _copy(tmp_path, "c3_usb")
    _text, rep = _emit(board)
    gen = gen_text(load_board(board), rep, "c3_usb").splitlines()
    assert gen[0].startswith("# schematic.gen.py - written by `pcbc build`'s sch stage for c3_usb")
    places = [ln for ln in gen if ln.startswith("SchPlace(")]
    assert len(places) == len(load_board(board).instances)
    assert all("at=(" in ln and "rotate=" in ln for ln in places)
    regions = [ln for ln in gen if ln.startswith("SchRegion(")]
    assert len(regions) == 3 and all("# locked: schematic.core.py:" in ln for ln in regions)
    assert sum(ln.startswith("SchText(") for ln in gen) == 2 * len(places)
    assert any(ln.startswith("SchPower(") for ln in gen) and any(ln.startswith("SchLabel(") for ln in gen)
    assert not any("PWR_FLAG" in ln for ln in gen if ln.startswith("Sch")), "flags are derived, not written as locks"


# ------------------------------------------------------------------ §3: the vocabulary and its refusals


def _refusal(tmp_path: Path, sch: str) -> str:
    with pytest.raises(ValueError) as got:
        load_board(_board(tmp_path, sch))
    return str(got.value)


@pytest.mark.parametrize(
    "sch, want",
    [
        ('SchPlace("R1", left=20, top=20)\n', "schematic.core.py:1: SchPlace('R1') takes no CSS; a pose is at=(x, y) in its region"),
        ('SchPlace("R1", parent="a")\n', "schematic.core.py:1: SchPlace('R1', parent='a'): membership is SchRegion(members=)"),
        ('SchRegion("a", left=1, top=1, members=("R1",))\n', "schematic.core.py:1: SchRegion('a') takes no CSS; its origin is at=(x, y) or a grid cell row=, col="),
        ('SchRegion("a", title="A")\n', "schematic.core.py:1: SchRegion('a'): members= is required"),
        ('SchRegion("a", members=("R1",), at=(0, 0), col=1)\n', "at= and row=/col= are two anchors for its origin"),
        ('SchRegion("auto-R9", members=("R1",))\n', "a region named auto-... is named by its anchor part, and R9 is not among its members"),
        ('SchRegion("a", members=("R1",))\nSchRegion("b", members=("R1", "R2"))\n', "schematic.core.py:2: SchRegion('b'): R1 is already a member of SchRegion('a') at line 1"),
        ('SchPlace("R1", at=(1.27, 2.54), to="R2.1")\n', "at= and to= are 2 anchors; a SchPlace takes one"),
        ('SchPlace("R1", side="left")\n', "side= belongs to to= or along=, and this SchPlace has none"),
        ('SchRegion("a", members=("R1",))\nSchPlace("R1", at=(1.0, 2.54))\n', "schematic.core.py:2: SchPlace('R1'): at=(1, 2.54): 1 is off the 1.27 mm grid"),
        ('SchPlace("R1", at=(1.27, 2.54))\n', "schematic.core.py:1: the at= of R1's SchPlace is a pose in its region's frame, and R1 is in no SchRegion(members=)"),
        ('SchRegion("a", members=("R1",))\nSchPlace("R1", to="R2.2")\n', "SchPlace('R1') hangs off R2; R1 is in SchRegion('a') and R2 in no declared region; an attach stays in its region"),
        ('SchRegion("a", members=("R1", "R2"))\nSchPlace("R1")\nSchPlace("R2")\n', "schematic.core.py:3: SchPlace('R2') is the anchor of region 'a', and SchPlace('R1') at line 2 already is"),
        ('SchPlace("R1", to="R2.1")\nSchPlace("R2", to="R1.2")\n', "the attachments go round in a circle (R1 -> R2 -> R1)"),
        ('SchLabel("MID", pin="R1.1", side="left", stub=2.54)\n', "schematic.core.py:1: SchLabel('MID', pin='R1.1'): R1.1 is on VCC, not MID"),
        ('SchLabel("VCC", pin="R1.1", side="left", stub=2.54)\n', "VCC is Power(), drawn with power symbols, not labels"),
        ('SchPower("MID", pin="R1.2", side="left")\n', "MID is a signal net, joined by wires and labels, not power symbols"),
        ('SchPower("VCC", pin="R1.7", side="top")\n', "R1 has no pin 7 (its pins: 1, 2)"),
        ('SchLabel("MID", pin="R1.2", side="left", stub=1.0)\n', "stub 1 is off the 1.27 mm grid"),
        ('SchLabel("MID", pin="R1.2")\n', "a label is side= and stub= (on a stub off its pin) or at= (on its group's wire), one of the two"),
        ('SchText("R1", "Footprint", at=(1, 1))\n', "field 'Footprint'; a SchText places the Reference or the Value"),
        ('SchText("R1", "Value", at=(1, 1))\nSchText("R1", "Value", at=(2, 1))\n', "R1's Value is already placed at schematic.core.py:1"),
        ('SchPower("VCC", pin="R1.1", side="top")\nSchPower("VCC", pin="R1.1", side="left")\n', "R1.1 already has SchPower() at schematic.core.py:1"),
    ],
    ids=[
        "css-place", "parent", "css-region", "members-required", "region-two-anchors", "auto-name", "two-regions",
        "two-anchors", "side-without-anchor", "off-grid", "pose-outside-region", "attach-across-regions",
        "two-region-anchors", "cycle", "label-wrong-net", "label-on-power", "power-on-signal", "unknown-pin",
        "stub-off-grid", "label-form", "text-field", "text-twice", "power-twice",
    ],
)
def test_a_wrong_core_line_is_refused_as_a_fact_naming_its_line(tmp_path: Path, sch: str, want: str):
    assert want in _refusal(tmp_path, sch)


def test_a_gen_region_line_named_auto_declares_that_region(tmp_path: Path):
    d = load_board(_board(tmp_path, 'SchRegion("auto-R1", title="R1 1k", members=("R1", "R2", "C1"))\n'))
    assert [r.name for r in d.sch_regions] == ["auto-R1"]


# ------------------------------------------------------------------ §3.3 / §3.4: what a lock locks


def test_a_pose_lock_is_where_it_says_and_never_moved(tmp_path: Path):
    sch = 'SchRegion("a", members=("R1", "R2", "C1"), at=(25.4, 25.4))\nSchPlace("R1", at=(12.7, 25.4), rotate=90)\n'
    _text, rep = _emit(_board(tmp_path, sch))
    r1 = next(p for p in rep["_parts"] if p.ref == "R1")
    assert (r1.x, r1.y, r1.rot) == (38.1, 50.8, 90.0)


def test_two_posed_bodies_that_overlap_are_refused_with_both_lines(tmp_path: Path):
    sch = 'SchRegion("a", members=("R1", "R2", "C1"))\nSchPlace("R1", at=(12.7, 25.4))\nSchPlace("R2", at=(12.7, 25.4))\n'
    with pytest.raises(ValueError) as got:
        _emit(_board(tmp_path, sch))
    assert "schematic.core.py:2 and schematic.core.py:3: SchPlace('R1') and SchPlace('R2') are both posed in region 'a' and their bodies overlap by" in str(got.value)


def test_a_stub_label_lock_makes_the_pin_a_label_where_a_wire_would_fit(tmp_path: Path):
    plain, _ = _emit(_board(tmp_path, ""))
    assert plain.count('(label "MID"') == 1, "MID is one wired group, named once"
    text, _ = _emit(_board(tmp_path, 'SchLabel("MID", pin="R2.1", side="top", stub=2.54)\n'))
    assert text.count('(label "MID"') == 2, "R2.1 takes its own label and no wire; the rest of MID is named once more"


def test_a_power_lock_is_drawn_as_written(tmp_path: Path):
    _text, rep = _emit(_board(tmp_path, 'SchPower("GND", pin="R2.2", side="bottom", stub=5.08)\n'))
    rec = next(r for r in rep["_sheet"].records.values() if r["kind"] == "power" and r["site"].part.ref == "R2" and r["net"] == "GND")
    assert (rec["side"], rec["stub"], rec["jog"], rec["rotate"]) == ("bottom", 5.08, 0.0, 0)


def test_a_text_lock_puts_the_field_at_its_offset_and_can_hide_it(tmp_path: Path):
    text, rep = _emit(_board(tmp_path, 'SchText("R1", "Value", at=(5.08, 0), justify="left", hide=True)\n'))
    r1 = next(p for p in rep["_parts"] if p.ref == "R1")
    assert r1.text_seen[1][:3] == (round(r1.x + 5.08, 4), round(r1.y, 4), "left")
    assert '(property "Value" "1k"' in text and "(hide yes)" in text


def test_a_lock_whose_stub_meets_another_net_is_refused_naming_what_it_meets(tmp_path: Path):
    with pytest.raises(ValueError) as got:
        _emit(_board(tmp_path, 'SchPower("VCC", pin="R1.1", side="bottom", stub=12.7)\n'))
    assert "schematic.core.py:1: SchPower('VCC', pin='R1.1'): its stub (bottom 12.7 mm, jog 0 mm) from" in str(got.value) and "meets" in str(got.value)


def test_declared_membership_is_closed_and_the_rest_is_inferred(tmp_path: Path):
    _text, rep = _emit(_board(tmp_path, 'SchRegion("pull", title="Pull", members=("R1",))\n'))
    regions = {r.name: r.members for r in rep["_layout"].regions}
    assert regions["pull"] == ["R1"] and any(n.startswith("auto-") and set(m) == {"R2", "C1"} for n, m in regions.items())


@pytest.mark.parametrize("first", ["a", "b"])
@pytest.mark.parametrize("cols", [(0, 1), (1, 0)])
def test_a_declared_column_orders_the_regions_whatever_the_file_order(tmp_path: Path, first: str, cols: tuple):
    """§3.3: `col=` locks the grid cell, and file order never places a region (refute:tests 2026-10-08: the old test
    declared b (col=0) before a (col=1), so declaration order alone passed it with `col=` ignored)."""
    lines = {"a": f'SchRegion("a", members=("R1",), col={cols[0]})\n', "b": f'SchRegion("b", members=("R2", "C1"), col={cols[1]})\n'}
    sch = lines[first] + lines["b" if first == "a" else "a"]
    _text, rep = _emit(_board(tmp_path, sch))
    origin = {r.name: r.origin for r in rep["_layout"].regions}
    left = "a" if cols[0] < cols[1] else "b"
    right = "b" if left == "a" else "a"
    assert origin[left][0] < origin[right][0], origin


def test_the_order_regions_are_declared_in_never_changes_the_sheet(tmp_path: Path):
    """§3.3: file order never places a region. A gen region line copied over its core line lands where that line was;
    the sheet is the same wherever it is (refute:split 2026-10-08: DS2's adc line in the middle of the file moved J2)."""
    a = 'SchRegion("a", members=("R1",))\n'
    b = 'SchRegion("b", members=("R2", "C1"))\n'
    one, _ = _emit(_board(tmp_path / "ab", a + b))
    two, _ = _emit(_board(tmp_path / "ba", b + a))
    assert one == two


def test_a_core_attached_part_counts_as_placed_once_its_target_is(tmp_path: Path):
    """`region_specs`' promote: R2 is attached by core to R1.2; once R1 is placed, R2 counts as placed, so the auto-layout
    hangs C1 off R2 (`to="R2.2"`, its net) rather than stacking it under R1 (refute:tests: promote() as a no-op gave C1
    `below R1` and survived every test)."""
    from pcbc.language import load_board as lb

    circuit = CIRCUIT.replace('MID = Net("MID")', 'MID = Net("MID")\nB = Net("B")').replace(
        'Resistor("R2", "1k", package="0402", mpn="X", lcsc="C1", p1=MID, p2=GND)', 'Resistor("R2", "1k", package="0402", mpn="X", lcsc="C1", p1=MID, p2=B)'
    ).replace('Capacitor("C1", "100nF", package="0402", mpn="X", lcsc="C1", p1=MID, p2=GND)', 'Capacitor("C1", "100nF", package="0402", mpn="X", lcsc="C1", p1=B, p2=GND)')
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "three.py").write_text(circuit)
    (tmp_path / "schematic.core.py").write_text('SchRegion("a", members=("R1", "R2", "C1"))\nSchPlace("R1")\nSchPlace("R2", to="R1.2")\n')
    rep: dict = {}
    emit_from_design(lb(tmp_path / "three.py"), title="x", report=rep)
    c1 = rep["_layout"].specs["C1"]
    assert (c1.source, c1.to, c1.below) == ("auto", "R2.2", None), (c1.to, c1.below)


# ------------------------------------------------------------------ the build: gen is stamped, hard is an error


@pytest.mark.kicad
def test_the_sch_stage_writes_and_stamps_gen_and_an_edited_gen_redraws_the_sheet(tmp_path: Path):
    from pcbc.build import build_job, sch_reason
    from pcbc.project import layout_dir

    board = _copy(tmp_path, "blinky")
    result = build_job(board, upto="sch")
    assert result.get("error") is None, result.get("error")
    layout = layout_dir(board)
    gen = layout / "schematic.gen.py"
    assert gen.exists() and gen.read_text().startswith("# schematic.gen.py")
    gen.write_text(gen.read_text() + "# a hand edit\n")
    assert sch_reason(layout, board) == "schematic.gen.py is not the file the sch stage wrote (edited or deleted): it is build output, never edited"


@pytest.mark.kicad
def test_a_hard_readability_number_is_a_build_error_naming_the_objects(tmp_path: Path):
    from pcbc.build import build_job

    board = _copy(tmp_path, "blinky")
    # A text lock written onto the resistor's own body: KiCad draws it there, and the sheet is not readable.
    (board.parent / "schematic.core.py").write_text('SchText("R1", "Reference", at=(0, 0), justify="left")\n')
    result = build_job(board, upto="sch")
    assert result["error"] and result["error"].startswith("schematic is not readable (") and "text_collisions: symbol R1 overlaps R1 ref" in result["error"]
    assert (board.parent / "layout" / "blinky" / "schematic.gen.py").exists(), "gen is written for the AI to lock from"


def test_a_part_standing_between_two_in_line_pins_of_one_net_is_handed_to_the_repair_loop(monkeypatch):
    # node, no core, with the hanger order before 2026-10-09 (every part the same reach): SW_RST hangs under the EN node,
    # its GND pin line stands on the LED row between U1.IO10 and R_LED.1, and the two are joined by labels. The in-line
    # lint names the part on the run (`blocked`) and the repair loop flips and pushes it, as it does a hard finding's
    # parts, even on a pass with no hard finding. With today's order (the shorter C_EN under the node) nothing stands
    # on the run (test_sch_sets: node's LED and BOOT pins are wired).
    import pcbc.sch_auto as A
    import pcbc.sch_emit as E

    monkeypatch.setattr(A, "_span", lambda part: 0.0)

    seen: list[dict] = []
    real = E._draw_pass

    def spy(design, kinds, extra, *a, **k):
        seen.append(dict(extra))
        return real(design, kinds, extra, *a, **k)

    monkeypatch.setattr(E, "_draw_pass", spy)
    rep: dict = {}
    emit_from_design(load_board(EXAMPLES / "node" / "node.py", schematic_core=False), title="node", report=rep)
    assert "SW_RST" in rep["blocked"], rep["blocked"]
    assert any(x.get("SW_RST#flip") for x in seen) and any(x.get("SW_RST", 0.0) > 0 for x in seen), seen
    assert rep["readability"]["hard"] == 0 and rep["readability"]["wire_over_pin"] == 0


def test_a_core_line_that_differs_is_placed_first_and_one_equal_to_the_auto_spec_in_its_turn(tmp_path: Path):
    """A core attach line that differs from what the auto-layout writes is placed before the parts the auto-layout
    chose (node's C_BME, `gap=15.24`, right after its region's anchor). One equal to the auto-layout's own spec is met
    and placed in its turn (§3.3, 2026-10-09: node's R_BOOT line), so the region's order is the order with no line."""
    rep: dict = {}
    board = _copy(tmp_path / "a", "node")
    emit_from_design(load_board(board), title="node", report=rep)
    lay = rep["_layout"]
    sensor = next(r for r in lay.regions if r.name == "sensor")
    assert sensor.order[:2] == ["U4", "C_BME"] and lay.specs["C_BME"].source == "core"
    assert "R_BOOT" in lay.met and lay.specs["R_BOOT"].source == "auto"
    core = board.parent / "schematic.core.py"
    core.write_text("\n".join(ln for ln in core.read_text().splitlines() if not ln.startswith('SchPlace("R_BOOT"')) + "\n")
    rep2: dict = {}
    emit_from_design(load_board(board), title="node", report=rep2)
    mcu = [r.order for r in lay.regions if r.name == "mcu"]
    assert mcu == [r.order for r in rep2["_layout"].regions if r.name == "mcu"]


@pytest.mark.parametrize("lock", [
    'SchPlace("J_OUT", to="C_OUT2.1", side="right")',  # a part the auto-layout would hang C_OUT1 off
    'SchPlace("U1", to="L1.1")',  # the region's own anchor hung off one of its hangers
])
def test_a_lock_against_the_auto_layouts_direction_does_not_close_a_loop(tmp_path: Path, lock: str):
    # buck has no core: the auto-layout hangs C_OUT1 off J_OUT and L1 off U1.SW. A core line the other way round is
    # placed from what the line names; the auto-layout's own attaches start from the rest, never from the locked part.
    from conftest import write_split

    shutil.copytree(EXAMPLES / "buck" / "components", tmp_path / "components")
    board = tmp_path / "buck.py"
    write_split(board, (EXAMPLES / "buck" / "buck.py").read_text())
    (tmp_path / "schematic.core.py").write_text(lock + "\n")
    rep: dict = {}
    emit_from_design(load_board(board), title="buck", report=rep)
    ref = lock.split('"')[1]
    spec = rep["_layout"].specs[ref]
    assert spec.source == "core" and spec.to == lock.split('to="')[1].split('"')[0]


def test_a_region_of_two_pin_parts_draws_its_bank_beside_the_anchor_and_reads_clean():
    # d14's neck: three resistors across PWR and GND, no IC. Hung off the anchor's pin end, R2 boxed in its own GND
    # symbol (1.82 mm2); as a row beside the anchor the sheet has no hard finding.
    rep: dict = {}
    emit_from_design(load_board(ROOT / "tests" / "fixtures" / "d14" / "neck.py"), title="neck", report=rep)
    assert rep["readability"]["hard"] == 0, rep["readability"]
    assert rep["_layout"].specs["R2"].along == "R1.1"


def test_a_busy_ic_sides_decoupling_is_a_row_under_it_and_a_second_connector_clears_the_first_ones_cluster(tmp_path: Path):
    """Two placement rules only DS2 exercises (refute:tests 2026-10-08: `_dense` and the cluster stacking survived every
    schematic test). `_dense`: the ADC's supply pins sit on a side with six or more bound pins, so its decoupling is a row
    under the IC (C4 below U1, C5 and C6 along it), not hung among the pin rows. Clusters: J5, below J2, clears J2's
    whole cluster (its filters), not J2's bare body."""
    from pcbc.sch_place import world_aabb

    board = _ds2_copy(tmp_path)
    _text, rep = _emit(board)
    specs = rep["_layout"].specs
    assert (specs["C4"].below, specs["C5"].along, specs["C6"].along) == ("U1", "C4.1", "C5.1"), (specs["C4"], specs["C5"], specs["C6"])
    by = {p.ref: p for p in rep["_parts"]}
    cluster = max(world_aabb(by[m])[3] for m in ("J2", "R6", "R7", "C7", "C8", "C9"))
    assert world_aabb(by["J5"])[1] > cluster, (world_aabb(by["J5"]), cluster)
    # The third cluster opens a column (at most two to a column): J4 and its filters stand right of everything J2's and
    # J5's clusters drew, J4's cluster top level with J2's (one column made DS2 1.64 times the hand sheet's area).
    first = ("J2", "R6", "R7", "C7", "C8", "C9", "J5", "R8", "R9", "C10", "C11", "C12")
    third = ("J4", "R1", "R2", "C1", "C2", "C3")
    right = max(world_aabb(by[m])[2] for m in first)
    assert min(world_aabb(by[m])[0] for m in third) > right
    assert abs(min(world_aabb(by[m])[1] for m in third) - min(world_aabb(by[m])[1] for m in first[:6])) < 1.3
    w, h = rep["readability"]["extent_mm"]
    assert w * h <= 1.5 * 283.1 * 78.2, (w, h)  # §2.7's gate against the hand sheet
    assert rep["readability"]["hard"] == 0


def test_ds2_with_no_core_draws_a_sheet_with_no_hard_finding(tmp_path: Path):
    """DS2's auto sheet (no schematic.core.py) had C7 on the AIN0 label and the VDDA PWR_FLAG's stub through J2's VDDA
    arrow, so DS2 could not build without a core. The pin-line rule and the flag's spots off the drawn symbol clear both."""
    board = _ds2_copy(tmp_path)
    (board.parent / "schematic.core.py").unlink()
    _text, rep = _emit(board)
    assert rep["readability"]["hard"] == 0, rep["readability"]["detail"]


@pytest.mark.parametrize("name, region", [("ds2", "adc"), ("ds2", "inputs"), ("ds2", "edge"), ("c3_usb", "pwr"), ("node", "sensor")])
def test_a_gen_region_line_copied_over_its_core_line_gives_the_same_sheet(tmp_path: Path, name: str, region: str):
    """A region's gen line (with its origin, at=) pasted over its core line in its place: the region keeps its grid cell,
    so every other region packs as before and the sheet is byte-identical (refute:split: a locked origin used to take
    the region off the grid, and the others packed into the hole it left)."""
    board = _ds2_copy(tmp_path) if name == "ds2" else _copy(tmp_path, name)
    first, rep = _emit(board)
    gen = gen_text(load_board(board), rep, name).splitlines()
    reg_line = next(ln for ln in gen if ln.startswith(f'SchRegion("{region}"')).split("  #")[0]
    core = board.parent / "schematic.core.py"
    lines = [reg_line if ln.startswith(f'SchRegion("{region}"') else ln for ln in core.read_text().splitlines()]
    core.write_text("\n".join(lines) + "\n")
    again, _rep = _emit(board)
    assert again == first


def test_a_region_origin_written_elsewhere_holds_and_the_other_regions_keep_their_cells(tmp_path: Path):
    """`SchRegion(at=)` away from where the grid puts the region: the kept sheet has the region at its lock (the passes
    pack it on the grid; the kept one is drawn once more at the lock), and the other regions keep their grid cells
    rather than packing into the hole it leaves."""
    board = _ds2_copy(tmp_path)
    _first, rep = _emit(board)
    before = {r.name: r.origin for r in rep["_layout"].regions}
    x, y = before["adc"]
    core = board.parent / "schematic.core.py"
    lines = core.read_text().splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith('SchRegion("adc"'))
    lines[i] = lines[i].replace("col=1, members=", f"at=({x:g}, {round(y + 2.54, 2):g}), members=")
    core.write_text("\n".join(lines) + "\n")
    _again, rep2 = _emit(board)
    after = {r.name: r.origin for r in rep2["_layout"].regions}
    assert after["adc"] == (x, round(y + 2.54, 2)), after
    assert after["inputs"] == before["inputs"] and after["edge"][0] == before["edge"][0], (before, after)


@pytest.mark.parametrize("name, kind, pins", [
    ("node", "SchLabel", ("U1.IO10", "R_LED.2")),
    ("ds2", "SchLabel", ("R2.2", "R9.2", "R7.2")),
    ("ds2", "SchPower", ("C10.2", "C1.2", "C7.2")),
])
def test_a_gen_label_or_power_line_copied_alone_holds(tmp_path: Path, name: str, kind: str, pins: tuple):
    """refute:split (major, 2026-10-08): some gen SchLabel(at=) and SchPower lines were refused when copied alone into
    core ("would connect nothing", "its stub ... meets symbol C10"). Each, copied alone, now builds with no hard finding
    and comes back unchanged."""
    board = _ds2_copy(tmp_path) if name == "ds2" else _copy(tmp_path, name)
    _first, rep = _emit(board)
    gen = [ln.split("  #")[0] for ln in gen_text(load_board(board), rep, name).splitlines()]
    core = board.parent / "schematic.core.py"
    base = core.read_text()
    for pin in pins:
        line = next((ln for ln in gen if ln.startswith(kind + "(") and f'pin="{pin}"' in ln), None)
        assert line is not None, (kind, pin)
        core.write_text(base + line + "\n")
        _again, rep2 = _emit(board)
        assert rep2["readability"]["hard"] == 0, (line, rep2["readability"]["detail"])
        assert line in [ln.split("  #")[0] for ln in gen_text(load_board(board), rep2, name).splitlines()], line


# ------------------------------------------------------------------ a lock equal to gen is a fixed point (refute:split 2026-10-08)


def _key(line: str) -> tuple:
    import ast

    call = ast.parse(line.split("  #")[0].strip()).body[0].value
    f = call.func.id
    a = [ast.literal_eval(x) for x in call.args]
    kw = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
    if f == "SchSheet":
        return (f,)
    if f in ("SchPlace", "SchRegion", "SchNet"):
        return (f, a[0])
    if f == "SchText":
        return (f, a[0], a[1])
    return (f, a[0], kw.get("pin"))


def _paste(core: Path, lines: list[str]) -> None:
    """Gen lines pasted into schematic.core.py as an author locks what he sees: over the line with the same key, in its
    place; a new key at the end."""
    new = {_key(ln): ln.split("  #")[0] for ln in lines}
    old = core.read_text().splitlines() if core.exists() else []
    out, used = [], set()
    for ln in old:
        k = _key(ln) if ln.startswith("Sch") else None
        if k in new:
            if k not in used:
                out.append(new[k])
                used.add(k)
            continue
        out.append(ln)
    core.write_text("\n".join(out + [new[k] for k in new if k not in used]) + "\n")


@pytest.mark.parametrize(
    "name, core, prefixes",
    [
        ("node", True, ['SchPlace("C_BME"']),  # over its attach line `to="U4.VDD", gap=15.24`: 0 -> 18 hard
        ("node", True, ['SchRegion("mcu"', 'SchPlace("U1"']),  # the region anchor: 0 -> 19 hard
        ("node", True, ['SchPlace("R_BOOT"']),
        ("node", True, ['SchPlace("C_MCU"', 'SchPlace("C_MCU_HF"']),
        ("node", False, ['SchRegion("auto-U1"', 'SchPlace("U1"']),  # 0 -> 7 hard
        ("node", True, ['SchPlace("SW_RST"', 'SchLabel("LED"']),
        ("c3_usb", True, ['SchPlace("C_MCU"']),
        ("c3_usb", True, ['SchPower("VBUS", pin="J1.B4A9"']),
        ("c3_usb", False, ['SchRegion("auto-U2"', 'SchRegion("auto-J1"']),  # the one inferred region left lost its outline
        ("buck", True, ['SchText("L1", "Value"']),  # moved L1's Reference
        ("buck", True, ['SchText("R_EN", "Reference"', 'SchText("C_OUT1", "Reference"']),
        ("ds2", True, ['SchLabel("AIN0", pin="R6.2"']),  # refused: on no wire
        ("ds2", False, ['SchLabel("AIN1", pin="C9.1"']),  # refused: on no wire
        ("ds2", False, ['SchRegion("auto-J4"', 'SchPlace("R1"']),  # 0 -> 1 hard
        ("ds2", True, ['SchText("R4", "Reference"']),  # 0 -> 1 hard
        ("ds2", True, ['SchRegion("inputs"', 'SchPlace("J5"', 'SchPlace("C7"', 'SchText("C9", "Value"']),
    ],
)
def test_gen_lines_pasted_into_core_change_nothing_on_the_sheet(tmp_path: Path, name: str, core: bool, prefixes: list[str]):
    """The invariant (owner via the lead, 2026-10-03/10-08): copying any gen line, or any set of them, into
    schematic.core.py, as the owner would to lock what he sees, is accepted, draws with hard 0 and leaves the whole sheet
    byte-identical: a lock equal to gen is a fixed point, and locking one part never moves another. refute:split
    (blocker, 2026-10-08) found, with real copies, 2 such lines refused, 6 raising the hard number and 117 changing
    the sheet; these are the named ones. Real copies (`load_board` resolves a symlinked board.py to its original)."""
    board = _ds2_copy(tmp_path) if name == "ds2" else _copy(tmp_path, name)
    assert not board.is_symlink()
    if not core:
        (board.parent / "schematic.core.py").unlink()
    first, rep = _emit(board)
    gen = gen_text(load_board(board), rep, name).splitlines()
    picked = [next(ln for ln in gen if ln.startswith(p)) for p in prefixes]
    _paste(board.parent / "schematic.core.py", picked)
    assert any(ln.split("  #")[0] in (board.parent / "schematic.core.py").read_text() for ln in picked)
    again, rep2 = _emit(board)
    assert rep2["readability"]["hard"] == 0, rep2["readability"]["detail"]
    assert again == first
    after = [ln.split("  #")[0] for ln in gen_text(load_board(board), rep2, name).splitlines()]
    assert all(ln.split("  #")[0] in after for ln in picked)


def test_a_pose_lock_equal_to_the_layout_is_recorded_as_a_lock_and_one_that_differs_is_applied(tmp_path: Path):
    """`sch_emit._settle`: a lock equal to the sheet laid out without it is met (gen names its line), and one that differs
    is applied (the part is at the written pose)."""
    board = _copy(tmp_path, "node")
    _first, rep = _emit(board)
    gen = gen_text(load_board(board), rep, "node").splitlines()
    line = next(ln for ln in gen if ln.startswith('SchPlace("R_LED"')).split("  #")[0]
    _paste(board.parent / "schematic.core.py", [line])
    n = len((board.parent / "schematic.core.py").read_text().splitlines())
    _again, rep2 = _emit(board)
    got = next(ln for ln in gen_text(load_board(board), rep2, "node").splitlines() if ln.startswith('SchPlace("R_LED"'))
    assert got == f"{line}  # locked: schematic.core.py:{n}"
    assert rep2["_applied_poses"] == {}
    x = float(line.split("at=(")[1].split(",")[0])
    moved = line.replace(f"at=({line.split('at=(')[1].split(',')[0]},", f"at=({x - 25.4:g},")
    _paste(board.parent / "schematic.core.py", [moved])
    _third, rep3 = _emit(board)
    assert rep3["_applied_poses"] == {"R_LED": n}
    got = next(ln for ln in gen_text(load_board(board), rep3, "node").splitlines() if ln.startswith('SchPlace("R_LED"'))
    assert got.split("  #")[0] == moved
