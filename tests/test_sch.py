from pathlib import Path

from pcbc.language import load_board
from pcbc.sch_emit import _fmt, _parts_from_design, _pin_world, emit_from_design
from pcbc.sch_place import apply_sch_places, parse_refpin, pin_world

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BLINKY = EXAMPLES / "blinky" / "blinky.py"


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
    assert abs(ey - oy) < 0.01 and 10.0 < ex - ox < 30.0


def test_along_with_side_turns_to_face_the_sibling():
    # R_EN (a pull-up to 3V3) stands up from the EN row; C_EN continues below
    # its EN pin in the same column; SW_RST, beside C_EN's node, turns to face it.
    by = _placed(C3_USB)
    c_en, r_en, sw = by["C_EN"], by["R_EN"], by["SW_RST"]
    r_top = pin_world(r_en, next(p for p in r_en.pins if p.net == "3V3"))
    node = pin_world(r_en, next(p for p in r_en.pins if p.net == "EN"))
    assert abs(r_top[0] - node[0]) < 0.01 and r_top[1] < node[1]
    top = pin_world(c_en, next(p for p in c_en.pins if p.net == "EN"))
    assert abs(top[0] - node[0]) < 0.01 and abs((top[1] - node[1]) - 2.54) < 0.01
    sw_en = pin_world(sw, next(p for p in sw.pins if p.net == "EN"))
    assert abs(sw_en[1] - top[1]) < 0.01 and sw_en[0] < top[0]
    assert (sw.rot, sw.mirror) == (0.0, "y")  # pin 1 is drawn on the left; mirrored to face right


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


def test_wire_limit_per_net_and_sheet_default(tmp_path: Path):
    src = BLINKY.read_text()
    board = tmp_path / "blinky.py"
    board.write_text(src.replace('LED = Net("LED")', 'LED = Net("LED", wire_mm=0)'))
    sch = emit_from_design(load_board(board), title="b")
    assert sch.count('(label "LED"') == 2  # labels only, one per pin
    board.write_text(src.replace('LED = Net("LED")', 'LED = Net("LED")\nSchStyle(wire_mm=0)'))
    sch = emit_from_design(load_board(board), title="b")
    assert sch.count('(label "LED"') == 2
    board.write_text(src)
    assert emit_from_design(load_board(board), title="b").count('(label "LED"') == 1


def test_wires_never_cross():
    from pcbc.sch_emit import _crosses

    assert _crosses(0, 0, 10, 0, 5, -5, 5, 5)  # + shape
    assert not _crosses(0, 0, 10, 0, 5, 0, 5, 5)  # T: an endpoint on the line, not a crossing
    assert not _crosses(0, 0, 10, 0, 0, 5, 10, 5)  # parallel
    assert not _crosses(0, 0, 10, 0, 12, -5, 12, 5)  # beside


def test_keepouts_shove_along_the_attach_axis_without_marching():
    by = _placed(C3_USB)
    r_en, c_en = by["R_EN"], by["C_EN"]
    node = pin_world(r_en, next(p for p in r_en.pins if p.net == "EN"))
    top = pin_world(c_en, next(p for p in c_en.pins if p.net == "EN"))
    assert abs(top[0] - node[0]) < 0.01  # still under its node
    assert 0 < top[1] - node[1] < 15.0  # a short wire, not the bottom of the sheet


def test_attached_pin_needs_no_symbol_room():
    # Both input caps hang from the VIN row side by side: the second one's
    # keepout has no symbol zone on its wired pin, so it sits one stagger
    # away, not a symbol's width.
    by = _placed(C3_USB)
    c_vbus, c_hf = by["C_VBUS"], by["C_VBUS_HF"]
    assert "1" in c_hf.wired_pins and "1" in c_vbus.wired_pins
    node = pin_world(c_vbus, next(p for p in c_vbus.pins if p.number == "1"))
    top = pin_world(c_hf, next(p for p in c_hf.pins if p.number == "1"))
    assert abs(top[1] - node[1]) < 0.01 and 2.54 <= abs(top[0] - node[0]) <= 12.7


def test_align_lands_the_hanging_pin_on_another_pins_row(tmp_path: Path):
    import shutil

    src = (EXAMPLES / "c3_usb" / "c3_usb.py").read_text()
    board = tmp_path / "c3_usb.py"
    shutil.copytree(EXAMPLES / "c3_usb" / "components", tmp_path / "components")
    # C_EN continues below R_EN's EN pin; align= picks the gap so C_EN's own
    # EN pin lands on the row of U1.IO1, five pins further down.
    old = 'SchPlace("C_EN", along="R_EN.2", side="bottom")'
    assert old in src
    board.write_text(src.replace(old, 'SchPlace("C_EN", along="R_EN.2", side="bottom", align="U1.IO1")'))
    by = _placed(board)
    io1 = pin_world(by["U1"], next(p for p in by["U1"].pins if p.name == "IO1"))
    top = pin_world(by["C_EN"], next(p for p in by["C_EN"].pins if p.net == "EN"))
    assert abs(top[1] - io1[1]) < 0.01
    node = pin_world(by["R_EN"], next(p for p in by["R_EN"].pins if p.net == "EN"))
    assert abs(top[0] - node[0]) < 0.01 and top[1] > node[1]


def test_wire_label_sits_mid_wire_not_at_the_pin():
    import re

    from pcbc.sch_emit import _text_w

    sch = emit_from_design(load_board(C3_USB), title="c3_usb")
    m = re.search(r'\(label "CC1"\n\t\t\(at ([0-9.]+) ([0-9.]+) 0\)\n\t\t\(effects \(font \(size 1.27 1.27\)\) \(justify (left|right) (bottom|top)\)', sch)
    assert m, "CC1 is a wire label"
    x, y, just, vjust = float(m.group(1)), float(m.group(2)), m.group(3), m.group(4)
    w = _text_w("CC1") + 0.4
    x0, x1 = (x, x + w) if just == "left" else (x - w, x)
    # R_CC1.1 sits 10.16 mm left of J1.CC1: the name lies along that wire, on
    # top of it, and stays clear of the pin end where the number is.
    by = _placed(C3_USB)
    cc1 = pin_world(by["J1"], next(p for p in by["J1"].pins if p.name == "CC1"))
    assert abs(y - cc1[1]) < 0.01 and vjust == "bottom"
    assert cc1[0] - 10.16 - 0.6 <= x0 and x1 <= cc1[0] - 1.0


def test_the_same_board_gives_the_same_file():
    import hashlib
    import os
    import subprocess
    import sys

    d = load_board(BLINKY)
    assert emit_from_design(d, title="blinky") == emit_from_design(d, title="blinky")
    code = (
        "import hashlib, sys; from pathlib import Path; from pcbc.language import load_board; "
        "from pcbc.sch_emit import emit_from_design; "
        "print(hashlib.sha1(emit_from_design(load_board(Path(sys.argv[1])), title='blinky').encode()).hexdigest())"
    )
    digests = {
        subprocess.run(
            [sys.executable, "-c", code, str(BLINKY)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("1", "2", "random")
    }
    assert len(digests) == 1
    sch = emit_from_design(d, title="blinky")
    assert "#PWR001" in sch and "#PWR002" in sch  # power symbols numbered, not random


def test_check_catches_a_schplace_on_a_missing_pin(tmp_path: Path):
    from pcbc.language import check_board

    src = BLINKY.read_text()
    assert 'to="R1.2"' in src
    board = tmp_path / "blinky.py"
    board.write_text(src.replace('to="R1.2"', 'to="R1.7"'))
    fails = check_board(board)
    assert len(fails) == 1 and fails[0].startswith("schematic:") and "R1" in fails[0] and "'7'" in fails[0]
    assert check_board(BLINKY) == []


def test_pcbc_sch_prints_the_list(tmp_path: Path, capsys):
    from pcbc.cli import main

    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    rc = main(["sch", str(board)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "schematic: " in out and "netlist: " in out and "readability: nothing overlaps" in out
    assert (tmp_path / "layout" / "blinky" / "schematic.kicad_sch").exists()


def test_report_says_how_to_share_a_node(tmp_path: Path):
    import shutil

    src = (EXAMPLES / "c3_usb" / "c3_usb.py").read_text()
    old = 'SchPlace("C_MCU_HF", along="C_MCU.1", side="bottom")'
    assert old in src
    shutil.copytree(EXAMPLES / "c3_usb" / "components", tmp_path / "components")
    board = tmp_path / "c3_usb.py"
    board.write_text(src.replace(old, 'SchPlace("C_MCU_HF", to="U1.3V3")'))
    report: dict = {}
    emit_from_design(load_board(board), title="c3", report=report)
    # Hung off U1.3V3 as well, it hangs from the same row one stagger out and
    # is wired along it: no symbol of its own, nothing to report.
    assert not [i for i in report["issues"] if "carries its own" in i], report["issues"]
    by = _placed(board)
    rail = pin_world(by["C_MCU"], next(p for p in by["C_MCU"].pins if p.net == "3V3"))
    top = pin_world(by["C_MCU_HF"], next(p for p in by["C_MCU_HF"].pins if p.net == "3V3"))
    assert abs(top[1] - rail[1]) < 0.01 and "1" in by["C_MCU_HF"].wired_pins
    # And the shipped example gets no such line either.
    report = {}
    emit_from_design(load_board(EXAMPLES / "c3_usb" / "c3_usb.py"), title="c3", report=report)
    assert not [i for i in report["issues"] if "carries its own" in i], report["issues"]


def test_multi_unit_symbols_are_refused(tmp_path: Path):
    import shutil

    from pcbc.language import check_board

    src = EXAMPLES / "node"
    shutil.copytree(src / "components", tmp_path / "components")
    sym = tmp_path / "components" / "TI" / "LMV321IDBVR" / "LMV321IDBVR.kicad_sym"
    text = sym.read_text()
    # Give the op-amp a second unit the way a KiCad library dual op-amp has:
    # the one sub-symbol becomes unit 1 and a copy of it unit 2.
    from pcbc.sexp import matching_paren

    i = text.index('(symbol "LMV321IDBVR_0_1"')
    block = text[i : matching_paren(text, i) + 1]
    unit1 = block.replace('"LMV321IDBVR_0_1"', '"LMV321IDBVR_1_1"', 1)
    unit2 = block.replace('"LMV321IDBVR_0_1"', '"LMV321IDBVR_2_1"', 1)
    text = text[:i] + unit1 + "\n    " + unit2 + text[i + len(block) :]
    sym.write_text(text)
    board = tmp_path / "node.py"
    board.write_text((src / "node.py").read_text())
    fails = [f for f in check_board(board, pcb=False) if "units" in f]
    assert fails and fails[0].startswith("U5:") and "2 units" in fails[0], fails
