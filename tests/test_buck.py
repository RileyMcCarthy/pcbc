"""The buck: a second real board, with the patterns c3_usb never had - a cap
bridging two IC pins, a series inductor whose far end is a node others hang
off, a feedback divider, connectors at the ends of rails."""

from __future__ import annotations

from pathlib import Path


from pcbc.language import check_board, load_board
from pcbc.sch_emit import _parts_from_design, emit_from_design
from pcbc.sch_place import apply_sch_places, pin_world

BOARD = Path(__file__).resolve().parent.parent / "examples" / "buck" / "buck.py"


def _placed():
    design = load_board(BOARD)
    parts = _parts_from_design(design, {n.name: n.kind for n in design.nets.values()})
    apply_sch_places(design, parts)
    return design, {p.ref: p for p in parts}


def _pin(part, number):
    return pin_world(part, next(p for p in part.pins if p.number == number))


def test_buck_checks():
    assert check_board(BOARD) == []
    d = load_board(BOARD)
    assert len(d.instances) == 12 and len(d.nets) == 7


def test_hangers_take_the_engineers_pose():
    _d, by = _placed()
    u1, l1, c_in1, r_en, r_top, r_bot = (by[r] for r in ("U1", "L1", "C_IN1", "R_EN", "R_FB_TOP", "R_FB_BOT"))
    sw = _pin(u1, "2")
    # The inductor lies along the SW row; its near pin sits on that row.
    l1_sw = _pin(l1, "1")
    assert abs(l1_sw[1] - sw[1]) < 0.01 and l1_sw[0] < sw[0]
    # The input cap (to ground) hangs down from the VIN pin's column; its far pin is lower.
    c_vin, c_gnd = _pin(c_in1, "1"), _pin(c_in1, "2")
    assert abs(c_vin[0] - c_gnd[0]) < 0.01 and c_gnd[1] > c_vin[1]
    # The EN pull-up stands up to VIN: its VIN end is above its EN end.
    en_end, vin_end = _pin(r_en, "2"), _pin(r_en, "1")
    assert abs(en_end[0] - vin_end[0]) < 0.01 and vin_end[1] < en_end[1]
    assert abs(en_end[1] - _pin(u1, "5")[1]) < 0.01  # on the EN row
    # The divider: top resistor up to 5V, bottom one continuing below the tap.
    tap = _pin(r_top, "2")
    assert _pin(r_top, "1")[1] < tap[1]
    assert abs(_pin(r_bot, "1")[0] - tap[0]) < 0.01 and _pin(r_bot, "1")[1] > tap[1]
    assert _pin(r_bot, "2")[1] > _pin(r_bot, "1")[1]


def test_parallel_caps_share_the_rail():
    _d, by = _placed()
    c1, c2, l1, j_out = by["C_OUT1"], by["C_OUT2"], by["L1"], by["J_OUT"]
    rail_y = _pin(l1, "2")[1]
    for c in (c1, c2):
        top = _pin(c, "1")
        assert abs(top[1] - rail_y) < 0.01, c.ref  # both hang from the 5V rail
        assert _pin(c, "2")[1] > top[1]
    assert _pin(c2, "1")[0] < _pin(c1, "1")[0] < _pin(l1, "2")[0]  # in a row, away from the inductor
    assert _pin(j_out, "1")[0] < _pin(c2, "1")[0]  # the connector at the end of the rail
    assert abs(_pin(j_out, "1")[1] - rail_y) < 0.01


def test_a_taken_lane_is_reported(tmp_path: Path):
    import shutil

    src = BOARD.read_text()
    shutil.copytree(BOARD.parent / "components", tmp_path / "components")
    board = tmp_path / "buck.py"
    # Ask for the connector on the inductor's side of the cap: its wire would
    # run through the cap, and there is no distance at which that is not so.
    board.write_text(src.replace('SchPlace("J_OUT", to="C_OUT2.1", side="left")', 'SchPlace("J_OUT", to="C_OUT2.1", side="right")'))
    report: dict = {}
    emit_from_design(load_board(board), title="buck", report=report)
    lines = [i for i in report["issues"] if i.startswith("J_OUT")]
    assert lines, report["issues"]
    assert any("wire from C_OUT2.1 would run through" in i or "pushed" in i or "at every distance" in i for i in lines)


def test_buck_readability_bar():
    report: dict = {}
    emit_from_design(load_board(BOARD), title="buck", report=report)
    assert report["count"] <= 1, report["issues"]
