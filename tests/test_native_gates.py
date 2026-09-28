"""Gates and locks the example boards cannot exercise while they stop before fab (the second refutation
round, majors 2, 3 and 5). Each is built from a small fixture board, so the branch runs end to end
inside `pcbc build`, and each test was shown to fail under a mutation that blinds what it pins
(`scratchpad/native/fix2/mut/`).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbc.build import build_job

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIXTURES = ROOT / "tests" / "fixtures"


def _copy(tmp_path: Path, src: Path, edits=()) -> Path:
    text = src.read_text()
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    out = tmp_path / src.name
    out.write_text(text)
    return out


def _route(result: dict) -> dict:
    return next(s for s in result["steps"] if s.get("stage") == "route")


# --- major 3: the pair gate is fatal -----------------------------------------------------------------


@pytest.mark.kicad
def test_an_uncoupled_pair_stops_a_board_that_routes_everything(tmp_path: Path):
    """`build._pair_gate` on a board with nothing else wrong: every link routed, 0 DRC errors, the pair
    routed as one object (`route_pairs`, a chain of one length on each half) and still 4.1065 mm
    uncoupled where its two halves leave pads 4 mm apart. Against `uncoupled_mm=1` the build stops with
    the gate's sentence and writes no fab package; the same board against 40 mm reaches fab. Measured
    2026-09-25 (`scratchpad/native/fix2/pair/`)."""
    board = _copy(tmp_path, FIXTURES / "pair" / "pair.py")
    result = build_job(board, upto="fab", force=True)
    route = _route(result)
    assert not route.get("unrouted") and route["copper"] == "verified", (route.get("unrouted"), route.get("copper"))
    assert route["route_stats"]["pairs"] == {"D_N/D_P": [1, 1]}, route["route_stats"].get("pairs")
    err = result["error"] or ""
    assert err.startswith("a differential pair is not coupled: pair D_N/D_P: ") and "uncoupled 4.1065 mm against 1" in err, err
    assert route["pairs"]["pairs"]["D_N/D_P"]["uncoupled"] == 4.1065, route["pairs"]["pairs"]
    assert len(route["pairs"]["fails"]) == 1 and not result.get("ok")
    assert not (tmp_path / "layout" / "pair" / "fab").exists(), "a board the pair gate stops ships no package"

    other = tmp_path / "wide"
    other.mkdir()
    board = _copy(other, FIXTURES / "pair" / "pair.py", [("UNCOUPLED_MM = 1.0", "UNCOUPLED_MM = 40.0")])
    result = build_job(board, upto="fab", force=True)
    assert result["error"] is None and result.get("ok"), result["error"]
    route = _route(result)
    assert route["pairs"]["fails"] == [] and route["pairs"]["pairs"]["D_N/D_P"]["uncoupled"] == 4.1065, route["pairs"]


# --- major 5: the thermal array's fab note --------------------------------------------------------


@pytest.mark.kicad
def test_a_thermal_array_reaches_fab_and_its_note_orders_the_holes_filled(tmp_path: Path):
    """`fab._thermal_notes` on a board that reaches fab with a `Thermal()` array: the vias pcbc put
    inside the ESP32-C3-MINI's exposed land are listed with their arithmetic, and the order is told to
    specify epoxy-filled and capped vias (IPC-4761 Type VII), because a tented barrel under a reflowed
    land wicks the paste. No example board reaches fab with a land (c3_usb and node stop at `VBUS`),
    so this branch was dead until this fixture. Measured 2026-09-25: 24 barrels, 9.63 K/W, 9.63 C at
    1 W against a 10 C budget."""
    board = _copy(tmp_path, FIXTURES / "thermal" / "thermal.py")
    shutil.copytree(EXAMPLES / "c3_usb" / "components" / "Espressif", tmp_path / "components" / "Espressif")
    result = build_job(board, upto="fab", force=True)
    assert result["error"] is None and result.get("ok"), result["error"]
    thermal = _route(result)["thermal"]
    rows = {r["pad"]: (r["want"], r["got"], r["in_pad"], r["verdict"]) for r in thermal["arrays"]}
    assert rows == {"U1.49": (24, 24, 24, "served")}, thermal["arrays"]
    assert thermal["fails"] == [] and thermal["blockers"] == 0, thermal
    notes = (tmp_path / "layout" / "thermal" / "fab" / "FAB_NOTES.md").read_text()
    assert "## Thermal vias (via-in-pad, deliberate)" in notes
    assert "- **U1.49** on `GND`: 24 x 0.5/0.3 mm through vias to the GND plane on `B.Cu`." in notes, notes
    assert "so 24 in parallel are 9.63 K/W and 1 W raises the copper 9.63 C against a 10 C budget" in notes
    assert "**The order must specify via covering = Epoxy Filled & Capped (IPC-4761 Type VII)** for these barrels" in notes
    assert "The passive rule is unchanged and unrelaxed" in notes


# --- major 2: a valid lock's free end stays joined ---------------------------------------------------


@pytest.mark.kicad
def test_a_lock_whose_two_free_ends_the_router_joins_to_one_pad_builds(tmp_path: Path):
    """buck with one `layout.core.py` line, `Seg("GND", (24, 2), (24, 6.5))`: a track whose two ends
    touch nothing. Each free end is a routing terminal (`route_native.EndTerminal`); the router joins
    both to the same GND pad, a loop (pad, link, lock, link, pad). The relaxer's `trim` used to drop
    one of the two links — the run still touched the lock at its other end, which is all the old rule
    asked — and the build then refused the valid lock as dangling, blaming the author's line
    ("the build's copper did not continue from it"; the second refutation round, major 2). Now the
    relaxer holds every free end of immovable copper that only the run keeps joined
    (`route_relax.free_ends_held`), and the board reaches fab with both links in place."""
    src = EXAMPLES / "buck"
    board = _copy(tmp_path, src / "buck.py")
    shutil.copytree(src / "components", tmp_path / "components")
    (tmp_path / "layout.core.py").write_text('Seg("GND", (24.0, 2.0), (24.0, 6.5), width=0.3, layer="F.Cu")\n')
    result = build_job(board, upto="fab", force=True)
    assert result["error"] is None and result.get("ok"), result["error"]
    route = _route(result)
    assert route["locked"] == ["seg-GND-0"], route["locked"]
    assert not [k for k in route["drc_by_type"] if k.endswith(("track_dangling", "via_dangling"))], route["drc_by_type"]
    gen = (tmp_path / "layout" / "buck" / "routed" / "layout.gen.py").read_text()
    for end in ("(24.0, 2.0)", "(24.0, 6.5)"):
        assert any(line.startswith('Seg("GND"') and end in line for line in gen.splitlines()), (end, "the router's link to this free end is in layout.gen.py")


def test_the_relaxer_keeps_a_free_end_it_alone_joins():
    """`route_relax.free_ends_held`, asked directly: a run landing on the free end of an immovable track
    gets an anchor at that end; the same run landing where a pad also holds the end gets none (it may
    slide along the stub, as before)."""
    from pcbc.route_geom import rect_shape
    from pcbc.route_relax import Chain, free_ends_held
    from pcbc.route_scene import Item

    lock = {"net": "GND", "layer": "F.Cu", "start": (24.0, 2.0), "end": (24.0, 6.5), "width": 0.3, "length": 4.5, "locked": True}
    run = Chain("GND", "F.Cu", 0.3, "route", ((24.0, 2.0), (21.5771, 4.4229), (21.5771, 6.454)), (("k",),))
    assert free_ends_held(run, [], [], [lock], frozenset()) == [((24.0, 2.0), 0.0)]
    pad = Item(0, "pad", "GND", frozenset({"F.Cu"}), rect_shape(24.0, 2.0, 0.6, 0.6), None, None, "X1.1")
    assert free_ends_held(run, [pad], [], [lock], frozenset()) == []
