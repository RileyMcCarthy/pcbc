"""The routing plan is compiled from NetReq: constrained nets first, pairs, planes, the rest."""

from __future__ import annotations

from pathlib import Path

from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.route import krt_plan

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _plan(board: str) -> list[tuple[str, list[str]]]:
    design = load_board(EXAMPLES / board / f"{board}.py")
    return krt_plan(compile_design(design), design, Path("placed.kicad_pcb"), Path("routed"), Path("/krt"))


def _args(cmd: list[str], flag: str) -> list[str]:
    i = cmd.index(flag)
    out = []
    for a in cmd[i + 1 :]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def test_buck_routes_the_switch_node_and_feedback_first_on_one_layer_without_vias():
    names = [n for n, _ in _plan("buck")]
    assert names == ["analog_nets", "switchnode_nets", "signals", "gnd_pour", "finalize"], names
    sw = dict(_plan("buck"))["switchnode_nets"]
    assert _args(sw, "--nets") == ["SW"] and _args(sw, "--layers") == ["F.Cu"] and "--via-cost" in sw
    assert _args(sw, "--track-width") == ["0.3"]
    fb = dict(_plan("buck"))["analog_nets"]
    assert _args(fb, "--nets") == ["FB"] and _args(fb, "--layers") == ["F.Cu"]
    sig = dict(_plan("buck"))["signals"]
    assert _args(sig, "--nets") == ["*", "!FB", "!SW"]  # the constrained nets are never finished with forbidden vias
    assert _args(sig, "--power-nets") == ["5V", "GND", "VIN"]  # sorted: the plan is a pure function of board.py
    assert _args(sig, "--power-nets-widths") == ["0.781", "0.781", "0.781"]  # 2 A on 1 oz
    pour = dict(_plan("buck"))["gnd_pour"]
    assert pour[3].endswith("route_planes.py") and _args(pour, "--plane-layers") == ["B.Cu"]
    assert "--keep-input-copper" not in pour and "--no-fix-drc-settings" in pour
    assert "--clearance" not in sig and "--fab-overrides" in sig  # classes carry the clearances; the fab rung is pinned


def test_vias_keep_out_of_same_net_pads_on_the_long_nets_and_the_pour_only():
    """The DS2 Addon's signals step dropped a VSS via inside a cap's pad and the fab stage refused
    the board. KRT's keepout blocks via placement only, so it goes on the steps that place vias
    on long nets; on the short-hop and constrained steps it once left a 0.2 mm hop with no path."""
    plan = dict(_plan("buck"))
    for name in ("signals", "gnd_pour"):
        assert _args(plan[name], "--same-net-pad-clearance") == ["0.127"], name  # the stackup's clearance floor
    for name in ("analog_nets", "switchnode_nets", "finalize"):
        assert "--same-net-pad-clearance" not in plan[name], name


def test_short_hops_route_first_on_the_empty_board(tmp_path: Path):
    """A hop between neighbouring pads has one path; the constrained analog nets, routed first,
    once ran through the slot between a header pin and its resistor (DS2 Addon: REFN, A0)."""
    import shutil

    from pcbc.build import pcb_job

    shutil.copytree(EXAMPLES / "buck", tmp_path / "buck")
    result = pcb_job(tmp_path / "buck" / "buck.py")
    assert result.get("error") is None, result
    design = load_board(tmp_path / "buck" / "buck.py")
    plan = krt_plan(compile_design(design), design, Path(result["placed"]), tmp_path / "routed", Path("/krt"))
    names = [n for n, _ in plan]
    assert "local_hops" in names, names
    assert names.index("local_hops") < min(i for i, n in enumerate(names) if n.endswith("_nets"))
    hops = dict(plan)["local_hops"]
    assert "--same-net-pad-clearance" in hops and "--via-cost" not in hops
    # A hop on a power net keeps the power width (node's 1 A LOAD was routed at 0.09 mm as a hop).
    assert _args(hops, "--power-nets") == ["5V", "GND", "VIN"] and _args(hops, "--power-nets-widths") == ["0.781", "0.781", "0.781"]


def test_the_plan_has_no_fanout_step_and_starts_from_the_board_it_is_given(tmp_path: Path):
    """The closed rows' escapes are pcbc's own copper (`fanout.py`), written before KRT runs;
    KRT's qfn_fanout is not in the plan (see test_fanout.py for why). R2 S4 renamed the step file
    `00_fanout` -> `00_patterns_pre`: the hops go in the same board and `blocking.step_boards` names
    the step that placed an item, so the name has to be the truth (C.1)."""
    import shutil

    from pcbc.build import pcb_job

    shutil.copytree(EXAMPLES / "c3_usb", tmp_path / "c3_usb")
    result = pcb_job(tmp_path / "c3_usb" / "c3_usb.py")
    assert result.get("error") is None, result
    design = load_board(tmp_path / "c3_usb" / "c3_usb.py")
    start = tmp_path / "routed" / "00_patterns_pre.kicad_pcb"
    plan = krt_plan(compile_design(design), design, start, tmp_path / "routed", Path("/krt"))
    names = [n for n, _ in plan]
    assert not any(n.startswith("fanout_") for n in names), names
    assert plan[0][1][4] == str(start)  # the first KRT step reads the fanned board
    assert all(not c[3].endswith("qfn_fanout.py") for _, c in plan)


def test_c3_usb_routes_the_usb_pair_as_a_pair():
    plan = dict(_plan("c3_usb"))
    assert list(plan) == ["pair_usb_dn", "signals", "gnd_pour", "finalize"]
    pair = plan["pair_usb_dn"]
    assert pair[3].endswith("route_diff.py") and _args(pair, "--nets") == ["USB_DN", "USB_DP"]
    assert "--diff-pair-intra-match" in pair and "--impedance" not in pair  # 90 ohm is not a 2-layer thing


def test_node_pours_its_planes_before_the_signals_and_asks_90_ohm():
    plan = _plan("node")
    names = [n for n, _ in plan]
    assert names == ["analog_nets", "pair_usb_dn", "planes", "plane_taps", "signals"], names
    taps = dict(plan)["plane_taps"]
    # KRT's pour places no tap vias; the route step welds each pad to its plane, and the same-net
    # keepout stopped that (node: 4 of 59 GND pads). The taps route alone, keepout off explicitly.
    assert _args(taps, "--nets") == ["GND", "3V3"] and _args(taps, "--same-net-pad-clearance") == ["-1"]
    sig = dict(plan)["signals"]
    assert _args(sig, "--nets") == ["*", "!T_DIV", "!T_OUT", "!GND", "!3V3"] and _args(sig, "--same-net-pad-clearance") == ["0.0889"]
    planes = dict(plan)["planes"]
    assert _args(planes, "--nets") == ["GND", "3V3"] and _args(planes, "--plane-layers") == ["In1.Cu", "In2.Cu"]
    pair = dict(plan)["pair_usb_dn"]
    assert _args(pair, "--impedance") == ["90"] and _args(pair, "--layers") == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    analog = dict(plan)["analog_nets"]
    assert _args(analog, "--nets") == ["T_DIV", "T_OUT"] and _args(analog, "--layers") == ["F.Cu"]


def test_every_step_chains_on_the_previous_output():
    plan = _plan("node")
    prev = "placed.kicad_pcb"
    for name, cmd in plan:
        assert cmd[4] == prev, (name, cmd[4], prev)
        prev = cmd[5]
        assert "--no-fix-drc-settings" in cmd
    assert _plan("node") == plan  # pure


def test_a_constrained_nets_copper_is_locked_and_kept_out_of_later_steps():
    """The signal step re-routed the DS2 Addon's AIN0 with four vias after its own no-via step:
    `--nets * !AIN0` did not keep KRT's rip-up off it. KiCad-locked copper is never ripped."""
    from pcbc.route import lock_copper

    text = (
        '(kicad_pcb\n\t(net 3 "AIN0")\n'
        '\t(segment\n\t\t(start 0 0)\n\t\t(end 1 1)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net 3)\n\t\t(uuid "a")\n\t)\n'
        '\t(via\n\t\t(at 2 2)\n\t\t(size 0.5)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "AIN0")\n\t\t(uuid "b")\n\t)\n'
        '\t(segment\n\t\t(start 0 0)\n\t\t(end 1 1)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "GND")\n\t\t(uuid "c")\n\t)\n)\n'
    )
    out = lock_copper(text, {"AIN0"})
    assert out.count("(locked yes)") == 2
    assert "(width 0.2)\n\t\t(locked yes)\n\t\t(layer" in out and '(layers "F.Cu" "B.Cu")\n\t\t(locked yes)' in out  # where KRT's parser reads it
    assert "(locked yes)" not in out.split('(net "GND")')[0].split('(uuid "b")')[1]  # GND untouched
    assert lock_copper(out, {"AIN0"}) == out  # idempotent
    plan = dict(_plan("node"))
    assert _args(plan["signals"], "--nets")[:3] == ["*", "!T_DIV", "!T_OUT"]


# ---------------------------------------------------------------------------------------------
# `Board(net_order=)`: the one router knob a board declares (docs/quality-plan.md slice 4)
# ---------------------------------------------------------------------------------------------


def _plan_of(board: Path) -> list[tuple[str, list[str]]]:
    design = load_board(board)
    return krt_plan(compile_design(design), design, Path("placed.kicad_pcb"), Path("routed"), Path("/krt"))


DECLARING = (
    "from pcbc import *\n"
    'VCC = Power("VCC"); GND = Ground("GND")\n'
    'Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
    'Resistor("R2", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
    'NetReq("VCC", "GND", kind="power", volts=5, amps=1)\n'
    'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz", net_order="original")\n'
    'Place("R1", at=(5, 5)); SchPlace("R1", left=20, top=20)\n'
    'Place("R2", at=(15, 5)); SchPlace("R2", left=60, top=20)\n'
)


def test_the_declared_net_order_reaches_the_wildcard_steps_and_no_others(tmp_path: Path):
    """`Board(net_order=)` on a board that declares it, and the scope is a measurement not a taste.

    Given to every `route.py` step instead -- `local_hops`, the constrained `*_nets` steps,
    `plane_taps` -- the DS2 Addon stops being a board: `kicad_drc` returns two `items_not_allowed`
    errors and a `solder_mask_bridge`, where the same board with the flag on `signals` alone is
    identical to base on every violation type (114 warnings, 0 errors, both arms, measured
    2026-09-21). The constrained steps route on an otherwise empty board and have no congestion for
    an order to negotiate, so the two wildcard steps are the only ones whose order can matter.

    A synthetic board and not buck, because **no board in this repo declares it** and the reason is
    in `docs/quality-plan.md` slice 4: buck is the one board it wins on, and it wins by routing the
    2 A `VIN` rail through two layer changes on a single 0.3 mm via each -- `via_parallelism` reads
    0.707 A carried against 3 needed, at `(18.2, 8.45)` and `(18.25, 5.75)`, where base buck changes
    layer on that rail **not at all**.
    """
    board = tmp_path / "declaring.py"
    board.write_text(DECLARING)
    got = {name: ("--ordering" in cmd and _args(cmd, "--ordering")) for name, cmd in _plan_of(board)}
    assert got == {"signals": ["original"], "gnd_pour": False, "finalize": ["original"]}, got


def test_a_board_that_declares_no_net_order_passes_the_flag_nowhere():
    """The default is KRT's own, and pcbc says nothing rather than repeating it: a board that
    declares nothing must produce the command it produced before `net_order` existed. **No board in
    this repo declares one**, so all five are byte-identical to base on a fresh build and not one
    recorded number moves; this is the cheap half of that check."""
    for board in ("blinky", "buck", "c3_usb", "node"):
        for name, cmd in _plan(board):
            assert "--ordering" not in cmd, (board, name)


def test_a_net_order_the_router_does_not_have_is_refused_at_declaration(tmp_path: Path):
    """A typo in `net_order=` must not become a KRT step that exits non-zero halfway through a build.

    The four names are KRT's own `--ordering` choices, pinned against `route.KRT_SHA`. The refusal
    lists them and **recommends none**: `"original"` is the only one that measured a win anywhere
    (buck, -9.7 % copper) and it pays for that win with two under-rated vias on a 2 A rail, so a
    refusal that named it would be advice this repo has measured against.
    """
    from pcbc.language import check_board

    head = (
        "from pcbc import *\n"
        'VCC = Power("VCC"); GND = Ground("GND")\n'
        'Resistor("R1", "1k", package="0603", mpn="X", lcsc="C1", p1=VCC, p2=GND)\n'
        'Board(width=40, height=25, layers=2, stackup="jlcpcb_2l_1oz", net_order={order})\n'
        'Place("R1", at=(5, 5)); SchPlace("R1", left=20, top=20)\n'
    )
    good = tmp_path / "good.py"
    good.write_text(head.format(order='"original"'))
    assert not [f for f in check_board(good, pcb=False) if "net_order" in f]
    bad = tmp_path / "bad.py"
    bad.write_text(head.format(order='"shortest"'))
    fail = check_board(bad, pcb=False)[0]
    assert "Board(net_order='shortest')" in fail
    assert "'mps', 'inside_out', 'original', 'bus'" in fail
    # And it recommends nothing: the value that measured fastest also measured unsafe, so the move
    # is "drop it, and measure any other choice against the via parallelism report".
    assert "drop the argument" in fail and "under-rated vias on a 2 A rail" in fail
    assert "pcbc build --strict-power" in fail  # a pointer at a flag that exists (cli.py:41)
