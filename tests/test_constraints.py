"""R1 slice S2 (docs/r1-design.md sections A and D): every NetReq / Pair / Bus / Chain / Isolation /
Guard line becomes numbers with their sources, the five boards keep today's classes and nets, and
every refusal names its line."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pcbc.compile import CompiledClass, DruRule, compile_design
from pcbc.constraints import CREEPAGE_FROM_V, PAIR_FIT_MM, PRESETS, SOFT_RULES, Derived, Source, compile_constraints
from pcbc.language import check_board, load_board

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "compiled"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"


def _ds2(tmp_path: Path) -> Path:
    """The DS2 Addon, copied whole (board + components) so nothing is ever built in the MaD checkout."""
    if not DS2.exists():
        pytest.skip(f"DS2 Addon not at {DS2}")
    shutil.copytree(DS2, tmp_path / "ds2")
    return tmp_path / "ds2" / "ds2_addon.py"


def _boards(tmp_path: Path) -> dict[str, Path]:
    boards = {b: EXAMPLES / b / f"{b}.py" for b in ("blinky", "buck", "c3_usb", "node")}
    if DS2.exists():
        boards["ds2_addon"] = _ds2(tmp_path)
    return boards


# A synthetic board with a net for every kind of section D; the NetReq lines start at 15.
HEAD = '''from pcbc import *
VCC = Power("VCC"); GND = Ground("GND"); HV = Power("HV")
Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2="SIG")
Resistor("R2", "1k", package="0603", mpn="X", lcsc="C1", p1="SIG", p2="FB")
Resistor("R3", "1k", package="0603", mpn="X", lcsc="C1", p1="SW", p2="CLK")
Resistor("R4", "1k", package="0603", mpn="X", lcsc="C1", p1="SCK", p2="MOSI")
Resistor("R5", "1k", package="0603", mpn="X", lcsc="C1", p1="SDA", p2="SCL")
Resistor("R6", "1k", package="0603", mpn="X", lcsc="C1", p1="SENSE_P", p2="SENSE_N")
Resistor("R7", "1k", package="0603", mpn="X", lcsc="C1", p1="D_P", p2="D_N")
Resistor("R8", "1k", package="0603", mpn="X", lcsc="C1", p1="AIN0", p2="MISO")
Resistor("R9", "1k", package="0603", mpn="X", lcsc="C1", p1=HV, p2="Z50")
Capacitor("C1", "1uF", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)
Capacitor("C2", "1uF", package="0603", mpn="X", lcsc="C1", p1="SDA", p2=GND)
'''
TAIL = '''
for i, r in enumerate(["R1","R2","R3","R4","R5","R6","R7","R8","R9","C1","C2"]):
    Place(r, at=(5 + 3*i, 5)); SchPlace(r, left=20 + 30*i, top=20)
'''
EVERY_KIND = '''NetReq("VCC", "GND", kind="power", volts=5, amps=1)
NetReq("HV", kind="power", volts=250, amps=0.5)
NetReq("FB", kind="feedback", keep_clear_of="SW")
NetReq("SW", kind="switch_node", loop_mm2=45)
NetReq("CLK", kind="clock")
NetReq("D_P", "D_N", kind="usb_hs")
NetReq("SCK", "MOSI", "MISO", kind="spi", clock="SCK", length_mm=80)
NetReq("SDA", "SCL", kind="i2c", pf_max=400)
NetReq("SENSE_P", "SENSE_N", kind="sense")
NetReq("AIN0", kind="analog", keep_clear_of="SW", keep_clear_mm=3)
NetReq("Z50", z_se_ohm=50)
NetReq("SIG", kind="digital", volts=48)
'''
TWO = 'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")\n'
FOUR = 'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu")])\n'


def _board(tmp_path: Path, name: str, board_line: str, reqs: str, head: str = HEAD, tail: str = TAIL) -> Path:
    p = tmp_path / f"{name}.py"
    p.write_text(head + board_line + reqs + tail)
    return p


def _expect(lines: tuple[str, ...], wanted: list[tuple[str, str]]) -> None:
    for line, why in wanted:
        assert line in lines, f"{why}\nmissing: {line}\nnear: " + "\n".join(x for x in lines if x.split(':')[0] == line.split(':')[0])


# ---------------------------------------------------------------------------------------------
# The five boards: today's numbers, byte for byte (node's USB pair excepted)
# ---------------------------------------------------------------------------------------------


def test_the_five_boards_keep_todays_classes_nets_and_krt(tmp_path: Path):
    """Snapshot fixtures were recorded from CompiledJob.to_dict() before S1; the projection of the
    ConstraintSet reproduces every recorded key. New keys (lane_clearance_mm) are allowed."""
    for name, path in _boards(tmp_path).items():
        was = json.loads((FIXTURES / f"{name}.json").read_text())
        now = json.loads(json.dumps(compile_design(load_board(path)).to_dict()))
        for key in ("nets", "krt", "keepouts", "places", "regions", "planes", "skip_autoroute_patterns"):
            assert now[key] == was[key], (name, key)
        classes = [{k: v for k, v in c.items() if k != "lane_clearance_mm"} for c in now["classes"]]
        if name == "node":
            # H.2: jlcpcb_4l_1oz is JLC04161H-7628, so the 90 ohm pair is 0.2288 / 0.15 (was 0.1554 / 0.12).
            usb = next(c for c in classes if c["name"] == "USB")
            assert (usb["track_width_mm"], usb["diff_pair_width_mm"], usb["diff_pair_gap_mm"]) == (0.2288, 0.2288, 0.15), "C.2 vector 9: JLC row 0.2332 within 2.3 %"
            classes = [c for c in classes if c["name"] != "USB"]
            was["classes"] = [c for c in was["classes"] if c["name"] != "USB"]
        assert classes == was["classes"], name
        assert "constraints" in now and "rule_areas" in now


def test_to_dict_round_trips_identically_on_two_compiles(tmp_path: Path):
    for name, path in _boards(tmp_path).items():
        a = json.dumps(compile_constraints(load_board(path)).to_dict(), sort_keys=True)
        b = json.dumps(compile_constraints(load_board(path)).to_dict(), sort_keys=True)
        assert a == b, name
        d = json.loads(a)
        width = d["constraints"][0]["width_mm"]
        assert set(width) == {"value", "unit", "formula", "ref", "note"}, "A.1: Derived -> {value, unit, formula, ref, note}"


def test_every_existing_consumer_keeps_its_interface():
    job = compile_design(load_board(EXAMPLES / "node" / "node.py"))
    assert isinstance(job.classes[0], CompiledClass) and isinstance(job.dru[0], DruRule)
    assert job.constraints is not None and job.constraints.canary_net == "3V3"
    power = next(c for c in job.classes if c.name == "Power")
    assert power.lane_clearance_mm == 0.2 and power.patterns == ["VBUS", "3V3", "GND", "LOAD"], "node's two power lines share one class (identical numbers)"
    load = job.constraints.by_net("LOAD")
    assert load is not None and load.class_name == "Power" and load.line == 146 and load.req_index == 3


# ---------------------------------------------------------------------------------------------
# The language: exact signatures, lines, refusals
# ---------------------------------------------------------------------------------------------


def test_an_unknown_kwarg_is_refused_with_the_did_you_mean_line(tmp_path: Path):
    """A.4: no **kwargs (amp=2 used to be a silent 0.2 A net); a TypeError at load that check names."""
    p = _board(tmp_path, "typo", TWO, 'NetReq("VCC", kind="power", amp=2)\n')
    assert check_board(p) == ["NetReq(\"VCC\") line 15: unexpected keyword 'amp'; did you mean amps=?"]
    p = _board(tmp_path, "typo2", TWO, 'Pair("D_P", "D_N", skew=2)\n')
    assert check_board(p) == [
        "Pair(\"D_P\") line 15: unexpected keyword 'skew'; it takes p=, n=, z_diff_ohm=, match_mm=, uncoupled_mm=, gap_mm=, layers=, reference="
    ]
    with pytest.raises(TypeError):
        load_board(p)


def test_a_kind_foreign_kwarg_and_an_unknown_kind_are_refused_citing_the_line(tmp_path: Path):
    p = _board(tmp_path, "foreign", TWO, 'NetReq("VCC", kind="power", amps=1)\nNetReq("SW", kind="switch_node", pf_max=3)\nNetReq("SIG", kind="spy")\n')
    fails = check_board(p)
    assert 'NetReq("SW") line 16: kind="switch_node" does not take pf_max=; it takes loop_mm2, amps, max_mm, layers, vias, autoroute, class_name, keep_clear_of, keep_clear_mm, volts, z_se_ohm' in fails, "D: switch_node's own kwargs then A.4's common set"
    assert 'NetReq("SIG") line 17: unknown kind \'spy\'; known: analog, clock, feedback, generic, i2c, power, sense, spi, switch_node, usb_hs' in fails
    assert len(fails) == 2, fails
    assert set(PRESETS) == {"generic", "power", "analog", "switch_node", "clock", "usb_hs", "spi", "i2c", "sense", "feedback"}


def test_the_line_is_the_board_files_line_and_zero_from_python():
    from pcbc.language import NetReq, reset

    reset()
    assert NetReq("X", kind="power").line == 0
    design = load_board(EXAMPLES / "node" / "node.py")
    assert [r.line for r in design.netreqs] == [143, 144, 145, 146]


def test_every_refusal_of_a4_is_pinned(tmp_path: Path):
    reqs = '''NetReq("VCC", "GND", kind="power", volts=5, amps=1)
NetReq("VCC", kind="analog")
NetReq("D_P", "D_N", kind="usb_hs")
Pair("D_P", "D_N")
Pair("D_P", "D_N")
Pair("SENSE_P", "SIG")
NetReq("SCK", "MOSI", "MISO", kind="spi")
Bus("AIN0", "MISO", match_mm=1.0, clock="CLK")
Chain("VCC", "C1.2", "R1.1")
Chain("VCC", "C9.1", "R1.1")
Guard("NOPE")
Isolation("primary", "secondary", volts=250)
'''
    p = _board(tmp_path, "refuse", TWO, reqs)
    assert check_board(p) == [
        "VCC: NetReq line 15 and NetReq line 16 both name it; say it once (Pair overrides only z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference)",
        # the first Pair on the usb_hs NetReq is the legal override (A.4 precedence); the second names the net again
        "D_P: NetReq line 17 and Pair line 19 both name it; say it once (Pair overrides only z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference)",
        'NetReq("SCK") line 21: kind="spi" with 3 nets needs clock=; which one is the clock?',
        "Pair line 20: KiCad pairs names ending P/N, _P/_N or +/-; rename SENSE_P/SIG",
        'Bus line 22: clock "CLK" is not one of AIN0, MISO',
        'Chain("VCC") line 23: C1.2 is on GND, not VCC',
        'Chain("VCC") line 24: no part C9',
        'Guard("NOPE") line 25: no net "NOPE"',
        'Isolation line 26: no Region "primary"',
        'Isolation line 26: no Region "secondary"',
    ]


def test_a_controlled_pair_on_four_layers_wants_its_plane_declared(tmp_path: Path):
    """F.6 at compile: node with planes=[] is refused before any geometry."""
    src = (EXAMPLES / "node" / "node.py").read_text().replace('planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")]', "planes=[]")
    (tmp_path / "node.py").write_text(src)
    shutil.copytree(EXAMPLES / "node" / "components", tmp_path / "components")
    assert compile_constraints(load_board(tmp_path / "node.py")).refusals == (
        'USB_DP: 90 ohm on F.Cu wants a plane on In1.Cu; Board(planes=[("GND", "In1.Cu")]) declares it',
    )
    assert check_board(EXAMPLES / "node" / "node.py") == []


# ---------------------------------------------------------------------------------------------
# Every kind of section D on 2L and 4L, lines pinned as exact strings
# ---------------------------------------------------------------------------------------------


def test_every_kind_compiles_on_two_layers_and_its_lines_are_pinned(tmp_path: Path):
    cs = compile_constraints(load_board(_board(tmp_path, "two", TWO, EVERY_KIND)))
    assert cs.refusals == ()
    _expect(cs.lines, [
        # power: C.6 with the pcbc floor; 1 A over the 2L pour: IPC-2221 0.300 and IPC-2152 0.183 under the 0.4 floor
        ("VCC: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm B.Cu pour 0.183)", "D power: pcbc_floor 0.4 at amps >= 0.2; C.6 board mod 1.092 (Fig 5-8), plane mod 0.593 at 1.53 mm (Fig 5-11)"),
        ("VCC: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)", "D power: clearance max(0.20, B2 row); IPC-2221B 6-1 0-15 V B2 = 0.1"),
        ("VCC: via 0.8/0.4 mm (preset power), 2 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]", "C.7: 0.4 mm drill at 18 um plating carries 0.871 A at 10 C; ceil(1/0.871) = 2, not a gate"),
        ("VCC: loop 6 mm2 (preset power; decap loop, pcbc default)", "D power: loop_mm2 6.0 decap loop, pcbc default"),
        ("VCC: layers F.Cu, B.Cu, In1.Cu, In2.Cu (preset power)", "today's power layers tuple, kept byte for byte"),
        ("VCC: spacing 3W (preset power)", "D: 3W"),
        # power at 250 V: the row wins the class clearance and creepage is written at >= 60 V
        ("HV: clearance 1.25 mm (ipc2221_6_1 IPC-2221B 6-1 row 171-250 V B2; over preset power 0.2)", "C.8 Table 6-1 171-250 V B2 = 1.25"),
        ("HV: creepage 2.5 mm (iec60664_f5 IEC 60664-1 F.5 row 250 V IIIa PD2)", "C.8 F.5 250 V IIIa PD2 = 2.5 (SLUP419 Table 3)"),
        ("HV: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 0.5 A 10 C 1 oz 0.150; ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm B.Cu pour 0.052)", "C.6 at 0.5 A: both fits under the 0.4 floor"),
        # feedback / analog / sense: 0.2/0.2, no vias, F.Cu, airwire, keep-away explicit
        ("FB: width 0.2 mm (preset feedback)", "D feedback: 0.20"),
        ("FB: no vias (preset feedback)", "D feedback: no vias"),
        ("FB: airwire 15 mm (preset feedback)", "D feedback: airwire 15"),
        ("FB: keep_clear_of SW 3 mm (preset feedback; 3 mm when keep_clear_of is given)", "D feedback: keep_clear_mm 3.0 when keep_clear_of given"),
        ("FB: spacing 5W (preset feedback)", "D: 5W"),
        ("AIN0: airwire 25 mm (preset analog)", "D analog: airwire 25 (today)"),
        ("AIN0: keep_clear_of SW 3 mm (NetReq line 24)", "D analog: keep_clear_mm written on the line"),
        ("AIN0: layers F.Cu (preset analog)", "D analog: F.Cu"),
        ("SENSE_P: bus SENSE_P, SENSE_N within 1 mm (preset sense) [soft: warning in R1]", "D sense: exactly two nets -> bus, match_mm 1.0 (R-A2)"),
        ('SENSE_P: keep_clear_of none; NetReq("SENSE_P", kind="sense", keep_clear_of="SW") holds 3 mm', "D notes: keep-away is explicit; the board has a switch_node net"),
        # switch_node: 0.30, no vias, airwire 8, hot loop overridden on the line
        ("SW: width 0.3 mm (preset switch_node)", "D switch_node: 0.30"),
        ("SW: airwire 8 mm (preset switch_node)", "D switch_node: airwire 8"),
        ("SW: loop 45 mm2 (NetReq line 18; overrides preset switch_node 20)", "D switch_node: loop_mm2 20.0 hot loop, pcbc default; the line overrides it"),
        # clock: 0.15, budget 2
        ("CLK: width 0.15 mm (preset clock)", "D clock: 0.15"),
        ("CLK: via 0.6/0.3 mm (preset clock), at most 2 (preset clock) [soft: warning in R1]", "D clock: 0.6/0.3, budget 2 (soft via_budget)"),
        ("CLK: spacing 5W (preset clock)", "D: 5W"),
        # usb_hs on 2L: the pair-fit clamp (C.3), stated
        ("D_P: pair with D_N, 0.127 mm wide, gap 0.127 mm on F.Cu over the B.Cu pour: 140.05 ohm (hj_coupled_microstrip x 1 jlcpcb_2l_1oz; formula only; target 90 +-15 %; uncontrolled; preset usb_hs)", "C.10 vector 10: zdiff(0.127, 0.127, 1.53, 0.035, 4.6) = 140.05; bias 1.0 (uncalibrated)"),
        ('D_P/D_N: 90 ohm needs 0.7746 mm members at gap 0.15 on jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; fine for USB full speed, use Board(stackup="jlcpcb_4l_1oz") for high speed', "C.3 pair-fit clamp note: solved member 0.7746 > PAIR_FIT_MM 0.25 (vector 10)"),
        ("D_P: clearance 0.155 mm (class_floor 0.16 over hole_clearance 0.25 - ring 0.1 + 0.005 = 0.155)", "D usb_hs: min(0.16, w) then the floor: 0.155 on 2L"),
        ("D_P: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]", "D usb_hs: stackup via, budget 2"),
        ("D_P: skew 0.5 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5: skew 0.5 (TI usb_layout_basics, the tight number)"),
        ("D_P: uncoupled 2 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5: uncoupled 2.0"),
        ("D_P: length none (give length_mm=)", "H.6: no preset USB length rule"),
        # spi: bus matched to the clock, length written on the line
        ("MISO: bus SCK, MOSI, MISO matched to SCK within 2.5 mm (preset spi) [soft: warning in R1]", "D spi: match_mm 2.5 (about 15 ps on FR-4, pcbc default)"),
        ("MISO: length 80 mm (NetReq line 21)", "D spi: length_mm when given -> KiCad length (max)"),
        ("MISO: clearance 0.2 mm (preset spi)", "D spi: 0.20"),
        ("MISO: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "D: floor max(0.16, track_min)"),
        # i2c: length from capacitance at the class width on the outer layer
        ("SDA: length 9405.94 mm (i2c_capacitance 400 pF (NetReq line 22) - 10 pF x 2 pins on the busiest line at 0.0404 pF/mm (0.16 mm F.Cu))", "C.9: 0.16 mm on 2L is 0.0391 pF/mm (vector 25); (400 - 30) / 0.0391"),
        # generic with volts=48: B2 row 0.6 over the floor, no creepage under 60 V
        ("SIG: clearance 0.6 mm (ipc2221_6_1 IPC-2221B 6-1 row 31-50 V B2; over class_floor 0.16)", "C.8 Table 6-1 31-50 V B2 = 0.6 (vector 22)"),
        ("SIG: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "D generic: floor"),
        # z_se_ohm on 2L: CPWG with the pour at s = max(0.3, 2 x clearance)
        ("Z50: width 1.5912 mm (ghione_naldi_cpwg x 1 jlcpcb_2l_1oz pour gap 0.32 t = 0; uncalibrated: formula only)", "D generic z_se_ohm on 2L: C.5 with the pour at max(0.3, 2 x 0.16) = 0.32"),
        ("Z50: 1.5912 mm on F.Cu over the B.Cu pour: 50 ohm (ghione_naldi_cpwg x 1 jlcpcb_2l_1oz pour gap 0.32 t = 0; uncalibrated: formula only; target 50)", "C.5 Ghione-Naldi, bias 1.0 on 2L"),
        # classes: the 250 V power line cannot share Power (its clearance differs), so it is Power_2 and the report says so
        ("class Power_2 (Power is NetReq line 15 at 0.4 mm)", "A.3: a later NetReq with different numbers gets {base}_{n}; today it silently overwrote"),
        ("class Default_2 (Default is the class floor at 0.16/0.16 mm)", "A.3: SIG's 0.6 clearance cannot share Default"),
        ("classes: Default 0.16/0.16, Power 0.4/0.2 via 0.8/0.4, Power_2 0.4/1.25 via 0.8/0.4, Feedback 0.2/0.2 via 0.6/0.3, SwitchNode 0.3/0.2 via 0.6/0.3, Clock 0.15/0.2 via 0.6/0.3, USB 0.127/0.155 pair 0.127/0.127, SPI 0.16/0.2, I2C 0.16/0.2, Sense 0.2/0.2 via 0.6/0.3, Analog 0.2/0.2 via 0.6/0.3, Z50 1.5912/0.16, Default_2 0.16/0.6", "the class summary"),
    ])
    sig = cs.by_net("SIG")
    assert sig is not None and sig.voltage is not None and sig.voltage.creepage_mm is None and not any(l.startswith("SIG: creepage") for l in cs.lines), "C.8 policy: creepage only at volts >= 60 (IEC 62368-1 ES1)"
    hv = cs.by_net("HV")
    assert hv is not None and hv.lane_clearance_mm == 0.2 and hv.clearance_mm.value == 1.25, "A.3: lane_clearance_mm is the kind number alone"
    assert hv.voltage is not None and hv.voltage.creepage_mm is not None and hv.voltage.creepage_mm.value == 2.5
    d_p = cs.by_net("D_P")
    assert d_p is not None and d_p.pair is not None and d_p.pair.controlled is False and d_p.pair.partner == "D_N"
    assert d_p.soft == ("track_width", "skew", "via_budget", "diff_pair_uncoupled") and set(d_p.soft) <= set(SOFT_RULES)
    assert CREEPAGE_FROM_V == 60.0 and PAIR_FIT_MM == 0.25
    assert [c.net for c in cs.constraints] == sorted(c.net for c in cs.constraints)


def test_every_kind_compiles_on_four_layers_and_its_lines_are_pinned(tmp_path: Path):
    cs = compile_constraints(load_board(_board(tmp_path, "four", FOUR, EVERY_KIND)))
    assert cs.refusals == ()
    _expect(cs.lines, [
        ("VCC: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.099 x plane 0.430 at 0.2104 mm In1.Cu 0.134)", "C.6 node case: 1 A over In1.Cu at 0.2104 -> plane mod 0.430; board mod 1.099 on the 1.5862 mm literal stack (S1: not the 1.6 nominal's 1.092)"),
        ("VCC: via 0.8/0.4 mm (preset power), 2 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]", "C.7 on the power class's 0.4 drill"),
        ("D_P: pair with D_N, 0.2288 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 90 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.2332/0.15; target 90 +-15 %; preset usb_hs)", "C.10 vector 9: (0.2288, 0.15) on 7628 with bias 0.8532; JLC row 0.2332/0.15; diff_pair_z rounds to 90.0"),
        ("D_P: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005)", "D usb_hs: the floor is 0.18 on 4L"),
        ("D_P: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]", "B.2 4L via 0.35/0.2"),
        ("Z50: width 0.3244 mm (hj_microstrip x 0.9147 JLC04161H-7628; fitted to JLC04161H-7628 rows (JITX), 2026-09-19)", "C.10 vector 5: width_for_z0(50, jlcpcb_4l_1oz) = 0.3244 (the JLC row, by construction of the bias)"),
        ("Z50: 0.3244 mm on F.Cu over In1.Cu (GND): 50 ohm (hj_microstrip x 0.9147 JLC04161H-7628; fitted to JLC04161H-7628 rows (JITX), 2026-09-19; target 50)", "D: reference plane_below(F.Cu) = In1.Cu (GND) when declared"),
        ("MISO: width 0.16 mm (class_floor max(0.16, track_min 0.0889))", "D: floor max(0.16, 4L track_min 0.0889)"),
        ("SDA: length 4175.82 mm (i2c_capacitance 400 pF (NetReq line 22) - 10 pF x 2 pins on the busiest line at 0.091 pF/mm (0.16 mm F.Cu))", "C.9 on 7628: 0.16 mm at 0.2104 mm Dk 4.4 with bias 0.9064"),
        ("SIG: clearance 0.6 mm (ipc2221_6_1 IPC-2221B 6-1 row 31-50 V B2; over class_floor 0.18)", "C.8 31-50 V B2 = 0.6 over the 4L floor 0.18"),
        ("class Default_2 (Default is the class floor at 0.16/0.18 mm)", "A.3 collision on the 4L floor"),
    ])
    z50 = cs.by_net("Z50")
    assert z50 is not None and z50.reference == "In1.Cu" and z50.class_name == "Z50" and z50.z_se == (50.0, Derived(50.0, "ohm", Source("hj_microstrip", "x 0.9147 JLC04161H-7628", "fitted to JLC04161H-7628 rows (JITX), 2026-09-19; target 50")))
    d_p = cs.by_net("D_P")
    assert d_p is not None and d_p.pair is not None and d_p.pair.controlled and d_p.reference == "In1.Cu"
    assert d_p.pair.z_computed.value == 90.0 and d_p.pair.tolerance_pct == 15.0


def test_a_netreq_at_48_v_gets_0_6_and_no_creepage_and_250_v_gets_1_25_and_2_5(tmp_path: Path):
    reqs = 'NetReq("VCC", kind="power", volts=48)\nNetReq("HV", kind="power", volts=250)\nNetReq("SIG", volts=12)\nNetReq("GND", kind="power", volts=3.3)\n'
    cs = compile_constraints(load_board(_board(tmp_path, "volts", TWO, reqs)))
    vcc, hv = cs.by_net("VCC"), cs.by_net("HV")
    assert vcc is not None and vcc.clearance_mm.value == 0.6 and vcc.voltage is not None and vcc.voltage.creepage_mm is None, "C.8: 48 V B2 = 0.6 (vector 22); under 60 V no creepage"
    assert hv is not None and hv.clearance_mm.value == 1.25 and hv.voltage is not None and hv.voltage.creepage_mm is not None and hv.voltage.creepage_mm.value == 2.5, "C.8: 250 V B2 = 1.25, F.5 IIIa = 2.5"
    for net in ("SIG", "GND"):
        c = cs.by_net(net)
        assert c is not None and c.voltage is not None and c.voltage.creepage_mm is None, "3.3 and 12 V nets get no creepage rule"
    assert [l for l in cs.lines if "creepage" in l] == ["HV: creepage 2.5 mm (iec60664_f5 IEC 60664-1 F.5 row 250 V IIIa PD2)"]


# ---------------------------------------------------------------------------------------------
# Pair, Bus, Chain, Isolation, Guard
# ---------------------------------------------------------------------------------------------


def test_pair_bus_chain_and_guard_load_validate_and_override_never_silently(tmp_path: Path):
    reqs = '''NetReq("VCC", "GND", kind="power", volts=5, amps=1)
NetReq("D_P", "D_N", kind="usb_hs")
Pair("D_P", "D_N", z_diff_ohm=100, match_mm=0.3, gap_mm=0.2)
Pair("SENSE_P", "SENSE_N", z_diff_ohm=100)
Bus("SCK", "MOSI", "MISO", match_mm=1.0, clock="SCK", length_mm=60)
Chain("VCC", "C1.1", "R1.1")
Guard("AIN0", stitch_mm=2.0)
'''
    p = _board(tmp_path, "groups", FOUR, reqs)
    design = load_board(p)
    assert (len(design.pairs), len(design.buses), len(design.chains), len(design.guards)) == (2, 1, 1, 1)
    assert design.pairs[0].line == 17 and design.buses[0].line == 19 and design.chains[0].line == 20 and design.guards[0].line == 21
    cs = compile_constraints(design)
    assert cs.refusals == ()
    _expect(cs.lines, [
        ("D_P: pair with D_N, 0.2004 mm wide, gap 0.2 mm on F.Cu over In1.Cu (GND): 100 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.1722/0.15; target 100 +-15 %; Pair line 17)", "A.4 precedence: Pair overrides z_diff_ohm and gap_mm on the usb_hs NetReq; C.3 solved at gap 0.2"),
        ("D_P: skew 0.3 mm (Pair line 17; overrides preset usb_hs 0.5) [soft: warning in R1]", "A.4: never silent"),
        ("D_P: uncoupled 2 mm (Pair line 17; overrides preset usb_hs 2) [soft: warning in R1]", "A.4: the Pair's own field, printed"),
        ("SENSE_P: pair with SENSE_N, 0.1759 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 100.01 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.1722/0.15; target 100 +-10 %; Pair line 18)", "C.10 vector 9: 100 ohm on 7628 = 0.1762/0.15 (JLC row 0.1722); a bare Pair is +-10 %"),
        ("SENSE_P: skew 0.5 mm (Pair line 18) [soft: warning in R1]", "D: a bare Pair carries its own numbers"),
        ("MISO: bus SCK, MOSI, MISO matched to SCK within 1 mm (Bus line 19) [soft: warning in R1]", "D: Bus() sets the group on generic constraints"),
        ("MISO: length 60 mm (Bus line 19)", "A.2 BusReq.length_mm"),
        ("VCC: chain C1.1 -> R1.1 (Chain line 20)", "A.2 ChainReq, pads as Place(to=) takes them"),
        ("AIN0: guard stitch 2 mm (Guard line 21; R2 pattern)", "D: Guard records guard_stitch_mm (printed; R2 pattern)"),
    ])
    job = compile_design(design)
    assert [c.name for c in job.classes] == ["Default", "Power", "USB", "Pair_SENSE_P"], "D: a bare Pair gets class Pair_<p>"
    assert job.krt["usb_pairs"] == [{"nets": ["D_P", "D_N"], "class": "USB"}, {"nets": ["SENSE_P", "SENSE_N"], "class": "Pair_SENSE_P"}]
    assert job.krt["length_match"] == [{"nets": ["SCK", "MOSI", "MISO"], "tolerance_mm": 1.0}, {"nets": ["SENSE_P", "SENSE_N"], "tolerance_mm": 0.5}]
    assert [g.kind for g in cs.groups] == ["bus", "chain"]
    ain0 = cs.by_net("AIN0")
    assert ain0 is not None and ain0.guard_stitch_mm == 2.0 and ain0.line == 0 and ain0.kind == "generic", "A.1: line 0 when synthesised from Pair/Bus alone"


def test_chain_on_the_ds2_addon_validates_its_pads_against_the_net(tmp_path: Path):
    """`U1.12` is AVDD (pad 12): pin names or pad numbers, as Place(to=) takes them."""
    board = _ds2(tmp_path)
    lines = board.read_text().split("\n")
    # The DS2 board carries the line S4's chain check asked for (R1: an intent line, never a coordinate).
    assert lines[96].startswith('Chain("VDDA", "J2.1", "C4.1", "U1.12")'), "line 97 of ds2_addon.py is the VDDA chain"
    cs = compile_constraints(load_board(board))
    assert cs.refusals == () and cs.groups[0].members == ("J2.1", "C4.1", "U1.12")
    assert "VDDA: chain J2.1 -> C4.1 -> U1.12 (Chain line 97)" in cs.lines
    lines[96] = 'Chain("VDDA", "J2.1", "C4.2", "U1.12")'
    board.write_text("\n".join(lines))
    assert check_board(board) == ['Chain("VDDA") line 97: C4.2 is on VSS, not VDDA'], "A.4 refusal table"


ISO_HEAD = '''from pcbc import *
VCC = Power("VCC"); GND = Ground("GND"); VS = Power("VS"); GS = Ground("GS")
Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2="LED_A")
Capacitor("C1", "1uF", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)
Resistor("R2", "1k", package="0603", mpn="X", lcsc="C1", p1=VS, p2="OUT")
Capacitor("C2", "1uF", package="0603", mpn="X", lcsc="C1", p1=VS, p2=GS)
Resistor("U7", "opto", package="0603", mpn="X", lcsc="C1", p1="LED_A", p2="OUT")
Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")
Region("primary", left=0, top=0, width=18, height=25)
Region("secondary", left=22, top=0, width=18, height=25)
NetReq("VCC", "GND", "VS", "GS", kind="power", volts=5, amps=1)
'''
ISO_TAIL = '''Place("R1", parent="primary", left=2, top=2); Place("C1", to="R1.1"); Place("R2", parent="secondary", left=2, top=2); Place("C2", to="R2.1")
for i, r in enumerate(["R1","R2","C1","C2","U7"]):
    SchPlace(r, left=20 + 30*i, top=20)
'''


def test_isolation_sides_come_from_place_lines_and_the_corridor_is_a_rule_area(tmp_path: Path):
    p = tmp_path / "iso.py"
    p.write_text(ISO_HEAD + 'Isolation("primary", "secondary", volts=250, across=("U7",))\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
    cs = compile_constraints(load_board(p))
    assert cs.refusals == ()
    spec = cs.isolation_specs[0]
    assert (spec.nets_a, spec.nets_b) == (("GND", "LED_A", "VCC"), ("GS", "OUT", "VS")), "a part in across= is on both sides; LED_A/OUT follow their one-sided pads"
    assert spec.clearance_mm.value == 1.5 and spec.creepage_mm.value == 2.5 and spec.gap_min_mm == 2.5, "C.8: 250 V -> max(1.25 B2, 1.5 F.2) = 1.5; F.5 IIIa 2.5"
    assert cs.rule_areas == (cs.rule_areas[0],) and cs.rule_areas[0].name == "ISO_primary_secondary"
    assert cs.rule_areas[0].box == (18.0, 0.0, 22.0, 25.0) and cs.rule_areas[0].layers == ("F&B.Cu",) and cs.rule_areas[0].disallow == ("track", "via", "zone"), "E.15: the strip between the Regions, full board across, all copper"
    _expect(cs.lines, [
        ("Isolation primary/secondary: clearance 1.5 mm (iec60664_f2 IEC 60664-1 F.1 OV II + F.2 PD2 250 V; over ipc2221_6_1 row 171-250 V B2 1.25)", "C.8 vector 24: iec_clearance_mm(250) = 1.5"),
        ("Isolation primary/secondary: creepage 2.5 mm (iec60664_f5 IEC 60664-1 F.5 row 250 V IIIa PD2)", "C.8 vector 23"),
        ("VCC: isolation side primary (Isolation primary/secondary line 12)", "D: isolation_side on every net whose parts are all on one side"),
        ("OUT: isolation side secondary (Isolation primary/secondary line 12)", "Place(to=) inherits the target's side"),
    ])
    assert cs.by_net("LED_A") is not None and cs.by_net("LED_A").isolation_side == "primary"
    job = compile_design(load_board(p))
    assert json.loads(json.dumps(job.to_dict()))["rule_areas"][0]["name"] == "ISO_primary_secondary"
    # slot=True: the creepage path runs through the slot; the gap keeps the clearance and 1.0 mm
    p.write_text(ISO_HEAD + 'Isolation("primary", "secondary", volts=250, across=("U7",), slot=True, reinforced=True)\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
    cs = compile_constraints(load_board(p))
    spec = cs.isolation_specs[0]
    assert (spec.clearance_mm.value, spec.creepage_mm.value, spec.gap_min_mm) == (3.0, 5.0, 3.0), "C.8: reinforced 250 V -> impulse x1.6 -> 3.0; creepage doubles to 5.0; with the slot gap >= max(3.0, 1.0, 5.0 - 2 x 1.6)"
    assert "Isolation primary/secondary: creepage 5 mm (iec60664_f5 IEC 60664-1 F.5 row 250 V IIIa PD2, reinforced); slot=True: the contour gap + 2 x board 1.6 mm satisfies it, gap >= 3 mm" in cs.lines


def test_isolation_refusals_name_the_part_and_the_short(tmp_path: Path):
    p = tmp_path / "iso_bad.py"
    p.write_text(ISO_HEAD + 'Isolation("primary", "secondary", volts=250)\nPlace("U7", at=(20, 10))\n' + ISO_TAIL)
    assert check_board(p) == [
        'Isolation primary/secondary line 12: U7 is placed on neither side; Place("U7", parent="primary", ...) or list it in across=',
    ]
    p.write_text(ISO_HEAD + 'Isolation("primary", "secondary", volts=250)\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
    assert check_board(p) == [
        "Isolation primary/secondary line 12: OUT has pads on both sides (U7.2 primary, R2.2 secondary) and no across= part carries it: that is a short across the barrier",
    ]
    overlapping = ISO_HEAD.replace('Region("secondary", left=22, top=0, width=18, height=25)', 'Region("secondary", left=10, top=0, width=18, height=25)')
    p.write_text(overlapping + 'Isolation("primary", "secondary", volts=250, across=("U7",))\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
    assert check_board(p) == ["Isolation primary/secondary: Regions overlap in both axes; no straight line separates them"]


# ---------------------------------------------------------------------------------------------
# The examples' and the DS2 Addon's lines
# ---------------------------------------------------------------------------------------------


def test_node_lines_are_pinned():
    cs = compile_constraints(load_board(EXAMPLES / "node" / "node.py"))
    assert cs.refusals == ()
    _expect(cs.lines, [
        ("USB_DP: pair with USB_DN, 0.2288 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 90 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.2332/0.15; target 90 +-15 %; NetReq line 143)", "H.2 / C.2: node's pair is 0.2288 / 0.15 on 7628 (JLC row 0.2332, -1.8 %)"),
        ("USB_DP: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005)", "D: hole_floor 0.25 - 0.075 + 0.005 = 0.18 on 4L"),
        ("USB_DP: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]", "B.2 4L via; D usb_hs budget 2"),
        ("USB_DP: skew 0.5 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5"),
        ("USB_DP: uncoupled 2 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5"),
        ("USB_DP: length none (give length_mm=)", "H.6"),
        ("VBUS: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.099 x plane 0.430 at 0.2104 mm In1.Cu 0.134)", "C.6: 1 A over In1.Cu 0.2104 -> 0.134 < 0.300 < the 0.4 floor (board mod 1.099 on the literal 1.5862 mm stack)"),
        ("VBUS: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)", "C.8: 5 V is row 0-15 B2 = 0.1, under the 0.20 preset"),
        ("VBUS: via 0.8/0.4 mm (preset power), 2 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]", "C.7: the Power class's 0.4 drill carries 0.871 A; 1 A wants 2 per change, report only"),
        ("LOAD: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.099 x plane 0.430 at 0.2104 mm In1.Cu 0.134)", "LOAD shares Power's numbers (identical), so one class"),
        ("T_DIV: width 0.2 mm (preset analog)", "D analog"),
        ("T_DIV: clearance 0.2 mm (preset analog)", "D analog"),
        ("T_DIV: no vias (preset analog)", "D analog: via_count (max 0)"),
        ("T_DIV: airwire 25 mm (preset analog)", "D analog: 25 (today)"),
        ("T_DIV: keep_clear_of none", "D notes: keep-away is explicit; node has no switch_node net, so no hint"),
        ("T_DIV: spacing 5W (preset analog)", "D: 5W"),
        ("classes: Default 0.16/0.18, USB 0.2288/0.18 pair 0.2288/0.15, Power 0.4/0.2 via 0.8/0.4, Analog 0.2/0.2 via 0.6/0.3", "the class summary line of the spec's node report"),
    ])
    assert cs.lines[-1].startswith("classes: ")


def test_c3_usb_keeps_its_two_layer_pair_and_its_report_stops_lying():
    cs = compile_constraints(load_board(EXAMPLES / "c3_usb" / "c3_usb.py"))
    usb = cs.by_net("USB_DP")
    assert usb is not None and usb.pair is not None and (usb.pair.width_mm.value, usb.pair.gap_mm.value, usb.pair.controlled) == (0.127, 0.127, False), "C.3: the pair-fit clamp writes (track_min, clearance_min), controlled False"
    assert usb.pair.z_computed.value == 140.05 and usb.clearance_mm.value == 0.155, "C.10 vector 10; D: 0.155 on 2L"
    assert 'USB_DP/USB_DN: 90 ohm needs 0.7746 mm members at gap 0.15 on jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; fine for USB full speed, use Board(stackup="jlcpcb_4l_1oz") for high speed' in cs.lines
    assert sum(1 for l in cs.lines if l.startswith("USB_DP/USB_DN:")) == 1, "the note is printed once, not per member"


def test_buck_lines_are_pinned():
    cs = compile_constraints(load_board(EXAMPLES / "buck" / "buck.py"))
    _expect(cs.lines, [
        ("VIN: width 0.781 mm (ipc2221_ext IPC-2221B eq. 6-2 external 2 A 10 C 1 oz; IPC-2221 floor holds over ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm 0.646)", "C.10 vector 18: 2 A over the 2L pour: 2221 0.781 > 2152 0.646 (H.1)"),
        ("VIN: via 0.8/0.4 mm (preset power), 3 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]", "C.7: ceil(2 / 0.871) = 3"),
        ("SW: airwire 6 mm (NetReq line 56; overrides preset switch_node 8)", "D switch_node: airwire 8, overridden on line 56"),
        ("SW: loop 20 mm2 (preset switch_node; hot loop, pcbc default)", "D switch_node: 20.0 hot loop (buck measures 3.3)"),
        ('FB: keep_clear_of none; NetReq("FB", kind="analog", keep_clear_of="SW") holds 3 mm', "D notes: buck's FB sits 1.84 mm from the boot cap; the AI states the distance, the tool holds it"),
    ])
    vin = cs.by_net("VIN")
    assert vin is not None and vin.current is not None and (vin.current.width_ipc2221.value, vin.current.width_ipc2152.value, vin.current.plane_h_mm) == (0.781, 0.646, 1.53)
    assert vin.width_mm.source.formula == "ipc2221_ext" and vin.via.per_change == 3


def test_ds2_addon_lines_are_pinned(tmp_path: Path):
    cs = compile_constraints(load_board(_ds2(tmp_path)))
    assert cs.refusals == ()
    _expect(cs.lines, [
        ("AIN0: airwire 30 mm (NetReq line 96; overrides preset analog 25)", "S2 acceptance: the DS2 line; Derived.line() joins the note with '; '"),
        ("AIN0: width 0.2 mm (preset analog)", "D analog"),
        ("AIN0: no vias (preset analog)", "D analog"),
        ("AIN0: keep_clear_of none", "D notes: no switch_node net on the DS2 Addon"),
        ("REFP_F: airwire 30 mm (NetReq line 96; overrides preset analog 25)", "every net of the NetReq shares the number"),
        ("VDDA: width 0.25 mm (pcbc_floor amps < 0.2; ipc2221_ext 0.1 A 10 C 1 oz 0.150; ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm B.Cu pour 0.003)", "D power: 0.25 floor under 0.2 A; C.6 below 0.274 A the 2152 fit extrapolates"),
        ("VDDA: via 0.8/0.4 mm (preset power), 1 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]", "C.7 at 0.1 A"),
        ("classes: Default 0.16/0.16, Power 0.25/0.2 via 0.8/0.4, Analog 0.2/0.2 via 0.6/0.3", "the DS2 classes, unchanged"),
    ])
    vdda = cs.by_net("VDDA")
    assert vdda is not None and vdda.current is not None and vdda.current.width_ipc2152.source.note == "below 0.274 A the fit extrapolates"
    assert vdda.airwire_max_mm is None and cs.by_net("AIN0").airwire_max_mm == 30
