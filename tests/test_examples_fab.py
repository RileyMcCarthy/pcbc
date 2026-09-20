"""Every example board goes from board.py to a JLC package: placed, routed, judged by KiCad, fabbed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbc.build import build_job
from pcbc.language import load_board
from pcbc.netcheck import check_copper

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


# Recorded 2026-09-19 with KRT 0.21.4; every number is a ceiling. Lower them when the router improves.
BAR = {
    # Re-recorded 2026-09-20 from a fresh build of every board, after two R2 S1 changes:
    # the escape stubs were straightened (the via's across-coordinate was snapped to KRT's grid
    # while the pad's was not), and a custom pad is now read as the copper it draws rather than
    # the anchor it declares (the USB-C's four shield pads declare 5 um and draw 0.7 x 1.4 mm).
    # Seeing those pads brought them into the closed rows, so a part standing on a board edge is
    # now left out of the fanout: its pads already face inward and an escape there buys nothing.
    # c3_usb comes out better on vias (16 -> 13), micro (183 -> 139) and length, and worse on
    # off-45 (5 -> 7) and its pair's detour (1.81 -> 1.92); node trades one via for a few micro
    # segments. Every number is a ceiling; lower them as R2's patterns land.
    "buck": {"vias": 4, "off45": 3, "micro": 26, "detour": 2.09},
    "c3_usb": {"vias": 13, "off45": 7, "micro": 139, "detour": 1.92},
    "node": {"vias": 28, "off45": 63, "micro": 181, "detour": 1.72},
}

# R1 (docs/r1-design.md E, H.3): the soft rules' hits per example, {rule name: KiCad warnings}, recorded
# 2026-09-20 with KRT 0.21.4 and KiCad 10.0.6. Exact, not a ceiling: a soft rule that hits zero on the
# four examples and the DS2 Addon is promoted to an error in the PR that shows the zeros; the rest wait
# for R4's tuning pass. A number going up here is a regression; going down is a candidate for promotion.
#
# What the numbers say: `width_*` hits are KRT's fanout stubs and pad necks at track_min on power nets
# (and node's whole USB pair, which KRT routes narrower than the 0.2291 mm class: an R4 item); the one
# skew hit per USB pair is KRT's untuned pair (c3_usb: DN 46.6 vs DP 35.0 mm against the 0.5 mm budget);
# the one uncoupled hit is the same pair's 42 mm of uncoupled length against 2 mm; c3_usb's USB_DP
# carries 3 vias against the budget of 2. No soft rule is at zero everywhere, so none is promoted yet.
# Re-recorded with BAR, from the same fresh build. The escape stubs' straightening and the
# truthful custom pads move what KRT routes, and with it how often it necks a power track below
# its class (`width_power`) or lays the pair narrow (`width_usb`).
SOFT = {
    "buck": {"width_power": 40},
    "c3_usb": {"width_power": 34, "vias_usb_dn": 0, "vias_usb_dp": 0, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
    "node": {"width_usb": 74, "width_power": 44, "vias_usb_dn": 0, "vias_usb_dp": 0, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
}


@pytest.mark.kicad
@pytest.mark.krt
@pytest.mark.parametrize("name", ["buck", "c3_usb", "node"])
def test_example_builds_to_fab(tmp_path: Path, name: str):
    src = EXAMPLES / name
    board = tmp_path / f"{name}.py"
    board.write_text((src / f"{name}.py").read_text())
    shutil.copytree(src / "components", tmp_path / "components")
    result = build_job(board, upto="fab", force=True)
    assert result.get("error") is None, result.get("error")
    # The copper bar, recorded when the plan last changed: a change that makes copper uglier
    # (more vias, more staircases, a longer detour) fails here, as schematic readability does.
    route = next(st for st in result["steps"] if st["stage"] == "route")
    totals = route["copper_bar"]["totals"]
    bar = BAR[name]
    for key in ("vias", "off45", "micro"):
        assert totals[key] <= bar[key], (name, key, totals[key], bar[key], route["copper_bar"]["lines"])
    assert totals["worst_detour"][1] <= bar["detour"] + 0.05, (name, totals["worst_detour"], route["copper_bar"]["lines"])
    assert route["geometry"]["segments"] <= bar["micro"] + 5  # KiCad's own count of the same staircases
    route = next(s for s in result["steps"] if s.get("stage") == "route")
    assert route["router"] == "krt" and route["unrouted"] == [] and route["copper"] == "verified", route
    place = next(s for s in result["steps"] if s.get("stage") == "place")
    assert place["layout"] == [], place["layout"]
    fab = tmp_path / "layout" / name / "fab"
    assert (fab / "bom.csv").exists() and (fab / "cpl.csv").exists()
    assert any((fab / "gerbers").glob("*")), "no gerbers"
    routed_pcb = tmp_path / "layout" / name / "routed" / "layout.kicad_pcb"
    routed = routed_pcb.read_text()
    assert "filled_polygon" in routed, "the gate judged unfilled pours"
    # The soft rules (E, H.3) beside the bar: warnings KiCad raised from pcbc's own soft rules, pinned per example.
    gate = check_copper(load_board(board), routed_pcb, refill=False)
    assert gate["canary"], "the canary rule must fire on every board with copper"
    assert gate["soft"] == SOFT[name], (name, gate["soft"], gate["rules"])
