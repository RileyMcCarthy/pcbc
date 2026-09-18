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


def test_library_symbol_visual_bbox_includes_value():
    from pcbc.symbol import parse_symbol_layout

    path = (
        Path(__file__).resolve().parent.parent
        / "examples/c3_usb/components/Espressif/ESP32-C3-MINI-1-N4/ESP32-C3-MINI-1-N4.kicad_sym"
    )
    layout = parse_symbol_layout(path.read_text())
    x0, y0, x1, y1 = layout["bbox"]
    assert y1 > 35.0  # Reference above the body
    assert y0 < -35.0  # Value below the body
    assert layout["prop_ref"] == (0.0, 38.10) or abs(layout["prop_ref"][1] - 38.10) < 0.05


def test_library_to_sheet_transform_matches_kicad():
    # Pin 1 of a probe symbol at library (-10.16, 5.08); these sheet offsets are
    # where kicad-cli 10 connected a wire for each instance rotation.
    from pcbc.sch_place import lib_to_sheet

    assert lib_to_sheet(-10.16, 5.08, 0) == (-10.16, -5.08)
    assert lib_to_sheet(-10.16, 5.08, 90) == (-5.08, 10.16)
    assert lib_to_sheet(-10.16, 5.08, 180) == (10.16, 5.08)
    assert lib_to_sheet(-10.16, 5.08, 270) == (5.08, -10.16)


def test_passives_follow_kicad_device_convention():
    # Device:R has pin 1 at library +Y, i.e. the top of the sheet.
    from pcbc.sch_emit import _passive_pins

    p1, p2 = _passive_pins("r", {"1": "VCC", "2": "LED"})
    assert (p1.number, p1.ly, p1.net) == ("1", 3.81, "VCC")
    assert (p2.number, p2.ly, p2.net) == ("2", -3.81, "LED")


C3_USB = Path(__file__).resolve().parent.parent / "examples" / "c3_usb" / "c3_usb.py"


def _placed(board):
    design = load_board(board)
    parts = _parts_from_design(design, {n.name: n.kind for n in design.nets.values()})
    apply_sch_places(design, parts)
    return {p.ref: p for p in parts}


def test_attach_picks_the_pin_on_the_target_net():
    by = _placed(C3_USB)
    r_en, u1 = by["R_EN"], by["U1"]
    en_pin = next(p for p in u1.pins if p.name == "EN")
    ours = next(p for p in r_en.pins if p.net == "EN")  # pin 2, not the default pin 1
    ex, ey = pin_world(u1, en_pin)
    ox, oy = pin_world(r_en, ours)
    # Same row (the wire is straight); the tool may slide it further out to make room.
    assert abs(ey - oy) < 0.01 and 12.0 < ex - ox < 30.0


def test_along_with_side_turns_to_face_the_sibling():
    by = _placed(C3_USB)
    c_en, r_en = by["C_EN"], by["R_EN"]
    assert r_en.rot in (90.0, 270.0)  # horizontal resistor on U1.EN
    assert c_en.rot in (0.0, 180.0)  # cap hangs under the EN node, pin 1 up
    en_pin = next(p for p in c_en.pins if p.net == "EN")
    node = pin_world(r_en, next(p for p in r_en.pins if p.net == "EN"))
    top = pin_world(c_en, en_pin)
    assert abs(top[0] - node[0]) < 0.01 and top[1] > node[1]


def test_gap_defaults_to_room_for_the_label():
    from pcbc.sch_place import _auto_gap

    by = _placed(C3_USB)
    r_boot, u1 = by["R_BOOT"], by["U1"]
    boot = pin_world(r_boot, next(p for p in r_boot.pins if p.net == "BOOT"))
    io9 = pin_world(u1, next(p for p in u1.pins if p.name == "IO9"))
    boot_pin = next(p for p in r_boot.pins if p.net == "BOOT")
    assert _auto_gap(r_boot, boot_pin, u1, {}) == 10.16  # off an IC: room to hang things
    assert io9[0] - boot[0] > 10.16 - 1e-6
    assert _auto_gap(r_boot, boot_pin, by["R_EN"], {}) == 7.62  # "BOOT" label on the wire
    assert _auto_gap(r_boot, next(p for p in r_boot.pins if p.net == "3V3"), by["R_EN"], {"3V3": "power"}) == 5.08


def test_switch_mirrors_to_face_its_node():
    # B3U pin 1 is drawn on the left; beside C_EN it must face right, and a
    # mirror keeps the text readable where a 180 turn would not.
    by = _placed(C3_USB)
    sw = by["SW_RST"]
    assert (sw.rot, sw.mirror) == (0.0, "y")
    sch = emit_from_design(load_board(C3_USB), title="c3_usb")
    i = sch.index('(property "Reference" "SW_RST"')
    assert "(mirror y)" in sch[i - 200 : i]


def test_library_to_sheet_mirror_matches_kicad():
    from pcbc.sch_place import lib_to_sheet

    # Verified with kicad-cli 10: mirror applies in sheet coordinates after the rotation.
    assert lib_to_sheet(-10.16, 5.08, 0, "y") == (10.16, -5.08)
    assert lib_to_sheet(-10.16, 5.08, 0, "x") == (-10.16, 5.08)
    assert lib_to_sheet(-10.16, 5.08, 90, "y") == (5.08, 10.16)


def test_ic_stays_upright_when_attached():
    assert _placed(C3_USB)["U3"].rot == 0.0


def test_rotated_symbol_fields_stay_horizontal():
    sch = emit_from_design(load_board(C3_USB), title="c3_usb")
    # R_EN is a 90/270 symbol; KiCad turns its fields, so the file compensates.
    i = sch.index('(property "Reference" "R_EN"')
    assert " 90)" in sch[i : i + 80]


def test_readability_report_is_empty_for_blinky():
    report: dict = {}
    emit_from_design(load_board(BLINKY), title="blinky", report=report)
    assert report["issues"] == [] and report["count"] == 0
