"""Escapes for closed pad rows are pcbc's own copper, from the stackup's numbers, locked."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from pcbc.build import pcb_job
from pcbc.compile import compile_design
from pcbc.fanout import fanout_copper
from pcbc.language import load_board
from pcbc.stackup import get_stackup

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _fanned(tmp_path: Path, board: str):
    shutil.copytree(EXAMPLES / board, tmp_path / board)
    result = pcb_job(tmp_path / board / f"{board}.py")
    assert result.get("error") is None, result
    design = load_board(tmp_path / board / f"{board}.py")
    job = compile_design(design)
    placed = Path(result["placed"])
    text = placed.read_text()
    out, notes = fanout_copper(design, job, text, "t")
    return design, job, placed, text, out, notes


def test_closed_rows_get_staggered_escape_vias_and_pairs_keep_their_pads(tmp_path: Path):
    """c3_usb: the USB-C's 0.5 mm pads, the LDO's and the ESD diode's SOT-23 rows are closed; the
    ESP32 module (0.8 mm pads, 0.4 mm gaps) is not. The USB pair keeps its pads bare.
    KRT's qfn_fanout staggered neighbouring vias by copper clearance alone (holes 0.42 mm apart
    on a 0.65 mm row; JLC wants 0.5) and hunted past the decaps with 3.5 mm stubs when the
    second row did not fit: the DS2 Addon failed KiCad's clearance check on the stub."""
    design, job, placed, text, out, notes = _fanned(tmp_path, "c3_usb")
    assert {n["ref"] for n in notes} == {"J1", "U2", "U3"}, notes
    assert not any(n["net"] in ("USB_DP", "USB_DN") for n in notes)
    stack = get_stackup(job.stackup)
    vias = [n["via"] for n in notes]
    for i, a in enumerate(vias):
        for b in vias[i + 1 :]:
            assert math.dist(a, b) >= stack.via_drill + stack.hole_to_hole - 1e-6, (a, b)
    assert out.count("\n\t(via") == text.count("\n\t(via") + len(notes)
    assert out.count("\n\t(segment") == text.count("\n\t(segment") + len(notes)
    assert out.count("(locked yes)") >= 2 * len(notes)
    grid = 0.05 if job.layers <= 2 else 0.1
    for x, y in vias:  # on KRT's grid along the escape, so the router's terminals are on-grid
        assert abs(round(x / grid) * grid - x) < 1e-6 or abs(round(y / grid) * grid - y) < 1e-6, (x, y)
    again, _ = fanout_copper(design, job, text, "t")
    assert again == out  # deterministic


def test_a_board_without_closed_rows_is_left_alone(tmp_path: Path):
    _design, _job, _placed, text, out, notes = _fanned(tmp_path, "blinky")
    assert notes == [] and out == text


@pytest.mark.kicad
def test_kicad_finds_no_copper_error_in_the_fanned_board(tmp_path: Path):
    """The stubs and vias clear every pad and each other by the board's own rules, hole-to-hole
    included (an error in pcbc's project, a warning in KiCad's defaults)."""
    from pcbc.fab import copper_drc_errors
    from pcbc.netcheck import kicad_drc
    from pcbc.project import copy_with_siblings

    _design, _job, placed, _text, out, notes = _fanned(tmp_path, "c3_usb")
    fan = tmp_path / "fan" / "layout.kicad_pcb"
    copy_with_siblings(placed, fan)
    fan.write_text(out)
    doc = kicad_drc(fan, refill=False)
    errors = copper_drc_errors(doc)
    assert errors == [], errors[:3]
    assert not [v for v in doc.get("violations", []) if v.get("type") == "hole_to_hole"]
    assert notes
