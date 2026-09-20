"""R1 slice S4 (docs/r1-design.md section F): what the placed board says about the copper before a
track exists. Each of F.1 to F.7 passes on a real example and fails, with its exact message, on a
modified copy. Every number pinned here was measured on the placed example (2026-09-20) and its
reference is in the assertion message; examples are copied into tmp_path, never built in place."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from pcbc.build import pcb_job
from pcbc.compile import CompiledClass, compile_design
from pcbc.constraints import compile_constraints
from pcbc.language import check_board, load_board
from pcbc.pcb_place import lane_rules
from pcbc.route_checks import _mst_mm, build_ctx, check_airwires, check_loops, route_aware_report

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
DS2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"

# ---------------------------------------------------------------- boards in tmp_path


def _copy(dst: Path, src: Path, edits: list[tuple[str, str]] = ()) -> Path:
    """An example (or the DS2 Addon) copied whole, minus its layout, with `edits` applied to the board;
    every old string must be there, so a drifted example fails loudly."""
    shutil.copytree(src.parent, dst, ignore=shutil.ignore_patterns("layout"))
    board = dst / src.name
    text = board.read_text()
    for old, new in edits:
        assert old in text, f"{src.name} no longer carries {old!r}"
        text = text.replace(old, new)
    board.write_text(text)
    return board


def _example(name: str) -> Path:
    return EXAMPLES / name / f"{name}.py"


def _ds2_src() -> Path:
    if not DS2.exists():
        pytest.skip(f"DS2 Addon not at {DS2}")
    return DS2 / "ds2_addon.py"


def _report(board: Path) -> tuple[list[str], list[str]]:
    r = pcb_job(board)
    assert r.get("check") == [], r.get("check")
    return r["layout_report"], r["layout_notes"]


def _placed(board: Path):
    """(design, job, ctx) of a board pcb_job has placed."""
    design = load_board(board)
    job = compile_design(design)
    text = (board.parent / "layout" / board.stem / "placed" / "layout.kicad_pcb").read_text()
    return design, job, build_ctx(design, job, text)


@pytest.fixture(scope="module")
def stock(tmp_path_factory) -> dict[str, tuple[Path, list[str], list[str]]]:
    """The examples as shipped, placed once: (board, moves, notes)."""
    root = tmp_path_factory.mktemp("stock")
    out: dict[str, tuple[Path, list[str], list[str]]] = {}
    for name in ("blinky", "buck", "c3_usb", "node"):
        board = _copy(root / name, _example(name))
        moves, notes = _report(board)
        out[name] = (board, moves, notes)
    if DS2.exists():
        board = _copy(root / "ds2_addon", DS2 / "ds2_addon.py")
        moves, notes = _report(board)
        out["ds2_addon"] = (board, moves, notes)
    return out


# ---------------------------------------------------------------- the five boards


def test_the_five_boards_add_no_route_aware_move(stock):
    """Acceptance: pcb_job on the five boards adds no new move. The intent lines the checks asked for
    are in the examples (c3_usb's and node's Chain lines; the DS2 Chain line is suggested in the PR)."""
    for name, (_board, moves, _notes) in stock.items():
        assert moves == [], (name, moves)


def test_lane_rules_read_the_kinds_clearance_not_the_voltage_row():
    """A.3 item 2: a 250 V class (clearance 1.25) never widens every closed row's fanout lane; the
    lane is sized from lane_clearance_mm (the kind's number), None reading as clearance_mm."""

    class Job:
        stackup = "jlcpcb_4l_1oz"
        classes = [CompiledClass("Default", 0.16, 0.18), CompiledClass("HV", 0.4, 1.25, lane_clearance_mm=0.2), CompiledClass("Old", 0.2, 0.25)]

    stack, clearance = lane_rules(Job())
    assert clearance == 0.25, "max over lane_clearance_mm (0.2 for HV, clearance_mm 0.25 for a class without one), never HV's 1.25"
    assert stack.name == "jlcpcb_4l_1oz"


# ---------------------------------------------------------------- F.1 airwires


def test_stock_c3_usb_pair_airwires_agree_within_the_skew_budget(stock):
    """F.1: the MST per member of the pair; the spread is under the 0.5 mm usb_hs skew budget."""
    board, moves, _notes = stock["c3_usb"]
    assert not any("airwires" in m for m in moves)
    _design, _job, ctx = _placed(board)
    dp = _mst_mm([(x, y) for _r, _n, x, y, _w, _h in ctx.pads_on["USB_DP"]])
    dn = _mst_mm([(x, y) for _r, _n, x, y, _w, _h in ctx.pads_on["USB_DN"]])
    assert (round(dp, 3), round(dn, 3)) == (24.175, 24.079), "c3_usb as placed: MST of the 5 pads of each member (J1 A6/B6, U3 1/6, U1 27; measured 2026-09-20)"
    assert round(dp - dn, 3) == 0.096, "spread 0.096 mm under the 0.5 mm budget (preset usb_hs, TI usb_layout_basics)"
    assert check_airwires(ctx) == []


def test_a_moved_esd_part_skews_the_pair_and_the_move_says_how_much_serpentine(tmp_path: Path):
    """F.1: c3_usb with U3 placed by CSS off the connector: the members' airwires differ by more
    than the budget; the router can only add length, so the message says the serpentine."""
    board = _copy(tmp_path / "c3", _example("c3_usb"), [('Place("U3", to="J1.DP1", reason="USB ESD at the connector")', 'Place("U3", position="absolute", left=30, top=18, rotate=90)')])
    moves, _notes = _report(board)
    assert (
        'USB_DN/USB_DP: airwires 36.6 and 34.5 mm differ by 2.1 mm; skew budget 0.5 mm (preset usb_hs): '
        'Place("U3", to="J1.A7") evens them, or the router adds 1.6 mm of serpentine'
    ) in moves, "measured on the copy: U3 at (30, 18) turned 90 puts its DN pads 2.1 mm farther along; 2.1 - 0.5 = 1.6 mm"


def test_the_whole_nets_airwire_is_held_to_max_mm(tmp_path: Path, stock):
    """F.1: the whole net's airwire is the MST over the pads' edge-to-edge gaps (a track ends at a
    pad's edge: buck's SW measures 5.1 mm that way, under its max_mm=6, and 8.7 mm centre to centre
    because the inductor's 2.4 x 3.6 mm pad centre sits 5.2 mm from U1.SW). With the boot cap moved
    off by CSS the whole net is 14.6 mm and a move, beside the existing nearest-pad line."""
    board = _copy(tmp_path / "buck", _example("buck"), [('Place("C_BOOT", to="U1.BOOT")', 'Place("C_BOOT", at=(30.0, 4.0))')])
    moves, _notes = _report(board)
    assert (
        'SW: 14.6 mm of airwire (MST of 3 pads, edge to edge) over max_mm=6 (NetReq line 56); the route is longer still: Place("C_BOOT", to="U1.SW") or raise max_mm'
    ) in moves, "buck with C_BOOT at (30, 4): U1.SW to L1.1 2.45 + U1.SW to C_BOOT.2 12.2 mm edge to edge (measured 2026-09-20)"
    assert any(m.startswith("SW: C_BOOT.2 is 14.7 mm from L1.1") for m in moves), "the existing nearest-pad line stays"
    assert not any("airwire" in m for m in stock["buck"][1]), "buck's SW as placed: 5.1 mm edge to edge under max_mm=6"


# ---------------------------------------------------------------- F.2 chains

DS2_POWER = 'NetReq("3V3", "GND", "VDDA", "VSS", kind="power", volts=3.3, amps=0.1)\n'
DS2_CHAIN = DS2_POWER + 'Chain("VDDA", "J2.1", "C4.1", "U1.12")\n'


def test_ds2_chain_passes_as_placed_and_a_cap_after_the_pin_is_named(tmp_path: Path):
    """F.2: with Chain("VDDA", "J2.1", "C4.1", "U1.12") the DS2 Addon passes as placed; with the cap
    told to sit right of the ADC it projects after the pin along the feed, and the move says so."""
    board = _copy(tmp_path / "ds2", _ds2_src(), [(DS2_POWER, DS2_CHAIN)])
    moves, _notes = _report(board)
    assert moves == [], moves
    board = _copy(tmp_path / "ds2_right", _ds2_src(), [(DS2_POWER, DS2_CHAIN), ('Place("C4", to="U1.AVDD")', 'Place("C4", to="U1.AVDD", toward="right")')])
    moves, _notes = _report(board)
    assert (
        'VDDA chain J2.1 -> C4.1 -> U1.12: C4.1 projects after U1.12 along the feed (t = 17.4 vs 14.3 mm); '
        'the cap must come first: Place("C4", to="U1.AVDD", toward="left")'
    ) in moves, "t along J2.1 -> U1.12 (14.3 mm long): C4.1 right of the ADC projects at 17.4 (measured 2026-09-20)"


def test_a_pad_in_the_chain_corridor_is_named(tmp_path: Path):
    """F.2: the corridor between consecutive chain pads (width + 2 clearances of the class) holds no
    other net's pad; R10 dropped by CSS between C4.1 and U1.12 is named with its net."""
    board = _copy(tmp_path / "ds2", _ds2_src(), [(DS2_POWER, DS2_CHAIN), ('Place("R10", to="U1.~{RESET}")', 'Place("R10", at=(20.6, 7.69))')])
    moves = pcb_job(board)["layout_report"]  # R10 also lands in U1's lane: the check step says so; the corridor line is what is pinned
    assert "VDDA chain: R10.2 (nRESET) lies in the corridor between C4.1 and U1.12; move R10" in moves


def test_a_third_pad_off_the_line_is_a_stub_until_the_chain_is_said(tmp_path: Path, stock):
    """F.2, implicit chain: a usb_hs net with pads on three parts and no Chain must have them on one
    line; c3_usb's ESD part sits off the J1 -> U1 line, so the message says the line to type."""
    board = _copy(tmp_path / "c3", _example("c3_usb"), [('Chain("USB_DP", "J1.A6", "U3.1", "U1.27")\nChain("USB_DN", "J1.A7", "U3.3", "U1.26")\n', "")])
    moves, _notes = _report(board)
    assert 'USB_DP has 3 pads (J1.A6, U3.1, U1.27): a third pad off the line is a stub; say the order: Chain("USB_DP", "J1.A6", "U3.1", "U1.27")' in moves
    assert not any("stub" in m for m in stock["c3_usb"][1]), "c3_usb.py carries the Chain lines"


# ---------------------------------------------------------------- F.3 corridors


def test_buck_wide_nets_have_their_channels_as_placed(stock):
    """F.3: buck's 0.781 mm VIN / 5V / GND class finds a 1.18 mm channel between MST-adjacent pads."""
    assert not any("channel" in m for m in stock["buck"][1])


def test_a_boxed_pad_names_the_pinch(tmp_path: Path):
    """F.3: C_IN1's VIN pad in the top-left corner with R_FB_BOT across the pocket's mouth, the net
    held to F.Cu: no 1.18 mm channel; the two obstacles across the line are the pinch."""
    board = _copy(
        tmp_path / "buck",
        _example("buck"),
        [
            ('Place("C_IN1", to="U1.VIN")', 'Place("C_IN1", at=(2.2, 1.9))'),
            ('Place("R_FB_BOT", to="U1.FB")', 'Place("R_FB_BOT", at=(1.9, 3.6))'),
            ('NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2)', 'NetReq("VIN", "5V", "GND", kind="power", volts=12, amps=2, layers=["F.Cu"])'),
        ],
    )
    moves, _notes = _report(board)
    assert (
        "VIN: no 0.78 mm channel from C_IN1.1 to J_IN.1 on F.Cu (0.781 width + 2 x 0.2 clearance = 1.18 mm): "
        'R_FB_BOT.1 and R_FB_BOT.2 pinch it to 0.48 mm; Place("R_FB_BOT", to="U1.FB", toward="up"); or NetReq("VIN", layers=["B.Cu"])'
    ) in moves, "0.781 = IPC-2221 2 A 10 C 1 oz; the 0402's pads are 0.48 mm apart edge to edge"


def test_the_corridor_check_stays_under_the_bar_on_node(stock):
    """Acceptance: node's pcb_job gets no more than 3 s slower; the route checks (four 0.4 mm nets,
    two outer layers, 0.1 mm grid over 60 x 45 mm) run in a fraction of that."""
    design, job, _ctx = _placed(stock["node"][0])
    text = (stock["node"][0].parent / "layout" / "node" / "placed" / "layout.kicad_pcb").read_text()
    t0 = time.perf_counter()
    moves, _notes = route_aware_report(design, job, text)
    assert time.perf_counter() - t0 < 3.0, "H.8: node under 3 s is the bar"
    assert moves == []


# ---------------------------------------------------------------- F.4 loops


def test_buck_hot_loop_is_measured_and_noted(stock):
    """F.4: Cin (C_IN1, the cap nearest the IC), U1.VIN, U1.GND (synchronous: the IC's own ground)
    enclose 3.3 mm2, under the 20 mm2 preset: a note, not a move."""
    _board, _moves, notes = stock["buck"]
    assert "SW: hot loop 3.3 mm2 (budget 20)" in notes, "shoelace of C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 as placed: 3.26 mm2 (2026-09-20)"


def test_a_far_input_cap_opens_the_hot_loop(tmp_path: Path):
    """F.4: C_IN1 12 mm below the IC (C_IN2 farther still, L1 held at its stock spot): the loop is
    1.9 x 12.04 = 22.9 mm2 over the 20 mm2 budget, and the move names the cap."""
    board = _copy(
        tmp_path / "buck",
        _example("buck"),
        [
            (
                'Place("C_IN1", to="U1.VIN")\nPlace("C_IN2", to="U1.VIN")\nPlace("L1", to="U1.SW")',
                'Place("C_IN1", at=(16.46, 22.0), rotate=180)\nPlace("C_IN2", at=(6.0, 20.0), rotate=180)\nPlace("L1", at=(22.84, 13.996), rotate=180)',
            )
        ],
    )
    moves, _notes = _report(board)
    assert (
        "SW: hot loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 encloses 22.9 mm2 over the 20 mm2 budget (preset switch_node, pcbc default): "
        'Place("C_IN1", to="U1.VIN") closes it, or NetReq("SW", kind="switch_node", loop_mm2=25) records it as intent'
    ) in moves, "U1's VIN and GND pads are 1.9 mm apart, the cap's pads 12.04 mm below them"


def test_decap_loops_under_the_budget_are_silent_and_over_it_are_style_notes(stock):
    """F.4: c3_usb's decaps measure 0.13 to 2.59 mm2 (under the 6 mm2 power default): nothing; the
    DS2 Addon's caps sit past the TSSOP's fanout lane and their loops are noted, never moved."""
    assert not any("decap loop" in n for n in stock["c3_usb"][2])
    _design, _job, ctx = _placed(stock["c3_usb"][0])
    assert check_loops(ctx) == ([], [])
    if "ds2_addon" in stock:
        _board, moves, notes = stock["ds2_addon"]
        assert (
            "VDDA: decap loop U1.AVDD -> C4.1 -> C4.2 -> U1.AVSS encloses 13.9 mm2 over the 6 mm2 budget (preset power, pcbc default): "
            'Place("C4", to="U1.AVDD") closes it'
        ) in notes, "DS2 as placed, 2026-09-20"
        assert not any("decap loop" in m for m in moves)


# ---------------------------------------------------------------- F.5 keep-away


def test_keep_away_from_the_switch_node_names_the_boot_cap(tmp_path: Path, stock):
    """F.5: with keep_clear_of="SW" on FB, R_FB_TOP.2 is 1.62 mm edge to edge from C_BOOT.2 (the
    IC's own FB and SW pins are its rating, not layout); stock buck says nothing and the constraint
    report carries the keep_clear_of none note."""
    board = _copy(tmp_path / "buck", _example("buck"), [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", keep_clear_of="SW")')])
    moves, _notes = _report(board)
    assert (
        "FB: R_FB_TOP.2 is 1.62 mm from C_BOOT.2 (SW); keep_clear_mm=3 (preset analog, NetReq line 57): "
        'Place("R_FB_TOP", to="U1.FB", toward="right"), or keep_clear_mm=1.5 if C_BOOT must sit there'
    ) in moves, "AABB gap of the 0402 pads as placed: dx 1.52, dy 0.57 -> 1.62 mm (measured 2026-09-20; the design pass wrote 1.84)"
    stock_board, stock_moves, _ = stock["buck"]
    assert not any(m.startswith("FB:") for m in stock_moves)
    assert 'FB: keep_clear_of none; NetReq("FB", kind="analog", keep_clear_of="SW") holds 3 mm' in compile_constraints(load_board(stock_board)).lines


# ---------------------------------------------------------------- F.6 reference plane

NODE_PLANES = 'planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")]'
NODE_ANTENNA = 'Keepout("ANTENNA", position="absolute", left=0, top=0, width=4, height=15)'


def test_a_pair_without_its_plane_is_a_compile_refusal(tmp_path: Path):
    """F.6 at compile: node with planes=[] wants In1.Cu declared before any geometry exists."""
    board = _copy(tmp_path / "node", _example("node"), [(NODE_PLANES, "planes=[]")])
    assert 'USB_DP: 90 ohm on F.Cu wants a plane on In1.Cu; Board(planes=[("GND", "In1.Cu")]) declares it' in check_board(board)


def test_a_keepout_under_a_pair_pad_is_a_move(tmp_path: Path):
    """F.6 at placement: the antenna keepout stretched over J1 leaves the pair's connector pads with
    no In1.Cu GND under them; the move names the pad and the Place() that clears it."""
    board = _copy(tmp_path / "node", _example("node"), [(NODE_ANTENNA, 'Keepout("ANTENNA", position="absolute", left=0, top=38, width=34, height=7)')])
    moves = pcb_job(board)["layout_report"]  # the keepout also boxes J1's ground and VBUS channels; those moves are the corridor check's
    assert 'USB_DP: J1.A6 sits in Keepout ANTENNA: no In1.Cu GND under it, the pair has no reference there; move the keepout or Place("J1", edge="bottom", left=34.2)' in moves
    assert 'USB_DN: J1.A7 sits in Keepout ANTENNA: no In1.Cu GND under it, the pair has no reference there; move the keepout or Place("J1", edge="bottom", left=34.2)' in moves


# ---------------------------------------------------------------- F.7 isolation

OPTO_MOD = """(footprint "SOP-4"
\t(version 20240108)
\t(generator "pcbc-test")
\t(layer "F.Cu")
\t(attr smd)
\t(fp_rect (start -5.2 -2.2) (end 5.2 2.2) (stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))
\t(fp_rect (start -2.2 -2.0) (end 2.2 2.0) (stroke (width 0.1) (type solid)) (fill no) (layer "F.Fab"))
\t(pad "1" smd rect (at -4 -1.27) (size 1.6 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
\t(pad "2" smd rect (at -4 1.27) (size 1.6 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
\t(pad "3" smd rect (at 4 1.27) (size 1.6 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
\t(pad "4" smd rect (at 4 -1.27) (size 1.6 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
)
"""
OPTO_SYM = """(kicad_symbol_lib
  (version 20211014)
  (generator pcbc-test)
  (symbol "OPTO"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (id 0) (at 0 7.62 0) (effects (font (size 1.27 1.27))))
    (property "Value" "OPTO" (id 1) (at 0 -7.62 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "SOP-4" (id 2) (at 0 -10.16 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "OPTO_0_1"
      (rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0) (type default)) (fill (type background)))
      (pin passive line (at -7.62 2.54 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at -7.62 -2.54 0) (length 2.54) (name "K" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 7.62 2.54 180) (length 2.54) (name "C" (effects (font (size 1.27 1.27)))) (number "3" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 7.62 -2.54 180) (length 2.54) (name "E" (effects (font (size 1.27 1.27)))) (number "4" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""
OPTO_PART = 'from pcbc import Component\n\npart = Component(name="OPTO", prefix="U", mpn="OPTO-TEST", manufacturer="pcbc", lcsc="C0", footprint="SOP-4.kicad_mod", symbol="OPTO.kicad_sym")\n'


def _isolation_board(tmp_path: Path, *, secondary_left: float = 21, u7_left: float = 14, r2_left: float = 27) -> Path:
    """Two Regions 40 x 25 mm apart by `21 - 18 = 3` mm (the default), an optocoupler U7 in across=
    straddling the gap, an RC on each side. A copper Place() with parent= is offset by its Region
    twice (resolve_places settles it, then apply re-resolves the settled spec against the Region;
    outside S4's files), so the secondary parts hang off a child Region back at the board origin."""
    lib = tmp_path / "components" / "OPTO"
    lib.mkdir(parents=True)
    (lib / "part.py").write_text(OPTO_PART)
    (lib / "SOP-4.kicad_mod").write_text(OPTO_MOD)
    (lib / "OPTO.kicad_sym").write_text(OPTO_SYM)
    board = tmp_path / "iso.py"
    board.write_text(
        f"""from pcbc import *
HVP = Power("HV+"); HVN = Ground("HV-")
V33 = Power("3V3"); GND = Ground("GND")
LED_A = Net("LED_A"); OUT = Net("OUT")
OPTO = load("components/OPTO")
Resistor("R1", "10k", package="0805", mpn="X", lcsc="C1", p1=HVP, p2=LED_A)
Capacitor("C1", "100nF", package="0805", mpn="X", lcsc="C1", p1=HVP, p2=HVN)
OPTO("U7", A=LED_A, K=HVN, C=OUT, E=GND)
Resistor("R2", "10k", package="0805", mpn="X", lcsc="C1", p1=V33, p2=OUT)
Capacitor("C2", "100nF", package="0805", mpn="X", lcsc="C1", p1=V33, p2=GND)
Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz")
Region("primary", position="absolute", left=0, top=0, width=18, height=25)
Region("secondary", position="absolute", left={secondary_left}, top=0, width=19, height=25)
Region("sec_box", parent="secondary", position="absolute", left=-{secondary_left}, top=0, width=40, height=25)
Place("R1", parent="primary", position="absolute", left=4, top=4)
Place("C1", parent="primary", position="absolute", left=4, top=10)
Place("R2", parent="sec_box", position="absolute", left={r2_left}, top=4)
Place("C2", parent="sec_box", position="absolute", left=27, top=10)
Place("U7", position="absolute", left={u7_left}, top=11)
Isolation("primary", "secondary", volts=250, across=("U7",))
NetReq("HV+", "HV-", kind="power", volts=250)
SchRegion("sheet", left=12, top=12, width=200, height=120)
SchPlace("U7", parent="sheet", left=80, top=40)
SchPlace("R1", to="U7.A")
SchPlace("C1", along="R1.1", side="bottom")
SchPlace("R2", to="U7.C")
SchPlace("C2", along="R2.1", side="bottom")
"""
    )
    return board


def test_isolation_regions_keep_the_creepage_gap_and_parts_stay_on_their_side(tmp_path: Path):
    """F.7: 3 mm apart at 250 V passes (2.5 mm creepage, IEC 60664-1 F.5 row 250 V IIIa PD2) and the
    ISO rule area is the strip between the Regions; 2 mm apart is a move naming the Region to shift;
    a part whose courtyard crosses the corridor is named; an across= part must span the gap."""
    board = _isolation_board(tmp_path / "pass")
    assert check_board(board) == []
    moves, _notes = _report(board)
    assert not any(m.startswith("Isolation") for m in moves), moves
    cs = compile_design(load_board(board)).constraints
    assert [(a.name, a.box) for a in cs.rule_areas] == [("ISO_primary_secondary", (18.0, 0.0, 21.0, 25.0))], "E.15: the corridor between the Regions, full board height (the zone itself is S3's apply)"

    moves, _notes = _report(_isolation_board(tmp_path / "gap2", secondary_left=20))
    assert (
        "Isolation primary/secondary (250 V): Regions are 2.0 mm apart; needs 2.5 mm creepage (IEC 60664-1 F.5 row 250 V IIIa PD2): "
        'Region("secondary", left=+0.5)'
    ) in moves, "C.8: creepage 2.5 mm at 250 V (H.5: the general PD2 column) governs over 1.5 mm clearance"

    moves, _notes = _report(_isolation_board(tmp_path / "side", r2_left=19.5))
    assert "Isolation primary/secondary: R2 (secondary) crosses the corridor: move R2" in moves

    moves = pcb_job(_isolation_board(tmp_path / "span", u7_left=4))["layout_report"]  # U7 at the far left also overlaps C1: reported by the check step
    assert 'Isolation primary/secondary: U7 is in across= but does not span the gap; Place("U7", parent="primary", right=-3.0)' in moves
