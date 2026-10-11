"""R1 slice S4 (docs/r1-design.md section F): what the placed board says about the copper before a
track exists. Each of F.1 to F.7 passes on a real example and fails, with its exact message, on a
modified copy. Every number pinned here was measured on the placed example (2026-09-20) and its
reference is in the assertion message; examples are copied into tmp_path, never built in place."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from conftest import declared_examples, edit_board, write_split
from pcbc.build import pcb_job
from pcbc.compile import CompiledClass, compile_design
from pcbc.constraints import compile_constraints
from pcbc.language import check_board, load_board
from pcbc.pcb_place import lane_rules
from boardtext import feet_of_text
from pcbc.route_checks import _mst_mm, build_ctx, check_airwires, check_loops, route_aware_report
from conftest import ds2_dir

EXAMPLES = declared_examples()  # the examples as shipped with a declared outline (vehicles): conftest.declared_examples
DS2 = ds2_dir()

# ---------------------------------------------------------------- boards in tmp_path


def _copy(dst: Path, src: Path, edits: list[tuple[str, str]] = ()) -> Path:
    """An example (or the DS2 Addon) copied whole, minus its layout, with `edits` applied to the board;
    every old string must be there, so a drifted example fails loudly."""
    shutil.copytree(src.parent, dst, ignore=shutil.ignore_patterns("layout"))
    board = dst / src.name
    edit_board(board, edits, every=True)
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
    # Generated, not read: `examples/**/layout/` is gitignored build output (`conftest.placed_board`).
    from conftest import placed_board

    from boardtext import with_outline

    text = placed_board(board.stem, board).read_text()
    with_outline(design, job)
    return design, job, build_ctx(design, job, feet_of_text(design, text))


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
    """Acceptance: pcb_job on the five boards adds no new move — except the ones F.8 and F.1 found.

    The intent lines the checks asked for are in the examples (c3_usb's and node's Path lines; the
    DS2 Path line is suggested in the PR). **The DS2 Addon gained exactly one move when F.8 landed**
    (`docs/stitch-plan.md` S3): `GND` and `VSS` are both `Ground()`, nothing ties them, and their
    pads overlap on all four separating axes, so there is no line to draw between them either.

    **c3_usb and node gained two each on 2026-09-22, and they are findings on real boards.**
    `route_checks.FAST_KINDS` records why no preset number was invented for `usb_hs`; the
    consequence is that the two boards that declare it declare **no length budget of any kind**, and
    until now nothing said so. Measured on fresh placements, `_unbudgeted_fast` reports c3_usb's
    `USB_DP`/`USB_DN` at 20.747 / 20.645 mm of pad-to-pad MST on a 50.000 mm board diagonal and
    node's at 39.124 / 38.971 mm on a 75.000 mm one — node's pair spans **52 %** of its board's
    diagonal, over five pads, with nothing checking it. That span is what the review of node's
    routing came back to: a two-segment elbow on `USB_DN`, reported at 38.84 mm against a 30.62 mm
    best octilinear path for the same two segments and the same one corner. blinky, buck and ds2
    declare no fast kind and stay where they were.

    Pinned as exactly these counts and prefixes, so a third move on any board fails this test rather
    than hiding behind it; `tests/test_bridge.py` holds F.8's text and the F.1 texts are below.

    **buck gained two on 2026-09-30, and they are the migrated example's finding** (docs/net-path-physics.md
    §5): the switch current now rides on `Path("SW", "U1.SW", "L1.1", amps=2)` at 0.781 mm on F.Cu with no
    vias, and as placed C_IN1 (attached to U1.VIN) sits 0.455 mm under U1's pin row with 0.9 mm between its
    pads: F.2 names C_IN1.1 in the link's corridor and F.3 names the 0.46 mm pinch. The router then leaves SW
    unrouted with the same move. Before the migration the 0.3 mm SW copper carried the 2 A at about twice its
    rating and nothing said so.

    **2026-10-01, the owner's decisions 2 and 4.** buck is down to one: the switch node may change layer
    (decision 2), so F.3's "SW may use F.Cu only" pinch is no move any more; F.2's corridor sentence stays,
    because C_IN1, moved off U1.2's exit corridor to (17.91, 12.396), still lies between U1.SW and L1.1. ds2
    gained one, and it is decision 4's cost on a real board: C5 (attached to U1.DVDD, placed before C4) now keeps
    U1.12's exit corridor clear and takes the spot C4 had, so C4.1 projects 1.4 mm past U1.12 along VDDA's feed.
    **2026-10-02**: the corridor runs only as far as the path needs to turn (refuter round, defect 4); ds2 is placed as
    at the checkpoint and is back to one.
    """
    # **2026-10-02, docs/physics-subjects.md §8 (PR 3).** The currents left the NetReqs for Paths, and two Paths are
    # new findings on placement as it stands: buck's VIN Path (line 69) is written J_IN.1 -> C_IN2.1 -> C_IN1.1 ->
    # U1.VIN, and C_IN2.1 projects after C_IN1.1 along that feed; node's VBUS Path (line 109) has R_CC1.2 [GND] in
    # its corridor. Both are facts about the declared placement, stated without a proposal.
    expect = {"blinky": 0, "buck": 2, "c3_usb": 2, "node": 3, "ds2_addon": 1}
    for name, (_board, moves, _notes) in stock.items():
        assert len(moves) == expect[name], (name, moves)
        for m in moves:
            assert m.startswith("GND/VSS: no axis separates the two grounds") or (
                m.startswith(("USB_DP:", "USB_DN:")) and "no budget checks it" in m
            ) or (name == "buck" and m in (
                "SW path (Path line 46): C_IN1.1 (VIN) lies in the corridor between U1.SW and L1.1 (layout.core.py:8 attaches C_IN1 to U1.VIN)",
                "Path line 53: C_IN2.1 projects after C_IN1.1 along the feed (t = 16.6 vs 15.4 mm), and the stations are fed in the order Path line 53 writes "
                "them (layout.core.py:9 attaches C_IN2 to U1.VIN; layout.core.py:8 attaches C_IN1 to U1.VIN)",
            )) or (name == "node" and m == "VBUS path (Path line 66): R_CC1.2 (GND) lies in the corridor between J1.VBUS and U2.VIN (layout.core.py:14 attaches R_CC1 to J1.CC1)"), (name, m)


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
    assert (round(dp, 3), round(dn, 3)) == (24.175, 24.079), "c3_usb as placed: MST of the 5 pads of each member (J1 A6/B6, U3 1/6, U1 27; measured 2026-09-20; 24.398 / 24.217 while decision 4's corridor ran to the board edge, 2026-10-01, and U3 back in place since 2026-10-02)"
    assert round(dp - dn, 3) == 0.096, "spread 0.096 mm under the 0.5 mm budget (preset usb_hs, TI usb_layout_basics)"
    assert not any("airwires" in m for m in check_airwires(ctx)), "the skew half stays silent"
    # F.1's other half does not: the pair has no length budget at all, and 0.096 mm of MST spread is
    # not evidence that it needs none. node's routed skew is 11.9196 mm against 0.5 on a board whose
    # MST spread is 0.094 mm (docs/roadmap.md item 6), so this check is not the one that catches it.
    assert [m.split(":")[0] for m in check_airwires(ctx)] == ["USB_DN", "USB_DP"]


def test_a_moved_esd_part_skews_the_pair_and_the_move_says_how_much_serpentine(tmp_path: Path):
    """F.1: c3_usb with U3 placed by CSS off the connector: the members' airwires differ by more
    than the budget; the router can only add length, so the message says the serpentine."""
    board = _copy(tmp_path / "c3", _example("c3_usb"), [('Place("U3", to="J1.DP1", reason="USB ESD at the connector")', 'Place("U3", position="absolute", left=30, top=18, rot=90)')])
    moves, _notes = _report(board)
    assert (
        "USB_DN/USB_DP: airwires 36.6 and 34.5 mm differ by 2.1 mm, over the skew budget 0.5 mm (preset usb_hs)"
    ) in moves, "measured on the copy: U3 at (30, 18) turned 90 puts its DN pads 2.1 mm farther along; the warning states the spread and the budget, and no edit (docs/direction.md §7b)"


def test_the_whole_nets_airwire_is_held_to_max_mm(tmp_path: Path, stock):
    """F.1: the whole net's airwire is the MST over the pads' edge-to-edge gaps (a track ends at a
    pad's edge: buck's SW measures 5.1 mm that way, under its max_mm=6, and 8.7 mm centre to centre
    because the inductor's 2.4 x 3.6 mm pad centre sits 5.2 mm from U1.SW). With the boot cap moved
    off by CSS the whole net is 14.6 mm and a move, beside the existing nearest-pad line."""
    board = _copy(tmp_path / "buck", _example("buck"), [('Place("C_BOOT", to="U1.BOOT")', 'Place("C_BOOT", at=(30.0, 4.0))')])
    moves, _notes = _report(board)
    assert (
        "SW: 14.6 mm of airwire (MST of 3 pads, edge to edge) over max_mm=6 (NetReq line 43); the route is longer still; "
        "C_BOOT is the pad farthest from the rest (layout.core.py:13 places C_BOOT); U1.SW is the nearest IC pin on SW"
    ) in moves, "buck with C_BOOT at (30, 4): U1.SW to L1.1 2.45 + U1.SW to C_BOOT.2 12.2 mm edge to edge (measured 2026-09-20)"
    assert any(m.startswith("SW: C_BOOT.2 is 14.2 mm from L1.1") for m in moves), "the existing nearest-pad line stays (14.7 mm until decision 4 moved C_IN1 and L1 with it)"
    assert not any("airwire" in m for m in stock["buck"][1]), "buck's SW as placed: 5.1 mm edge to edge under max_mm=6"


def test_a_fast_kind_with_no_budget_is_a_move_naming_the_span_nobody_checked(stock):
    """F.1: `PRESETS` gives `usb_hs`, `clock`, `spi` and `i2c` no `airwire_mm` — see
    `route_checks.FAST_KINDS` for the two derivations that were weighed and refuted — so the
    kind with the fastest edge on these boards was the one kind no placement budget ever saw.
    Refusing to invent the number is half a refusal; this is the other half, with the measurement.

    node as shipped: five pads per member, 38.971 and 39.124 mm of edge-to-edge MST on a 75.000 mm
    board diagonal, and until this landed the router was handed that span with nothing said.
    """
    _board, moves, _notes = stock["node"]
    # docs/physics-subjects.md E21 (PR 5, PR 6): the line names the subjects that may carry a length budget, as a fact:
    # the member's Path, and the Pair line that may write one length for both members.
    assert (
        "USB_DP: 39.1 mm of airwire (MST of 5 pads, edge to edge) and no budget checks it: no length_mm= on Path line 70 or Pair line 62"
    ) in moves, "node as placed, measured 2026-09-22: USB_DP 39.124 mm, USB_DN 38.971 mm"
    assert sum("no budget checks it" in m for m in moves) == 2, moves
    for name in ("blinky", "buck"):
        assert not any("no budget checks it" in m for m in stock[name][1]), f"{name} declares no fast kind"


def test_declaring_the_budget_turns_the_move_into_the_ordinary_over_budget_one(tmp_path: Path):
    """F.1: the move ends in a `board.py` edit, and making that edit is what the check is for. node's
    pair with `length_mm=30` on its Pair line (docs/physics-subjects.md PR 6: a pair's numbers are its
    Pair's, and `max_mm=` left it with the NetReq) stops printing "no budget checks it" and prints the
    over-budget line of each member Path, citing the Pair line the 30 mm is written on."""
    src = _example("node")
    line = 'Pair("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90)'
    board = _copy(tmp_path / "node", src, [(line, line[:-1] + ", length_mm=30)")])
    moves, _notes = _report(board)
    assert not any("no budget checks it" in m for m in moves)
    assert (
        "USB_DP path J1.B6 -> U3.6 -> U1.27 (Path line 70): 39.5 mm between stations, over length_mm=30 on Pair line 62; "
        "0.2288 mm wide on F.Cu, clearance 0.18 mm; U1.27 is the farthest station (layout.core.py:8 places U1)"
    ) in moves, "node with length_mm=30 on the pair, measured 2026-10-02"


def test_a_derived_routed_length_is_a_placement_budget_one_stage_early(tmp_path: Path):
    """F.1: `_airwire_budget`'s second source. The MST over a net's pads is a **lower bound** on the
    copper that joins them, so a placement already over the net's *routed* budget cannot be routed
    inside it by any router — which makes `length_max_mm` a placement budget for free, with no new
    number anywhere.

    `i2c` is the kind where that budget is already fully derived: `(pf_max - 10 pF x pins) / C_per_mm`
    from UM10204 rev 7 section 7.1 and the exact per-mm capacitance of the class width on the board's
    own stackup. Measured on node + an `i2c` NetReq, 2026-09-22: `SDA` spans 38.474 mm edge to edge,
    and the derived budget is **4067.1441 mm** at the UM10204 default 400 pF, **109.9228** at 40 pF
    and **32.9768** at 33 pF (3 device pins at 10 pF each, 0.091 pF/mm on 0.16 mm F.Cu). So the
    derivation is sound and, at any capacitance a real I2C bus declares, **inert** — 400 pF buys four
    metres of track on a 60 x 45 mm board. It is pinned at 33 pF, where it fires, precisely because
    nothing on these five boards can reach it otherwise.
    """
    src = _example("node")
    hook = 'NetReq("LOAD", volts=3.3)'
    board = _copy(tmp_path / "node", src, [(hook, hook + '\nNetReq("SDA", "SCL", kind="i2c", pf_max=33)')])
    moves, _notes = _report(board)
    assert (
        'SDA: 38.5 mm of airwire (MST of 3 pads, edge to edge) over max_mm=32.9768 '
        '(routed length i2c_capacitance 33 pF (NetReq line 69) - 10 pF x 3 pins on the busiest line '
        'at 0.091 pF/mm (0.16 mm F.Cu), and the MST is a lower bound on it); '
        "the route is longer still; R_SDA is the pad farthest from the rest (layout.core.py:30 attaches R_SDA to U4.SDI); U1.IO4 is the nearest IC pin on SDA"
    ) in moves, "node + i2c at pf_max=33, measured 2026-09-22"
    assert not any(m.startswith(("SDA:", "SCL:")) and "no budget checks it" in m for m in moves), "i2c is budgeted, not unbudgeted"


def test_the_default_i2c_budget_is_derived_and_never_fires_at_this_board_size(tmp_path: Path):
    """The same derivation at the UM10204 default: 4067.1441 mm against node's 38.474 mm span. It is
    recorded because it is the honest reason `FAST_KINDS` could not simply be "use the routed budget
    everywhere" — the one kind with a fully derived length budget has one three orders of magnitude
    above anything a board of this size reaches, so on `i2c` the transfer is correct and silent."""
    src = _example("node")
    hook = 'NetReq("LOAD", volts=3.3)'
    board = _copy(tmp_path / "node", src, [(hook, hook + '\nNetReq("SDA", "SCL", kind="i2c")')])
    design = load_board(board)
    cs = compile_constraints(design)
    sda = next(c for c in cs.constraints if c.net == "SDA")
    assert sda.length_max_mm is not None and sda.length_max_mm.value == 4067.1441
    assert sda.airwire_max_mm is None, "the compiler's printed rules are unchanged; the transfer happens in the check"
    moves, _notes = _report(board)
    assert not any(m.startswith(("SDA:", "SCL:")) for m in moves)


# ---------------------------------------------------------------- F.2 chains

DS2_POWER = 'NetReq("3V3", "VDDA", kind="power", volts=3.3); NetReq("GND", "VSS", kind="power")\n'
DS2_CHAIN = DS2_POWER + 'Path("VDDA", "J2.1", "C4.1", "U1.12")\n'


def _ds2_chain_edit() -> list[tuple[str, str]]:
    """The edit that declares the VDDA path, unless the owner's file already carries it (it does since
    its line 97 was written): a link named twice is refused (docs/net-path-physics.md §7)."""
    return [] if 'Path("VDDA", "J2.1", "C4.1", "U1.12")' in _ds2_src().read_text() else [(DS2_POWER, DS2_CHAIN)]


def test_ds2_chain_passes_as_placed_and_a_cap_after_the_pin_is_named(tmp_path: Path):
    """F.2: with Path("VDDA", "J2.1", "C4.1", "U1.12") the DS2 Addon passes as placed; with the cap
    told to sit right of the ADC it projects after the pin along the feed, and the move says so."""
    board = _copy(tmp_path / "ds2", _ds2_src(), _ds2_chain_edit())
    moves, _notes = _report(board)
    # F.8's own move about this board's two untied grounds is not F.2's business; it is pinned in
    # `test_the_five_boards_add_no_route_aware_move` and its text in `tests/test_bridge.py`.
    # On 2026-10-01 decision 4's corridor (run to the board edge) moved C5 into C4's spot and C4.1 projected 1.4 mm
    # past U1.12; since 2026-10-02 the corridor runs only as far as the path needs to turn and ds2 is placed as before.
    assert [m for m in moves if not m.startswith("GND/VSS:")] == [], moves
    board = _copy(tmp_path / "ds2_right", _ds2_src(), [*_ds2_chain_edit(), ('\nPlace("C4", to="U1.AVDD")', '\nPlace("C4", to="U1.AVDD", toward="right")')])
    moves, _notes = _report(board)
    line = next(i + 1 for i, t in enumerate((board.parent / "layout.core.py").read_text().splitlines()) if t.startswith('Place("C4"'))
    assert (
        "Path line 97: C4.1 projects after U1.12 along the feed (t = 17.4 vs 14.3 mm), and the stations are fed in the order Path line 97 writes them "
        f"(layout.core.py:{line} attaches C4 to U1.AVDD; layout.core.py:10 places U1)"
    ) in moves, "t along J2.1 -> U1.12 (14.3 mm long): C4.1 right of the ADC projects at 17.4 (measured 2026-09-20); no pose (docs/net-path-physics.md §4.5)"


def test_a_pad_in_the_chain_corridor_is_named(tmp_path: Path):
    """F.2: the corridor between consecutive chain pads (width + 2 clearances of the class) holds no
    other net's pad; R10 dropped by CSS between C4.1 and U1.12 is named with its net."""
    board = _copy(tmp_path / "ds2", _ds2_src(), [*_ds2_chain_edit(), ('\nPlace("R10", to="U1.~{RESET}")', '\nPlace("R10", at=(20.6, 7.69))')])
    moves = pcb_job(board)["layout_report"]  # R10 also lands in U1's lane: the check step says so; the corridor line is what is pinned
    line = next(i + 1 for i, t in enumerate((board.parent / "layout.core.py").read_text().splitlines()) if t.startswith('Place("R10"'))
    assert f"VDDA path (Path line 97): R10.2 (nRESET) lies in the corridor between C4.1 and U1.12 (layout.core.py:{line} places R10)" in moves


def test_a_pair_member_with_no_path_is_refused_e13(tmp_path: Path, stock):
    """E13 (docs/physics-subjects.md §7, PR 6): a Pair compares the lengths of its members' paths, so a
    member with three or more pads and no Path has no length to compare. It was the implicit-chain move
    (F.2: "a third pad off the line is a stub"); it is now a compile refusal with the pads, as facts."""
    board = _copy(tmp_path / "c3", _example("c3_usb"), [('Path("USB_DP", "J1.A6", "U3.1", "U1.27")\nPath("USB_DN", "J1.A7", "U3.3", "U1.26")\n', "")])
    assert check_board(board) == [
        "Pair line 205: USB_DP has 5 pads (J1.A6, J1.B6, U3.1, U3.6, U1.27) and 0 Paths name its stations, so the length the pair compares is not defined",
        "Pair line 205: USB_DN has 5 pads (J1.A7, J1.B7, U3.3, U3.4, U1.26) and 0 Paths name its stations, so the length the pair compares is not defined",
    ]
    assert not any("stub" in m for m in stock["c3_usb"][1]), "c3_usb.py carries the Path lines"


# ---------------------------------------------------------------- F.3 corridors


def test_buck_wide_nets_have_their_channels_as_placed(stock):
    """F.3: buck's 0.781 mm VIN / GND class, the 5V trunk's links and 5V's own 0.4 mm branches find
    their channels between MST-adjacent pads; the one link that does not is the switch path's (§5:
    C_IN1 pinches it to 0.46 mm, named with the line to edit, never a pose)."""
    got = [m for m in stock["buck"][1] if "channel" in m]
    # Until 2026-10-01 the switch path's own channel was named (C_IN1 pinched it to 0.46 mm and SW was held to F.Cu).
    # Decision 2 lets a switch node change layer and decision 4 moved C_IN1 off U1.2's exit: every link has its channel.
    assert got == [], got


def test_a_boxed_pad_names_the_pinch(tmp_path: Path):
    """F.3: C_IN1's VIN pad in the top-left corner with R_FB_BOT across the pocket's mouth, the net
    held to F.Cu: no 0.86 mm channel (1.18 before decision 5 put VIN at 1.36 A); the two obstacles across the line are the pinch."""
    board = _copy(
        tmp_path / "buck",
        _example("buck"),
        [
            ('Place("C_IN1", to="U1.VIN")', 'Place("C_IN1", at=(2.2, 1.9))'),
            ('Place("R_FB_BOT", to="U1.FB")', 'Place("R_FB_BOT", at=(1.9, 3.6))'),
            # docs/physics-subjects.md §8: VIN's current, and so its 0.459 mm, is Path line 69's.
            ('Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=1.36)', 'Path("VIN", "J_IN.1", "C_IN2.1", "C_IN1.1", "U1.VIN", amps=1.36, layers=["F.Cu"])'),
        ],
    )
    moves, _notes = _report(board)
    # docs/physics-subjects.md §8 (PR 3): VIN's 0.459 mm is its Path's (line 69), so the channel asked is the Path's
    # link C_IN1.1 -> U1.VIN, and the pocket's mouth is C_IN1's own GND pad and R_FB_BOT.2. Re-measured 2026-10-02.
    assert (
        "VIN path C_IN1.1 -> U1.VIN (Path line 53): no 0.46 mm channel from C_IN1.1 to U1.VIN on F.Cu (0.459 width + 2 x 0.2 clearance = 0.86 mm): "
        "C_IN1.2 and R_FB_BOT.2 pinch it to 0.66 mm (layout.core.py:8 places C_IN1; layout.core.py:16 places R_FB_BOT)"
    ) in moves, "0.459 = IPC-2221 1.36 A 10 C 1 oz jlcpcb_2l_1oz on Path line 53"


def test_the_corridor_check_stays_under_the_bar_on_node(stock):
    """Acceptance: node's pcb_job gets no more than 3 s slower; the route checks (four 0.4 mm nets,
    two outer layers, 0.1 mm grid over 60 x 45 mm) run in a fraction of that."""
    design, job, _ctx = _placed(stock["node"][0])
    from conftest import placed_board

    text = placed_board("node", stock["node"][0]).read_text()
    t0 = time.perf_counter()
    moves, _notes = route_aware_report(design, job, feet_of_text(design, text))
    assert time.perf_counter() - t0 < 3.0, "H.8: node under 3 s is the bar"
    # docs/physics-subjects.md §8 (PR 3): node's VBUS current is Path line 111's, and R_CC1.2 lies in its corridor (F.2).
    assert [m.split(":")[0] for m in moves] == ["USB_DN", "USB_DP", "VBUS path (Path line 66)"], "F.1's unbudgeted-fast move, F.2's VBUS corridor, nothing from F.3"
    assert [m for m in moves if "corridor" in m] == ["VBUS path (Path line 66): R_CC1.2 (GND) lies in the corridor between J1.VBUS and U2.VIN (layout.core.py:14 attaches R_CC1 to J1.CC1)"]


# ---------------------------------------------------------------- F.4 loops


def test_buck_hot_loop_is_measured_and_noted(stock):
    """F.4: Cin (C_IN1, the cap nearest the IC), U1.VIN, U1.GND (synchronous: the IC's own ground)
    enclose 3.3 mm2, under the 20 mm2 preset: a note, not a move. Since the migration buck declares the
    loop (`Loop()` line 50, docs/net-path-physics.md §6) and the guess does not run for its parts; the
    declared loop's note says the area is a lower bound."""
    _board, _moves, notes = stock["buck"]
    assert "loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 (Loop line 52): pads enclose at least 3.7 mm2 of 20 (shoelace of pad centres; no vertical leg)" in notes, notes
    # Since decision 4 moved C_IN1 off U1.2's exit, the input cap nearest U1 is C_IN2, which the Loop does not name:
    # the guess measures C_IN2's loop and only that.
    assert [n for n in notes if n.startswith("SW: hot loop")] == ["SW: hot loop 0.9 mm2 (budget 20)"], notes


def test_a_far_input_cap_opens_the_hot_loop(tmp_path: Path):
    """F.4: C_IN1 12 mm below the IC (C_IN2 farther still, L1 held at its stock spot): the loop is
    1.9 x 12.04 = 22.9 mm2 over the 20 mm2 budget, and the move names the cap."""
    board = _copy(
        tmp_path / "buck",
        _example("buck"),
        [
            (
                'Place("C_IN1", to="U1.VIN")\nPlace("C_IN2", to="U1.VIN")\nPlace("L1", to="U1.SW")',
                'Place("C_IN1", at=(16.46, 22.0), rot=180)\nPlace("C_IN2", at=(6.0, 20.0), rot=180)\nPlace("L1", at=(22.84, 13.996), rot=180)',
            )
        ],
    )
    # The guessed loop's text (the declared Loop() is taken out so the guess runs; `test_loops.py` holds
    # the declared form). Rewritten by docs/net-path-physics.md §6: no budget fitted to the placement
    # being judged, no pose, the attachment board.py already writes (none here: C_IN1 is placed with at=).
    text = board.read_text()
    write_split(board, text.replace('Loop("C_IN1.1", "U1.VIN", "U1.GND", "C_IN1.2")\n', ""))
    moves, _notes = _report(board)
    assert (
        "SW: hot loop C_IN1.1 -> U1.VIN -> U1.GND -> C_IN1.2 encloses 22.9 mm2 over the 20 mm2 budget (preset switch_node, pcbc default); "
        "C_IN1.1 is 12.0 mm from U1.VIN (layout.core.py:8 places C_IN1; layout.core.py:3 places U1)"
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
            "VDDA: decap loop U1.AVDD -> C4.1 -> C4.2 -> U1.AVSS encloses 13.9 mm2 over the 6 mm2 budget (preset power, pcbc default); "
            "layout.core.py:14 attaches C4 to U1.AVDD"
        ) in notes, "DS2 as placed, 2026-09-20 (8.8 mm2 while decision 4's corridor ran to the board edge, 2026-10-01; 13.9 again since 2026-10-02)"
        assert not any("decap loop" in m for m in moves)


# ---------------------------------------------------------------- F.5 keep-away


def test_keep_away_from_the_switch_node_names_the_boot_cap(tmp_path: Path, stock):
    """F.5: with keep_clear_of="SW" on FB, R_FB_TOP.2 is 1.62 mm edge to edge from C_BOOT.2 (the
    IC's own FB and SW pins are its rating, not layout); stock buck says nothing and the constraint
    report carries the keep_clear_of none note."""
    board = _copy(tmp_path / "buck", _example("buck"), [('NetReq("FB", kind="analog")', 'NetReq("FB", kind="analog", keep_clear_of="SW")')])
    moves, _notes = _report(board)
    assert (
        "FB: R_FB_TOP.2 is 1.62 mm from C_BOOT.2 (SW), under keep_clear_mm=3 (preset analog, NetReq line 44) "
        "(layout.core.py:15 attaches R_FB_TOP to U1.FB; layout.core.py:13 attaches C_BOOT to U1.BOOT)"
    ) in moves, "AABB gap of the 0402 pads as placed: dx 1.52, dy 0.57 -> 1.62 mm (measured 2026-09-20; the design pass wrote 1.84)"
    stock_board, stock_moves, _ = stock["buck"]
    assert not any(m.startswith("FB:") for m in stock_moves)
    assert 'FB: keep_clear_of none; this board has a switch node (SW)' in compile_constraints(load_board(stock_board)).lines


# ---------------------------------------------------------------- F.6 reference plane

NODE_PLANES = 'planes=[("GND", "In1.Cu"), ("3V3", "In2.Cu")]'
NODE_ANTENNA = 'Keepout("ANTENNA", position="absolute", left=0, top=0, width=4, height=15)'


def test_a_pair_without_its_plane_is_a_compile_refusal(tmp_path: Path):
    """F.6 at compile: node with planes=[] wants In1.Cu declared before any geometry exists."""
    board = _copy(tmp_path / "node", _example("node"), [(NODE_PLANES, "planes=[]")])
    assert "USB_DP: 90 ohm on F.Cu (Pair line 62) has no reference plane: In1.Cu, the layer next to F.Cu, is not in Board(planes=)" in check_board(board)


def test_a_keepout_under_a_pair_pad_is_a_move(tmp_path: Path):
    """F.6 at placement: the antenna keepout stretched over J1 leaves the pair's connector pads with
    no In1.Cu GND under them; the move names the pad and the Place() that clears it."""
    board = _copy(tmp_path / "node", _example("node"), [(NODE_ANTENNA, 'Keepout("ANTENNA", position="absolute", left=0, top=38, width=34, height=7)')])
    moves = pcb_job(board)["layout_report"]  # the keepout also boxes J1's ground and VBUS channels; those moves are the corridor check's
    assert "USB_DP: J1.A6 sits in Keepout ANTENNA: no In1.Cu GND under it, the pair has no reference there (layout.core.py:7 puts J1 on the bottom edge)" in moves
    assert "USB_DN: J1.A7 sits in Keepout ANTENNA: no In1.Cu GND under it, the pair has no reference there (layout.core.py:7 puts J1 on the bottom edge)" in moves


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
    write_split(board, f"""from pcbc import *
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
NetReq("HV+", kind="power", volts=250); NetReq("HV-", kind="power")  # HV- is Ground(): no volts= (physics-subjects E3)
SchRegion("sheet", left=12, top=12, width=200, height=120)
SchPlace("U7", parent="sheet", left=80, top=40)
SchPlace("R1", to="U7.A")
SchPlace("C1", along="R1.1", side="bottom")
SchPlace("R2", to="U7.C")
SchPlace("C2", along="R2.1", side="bottom")
""")
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
        "Isolation primary/secondary (250 V): Regions are 2.0 mm apart, 0.5 mm short of the 2.5 mm creepage (IEC 60664-1 F.5 row 250 V IIIa PD2)"
    ) in moves, "C.8: creepage 2.5 mm at 250 V (H.5: the general PD2 column) governs over 1.5 mm clearance"

    moves, _notes = _report(_isolation_board(tmp_path / "side", r2_left=19.5))
    assert any(m.startswith("Isolation primary/secondary: R2 (secondary) crosses the corridor (layout.core.py:") and "move" not in m for m in moves), moves

    moves = pcb_job(_isolation_board(tmp_path / "span", u7_left=4))["layout_report"]  # U7 at the far left also overlaps C1: reported by the check step
    assert any(m.startswith("Isolation primary/secondary: U7 is in across= but does not span the 3.0 mm gap (layout.core.py:") for m in moves), moves


def test_a_move_never_asks_the_ai_to_undo_its_own_floorplan(tmp_path: Path):
    """F.1 named whichever pad was farthest from its neighbours, which on a board-wide net is the
    module or the edge connector: `Place("U1", to="J1.EH")` asks the AI to re-relate the part it
    anchored on purpose. A connector on an edge and a multi-pin IC placed by CSS are the
    floorplan; the farthest part that is not becomes the move."""
    src = _example("c3_usb")
    # The three power nets each on their own NetReq since docs/physics-subjects.md PR 4 (E3): the budget on each.
    lines = [l for l in src.read_text().splitlines() if l.startswith(('NetReq("VBUS"', 'NetReq("3V3"', 'NetReq("GND"'))]
    assert len(lines) == 3, lines
    board = _copy(tmp_path / "c3", src, [(line, line[:-1] + ", max_mm=5)") for line in lines])
    moves, _notes = _report(board)
    # The two usb_hs nets carry no budget at all and print the other F.1 line, which names no part:
    # a missing declaration is not a misplaced part (`route_checks._unbudgeted_fast`).
    airwire = [m for m in moves if "airwire (MST" in m and "is the pad farthest from the rest" in m]
    assert len(airwire) == 3, airwire
    assert len([m for m in moves if "no budget checks it" in m]) == 2
    named = {m.split("; ")[2].split(" is the pad farthest")[0] for m in airwire}
    assert named == {"R_BOOT", "D1", "U3"}, named
    assert not any('Place("' in m for m in airwire), "a fact, never a Place() line (docs/direction.md §7b)"


def test_a_pair_member_split_over_two_paths_is_refused_and_names_no_order(tmp_path: Path):
    """E13 with two Paths: the member's length is the sum of two paths only if they meet, and which
    order the signal is fed in is the author's. The refusal is a statement: it names no Path() to type
    (docs/direction.md §7b)."""
    board = _copy(
        tmp_path / "c3",
        _example("c3_usb"),
        [('Path("USB_DN", "J1.A7", "U3.3", "U1.26")', 'Path("USB_DN", "J1.A7", "U3.3")\nPath("USB_DN", "U3.3", "U1.26")')],
    )
    fails = check_board(board)
    assert fails == ["Pair line 205: USB_DN has 5 pads (J1.A7, J1.B7, U3.3, U3.4, U1.26) and 2 Paths name its stations, so the length the pair compares is not defined"], fails
    assert not any('Path("' in m for m in fails)


def test_a_keepout_under_a_two_layer_controlled_line_is_a_style_note(tmp_path: Path):
    """F.6 on two layers: the B.Cu pour is the reference, so a copper keepout under the straight
    line between a controlled net's pads takes it away. S4 implemented it with no test."""
    board = tmp_path / "ref2l.py"
    write_split(board, '''from pcbc import *
VCC = Power("VCC"); GND = Ground("GND"); SIG = Net("SIG")
Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=SIG, p2=GND)
Resistor("R2", "1k", package="0603", mpn="X", lcsc="C1", p1=SIG, p2=VCC)
Capacitor("C1", "1uF", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)
Board(width=40, height=20, layers=2, stackup="jlcpcb_2l_1oz")
Keepout("SPLIT", position="absolute", left=18, top=0, width=4, height=20, no="copper")
NetReq("GND", kind="power")
Path("SIG", "R1.1", "R2.1", z_se_ohm=50, layers=("F.Cu",))  # an impedance is a Path's (physics-subjects PR 5)
Place("R1", position="absolute", left=4, top=8)
Place("R2", position="absolute", left=32, top=8)
Place("C1", to="R2.2")
for i, r in enumerate(["R1", "R2", "C1"]):
    SchPlace(r, left=20 + 30 * i, top=20)
''')
    _moves, notes = _report(board)
    assert [n for n in notes if "SPLIT" in n] == [
        "SIG: the line from R1.1 to R2.1 crosses Keepout SPLIT; the B.Cu pour is the track's reference and has a hole there"
    ], notes
