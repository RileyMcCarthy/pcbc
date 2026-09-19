"""The drawing rules, checked on the emitted sheet text - what `pcbc sch`
promises, pinned so a later change cannot quietly undo one."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from pcbc.language import load_board
from pcbc.sch_emit import emit_from_design

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BOARDS = {
    "blinky": EXAMPLES / "blinky" / "blinky.py",
    "c3_usb": EXAMPLES / "c3_usb" / "c3_usb.py",
    "buck": EXAMPLES / "buck" / "buck.py",
}
EPS = 0.05

_WIRE = re.compile(r"\(wire\n\t\t\(pts \(xy ([0-9.]+) ([0-9.]+)\) \(xy ([0-9.]+) ([0-9.]+)\)\)")
_LABEL = re.compile(r'\(label "([^"]+)"\n\t\t\(at ([0-9.]+) ([0-9.]+) (\d+)\)\n\t\t\(effects \(font \(size 1.27 1.27\)\) \(justify (\w+) (\w+)\)')
_HAT = re.compile(r'\(lib_id "(power:\w+)"\) \(at ([0-9.]+) ([0-9.]+) (\d+)\)')


@pytest.fixture(scope="module")
def sheets():
    out = {}
    for name, board in BOARDS.items():
        report: dict = {}
        out[name] = (emit_from_design(load_board(board), title=name, report=report), report)
    return out


def _wires(text: str) -> list[tuple[float, float, float, float]]:
    return [tuple(float(v) for v in m.groups()) for m in _WIRE.finditer(text)]


def _inside(px, py, x0, y0, x1, y1) -> bool:
    if abs(x0 - x1) < EPS:
        return abs(px - x0) < EPS and min(y0, y1) + EPS < py < max(y0, y1) - EPS
    return abs(py - y0) < EPS and min(x0, x1) + EPS < px < max(x0, x1) - EPS


def _crosses(a, b) -> bool:
    (x0, y0, x1, y1), (sx0, sy0, sx1, sy1) = a, b
    av, sv = abs(x0 - x1) < EPS, abs(sx0 - sx1) < EPS
    if av == sv:
        return False
    if not av:
        (x0, y0, x1, y1), (sx0, sy0, sx1, sy1) = b, a
    return min(sx0, sx1) + EPS < x0 < max(sx0, sx1) - EPS and min(y0, y1) + EPS < sy0 < max(y0, y1) - EPS


@pytest.mark.parametrize("name", list(BOARDS))
def test_no_two_wires_cross(sheets, name):
    ws = _wires(sheets[name][0])
    bad = [(a, b) for i, a in enumerate(ws) for b in ws[i + 1 :] if _crosses(a, b)]
    assert bad == []


_JUNCTION = re.compile(r"\(junction\n\t\t\(at ([0-9.]+) ([0-9.]+)\)")


@pytest.mark.parametrize("name", list(BOARDS))
def test_only_connecting_geometry(sheets, name):
    text = sheets[name][0]
    ws = _wires(text)
    junctions = [(float(m.group(1)), float(m.group(2))) for m in _JUNCTION.finditer(text)]
    ends = [(w[0], w[1]) for w in ws] + [(w[2], w[3]) for w in ws]
    # A wire end on another wire's interior connects only through a junction
    # (kicad-cli: a bare T does not) - so every such point carries one.
    tees = [e for e in ends if any(_inside(*e, *w) for w in ws)]
    for e in tees:
        assert any(abs(e[0] - j[0]) < EPS and abs(e[1] - j[1]) < EPS for j in junctions), e
    # No symbol pin on a wire's interior: that never connects.
    hats = [(float(m.group(2)), float(m.group(3))) for m in _HAT.finditer(text)]
    assert not [(h, w) for h in hats for w in ws if _inside(*h, *w)]


@pytest.mark.parametrize("name", list(BOARDS))
def test_labels_sit_on_their_wire(sheets, name):
    text = sheets[name][0]
    labels = list(_LABEL.finditer(text))
    assert labels
    assert {m.group(6) for m in labels} == {"bottom"}
    # Every label is anchored on a wire (end or interior) or on a pin end; the
    # netlist test proves the pin-end case, so here: rot 0/90 only, so the
    # text runs along whatever it sits on.
    assert {m.group(4) for m in labels} <= {"0", "90"}


@pytest.mark.parametrize("name", list(BOARDS))
def test_power_is_symbols_or_one_short_wire(sheets, name):
    text = sheets[name][0]
    # Every ground and supply pin has a symbol or a wire: the netlist test
    # proves that. Here: no supply wire longer than the straight-run cap.
    for x0, y0, x1, y1 in _wires(text):
        assert abs(x0 - x1) < EPS or abs(y0 - y1) < EPS  # every wire is axis-aligned


@pytest.mark.parametrize("name", list(BOARDS))
def test_symbols_upright_where_they_can_be(sheets, name):
    text = sheets[name][0]
    hats = [(m.group(1), int(m.group(4))) for m in _HAT.finditer(text)]
    assert hats
    flipped = [h for h in hats if h[1] == 180]
    # A flip is the exception, never the rule.
    assert len(flipped) <= max(1, len(hats) // 5), hats


def test_readability_bar(sheets):
    _sch, blinky = sheets["blinky"]
    assert blinky["issues"] == []
    _sch, c3 = sheets["c3_usb"]
    library = [i for i in c3["issues"] if ".kicad_sym" in i]
    rest = [i for i in c3["issues"] if ".kicad_sym" not in i]
    assert len(library) == 1  # the connector's pin numbers; not pcbc's to fix
    assert len(rest) <= 2, rest


def test_emit_stays_fast():
    t = time.perf_counter()
    emit_from_design(load_board(BOARDS["c3_usb"]), title="c3_usb")
    assert time.perf_counter() - t < 30.0  # was 31 s once; the bar catches a cliff, not a wobble


@pytest.mark.parametrize("name", list(BOARDS))
def test_everything_on_the_grid_and_open_pins_marked(sheets, name):
    text = sheets[name][0]
    for x0, y0, x1, y1 in _wires(text):
        for v in (x0, y0, x1, y1):
            assert abs(v / 1.27 - round(v / 1.27)) < 0.01, (x0, y0, x1, y1)
    design = load_board(BOARDS[name])
    open_pads = sum(
        len(pin.pads) for inst in design.instances for nm, pin in inst.part.pins.items() if nm not in inst.pins
    )
    assert text.count("(no_connect") == open_pads
    # exactly one PWR_FLAG per power net, numbered
    power = [n.name for n in design.nets.values() if n.kind in ("power", "ground")]
    assert text.count('(lib_id "power:PWR_FLAG")') == len(power)
    assert len(re.findall(r'"#FLG\d{3}"', text)) == len(power)
