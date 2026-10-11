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
from conftest import ds2_dir, write_split

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "compiled"
DS2 = ds2_dir()


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
EVERY_KIND = '''NetReq("VCC", kind="power", volts=5); NetReq("GND", kind="power")
NetReq("HV", kind="power", volts=250)
NetReq("FB", kind="analog", keep_clear_of="SW")
NetReq("SW", kind="switch_node")
NetReq("CLK", kind="clock")
Pair("D_P", "D_N", kind="usb_hs")
Bus("SCK", "MOSI", "MISO", kind="spi", clock="SCK")
NetReq("SDA", "SCL", kind="i2c", pf_max=400)
NetReq("SENSE_P", "SENSE_N", kind="analog")
NetReq("AIN0", kind="analog", keep_clear_of="SW", keep_clear_mm=3)
NetReq("SIG", kind="digital", volts=48)
Path("VCC", "C1.1", "R1.1", amps=1)
'''
TWO = 'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")\n'
FOUR = 'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu")])\n'


def _board(tmp_path: Path, name: str, board_line: str, reqs: str, head: str = HEAD, tail: str = TAIL) -> Path:
    p = tmp_path / f"{name}.py"
    write_split(p, head + board_line + reqs + tail)
    return p


def _expect(lines: tuple[str, ...], wanted: list[tuple[str, str]]) -> None:
    for line, why in wanted:
        assert line in lines, f"{why}\nmissing: {line}\nnear: " + "\n".join(x for x in lines if x.split(':')[0] == line.split(':')[0])


# ---------------------------------------------------------------------------------------------
# The five boards: today's numbers, byte for byte (node's USB pair excepted)
# ---------------------------------------------------------------------------------------------


def test_the_five_boards_keep_todays_classes_and_nets(tmp_path: Path):
    """Snapshot fixtures were recorded from CompiledJob.to_dict() before S1; the projection of the
    ConstraintSet reproduces every recorded key. New keys (lane_clearance_mm) are allowed. The old
    router's `krt` block and `net_order` left the job with the router (the fixtures were rewritten
    without the two keys, nothing else)."""
    for name, path in _boards(tmp_path).items():
        if name == "buck":
            # docs/net-path-physics.md PR 10: buck migrated by design (§5). docs/physics-subjects.md PR 3: no net
            # carries a current, so every power net of buck shares one Power class at the class floor; its
            # currents ride on Path lines 62, 64 and 69. The snapshot is of the shipped NetReq; the classes are
            # pinned here instead.
            cs = compile_design(load_board(path)).constraints
            assert [(c.name, c.track_width_mm) for c in cs.classes] == [("Default", 0.16), ("Power", 0.16), ("SwitchNode", 0.16), ("Analog", 0.16)], cs.classes  # PR 5: no net kind sets a width
            continue
        was = json.loads((FIXTURES / f"{name}.json").read_text())
        # docs/physics-subjects.md PR 3, the deliberate differences: a net carries no current, so the recorded
        # `amps` is gone from every net and a Power class is the class floor (its width was the net current's);
        # node's LOAD (§8) is kind generic in class Default, its 1 A on Path line 150.
        floor = 0.16
        # PR 4 (E3): a Ground() net takes no volts=, so a power NetReq that named GND with its rails is now one
        # NetReq per rail and one for the grounds; each recorded entry is split the same way.
        split = {
            ("VCC", "GND"): [["VCC"], ["GND"]],
            ("VBUS", "3V3", "GND"): [["VBUS"], ["3V3"], ["GND"]],
            ("3V3", "GND", "VDDA", "VSS"): [["3V3", "VDDA"], ["GND", "VSS"]],
        }
        nets = []
        for n in was["nets"]:
            for pats in split.get(tuple(n["patterns"]), [n["patterns"]]):
                nets.append({**n, "patterns": pats})
        was["nets"] = nets
        for n in was["nets"]:
            n.pop("amps", None)
            if name == "node" and n["patterns"] == ["LOAD"]:
                n.update(kind="generic", class_name="Default", layers=["F.Cu", "B.Cu"])
        # PR 5 (§4.1): no net kind sets a width, a via or a layer: every net may use every layer no plane takes
        # (F.Cu, B.Cu on all five boards) with vias, and every class but the pair's is the floor at the stackup via.
        via = {"jlcpcb_2l_1oz": (0.5, 0.3), "jlcpcb_4l_1oz": (0.35, 0.2)}[was["stackup"]]
        for n in was["nets"]:
            n.update(layers=["F.Cu", "B.Cu"], vias=True)
        for c in was["classes"]:
            if c["name"] != "USB":
                c.update(track_width_mm=floor, via_diameter_mm=via[0], via_drill_mm=via[1])
            if c["name"] == "Power":
                c["track_width_mm"] = floor
                if name == "ds2_addon":
                    c["patterns"] = ["3V3", "VDDA", "GND", "VSS"]  # the order of the two NetReqs of PR 4
                if name == "node":
                    c["patterns"] = [x for x in c["patterns"] if x != "LOAD"]
            if c["name"] == "Default" and name == "node":
                c["patterns"] = ["LOAD"]
        now = json.loads(json.dumps(compile_design(load_board(path)).to_dict()))
        assert "krt" not in now and "net_order" not in now
        for key in ("keepouts", "places", "regions", "planes"):
            assert now[key] == was[key], (name, key)
        # docs/physics-subjects.md PR 7: `autoroute` was the old router's switch; nothing read the skip list.
        assert "skip_autoroute_patterns" not in now
        # The one deliberate difference in `nets`: a power net on a two-layer board used to be
        # told it runs on In1.Cu and In2.Cu (the preset's four-layer tuple, printed verbatim in
        # the report). The preset is now trimmed to the stackup's own layers; nothing reads the
        # field for an unconstrained net, so
        # only the report changes. Every other key is byte-identical.
        two_layer = now["layers"] == 2
        for a, b in zip(now["nets"], was["nets"], strict=True):
            if two_layer and a.get("layers") != b.get("layers"):
                assert b["layers"] == ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"] and a["layers"] == ["F.Cu", "B.Cu"], (name, a["patterns"])
                a = {**a, "layers": b["layers"]}
            if b.get("autoroute") is False:
                b = {**b, "autoroute": True}  # PR 7: no preset sets autoroute=False (the sensitive kinds route first by kind)
            assert a == b, (name, a.get("patterns"))
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
    assert power.lane_clearance_mm == 0.2 and power.patterns == ["VBUS", "3V3", "GND"], "node's power line"
    # docs/physics-subjects.md §8: LOAD is the low-side switch's drain, kind generic; its 1 A is Path line 114's.
    load = job.constraints.by_net("LOAD")
    assert load is not None and load.class_name == "Default" and load.line == 68 and load.req_index == 4


# ---------------------------------------------------------------------------------------------
# The language: exact signatures, lines, refusals
# ---------------------------------------------------------------------------------------------


def test_an_unknown_kwarg_is_refused_with_the_did_you_mean_line(tmp_path: Path):
    """A.4: no **kwargs (amp=2 used to be a silent 0.2 A net); a TypeError at load that check names."""
    p = _board(tmp_path, "typo", TWO, 'NetReq("VCC", kind="power", amp=2)\n')
    assert check_board(p) == ["NetReq(\"VCC\") line 15: unexpected keyword 'amp'; it takes kind=, volts=, max_mm=, keep_clear_of=, keep_clear_mm=, pf_max=, rise_ps=, class_name="]
    p = _board(tmp_path, "typo2", TWO, 'Pair("D_P", "D_N", skew=2)\n')
    assert check_board(p) == [
        "Pair(\"D_P\") line 15: unexpected keyword 'skew'; it takes p=, n=, kind=, z_diff_ohm=, match_mm=, uncoupled_mm=, gap_mm=, layers=, reference=, length_mm=, vias=, vias_max="
    ]
    with pytest.raises(TypeError):
        load_board(p)


def test_a_kind_foreign_kwarg_and_an_unknown_kind_are_refused_citing_the_line(tmp_path: Path):
    p = _board(tmp_path, "foreign", TWO, 'NetReq("VCC", kind="power", volts=5)\nNetReq("SW", kind="switch_node", pf_max=3)\nNetReq("SIG", kind="spy")\n')
    fails = check_board(p)
    # `rise_ps` is last because S8 appended it to `COMMON_KWARGS`: an edge rate belongs to any net that
    # has one, and the plane stitch folds the fastest over the whole board (`ConstraintSet.fastest_edge`).
    assert 'NetReq("SW") line 16: kind="switch_node" does not take pf_max=; it takes max_mm, class_name, keep_clear_of, keep_clear_mm, volts, rise_ps' in fails, "D: switch_node's own kwargs then A.4's common set"
    assert 'NetReq("SIG") line 17: unknown kind \'spy\'; known: analog, clock, generic, i2c, power, switch_node, and the aliases digital and default' in fails
    assert len(fails) == 2, fails
    assert set(PRESETS) == {"generic", "power", "analog", "switch_node", "clock", "i2c"}, "PR 6: usb_hs is a Pair kind, spi a Bus kind; PR 7: sense and feedback are Path kinds"


def test_the_line_is_the_board_files_line_and_zero_from_python():
    from pcbc.language import NetReq, reset

    reset()
    assert NetReq("X", kind="power").line == 0
    design = load_board(EXAMPLES / "node" / "node.py")
    assert [r.line for r in design.netreqs] == [63, 64, 65, 67, 68], "PR 6: line 62 is a Pair"


def test_every_refusal_of_a4_is_pinned(tmp_path: Path):
    reqs = '''NetReq("VCC", kind="power", volts=5); NetReq("GND", kind="power")
NetReq("VCC", kind="analog")
Pair("D_P", "D_N", kind="usb_hs")
Pair("D_P", "D_N")
Pair("D_P", "D_N")
Pair("SENSE_P", "SIG")
NetReq("SCK", "MOSI", "MISO", kind="spi")
Bus("AIN0", "MISO", match_mm=1.0, clock="CLK")
Path("VCC", "C1.2", "R1.1")
Path("VCC", "C9.1", "R1.1")
Guard("NOPE")
Isolation("primary", "secondary", volts=250)
'''
    p = _board(tmp_path, "refuse", TWO, reqs)
    assert check_board(p) == [
        'VCC: NetReq line 15 and NetReq line 16 both name it, and a net is named once (every net of one NetReq shares every number)',
        # PR 6: the usb_hs pair is a Pair line, so no second Pair overrides it; each later one names the net again
        'D_P: Pair line 17 and Pair line 18 both name it, and a net is named once (one of them, not both)',
        'D_P: Pair line 17 and Pair line 19 both name it, and a net is named once (one of them, not both)',
        # E6 (PR 6): spi is a Bus kind
        'NetReq("SCK", "MOSI", "MISO") line 21: kind="spi" is a Bus kind: it times two or more paths together; a NetReq takes kind generic, power, switch_node, analog, clock or i2c',
        "Pair line 20: KiCad pairs names ending P/N, _P/_N or +/-, and SENSE_P/SIG are not such a pair",
        'Bus line 22: clock "CLK" is not one of AIN0, MISO',
        'Path("VCC") line 23: C1.2 is on GND, not VCC; C1\'s VCC pad is C1.1',
        'Path("VCC") line 24: no part C9',
        'Guard("NOPE") line 25: no net "NOPE"',
        'refuse.py:26: Isolation("primary", "secondary") names Region "primary", and layout.core.py declares no Region "primary"',
        'refuse.py:26: Isolation("primary", "secondary") names Region "secondary", and layout.core.py declares no Region "secondary"',
    ]


def test_a_controlled_pair_on_four_layers_wants_its_plane_declared(tmp_path: Path):
    """F.6 at compile: node with planes=[] is refused before any geometry."""
    src = (EXAMPLES / "node" / "node.py").read_text().replace('planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")]', "planes=[]")
    write_split(tmp_path / "node.py", src)
    shutil.copytree(EXAMPLES / "node" / "components", tmp_path / "components")
    assert compile_constraints(load_board(tmp_path / "node.py")).refusals == (
        'USB_DP: 90 ohm on F.Cu (Pair line 62) has no reference plane: In1.Cu, the layer next to F.Cu, is not in Board(planes=)',
        # S6: the second statement on the board that needs a plane. A `Thermal()` array's whole job
        # is to reach one, and with `planes=[]` node's `U1.49` barrels would land in bare laminate —
        # the blinky trap one stackup up. Both name the fact (no plane in Board(planes=)), and since
        # docs/direction.md §7b neither proposes the edit.
        'Thermal("U1.49") line 76: GND has no plane or pour to reach, so an array under U1.49 would be 0.35 W of vias '
        'into bare laminate (Board(planes=) pours no GND plane)',
    )
    assert check_board(EXAMPLES / "node" / "node.py") == []


# ---------------------------------------------------------------------------------------------
# Every kind of section D on 2L and 4L, lines pinned as exact strings
# ---------------------------------------------------------------------------------------------


def test_every_kind_compiles_on_two_layers_and_its_lines_are_pinned(tmp_path: Path):
    cs = compile_constraints(load_board(_board(tmp_path, "two", TWO, EVERY_KIND)))
    assert cs.refusals == ()
    _expect(cs.lines, [
        # power (docs/physics-subjects.md PR 3): the net carries no current, so its own copper is the class floor;
        # the current is the Path's, sized by C.6 with the KD 10 floor (1 A over the 2L pour: under the 0.4 floor)
        ("VCC: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 3: a net's width is the class floor"),
        ("VCC: path C1.1 -> R1.1 (Path line 26): width 0.4 mm on F.Cu (pcbc_floor amps >= 0.2; plane 0.593 at 1.53 mm B.Cu pour), 0.4 mm on B.Cu (pcbc_floor amps >= 0.2; no plane); 2 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.500 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)", "D power: pcbc_floor 0.4 at amps >= 0.2 on the Path; C.7: 0.4 mm drill at 18 um plating carries 0.871 A at 10 C; ceil(1/0.871) = 2"),
        ("VCC: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)", "D power: clearance max(0.20, B2 row); IPC-2221B 6-1 0-15 V B2 = 0.1"),
        ("VCC: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)", "PR 5: a net kind sets no via; the Path's group is on its line at KD 13's 0.8/0.4"),
        ("VCC: loop 6 mm2 (preset power; decap loop, pcbc default)", "D power: loop_mm2 6.0 decap loop, pcbc default"),
        ("VCC: layers F.Cu, B.Cu (preset power)", "D: the preset's four-layer tuple trimmed to the stackup (a 2-layer board has no In1.Cu)"),
        ("VCC: spacing 3W (preset power)", "D: 3W"),
        # power at 250 V: the row wins the class clearance and creepage is written at >= 60 V
        ("HV: clearance 1.25 mm (ipc2221_6_1 IPC-2221B 6-1 row 171-250 V B2; over preset power 0.2)", "C.8 Table 6-1 171-250 V B2 = 1.25"),
        ("HV: creepage 2.5 mm (iec60664_f5 IEC 60664-1 F.5 row 250 V IIIa PD2)", "C.8 F.5 250 V IIIa PD2 = 2.5 (SLUP419 Table 3)"),
        ("HV: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 3: no net current, the class floor"),
        # analog (PR 5, §3.5): no width, via or layer of its own; the span and the keep-away stay. feedback and sense
        # are Path kinds since PR 7 (their 0.2 mm, no vias, F.Cu defaults are the path kinds', KD 15), so FB is analog.
        ("FB: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 5: no net width"),
        ("FB: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)", "PR 5: vias allowed at the stackup via"),
        ("FB: airwire 25 mm (preset analog)", "D analog: airwire 25 (the node's span)"),
        ("FB: keep_clear_of SW 3 mm (preset analog; 3 mm when keep_clear_of is given)", "D analog: keep_clear_mm 3.0 when keep_clear_of given"),
        ("FB: spacing 5W (preset analog)", "D: 5W"),
        ("AIN0: airwire 25 mm (preset analog)", "D analog: airwire 25 (today)"),
        ("AIN0: keep_clear_of SW 3 mm (NetReq line 24)", "D analog: keep_clear_mm written on the line"),
        ("AIN0: layers F.Cu, B.Cu (preset analog)", "PR 5: every layer no plane takes"),
        ('SENSE_P: keep_clear_of none; this board has a switch node (SW)', "D notes: keep-away is explicit; the board has a switch_node net"),
        # switch_node: no width of its own (PR 5: the 0.3 mm folds into KD 10), airwire 8, hot loop overridden on the line
        ("SW: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 5: the class floor"),
        ("SW: airwire 8 mm (preset switch_node)", "D switch_node: airwire 8"),
        ("SW: loop 20 mm2 (preset switch_node; hot loop, pcbc default)", "D switch_node: loop_mm2 20.0 hot loop, pcbc default; a loop's own budget is a Loop's (PR 7, E7)"),
        # clock: no width (0.15 had no source) and no via budget of its own (vias_max is a Path's, PR 5)
        ("CLK: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 5: the class floor"),
        ("CLK: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)", "PR 5: the stackup via, no budget"),
        ("CLK: spacing 5W (preset clock)", "D: 5W"),
        # usb_hs on 2L: the pair-fit clamp (C.3), stated
        ("D_P: pair with D_N, 0.127 mm wide, gap 0.127 mm on F.Cu over the B.Cu pour: 140.05 ohm (hj_coupled_microstrip x 1 jlcpcb_2l_1oz; formula only; target 90 +-15 %; uncontrolled; preset usb_hs)", "C.10 vector 10: zdiff(0.127, 0.127, 1.53, 0.035, 4.6) = 140.05; bias 1.0 (uncalibrated)"),
        ('D_P/D_N: 90 ohm needs 0.7746 mm members at gap 0.15 on jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; within the USB full speed tolerance, outside the high speed one', "C.3 pair-fit clamp note: solved member 0.7746 > PAIR_FIT_MM 0.25 (vector 10)"),
        ("D_P: clearance 0.155 mm (class_floor 0.16 over hole_clearance 0.25 - ring 0.1 + 0.005 = 0.155)", "D usb_hs: min(0.16, w) then the floor: 0.155 on 2L"),
        ("D_P: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]", "D usb_hs: stackup via, budget 2"),
        ("D_P: skew 0.5 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5: skew 0.5 (TI usb_layout_basics, the tight number)"),
        ("D_P: uncoupled 2 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5: uncoupled 2.0"),
        ("D_P: length none (no length_mm= on Pair line 20 or on a Path of D_P)", "H.6: no preset USB length rule; a length is a Path's or the Pair's (PR 6)"),
        # spi: bus matched to the clock, length written on the line
        ("MISO: bus SCK, MOSI, MISO matched to SCK within 2.5 mm (preset spi, Bus line 21) [soft: warning in R1]", "D spi: match_mm 2.5 (about 15 ps on FR-4, pcbc default)"),
        ("MISO: clearance 0.16 mm (class_floor 0.16 over hole_clearance 0.25 - ring 0.1 + 0.005 = 0.155)", "physics-subjects §4.1 (PR 6): spi is a Bus kind; its 0.20 clearance (no SPI-specific source) disappears"),
        ("MISO: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "D: floor max(0.16, track_min)"),
        # i2c: length from capacitance at the class width on the outer layer
        ("SDA: length 9412.47 mm (i2c_capacitance 400 pF (NetReq line 22) - 10 pF x 2 pins on the busiest line at 0.0404 pF/mm (0.16 mm F.Cu))", "C.9: 0.16 mm on 2L is 0.0391 pF/mm (vector 25); (400 - 30) / 0.0391"),
        # generic with volts=48: B2 row 0.6 over the floor, no creepage under 60 V
        ("SIG: clearance 0.6 mm (ipc2221_6_1 IPC-2221B 6-1 row 31-50 V B2; over class_floor 0.16)", "C.8 Table 6-1 31-50 V B2 = 0.6 (vector 22)"),
        ("SIG: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "D generic: floor"),
        # z_se_ohm left the net (PR 5): it is a Path's (`test_paths_compile.py::test_copper_fields_are_a_paths_and_size_its_links`)
        # classes: the 250 V power line cannot share Power (its clearance differs), so it is Power_2 and the report says so
        ("class Power_2 (Power is NetReq line 15 at 0.16 mm)", "A.3: a later NetReq with different numbers gets {base}_{n}; today it silently overwrote"),
        ("class Default_2 (Default is the class floor at 0.16/0.16 mm)", "A.3: SIG's 0.6 clearance cannot share Default"),
        ("classes: Default 0.16/0.16, Power 0.16/0.2, Power_2 0.16/1.25, Analog 0.16/0.2, SwitchNode 0.16/0.2, Clock 0.16/0.2, USB 0.127/0.155 pair 0.127/0.127, I2C 0.16/0.2, Default_2 0.16/0.6", "the class summary: no net kind sets a width or a via (PR 5); spi is a Bus kind (PR 6), sense and feedback Path kinds (PR 7)"),
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
        ("VCC: width 0.16 mm (class_floor max(0.16, track_min 0.0889))", "PR 3: the net's own copper is the class floor"),
        ("VCC: path C1.1 -> R1.1 (Path line 26): width 0.4 mm on F.Cu (pcbc_floor amps >= 0.2; plane 0.430 at 0.2104 mm In1.Cu), 1.799 mm on In2.Cu (ipc2221_int 1 A 10 C 0.4343 oz In2.Cu 0.0152 mm jlcpcb_4l_1oz; plane 0.536 at 1.065 mm In1.Cu), 0.4 mm on B.Cu (pcbc_floor amps >= 0.2; no plane); 2 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.500 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)", "C.6 node case on the Path: 1 A over In1.Cu at 0.2104 under the 0.4 floor; C.7 on KD 13's 0.4 drill"),
        ("D_P: pair with D_N, 0.2288 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 90 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.2332/0.15; target 90 +-15 %; preset usb_hs)", "C.10 vector 9: (0.2288, 0.15) on 7628 with bias 0.8532; JLC row 0.2332/0.15; diff_pair_z rounds to 90.0"),
        ("D_P: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005)", "D usb_hs: the floor is 0.18 on 4L"),
        ("D_P: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]", "B.2 4L via 0.35/0.2"),
        ("MISO: width 0.16 mm (class_floor max(0.16, track_min 0.0889))", "D: floor max(0.16, 4L track_min 0.0889)"),
        ("SDA: length 4177.07 mm (i2c_capacitance 400 pF (NetReq line 22) - 10 pF x 2 pins on the busiest line at 0.091 pF/mm (0.16 mm F.Cu))", "C.9 on 7628: 0.16 mm at 0.2104 mm Dk 4.4 with bias 0.9064"),
        ("SIG: clearance 0.6 mm (ipc2221_6_1 IPC-2221B 6-1 row 31-50 V B2; over class_floor 0.18)", "C.8 31-50 V B2 = 0.6 over the 4L floor 0.18"),
        ("class Default_2 (Default is the class floor at 0.16/0.18 mm)", "A.3 collision on the 4L floor"),
    ])
    d_p = cs.by_net("D_P")
    assert d_p is not None and d_p.pair is not None and d_p.pair.controlled and d_p.reference == "In1.Cu"
    assert d_p.pair.z_computed.value == 90.0 and d_p.pair.tolerance_pct == 15.0


def test_a_netreq_at_48_v_gets_0_6_and_no_creepage_and_250_v_gets_1_25_and_2_5(tmp_path: Path):
    reqs = 'NetReq("VCC", kind="power", volts=48)\nNetReq("HV", kind="power", volts=250)\nNetReq("SIG", volts=12)\nNetReq("GND", kind="power")\n'
    cs = compile_constraints(load_board(_board(tmp_path, "volts", TWO, reqs)))
    vcc, hv = cs.by_net("VCC"), cs.by_net("HV")
    assert vcc is not None and vcc.clearance_mm.value == 0.6 and vcc.voltage is not None and vcc.voltage.creepage_mm is None, "C.8: 48 V B2 = 0.6 (vector 22); under 60 V no creepage"
    assert hv is not None and hv.clearance_mm.value == 1.25 and hv.voltage is not None and hv.voltage.creepage_mm is not None and hv.voltage.creepage_mm.value == 2.5, "C.8: 250 V B2 = 1.25, F.5 IIIa = 2.5"
    sig = cs.by_net("SIG")
    assert sig is not None and sig.voltage is not None and sig.voltage.creepage_mm is None, "a 12 V net gets no creepage rule"
    gnd = cs.by_net("GND")
    assert gnd is not None and gnd.voltage is None, "PR 4 (E3): a Ground() net takes no volts=, so it has no voltage of its own"
    assert [l for l in cs.lines if "creepage" in l] == ["HV: creepage 2.5 mm (iec60664_f5 IEC 60664-1 F.5 row 250 V IIIa PD2)"]


# ---------------------------------------------------------------------------------------------
# Pair, Bus, Chain, Isolation, Guard
# ---------------------------------------------------------------------------------------------


def test_pair_bus_chain_and_guard_load_validate_and_override_never_silently(tmp_path: Path):
    reqs = '''NetReq("VCC", kind="power", volts=5); NetReq("GND", kind="power")
Pair("D_P", "D_N", kind="usb_hs", z_diff_ohm=100, match_mm=0.3, gap_mm=0.2)
Pair("SENSE_P", "SENSE_N", z_diff_ohm=100)
Bus("SCK", "MOSI", "MISO", match_mm=1.0, clock="SCK", length_mm=60)
Path("VCC", "C1.1", "R1.1")
Guard("AIN0", stitch_mm=2.0)
'''
    p = _board(tmp_path, "groups", FOUR, reqs)
    design = load_board(p)
    assert (len(design.pairs), len(design.buses), len(design.paths), len(design.guards)) == (2, 1, 1, 1)
    assert design.pairs[0].line == 16 and design.buses[0].line == 18 and design.paths[0].line == 19 and design.guards[0].line == 20
    cs = compile_constraints(design)
    assert cs.refusals == ()
    _expect(cs.lines, [
        ("D_P: pair with D_N, 0.2004 mm wide, gap 0.2 mm on F.Cu over In1.Cu (GND): 100 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.1722/0.15; target 100 +-15 %; Pair line 16)", "PR 6: the usb_hs Pair line overrides z_diff_ohm and gap_mm on its own preset; C.3 solved at gap 0.2"),
        ("D_P: skew 0.3 mm (Pair line 16; overrides preset usb_hs 0.5) [soft: warning in R1]", "A.4: never silent"),
        ("D_P: uncoupled 2 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "PR 6: a field the Pair line leaves out is the preset's, and says so"),
        ("SENSE_P: pair with SENSE_N, 0.1759 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 100.01 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.1722/0.15; target 100 +-10 %; Pair line 17)", "C.10 vector 9: 100 ohm on 7628 = 0.1762/0.15 (JLC row 0.1722); a bare Pair is +-10 %"),
        ("SENSE_P: skew 0.5 mm (Pair line 17) [soft: warning in R1]", "D: a bare Pair carries its own numbers"),
        ("MISO: bus SCK, MOSI, MISO matched to SCK within 1 mm (Bus line 18) [soft: warning in R1]", "D: Bus() sets the group on generic constraints"),
        ("MISO: length 60 mm (Bus line 18)", "A.2 BusReq.length_mm"),
        ("VCC: path C1.1 -> R1.1 (Path line 19)", "A.2 ChainReq, pads as Place(to=) takes them"),
        ("AIN0: guard stitch 2 mm in GND (Guard line 20; R2 pattern)", "D: Guard records BOTH halves of the statement — the pitch and the net the shield is made of (S7; section 2(r))"),
    ])
    job = compile_design(design)
    assert [c.name for c in job.classes] == ["Default", "Power", "USB", "Pair_SENSE_P"], "D: a bare Pair gets class Pair_<p>"
    from pcbc.route_native import pair_nets

    assert pair_nets(job) == [("D_N", "D_P"), ("SENSE_N", "SENSE_P")], "the two pairs the router routes as one object each"
    assert [g.kind for g in cs.groups] == ["bus", "chain"]
    ain0 = cs.by_net("AIN0")
    assert ain0 is not None and ain0.guard_stitch_mm == 2.0 and ain0.line == 0 and ain0.kind == "generic", "A.1: line 0 when synthesised from Pair/Bus alone"
    assert ain0.guard_ground == "GND", (
        "S7 / section 2(r): `Guard(ground=)` reaches the router. Measured 2026-09-21 before the fix, `_compile` stored "
        "`stitch_mm` alone and this attribute did not exist, so the ground half of every Guard() on every board was dropped "
        "between `language.Guard` and the pattern that would have to write the copper"
    )


def test_chain_on_the_ds2_addon_validates_its_pads_against_the_net(tmp_path: Path):
    """`U1.12` is AVDD (pad 12): pin names or pad numbers, as Place(to=) takes them."""
    board = _ds2(tmp_path)
    lines = board.read_text().split("\n")
    # The DS2 board carries the line S4's chain check asked for (R1: an intent line, never a coordinate).
    assert lines[96].startswith('Path("VDDA", "J2.1", "C4.1", "U1.12")'), "line 97 of ds2_addon.py is the VDDA chain"
    cs = compile_constraints(load_board(board))
    assert cs.refusals == () and cs.groups[0].members == ("J2.1", "C4.1", "U1.12")
    assert "VDDA: path J2.1 -> C4.1 -> U1.12 (Path line 97)" in cs.lines
    lines[96] = 'Path("VDDA", "J2.1", "C4.2", "U1.12")'
    write_split(board, "\n".join(lines))
    assert check_board(board) == ['Path("VDDA") line 97: C4.2 is on VSS, not VDDA; C4\'s VDDA pad is C4.1'], "A.4 refusal table"


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
NetReq("VCC", "VS", kind="power", volts=5); NetReq("GND", "GS", kind="power")
'''
ISO_TAIL = '''Place("R1", parent="primary", left=2, top=2); Place("C1", to="R1.1"); Place("R2", parent="secondary", left=2, top=2); Place("C2", to="R2.1")
for i, r in enumerate(["R1","R2","C1","C2","U7"]):
    SchPlace(r, left=20 + 30*i, top=20)
'''


def test_isolation_sides_come_from_place_lines_and_the_corridor_is_a_rule_area(tmp_path: Path):
    p = tmp_path / "iso.py"
    write_split(p, ISO_HEAD + 'Isolation("primary", "secondary", volts=250, across=("U7",))\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
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
    write_split(p, ISO_HEAD + 'Isolation("primary", "secondary", volts=250, across=("U7",), slot=True, reinforced=True)\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
    cs = compile_constraints(load_board(p))
    spec = cs.isolation_specs[0]
    assert (spec.clearance_mm.value, spec.creepage_mm.value, spec.gap_min_mm) == (3.0, 5.0, 3.0), "C.8: reinforced 250 V -> impulse x1.6 -> 3.0; creepage doubles to 5.0; with the slot gap >= max(3.0, 1.0, 5.0 - 2 x 1.6)"
    assert "Isolation primary/secondary: creepage 5 mm (iec60664_f5 IEC 60664-1 F.5 row 250 V IIIa PD2, reinforced); slot=True: the contour gap + 2 x board 1.6 mm satisfies it, gap >= 3 mm" in cs.lines


def test_isolation_refusals_name_the_part_and_the_short(tmp_path: Path):
    p = tmp_path / "iso_bad.py"
    write_split(p, ISO_HEAD + 'Isolation("primary", "secondary", volts=250)\nPlace("U7", at=(20, 10))\n' + ISO_TAIL)
    assert check_board(p) == [
        'Isolation primary/secondary (iso_bad.py:12): U7 is on neither side: no Place(parent=) in layout.core.py puts it in primary or secondary, and it is not in across=',
    ]
    write_split(p, ISO_HEAD + 'Isolation("primary", "secondary", volts=250)\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
    assert check_board(p) == [
        "Isolation primary/secondary line 12: OUT has pads on both sides (U7.2 primary, R2.2 secondary) and no across= part carries it: that is a short across the barrier",
    ]
    overlapping = ISO_HEAD.replace('Region("secondary", left=22, top=0, width=18, height=25)', 'Region("secondary", left=10, top=0, width=18, height=25)')
    write_split(p, overlapping + 'Isolation("primary", "secondary", volts=250, across=("U7",))\nPlace("U7", parent="primary", left=14, top=10)\n' + ISO_TAIL)
    assert check_board(p) == ["Isolation primary/secondary line 12: Regions overlap in both axes; no straight line separates them"]


# ---------------------------------------------------------------------------------------------
# The examples' and the DS2 Addon's lines
# ---------------------------------------------------------------------------------------------


def test_node_lines_are_pinned():
    cs = compile_constraints(load_board(EXAMPLES / "node" / "node.py"))
    assert cs.refusals == ()
    _expect(cs.lines, [
        ("USB_DP: pair with USB_DN, 0.2288 mm wide, gap 0.15 mm on F.Cu over In1.Cu (GND): 90 ohm (hj_coupled_microstrip x 0.8532 JLC04161H-7628; JLC row 0.2332/0.15; target 90 +-15 %; Pair line 62)", "H.2 / C.2: node's pair is 0.2288 / 0.15 on 7628 (JLC row 0.2332, -1.8 %)"),
        ("USB_DP: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005)", "D: hole_floor 0.25 - 0.075 + 0.005 = 0.18 on 4L"),
        ("USB_DP: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz), at most 2 (preset usb_hs) [soft: warning in R1]", "B.2 4L via; D usb_hs budget 2"),
        ("USB_DP: skew 0.5 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5"),
        ("USB_DP: uncoupled 2 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]", "R-Z5"),
        ("USB_DP: length none (no length_mm= on Pair line 62 or on a Path of USB_DP)", "H.6; a length is a Path's or the Pair's (PR 6)"),
        # docs/physics-subjects.md PR 3: VBUS's 1 A is Path line 111's; the net's own copper is the class floor.
        ("VBUS: width 0.16 mm (class_floor max(0.16, track_min 0.0889))", "PR 3: no net current"),
        ("VBUS: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)", "C.8: 5 V is row 0-15 B2 = 0.1, under the 0.20 preset"),
        ("VBUS: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz)", "PR 5: a net kind sets no via; the Path's 0.8/0.4 is KD 13's"),
        ("VBUS: path J1.VBUS -> U2.VIN (Path line 66): width 0.4 mm on F.Cu (pcbc_floor amps >= 0.2; plane 0.430 at 0.2104 mm In1.Cu), 0.4 mm on B.Cu (pcbc_floor amps >= 0.2; plane 0.430 at 0.2104 mm In2.Cu); 2 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.500 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model); J1.VBUS's pads joined at 1 A (join J1.A4B9~J1.B4A9, conservative: each pad carries part of the current)", "C.6/C.7 on the Path: 1 A under the 0.4 floor; 2 per change on KD 13's 0.4 drill; PR 5: F.Cu, B.Cu, the layers no plane takes"),
        # §8: LOAD is the switch's drain, kind generic: clearance the class floor 0.18 (over B2's 0.1 at 3.3 V), class Default.
        ("LOAD: width 0.16 mm (class_floor max(0.16, track_min 0.0889))", "PR 3: no net current"),
        ("LOAD: clearance 0.18 mm (class_floor hole_clearance 0.25 - ring 0.075 + 0.005; ipc2221_6_1 row 0-15 V B2 0.1)", "§8: 0.20 (preset power) -> 0.18 (class floor)"),
        ("LOAD: path J_LOAD.2 -> Q1.D (Path line 69): width 0.4 mm on F.Cu (pcbc_floor amps >= 0.2; plane 0.430 at 0.2104 mm In1.Cu), 0.4 mm on B.Cu (pcbc_floor amps >= 0.2; plane 0.430 at 0.2104 mm In2.Cu); 2 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.500 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)", "§8: the link stays 0.4 mm; KD 13 keeps its 0.8/0.4 via"),
        # PR 5 (§3.5): analog keeps its clearance, span, spacing and keep-aways; no width, via or layer of its own.
        ("T_DIV: width 0.16 mm (class_floor max(0.16, track_min 0.0889))", "PR 5"),
        ("T_DIV: clearance 0.2 mm (preset analog)", "D analog"),
        ("T_DIV: via 0.35/0.2 mm (stackup jlcpcb_4l_1oz)", "PR 5: vias allowed at the stackup via"),
        ("T_DIV: airwire 25 mm (preset analog)", "D analog: 25 (today)"),
        ("T_DIV: keep_clear_of none", "D notes: keep-away is explicit; node has no switch_node net, so no hint"),
        ("T_DIV: spacing 5W (preset analog)", "D: 5W"),
        ("classes: Default 0.16/0.18, USB 0.2288/0.18 pair 0.2288/0.15, Power 0.16/0.2, Analog 0.16/0.2", "the class summary line of the spec's node report"),
    ])
    assert cs.lines[-1].startswith("classes: ")


def test_c3_usb_keeps_its_two_layer_pair_and_its_report_stops_lying():
    cs = compile_constraints(load_board(EXAMPLES / "c3_usb" / "c3_usb.py"))
    usb = cs.by_net("USB_DP")
    assert usb is not None and usb.pair is not None and (usb.pair.width_mm.value, usb.pair.gap_mm.value, usb.pair.controlled) == (0.127, 0.127, False), "C.3: the pair-fit clamp writes (track_min, clearance_min), controlled False"
    assert usb.pair.z_computed.value == 140.05 and usb.clearance_mm.value == 0.155, "C.10 vector 10; D: 0.155 on 2L"
    assert 'USB_DP/USB_DN: 90 ohm needs 0.7746 mm members at gap 0.15 on jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; within the USB full speed tolerance, outside the high speed one' in cs.lines
    assert sum(1 for l in cs.lines if l.startswith("USB_DP/USB_DN:")) == 1, "the note is printed once, not per member"


def test_buck_lines_are_pinned():
    """The migrated buck (docs/physics-subjects.md §8): VIN's 1.36 A on Path line 53 through both input caps (TI Table
    4-1 roles, rms at 12 V -> 5 V / 2 A; a width per layer and a 2-via group), GND's 1.53 A undeclared (a poured
    return path), 5V carries its 2 A on Path line 48 and a 0.2 mm feedback tap on Path line 50, SW its switch
    current on Path line 46 (decision 2: a 4-via group at a layer change), and the input hot loop is declared."""
    cs = compile_constraints(load_board(EXAMPLES / "buck" / "buck.py"))
    assert cs.refusals == ()
    _expect(cs.lines, [
        # docs/physics-subjects.md §8 (PR 3): VIN's 1.36 A is Path line 69's, through both input caps; GND's 1.53 A
        # cannot be declared (a poured return path); every net's own copper is the class floor.
        ("VIN: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 3: no net current"),
        ("VIN: path J_IN.1 -> C_IN2.1 -> C_IN1.1 -> U1.VIN (Path line 53): width 0.459 mm on F.Cu (ipc2221_ext 1.36 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), 0.541 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 2 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.680 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)", "§8: 0.459 mm on F.Cu, the width the net-wide 1.36 A gave"),
        ("GND: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "§8 line 41: GND declares no current"),
        ("5V: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 3: C_OUT2.1's branch is the class floor"),
        ("5V: path L1.2 -> C_OUT1.1 -> J_OUT.1 (Path line 48): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), 1.09 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 3 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.750 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)", "§5"),
        ("5V: path C_OUT1.1 -> R_FB_TOP.1 (Path line 50, kind feedback): width 0.2 mm (preset feedback), no vias, layers F.Cu, length 15 mm; meets 5V only inside C_OUT1.1, its first station", "§5"),
        ("SW: path U1.SW -> L1.1 (Path line 46): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), 1.09 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 4 vias per layer change, flow-through at 1.1 mm pitch, worst barrel 0.622 A of 0.707 (via_barrel 0.3/0.018 mm at 10 C; lumped DC model)", "§5: physics v1-R1; decision 2"),
        ("SW: airwire 6 mm (NetReq line 43; overrides preset switch_node 8)", "D switch_node: airwire 8, overridden on line 43"),
        ("SW: loop 20 mm2 (preset switch_node; hot loop, pcbc default)", "D switch_node: 20.0 hot loop"),
        ('FB: keep_clear_of none; this board has a switch node (SW)', "D notes: buck's FB sits 1.84 mm from the boot cap; the AI states the distance, the tool holds it"),
        ("loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52): 20 mm2 (pcbc default; U1 carries SW)", "§6"),
    ])
    lk = cs.link("VIN", "J_IN.1", "C_IN2.1")
    assert lk is not None and lk.current is not None and (lk.current.width_ipc2221.value, lk.current.width_ipc2152.value, lk.current.plane_h_mm) == (0.459, 0.321, 1.53)
    assert lk.via.per_change == 2 and lk.group.count == 2 and cs.by_net("VIN").width_mm.source.formula == "class_floor"


def test_ds2_addon_lines_are_pinned(tmp_path: Path):
    cs = compile_constraints(load_board(_ds2(tmp_path)))
    assert cs.refusals == ()
    _expect(cs.lines, [
        ("AIN0: airwire 30 mm (NetReq line 96; overrides preset analog 25)", "S2 acceptance: the DS2 line; Derived.line() joins the note with '; '"),
        ("AIN0: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 5: no net width"),
        ("AIN0: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)", "PR 5: vias allowed (§3.5's measured consequence)"),
        ("AIN0: keep_clear_of none", "D notes: no switch_node net on the DS2 Addon"),
        ("REFP_F: airwire 30 mm (NetReq line 96; overrides preset analog 25)", "every net of the NetReq shares the number"),
        # §8 line 95: 0.1 A is under the 0.633 A a 0.16 mm branch carries; no Path is needed.
        ("VDDA: width 0.16 mm (class_floor max(0.16, track_min 0.127))", "PR 3: 0.25 mm (floor under 0.2 A) -> the class floor"),
        ("VDDA: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)", "PR 5: no net via"),
        ("VDDA: path J2.1 -> C4.1 -> U1.12 (Path line 97)", "a field-less Path: an order, no current"),
        ("classes: Default 0.16/0.16, Power 0.16/0.2, Analog 0.16/0.2", "the DS2 classes"),
    ])
    vdda = cs.by_net("VDDA")
    assert vdda is not None and all(lk.current is None for lk in cs.links_of("VDDA")), "PR 3: the field-less VDDA Path carries no current (it carried the net's 0.1 A before)"
    assert vdda.airwire_max_mm is None and cs.by_net("AIN0").airwire_max_mm == 30


# ---------------------------------------------------------------------------------------------
# Boards a careless AI writes: every one of these was a traceback, a silent number or a wrong
# refusal in R1 as first merged (docs/r1-review.md, the robustness lens).
# ---------------------------------------------------------------------------------------------

ROBUST_HEAD = '''from pcbc import *
VCC = Power("VCC"); GND = Ground("GND"); DP = Net("USB_DP"); DN = Net("USB_DN")
Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=DP, p2=DN)
Capacitor("C1", "1uF", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)
Board(width=40, height=25, layers={layers}, stackup="{stackup}")
'''


def _robust(tmp_path: Path, name: str, line: str, *, layers: int = 2, stackup: str = "jlcpcb_2l_1oz") -> list[str]:
    board = tmp_path / f"{name}.py"
    write_split(board, ROBUST_HEAD.format(layers=layers, stackup=stackup) + line + "\n")
    from pcbc.language import check_board

    return check_board(board, pcb=False)


@pytest.mark.parametrize(
    ("name", "line", "expected"),
    [
        ("volts_str", 'NetReq("VCC", kind="power", volts="5V")',
         'line 6: NetReq("VCC"): volts=\'5V\' is not a number (volts= is a number in millimetres, volts, amps or ohms, with no unit)'),
        # A current is a Path's (docs/physics-subjects.md PR 3): its numbers are checked on the Path.
        ("amps_str", 'Path("VCC", "C1.1", "R1.1", amps="2A")',
         'line 6: Path("VCC"): amps=\'2A\' is not a number (amps= is a number in millimetres, volts, amps or ohms, with no unit)'),
        ("amps_zero", 'Path("VCC", "C1.1", "R1.1", amps=0)',
         'line 6: Path("VCC"): amps=0 is not physical; amps must be greater than 0'),
        ("amps_huge", 'Path("VCC", "C1.1", "R1.1", amps=50)',
         'line 6: Path("VCC"): amps=50 is past what pcbc models (30)'),
        ("volts_negative", 'NetReq("VCC", kind="power", volts=-12)',
         'line 6: NetReq("VCC"): volts=-12 is not physical; volts must be at least 0'),
        ("volts_past_table", 'NetReq("VCC", kind="power", volts=2000)',
         'line 6: NetReq("VCC"): volts=2000 is past what pcbc models (1000)'),
        ("temp_zero", 'Path("VCC", "C1.1", "R1.1", amps=1, temp_rise_c=0)',
         'line 6: Path("VCC"): temp_rise_c=0 is not physical; temp_rise_c must be greater than 0'),
        ("keep_negative", 'NetReq("VCC", kind="analog", keep_clear_of="GND", keep_clear_mm=-1)',
         'line 6: NetReq("VCC"): keep_clear_mm=-1 is not physical; keep_clear_mm must be greater than 0'),
        ("layers_empty", 'Path("VCC", "C1.1", "R1.1", layers=[])',
         'line 6: Path("VCC"): layers=[] names no layer'),
        ("bus_one_net", 'Bus("VCC", match_mm=1)', 'line 6: Bus("VCC") needs at least two nets'),
        ("chain_one_pad", 'Path("VCC", "R1.1")', 'line 6: Path("VCC"): a Path needs a net and two or more stations, "REF.PIN" in feed order'),
        ("chain_no_dot", 'Path("VCC", "R1", "C1")', 'line 6: Path("VCC"): \'R1\' is not REF.PIN (a part reference, a dot, a pin name or pad number)'),
        # PR 5 (E4): a NetReq writes no layers=; a Path's layer not on the stackup is pinned in test_paths_compile.
        ("layers_on_a_netreq", 'NetReq("VCC", layers=["In1.Cu"])',
         "NetReq(\"VCC\") line 6: layers= is about the copper between two pads (the layers that copper runs on), and VCC's pads are joined by copper the router lays link by link; layers= is a Path field"),
        ("i2c_budget_gone", 'NetReq("USB_DP", kind="i2c", pf_max=5)',
         'NetReq("USB_DP") line 6: pf_max=5 pF is under the 1 device pin on the bus (10 pF at 10 pF each, UM10204 7.1): no length is left for copper'),
        ("vias_max_negative", 'Path("VCC", "C1.1", "R1.1", vias_max=-1)',
         'line 6: Path("VCC"): vias_max=-1 must be a whole number of vias, 0 or more'),
    ],
)
def test_a_board_that_writes_nonsense_is_refused_with_its_line(tmp_path: Path, name: str, line: str, expected: str):
    """No traceback, no silent number: the board's own line and what to change. (A refusal the
    compiler raises lands after check_design's own lines; one the language raises is the only one.)"""
    fails = _robust(tmp_path, name, line)
    assert expected in fails, fails


def test_a_pair_written_negative_first_is_the_same_pair(tmp_path: Path):
    """`_kicad_pairs` was order-dependent, so `NetReq("USB_DN", "USB_DP")` (and any glob, which
    hands the nets over sorted) was refused with "rename USB_DN/USB_DP"."""
    from pcbc.constraints import _kicad_pairs

    assert _kicad_pairs("USB_DP", "USB_DN") and _kicad_pairs("USB_DN", "USB_DP")
    assert _kicad_pairs("D+", "D-") and _kicad_pairs("D-", "D+")
    assert not _kicad_pairs("D_A", "D_B") and not _kicad_pairs("SCK", "SDA")
    fails = _robust(tmp_path, "npair", 'NetReq("USB_DN", "USB_DP", kind="usb_hs")', layers=4, stackup="jlcpcb_4l_1oz")
    assert not any("rename" in f for f in fails), fails


def test_a_two_layer_board_is_not_told_its_power_runs_on_inner_layers(tmp_path: Path):
    """The power preset names four layers; a 2-layer board has two, and the report printed the
    preset's tuple verbatim. Nothing but the report reads the field for an unconstrained net."""
    board = tmp_path / "pl.py"
    write_split(board, ROBUST_HEAD.format(layers=2, stackup="jlcpcb_2l_1oz") + 'NetReq("VCC", "GND", kind="power")\n')
    cs = compile_constraints(load_board(board))
    assert cs.refusals == ()
    assert "VCC: layers F.Cu, B.Cu (preset power)" in cs.lines
    assert not any("In1.Cu" in line for line in cs.lines)


def test_a_net_that_does_not_exist_is_a_typo_not_a_net(tmp_path: Path):
    """R1 as merged accepted `NetReq("NOPE")` and printed six lines about a net with no pads, and
    a glob matching nothing became a class pattern with the literal `*` in it."""
    for line, expected in [
        ('NetReq("NOPE", kind="digital")', 'NetReq("NOPE") line 6: no net \'NOPE\''),
        ('NetReq("VCX", kind="power")', 'NetReq("VCX") line 6: no net \'VCX\'; the nearest net name on this board is \'VCC\''),
        ('NetReq("SPI_*")', "NetReq(\"SPI_*\") line 6: no net matches 'SPI_*'"),
        ('Pair("X_P", "X_N")', "Pair line 6: no net 'X_P'"),
        ('Bus("Y0", "Y1", match_mm=1)', "Bus line 6: no net 'Y0'"),
    ]:
        fails = _robust(tmp_path, f"nope{abs(hash(line))}", line)
        assert expected in fails, (line, fails)
    # A glob that does match is not a refusal.
    assert not [f for f in _robust(tmp_path, "glob_ok", 'NetReq("USB_*", kind="digital")') if "no net" in f]


def test_a_board_whose_layer_count_and_stackup_disagree_is_refused(tmp_path: Path):
    """The seed writes `layers` copper layers while every number comes from the stackup: a
    4-layer board on a 2-layer stackup was seeded with In1/In2 and 2-layer impedances."""
    board = tmp_path / "mismatch.py"
    write_split(board, ROBUST_HEAD.format(layers=4, stackup="jlcpcb_2l_1oz"))
    from pcbc.language import check_board

    assert check_board(board, pcb=False)[:1] == [
        "line 5: Board(layers=4, stackup='jlcpcb_2l_1oz'): jlcpcb_2l_1oz is a 2-layer stackup, not a 4-layer one"
    ]


def test_a_plane_the_router_will_never_pour_is_refused_at_the_board_line(tmp_path: Path):
    """A declaration nothing reads is a lie the board tells its author — and **the two-layer half of
    that refusal is gone, because S8 made the router read it.**

    The refusal was right for the router it was written against: `route.krt_plan` took `planes=` only
    above two layers and poured `GND` on `B.Cu` itself below that, so a two-layer `planes=` poured
    nothing, and until the `power_moves` review nothing noticed — `Board()` accepted it, the router
    ignored it, the ampacity measurement exempted the net **because of it**, and `FAB_NOTES.md` called
    a rail poured that had no pour.

    S8 (`docs/stitch-plan.md` section 8 item 1) changed the router instead of the author:
    `route_scene.plane_targets` reads a declared `planes=` on **every** stackup and the implicit back
    pour is what a board that declares nothing gets, and `krt_plan`'s `gnd_pour` step pours whatever
    that says. So the sentence stopped being true, and a refusal that has stopped being true is worse
    than no refusal — this one would now forbid the only way to write down two pours of one net facing
    each other across the core, which is the population R-E1 has never had. The 2-layer case is
    therefore asserted to **pass**, and the pour it produces is asserted, so "the router reads it" is
    checked rather than assumed.

    The layer check keeps its full force: a layer the stackup does not have is a lie no router change
    can make true.
    """
    from pcbc.compile import compile_design
    from pcbc.language import check_board
    from pcbc.route_scene import plane_targets

    two = tmp_path / "two.py"
    write_split(two, ROBUST_HEAD.format(layers=2, stackup="jlcpcb_2l_1oz").replace(
            'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")',
            'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz", planes=[("GND", "F.Cu"), ("GND", "B.Cu")])',
        ))
    assert not [f for f in check_board(two, pcb=False) if "planes" in f], check_board(two, pcb=False)
    assert plane_targets(compile_design(load_board(two))) == (("GND", "F.Cu"), ("GND", "B.Cu")), (
        "S8: a declared pour is poured on whatever stackup declared it"
    )
    # And the implicit pour is still exactly what a board that declares nothing gets, which is what
    # keeps all four two-layer examples byte-identical across the change.
    bare = tmp_path / "bare.py"
    write_split(bare, ROBUST_HEAD.format(layers=2, stackup="jlcpcb_2l_1oz") + 'NetReq("VCC", kind="power", volts=5); NetReq("GND", kind="power")\n')
    assert plane_targets(compile_design(load_board(bare))) == (("GND", "B.Cu"),)
    # And with no power GND there is no implicit pour either, which is the third arm and the one the
    # blinky trap of `docs/stitch-plan.md` section 7.1 is about.
    none = tmp_path / "none.py"
    write_split(none, ROBUST_HEAD.format(layers=2, stackup="jlcpcb_2l_1oz"))
    assert plane_targets(compile_design(load_board(none))) == ()
    ghost = tmp_path / "ghost.py"
    write_split(ghost, ROBUST_HEAD.format(layers=4, stackup="jlcpcb_4l_1oz").replace(
            'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz")',
            'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In5.Cu")])',
        ))
    assert check_board(ghost, pcb=False)[:1] == [
        "line 5: Board(planes=[('GND', 'In5.Cu')]): jlcpcb_4l_1oz has no copper layer In5.Cu; "
        "it has B.Cu, F.Cu, In1.Cu, In2.Cu"
    ], check_board(ghost, pcb=False)[:1]
    # node's own declaration is the control: four layers, both layers real, and no refusal.
    ok = tmp_path / "ok.py"
    write_split(ok, ROBUST_HEAD.format(layers=4, stackup="jlcpcb_4l_1oz").replace(
            'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz")',
            'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu")])',
        ))
    assert not [f for f in check_board(ok, pcb=False) if "planes" in f], check_board(ok, pcb=False)


def test_an_impedance_target_the_fab_cannot_reach_says_so(tmp_path: Path):
    """`z_se_ohm=5` solved to the 6 mm bisection ceiling and printed 'target 5' as if it were met. Since
    docs/physics-subjects.md PR 5 the impedance is a Path's (N4), and its link says the target is not reached."""
    board = tmp_path / "unreachable.py"
    write_split(board, ROBUST_HEAD.format(layers=4, stackup="jlcpcb_4l_1oz").replace(
            'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz")',
            'Resistor("R2", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
            'Board(width=40, height=25, layers=4, stackup="jlcpcb_4l_1oz", planes=[("GND", "In1.Cu")])',
        )
        + 'Path("VCC", "C1.1", "R2.1", z_se_ohm=5, layers=("F.Cu",))\n')
    cs = compile_constraints(load_board(board))
    assert cs.refusals == ()
    (lk,) = cs.links_of("VCC")
    (_lay, (target, z)), = lk.z_se
    assert target == 5 and "not reached: 6 mm of copper only reaches 5.28 ohm" in z.source.note, z
    assert any("not reached: 6 mm of copper only reaches" in line for line in cs.lines), cs.lines


def test_a_single_name_given_as_a_string_is_one_name_not_its_letters(tmp_path: Path):
    """`Keepout(no="copper")` was iterated into ('c','o','p','p','e','r') and the keepout then
    forbade nothing the checks look for; `Isolation(across="U7")` did the same with ('U','7')."""
    from pcbc.language import load_board

    board = tmp_path / "strings.py"
    write_split(board, ROBUST_HEAD.format(layers=2, stackup="jlcpcb_2l_1oz")
        + 'Keepout("K", position="absolute", left=1, top=1, width=2, height=2, no="copper")\n'
        + 'Region("p", left=0, top=0, width=10, height=25)\nRegion("s", left=20, top=0, width=10, height=25)\n'
        + 'Isolation("p", "s", volts=250, across="R1")\n')
    design = load_board(board)
    assert design.keepouts[0].no == ("copper",)
    assert design.isolations[0].across == ("R1",)
