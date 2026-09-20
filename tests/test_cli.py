import json
import shutil
from pathlib import Path

import pytest

from pcbc.build import build_job, constraint_lines, pcb_job, rules_line, rules_summary
from pcbc.cli import main
from pcbc.compile import compile_design
from pcbc.language import load_board

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"
EXAMPLES = BLINKY.parent.parent
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"


def test_cli_check_blinky(capsys):
    assert main(["check", str(BLINKY)]) == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out


def test_cli_check_missing(tmp_path: Path):
    assert main(["check", str(tmp_path / "nope.py")]) == 2


# ---------------------------------------------------------------------------------------------
# R1 slice S5 (docs/r1-design.md, Slices: S5): `pcbc check board.py --constraints [--json]` and
# `pcbc pcb --constraints` print the numbers a NetReq line became, each with its source.
# ---------------------------------------------------------------------------------------------


def _ds2(tmp_path: Path) -> Path:
    """The DS2 Addon, copied whole (board + components): nothing is ever built in the MaD checkout."""
    if not DS2.exists():
        pytest.skip(f"DS2 Addon not at {DS2}")
    shutil.copytree(DS2, tmp_path / "ds2")
    return tmp_path / "ds2" / "ds2_addon.py"


def _power(net: str) -> list[str]:
    """The DS2 Addon's four power nets share one NetReq (line 95) and so every number."""
    return [
        f"{net}: width 0.25 mm (pcbc_floor amps < 0.2; ipc2221_ext 0.1 A 10 C 1 oz 0.150; ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm B.Cu pour 0.003; below 0.274 A the IPC-2152 fit extrapolates)",
        f"{net}: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)",
        f"{net}: via 0.8/0.4 mm (preset power), 1 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]",
        f"{net}: loop 6 mm2 (preset power; decap loop, pcbc default)",
        f"{net}: layers F.Cu, B.Cu (preset power)",
        f"{net}: spacing 3W (preset power)",
    ]


def _analog(net: str) -> list[str]:
    """The six analog nets share NetReq line 96 (max_mm=30 over the preset's 25)."""
    return [
        f"{net}: width 0.2 mm (preset analog)",
        f"{net}: clearance 0.2 mm (preset analog)",
        f"{net}: no vias (preset analog)",
        f"{net}: airwire 30 mm (NetReq line 96; overrides preset analog 25)",
        f"{net}: keep_clear_of none",
        f"{net}: layers F.Cu (preset analog)",
        f"{net}: spacing 5W (preset analog)",
    ]


# `pcbc check ds2_addon.py --constraints`, after the check line: one number per line with its source,
# sorted by net (A.1; every net of one NetReq shares every number), then the class summary.
DS2_CONSTRAINT_LINES = (
    _power("3V3")
    + _analog("AIN0") + _analog("AIN1") + _analog("AIN2") + _analog("AIN3")
    + _power("GND")
    + _analog("REFN_F") + _analog("REFP_F")
    + _power("VDDA") + _power("VSS")
    + ["classes: Default 0.16/0.16, Power 0.25/0.2 via 0.8/0.4, Analog 0.2/0.2 via 0.6/0.3"]
)


def _rule_counts(job) -> tuple[int, int, int]:
    return (
        len(job.dru),
        sum(1 for r in job.dru if r.severity == "error"),
        sum(1 for r in job.dru if r.severity == "warning"),
    )


# The DS2 board's VDDA chain line (Chain line 97) prints between VDDA's via and loop lines.
DS2_CONSTRAINT_LINES.insert(
    next(i for i, l in enumerate(DS2_CONSTRAINT_LINES) if l.startswith("VDDA: loop ")),
    "VDDA: chain J2.1 -> C4.1 -> U1.12 (Chain line 97)",
)


def test_check_constraints_prints_every_number_with_its_source(tmp_path: Path, capsys):
    board = _ds2(tmp_path)
    assert main(["check", str(board), "--constraints"]) == 0, "S5 acceptance: exit code as `check` today; a clean board is 0"
    out = capsys.readouterr().out.splitlines()
    assert json.loads(out[0]) == {"ok": True, "instances": 28, "nets": 25}, "the check line comes first, as `pcbc check` prints it today"
    lines = out[1:]
    assert lines[:-1] == DS2_CONSTRAINT_LINES, (
        "S5 acceptance: one line per number with its source (D power: 0.25 floor under 0.2 A, C.6 2152 extrapolates below 0.274 A, "
        "C.7 0.4/0.018 mm barrel 0.871 A at 10 C, C.8 row 0-15 V B2 0.1; D analog: 0.2/0.2, no vias, F.Cu, 5W, airwire 25 overridden to 30 on line 96)"
    )
    job = compile_design(load_board(board))
    assert lines[-1] == rules_line(rules_summary(job)), "the rules line is computed from job.dru, which S3 rewrites; its format is pinned below"
    n, e, s = _rule_counts(job)
    assert lines[-1] == f"rules: {n} written ({e} error, {s} soft), canary on net 3V3", (
        "the spec's report example ends `rules: N written (E error, S soft), canary on net 3V3`; E.17: the canary is on the first net with two pads"
    )
    assert lines == constraint_lines(job), "build.constraint_lines is the one writer: cli, the build check step and pcb_job print the same lines"


def test_check_constraints_json_prints_the_constraintset_with_its_keys(tmp_path: Path, capsys):
    board = _ds2(tmp_path)
    assert main(["check", str(board), "--constraints", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert sorted(doc) == [
        "canary_net", "classes", "constraints", "groups", "isolation_specs", "isolations", "lines", "refusals", "rule_areas", "stackup",
    ], "A.1 ConstraintSet.to_dict(): the fields of the frozen dataclass (isolation_specs is S2's additive carrier of the C.8 numbers)"
    assert doc["stackup"] == "jlcpcb_2l_1oz" and doc["canary_net"] == "3V3" and doc["refusals"] == []
    assert doc["lines"] == DS2_CONSTRAINT_LINES, "the same lines as the text report (cs.lines; the rules line is the CLI's, from job.dru)"
    assert sorted(doc["constraints"][0]) == [
        "airwire_max_mm", "autoroute", "class_name", "clearance_mm", "current", "group", "guard_stitch_mm", "isolation_side", "keep_away",
        "kind", "lane_clearance_mm", "layers", "length_max_mm", "line", "loop_mm2", "net", "notes", "pair", "reference", "req_index",
        "soft", "spacing_w", "via", "voltage", "width_mm", "z_se",
    ], "A.1 Constraint, plus S2's additive req_index"
    assert doc["constraints"][0]["width_mm"] == {
        "value": 0.25, "unit": "mm", "formula": "pcbc_floor", "ref": "amps < 0.2",
        "note": "ipc2221_ext 0.1 A 10 C 1 oz 0.150; ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm B.Cu pour 0.003; below 0.274 A the IPC-2152 fit extrapolates",
    }, "A.1: a Derived flattens to {value, unit, formula, ref, note}"
    assert sorted(doc["classes"][0]) == [
        "clearance_mm", "diff_pair_gap_mm", "diff_pair_width_mm", "lane_clearance_mm", "name", "patterns", "track_width_mm", "via_diameter_mm", "via_drill_mm",
    ], "A.3: CompiledClass keeps every field and gains lane_clearance_mm"
    assert json.dumps(doc, sort_keys=True) == json.dumps(compile_design(load_board(board)).constraints.to_dict(), sort_keys=True), "byte-identical to ConstraintSet.to_dict()"


def test_check_constraints_exit_code_is_checks_and_a_caveat_is_not_a_failure(tmp_path: Path, capsys):
    # A refusal (A.4: a kwarg the kind does not take) is 1, the message on stderr; --json also puts it on stdout.
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from pcbc import *\n"
        'VCC = Power("VCC"); GND = Ground("GND")\n'
        'Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")\n'
        'NetReq("VCC", kind="switch_node", pf_max=3)\n'
        'Place("R1", at=(5, 5)); SchPlace("R1", left=20, top=20)\n'
    )
    msg = 'NetReq("VCC") line 5: kind="switch_node" does not take pf_max=; it takes loop_mm2, amps, max_mm, layers, vias, autoroute, class_name, keep_clear_of, keep_clear_mm, volts, z_se_ohm'
    assert main(["check", str(bad), "--constraints"]) == 1, "S5 acceptance: a refusal is 1"
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [msg] and captured.out == ""
    assert main(["check", str(bad), "--constraints", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "fails": [msg]}
    # c3_usb's 2-layer USB pair cannot reach 90 ohm (C.3 clamp): the note is a line, the exit code stays 0.
    assert main(["check", str(EXAMPLES / "c3_usb" / "c3_usb.py"), "--constraints"]) == 0, "S5 acceptance: the 2L USB note is not a failure"
    out = capsys.readouterr().out.splitlines()
    assert (
        'USB_DP/USB_DN: 90 ohm needs 0.7746 mm members at gap 0.15 on jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; '
        'fine for USB full speed, use Board(stackup="jlcpcb_4l_1oz") for high speed'
    ) in out, "C.10 vector 10: the pair-fit clamp's note"
    assert out[-1].startswith("rules: ") and out[-1].endswith(", canary on net 3V3")
    assert main(["check", str(EXAMPLES / "c3_usb" / "c3_usb.py")]) == 0
    assert capsys.readouterr().out.splitlines() == ['{"ok": true, "instances": 19, "nets": 11}'], "without --constraints the check prints what it printed before R1"


def test_pcb_constraints_prints_the_lines_and_json_carries_the_constraintset(tmp_path: Path, capsys):
    shutil.copytree(EXAMPLES / "blinky", tmp_path / "blinky")
    board = tmp_path / "blinky" / "blinky.py"
    assert main(["pcb", str(board), "--constraints"]) == 0
    out = capsys.readouterr().out.splitlines()
    job = compile_design(load_board(board))
    lines = constraint_lines(job)
    head = out.index(f"constraints: {len(lines)} lines (what the placement was checked against; pcbc check --constraints prints them alone)")
    assert out[head + 1 :] == [f"  {line}" for line in lines], "S5: `pcbc pcb --constraints` prints the constraints: lines after the moves"
    assert "VCC: width 0.25 mm (pcbc_floor amps < 0.2; ipc2221_ext 0.05 A 10 C 1 oz 0.150; ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm B.Cu pour 0.001; below 0.274 A the IPC-2152 fit extrapolates)" in lines, "D power at 0.05 A: the 0.25 floor"
    assert lines[-1].endswith(", canary on net LED"), "E.17: blinky's canary net"
    assert main(["pcb", str(board), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert sorted(doc["constraints"]) == sorted(job.constraints.to_dict()), "S5: pcb_job carries constraints: cs.to_dict() under --json"
    n, e, s = _rule_counts(job)
    assert doc["rules"] == {"written": n, "error": e, "soft": s, "canary_net": "LED"}
    assert doc["constraints"]["lines"] == lines[:-1]


def test_build_check_step_carries_the_constraint_lines(tmp_path: Path):
    shutil.copytree(EXAMPLES / "blinky", tmp_path / "blinky", ignore=shutil.ignore_patterns("layout"))
    board = tmp_path / "blinky" / "blinky.py"
    result = build_job(board, upto="check", force=True)
    assert result["ok"] and result["steps"][0]["stage"] == "check" and result["steps"][0]["fails"] == []
    assert result["steps"][0]["constraints"] == constraint_lines(compile_design(load_board(board))), "S5: the check step carries constraints: cs.lines (plus the rules line)"
    assert not (tmp_path / "blinky" / "layout" / "blinky" / "layout.kicad_pcb").exists(), "--upto check writes no board"
    job = pcb_job(board)
    assert job["error"] is None and sorted(job["constraints"]) == sorted(compile_design(load_board(board)).constraints.to_dict())


# The S5 acceptance pins `pcbc check --constraints` on the DS2 Addon, which lives outside this
# repo and skips in CI. The same acceptance on an example runs everywhere.
BUCK_CONSTRAINT_LINES = [
    *[
        f"{net}: {line}"
        for net in ("5V", "GND", "VIN")
        for line in (
            "width 0.781 mm (ipc2221_ext IPC-2221B eq. 6-2 external 2 A 10 C 1 oz; IPC-2221 floor holds over ipc2152_fit x board 1.092 x plane 0.593 at 1.53 mm 0.646)",
            "clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)",
            "via 0.8/0.4 mm (preset power), 3 per layer change (via_barrel 0.4/0.018 mm 0.871 A at 10 C) [report only in R1]",
            "loop 6 mm2 (preset power; decap loop, pcbc default)",
            "layers F.Cu, B.Cu (preset power)",
            "spacing 3W (preset power)",
        )
    ],
]
BUCK_ANALOG = [
    "FB: width 0.2 mm (preset analog)",
    "FB: clearance 0.2 mm (preset analog)",
    "FB: no vias (preset analog)",
    "FB: airwire 25 mm (preset analog)",
    'FB: keep_clear_of none; NetReq("FB", kind="analog", keep_clear_of="SW") holds 3 mm',
    "FB: layers F.Cu (preset analog)",
    "FB: spacing 5W (preset analog)",
]
BUCK_SWITCH = [
    "SW: width 0.3 mm (preset switch_node)",
    "SW: clearance 0.2 mm (preset switch_node)",
    "SW: no vias (preset switch_node)",
    "SW: airwire 6 mm (NetReq line 56; overrides preset switch_node 8)",
    "SW: loop 20 mm2 (preset switch_node; hot loop, pcbc default)",
    "SW: layers F.Cu (preset switch_node)",
    "SW: spacing 3W (preset switch_node)",
]


def test_check_constraints_on_an_example_prints_every_number_with_its_source(tmp_path: Path, capsys):
    """The S5 acceptance, on a board this repo carries: every number one line, with its source
    (IPC-2221 beating the 2152 fit and saying so, the 3 vias a 2 A rail wants per layer change,
    the keep-away pcbc will not default, the airwire the NetReq overrode)."""
    src = Path(__file__).resolve().parent.parent / "examples" / "buck"
    board = tmp_path / "buck.py"
    board.write_text((src / "buck.py").read_text())
    shutil.copytree(src / "components", tmp_path / "components")
    assert main(["check", str(board), "--constraints"]) == 0
    out = capsys.readouterr().out.splitlines()
    lines = out[1:]
    expected = sorted(BUCK_CONSTRAINT_LINES + BUCK_ANALOG + BUCK_SWITCH, key=lambda line: line.split(":")[0])
    assert lines[:-2] == expected, lines[:-2]
    assert lines[-2] == "classes: Default 0.16/0.16, Power 0.781/0.2 via 0.8/0.4, SwitchNode 0.3/0.2 via 0.6/0.3, Analog 0.2/0.2 via 0.6/0.3"
    assert lines[-1].startswith("rules: ") and "canary on net " in lines[-1]
