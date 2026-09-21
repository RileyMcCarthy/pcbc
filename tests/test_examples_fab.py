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
    # Re-recorded 2026-09-20 for R2 S5, the slice where pcbc welds every SMD plane pad to its plane
    # itself: the tap pattern runs between KRT's `planes` and `signals` steps, and KRT's own
    # `plane_taps` step then runs only for the nets pcbc refused a pad of (C.4). `PCBC_PATTERNS=off`
    # reproduces the S1b row on all five boards exactly, so every move below is the hops and the taps
    # and nothing else; the numbers and the reasons are in `docs/r2-measurements.md` S5.
    #
    # **`vias` is now `vias_leftover`** — D.4's deliberate split. The old total was recorded to catch
    # KRT's staircase vias, and a tap via is the point of a four-layer board: holding node to 28 total
    # vias means holding forty-seven ground pads off the ground plane. So the ceiling is on the vias
    # the leftover router places, which is what it was always about, and `vias_pattern` below is
    # **exact** per reason, like `SOFT` and the refusal counts.
    #
    # **Five ceilings were raised by S5 and three of them were not written down**, which is what the
    # sentence that stood here ("every ceiling improves or holds") claimed and what the S5 review
    # measured (findings 5 and 15): buck micro 26 -> 37 and c3_usb 168 -> 204 (KRT staircasing the
    # leftover around six and thirty-four new locked vias) were recorded; c3_usb off45 7 -> 11, all
    # four of them GND, and `angles` — pcbc's own 135-degree corner rule, which tripled on three
    # boards — were not. `angles` is `track_angle` out of the gate's own `geometry` dict: a warning,
    # never an error, and a ceiling here rather than the silence it was.
    #
    # Re-recorded 2026-09-20 from fresh builds for the review's own fixes, whose reasons are in
    # `docs/r2-measurements.md` S5r. node improves on every count in this table but one — vias 14 ->
    # **11**, off45 31 -> **30**, micro 67 -> **62**, angles 104 -> **98** — because the taps that
    # would have slotted the 3V3 plane now step out of line, and KRT has an easier board to finish;
    # its `detour` rises 1.72 -> **1.82** on `USB_DN`, the pair the signals step re-lays and R4 owns,
    # and that is the one number of the slice's fixes that got worse. c3_usb's off45 11 -> **13** and
    # micro 204 -> **203** are its leftover answering 34 taps moved 0.1 um by the EPS fix (finding 8):
    # a 100 nm move, two more staircases, which is what a grid router's sensitivity looks like.
    "blinky": {"vias": 0, "off45": 0, "micro": 0, "detour": 1.04, "angles": 0},
    "buck": {"vias": 1, "off45": 3, "micro": 37, "detour": 2.09, "angles": 32},
    "c3_usb": {"vias": 8, "off45": 13, "micro": 203, "detour": 1.81, "angles": 122},
    "node": {"vias": 11, "off45": 30, "micro": 62, "detour": 1.82, "angles": 98},
}

# D.5 as a number a deleted fragment can move. `plane_islands` cannot see the failure it is
# documented to watch: with `island_removal_mode 0` a cut-off fragment is **deleted**, so it is never
# written as a second `filled_polygon` and the count stays at 1 (S5 review, finding 9 — ds2 silently
# drops five orphan GND islands, 8.52 mm2, and reports one island). The area moves; the count does
# not. The **key set** is the other half: `set(...values()) <= {1}` was True of an empty dict, so a
# plane that came back unfilled passed in silence (finding 17). mm2 of filled copper, KiCad 10.0.6,
# compared to 0.05 mm2 — KiCad fills in integer nm so the polygon is exact, and the tolerance is for
# the last digit of a shoelace sum over a few thousand points, not for the fill wandering.
PLANES = {
    "blinky": {("GND", "B.Cu"): 917.25},
    "buck": {("GND", "B.Cu"): 935.59},
    "c3_usb": {("GND", "B.Cu"): 1050.50},
    "node": {("GND", "In1.Cu"): 2533.39, ("3V3", "In2.Cu"): 2520.84},
}

# Finding 14: how many of each board's `track_width` warnings are pcbc's own tap stubs. `tap._width`
# narrows the class width to the pad's across dimension — `fanout.py`'s own neck — and the width rule
# has no such exemption, so the stub is a soft hit like any other necked track. The comment under
# SOFT used to say "no tap stub is in these counts"; buck's `R_FB_BOT.2` at 0.64 mm against
# `width_power`'s 0.781 and node's `U4.2`, `U4.6` and `U4.7` at 0.364 against 0.4 were in them.
TAP_NECKED = {"blinky": 0, "buck": 1, "c3_usb": 0, "node": 3}

# Finding 4: pcbc's own vias that sit inside **another** footprint's courtyard. KiCad's courtyard
# rules are footprint-to-footprint, so DRC says nothing; a tented via under a part's keep-out is a
# placement note and not a refusal, and H.1's third decision is that it gets a number. Two were found
# at S5 — c3_usb's tap for `J1.A1B12` inside `SW_RST`'s and node's for `C_MCU.1` inside `U1`'s — and
# node's is gone, because the antipad rule of finding 10 moved that tap.
COURTYARD = {"blinky": 0, "buck": 0, "c3_usb": 1, "node": 0}

# Finding 16: per-net detour, because `worst_detour` is a max and a max hides every net under it —
# c3_usb's VBUS went 1.33 -> 1.70 through S5 while the reported worst held at USB_DN 1.81. Ceilings,
# for every net whose airwire is at least 1 mm. A net that creeps here is a test failure now.
DETOURS = {
    "blinky": {"LED": 1.04},
    "buck": {"5V": 1.12, "BOOT": 1.0, "EN": 1.09, "FB": 1.01, "GND": 1.31, "SW": 1.53, "VIN": 2.09},
    "c3_usb": {"3V3": 1.38, "BOOT": 1.05, "CC1": 1.2, "CC2": 1.08, "EN": 1.04, "GND": 1.44, "LED": 1.19, "LED_A": 0.79, "USB_DN": 1.81, "USB_DP": 1.62, "VBUS": 1.7},
    "node": {
        "3V3": 0.13, "BOOT": 1.12, "CC1": 1.0, "CC2": 1.24, "DRV": 1.08, "EN": 1.09, "GATE": 1.03, "GND": 0.43,
        "LED": 1.12, "LED_A": 1.0, "LOAD": 1.0, "SCL": 1.04, "SDA": 1.09, "T_DIV": 1.03, "T_OUT": 1.72,
        "USB_DN": 1.82, "USB_DP": 1.53, "VBUS": 1.27,
    },
}

# Finding 1: every passive pad on each board, counted, so the probe below measures the guard rather
# than assuming it.
PASSIVE_PADS = {"blinky": 4, "buck": 18, "c3_usb": 26, "node": 42}

# D.4: the vias pcbc placed itself, **exact** per reason. A pattern that stops claiming a pad shows
# up here before it shows up anywhere else. Measured 2026-09-20 (`docs/r2-measurements.md` S5).
VIAS_PATTERN = {
    "blinky": {"tap": 1},
    "buck": {"tap": 6},
    "c3_usb": {"fanout": 5, "tap": 34},
    "node": {"tap": 62},
}


# C.6: the refusal count per board, **exact**, so a new refusal is a test failure and cannot drift
# into being ignored. Zero hard refusals everywhere — a hop refusal is never hard, because KRT's own
# constrained `*_nets` step honours the same intent (C.6, and `patterns/hop.py`'s `_refuse`).
REFUSED = {"blinky": {}, "buck": {"hop": 2}, "c3_usb": {"hop": 1, "tap": 2}, "node": {"hop": 1, "tap": 2}}

# B.3's refusals, pad by pad, with the rule that decided each. A tap refusal is soft: KRT's own
# `plane_taps` step runs for exactly these nets and welds exactly these pads, which is why node's
# `06_plane_taps` adds two vias to the post stage's 62 and c3_usb's pads fall to the pour.
TAP_REFUSED = {
    "blinky": [],
    "buck": [],
    "c3_usb": [("C_EN.2", "copper"), ("R_CC1.2", "copper")],
    "node": [("C_VBUS.2", "copper"), ("U1.51", "edge")],
}

# D.4: what pcbc owns, exact per board — {reason: (segments, vias)}. A pattern that stops claiming a
# net shows up here before it shows up in the bar.
OWNS = {
    "blinky": {"hop": (5, 0), "tap": (1, 1)},
    "buck": {"tap": (6, 6)},
    "c3_usb": {"fanout": (5, 5), "hop": (11, 0), "tap": (34, 34)},
    "node": {"hop": (11, 0), "tap": (62, 62)},
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
# Re-recorded 2026-09-20 for R2 S5. node's `width_power` falls 44 -> 20: the taps carry GND and 3V3
# to their planes at the class width, so KRT necks far less of what is left. c3_usb's rises 36 -> 49
# and node's `width_usb` by one, both of them KRT's own leftover copper routed around thirty-four new
# locked vias — a rise, which F.3 item 7 forbids, so it is in S5's open issues and not in a footnote.
#
# Re-recorded again for the S5 review. node's fall further — `width_usb` 58 -> 46 and `width_power`
# 20 -> 15 — because the taps now step out of line where a row would slot the plane below it, and KRT
# lays what is left more freely; c3_usb's `width_power` rises 49 -> 51, its leftover answering 34 taps
# moved by 0.1 um. Both are in S5's open issues with the rest.
#
# **Tap stubs ARE in these counts**, which the sentence here used to deny (finding 14): `tap._width`
# necks the class width to the pad's across dimension — `fanout.py`'s own neck — and the width rule
# has no exemption for it, so buck's `R_FB_BOT.2` at 0.64 mm and node's `U4.2`, `U4.6` and `U4.7` at
# 0.364 mm are four of the 54 combined `width_power` hits. `TAP_NECKED` pins how many, matched to the
# stubs by position and length, so the claim is a number rather than a sentence.
SOFT = {
    "blinky": {"width_power": 0},
    "buck": {"width_power": 39},
    "c3_usb": {"width_power": 51, "vias_usb_dn": 0, "vias_usb_dp": 1, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
    "node": {"width_usb": 46, "width_power": 15, "vias_usb_dn": 0, "vias_usb_dp": 0, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
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
    for key, got in (("vias", totals["vias_leftover"]), ("off45", totals["off45"]), ("micro", totals["micro"])):
        assert got <= bar[key], (name, key, got, bar[key], route["copper_bar"]["lines"])
    assert totals["vias_pattern"] == VIAS_PATTERN[name], (name, totals["vias_pattern"], VIAS_PATTERN[name], "D.4: exact, not a ceiling")
    assert totals["worst_detour"][1] <= bar["detour"] + 0.05, (name, totals["worst_detour"], route["copper_bar"]["lines"])
    assert route["geometry"]["segments"] <= bar["micro"] + 5  # KiCad's own count of the same staircases
    # pcbc's own 135-degree corner rule, as a ceiling like the rest of the bar (finding 5).
    assert route["geometry"]["angles"] <= bar["angles"], (name, route["geometry"], bar["angles"], "BAR: `track_angle`, pcbc's own geometry rule")
    # Per net, so a pattern cannot push one net toward the worst net's ceiling unrecorded (finding 16).
    for net, got in sorted(totals["detours"].items()):
        assert got <= DETOURS[name].get(net, 0.0) + 0.05, (name, net, got, DETOURS[name].get(net), "DETOURS: a per-net detour ceiling, recorded 2026-09-20")
    assert sorted(totals["detours"]) == sorted(DETOURS[name]), (name, sorted(totals["detours"]), "a net that appears or disappears here is a routing change nobody recorded")
    assert len(totals["vias_in_courtyard"]) == COURTYARD[name], (name, totals["vias_in_courtyard"], "COURTYARD: a pattern via under another part's keep-out")
    route = next(s for s in result["steps"] if s.get("stage") == "route")
    assert route["router"] == "krt" and route["unrouted"] == [] and route["copper"] == "verified", route
    # R2 C.6 and D.4: the refusals are exact, nothing pcbc wrote is hard-refused, and the copper the
    # patterns own is what the census says it is. A pattern that stops claiming a net fails here.
    assert route["refused"] == REFUSED[name], (name, route["refused"], route["pattern_moves"])
    assert not any(r["hard"] for r in route["refusals"]), route["refusals"]
    assert [(r["what"], r["rule"]) for r in route["refusals"] if r["pattern"] == "tap"] == TAP_REFUSED[name], (name, route["refusals"])
    taps = [m for m in route["pattern_moves"] if m.startswith("tap ")]
    if name == "node":
        assert taps[0].splitlines()[0] == (
            "tap GND: C_VBUS.2 at (23.545,29.85) cannot reach the GND plane on In1.Cu with a 0.35/0.2 via "
            "(the fab's standard via: 0.527 A carries this pad's 0.0213 A of GND's 1 A across 47 pads)."
        ), taps[0]
        assert taps[1].splitlines()[1] == (
            "  In the way: worst first: board edge [no net] at 17.2,0.843 leaves -0.414 mm of the 0.300 mm "
            "stackup.edge_clearance needs (rule: edge); USB_DN track on F.Cu [USB_DN] at 16.45,0.9044 leaves "
            "-0.314 mm of the 0.200 mm class Power needs (rule: copper)"
        ), taps[1]
    # C.4: KRT's own tap step runs for the plane nets pcbc refused a pad of, and not at all when
    # there are none. node refuses two GND pads, so it runs for GND alone and welds exactly those two.
    step = next((s for s in route["steps"] if s["step"] == "plane_taps"), None)
    if name == "node":
        assert step is not None and not step["summary"].get("skipped"), step
        assert step["nets"] == ["GND"], (step, "both refusals are GND's, so 3V3 has nothing left for KRT to weld")
    else:
        assert step is None, (name, step)
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
    # D.5, asked of the arbiter's own answer after the gate refilled: every plane is still ONE island,
    # still filled, still the area it was, and every tap via lands inside its own net's plane.
    from pcbc.route_emit import via_piece
    from pcbc.route_verify import plane_area, plane_checks, plane_islands

    # The via's real size, off the sidecar. It used to be handed 0.0 here — the key carries no
    # diameter — which is why `plane_checks` could only ever test the centre (finding 12).
    vias = [via_piece(i["net"], i["reason"], tuple(i["key"][1]), float(i["w"]), float(i["drill"]), owner=i["owner"]) for i in doc.items if i["key"][0] == "via"]
    assert plane_checks(routed, vias) == [], (name, "D.5: a tap via outside its own plane welds nothing")
    assert plane_islands(routed) == {k: 1 for k in PLANES[name]}, (name, plane_islands(routed), "D.5: one island per plane, and a plane that lost its fill is a missing key")
    area = plane_area(routed)
    assert set(area) == set(PLANES[name]) and all(abs(area[k] - PLANES[name][k]) <= 0.05 for k in PLANES[name]), (name, area, PLANES[name], "D.5: a fragment the fill deleted moves the area, never the island count (finding 9)")
    # And the same two questions asked inside `pcbc build`, so they run on a user's board and not
    # only here (finding 11).
    assert route["planes"]["fails"] == [] and route["planes"]["islands"] == {f"{n} {L}": 1 for n, L in PLANES[name]}, (name, route["planes"])
    from pcbc.fab import board_pads, mask_flashes, passive_refs, via_in_pad, via_in_pad_blockers
    from pcbc.route_emit import append_items, via as via_text

    design = load_board(board)
    assert via_in_pad_blockers(via_in_pad(routed), design) == [], (name, via_in_pad(routed), "a via inside a passive's pad wicks the joint; the fab stage refuses the board")
    # Measured rather than assumed (finding 1): drop a via dead-centre in every passive pad and the
    # guard must name every one. The regex this used to rest on needed a digit straight after the
    # letter, so it saw `C1` and `R1` and not `C_VBUS`, `R_FB_BOT` or `R_CC1` — which is every passive
    # on buck, c3_usb and node, and the assertion above could not tell "no via in a passive's pad"
    # from "no ref this regex recognises".
    mine = [g for g in board_pads(routed) if g.ref in passive_refs(design)]
    probe = append_items(routed, [via_text(g.at[0], g.at[1], 0.35, 0.2, g.net, f"probe{i:04d}") for i, g in enumerate(mine)])
    assert len(mine) == PASSIVE_PADS[name], (name, len(mine), "PASSIVE_PADS: every SMD pad of every part whose declared prefix is a passive's")
    assert len(via_in_pad_blockers(via_in_pad(probe), design)) == len(mine), (name, "the fab's hard stop must fire on every one of them")
    # Finding 3: the tenting is pcbc's own fact now (`Stackup.via_tenting`, written by `seed`), and
    # the mask Gerber is where it is read back — a tap's ring sits at exactly `clearance_min` from
    # the pad it welds (0.0889 mm on node), so an open barrel and the pad share one mask opening.
    assert "(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)" in routed, (name, "the board must declare its own via tenting, not inherit KiCad's default")
    flashes = {(round(x, 3), round(y, 3)) for g in ("F_Mask.gts", "B_Mask.gbs") for x, y in mask_flashes((fab / "gerbers" / f"layout-{g}").read_text())}
    tapped = {(round(i["key"][1][0], 3), round(i["key"][1][1], 3)) for i in doc.items if i["key"][0] == "via"}
    assert not (tapped & flashes), (name, sorted(tapped & flashes), "a pattern via with a mask aperture is an untented barrel beside a pad's own opening")
    # The soft rules (E, H.3) beside the bar: warnings KiCad raised from pcbc's own soft rules, pinned per example.
    gate = check_copper(design, routed_pcb, refill=False)
    assert gate["canary"], "the canary rule must fire on every board with copper"
    assert gate["soft"] == SOFT[name], (name, gate["soft"], gate["rules"])
    # Whose necked copper each `track_width` warning is (finding 14). A `track_width` hit names the
    # segment by its position and its length; a tap stub is one of ours when both match a tap segment
    # in `copper.json`. The count is pinned, so a pattern that necks more of its own copper below the
    # class it declared fails here instead of hiding inside a KRT total.
    stubs = {(round(i["key"][2][0], 3), round(i["key"][2][1], 3), round(i["mm"], 3)) for i in doc.items if i["key"][0] == "seg" and i["reason"] == "tap"}
    stubs |= {(round(i["key"][3][0], 3), round(i["key"][3][1], 3), round(i["mm"], 3)) for i in doc.items if i["key"][0] == "seg" and i["reason"] == "tap"}
    necked = [h for h in gate["width_hits"] if (round(h["at"][0], 3), round(h["at"][1], 3), round(h["mm"], 3)) in stubs]
    assert len(necked) == TAP_NECKED[name], (name, necked, TAP_NECKED[name], "TAP_NECKED: `tap._width` necks to the pad's across dimension and the width rule has no exemption")
    # And pcbc says so itself, once per necked stub, rather than leaving it to a DRC warning nobody
    # attributed (finding 7).
    assert sum(1 for n in route["notes"] if n.startswith("style: tap ") and "necks to" in n) == TAP_NECKED[name], (name, [n for n in route["notes"] if "necks to" in n])
