"""`Loop()` as a check on named pads (`docs/net-path-physics.md` §6, PR 7).

A declared loop replaces the guessed one for the parts it names; its area is the shoelace of its pad
centres in the order written (a lower bound: no vertical leg); over budget is a move naming the farthest
station and the attachment already written; under budget is a note that says it is a lower bound.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import edit_board
from pcbc.build import pcb_job
from pcbc.constraints import compile_constraints
from pcbc.language import load_board

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
LOOP = 'Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")'


def _copy(tmp_path: Path, name: str, edits=()) -> Path:
    work = tmp_path / name
    shutil.copytree(EXAMPLES / name, work, ignore=shutil.ignore_patterns("layout"))
    board = work / f"{name}.py"
    edit_board(board, edits)
    return board


def test_buck_declares_its_hot_loop(tmp_path: Path):
    board = _copy(tmp_path, "buck")
    assert LOOP in board.read_text().splitlines()[51], "Loop line 52 (§5)"
    cs = compile_constraints(load_board(board))
    (lp,) = cs.loops
    assert lp.written == ("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2") and lp.line == 52
    assert lp.steps == ("net VIN", "part U1", "net GND", "part C_IN1") and set(lp.parts) == {"C_IN1", "U1"}
    assert (lp.budget.value, lp.budget.source.formula, lp.budget.source.note) == (20.0, "pcbc default", "U1 carries SW")


def test_the_declared_loop_replaces_the_guess_and_its_pass_line_is_a_lower_bound(tmp_path: Path):
    r = pcb_job(_copy(tmp_path, "buck"))
    assert r.get("check") == [], r.get("check")
    notes = r["layout_notes"]
    assert f"loop {LOOP[5:-1].replace(chr(34), '').replace(', ', ' -> ')} (Loop line 52): pads enclose at least 3.7 mm2 of 20 (shoelace of pad centres; no vertical leg)" in notes, notes
    # The guess does not run for parts a Loop() names. Since decision 4 moved C_IN1 off U1.2's exit, the input cap
    # nearest U1 is C_IN2, which the Loop does not name, so the guess measures C_IN2's loop (0.9 mm2) and only that.
    assert [n for n in notes if n.startswith("SW: hot loop")] == ["SW: hot loop 0.9 mm2 (budget 20)"], notes
    assert not any(m.startswith("SW: hot loop") for m in r["layout_report"])


def test_a_far_input_cap_opens_the_declared_loop(tmp_path: Path):
    """The pinned fixture of `test_route_checks.py`, with the Loop() kept: the warning names the loop,
    the area, the budget, the farthest station and the Place() lines of that station's part and of its
    neighbour's (refutation round 2: it said "no Place() attaches" of a part a line did place)."""
    board = _copy(
        tmp_path,
        "buck",
        [('Place("C_IN1", to="U1.VIN")\nPlace("C_IN2", to="U1.VIN")\nPlace("L1", to="U1.SW")',
          'Place("C_IN1", at=(16.46, 22.0), rot=180)\nPlace("C_IN2", at=(6.0, 20.0), rot=180)\nPlace("L1", at=(22.84, 13.996), rot=180)')],
    )
    moves = pcb_job(board)["layout_report"]
    hit = [m for m in moves if m.startswith("loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52) encloses 22.9 mm2 over the 20 mm2 budget")]
    assert hit and hit[0].endswith("(layout.core.py:8 places C_IN1; layout.core.py:3 places U1)") and "Place(\"" not in hit[0], moves


def test_a_multi_pad_station_is_refused_with_its_pads(tmp_path: Path):
    board = _copy(tmp_path, "c3_usb", [('Thermal("U1.49", watts=0.35)', 'Thermal("U1.49", watts=0.35)\nLoop("C_MCU_HF.1", "U1.3V3", "U1.GND", "C_MCU_HF.2")')])
    cs = compile_constraints(load_board(board))
    hit = [r for r in cs.refusals if r.startswith("Loop line 219: U1.GND is ") and "a loop's station is one pad" in r]
    assert hit, cs.refusals


def test_one_attachment_one_statement(tmp_path: Path):
    """A loop step from a cap to another pin of the IC on the same net than the cap's Place(to=) names
    is refused: two statements about one attachment."""
    board = _copy(tmp_path, "buck", [(LOOP, 'Loop("C_IN1.1", "U1.EN", "U1.GND", "C_IN1.2")')])
    cs = compile_constraints(load_board(board))
    assert not any("one attachment, one statement" in r for r in cs.refusals), "U1.EN is not on VIN: a loop step across nets is refused first"
    assert any(r.startswith("Loop line 52: C_IN1.1 -> U1.EN is neither one net") for r in cs.refusals), cs.refusals


def test_a_netreq_loop_budget_is_refused_e7(tmp_path: Path):
    """E7 (`docs/physics-subjects.md` PR 7): a loop runs through two nets and two parts at least, so its budget
    is a Loop's (`Loop(mm2=)`), never a NetReq's; one loop can no longer have two budgets."""
    from pcbc.language import check_board

    board = _copy(tmp_path, "buck", [('NetReq("SW", kind="switch_node", max_mm=6)', 'NetReq("SW", kind="switch_node", max_mm=6, loop_mm2=25)')])
    assert check_board(board) == [
        'NetReq("SW") line 43: loop_mm2= budgets the area of a loop, which runs through two nets and two parts at least; it is a Loop field'
    ]


def test_a_written_budget_is_the_one_judged(tmp_path: Path):
    """`Loop(mm2=)` is the budget (refutation round 1, defect 11: disabling it failed no test). The
    stock buck's loop encloses 3.7 mm2 (3.3 before decision 4 moved C_IN1 off U1.2's exit): under the 20 mm2 default a note, over a written 2 mm2 a move
    that cites the Loop line as the budget's source."""
    board = _copy(tmp_path, "buck", [(LOOP, LOOP[:-1] + ", mm2=2)")])
    cs = compile_constraints(load_board(board))
    (lp,) = cs.loops
    assert (lp.budget.value, lp.budget.source.formula, lp.budget.source.ref) == (2.0, "Loop", "line 52")
    r = pcb_job(board)
    hit = [m for m in r["layout_report"] if m.startswith("loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52) encloses 3.7 mm2 over the 2 mm2 budget (Loop line 52)")]
    assert hit, r["layout_report"]
    assert not any("pads enclose at least" in n for n in r["layout_notes"]), "over budget is a move, not a pass note"
