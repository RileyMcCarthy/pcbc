"""R1 slice S3 (docs/r1-design.md section E): every rule KiCad can check is written from the
ConstraintSet, validated before the write, ordered so the last matching rule is the specific one,
and probed one kind at a time against KiCad itself with the canary firing every time; the project
classes have one writer; the gate counts the soft rules."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from pcbc import netcheck
from pcbc.apply import _apply_pro, _apply_rule_areas, _apply_slot, apply_job, write_dru
from pcbc.compile import compile_design
from pcbc.constraints import DruRule, RuleArea
from pcbc.copper import copper_by_net, power_ampacity_failures
from pcbc.dru import KNOWN_CONSTRAINTS, PROJECT_CLASS_ORDER, netclass_patterns, project_classes, render, rules, soft_kind, validate
from pcbc.fab import _IGNORE_DRC, _write_notes, copper_drc_errors
from pcbc.language import load_board
from pcbc.seed import emit_pcb, emit_pro, seed_job

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"

NOT_OWN = "!(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference)"

# The synthetic board of test_constraints.py: a net for every kind of section D (NetReq lines from 15).
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
ISO = 'Isolation("primary", "secondary", volts=250, across=("U7",))\nPlace("U7", parent="primary", left=14, top=10)\n'
ISO_SLOT = 'Isolation("primary", "secondary", volts=250, across=("U7",), slot=True)\nPlace("U7", parent="primary", left=14, top=10)\n'


def _board(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / f"{name}.py"
    p.write_text(text)
    return p


def _rules(tmp_path: Path, name: str, text: str) -> list[tuple[str, str, str, str]]:
    return [(r.name, r.constraint, r.condition, r.severity) for r in compile_design(load_board(_board(tmp_path, name, text))).dru]


# ---------------------------------------------------------------------------------------------
# E: every row pinned as rendered text
# ---------------------------------------------------------------------------------------------


def test_every_row_of_e_is_pinned_on_two_layers(tmp_path: Path):
    got = _rules(tmp_path, "two", HEAD + TWO + EVERY_KIND + TAIL)
    assert got == [
        # E.1, E.2 geometry (R-M2), warnings; no unit on the angle
        ("pcbc_geometry_segments", "(constraint track_segment_length (min 0.2mm))", "A.Type == 'Track'", "warning"),
        ("pcbc_geometry_angles", "(constraint track_angle (min 135))", "A.Type == 'Track'", "warning"),
        # E.3 class widths (R-I1), soft: power, z_se_ohm classes; the 2L USB pair sits at track_min and gets none
        ("width_power", "(constraint track_width (min 0.4mm))", "A.hasNetclass('Power')", "warning"),
        ("width_power_2", "(constraint track_width (min 0.4mm))", "A.hasNetclass('Power_2')", "warning"),
        ("width_z50", "(constraint track_width (min 1.5912mm))", "A.hasNetclass('Z50')", "warning"),
        # E.4 keep-aways (R-D3, R-A1), one per (class, other net), the footprint's own pads exempt
        ("feedback_away_from_sw", "(constraint clearance (min 3mm))", f"A.hasNetclass('Feedback') && B.NetName == 'SW' && {NOT_OWN}", "error"),
        ("analog_away_from_sw", "(constraint clearance (min 3mm))", f"A.hasNetclass('Analog') && B.NetName == 'SW' && {NOT_OWN}", "error"),
        # E.6 creepage by volts (R-V1): the 250 V class only (C.8: IEC 60664-1 F.5 row 250 V IIIa PD2 = 2.5); 48 V gets none
        ("creepage_power_2", "(constraint creepage (min 2.5mm))", "A.hasNetclass('Power_2') && !B.hasNetclass('Power_2') && B.NetName != ''", "error"),
        # E.7 routed length (R-L1): length_mm= on spi, C.9 on i2c ((400 - 20) / 0.0404 pF/mm), never from max_mm
        ("length_miso", "(constraint length (max 80mm))", "A.NetName == 'MISO'", "error"),
        ("length_mosi", "(constraint length (max 80mm))", "A.NetName == 'MOSI'", "error"),
        ("length_sck", "(constraint length (max 80mm))", "A.NetName == 'SCK'", "error"),
        ("length_scl", "(constraint length (max 9412.47mm))", "A.NetName == 'SCL'", "error"),
        ("length_sda", "(constraint length (max 9412.47mm))", "A.NetName == 'SDA'", "error"),
        # E.9 no vias (R-D3, R-A1), per net: analog, feedback, sense, switch_node
        ("novia_ain0", "(constraint via_count (max 0))", "A.NetName == 'AIN0'", "error"),
        ("novia_fb", "(constraint via_count (max 0))", "A.NetName == 'FB'", "error"),
        ("novia_sense_n", "(constraint via_count (max 0))", "A.NetName == 'SENSE_N'", "error"),
        ("novia_sense_p", "(constraint via_count (max 0))", "A.NetName == 'SENSE_P'", "error"),
        ("novia_sw", "(constraint via_count (max 0))", "A.NetName == 'SW'", "error"),
        # E.10 via budget (R-Z4), soft: clock and usb_hs at 2 (D)
        ("vias_clk", "(constraint via_count (max 2))", "A.NetName == 'CLK'", "warning"),
        ("vias_d_n", "(constraint via_count (max 2))", "A.NetName == 'D_N'", "warning"),
        ("vias_d_p", "(constraint via_count (max 2))", "A.NetName == 'D_P'", "warning"),
        # E.8 skew (R-L2), soft: the pair (0.5, TI usb_layout_basics), the spi bus (2.5), the sense pair (1.0, R-A2); members sorted
        ("skew_d_n_d_p", "(constraint skew (max 0.5mm))", "A.NetName == 'D_N' || A.NetName == 'D_P'", "warning"),
        ("skew_miso_mosi_sck", "(constraint skew (max 2.5mm))", "A.NetName == 'MISO' || A.NetName == 'MOSI' || A.NetName == 'SCK'", "warning"),
        ("skew_sense_n_sense_p", "(constraint skew (max 1mm))", "A.NetName == 'SENSE_N' || A.NetName == 'SENSE_P'", "warning"),
        # E.11 pair gap (R-Z5), error, the class's own 3-dp gap (the .2f form printed 0.13 where the class and the report say 0.127): max(0.1, 0.127 - 0.03) / 0.127; E.12 uncoupled 2 mm, soft
        ("usb_pair_gap", "(constraint diff_pair_gap (min 0.1mm) (opt 0.127mm))", "A.hasNetclass('USB')", "error"),
        ("uncoupled_usb", "(constraint diff_pair_uncoupled (max 2mm))", "A.hasNetclass('USB')", "warning"),
        # E.16 the footprint's own pads, after every clearance rule so it wins; E.17 the canary last
        ("pads_of_one_footprint", "(constraint clearance (min 0.1mm))", "A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference", "error"),
        ("pcbc_canary", "(constraint length (max 0.001mm))", "A.NetName == 'GND'", "warning"),
    ]


def test_every_row_of_e_is_pinned_on_four_layers(tmp_path: Path):
    got = _rules(tmp_path, "four", HEAD + FOUR + EVERY_KIND + TAIL)
    names = [g[0] for g in got]
    assert ("width_usb", "(constraint track_width (min 0.2288mm))", "A.hasNetclass('USB')", "warning") in got, "E.3: the 7628 pair (C.2 vector 9) is wider than track_min, so its class gets the soft width rule"
    assert ("width_z50", "(constraint track_width (min 0.3244mm))", "A.hasNetclass('Z50')", "warning") in got, "C.2 vector 5: 50 ohm on 7628 = 0.3244"
    assert ("usb_pair_gap", "(constraint diff_pair_gap (min 0.12mm) (opt 0.15mm))", "A.hasNetclass('USB')", "error") in got, "E.11: max(0.1, 0.15 - 0.03) / 0.15 at 2 dp"
    assert ("length_sda", "(constraint length (max 4177.07mm))", "A.NetName == 'SDA'", "error") in got, "C.9 on 7628 at the 0.16 mm floor"
    assert names[:2] == ["pcbc_geometry_segments", "pcbc_geometry_angles"] and names[-2:] == ["pads_of_one_footprint", "pcbc_canary"]
    assert names.index("width_usb") < names.index("analog_away_from_sw") < names.index("creepage_power_2") < names.index("length_miso") < names.index("novia_ain0") < names.index("vias_clk") < names.index("skew_d_n_d_p") < names.index("usb_pair_gap") < names.index("uncoupled_usb"), "E: general to specific, KiCad applies the last matching rule of a type"


def test_isolation_rows_e13_to_e15_are_pinned(tmp_path: Path):
    both = "(A.NetName == 'GND' || A.NetName == 'LED_A' || A.NetName == 'VCC') && (B.NetName == 'GS' || B.NetName == 'OUT' || B.NetName == 'VS')"
    # LED_A and OUT are U7's own two nets: it is the `across=` part, so they leave the creepage rule.
    sides_only = "(A.NetName == 'GND' || A.NetName == 'VCC') && (B.NetName == 'GS' || B.NetName == 'VS')"
    got = _rules(tmp_path, "iso", ISO_HEAD + ISO + ISO_TAIL)
    assert got[3:6] == [
        # C.8: 250 V -> max(IPC-2221B B2 1.25, IEC 60664-1 F.2 1.5) = 1.5; F.5 row 250 V IIIa PD2 = 2.5
        ("iso_primary_secondary_clearance", "(constraint clearance (min 1.5mm))", f"{both} && {NOT_OWN}", "error"),
        # KiCad resolves creepage per NET PAIR, so NOT_OWN cannot suppress the isolator's own two
        # pads (measured: 0.85 mm, the gate failed on U7 itself). The across= part's nets are left
        # out of the condition instead; the barrier inside that part is its own rating.
        ("iso_primary_secondary_creepage", "(constraint creepage (min 2.5mm))", sides_only, "error"),
        ("iso_primary_secondary_area", "(constraint disallow track via zone)", "A.intersectsArea('ISO_primary_secondary')", "error"),
    ], "E.13-E.15: sides sorted, the bridging part exempt (clearance by item, creepage by net), the corridor a rule area"
    slot = _rules(tmp_path, "iso_slot", ISO_HEAD + ISO_SLOT + ISO_TAIL)
    assert [g[0] for g in slot] == ["pcbc_geometry_segments", "pcbc_geometry_angles", "width_power", "iso_primary_secondary_clearance", "iso_primary_secondary_area", "pads_of_one_footprint", "pcbc_canary"], "E.14: with slot=True the slot satisfies the creepage and no creepage rule is written"


def test_todays_rules_are_still_written_unchanged_and_the_examples_lists_are_pinned():
    """The four rules R0 wrote keep their text (replaces S2's stub snapshot); the examples' rule
    lists are the E rows their NetReqs ask for."""
    lists = {}
    for name in ("blinky", "buck", "c3_usb", "node"):
        job = compile_design(load_board(EXAMPLES / name / f"{name}.py"))
        by = {r.name: r for r in job.dru}
        assert (by["pads_of_one_footprint"].constraint, by["pads_of_one_footprint"].condition, by["pads_of_one_footprint"].severity) == (
            "(constraint clearance (min 0.1mm))", "A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference", "error")
        assert (by["pcbc_geometry_segments"].constraint, by["pcbc_geometry_angles"].constraint) == ("(constraint track_segment_length (min 0.2mm))", "(constraint track_angle (min 135))")
        assert by["pcbc_canary"].constraint == "(constraint length (max 0.001mm))" and by["pcbc_canary"].severity == "warning"
        assert job.dru[-1].name == "pcbc_canary" and job.dru[-2].name == "pads_of_one_footprint"
        assert validate(job.dru) == []
        assert job.dru == rules(job.constraints), "A.3 item 5: computed once at compile from the ConstraintSet; apply and fab only render it"
        lists[name] = [r.name for r in job.dru]
    assert lists == {
        "blinky": ["pcbc_geometry_segments", "pcbc_geometry_angles", "width_power", "pads_of_one_footprint", "pcbc_canary"],
        "buck": ["pcbc_geometry_segments", "pcbc_geometry_angles", "width_power", "novia_fb", "novia_sw", "pads_of_one_footprint", "pcbc_canary"],
        "c3_usb": ["pcbc_geometry_segments", "pcbc_geometry_angles", "width_power", "vias_usb_dn", "vias_usb_dp", "skew_usb_dn_usb_dp", "usb_pair_gap", "uncoupled_usb", "pads_of_one_footprint", "pcbc_canary"],
        "node": ["pcbc_geometry_segments", "pcbc_geometry_angles", "width_usb", "width_power", "novia_t_div", "novia_t_out", "vias_usb_dn", "vias_usb_dp", "skew_usb_dn_usb_dp", "usb_pair_gap", "uncoupled_usb", "pads_of_one_footprint", "pcbc_canary"],
    }
    c3 = {r.name: r for r in compile_design(load_board(EXAMPLES / "c3_usb" / "c3_usb.py")).dru}
    assert (c3["usb_pair_gap"].constraint, c3["usb_pair_gap"].condition) == ("(constraint diff_pair_gap (min 0.1mm) (opt 0.127mm))", "A.hasNetclass('USB')"), "E.11: today's numbers, the condition rewritten from A.NetClass =="
    node = {r.name: r for r in compile_design(load_board(EXAMPLES / "node" / "node.py")).dru}
    assert node["usb_pair_gap"].constraint == "(constraint diff_pair_gap (min 0.12mm) (opt 0.15mm))", "H.2: node's pair at 0.2288 / 0.15"
    assert node["width_usb"].constraint == "(constraint track_width (min 0.2288mm))"


def test_soft_rule_kinds_are_the_four_of_h3():
    assert {soft_kind(n) for n in ("width_power", "skew_d_n_d_p", "vias_clk", "uncoupled_usb")} == {"track_width", "skew", "via_budget", "diff_pair_uncoupled"}
    assert soft_kind("pcbc_canary") is None and soft_kind("novia_fb") is None and soft_kind("length_sda") is None


# ---------------------------------------------------------------------------------------------
# validate and render
# ---------------------------------------------------------------------------------------------


def test_validate_refuses_what_would_disable_the_whole_file():
    """One malformed rule silently disables every rule and kicad-cli prints nothing (found by the
    canary on its first day: `(min 135deg)`)."""
    ok = DruRule("pcbc_geometry_angles", "(constraint track_angle (min 135))", "A.Type == 'Track'", "warning")
    assert validate([ok]) == []
    assert validate([DruRule("pcbc_geometry_angles", "(constraint track_angle (min 135deg))", "A.Type == 'Track'", "warning")]) == [
        "rule 'pcbc_geometry_angles': track_angle takes a bare number; a unit ((min 135deg)) silently disables every rule in KiCad 10.0.6"
    ]
    assert validate([DruRule("x", "(constraint physical_length (max 1mm))", "A.Type == 'Track'")]) == [
        "rule 'x': unknown constraint 'physical_length'; KiCad 10 checks " + ", ".join(sorted(KNOWN_CONSTRAINTS))
    ]
    assert validate([ok, DruRule("pcbc_geometry_angles", "(constraint track_angle (min 135))", "A.Type == 'Track'", "warning")]) == [
        "rule 'pcbc_geometry_angles' is written 2 times; KiCad keeps one and pcbc cannot say which"
    ]
    assert validate([DruRule("length_sda", "(constraint length (max 80))", "A.NetName == 'SDA'")]) == ["rule 'length_sda': length (max 80) needs a length in mm"]
    assert validate([DruRule("length_sda", "(constraint length (max 80mm))", "")]) == ["rule 'length_sda': empty condition"]
    assert validate([DruRule("length_sda", "(constraint length (max 80mm))", "A.NetName == 'SDA'", "soft")]) == ["rule 'length_sda': severity 'soft' is not one of error, warning, ignore"]
    assert validate([DruRule("novia_fb", "(constraint via_count (max 0))", "A.NetName == 'FB'"), DruRule("iso_a_b_area", "(constraint disallow track via zone)", "A.intersectsArea('ISO_a_b')")]) == [], "via_count and disallow carry no length"


def test_write_dru_validates_before_the_file_exists(tmp_path: Path):
    job = compile_design(load_board(EXAMPLES / "blinky" / "blinky.py"))
    pcb = tmp_path / "layout.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")
    assert write_dru(job, pcb).read_text() == render(job.dru)
    bad = replace(job, dru=job.dru + [DruRule("pcbc_geometry_angles", "(constraint track_angle (min 135deg))", "A.Type == 'Track'", "warning")])
    with pytest.raises(ValueError) as exc:
        write_dru(bad, tmp_path / "other.kicad_pcb")
    assert str(exc.value) == (
        "refusing to write .kicad_dru (one bad rule disables every rule in KiCad): "
        "rule 'pcbc_geometry_angles': track_angle takes a bare number; a unit ((min 135deg)) silently disables every rule in KiCad 10.0.6; "
        "rule 'pcbc_geometry_angles' is written 2 times; KiCad keeps one and pcbc cannot say which"
    )
    assert not (tmp_path / "other.kicad_dru").exists()


def test_render_is_todays_file_format():
    text = render([DruRule("a", "(constraint clearance (min 0.1mm))", "A.Type == 'Pad'"), DruRule("b", "(constraint length (max 0.001mm))", "A.NetName == 'GND'", "warning")])
    assert text == (
        "(version 1)\n# Generated by pcbc. Do not hand-edit.\n"
        '\n(rule "a"\n\t(constraint clearance (min 0.1mm))\n\t(condition "A.Type == \'Pad\'"))\n'
        '\n(rule "b"\n\t(severity warning)\n\t(constraint length (max 0.001mm))\n\t(condition "A.NetName == \'GND\'"))\n'
    )


# ---------------------------------------------------------------------------------------------
# Project classes: one writer for seed and apply, the examples' files byte for byte
# ---------------------------------------------------------------------------------------------


def test_project_classes_are_ordered_like_todays_files_and_seed_and_apply_agree(tmp_path: Path):
    for name in ("blinky", "buck", "c3_usb", "node"):
        design = load_board(EXAMPLES / name / f"{name}.py")
        job = compile_design(design)
        rows = project_classes(job.constraints)
        assert [r["name"] for r in rows] == [n for n in PROJECT_CLASS_ORDER if n in {c.name for c in job.classes}] + [c.name for c in job.classes if c.name not in PROJECT_CLASS_ORDER]
        seed = json.loads(emit_pro(design, name=name))
        assert seed["net_settings"]["classes"] == rows and seed["net_settings"]["netclass_patterns"] == netclass_patterns(job.constraints)
        pro = tmp_path / f"{name}.kicad_pro"
        pro.write_text(emit_pro(design, name=name))
        _apply_pro(pro, job)
        applied = json.loads(pro.read_text())
        assert [{k: r[k] for k in row} for r, row in zip(applied["net_settings"]["classes"], rows)] == rows, "identical class rows from both writers"
        assert pro.read_text() == (FIXTURES / "project" / f"{name}.kicad_pro").read_text(), f"{name}: the placed/routed/fab .kicad_pro is byte-identical to the one recorded before S3"
    node = project_classes(compile_design(load_board(EXAMPLES / "node" / "node.py")).constraints)
    assert node == [
        {"name": "Default", "clearance": 0.18, "track_width": 0.16, "via_diameter": 0.35, "via_drill": 0.2},
        {"name": "Analog", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "Power", "clearance": 0.2, "track_width": 0.4, "via_diameter": 0.8, "via_drill": 0.4},
        {"name": "USB", "clearance": 0.18, "track_width": 0.2288, "via_diameter": 0.35, "via_drill": 0.2, "diff_pair_gap": 0.15, "diff_pair_width": 0.2288},
    ], "node's rows: H.2 pair 0.2288 / 0.15; everything else today's"


def test_apply_pro_keeps_what_kicad_added_to_a_row(tmp_path: Path):
    job = compile_design(load_board(EXAMPLES / "buck" / "buck.py"))
    pro = tmp_path / "b.kicad_pro"
    pro.write_text(json.dumps({"net_settings": {"classes": [{"name": "Power", "clearance": 0.05, "track_width": 0.05, "via_diameter": 0.1, "via_drill": 0.05, "pcb_color": "rgba(1, 2, 3, 1.000)"}, {"name": "KRT", "clearance": 0.1}]}}))
    _apply_pro(pro, job)
    got = json.loads(pro.read_text())["net_settings"]
    names = [c["name"] for c in got["classes"]]
    assert names == ["Default", "Analog", "Power", "SwitchNode", "KRT"], "today's order: the listed classes first, then the rest as the file had them"
    power = next(c for c in got["classes"] if c["name"] == "Power")
    assert (power["track_width"], power["clearance"], power["pcb_color"]) == (0.781, 0.2, "rgba(1, 2, 3, 1.000)")
    assert got["netclass_patterns"][0] == {"netclass": "Default", "pattern": "SW"} or {"netclass": "Power", "pattern": "VIN"} in got["netclass_patterns"]


# ---------------------------------------------------------------------------------------------
# Rule areas and the slot (E.15, F.7), and the check that finds them missing
# ---------------------------------------------------------------------------------------------


def _seed_and_apply(tmp_path: Path, name: str, text: str):
    board = _board(tmp_path, name, text)
    design = load_board(board)
    job = compile_design(design)
    pcb = tmp_path / name / "layout.kicad_pcb"
    seed_job(design, pcb, name=name)
    apply_job(job, pcb, backup=False)
    return job, pcb


def test_an_isolation_writes_its_rule_area_zone_and_check_misses_it_when_gone(tmp_path: Path):
    from pcbc.check import check_job

    job, pcb = _seed_and_apply(tmp_path, "iso", ISO_HEAD + ISO + ISO_TAIL)
    text = pcb.read_text()
    assert job.constraints.rule_areas[0].box == (18.0, 0.0, 22.0, 25.0)
    zone = text[text.index('(name "ISO_primary_secondary")') - 200 : text.index('(name "ISO_primary_secondary")') + 400]
    assert '(layers "F&B.Cu")' in zone and "(tracks allowed)" in zone and "(vias allowed)" in zone and "(pads allowed)" in zone and "(copperpour allowed)" in zone and "(footprints allowed)" in zone, "E.15: pads and footprints stay allowed so the isolator straddles; the .kicad_dru rule carries the disallow"
    assert "(xy 18.00 0.00)" in zone and "(xy 22.00 25.00)" in zone
    assert text.count('(name "ISO_primary_secondary")') == 1
    assert "slot" not in text and text.count('(layer "Edge.Cuts")') == 1, "no slot without slot=True"
    assert "missing rule area ISO_primary_secondary" not in check_job(job, pcb)
    assert apply_job(job, pcb, backup=False) and pcb.read_text() == text, "re-applying replaces the zone, never duplicates it"
    from pcbc.apply import _drop_named_zone

    pcb.write_text(_drop_named_zone(text, "ISO_primary_secondary"))
    assert "missing rule area ISO_primary_secondary" in check_job(job, pcb)


def test_slot_true_cuts_a_1_mm_slot_centred_in_the_corridor(tmp_path: Path):
    job, pcb = _seed_and_apply(tmp_path, "iso_slot", ISO_HEAD + ISO_SLOT + ISO_TAIL)
    text = pcb.read_text()
    slot = "\t(gr_rect\n\t\t(start 19.5 3)\n\t\t(end 20.5 22)\n\t\t(stroke (width 0.05) (type default))\n\t\t(fill none)\n\t\t(layer \"Edge.Cuts\")\n"
    assert slot in text, "F.7: 1.0 mm wide (IEC 60664-1 groove rule at PD2), centred in the 18..22 corridor, 3 mm of web at each end of the 25 mm board"
    assert text.count('(layer "Edge.Cuts")') == 2
    assert _apply_slot(_apply_rule_areas(text, job), job) == text, "idempotent"


# ---------------------------------------------------------------------------------------------
# The gate: soft and rule counts; fab's ignore list and notes; copper reads the constraint
# ---------------------------------------------------------------------------------------------


def test_check_copper_counts_every_rule_and_the_soft_ones_and_keeps_them_out_of_drc_warnings(tmp_path: Path, monkeypatch):
    """KiCad's descriptions name the rule as `rule 'x'`, except the two diff-pair checks, which say
    `(x minimum gap ...)` / `(x maximum uncoupled length ...)` (KiCad 10.0.6, probed below)."""
    design = load_board(EXAMPLES / "c3_usb" / "c3_usb.py")
    pcb = tmp_path / "layout.kicad_pcb"
    pcb.write_text(emit_pcb(design, name="c3_usb"))
    doc = {
        "violations": [
            {"type": "length_out_of_range", "severity": "warning", "description": "Track length out of range (rule 'pcbc_canary' max length 0.0010 mm; actual 46.5664 mm)"},
            {"type": "track_width", "severity": "warning", "description": "Track width (rule 'width_power' min width 0.2500 mm; actual 0.1270 mm)"},
            {"type": "track_width", "severity": "warning", "description": "Track width (rule 'width_power' min width 0.2500 mm; actual 0.1270 mm)"},
            {"type": "skew_out_of_range", "severity": "warning", "description": "Skew between tracks out of range (rule 'skew_usb_dn_usb_dp' max skew 0.5000 mm; actual -11.6208 mm)"},
            {"type": "diff_pair_uncoupled_length_too_long", "severity": "warning", "description": "Differential uncoupled length too long (uncoupled_usb maximum uncoupled length 2.0000 mm; actual 42.4331 mm)"},
            {"type": "too_many_vias", "severity": "warning", "description": "Too many vias on a connection (rule 'vias_usb_dp' max count 2; actual 3)"},
            {"type": "track_segment_length", "severity": "warning", "description": "Track segment too short (rule 'pcbc_geometry_segments' ...)"},
            {"type": "track_angle", "severity": "warning", "description": "Track angle (rule 'pcbc_geometry_angles' ...)"},
            {"type": "silk_overlap", "severity": "warning", "description": "Silkscreen clearance"},
            {"type": "diff_pair_gap_out_of_range", "severity": "error", "description": "Differential pair gap out of range (usb_pair_gap minimum gap 0.1000 mm; actual 0.0900 mm)"},
        ],
        "unconnected_items": [],
    }
    monkeypatch.setattr(netcheck, "kicad_drc", lambda *a, **k: doc)
    gate = netcheck.check_copper(design, pcb)
    assert gate["canary"] is True and gate["geometry"] == {"segments": 1, "angles": 1}
    assert gate["soft"] == {"vias_usb_dn": 0, "vias_usb_dp": 1, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1, "width_power": 2}
    assert gate["rules"] == {"pcbc_geometry_segments": 1, "pcbc_geometry_angles": 1, "width_power": 2, "vias_usb_dn": 0, "vias_usb_dp": 1, "skew_usb_dn_usb_dp": 1, "usb_pair_gap": 1, "uncoupled_usb": 1, "pads_of_one_footprint": 0, "pcbc_canary": 1}, "every pcbc rule's hits, errors included (the pair-gap error itself is dropped by fab._IGNORE_DRC, not here)"
    assert gate["drc_warnings"] == 1, "the silk warning only: soft rules, geometry and the canary are excluded"
    assert gate["drc_errors"] == [] and gate["nets"] == [] and gate["ok"], "the seed binds every pad as board.py says"
    assert netcheck.names_rule("Track width (rule 'width_power' min width 0.2500 mm)", "width_power") and netcheck.names_rule("(uncoupled_usb maximum uncoupled length 2 mm)", "uncoupled_usb")
    assert not netcheck.names_rule("Track width (rule 'width_power_2' min width 0.4 mm)", "width_power")


def test_fab_ignores_the_rewritten_pair_gap_but_gates_on_length():
    assert "diff_pair_gap_out_of_range" in _IGNORE_DRC and "length_out_of_range" not in _IGNORE_DRC, "E: the canary is a warning and never reaches the error gate; an explicit length_mm= must gate"
    drc = {"violations": [
        {"type": "length_out_of_range", "severity": "error", "description": "Track length out of range (rule 'length_sda' max length 80.0000 mm; actual 90.0000 mm)"},
        {"type": "diff_pair_gap_out_of_range", "severity": "error", "description": "(usb_pair_gap minimum gap 0.1000 mm; actual 0.0900 mm)"},
        {"type": "length_out_of_range", "severity": "warning", "description": "Track length out of range (rule 'pcbc_canary' max length 0.0010 mm; actual 46.5664 mm)"},
    ]}
    assert [v["type"] for v in copper_drc_errors(drc)] == ["length_out_of_range"]


def test_fab_notes_print_the_constraint_lines_and_the_rule_count(tmp_path: Path):
    job = compile_design(load_board(EXAMPLES / "node" / "node.py"))
    _write_notes(tmp_path, job, {"via_in_pad": [], "fiducials": [], "missing_lcsc": []})
    notes = (tmp_path / "FAB_NOTES.md").read_text()
    assert "\n## Constraints\n\n- 3V3: width 0.4 mm (pcbc_floor amps >= 0.2; ipc2221_ext 1 A 10 C 1 oz 0.300; ipc2152_fit x board 1.099 x plane 0.430 at 0.2104 mm In1.Cu 0.134)\n" in notes
    assert "- USB_DP: skew 0.5 mm (preset usb_hs; TI usb_layout_basics) [soft: warning in R1]\n" in notes
    assert notes.endswith("- classes: Default 0.16/0.18, USB 0.2288/0.18 pair 0.2288/0.15, Power 0.4/0.2 via 0.8/0.4, Analog 0.2/0.2 via 0.6/0.3\n- rules: 13 written (4 error, 6 soft), canary on net 3V3\n"), "node: 13 rules; error: novia x2, usb_pair_gap, pads_of_one_footprint; soft: width x2, vias x2, skew, uncoupled; the geometry rules and the canary are warnings of neither kind"


def test_power_ampacity_reads_the_constraints_width():
    """A.3 item 7: one source. The number is the constraint's IPC-2221 external width (node VBUS at
    1 A: 0.300 mm), so the gate is unchanged."""
    job = compile_design(load_board(EXAMPLES / "node" / "node.py"))
    assert job.constraints.by_net("VBUS").current.width_ipc2221.value == 0.3
    text = (
        '(kicad_pcb\n\t(net 1 "VBUS")\n\t(net 2 "LOAD")\n'
        '\t(segment\n\t\t(start 1 1)\n\t\t(end 5 1)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net 1)\n\t)\n'
        '\t(segment\n\t\t(start 1 3)\n\t\t(end 5 3)\n\t\t(width 0.4)\n\t\t(layer "F.Cu")\n\t\t(net 2)\n\t)\n)\n'
    )
    assert power_ampacity_failures(job, text) == ["VBUS copper 0.20 mm < 0.30 mm required for 1 A (add a plane or pour)"]
    assert power_ampacity_failures(replace(job, constraints=None), text) == ["VBUS copper 0.20 mm < 0.30 mm required for 1 A (add a plane or pour)"], "without a ConstraintSet the fallback is the same formula"


# ---------------------------------------------------------------------------------------------
# KiCad probes (kicad-cli): the file parses, each rule kind fires alone and together
# ---------------------------------------------------------------------------------------------

_NS = uuid.UUID("5b9d2b2e-2c3a-4a1e-9c1e-8f1a3b2c4d5e")


def _uid(*parts) -> str:
    return str(uuid.uuid5(_NS, "|".join(map(str, parts))))


def _segment(x0, y0, x1, y1, net: int, w: float = 0.2, layer: str = "F.Cu") -> str:
    return (
        f"\t(segment\n\t\t(start {x0:g} {y0:g})\n\t\t(end {x1:g} {y1:g})\n\t\t(width {w:g})\n"
        f'\t\t(layer "{layer}")\n\t\t(net {net})\n\t\t(uuid "{_uid("seg", x0, y0, x1, y1, net)}")\n\t)\n'
    )


def _tiny_pcb(nets: list[str], segments: str) -> str:
    """A board with named nets and bare tracks (dangling, which KiCad only warns about)."""
    net_lines = "".join(f'\t(net {i + 1} "{n}")\n' for i, n in enumerate(nets))
    return (
        '(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbc")\n\t(generator_version "0.1")\n'
        '\t(general\n\t\t(thickness 1.6)\n\t)\n\t(paper "A4")\n'
        '\t(layers\n\t\t(0 "F.Cu" mixed)\n\t\t(2 "B.Cu" mixed)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
        "\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n"
        '\t(net 0 "")\n' + net_lines + segments
        + "\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 40 20)\n\t\t(stroke (width 0.05) (type default))\n\t\t(fill none)\n"
        f'\t\t(layer "Edge.Cuts")\n\t\t(uuid "{_uid("edge")}")\n\t)\n)\n'
    )


def _drc(work: Path) -> list[dict]:
    doc = netcheck.kicad_drc(work / "layout.kicad_pcb", refill=False)
    return doc.get("violations") or []


def _types(violations: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in violations:
        out[v["type"]] = out.get(v["type"], 0) + 1
    return out


def _canary_fired(violations: list[dict]) -> bool:
    return any("rule 'pcbc_canary'" in v.get("description", "") for v in violations)


@pytest.mark.kicad
@pytest.mark.parametrize("name", ["blinky", "buck", "c3_usb", "node"])
def test_the_examples_rule_files_parse_in_kicad(tmp_path: Path, name: str):
    """Each example's rendered .kicad_dru beside a board that has one track on the canary net and
    the example's project classes: the canary firing proves KiCad read every rule (a parse error
    anywhere drops the whole file and the canary with it)."""
    design = load_board(EXAMPLES / name / f"{name}.py")
    job = compile_design(design)
    work = tmp_path / name
    work.mkdir()
    canary = job.constraints.canary_net
    (work / "layout.kicad_pcb").write_text(_tiny_pcb([canary], _segment(5, 5, 25, 5, 1)))
    (work / "layout.kicad_pro").write_text(emit_pro(design, name=name))
    write_dru(job, work / "layout.kicad_pcb")
    violations = _drc(work)
    assert _canary_fired(violations), (name, _types(violations))
    assert _types(violations).get("length_out_of_range") == 1


PAIR_PRO = {
    "board": {"design_settings": {"defaults": {}, "rules": {"min_clearance": 0.1, "min_track_width": 0.1}, "rule_severities": {}}},
    "meta": {"filename": "layout.kicad_pcb", "version": 1},
    "net_settings": {
        "classes": [
            {"name": "Default", "clearance": 0.16, "track_width": 0.16, "via_diameter": 0.5, "via_drill": 0.3},
            {"name": "USB", "clearance": 0.155, "track_width": 0.2, "via_diameter": 0.5, "via_drill": 0.3, "diff_pair_gap": 0.15, "diff_pair_width": 0.2},
        ],
        "netclass_patterns": [{"netclass": "USB", "pattern": "D_P"}, {"netclass": "USB", "pattern": "D_N"}],
    },
    "text_variables": {},
}
PAIR_RULES = [
    DruRule("usb_pair_gap", "(constraint diff_pair_gap (min 0.20mm) (opt 0.25mm))", "A.hasNetclass('USB')"),
    DruRule("uncoupled_usb", "(constraint diff_pair_uncoupled (max 1mm))", "A.hasNetclass('USB')", "warning"),
    DruRule("skew_d_n_d_p", "(constraint skew (max 0.5mm))", "A.NetName == 'D_N' || A.NetName == 'D_P'", "warning"),
    DruRule("pcbc_canary", "(constraint length (max 0.001mm))", "A.NetName == 'D_P'", "warning"),
]


def _pair_board(tmp_path: Path, name: str, segments: str) -> dict[str, int]:
    work = tmp_path / name
    work.mkdir()
    (work / "layout.kicad_pcb").write_text(_tiny_pcb(["D_P", "D_N"], segments))
    (work / "layout.kicad_pro").write_text(json.dumps(PAIR_PRO, indent=2))
    (work / "layout.kicad_dru").write_text(render(PAIR_RULES))
    violations = _drc(work)
    assert _canary_fired(violations), (name, _types(violations))
    return {k: v for k, v in _types(violations).items() if k != "track_dangling"}


@pytest.mark.kicad
def test_kicad_evaluates_the_pair_rules_only_where_it_finds_the_pair_coupled(tmp_path: Path):
    """The open item from c3_usb's routed board (diff_pair_gap and diff_pair_uncoupled parsed, 0 hits,
    while skew on the same pair fired), settled on hand-made pairs (KiCad 10.0.6):

    - a pair coupled 10 mm at gap 0.15 (rule min 0.20) then diverging fires `diff_pair_gap_out_of_range`
      (one per net) and `diff_pair_uncoupled_length_too_long`, and the uncoupled length counts the
      too-tight run as uncoupled (16.8 mm = the 10 mm at 0.15 plus the divergence): a run whose gap is
      outside the rule's (min ...) does not count as coupled;
    - a pair coupled all the way at the (opt) gap fires nothing but the canary;
    - a pair whose tracks never run close (3 mm apart, or 1 mm = five widths) fires NEITHER rule: with no
      coupled segment at all KiCad reports no gap and no uncoupled length, however long the tracks;
    - skew fires whenever the two nets' lengths differ, coupled or not.

    So on c3_usb the routed gap (0.159) satisfies the written (min 0.10), and the pair's 42 mm of
    uncoupled length shows up only with `diff_pair_uncoupled` written (`uncoupled_usb` counts it as a
    soft hit in test_examples_fab.py); both rules are evaluated only on segments KiCad pairs up. The
    descriptions of the two pair checks name the rule without the word `rule` (`(usb_pair_gap minimum
    gap ...)`), which `netcheck.names_rule` reads."""
    coupled_then_diverge = _segment(5, 5, 15, 5, 1) + _segment(15, 5, 25, 5, 1) + _segment(5, 5.35, 15, 5.35, 2) + _segment(15, 5.35, 20, 10, 2) + _segment(20, 10, 25, 10, 2)
    assert _pair_board(tmp_path, "coupled_then_diverge", coupled_then_diverge) == {"diff_pair_gap_out_of_range": 2, "diff_pair_uncoupled_length_too_long": 1, "skew_out_of_range": 1, "length_out_of_range": 1}
    at_opt = _segment(5, 5, 25, 5, 1) + _segment(5, 5.45, 25, 5.45, 2)
    assert _pair_board(tmp_path, "coupled_at_opt", at_opt) == {"length_out_of_range": 1}
    too_tight = _segment(5, 5, 25, 5, 1) + _segment(5, 5.35, 25, 5.35, 2)
    assert _pair_board(tmp_path, "coupled_too_tight", too_tight) == {"diff_pair_gap_out_of_range": 2, "diff_pair_uncoupled_length_too_long": 1, "length_out_of_range": 1}
    never = _segment(5, 5, 25, 5, 1) + _segment(5, 8, 25, 8, 2)
    assert _pair_board(tmp_path, "never_coupled", never) == {"length_out_of_range": 1}, "no coupled segment: neither pair rule reports, however long the tracks"
    five_widths = _segment(5, 5, 25, 5, 1) + _segment(5, 6.2, 25, 6.2, 2)
    assert _pair_board(tmp_path, "apart_1mm", five_widths) == {"length_out_of_range": 1}


def _ds2_copy(dst: Path) -> Path:
    if not DS2.exists():
        pytest.skip(f"DS2 Addon not at {DS2}")
    shutil.copytree(DS2, dst / "ds2", ignore=shutil.ignore_patterns("layout"))
    return dst / "ds2" / "ds2_addon.py"


@pytest.fixture(scope="module")
def ds2_routed(tmp_path_factory) -> tuple[Path, Path]:
    """The DS2 Addon built to the routed stage once, in a temp copy (board + components); never in
    the MaD checkout."""
    from pcbc.build import build_job

    board = _ds2_copy(tmp_path_factory.mktemp("ds2"))
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result.get("error")
    return board, board.parent / "layout" / "ds2_addon" / "routed" / "layout.kicad_pcb"


@pytest.fixture(scope="module")
def buck_routed(tmp_path_factory) -> tuple[Path, Path]:
    """`examples/buck` built to the routed stage once, in a temp copy: a two-layer board with a
    power, a switch_node and an analog class, so the rule-kind probe runs wherever this repo is
    checked out (the DS2 Addon lives outside it and skips in CI)."""
    from pcbc.build import build_job

    work = tmp_path_factory.mktemp("buck")
    src = Path(__file__).resolve().parent.parent / "examples" / "buck"
    board = work / "buck.py"
    board.write_text((src / "buck.py").read_text())
    shutil.copytree(src / "components", work / "components")
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result.get("error")
    return board, work / "layout" / "buck" / "routed" / "layout.kicad_pcb"


def _probe(routed_board: tuple[Path, Path], tmp_path: Path, name: str, probe_rules: list[DruRule], area: RuleArea) -> dict[str, int]:
    """Write `probe_rules` (plus the canary) beside a copy of a routed board that carries the
    probe rule area, run DRC, return the violation types."""
    _board_py, routed = routed_board
    job = compile_design(load_board(_board_py))
    canary = next(r for r in job.dru if r.name == "pcbc_canary")
    work = tmp_path / name
    work.mkdir()
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy2(routed.with_suffix(ext), work / f"layout{ext}")
    text = _apply_rule_areas((work / "layout.kicad_pcb").read_text(), replace(job, constraints=replace(job.constraints, rule_areas=(area,))))
    (work / "layout.kicad_pcb").write_text(text)
    write_dru(replace(job, dru=probe_rules + [canary]), work / "layout.kicad_pcb")
    violations = _drc(work)
    assert _canary_fired(violations), (name, _types(violations))
    return _types(violations)


@pytest.mark.kicad
@pytest.mark.krt
def _rule_kind_probe(routed_board: tuple[Path, Path], tmp_path: Path, analog: tuple[str, str], area_box: tuple[float, float, float, float]) -> None:
    """Every E row's constraint and condition form, one at a time on a deliberately violating
    fixture, with the canary firing every time (H.7: the probe is the safety net for the rule
    semantics no source states all in one file), then all together."""
    _board_py, routed = routed_board
    copper = copper_by_net(routed.read_text())
    via_net = max(copper, key=lambda n: (copper[n]["via"], n))
    assert copper[via_net]["via"] >= 1
    a, b = analog
    area = RuleArea("ISO_left_right", area_box, ("F&B.Cu",), ("track", "via", "zone"), "probe")
    probes = {
        "length_out_of_range": DruRule(f"length_{a.lower()}", "(constraint length (max 1mm))", f"A.NetName == '{a}'"),
        "skew_out_of_range": DruRule("skew_probe", "(constraint skew (max 0.01mm))", f"A.NetName == '{a}' || A.NetName == '{b}'", "warning"),
        "too_many_vias": DruRule(f"novia_{via_net.lower()}", "(constraint via_count (max 0))", f"A.NetName == '{via_net}'"),
        "creepage": DruRule("creepage_power", "(constraint creepage (min 3mm))", "A.hasNetclass('Power') && !B.hasNetclass('Power') && B.NetName != ''"),
        "clearance": DruRule("analog_away_from_gnd", "(constraint clearance (min 1mm))", f"A.hasNetclass('Analog') && B.NetName == 'GND' && {NOT_OWN}"),
        "items_not_allowed": DruRule("iso_left_right_area", "(constraint disallow track via zone)", "A.intersectsArea('ISO_left_right')"),
        "track_width": DruRule("width_power", "(constraint track_width (min 1mm))", "A.hasNetclass('Power')"),
    }
    for typ, rule in probes.items():
        types = _probe(routed_board, tmp_path, typ, [rule], area)
        assert types.get(typ, 0) > 0, (typ, types)
        assert types.get("length_out_of_range", 0) >= 1
    together = _probe(routed_board, tmp_path, "all", list(probes.values()), area)
    for typ in probes:
        assert together.get(typ, 0) > 0, (typ, together)
    assert together["length_out_of_range"] == 2, "the canary and the length rule"


@pytest.mark.kicad
@pytest.mark.krt
def test_each_rule_kind_fires_alone_on_an_example_and_then_all_together(buck_routed, tmp_path: Path):
    """On `examples/buck`, so the probe runs in CI: FB and SW carry the Analog and SwitchNode
    copper, and the strip crosses the middle of the 40 x 25 board."""
    _rule_kind_probe(buck_routed, tmp_path, ("FB", "SW"), (19.0, 0.0, 22.0, 25.0))


@pytest.mark.kicad
@pytest.mark.krt
def test_each_rule_kind_fires_alone_on_the_routed_ds2_board_and_then_all_together(ds2_routed, tmp_path: Path):
    """The same probe on the real board, when it is on this machine."""
    _rule_kind_probe(ds2_routed, tmp_path, ("AIN0", "AIN1"), (22.0, 0.0, 25.0, 25.4))


@pytest.mark.kicad
@pytest.mark.krt
def test_the_gate_reports_soft_and_rule_counts_on_the_routed_ds2_board(ds2_routed):
    board, routed = ds2_routed
    design = load_board(board)
    gate = netcheck.check_copper(design, routed, refill=False)
    assert gate["ok"] and gate["canary"], gate["fails"]
    # Re-recorded 2026-09-20 for R2 S4: 5 -> 3. The hop pattern claims ten of ds2's two-pad nets
    # before KRT runs and `nRESET`'s escape is no longer spent, so KRT necks a power track below its
    # class twice less often (`docs/r2-measurements.md` S4). A fall, which is the direction a soft
    # rule is allowed to move; promotion still needs zero on every board (H.3).
    assert gate["soft"] == {"width_power": 3}, "H.3: the DS2 power stubs on closed rows are track_min wide; a soft hit, pinned (promotion needs zero on every board)"
    assert set(gate["rules"]) == {"pcbc_geometry_segments", "pcbc_geometry_angles", "width_power", "novia_ain0", "novia_ain1", "novia_ain2", "novia_ain3", "novia_refn_f", "novia_refp_f", "pads_of_one_footprint", "pcbc_canary"}
    assert gate["rules"]["pcbc_canary"] == 1 and all(gate["rules"][n] == 0 for n in gate["rules"] if n.startswith("novia_"))
    assert gate["rules"]["pcbc_geometry_segments"] == gate["geometry"]["segments"] and gate["rules"]["pcbc_geometry_angles"] == gate["geometry"]["angles"]


@pytest.mark.kicad
def test_kicad_resolves_creepage_per_net_pair_so_the_own_pads_exemption_is_dead(tmp_path: Path):
    """Why E.14 leaves the `across=` part's nets out of the condition instead of exempting its pads.

    Measured on KiCad 10.0.6: with `!(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference ==
    B.Reference)` on a creepage rule, the optocoupler's own two pads are still reported (0.85 mm
    against the 2.5 mm barrier), so every non-slot `Isolation` failed its own gate on the part
    whose internal barrier IS the rating. `creepage` resolves per net pair; no item-level
    exemption reaches it. The rule pcbc writes today names only the two sides' own nets.
    """
    from pcbc.build import build_job
    from pcbc.netcheck import kicad_drc

    board = _board(tmp_path, "iso_gate", ISO_HEAD + ISO + ISO_TAIL)
    result = build_job(board, upto="place", force=True)
    assert result.get("error") is None, result.get("error")
    placed = tmp_path / "layout" / "iso_gate" / "placed" / "layout.kicad_pcb"
    dru_path = placed.with_suffix(".kicad_dru")
    dru = dru_path.read_text()
    assert "iso_primary_secondary_creepage" in dru
    rule = [r for r in dru.split("\n(rule ") if r.startswith('"iso_primary_secondary_creepage"')][0]
    assert "LED_A" not in rule and "OUT" not in rule, "U7's own nets are out of the creepage condition"

    def creepage_hits(text: str) -> list[str]:
        # No canary here: it is a `length` rule and a placed board has no tracks for it to measure.
        # The old-condition run below is its own witness that the file is live.
        dru_path.write_text(text)
        doc = kicad_drc(placed, refill=False)
        return [
            v["description"] + " | " + "; ".join(i.get("description", "") for i in v.get("items") or [])
            for v in doc.get("violations") or []
            if "iso_primary_secondary_creepage" in (v.get("description") or "")
        ]

    old = dru.replace(
        "(A.NetName == 'GND' || A.NetName == 'VCC') && (B.NetName == 'GS' || B.NetName == 'VS')",
        "(A.NetName == 'GND' || A.NetName == 'LED_A' || A.NetName == 'VCC') && (B.NetName == 'GS' || B.NetName == 'OUT' || B.NetName == 'VS') && "
        "!(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference)",
        1,
    )
    hits = creepage_hits(old)
    assert hits, "with the across part's nets in the condition the exemption does not save it: KiCad reports U7's own pads"
    assert any("of U7" in h for h in hits) and any("0.8500" in h for h in hits), hits  # U7's own two pads, at its 0603 pitch
    assert creepage_hits(dru) == [], "as written (the across part's nets left out), the isolation creepage rule passes"
    dru_path.write_text(dru)
