from pathlib import Path

from pcbc.language import load_board
from pcbc.sch_emit import _fmt, _layout, _parts_from_design, _pin_world, emit_from_design

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
    kinds = {n.name: n.kind for n in design.nets.values()}
    _layout(parts, kinds)
    by = {p.ref: p for p in parts}
    assert abs(by["R1"].x - by["D1"].x) < 0.01
    assert abs(by["R1"].y - by["D1"].y) <= 15.0
    assert by["D1"].lib_id == "LED"
    assert by["R1"].y < by["D1"].y  # VCC/R at smaller Y = top of sheet


def test_blinky_one_led_wire_not_two_stubs():
    sch = emit_from_design(load_board(BLINKY), title="blinky")
    assert sch.count('(label "LED"') == 1
    assert sch.count("(wire") == 1  # only the LED net; power symbols are not exploded
    assert '(lib_id "LED")' in sch


def test_power_symbols_sit_on_pins_as_one_group():
    design = load_board(BLINKY)
    parts = _parts_from_design(design)
    kinds = {n.name: n.kind for n in design.nets.values()}
    _layout(parts, kinds)
    by = {p.ref: p for p in parts}
    r_vcc = next(_pin_world(by["R1"], pin) for pin in by["R1"].pins if pin.net == "VCC")
    d_gnd = next(_pin_world(by["D1"], pin) for pin in by["D1"].pins if pin.net == "GND")
    sch = emit_from_design(design, title="blinky")
    assert f'(at {_fmt(r_vcc[0])} {_fmt(r_vcc[1])} 0)' in sch
    assert f'(at {_fmt(d_gnd[0])} {_fmt(d_gnd[1])} 0)' in sch
    assert sch.count('(lib_id "power:VCC")') == 1
    assert sch.count('(lib_id "power:GND")') == 1
