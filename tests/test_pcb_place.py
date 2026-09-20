"""Copper placement by relation: the AI says what a part belongs to, the tool puts it there."""

from __future__ import annotations

import shutil
from pathlib import Path


from pcbc.build import pcb_job
from pcbc.cli import main
from pcbc.compile import compile_design
from pcbc.language import check_board, load_board
from pcbc.pcb_place import Foot, Pad, _face, _farads, _rot_for_face, resolve_places

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
C3_USB = EXAMPLES / "c3_usb" / "c3_usb.py"
BLINKY = EXAMPLES / "blinky" / "blinky.py"


def _c3(tmp_path: Path, place_block: str) -> Path:
    """c3_usb with its Place() lines replaced."""
    src = C3_USB.read_text()
    head = src[: src.index('Place("J1"')]
    tail = src[src.index('Place("D1"') :]
    tail = tail[tail.index("\n") + 1 :]
    tmp_path.mkdir(parents=True, exist_ok=True)
    board = tmp_path / "c3_usb.py"
    board.write_text(head + place_block + tail)
    shutil.copytree(C3_USB.parent / "components", tmp_path / "components")
    return board


ANCHORS = '''Place("J1", edge="bottom")
Place("U1", position="absolute", left=1, top=1, rotate=90)
Keepout("ANTENNA", position="absolute", left=0, top=0, width=4, height=15)
Place("U2", position="absolute", right=4, top=10)
'''
REST = "".join(
    f'Place("{r}", to="{t}")\n'
    for r, t in [
        ("U3", "J1.DP1"), ("R_CC1", "J1.CC1"), ("R_CC2", "J1.CC2"), ("C_VBUS_HF", "U2.VIN"), ("C_VBUS", "U2.VIN"),
        ("C_3V3_HF", "U2.VOUT"), ("C_3V3", "U2.VOUT"), ("C_MCU_HF", "U1.3V3"), ("C_MCU", "U1.3V3"), ("R_EN", "U1.EN"), ("C_EN", "U1.EN"),
        ("SW_RST", "U1.EN"), ("R_BOOT", "U1.IO9"), ("SW_BOOT", "U1.IO9"), ("R_LED", "U1.IO10"), ("D1", "R_LED.2"),
    ]
)


def _placed(board: Path) -> tuple[dict, dict]:
    result = pcb_job(board)
    assert result.get("error") is None, result
    return result, result["poses"]


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def test_validate_names_the_mistake(tmp_path: Path):
    board = _c3(tmp_path, ANCHORS + 'Place("R_EN", to="U2.EN")\nPlace("C_EN", to="U9.EN")\nPlace("C_MCU", to="U1.NOPE")\nPlace("C_MCU_HF", to="U1.3V3", toward="sideways")\n')
    fails = check_board(board)
    text = "\n".join(fails)
    assert "R_EN has no pin on net VBUS" in text
    assert "no part U9" in text
    assert "U1 has no pin NOPE" in text
    assert "toward must be one of" in text


def test_to_puts_the_pad_next_to_the_pin_outside_the_target(tmp_path: Path):
    board = _c3(tmp_path, ANCHORS + 'Place("C_3V3_HF", to="U2.VOUT")\n' + "".join(l + "\n" for l in REST.splitlines() if "C_3V3_HF" not in l))
    result, poses = _placed(board)
    design = load_board(board)
    job = compile_design(design)
    places, moves, _fids = resolve_places(design, job, Path(result["placed"]).read_text())
    assert moves == []
    from pcbc.layout import footprints_by_ref
    from pcbc.pcb_place import parse_foot

    blocks = footprints_by_ref(Path(result["placed"]).read_text())
    u2 = parse_foot("U2", blocks["U2"])
    u2.at, u2.rot = tuple(poses["U2"]["at"]), poses["U2"]["rot"]
    cap = parse_foot("C_3V3_HF", blocks["C_3V3_HF"])
    cap.at, cap.rot = tuple(poses["C_3V3_HF"]["at"]), poses["C_3V3_HF"]["rot"]
    vout = next(p for p in u2.pads if p.net == "3V3")
    cpad = next(p for p in cap.pads if p.net == "3V3")
    assert _dist(u2.pad_world(vout), cap.pad_world(cpad)) < 2.5
    ub, cb = u2.world_box(), cap.world_box()
    assert cb[0] >= ub[2] or cb[2] <= ub[0] or cb[1] >= ub[3] or cb[3] <= ub[1], "the cap sits inside U2's keepout"
    assert result["layout_report"] == []


def _pad_to_pin(result: dict, ref: str, target: str, net: str) -> float:
    """Pad-to-pad distance on `net` between a placed part and its target, from the placed board."""
    from pcbc.layout import footprints_by_ref
    from pcbc.pcb_place import parse_foot
    from pcbc.sexp import footprint_at

    blocks = footprints_by_ref(Path(result["placed"]).read_text())
    feet = {}
    for r in (ref, target):
        f = parse_foot(r, blocks[r])
        at = footprint_at(blocks[r])
        f.at, f.rot = (at[0], at[1]), at[2]
        feet[r] = f
    a = feet[ref].pad_world(next(p for p in feet[ref].pads if p.net == net))
    b = feet[target].pad_world(next(p for p in feet[target].pads if p.net == net))
    return _dist(a, b)


def test_two_parts_on_one_pin_share_it_in_file_order(tmp_path: Path):
    """The first Place() written gets the closest spot (pad to pin), whichever cap it is."""
    board = _c3(tmp_path, ANCHORS + REST)
    result, _poses = _placed(board)
    assert result["layout_report"] == [], result["layout_report"]
    assert _pad_to_pin(result, "C_MCU_HF", "U1", "3V3") < _pad_to_pin(result, "C_MCU", "U1", "3V3")
    swapped = _c3(tmp_path / "swapped", ANCHORS + REST.replace('Place("C_MCU_HF", to="U1.3V3")\nPlace("C_MCU", to="U1.3V3")\n', 'Place("C_MCU", to="U1.3V3")\nPlace("C_MCU_HF", to="U1.3V3")\n'))
    r2, _p2 = _placed(swapped)
    assert _pad_to_pin(r2, "C_MCU", "U1", "3V3") < _pad_to_pin(r2, "C_MCU_HF", "U1", "3V3")


def test_a_closed_pad_row_gets_a_fanout_lane():
    """A TSSOP's 0.65 mm pads leave 0.31 mm between them: the thinnest track (0.127 + 2 x 0.127)
    cannot pass, so every pad escapes straight out and needs room for a via there. The DS2
    Addon's decaps sat 0.48 mm above the row and its AVDD pad had no path on any layer."""
    from pcbc.stackup import fanout_lane, fanout_stagger, get_stackup

    stack = get_stackup("jlcpcb_2l_1oz")
    # Two neighbouring vias on a 0.65 mm row: their holes need 0.3 + 0.5 centre to centre.
    assert fanout_stagger(stack, 0.2, 0.65) == round((0.8**2 - 0.65**2) ** 0.5, 4) == 0.4664
    assert fanout_stagger(stack, 0.2, 1.0) == 0.0  # a coarse row: one row of vias
    lane = fanout_lane(stack, 0.2, 0.65)
    assert lane == round(0.127 + 0.5 + 0.2 + 0.4664, 4) == 1.2934  # keep-off, via, clearance, stagger
    pads = [Pad(str(i + 1), -2.275 + 0.65 * i, 2.87, 0.343, 1.731, "") for i in range(8)]
    pads += [Pad(str(16 - i), -2.275 + 0.65 * i, -2.87, 0.343, 1.731, "") for i in range(8)]
    ic = Foot("U1", (-2.5, -3.74, 2.5, 3.74), pads)
    ic.lane(stack, 0.2)
    assert ic.closed == frozenset(str(i) for i in range(1, 17))
    assert ic.lane_mm == lane and ic.lanes == {"top": lane, "bottom": lane}
    assert ic.escape["1"] == "bottom" and ic.escape["16"] == "top"
    assert ic.keep == (-2.5, round(-2.87 - 1.731 / 2 - lane, 4), 2.5, round(2.87 + 1.731 / 2 + lane, 4))
    header = Foot("J1", (-2.54, -1.25, 2.54, 1.25), [Pad("1", -1.27, 0, 1.7, 1.7, ""), Pad("2", 1.27, 0, 1.7, 1.7, "")])
    header.lane(stack, 0.2)
    assert header.closed == frozenset() and header.keep is None and header.lane_mm == 0.0
    assert header.world_keep(at=(10, 10)) == header.world_box(at=(10, 10))
    cap = Foot("C1", (-1.48, -0.73, 1.48, 0.73), [Pad("1", -0.775, 0, 0.9, 0.95, ""), Pad("2", 0.775, 0, 0.9, 0.95, "")])
    cap.lane(stack, 0.2)
    assert cap.closed == frozenset()  # two pads with a free side each are not a row


def test_a_part_placed_on_a_closed_row_keeps_out_of_the_lane(tmp_path: Path):
    """U2 (SOT-23-5) has three 0.95 mm pads on one side; the cap on its VIN pad stops past the lane."""
    board = _c3(tmp_path, ANCHORS + REST)
    result, poses = _placed(board)
    assert result["layout_report"] == [], result["layout_report"]
    from pcbc.layout import footprints_by_ref
    from pcbc.pcb_place import lane_rules, parse_foot

    design = load_board(board)
    job = compile_design(design)
    blocks = footprints_by_ref(Path(result["placed"]).read_text())
    u2 = parse_foot("U2", blocks["U2"])
    u2.at, u2.rot = tuple(poses["U2"]["at"]), poses["U2"]["rot"]
    u2.lane(*lane_rules(job))
    assert "1" in u2.closed and u2.keep != u2.box
    cap = parse_foot("C_VBUS_HF", blocks["C_VBUS_HF"])
    cap.at, cap.rot = tuple(poses["C_VBUS_HF"]["at"]), poses["C_VBUS_HF"]["rot"]
    kb, cb = u2.world_keep(), cap.world_box()
    assert cb[0] >= kb[2] - 1e-6 or cb[2] <= kb[0] + 1e-6 or cb[1] >= kb[3] - 1e-6 or cb[3] <= kb[1] + 1e-6, "the cap sits in U2's fanout lane"
    # And it sits straight out from the row, on the side the pad escapes to, not beside the row.
    side = u2.escape["1"]
    from pcbc.pcb_place import _EDGE_OUT, _rotate

    ox, oy = _rotate(*_EDGE_OUT[side], u2.rot)
    past = {(1.0, 0.0): cb[0] >= kb[2] - 1e-6, (-1.0, 0.0): cb[2] <= kb[0] + 1e-6, (0.0, 1.0): cb[1] >= kb[3] - 1e-6, (0.0, -1.0): cb[3] <= kb[1] + 1e-6}
    assert past[(round(ox), round(oy))], f"the cap is beside U2's closed row, not out through its lane ({side})"


def test_toward_overrides_the_side(tmp_path: Path):
    left = _c3(tmp_path / "a", ANCHORS + 'Place("C_VBUS", to="U2.VIN", toward="left")\n' + "".join(l + "\n" for l in REST.splitlines() if "C_VBUS\"" not in l))
    up = _c3(tmp_path / "b", ANCHORS + 'Place("C_VBUS", to="U2.VIN", toward="up")\n' + "".join(l + "\n" for l in REST.splitlines() if "C_VBUS\"" not in l))
    _r1, p1 = _placed(left)
    _r2, p2 = _placed(up)
    u2 = p1["U2"]["at"]
    assert p1["C_VBUS"]["at"][0] < u2[0] and abs(p1["C_VBUS"]["at"][1] - u2[1]) < 3
    assert p2["C_VBUS"]["at"][1] < u2[1]


def test_edge_is_the_old_css_for_a_connector(tmp_path: Path):
    css = _c3(tmp_path / "css", 'Place("J1", position="absolute", left=0, right=0, bottom=0, margin_left="auto", margin_right="auto")\n' + ANCHORS.split("\n", 1)[1] + REST)
    edge = _c3(tmp_path / "edge", ANCHORS + REST)
    _r1, p1 = _placed(css)
    _r2, p2 = _placed(edge)
    assert p1["J1"] == p2["J1"]
    assert p2["J1"]["rot"] == 0.0
    right = _c3(tmp_path / "right", 'Place("J1", edge="right")\n' + ANCHORS.split("\n", 1)[1].replace("right=4, top=10", "left=22, top=2") + REST)
    _r3, p3 = _placed(right)
    assert p3["J1"]["rot"] in (90.0, 270.0) and p3["J1"]["at"][0] > 32


def test_face_and_rotation_for_edges():
    foot = Foot("J", (-4.0, -2.0, 4.0, 5.0), [Pad("1", -1, -2.5, 1, 1, ""), Pad("2", 1, -2.5, 1, 1, "")])
    assert _face(foot) == (0.0, 1.0)
    assert _rot_for_face((0.0, 1.0), (0.0, 1.0)) == 0.0
    assert _rot_for_face((0.0, 1.0), (0.0, -1.0)) == 180.0
    assert _rot_for_face((0.0, 1.0), (-1.0, 0.0)) in (90.0, 270.0)


def test_caps_sort_smallest_first():
    assert _farads("100nF") < _farads("4.7uF") < _farads("10uF")
    assert _farads("22pF") < _farads("100nF")
    assert _farads("1k") == float("inf") and _farads("") == float("inf")


def test_a_boxed_in_part_is_reported_not_hidden(tmp_path: Path):
    board = _c3(tmp_path, ANCHORS + 'Region("tiny", position="absolute", right=4, top=10, width=5, height=4)\n' + 'Place("C_VBUS", to="U2.VIN", parent="tiny")\n' + "".join(l + "\n" for l in REST.splitlines() if "C_VBUS\"" not in l))
    result = pcb_job(board)
    text = "\n".join(result["layout_report"])
    assert "C_VBUS: no clear spot next to U2.VIN" in text, result["layout_report"]


def test_report_reads_a_hand_placed_board(tmp_path: Path):
    board = _c3(
        tmp_path,
        'Place("J1", position="absolute", left=20, top=10)\n'
        + ANCHORS.split("\n", 1)[1]
        + 'Place("C_MCU", position="absolute", right=2, bottom=2)\n'
        + 'Place("C_MCU_HF", position="absolute", right=2, bottom=2)\n'
        + "".join(l + "\n" for l in REST.splitlines() if "C_MCU" not in l),
    )
    result = pcb_job(board)
    text = "\n".join(result["layout_report"])
    assert "C_MCU and C_MCU_HF overlap" in text
    assert "C_MCU is" in text and "belongs within" in text
    assert "J1 is" in text and "from the nearest board edge" in text


def test_the_same_board_places_the_same(tmp_path: Path):
    a = _c3(tmp_path / "a", ANCHORS + REST)
    b = _c3(tmp_path / "b", ANCHORS + REST)
    ra = pcb_job(a)
    rb = pcb_job(b)
    assert ra["poses"] == rb["poses"]
    assert Path(ra["placed"]).read_text() == Path(rb["placed"]).read_text().replace(str(tmp_path / "b"), str(tmp_path / "a"))


def test_c3_usb_layout_bar():
    result = pcb_job(C3_USB)
    assert result.get("error") is None, result
    assert result["layout_report"] == [], result["layout_report"]


def test_cli_pcb(capsys):
    assert main(["pcb", str(BLINKY)]) == 0
    out = capsys.readouterr().out
    assert "layout: nothing to move" in out
