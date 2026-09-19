"""The sensor node: 31 parts, six groups - an op-amp with vertical supply
pins, a sensor with tie-off pins bound straight to power nets, a MOSFET, a
divider, connectors. The patterns the first two boards never had."""

from __future__ import annotations

from pathlib import Path

from pcbc.language import check_board, load_board
from pcbc.sch_emit import _parts_from_design, emit_from_design
from pcbc.sch_place import apply_sch_places, pin_world

BOARD = Path(__file__).resolve().parent.parent / "examples" / "node" / "node.py"


def _placed():
    design = load_board(BOARD)
    parts = _parts_from_design(design, {n.name: n.kind for n in design.nets.values()})
    apply_sch_places(design, parts)
    return design, {p.ref: p for p in parts}


def _pin(part, key):
    return pin_world(part, next(p for p in part.pins if p.number == key or p.name == key))


def test_node_checks():
    assert check_board(BOARD) == []
    d = load_board(BOARD)
    assert len(d.instances) == 31 and len(d.nets) == 18


def test_cap_lies_sideways_off_a_vertical_supply_pin():
    _d, by = _placed()
    u5, c = by["U5"], by["C_OPA"]
    vplus = _pin(u5, "V+")
    node = _pin(c, "1")
    far = _pin(c, "2")
    assert abs(node[0] - vplus[0]) < 0.01 and node[1] < vplus[1]  # on a short stub up from V+
    assert abs(far[1] - node[1]) < 0.01 and far[0] != node[0]  # lying sideways, GND at the far end


def test_divider_and_pullups_stand_the_right_way_up():
    _d, by = _placed()
    for ref, top_net in (("R_NTC", "3V3"), ("R_SDA", "3V3"), ("R_SCL", "3V3"), ("R_EN", "3V3"), ("R_BOOT", "3V3")):
        part = by[ref]
        top = next(p for p in part.pins if p.net == top_net)
        other = next(p for p in part.pins if p is not top)
        assert pin_world(part, top)[1] < pin_world(part, other)[1], ref
    r_top, r_bot = by["R_NTC"], by["R_TDIV"]
    tap = _pin(r_top, "2")
    assert abs(_pin(r_bot, "1")[0] - tap[0]) < 0.01 and _pin(r_bot, "1")[1] > tap[1]


def test_load_group_reads_left_to_right():
    _d, by = _placed()
    r_g, r_pd, q1, j = by["R_G"], by["R_PD"], by["Q1"], by["J_LOAD"]
    gate = _pin(r_g, "2")
    assert abs(_pin(r_g, "1")[1] - gate[1]) < 0.01  # R_G lies along
    assert abs(_pin(r_pd, "1")[0] - gate[0]) < 0.01 and _pin(r_pd, "1")[1] > gate[1]  # R_PD hangs from the node
    g = _pin(q1, "G")
    assert abs(g[1] - gate[1]) < 0.01 and g[0] > gate[0]  # Q1's gate on the node row, to the right
    assert _pin(j, "2")[1] < _pin(q1, "D")[1]  # the load connector above the drain


def test_node_readability_bar():
    report: dict = {}
    emit_from_design(load_board(BOARD), title="node", report=report)
    library = [i for i in report["issues"] if ".kicad_sym" in i]
    rest = [i for i in report["issues"] if ".kicad_sym" not in i]
    assert len(library) == 1
    assert len(rest) <= 2, rest
