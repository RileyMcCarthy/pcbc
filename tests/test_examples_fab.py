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
    # Re-recorded 2026-09-20 for R2 S4, the slice where pcbc started writing the copper itself: the
    # hop pattern claims every two-pad net whose link it can draw, locked, before KRT runs, and
    # `write_fab_overrides` now writes the smallest class clearance as KRT's floor instead of the
    # stackup's absolute minimum (C.5). `PCBC_PATTERNS=off` reproduces the S1b row on all five boards
    # exactly, so every move below is the hops and nothing else; the numbers and the reasons are in
    # `docs/r2-measurements.md` S4.
    #
    # `vias` is now `vias_leftover` in spirit — the tap pattern (S5) will place pattern vias by design
    # and the total would then pin the wrong thing — but until S5 exists the two are the same number
    # on every board but c3_usb and ds2, whose fanout vias are pcbc's own and always were.
    #
    # node and ds2 improve and are tightened. c3_usb gets worse on two numbers and better on a third,
    # and the reason is recorded rather than papered over: locking CC1, CC2 and LED shortens the USB
    # pair (46.3 -> 43.7 mm, detour 1.92 -> 1.81) and KRT's **pair** router then staircases more
    # (USB_DN 22 -> 44 micro segments, USB_DP 15 -> 23, and one more via on USB_DP). The pair is
    # 14.3 % of the copper that R2 explicitly does not touch and R4 owns; see open_issues in S4.
    "blinky": {"vias": 0, "off45": 0, "micro": 0, "detour": 1.04},
    "buck": {"vias": 4, "off45": 3, "micro": 26, "detour": 2.09},
    "c3_usb": {"vias": 14, "off45": 7, "micro": 168, "detour": 1.81},
    "node": {"vias": 28, "off45": 57, "micro": 154, "detour": 1.72},
}

# C.6: the refusal count per board, **exact**, so a new refusal is a test failure and cannot drift
# into being ignored. Zero hard refusals everywhere — a hop refusal is never hard, because KRT's own
# constrained `*_nets` step honours the same intent (C.6, and `patterns/hop.py`'s `_refuse`).
REFUSED = {"blinky": {}, "buck": {"hop": 2}, "c3_usb": {"hop": 1}, "node": {"hop": 1}}

# D.4: what pcbc owns, exact per board — {reason: (segments, vias)}. A pattern that stops claiming a
# net shows up here before it shows up in the bar.
OWNS = {
    "blinky": {"hop": (5, 0)},
    "buck": {},
    "c3_usb": {"fanout": (5, 5), "hop": (11, 0)},
    "node": {"hop": (11, 0)},
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
# Re-recorded 2026-09-20 for R2 S4. node's `width_usb` falls 74 -> 57 because the hops take three
# short nets out of KRT's way and it lays the pair closer to its class width. c3_usb's `width_power`
# rises 34 -> 36 and `vias_usb_dp` 0 -> 1 (three vias against a budget of two) for the reason recorded
# in BAR: the pair is KRT's until R4, and locking the CC and LED hops changes how it routes it. That
# is a rise, which F.3 item 7 forbids, so it is in S4's open issues and not in a footnote.
SOFT = {
    "blinky": {"width_power": 0},
    "buck": {"width_power": 40},
    "c3_usb": {"width_power": 36, "vias_usb_dn": 0, "vias_usb_dp": 1, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
    "node": {"width_usb": 57, "width_power": 44, "vias_usb_dn": 0, "vias_usb_dp": 0, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
}


@pytest.mark.kicad
@pytest.mark.krt
@pytest.mark.parametrize("name", ["blinky", "buck", "c3_usb", "node"])
def test_example_builds_to_fab(tmp_path: Path, name: str):
    src = EXAMPLES / name
    board = tmp_path / f"{name}.py"
    board.write_text((src / f"{name}.py").read_text())
    if (src / "components").exists():
        shutil.copytree(src / "components", tmp_path / "components")  # blinky's two parts carry no 3D models
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
    # R2 C.6 and D.4: the refusals are exact, nothing pcbc wrote is hard-refused, and the copper the
    # patterns own is what the census says it is. A pattern that stops claiming a net fails here.
    assert route["refused"] == REFUSED[name], (name, route["refused"], route["pattern_moves"])
    assert not any(r["hard"] for r in route["refusals"]), route["refusals"]
    owns = {r: (v["segments"], v["vias"]) for r, v in route["copper_bar"]["totals"]["by_reason"].items() if r != "leftover"}
    assert owns == OWNS[name], (name, owns, route["copper_bar"]["lines"])
    from pcbc.route_emit import read_sidecar

    doc = read_sidecar(tmp_path / "layout" / name / "routed" / "copper.json")
    final = (tmp_path / "layout" / name / "routed" / "layout.kicad_pcb").read_text()
    assert bool(doc.items) == bool(owns), (name, len(doc.items), owns)
    assert all(i["uuid"] in final for i in doc.items), "D.4: a piece is traceable from copper.json into the board it is in"
    assert {r: (v["segments"], v["vias"]) for r, v in doc.census.items() if r != "leftover"} == OWNS[name], doc.census
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
