from pathlib import Path

from pcbc.language import load_board
from pcbc.sch_emit import _fmt, _parts_from_design, _pin_world, emit_from_design
from pcbc.sch_place import apply_sch_places, parse_refpin, pin_world

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"


def test_blinky_schematic_has_led_net():
    design = load_board(BLINKY)
    sch = emit_from_design(design, title="blinky")
    assert sch.startswith("(kicad_sch")
    assert '(label "LED"' in sch
    assert "(symbol" in sch
    assert "default.net" not in sch


def test_blinky_d1_hangs_off_r1_pin():
    design = load_board(BLINKY)
    parts = _parts_from_design(design)
    apply_sch_places(design, parts)
    by = {p.ref: p for p in parts}
    r2 = next(p for p in by["R1"].pins if p.number == "2")
    da = next(p for p in by["D1"].pins if p.name == "A")
    rx, ry = pin_world(by["R1"], r2)
    dx, dy = pin_world(by["D1"], da)
    dist = ((dx - rx) ** 2 + (dy - ry) ** 2) ** 0.5
    assert dist < 20.0
    assert by["D1"].lib_id == "LED"


def test_blinky_one_led_wire_not_two_stubs():
    sch = emit_from_design(load_board(BLINKY), title="blinky")
    assert sch.count('(label "LED"') == 1
    assert sch.count("(wire") >= 1
    assert '(lib_id "LED")' in sch


def test_power_symbols_sit_on_pins_as_one_group():
    design = load_board(BLINKY)
    parts = _parts_from_design(design)
    apply_sch_places(design, parts)
    by = {p.ref: p for p in parts}
    r_vcc = next(_pin_world(by["R1"], pin) for pin in by["R1"].pins if pin.net == "VCC")
    d_gnd = next(_pin_world(by["D1"], pin) for pin in by["D1"].pins if pin.net == "GND")
    sch = emit_from_design(design, title="blinky")
    assert f'(at {_fmt(r_vcc[0])} {_fmt(r_vcc[1])} 0)' in sch
    assert f'(at {_fmt(d_gnd[0])} {_fmt(d_gnd[1])} 0)' in sch
    assert sch.count('(lib_id "power:VCC")') == 1
    assert sch.count('(lib_id "power:GND")') == 1


def test_parse_refpin_slash_name():
    assert parse_refpin("U3.I/O1") == ("U3", "I/O1")
