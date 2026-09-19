"""The emitted schematic must be the board's netlist — as KiCad reads it."""

from pathlib import Path

import pytest

from pcbc.language import load_board
from pcbc.netcheck import check_schematic, compare, expected_nets
from pcbc.sch_emit import emit_schematic_file

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BLINKY = EXAMPLES / "blinky" / "blinky.py"
C3_USB = EXAMPLES / "c3_usb" / "c3_usb.py"


def test_expected_nets_expand_pin_names_to_pads():
    nets = expected_nets(load_board(C3_USB))
    assert nets["USB_DP"] >= {("J1", "A6"), ("J1", "B6"), ("U3", "6"), ("U1", "27")}
    assert ("U1", "3") in nets["3V3"]
    assert ("D1", "1") in nets["GND"]  # Led K is pad 1
    assert ("D1", "2") in nets["LED_A"]


def test_compare_identical_is_clean():
    want = {"A": {("R1", "1"), ("U1", "3")}, "GND": {("R1", "2")}}
    assert compare(want, {"A": {("R1", "1"), ("U1", "3")}, "GND": {("R1", "2")}}) == []


def test_compare_reports_wrong_pin_split_rename_and_short():
    want = {"EN": {("R1", "2"), ("U1", "8")}, "BOOT": {("R2", "2"), ("U1", "9")}, "LED": {("R3", "1"), ("U1", "16")}}
    got = {
        "EN": {("R1", "2"), ("U1", "18")},  # mirrored pin
        "Net-(R2-Pad2)": {("R2", "2")},  # split, unnamed
        "unconnected-(U1-IO9-Pad9)": {("U1", "9")},
        "LED": {("R3", "1"), ("U1", "16"), ("U1", "17")},  # short to a neighbour
    }
    fails = compare(want, got)
    text = "\n".join(fails)
    assert "EN: schematic net 'EN' is missing U1.8 and also has U1.18" in text
    assert "BOOT: split across 2 schematic nets" in text
    assert "LED: schematic net 'LED' also has U1.17" in text
    assert "schematic-only net" not in text


def test_compare_flags_schematic_only_nets():
    want = {"A": {("R1", "1"), ("R2", "1")}}
    got = {"A": {("R1", "1"), ("R2", "1")}, "GHOST": {("R1", "2"), ("R2", "2")}}
    assert compare(want, got) == ["schematic-only net 'GHOST' = R1.2, R2.2"]


@pytest.mark.kicad
@pytest.mark.parametrize(
    "board",
    [BLINKY, C3_USB, EXAMPLES / "buck" / "buck.py", EXAMPLES / "node" / "node.py"],
    ids=["blinky", "c3_usb", "buck", "node"],
)
def test_kicad_reads_the_board_netlist(tmp_path: Path, board: Path):
    design = load_board(board)
    sch = emit_schematic_file(design, tmp_path / "schematic.kicad_sch", title=board.stem)
    assert check_schematic(design, sch) == []


BUCK = EXAMPLES / "buck" / "buck.py"


NODE = EXAMPLES / "node" / "node.py"


@pytest.mark.kicad
@pytest.mark.parametrize("board", [BLINKY, C3_USB, BUCK, NODE], ids=["blinky", "c3_usb", "buck", "node"])
def test_erc_is_clean_and_on_grid(tmp_path: Path, board: Path):
    from pcbc.netcheck import check_erc

    design = load_board(board)
    sch = emit_schematic_file(design, tmp_path / "schematic.kicad_sch", title=board.stem)
    erc = check_erc(sch)
    assert erc["errors"] == []
    assert erc["warnings"].get("endpoint_off_grid", 0) == 0
    assert erc["warnings"].get("pin_not_connected", 0) == 0
