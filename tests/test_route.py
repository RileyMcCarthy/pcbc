"""Copper: the native router routes, KiCad judges, ids are derived from geometry, the AI never draws a
track."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbc.build import build_job
from pcbc.language import load_board
from pcbc.netcheck import check_copper, compare, copper_nets, expected_nets
from pcbc.stackup import copper_layers

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BLINKY = EXAMPLES / "blinky" / "blinky.py"
C3_USB = EXAMPLES / "c3_usb" / "c3_usb.py"


def test_copper_layers_by_count():
    assert copper_layers(2) == ["F.Cu", "B.Cu"]
    assert copper_layers(4) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_copper_nets_reads_pad_bindings():
    text = (
        "(kicad_pcb\n"
        '\t(footprint "R"\n\t\t(property "Reference" "R1"\n\t\t)\n'
        '\t\t(pad "1" smd rect\n\t\t\t(net 1 "VCC")\n\t\t)\n'
        '\t\t(pad "2" smd rect\n\t\t\t(net 2 "LED")\n\t\t)\n'
        '\t\t(pad "" np_thru_hole circle\n\t\t\t(net 2 "LED")\n\t\t)\n'
        '\t\t(pad "3" smd rect\n\t\t)\n\t)\n)\n'
    )
    assert copper_nets(text) == {"VCC": {("R1", "1")}, "LED": {("R1", "2")}}


def test_compare_speaks_copper_when_asked():
    fails = compare({"LED": {("R1", "2"), ("D1", "2")}}, {"LED": {("R1", "2")}}, what="copper")
    assert fails == ["LED: copper net 'LED' is missing D1.2"]


@pytest.mark.kicad
def test_placed_board_fails_the_copper_gate_as_unconnected(tmp_path: Path):
    import shutil

    board = tmp_path / "c3_usb.py"
    board.write_text(C3_USB.read_text())
    shutil.copytree(C3_USB.parent / "components", tmp_path / "components")
    result = build_job(board, upto="place", force=True)
    assert result.get("error") is None, result
    placed = tmp_path / "layout" / "c3_usb" / "placed" / "layout.kicad_pcb"
    gate = check_copper(load_board(board), placed)
    assert not gate["ok"]
    assert gate["unconnected"] > 0
    assert gate["nets"] == [], gate["nets"]  # the placed footprints bind every pad exactly as board.py says


@pytest.mark.kicad
def test_blinky_routes_clean_and_the_same_twice(tmp_path: Path):
    outs = []
    for i in (1, 2):
        board = tmp_path / f"b{i}" / "blinky.py"
        board.parent.mkdir()
        board.write_text(BLINKY.read_text())
        result = build_job(board, upto="route", force=True)
        assert result.get("error") is None, result
        step = result["steps"][-1]
        assert step["stage"] == "route" and step["router"] == "native"
        assert step["emitted"]["segments"] > 0 and not step.get("unrouted")
        assert step["copper"] == "verified", step
        outs.append((board.parent / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_bytes())
    assert outs[0] == outs[1], "routed copper is not byte-for-byte"
    design = load_board(tmp_path / "b1" / "blinky.py")
    text = outs[0].decode()
    assert copper_nets(text) == expected_nets(design)


def test_pair_false_is_the_no_op_the_pair_refusal_warns_about(tmp_path: Path):
    """The claim the move makes, asserted rather than believed: the preset wins, and only
    `autoroute=` overrides it."""
    import shutil

    from pcbc.compile import compile_design
    from pcbc.language import load_board

    src = Path(__file__).resolve().parent.parent / "examples" / "c3_usb"
    shutil.copytree(src / "components", tmp_path / "components")
    line = 'NetReq("USB_DP", "USB_DN", kind="usb_hs", z_diff_ohm=90, pair=True)'
    text = (src / "c3_usb.py").read_text()
    assert text.count(line) == 1

    def _auto(replacement: str):
        board = tmp_path / "c3.py"
        board.write_text(text.replace(line, replacement))
        job = compile_design(load_board(board))
        return next(cn.autoroute for cn in job.nets if "USB_DP" in cn.patterns)

    assert _auto(line.replace("pair=True", "pair=False")) == "diff_pair"  # the no-op
    assert _auto(line.replace("pair=True", "autoroute=True")) is True  # the edit that works


@pytest.mark.kicad
def test_the_route_stage_leaves_nothing_of_an_earlier_build(tmp_path: Path):
    """Every output of the route stage from an earlier build is unlinked before it starts (so a refused
    build never leaves an older board behind): the gen file, the emitted board, its sidecars, and the
    unrouted problem files. What is left is exactly what this build wrote."""
    board = tmp_path / "blinky.py"
    board.write_text(BLINKY.read_text())
    work = tmp_path / "layout" / "blinky" / "routed"
    (work / "unrouted").mkdir(parents=True)
    (work / "unrouted" / "LED-1.json").write_text("{}")
    result = build_job(board, upto="route", force=True)
    assert result.get("error") is None, result
    assert not (work / "unrouted").exists(), "a problem file from an earlier build survived a clean route"
    assert sorted(p.name for p in work.iterdir()) == ["layout.gen.py", "layout.kicad_dru", "layout.kicad_pcb", "layout.kicad_prl", "layout.kicad_pro"]


@pytest.mark.kicad
def test_an_unrouted_net_is_a_move_with_its_lines_and_a_problem_file():
    """N0 (docs/native-plan.md §3.6, As built 12): a net the native router cannot finish ships no
    generated copper at all; the build still emits, fills and runs every gate, then fails with
    `unrouted:`, one move per missing link and a closing line saying what was dropped,
    each naming what is in the way by its `board.py` line and pointing at a problem file; no stamp, no
    fab; KiCad's unconnected nets are exactly the router's unrouted nets. Measured on c3_usb, whose
    `VBUS` has no path out of the USB-C connector J1 (2026-09-25)."""
    from conftest import routed_result

    out, result = routed_result("c3_usb", C3_USB)
    assert str(result["error"]).startswith("unrouted:"), result["error"]
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert set(route["unconnected_nets"]) == set(result["unrouted"]), (route["unconnected_nets"], result["unrouted"])
    layout = out.parent.parent
    assert not (layout / "fab").exists() and not (layout / "inputs.json").exists()
    assert result["moves"], result
    links = [m for m in result["moves"] if "cannot reach" in m]
    closing = [m for m in result["moves"] if m not in links]
    assert links and [m.split(":")[0] for m in closing] == sorted(result["unrouted"]), closing
    assert all("unfinished, so none of its generated copper" in m for m in closing), closing
    for move in links:
        assert "Move:" in move and "Problem file:" in move, move
        assert "board.py" in move or "layout.core.py" in move, move
        name = move.split("Problem file: ")[1].split("/")[-1].strip()
        doc = json.loads((out.parent / "unrouted" / name).read_text())
        assert doc["net"] in result["unrouted"] and doc["scene_near"], doc
    assert "Traceback" not in result["error"]
