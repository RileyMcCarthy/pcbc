"""Copper: KRT routes, KiCad judges, ids are pinned, the AI never draws a track."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbc.build import build_job
from pcbc.language import load_board
from pcbc.netcheck import check_copper, compare, copper_nets, expected_nets
from pcbc.route import copper_layers, krt_missing, pin_copper_ids, route_job

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BLINKY = EXAMPLES / "blinky" / "blinky.py"
C3_USB = EXAMPLES / "c3_usb" / "c3_usb.py"


def test_copper_layers_by_count():
    assert copper_layers(2) == ["F.Cu", "B.Cu"]
    assert copper_layers(4) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_copper_ids_are_keyed_by_order_not_chance():
    text = (
        '(kicad_pcb\n\t(segment\n\t\t(start 0 0)\n\t\t(uuid "aaaa")\n\t)\n'
        '\t(via\n\t\t(at 1 1)\n\t\t(uuid "bbbb")\n\t)\n'
        '\t(segment\n\t\t(start 2 2)\n\t\t(uuid "cccc")\n\t)\n)\n'
    )
    once = pin_copper_ids(text, "b")
    assert "aaaa" not in once and "bbbb" not in once and "cccc" not in once
    assert once == pin_copper_ids(text.replace("aaaa", "zzzz"), "b")  # KRT's random ids do not matter
    assert once != pin_copper_ids(text, "other-board")


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


def test_route_without_krt_says_how_to_get_it(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KRT_HOME", str(tmp_path / "nowhere"))
    assert "git clone" in (krt_missing() or "")
    design = load_board(BLINKY)
    src = tmp_path / "in.kicad_pcb"
    src.write_text("(kicad_pcb)\n")
    result = route_job(design, src, out=tmp_path / "out" / "layout.kicad_pcb")
    assert result["router"] is None and "KiCadRoutingTools not found" in result["error"]


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
    assert gate["nets"] == [], gate["nets"]  # the seed binds every pad exactly as board.py says


@pytest.mark.kicad
@pytest.mark.krt
def test_blinky_routes_clean_and_the_same_twice(tmp_path: Path):
    outs = []
    for i in (1, 2):
        board = tmp_path / f"b{i}" / "blinky.py"
        board.parent.mkdir()
        board.write_text(BLINKY.read_text())
        result = build_job(board, upto="route", force=True)
        assert result.get("error") is None, result
        step = result["steps"][-1]
        assert step["stage"] == "route" and step["router"] == "krt"
        assert step["segments"] > 0 and step["unrouted"] == []
        assert step["copper"] == "verified", step
        outs.append((board.parent / "layout" / "blinky" / "routed" / "layout.kicad_pcb").read_bytes())
    assert outs[0] == outs[1], "routed copper is not byte-for-byte"
    design = load_board(tmp_path / "b1" / "blinky.py")
    text = outs[0].decode()
    assert copper_nets(text) == expected_nets(design)


def test_route_starts_from_a_clean_work_dir(tmp_path: Path, monkeypatch):
    """A killed build left 01_analog_nets.kicad_pro behind; KRT read it as the next step's
    project and routed FB twice, shorting it into the JST's GND pad."""
    monkeypatch.setenv("KRT_HOME", str(tmp_path / "nowhere"))
    design = load_board(BLINKY)
    src = tmp_path / "in.kicad_pcb"
    src.write_text("(kicad_pcb)\n")
    work = tmp_path / "routed"
    work.mkdir()
    for name in ("01_analog_nets.kicad_pcb", "01_analog_nets.kicad_pro", "03_signals.kicad_pcb"):
        (work / name).write_text("stale")
    route_job(design, src, out=work / "layout.kicad_pcb")
    assert sorted(p.name for p in work.iterdir()) == ["layout.kicad_pcb"]


def test_a_net_krt_could_not_finish_is_reported_as_a_move():
    """KRT prints failed nets in its summary while the net still has copper; pcbc's open-net
    check saw the copper and said nothing, and the gate then reported 'unconnected items'."""
    from pcbc.route import _krt_summary

    log = 'noise\nJSON_SUMMARY_MIN: {"failed": 3, "failed_single": ["VSS"], "multipoint_deficit": 3, "open_single": [], "routed": 5, "vias": 2}\nEXIT=0\n'
    assert _krt_summary(log) == {"failed": 3, "failed_single": ["VSS"], "multipoint_deficit": 3, "open_single": [], "routed": 5, "vias": 2}
    assert _krt_summary("nothing here") == {}


def test_a_pad_krt_left_open_is_reported_whichever_field_names_it():
    """The DS2 Addon's ground pin: route.py said `pad_pairs_open: {nets: [GND]}` with an empty
    `failed_single`, route_planes.py said `unconnected pad U1 on 'GND' at (22.18, 15.07)`, and
    pcbc read neither: 'unrouted: []', then the gate found the open item."""
    from pcbc.route import _krt_summary, _unreached_pads

    log = 'JSON_SUMMARY_MIN: {"failed": 1, "failed_single": [], "multipoint_deficit": 1, "open_single": [], "pad_pairs_open": {"count": 1, "nets": ["GND"]}, "routed": 0, "vias": 26}\n'
    assert _krt_summary(log)["pad_pairs_open"] == {"count": 1, "nets": ["GND"]}
    pour = "      GND: 4/5 pads connected to plane on B.Cu\n          unconnected pad U1 on 'GND' at (22.18, 15.07) [F.Cu]\n"
    assert _unreached_pads(pour) == [("U1", "GND", "22.18", "15.07")]
    assert _unreached_pads("all connected") == []
    # The pour's list only annotates a net a route step left open: KRT's bare pour defers every
    # tap to the route step after it (node: 55 GND pads "unconnected", all welded a step later).
    from pcbc.compile import compile_design
    from pcbc.language import load_board
    from pcbc.route import _unrouted_move

    design = load_board(Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py")
    move = _unrouted_move(compile_design(design), design, "GND", [f"P{i} at (0, {i})" for i in range(9)])
    assert "the pour could not reach P0 at (0, 0), " in move and "P5 at (0, 5), ..." in move and "P6" not in move
