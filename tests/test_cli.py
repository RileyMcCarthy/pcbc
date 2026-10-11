import json
import os
import shutil
from pathlib import Path

import pytest

from pcbc.build import build_job, constraint_lines, pcb_job, rules_line, rules_summary
from pcbc.cli import main
from pcbc.compile import compile_design
from pcbc.language import check_board, load_board
from conftest import ds2_dir, write_split
from conftest import copy_board_file

BLINKY = Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py"
EXAMPLES = BLINKY.parent.parent
DS2 = ds2_dir()


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
    """The DS2 Addon's four power nets share one NetReq (line 95) and so every number; since
    docs/physics-subjects.md PR 3 a net declares no current, so the width is the class floor."""
    return [
        f"{net}: width 0.16 mm (class_floor max(0.16, track_min 0.127))",
        f"{net}: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)",
        f"{net}: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)",  # PR 5: a net kind sets no via
        f"{net}: loop 6 mm2 (preset power; decap loop, pcbc default)",
        f"{net}: layers F.Cu, B.Cu (preset power)",
        f"{net}: spacing 3W (preset power)",
    ]


def _analog(net: str) -> list[str]:
    """The six analog nets share NetReq line 96 (max_mm=30 over the preset's 25)."""
    return [
        # PR 5 (§3.5): analog keeps clearance, span, keep-aways and spacing; no width, via or layer of its own.
        f"{net}: width 0.16 mm (class_floor max(0.16, track_min 0.127))",
        f"{net}: clearance 0.2 mm (preset analog)",
        f"{net}: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)",
        f"{net}: airwire 30 mm (NetReq line 96; overrides preset analog 25)",
        f"{net}: keep_clear_of none",
        f"{net}: layers F.Cu, B.Cu (preset analog)",
        f"{net}: spacing 5W (preset analog)",
    ]


# `pcbc check ds2_addon.py --constraints`, after the check line: one number per line with its source,
# sorted by net (A.1; every net of one NetReq shares every number), then the class summary.
def _ground(net: str) -> list[str]:
    """PR 4 (E3): GND and VSS are Ground() nets, on their own NetReq (line 95's second statement) with no volts=."""
    return [x.replace("preset power; ipc2221_6_1 row 0-15 V B2 0.1)", "preset power)") for x in _power(net)]


DS2_CONSTRAINT_LINES = (
    _power("3V3")
    + _analog("AIN0") + _analog("AIN1") + _analog("AIN2") + _analog("AIN3")
    + _ground("GND")
    + _analog("REFN_F") + _analog("REFP_F")
    + _power("VDDA") + _ground("VSS")
    + ["classes: Default 0.16/0.16, Power 0.16/0.2, Analog 0.16/0.2"]
)


def _rule_counts(job) -> tuple[int, int, int]:
    return (
        len(job.dru),
        sum(1 for r in job.dru if r.severity == "error"),
        sum(1 for r in job.dru if r.severity == "warning"),
    )


# The DS2 board's VDDA chain line (Path line 97) prints between VDDA's via and loop lines.
DS2_CONSTRAINT_LINES.insert(
    next(i for i, l in enumerate(DS2_CONSTRAINT_LINES) if l.startswith("VDDA: loop ")),
    "VDDA: path J2.1 -> C4.1 -> U1.12 (Path line 97)",
)


def test_check_constraints_prints_every_number_with_its_source(tmp_path: Path, capsys):
    board = _ds2(tmp_path)
    assert main(["check", str(board), "--constraints"]) == 0, "S5 acceptance: exit code as `check` today; a clean board is 0"
    out = capsys.readouterr().out.splitlines()
    doc = json.loads(out[0])
    assert {k: doc[k] for k in ("ok", "instances", "nets")} == {"ok": True, "instances": 28, "nets": 25}, "the check line comes first, as `pcbc check` prints it today"
    # `notes` is `check_design`'s out-parameter for a finding that is a **move and not a failure**
    # (`docs/stitch-plan.md` S3): the DS2 Addon's `GND` and `VSS` are both `Ground()` and nothing ties
    # them. It has to be in the JSON as well as on stdout, or the one consumer that reads only the
    # JSON — an AI — never sees the finding at all. Exit code stays 0, which is the point.
    assert len(doc["notes"]) == 1 and doc["notes"][0].startswith("GND/VSS: VSS cannot reach GND."), doc["notes"]
    lines = [ln for ln in out[1:] if not ln.startswith("note: ")]
    assert lines[:-1] == DS2_CONSTRAINT_LINES, (
        "S5 acceptance: one line per number with its source (D power: the class floor, no net current (physics-subjects PR 3), "
        "C.8 row 0-15 V B2 0.1; D analog: the class floor, vias, F.Cu and B.Cu (PR 5), 5W, airwire 25 overridden to 30 on line 96)"
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
        "bridges", "canary_net", "classes", "constraints", "groups", "isolation_specs", "isolations", "lines", "loops", "planes", "refusals", "rule_areas", "stackup", "thermals",
    ], (
        "A.1 ConstraintSet.to_dict(): the fields of the frozen dataclass (isolation_specs is S2's additive carrier of the C.8 numbers; "
        "bridges is S3's, and it is the one spec in the file with no number in it — a `Bridge()` compiles to a part the board already places; "
        "planes is S5's, the (net, layer) pours the board will HAVE — `Board(planes=)` on four layers and the back GND pour krt_plan writes "
        "on two, which `Board` refuses to let an author declare. Without it the ConstraintSet had no net-to-layer map at all and classifying "
        "a layer change needed a PCB file, which is exactly what `docs/stitch-plan.md` S5 exists not to need; "
        "thermals is S6's, one per `Thermal()` — the barrel's K/W and the count a watt needs, both computable from board.py and the stackup "
        "alone, with the pitch and the sites deliberately left to the pattern because both are questions about a land's own geometry)"
    )
    assert doc["planes"] == [["GND", "B.Cu"]], "the DS2 Addon is two layers with GND a power net, so krt_plan writes it a back pour"
    assert doc["thermals"] == [], "the DS2 Addon is TSSOP-16 with no pad_prop_heatsink land anywhere, so it declares no Thermal() and the key is an empty list rather than absent"
    assert doc["stackup"] == "jlcpcb_2l_1oz" and doc["canary_net"] == "3V3" and doc["refusals"] == []
    assert doc["lines"] == DS2_CONSTRAINT_LINES, "the same lines as the text report (cs.lines; the rules line is the CLI's, from job.dru)"
    assert sorted(doc["constraints"][0]) == [
        "airwire_max_mm", "autoroute", "class_name", "clearance_mm", "group", "guard_ground", "guard_stitch_mm", "isolation_side",
        "keep_away", "kind", "lane_clearance_mm", "layers", "length_max_mm", "line", "loop_mm2", "net", "notes", "off_path", "pair", "paths", "reference",
        "req_index", "rise_ps", "soft", "spacing_w", "via", "voltage", "width_mm",
    ], (
        "A.1 Constraint, plus S2's additive req_index and S7's guard_ground. `Guard(net, ground=)` has taken a ground net since R1 and, "
        "measured 2026-09-21, `constraints._compile` stored only `stitch_mm`: a board saying `Guard(\"AIN0\", ground=\"VSS\")` compiled "
        "to a guard the router would have written in whatever net the pattern defaulted to (`docs/stitch-plan.md` section 2(r) predicted "
        "this pin would move and said to move it in the same commit as the geometry). Half a statement reaching the router is the failure "
        "the ConstraintSet exists to prevent, so the field is the fix and this line is the ledger of it. "
        "S8 adds `rise_ps`, and it is the only field here with no `Derived` around it and no default behind it: an edge rate is a fact "
        "about the parts a board is built from, the way `Thermal(watts=)` is, and `docs/stitch-plan.md` section 8 item 3 refuses the "
        "per-kind `DEFAULT_RISE_PS` guesses that a stitch pitch would then scale linearly with. It is `None` on every net of every board "
        "in this repo (measured 2026-09-21), which is why the plane stitch refuses by name instead of inventing a pitch. "
        "docs/net-path-physics.md adds `paths` and `off_path` (every Path() of the net, compiled, and the pads none names); "
        "docs/physics-subjects.md PR 3 removes `current`, `widths` and `via_group`: a net carries no current of its own; "
        "PR 5 removes `z_se`: an impedance is a Path's"
    )
    assert doc["constraints"][0]["width_mm"] == {
        "value": 0.16, "unit": "mm", "formula": "class_floor", "ref": "max(0.16, track_min 0.127)", "note": "",
    }, "A.1: a Derived flattens to {value, unit, formula, ref, note}"
    assert sorted(doc["classes"][0]) == [
        "clearance_mm", "diff_pair_gap_mm", "diff_pair_width_mm", "lane_clearance_mm", "name", "patterns", "track_width_mm", "via_diameter_mm", "via_drill_mm",
    ], "A.3: CompiledClass keeps every field and gains lane_clearance_mm"
    assert json.dumps(doc, sort_keys=True) == json.dumps(compile_design(load_board(board)).constraints.to_dict(), sort_keys=True), "byte-identical to ConstraintSet.to_dict()"


def test_check_constraints_exit_code_is_checks_and_a_caveat_is_not_a_failure(tmp_path: Path, capsys):
    # A refusal (A.4: a kwarg the kind does not take) is 1, the message on stderr; --json also puts it on stdout.
    bad = tmp_path / "bad.py"
    write_split(bad, "from pcbc import *\n"
        'VCC = Power("VCC"); GND = Ground("GND")\n'
        'Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")\n'
        'NetReq("VCC", kind="switch_node", pf_max=3)\n'
        'Place("R1", at=(5, 5)); SchPlace("R1", left=20, top=20)\n')
    # `rise_ps` is last: S8 appended it to `constraints.COMMON_KWARGS`, because an edge rate belongs to
    # whichever net has one and the plane stitch folds the fastest of them over the whole board.
    msg = 'NetReq("VCC") line 5: kind="switch_node" does not take pf_max=; it takes max_mm, class_name, keep_clear_of, keep_clear_mm, volts, rise_ps'
    assert main(["check", str(bad), "--constraints"]) == 1, "S5 acceptance: a refusal is 1"
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [msg] and captured.out == ""
    assert main(["check", str(bad), "--constraints", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "fails": [msg], "notes": []}
    # c3_usb's 2-layer USB pair cannot reach 90 ohm (C.3 clamp): the note is a line, the exit code stays 0.
    assert main(["check", str(EXAMPLES / "c3_usb" / "c3_usb.py"), "--constraints"]) == 0, "S5 acceptance: the 2L USB note is not a failure"
    out = capsys.readouterr().out.splitlines()
    assert (
        'USB_DP/USB_DN: 90 ohm needs 0.7746 mm members at gap 0.15 on jlcpcb_2l_1oz (formula only); pair written at the fab floor 0.127/0.127 = 140.1 ohm; '
        'within the USB full speed tolerance, outside the high speed one'
    ) in out, "C.10 vector 10: the pair-fit clamp's note"
    assert out[-1].startswith("rules: ") and out[-1].endswith(", canary on net 3V3")
    assert main(["check", str(EXAMPLES / "c3_usb" / "c3_usb.py")]) == 0
    assert capsys.readouterr().out.splitlines() == ['{"ok": true, "instances": 19, "nets": 11, "notes": []}'], (
        "without --constraints the check prints what it printed before R1, plus S3's `notes` — empty here, and empty on every board "
        "with fewer than two `Ground()` nets, which is all four examples"
    )


def test_pcb_constraints_prints_the_lines_and_json_carries_the_constraintset(tmp_path: Path, capsys):
    shutil.copytree(EXAMPLES / "blinky", tmp_path / "blinky")
    board = tmp_path / "blinky" / "blinky.py"
    assert main(["pcb", str(board), "--constraints"]) == 0
    out = capsys.readouterr().out.splitlines()
    job = compile_design(load_board(board))
    lines = constraint_lines(job)
    head = out.index(f"constraints: {len(lines)} lines (what the placement was checked against; pcbc check --constraints prints them alone)")
    assert out[head + 1 :] == [f"  {line}" for line in lines], "S5: `pcbc pcb --constraints` prints the constraints: lines after the moves"
    assert "VCC: width 0.16 mm (class_floor max(0.16, track_min 0.127))" in lines, "physics-subjects PR 3: blinky's VCC declares no current; the class floor"
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
BUCK_LINES = [
    '5V: width 0.16 mm (class_floor max(0.16, track_min 0.127))',
    '5V: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)',
    '5V: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)',
    '5V: path L1.2 -> C_OUT1.1 -> J_OUT.1 (Path line 48): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), 1.09 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 3 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.750 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)',
    '5V: path C_OUT1.1 -> R_FB_TOP.1 (Path line 50, kind feedback): width 0.2 mm (preset feedback), no vias, layers F.Cu, length 15 mm; meets 5V only inside C_OUT1.1, its first station',
    "5V: C_OUT2.1 is on no Path; its branch is 0.16 mm (class floor) and carries no declared current; a 0.16 mm branch carries 0.633 A on F.Cu (over the B.Cu pour) and 0.633 A on B.Cu at 10 C, not Path line 48's 2 A",
    "GND: J_OUT is a two-pad part between 5V and GND; Path line 48 carries 2 A into J_OUT.1, so if J_OUT passes DC, J_OUT.2 carries 2 A too, into the GND pour; version 1 does not size a return in a pour (net-path-physics.md §10 A), so nothing checks that J_OUT.2's joint to the pour carries 2 A",
    "5V: KiCad does not check Path line 48's copper (width, vias, layers): a KiCad rule selects copper by net or class, never the copper between two pads; pcbc's copper gate checks it on the emitted board",
    "5V: KiCad does not check Path line 50's copper (width, vias, layers): a KiCad rule selects copper by net or class, never the copper between two pads; pcbc's copper gate checks it on the emitted board",
    '5V: loop 6 mm2 (preset power; decap loop, pcbc default)',
    '5V: layers F.Cu, B.Cu (preset power)',
    '5V: spacing 3W (preset power)',
    'FB: width 0.16 mm (class_floor max(0.16, track_min 0.127))',
    'FB: clearance 0.2 mm (preset analog)',
    'FB: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)',
    'FB: airwire 25 mm (preset analog)',
    'FB: keep_clear_of none; this board has a switch node (SW)',
    'FB: layers F.Cu, B.Cu (preset analog)',
    'FB: spacing 5W (preset analog)',
    'GND: width 0.16 mm (class_floor max(0.16, track_min 0.127))',
    'GND: clearance 0.2 mm (preset power)',
    'GND: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)',
    'GND: loop 6 mm2 (preset power; decap loop, pcbc default)',
    'GND: layers F.Cu, B.Cu (preset power)',
    'GND: spacing 3W (preset power)',
    'SW: width 0.16 mm (class_floor max(0.16, track_min 0.127))',
    'SW: clearance 0.2 mm (preset switch_node)',
    'SW: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)',
    'SW: airwire 6 mm (NetReq line 43; overrides preset switch_node 8)',
    'SW: path U1.SW -> L1.1 (Path line 46): width 0.781 mm on F.Cu (ipc2221_ext 2 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), 1.09 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 4 vias per layer change, flow-through at 1.1 mm pitch, worst barrel 0.622 A of 0.707 (via_barrel 0.3/0.018 mm at 10 C; lumped DC model)',
    "SW: C_BOOT.2 is on no Path; its branch is 0.16 mm (class floor) and carries no declared current; a 0.16 mm branch carries 0.633 A on F.Cu (over the B.Cu pour) and 0.633 A on B.Cu at 10 C, not Path line 46's 2 A",
    # Postfix A5: a current into or out of a pad of a part with more than two pads, whose other pad is on a pour.
    "GND: U1 has 6 pads; Path line 46 carries 2 A out of U1.2, and up to 2 A of it can pass through U1.1 on GND, into the GND pour; version 1 does not size a return in a pour (net-path-physics.md §10 A), so nothing checks that its joint to the pour carries 2 A",
    "SW: KiCad does not check Path line 46's copper (width, vias, layers): a KiCad rule selects copper by net or class, never the copper between two pads; pcbc's copper gate checks it on the emitted board",
    'SW: loop 20 mm2 (preset switch_node; hot loop, pcbc default)',
    'SW: layers F.Cu, B.Cu (preset switch_node)',
    'SW: spacing 3W (preset switch_node)',
    'VIN: width 0.16 mm (class_floor max(0.16, track_min 0.127))',
    'VIN: clearance 0.2 mm (preset power; ipc2221_6_1 row 0-15 V B2 0.1)',
    'VIN: via 0.5/0.3 mm (stackup jlcpcb_2l_1oz)',
    'VIN: path J_IN.1 -> C_IN2.1 -> C_IN1.1 -> U1.VIN (Path line 53): width 0.459 mm on F.Cu (ipc2221_ext 1.36 A 10 C 1 oz jlcpcb_2l_1oz; plane 0.593 at 1.53 mm B.Cu pour), 0.541 mm on B.Cu (ipc2152_fit 1 oz jlcpcb_2l_1oz, no plane); 2 vias per layer change, flow-through at 1.3 mm pitch, worst barrel 0.680 A of 0.871 (via_barrel 0.4/0.018 mm at 10 C; lumped DC model)',
    "VIN: R_EN.1 is on no Path; its branch is 0.16 mm (class floor) and carries no declared current; a 0.16 mm branch carries 0.633 A on F.Cu (over the B.Cu pour) and 0.633 A on B.Cu at 10 C, not Path line 53's 1.36 A",
    "GND: J_IN is a two-pad part between VIN and GND; Path line 53 carries 1.36 A out of J_IN.1, so if J_IN passes DC, J_IN.2 carries 1.36 A too, into the GND pour; version 1 does not size a return in a pour (net-path-physics.md §10 A), so nothing checks that J_IN.2's joint to the pour carries 1.36 A",
    "GND: U1 has 6 pads; Path line 53 carries 1.36 A into U1.3, and up to 1.36 A of it can pass through U1.1 on GND, into the GND pour; version 1 does not size a return in a pour (net-path-physics.md §10 A), so nothing checks that its joint to the pour carries 1.36 A",
    "VIN: KiCad does not check Path line 53's copper (width, vias, layers): a KiCad rule selects copper by net or class, never the copper between two pads; pcbc's copper gate checks it on the emitted board",
    'VIN: loop 6 mm2 (preset power; decap loop, pcbc default)',
    'VIN: layers F.Cu, B.Cu (preset power)',
    'VIN: spacing 3W (preset power)',
    'loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52): 20 mm2 (pcbc default; U1 carries SW)',
]
"""The migrated buck's report, every line (docs/physics-subjects.md §8): no net carries a current, so every net's own
copper is the class floor; VIN's 1.36 A is Path line 53 (through both input caps), 5V's 2 A Path line 48, SW's Path
line 46; GND's 1.53 A is undeclared (a poured return path); the off-path pads say what their branch carries and
that it declares no current; each path width KiCad does not check is listed (KD 12). Re-pinned 2026-10-02 from
`pcbc check buck.py --constraints` (build.constraint_lines)."""


def test_check_constraints_on_an_example_prints_every_number_with_its_source(tmp_path: Path, capsys):
    """The S5 acceptance, on a board this repo carries: every number one line, with its source
    (IPC-2221 beating the 2152 fit and saying so, the 3 vias a 2 A rail wants per layer change,
    the keep-away pcbc will not default, the airwire the NetReq overrode)."""
    src = Path(__file__).resolve().parent.parent / "examples" / "buck"
    board = tmp_path / "buck.py"
    copy_board_file(board, src / "buck.py")
    shutil.copytree(src / "components", tmp_path / "components")
    assert main(["check", str(board), "--constraints"]) == 0
    out = capsys.readouterr().out.splitlines()
    lines = out[1:]
    assert lines[:-2] == BUCK_LINES, lines[:-2]
    assert lines[-2] == 'classes: Default 0.16/0.16, Power 0.16/0.2, SwitchNode 0.16/0.2, Analog 0.16/0.2'
    assert lines[-1].startswith("rules: ") and "canary on net " in lines[-1]


# ---------------------------------------------------------------------------------------------
# A rail that cannot carry its declared current is the one thing a *passing* build says that a
# reader must not scroll past. Until now it said it only in `fab/report.json` and `FAB_NOTES.md`
# while the build printed `copper: verified`, `error: null` and exited 0 (docs/r2-measurements.md,
# "Power, in the build's own words").
# ---------------------------------------------------------------------------------------------


def test_build_prints_the_power_moves_on_stderr_and_still_exits_zero(monkeypatch, capsys):
    """The default: a move, loudly, and exit 0. The copper that necks is KRT's leftover and R3's
    maze router owns it — a gate that stops three of five example boards on a fault the tool cannot
    yet repair teaches an author to reach for `--force`, which is worse than a move they read."""
    move = 'VIN: declared 2 A, carries 1.21 A ... NetReq("VIN", amps=...)'
    monkeypatch.setattr("pcbc.cli.build_job", lambda *a, **k: {"ok": True, "error": None, "power": [move]})
    assert main(["build", str(BLINKY)]) == 0
    cap = capsys.readouterr()
    assert f"power: {move}" in cap.err, cap.err
    assert json.loads(cap.out)["power"] == [move], "and in the JSON the next tool reads"


def test_strict_power_asks_for_the_stop(monkeypatch, capsys):
    """`--strict-power` (or `PCBC_STRICT_POWER=1`) is the author saying "I want the move to stop the
    build"; `fab_job` turns the moves into its own error. The flag is the only thing the CLI does —
    the decision lives in the fab gate, next to the measurement it is made of."""
    monkeypatch.delenv("PCBC_STRICT_POWER", raising=False)
    seen = {}
    monkeypatch.setattr(
        "pcbc.cli.build_job",
        lambda *a, **k: seen.setdefault("env", os.environ.get("PCBC_STRICT_POWER")) and None or {"ok": True, "error": None},
    )
    assert main(["build", str(BLINKY)]) == 0
    assert seen["env"] is None, "the default is a move, not a gate"
    seen.clear()
    assert main(["build", str(BLINKY), "--strict-power"]) == 0
    assert seen["env"] == "1", seen
    # And it does not outlive its build. A flag set on the process instead of scoped to the call
    # turned buck's, c3_usb's and node's own builds into errors later in the same pytest session —
    # which is exactly what a second `main()` call in one process would do to a user.
    assert "PCBC_STRICT_POWER" not in os.environ, "a strict flag that outlives its build is a leak"


def test_strict_flags_put_back_what_was_there_before(monkeypatch):
    """Including a value the caller set on purpose: the flag restores, it does not delete."""
    monkeypatch.setenv("PCBC_STRICT_PATTERNS", "keep-me")
    monkeypatch.setattr("pcbc.cli.build_job", lambda *a, **k: {"ok": True, "error": None})
    assert main(["build", str(BLINKY), "--strict-patterns", "--strict-power"]) == 0
    assert os.environ["PCBC_STRICT_PATTERNS"] == "keep-me", os.environ.get("PCBC_STRICT_PATTERNS")
    assert "PCBC_STRICT_POWER" not in os.environ


def test_build_with_no_power_move_says_nothing_about_power(monkeypatch, capsys):
    monkeypatch.setattr("pcbc.cli.build_job", lambda *a, **k: {"ok": True, "error": None})
    assert main(["build", str(BLINKY)]) == 0
    assert "power" not in capsys.readouterr().err


def test_strict_power_reads_the_same_spellings_every_other_switch_does(monkeypatch, tmp_path: Path):
    """`PCBC_STRICT_PATTERNS` has always accepted 1/true/yes/on (`patterns.hard_refusals`), and the
    first version of this one matched `"1"` exactly — so `PCBC_STRICT_POWER=true` was a silent
    no-op. A silent no-op on a strictness flag is the worst kind: the author believes the gate is
    armed and the build says nothing."""
    from pcbc.fab import _ON

    assert _ON == ("1", "true", "yes", "on")
    from pcbc.patterns import hard_refusals  # noqa: F401  — the spelling this one is copied from

    import inspect

    from pcbc import fab, patterns

    assert 'os.environ.get("PCBC_STRICT_POWER", "").lower() in _ON' in inspect.getsource(fab.fab_job)
    assert '("1", "true", "yes", "on")' in inspect.getsource(patterns.hard_refusals)


@pytest.mark.parametrize(
    "old, new, want",
    [
        ('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", ampz=1)', "NetReq(\"FB\") line 44: unexpected keyword 'ampz'; it takes kind="),
        ('Board(layers=2, stackup="jlcpcb_2l_1oz")', 'Board(layers=2, stackup="jlcpcb_2l_1oz", amps=1)', "Board() line 36: unexpected keyword 'amps'; it takes "),
        # Round 3 (refute-tests): every pcbc statement, not only the requirement constructors.
        ('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")', 'Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")\nCapacitor("C9", "1uF", p1=VIN, p2=GND, packge="0603")',
         "Capacitor(\"C9\") line 53: unexpected keyword 'packge'; it takes ref=, value=, package=, mpn=, lcsc=, manufacturer=, p1=, p2=, P1=, P2="),
        ('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")', 'Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")\nResistor("R9", "1k", p1=VIN, p2=GND, pakage="0603")', "Resistor(\"R9\") line 53: unexpected keyword 'pakage'; it takes ref="),
        ('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")', 'Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")\nPower("X5", volts=5)', "Power(\"X5\") line 53: unexpected keyword 'volts'; it takes name="),
    ],
)
def test_build_states_a_bad_keyword_with_its_line_and_no_guess(tmp_path: Path, old: str, new: str, want: str):
    """Postfix B4 (refutation nosuggest r2): `pcbc build` passed Python 3.13's own "Did you mean 'amps'?" through for an
    unknown keyword (build_job caught only ValueError). The NetReq case is a facts-only refusal on the build path now;
    a keyword on Board() still raised (Board was not in `_CONSTRUCTORS`)."""
    shutil.copytree(EXAMPLES / "buck", tmp_path / "buck", ignore=shutil.ignore_patterns("layout"))
    board = tmp_path / "buck" / "buck.py"
    text = board.read_text()
    assert old in text
    write_split(board, text.replace(old, new))
    result = build_job(board, upto="check", force=True)
    assert result["error"].startswith(want) and "Did you mean" not in result["error"], result["error"]
    (fail,) = check_board(board)
    assert fail == result["error"], (fail, result["error"])
