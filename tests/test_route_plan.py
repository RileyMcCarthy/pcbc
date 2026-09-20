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
    assert _args(sig, "--power-nets") == ["5V", "GND", "VIN"]  # sorted: the plan is a pure function of board.py
    assert _args(sig, "--power-nets-widths") == ["0.781", "0.781", "0.781"]  # 2 A on 1 oz
    pour = dict(_plan("buck"))["gnd_pour"]
    assert pour[3].endswith("route_planes.py") and _args(pour, "--plane-layers") == ["B.Cu"]
    assert "--keep-input-copper" not in pour and "--no-fix-drc-settings" in pour
    assert "--clearance" not in sig and "--fab-overrides" in sig  # classes carry the clearances; the fab rung is pinned


def test_c3_usb_routes_the_usb_pair_as_a_pair():
    plan = dict(_plan("c3_usb"))
    assert list(plan) == ["pair_usb_dn", "signals", "gnd_pour", "finalize"]
    pair = plan["pair_usb_dn"]
    assert pair[3].endswith("route_diff.py") and _args(pair, "--nets") == ["USB_DN", "USB_DP"]
    assert "--diff-pair-intra-match" in pair and "--impedance" not in pair  # 90 ohm is not a 2-layer thing


def test_node_pours_its_planes_before_the_signals_and_asks_90_ohm():
    plan = _plan("node")
    names = [n for n, _ in plan]
    assert names == ["analog_nets", "pair_usb_dn", "planes", "signals"], names
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
