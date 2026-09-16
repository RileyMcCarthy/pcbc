from pathlib import Path

from pcbc.language import load_board
from pcbc.sch_emit import _layout, _parts_from_design, emit_from_design

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


def test_blinky_schematic_has_led_net():
    design = load_board(BLINKY)
    sch = emit_from_design(design, title="blinky")
    assert sch.startswith("(kicad_sch")
    assert '(label "LED"' in sch
    assert "(symbol" in sch
    assert "default.net" not in sch


def test_blinky_passives_sit_in_one_column():
    design = load_board(BLINKY)
    parts = _parts_from_design(design)
    _layout(parts)
    by = {p.ref: p for p in parts}
    assert abs(by["R1"].x - by["D1"].x) < 0.01
    assert abs(by["R1"].y - by["D1"].y) <= 15.0


def test_blinky_one_led_wire_not_two_stubs():
    sch = emit_from_design(load_board(BLINKY), title="blinky")
    assert sch.count('(label "LED"') == 1
    assert sch.count("(wire") >= 3  # LED + VCC hat + GND hat
