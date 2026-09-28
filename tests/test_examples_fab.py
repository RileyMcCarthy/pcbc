"""Every example board goes from board.py to a JLC package — natively placed and routed, judged by
KiCad, fabbed — or, where the native router (N0) cannot finish it yet, to an `unrouted:` refusal whose
moves are exactly KiCad's open nets, with no package.

**Every number below is a native recording** (2026-09-25, `pcbc build` of a copy of each board with
the tree of this commit; `scratchpad/native/record.py` re-measures all of them in one run). The old
router's ledger (KRT, 2026-09-19..24) is kept in `tests/fixtures/native/krt_baseline.json` with its
command; nothing here compares against it, because the two routers draw different copper for the
same board and a ceiling recorded for one says nothing about the other. The tables are pinned with
zero margin for the same reason they were before: the board is a function of this repo, so a number
that moves is a routing change somebody should read.

Tables are recorded for the boards the native router finishes (blinky, buck). c3_usb and node are
left with `VBUS` unrouted at their USB-C connector J1 (the dense-connector case `docs/direction.md`
§5 names as where locks earn their keep); for them this file asserts the refusal, not a ledger.
`RETURNS` and `PARALLEL` cover all five, because `test_route_verify_stitch.py` reads the emitted
boards whether or not they are finished.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from pcbc.build import build_job
from pcbc.compile import compile_design
from pcbc.language import load_board
from pcbc.netcheck import check_copper

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
COMPLETE = ("blinky", "buck")
UNFINISHED = ("c3_usb", "node")

BAR = {
    # Ceilings on the copper bar of the emitted board (`copper_bar` totals), native router. `vias` is
    # `vias_leftover`: copper no `pcbc:` group names, which natively is none (the router's copper is
    # `pcbc:route:<net>`), so it is 0 and the router's vias are counted, exactly, in `VIAS_PATTERN`.
    # `angles` is KiCad's `track_angle` warning count; buck's fell 20 -> 4 against the old router's
    # board (the native router prices a corner by the copper it forces on the next track and pulls its
    # path taut against that price; `route_cost`).
    #
    # Re-recorded 2026-09-25 for the native router, and **two of buck's ceilings rose**, said here
    # because the refuters found them raised in silence: `off45` 0 -> **3** (three legs off the 45-degree
    # grid; which pieces, and why, has not been measured) and
    # `detour` 2.51 -> **2.59** (`EN`, see DETOURS). `vias` 1 -> 0, `micro` 7 -> 1 and `angles`
    # 20 -> 4 fell. Measured by `scratchpad/native/record.py` (a native build of a copy of each board).
    "blinky": {"vias": 0, "off45": 0, "micro": 0, "detour": 1.03, "angles": 0},
    "buck": {"vias": 0, "off45": 3, "micro": 1, "detour": 2.59, "angles": 4},
}
"""`detour` is the worst per-net ratio of routed length to the shortest possible (`copper_bar`); buck's
2.59 is `EN`, which leaves `U1` round the `FB` and `SW` copper routed before it on F.Cu."""

PLANES = {
    # The filled pour's area, mm2 (KiCad 10.0.6). blinky's is the old router's to the last digit: its
    # only copper is the patterns' and the pour fields are the ones every shipped plane carried
    # (`route_native.plane_pours`). buck's is 31.95 mm2 smaller: the router takes B.Cu for its layer
    # changes where the old router kept to F.Cu.
    "blinky": {("GND", "B.Cu"): 942.54},
    "buck": {("GND", "B.Cu"): 903.64},
}

TAP_NECKED = {"blinky": 0, "buck": 1}
"""Tap stubs the tap pattern necks to a pad's across dimension (a `track_width` hit each), unchanged."""

COURTYARD = {"blinky": 0, "buck": 2}
"""pcbc's own vias inside another part's courtyard (`copper_bar.vias_in_courtyard`): buck's two are
router vias (`route`) under `U1`'s courtyard, where the old router placed none. A warning-grade census,
not a rule: the courtyard is an assembly keep-out and KiCad's DRC checks copper, not this."""

DETOURS = {
    "blinky": {"LED": 1.03},
    "buck": {"5V": 1.13, "BOOT": 1.0, "EN": 2.59, "FB": 1.02, "GND": 0.49, "SW": 1.6, "VIN": 1.53},
}

PASSIVE_PADS = {"blinky": 4, "buck": 18}

VIAS_PATTERN = {
    # Exact per reason. `route` is the native router's own layer changes.
    "blinky": {"tap": 1},
    "buck": {"route": 5, "tap": 6},
}

REFUSED = {
    "blinky": {},
    # `stitch: 3` is new: the router puts `VIN`'s layer changes on single 0.4 mm-drill vias (the old router
    # kept VIN on F.Cu), a 2 A rail needs three barrels per change, and the parallel carrier finds no
    # site for a rung at any of the three (two would sit in a pad, one against copper).
    "buck": {"hop": 2, "spine": 2, "stitch": 3},
}

SPINE_REFUSED = {
    "blinky": [],
    "buck": [("C_OUT2.1->J_OUT.1", "copper"), ("J_IN.1->C_IN1.1", "copper")],
}

TAP_REFUSED = {"blinky": [], "buck": []}

STITCH_REFUSED = {
    "blinky": [],
    "buck": [("VIN via at (18.588,5.38)", "via_in_pad"), ("VIN via at (18.842,10.714)", "via_in_pad"), ("VIN via at (20.112,11.476)", "copper")],
}

RUNGS = {"blinky": [], "buck": []}

OWNS = {
    # (segments, vias) per `pcbc:` role on the emitted board.
    "blinky": {"hop": (3, 0), "tap": (1, 1)},
    "buck": {"route": (33, 5), "spine": (10, 0), "tap": (6, 6)},
}

SOFT = {
    "blinky": {"width_power": 0},
    # buck 8 -> 1: the router draws every net at its class width; the one hit is the tap stub
    # `TAP_NECKED` names.
    "buck": {"width_power": 1},
}

SPINE_LINKS = {
    "blinky": {},
    "buck": {"5V": (3, 4, 0.6386), "VIN": (2, 4, 0.1593)},
}

SPINE_NETS = {
    "blinky": {},
    "buck": {"5V": (7, 14.6184), "VIN": (3, 3.3831)},
}

BOTTLENECK = {
    # (what the worst pad-to-pad path carries, mm under class width). buck's `VIN`: 1.21 A through a
    # necked track on the old router's board; natively 0.871 A through one 0.4 mm-drill via (every track at
    # class width, 0 mm under it). Still short of its declared 2 A either way: `UNDER`.
    "blinky": {},
    "buck": {"5V": (1.999, 0.0), "VIN": (0.871, 0.0)},
}
UNDER = {"blinky": [], "buck": ["VIN"]}

SAME_NET = {"blinky": 0, "buck": 1}
"""Re-recorded 2026-09-25 for the native router: buck 0 -> **1**, a new same-net clearance slot (two
pieces of one net closer than `clearance_min` with no copper between them) that the old router's board
did not have. A ceiling that rose, recorded with its cause unmeasured: nobody has yet looked at which
two pieces they are (`route_verify.same_net_slots` lists them)."""

LEFTOVER = {
    # (the spine's millimetres, the rest's) on each spined net; natively the rest is the router's own
    # `route` copper, which the bar counts as pattern copper, so the second number is 0.
    "blinky": {},
    "buck": {"5V": (25.76, 0.0), "VIN": (32.489, 0.0)},
}

RETURNS = {
    # Every via on a net carrying `Constraint.reference`, (net, at, verdict, nearest reference-net via
    # mm), on the five emitted boards. Only c3_usb has any: two `USB_DP` vias of the single-net joins
    # at `U3`, both `lost` (two layers, one pour on B.Cu: a via onto B.Cu leaves the reference).
    "blinky": (),
    "buck": (),
    "c3_usb": (
        ("USB_DP", (19.604, 21.382), "lost", 2.8041),
        ("USB_DP", (20.874, 20.366), "lost", 1.96),
    ),
    "node": (),
    "ds2": (),
}

PARALLEL = {
    # Every parallel-via group on an unpoured rail, (anchor, barrels, barrels needed), five boards.
    "blinky": {},
    "buck": {"VIN": (((18.588, 5.38), 1, 3), ((18.842, 10.714), 1, 3), ((20.112, 11.476), 1, 3))},
    "c3_usb": {"3V3": (((13.0, 16.302), 1, 1), ((18.588, 8.936), 1, 1))},
    # node: `VBUS` is unrouted at N0 and an unfinished net ships no copper, so it has no via to group
    # (re-recorded 2026-09-25 for the D1 fix; it had two single 0.4 mm-drill groups of the dropped copper).
    "node": {},
    "ds2": {
        "3V3": (((18.588, 18.08), 1, 1), ((19.35, 15.54), 1, 1), ((23.922, 7.666), 1, 1)),
        "VDDA": (((21.128, 5.634), 1, 1), ((22.83, 8.05), 1, 1)),
        "VSS": (
            ((3.094, 18.588), 1, 1),
            ((8.682, 16.556), 1, 1),
            ((10.46, 14.778), 1, 1),
            ((17.572, 6.396), 1, 1),
            ((22.652, 5.38), 1, 1),
            ((23.414, 13.508), 1, 1),
            ((36.368, 11.73), 1, 1),
            ((40.686, 16.556), 1, 1),
        ),
    },
}

THERMAL = {
    # (want, got, in pad, in plane, pitch, theta C/W, rise C, verdict); neither complete board declares one.
    "blinky": {},
    "buck": {},
}

UNFINISHED_PLANES = {
    # {plane: (islands, filled mm2)} on the emitted board of each unfinished example, measured 2026-09-25
    # (KiCad 10.0.6; `scratchpad/native/fix2/measure_r.py`). node's are its two inner planes, the only
    # four-layer pours in this repo: the plane gate (`build._plane_gate`) is asked of them on every
    # build even though the board stops at `VBUS`, and this is the only place that answer is held
    # (the second refutation round, major 4: inner-layer island blindness passed the suite).
    "c3_usb": {"GND B.Cu": (1, 1070.36)},
    "node": {"3V3 In2.Cu": (1, 2517.18), "GND In1.Cu": (1, 2545.0)},
}

OVERLAPS = {
    # (net, what `route_native.merge_overlaps` did) per resolved overlap on each unfinished example,
    # measured 2026-09-26 (`scratchpad/native/fix3/acc/table.json`). c3_usb: USB_DP's 3.695 mm run along
    # itself and USB_DN's 0.926 mm fold (its turn is joined by other copper, so no tail is cut) are
    # dropped. node: USB_DP's fold at (32.232,32.665) is dropped with the container's tail (nothing else
    # is at the turn), and GND's second tap stub is cut back to the first tap's via.
    "c3_usb": [("USB_DN", "dropped"), ("USB_DP", "dropped")],
    "node": [("GND", "trimmed"), ("USB_DP", "folded")],
}

UNFINISHED_THERMAL = {
    # The `Thermal("U1.49")` array on each unfinished example's emitted board, as `THERMAL` above: 9 barrels
    # on c3_usb (two layers, one per block at the fab's 1.975 mm pitch) and 12 on node (four layers, the
    # pitch derived from the 3V3 plane's antipads), both `served`, both wholly inside the land.
    "c3_usb": {"U1.49": (9, 9, 9, 9, 1.975, 25.68, 8.99, "served")},
    "node": {"U1.49": (12, 12, 12, 12, 0.850101, 27.85, 9.75, "served")},
}


@pytest.mark.kicad
@pytest.mark.parametrize("name", COMPLETE)
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
    # The build never ships a dangling track or via of its own (fourth review, C4; critique #1): the
    # route stage removes what KiCad would call dangling before it becomes an object
    # (`route_native.prune_dangling`), and KiCad's own count on the emitted board is zero.
    assert not [k for k in route["drc_by_type"] if k.endswith(("track_dangling", "via_dangling"))], (name, route["drc_by_type"])
    # And what that sweep removed, exactly (the second refutation round, major 7): nothing on either
    # complete board. Every piece pcbc wrote here is joined on both sides; a removal would be a note
    # (`prune: removed ...`) and would move this list.
    assert route["route_stats"]["dangling_removed"] == [], (name, route["route_stats"]["dangling_removed"])
    assert not [n for n in route["notes"] if n.startswith("prune:")], name
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
    assert route["router"] == "native" and not route.get("unrouted") and route["copper"] == "verified", route
    # R2 C.6 and D.4: the refusals are exact, nothing pcbc wrote is hard-refused, and the copper the
    # patterns own is what the census says it is. A pattern that stops claiming a net fails here.
    assert route["refused"] == REFUSED[name], (name, route["refused"], route["pattern_moves"])
    assert not any(r["hard"] for r in route["refusals"]), route["refusals"]
    assert [(r["what"], r["rule"]) for r in route["refusals"] if r["pattern"] == "tap"] == TAP_REFUSED[name], (name, route["refusals"])
    assert [(r["what"], r["rule"]) for r in route["refusals"] if r["pattern"] == "spine"] == SPINE_REFUSED[name], (name, route["refusals"])
    assert [(r["what"], r["rule"]) for r in route["refusals"] if r["pattern"] == "stitch"] == STITCH_REFUSED[name], (name, route["refusals"])
    # `docs/stitch-plan.md` S4: every rung pcbc wrote, and whether it is joined to the anchor it is
    # supposed to be parallel to, on **both** layers its barrel spans. `fails` is the only fatal
    # thing in `build._barrel_gate` and it is empty on every board: a rail that is still short is a
    # printed move, a rung that is not a rung is a pcbc bug.
    barrel = route["parallel"]
    assert [(r["net"], tuple(r["at"]), tuple(r["anchor"]), r["mm"], r["joined"]) for r in barrel["rungs"]] == [
        (n, tuple(a), tuple(b), mm, j) for n, a, b, mm, j in RUNGS[name]
    ], (name, barrel["rungs"])
    assert barrel["fails"] == [], (name, barrel["fails"])
    assert all(r["joined"] == r["spans"] for r in barrel["rungs"]), (name, barrel["rungs"])
    # And the copper is where the census says it is: a rung is one via and two links, so the stitch
    # millimetres on the bar are exactly the two link segments and nothing counts them as route.
    stitch_mm = {n: r["stitch_mm"] for n, r in route["copper_bar"]["nets"].items() if r.get("stitch_mm")}
    assert stitch_mm == {}, (name, stitch_mm)
    # B.4's own rule, asked of the built board — and asked so that it **can** fail, which it could
    # not before the S7 review (findings 1, 7, 12, 20). The gate is handed the copper pcbc wrote, off
    # the sidecar: a hop, a spine trunk or a backbone link narrower than its class is a bug a pattern
    # committed, and the two reasons R-I3 exempts (`tap`, `fanout`) are the pad necks `TAP_NECKED`
    # already counts. The probe below shows it firing.
    from pcbc.ampacity import power_bottlenecks
    from pcbc.copper import power_ampacity_failures
    from pcbc.fab import _owned_copper, board_roles

    job = compile_design(load_board(board))
    routed_pcb = tmp_path / "layout" / name / "routed" / "layout.kicad_pcb"
    routed = routed_pcb.read_text()
    owned_cu = _owned_copper(board_roles(routed))  # off the board's own `pcbc:` groups, as fab reads it
    assert power_ampacity_failures(job, routed, owned=owned_cu) == [], name
    # And the honest whole-net number beside it: what the worst pad-to-pad path carries, vias
    # included. A ledger — `carries` must not fall and `under_mm` must not rise.
    bottleneck = power_bottlenecks(job, routed)
    necked = [(n, r, 0.1) for n, r, _w in owned_cu if r not in ("tap", "fanout") and n in bottleneck]
    assert bool(power_ampacity_failures(job, routed, owned=necked)) == bool(necked), (name, necked[:2], "a gate that cannot fail is not a gate")
    got = {n: (r["carries"], r["under_mm"]) for n, r in bottleneck.items() if not r["zoned"]}
    assert sorted(got) == sorted(BOTTLENECK[name]), (name, sorted(got), "a power net that appears or disappears here is a change nobody recorded")
    for net, (carries, under) in sorted(BOTTLENECK[name].items()):
        assert got[net][0] >= carries - 1e-9 and got[net][1] <= under + 1e-9, (name, net, got[net], (carries, under), "BOTTLENECK: a floor that rises and a ceiling that falls")
    assert sorted(n for n, r in bottleneck.items() if r["verdict"] not in ("ok",)) == UNDER[name], (name, {n: r["verdict"] for n, r in bottleneck.items()})
    # And the same shortfall in the **build's own output**, which is the half that was missing: the
    # measurement lived in `fab/report.json` and `FAB_NOTES.md` while `pcbc build` printed
    # `copper: verified`, `error: null` and exited 0 on three boards with a rail under its declared
    # current. One move per `UNDER` net, in the fab step and at the top of the result.
    fab_step = next(s for s in result["steps"] if s.get("stage") == "fab")
    assert [m.split(":")[0] for m in fab_step["power"]] == UNDER[name], (name, fab_step["power"])
    assert result.get("power", []) == fab_step["power"], (name, result.get("power"))
    assert (name == "blinky") == ("power" not in result), (name, "a board with nothing to say says nothing")
    if name == "buck":
        # And the author who wants the stop gets it, on the same board and the same copper:
        # `--strict-power` (`PCBC_STRICT_POWER=1`) turns every one of those moves into the fab
        # gate's error. Re-run of the fab stage alone, on the board the build already routed.
        import os

        from pcbc.fab import fab_job

        was = os.environ.get("PCBC_STRICT_POWER")
        os.environ["PCBC_STRICT_POWER"] = "1"
        try:
            strict = fab_job(job, routed_pcb, out_dir=tmp_path / "strict", design=load_board(board))
        finally:
            os.environ.pop("PCBC_STRICT_POWER", None) if was is None else os.environ.update({"PCBC_STRICT_POWER": was})
        assert strict["error"] == "power: " + "; ".join(fab_step["power"]), strict["error"]
        assert "## Power, end to end" in (tmp_path / "strict" / "FAB_NOTES.md").read_text(), "a stopped build still writes the ledger it stopped on"
        # Verbatim, because every clause of it was wrong once. The **coordinate** and not the pad
        # pair: `C_IN1.1->R_EN.1` is an alphabetical tie member — `R_EN` is a 100 k enable pull-up
        # drawing 0.12 mA — while the 2 A path is `J_IN.1->U1.3`, so "move those two parts closer"
        # named two parts not on the rail. **No pour clause**: buck is two layers, where
        # `route.krt_plan` reads `planes=` not at all, so that edit would have poured no copper and
        # marked `VIN` zoned, silencing this very line. And an **edit to the NetReq that exists**:
        # a second `NetReq("VIN", ...)` is refused twice over before the board is drawn.
        #
        # Re-recorded 2026-09-21 for slice 1. Two numbers in the sentence move and neither is the
        # verdict: the narrowest **position** 13.75,9.075 -> **13.8,9.175**, because the relaxer
        # rewrote the leftover run that neck is on and the narrowest point of the new run is a
        # different point of the same track; and the under-width length 19.394 -> **19.12 mm**, the
        # same run being shorter. The width (0.3905), the current (1.21 A), the class (0.781) and
        # the path (`C_IN1.1->R_EN.1`) are byte-identical, which is the point — the relaxer changes
        # no width and re-orders no net, so the rail is as short of its declaration as it was.
        # Natively the rail is short by its **vias**, not its width: every VIN track is at its 0.781 mm
        # class, and the router's layer changes are single 0.4 mm-drill barrels (0.871 A each) where 2 A
        # needs three (`vias_per_change`); the stitch carrier found no site for a rung (`STITCH_REFUSED`).
        assert len(fab_step["power"]) == 1 and fab_step["power"][0].startswith(
            "VIN: declared 2 A, carries 0.871 A through its narrowest single via at 18.588,5.38"
        ), fab_step["power"]
    # `docs/stitch-plan.md` S6 and section 6 row 4: the array measured where it was placed, and the
    # two things the gate is allowed to fail on. `via_in_pad` **grows** by exactly the array — that is
    # the point of the technique — and `via_in_pad_blockers` does not grow at all, which is the half
    # that keeps `Thermal()` from ever naming a passive. `fab.via_in_pad_blockers` has a zero diff in
    # this slice and this is what holds it.
    thermal = route["thermal"]
    got = {
        r["pad"]: (r["want"], r["got"], r["in_pad"], r["in_plane"], r["pitch_mm"], r["theta_c_per_w"], r["rise_c"], r["verdict"])
        for r in thermal["arrays"]
    }
    assert got == THERMAL[name], (name, thermal["arrays"])
    assert thermal["fails"] == [], (name, thermal["fails"])
    assert thermal["blockers"] == 0, (name, "a via inside a passive's pad fails the fab gate at any via fill")
    assert thermal["via_in_pad_ours"] == thermal["via_in_pad_inside"] == sum(v[1] for v in THERMAL[name].values()), (
        name, thermal, "pcbc's own via-in-pad detector must find every via pcbc deliberately put in a pad, wholly inside it"
    )
    if THERMAL[name]:
        notes = (tmp_path / "layout" / name / "fab" / "FAB_NOTES.md").read_text()
        assert "## Thermal vias (via-in-pad, deliberate)" in notes, name
        assert "Epoxy Filled & Capped (IPC-4761 Type VII)" in notes, (
            name, "the array is unassemblable tented, and only the person ordering can make the fill true"
        )
        assert "The passive rule is unchanged and unrelaxed" in notes, name
    else:
        # And a board with no `Thermal()` orders no paid via fill (the third refutation round, A5d: with
        # the empty-list guard mutated away every board printed the Type VII order, and nothing failed).
        notes = (tmp_path / "layout" / name / "fab" / "FAB_NOTES.md").read_text()
        assert "## Thermal vias" not in notes and "IPC-4761 Type VII)** for these barrels" not in notes, (name, "a board with no thermal land orders no filled vias")
    # Same-net copper laid over itself (`route_native.merge_overlaps`): none is handed on.
    assert route["route_stats"]["overlap_mm"] == 0.0, (name, route["route_stats"]["overlaps_merged"])
    # `docs/router-plan.md` R-E1 and `docs/stitch-plan.md` section 8 item 1, S8: the plane lattice.
    # **Every board here writes none, and for a structural reason rather than a tuned bound** — not one
    # of the five pours a single net on two facing layers, which is section 2(l)'s measurement re-taken
    # after S8 made the router read `planes=` on both stackups (four boards `(('GND','B.Cu'),)`, node
    # `(('GND','In1.Cu'),('3V3','In2.Cu'))`, two pours of two *different* nets). The gate runs on every
    # board anyway, because the sentence it prints is what says so. `tests/test_planes.py` owns the
    # board that does get a lattice.
    assert route["planes_stitched"]["lattices"] == [], (name, route["planes_stitched"])
    assert route["planes_stitched"]["fails"] == [], (name, route["planes_stitched"]["fails"])
    assert route["planes_stitched"]["lines"] == ["planes: pcbc wrote no plane-stitch copper on this board"], name
    assert "plane" not in route["copper_bar"]["totals"]["vias_pattern"], (name, "no facing pair, no lattice")
    owns = {r: (v["segments"], v["vias"]) for r, v in route["copper_bar"]["totals"]["by_reason"].items() if r != "leftover"}
    assert owns == OWNS[name], (name, owns, route["copper_bar"]["lines"])
    # Per net, so a net recorded as spined that writes no copper fails here rather than in a table
    # (findings 5, 8, 15, 17). Both halves come off the build, not off a hand-copied row.
    spine_nets = {n: (v["segments"], v["mm"]) for n, v in (route["pattern_nets"].get("spine") or {}).items()}
    assert spine_nets == SPINE_NETS[name], (name, spine_nets, "D.4's census, one key deeper")
    links = {n: tuple(v) for n, v in (route["pattern_links"].get("spine") or {}).items()}
    assert links == {n: tuple(v) for n, v in SPINE_LINKS[name].items()}, (name, links, "links made, counted off the copper")
    assert all(n in spine_nets or made == 0 for n, (made, _need, _cov) in SPINE_LINKS[name].items()), (name, "a spine with no copper joins nothing")
    # The leftover's own half of the trade (finding 10).
    bar_nets = route["copper_bar"]["nets"]
    left = {n: (bar_nets[n]["pattern_mm"], bar_nets[n]["leftover_mm"]) for n in LEFTOVER[name]}
    assert left == LEFTOVER[name], (name, left, "LEFTOVER: what the spine wrote, and what KRT still had to")
    # The one clearance question nobody asks (finding 16), as a ceiling.
    assert len(route["same_net_slots"]) <= SAME_NET[name], (name, route["same_net_slots"][:3], SAME_NET[name])
    from pcbc.layout_job import roles_doc

    final = (tmp_path / "layout" / name / "routed" / "layout.kicad_pcb").read_text()
    doc = roles_doc(final)
    assert bool(doc.items) == bool(owns), (name, len(doc.items), owns)
    assert all(i["uuid"] in final for i in doc.items), "D.4: a piece is traceable from its role group into the board it is in"
    # The second review's M1 and M3: nothing writes or reads a sidecar (fab's gate reads the board's
    # own `pcbc:` groups); the fab board keeps every Python uuid, so its role groups do not dangle and
    # it decompiles back to the very bytes of `layout.gen.py`.
    from pcbc.gen import assign_ids, decompile, gen_source

    assert not list((tmp_path / "layout" / name / "routed").glob("copper*.json"))
    fab_text = (tmp_path / "layout" / name / "fab" / "layout.kicad_pcb").read_text()
    assert fab_text.count("\n\t(group") == final.count("\n\t(group")
    back = decompile(fab_text)
    assign_ids(back.copper, set(), back.graphics)
    assert gen_source(back.poses, back.copper, board=name, graphics=back.graphics) == (tmp_path / "layout" / name / "routed" / "layout.gen.py").read_text()
    place = next(s for s in result["steps"] if s.get("stage") == "place")
    # F.1's `usb_hs` budget line, the same carve-out `test_pcb_place._relations` makes and for the
    # same reason: `PRESETS` gives a fast kind no `airwire_mm`, so a board declaring `usb_hs` with
    # neither `max_mm=` nor `length_mm=` gets a move carrying its measured span. c3_usb and node are
    # both such boards. Asserting those two lines are the **only** report keeps a third move failing
    # here rather than hiding behind a relaxed assertion.
    fast = sorted(m.split(":")[0] for m in place["layout"] if "no budget checks it" in m)
    assert fast == (["USB_DN", "USB_DP"] if name in ("c3_usb", "node") else []), (name, place["layout"])
    assert [m for m in place["layout"] if "no budget checks it" not in m] == [], place["layout"]
    fab = tmp_path / "layout" / name / "fab"
    assert (fab / "bom.csv").exists() and (fab / "cpl.csv").exists()
    assert any((fab / "gerbers").glob("*")), "no gerbers"
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
    # R-X4's **V** half, same wiring: every declared `Chain()` on this board read off the emitted copper
    # (`route_verify.chain_order` via `build._chain_gate`); the verdicts are pinned in
    # `test_route_verify_chains.py`. Neither complete board declares one.
    declared = {ch.net for ch in load_board(board).chains}
    assert set(route["chains"]["checked"]) == declared, (name, route["chains"], declared)
    assert route["chains"]["fails"] == [], (name, route["chains"]["fails"], "R-X4: a declared order pcbc routes must be in the copper")
    # `docs/stitch-plan.md` S1's two counts, asked inside `pcbc build` and not only in the suite —
    # finding 11's lesson applied *before* the feature that needs them exists. The **numbers** are
    # pinned against the checked-in routed boards in `test_route_verify_stitch.py` (a coordinate is a
    # property of the copper KRT chose on the run that wrote the file); what is asserted here is that
    # they ran and that every verdict is one the classifier admits.
    #
    # The return count is report-only **by construction**: it has no `fails` key at all, so there is
    # no path by which it stops a build, which is why that slice could ship it on all five boards at
    # once. The barrel gate grew one in S4 and it is the narrowest fatal thing in the feature — a rung
    # pcbc wrote that is not joined to the anchor it is supposed to be parallel to, which is copper
    # doing the opposite of what `ampacity._via_clusters` will report about it. A rail that is still
    # short stays a printed move (`RUNGS`, `STITCH_REFUSED`).
    #
    # S5 added the **C** to the gate (`rules`): the same classification made from `board.py` with no
    # PCB file, which is the half that can reach an author before the router runs. `mispredicted` is
    # the claim that the two agree, and it must be empty — a rule that says `lost` from the stackup
    # and a finished board that says otherwise means one of the two is reading the wrong pours.
    assert set(route["returns"]) == {"watched", "vias", "verdicts", "rules", "mispredicted", "lines"} and "fails" not in route["returns"], (name, route["returns"])
    assert set(route["parallel"]) == {"rungs", "groups", "short", "fails", "lines"}, (name, route["parallel"])
    assert set(route["returns"]["verdicts"]) <= {"served", "far", "none", "net_change", "lost"}, (name, route["returns"]["verdicts"])
    assert len(route["returns"]["vias"]) == sum(route["returns"]["verdicts"].values()), (name, route["returns"])
    assert {r["verdict"] for r in route["returns"]["rules"]} <= {"lost", "net_change", "kept", "pinned"}, (name, route["returns"]["rules"])
    assert [r["net"] for r in route["returns"]["rules"]] == route["returns"]["watched"], (name, "one rule per watched net, in net order")
    assert route["returns"]["mispredicted"] == [], (name, route["returns"]["mispredicted"], "the C predicts the V: a verdict computed from board.py alone is the verdict the finished board carries")
    from pcbc.fab import board_pads, mask_flashes, passive_refs, via_in_pad, via_in_pad_blockers
    from boardtext import append_items, via as via_text

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
    # the pad it welds, so an open barrel and the pad share one mask opening.
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
    # in its role group. The count is pinned, so a pattern that necks more of its own copper below the
    # class it declared fails here instead of hiding inside a total.
    import math as _m

    tap_mm = {id(i): round(_m.dist(i["key"][2], i["key"][3]), 3) for i in doc.items if i["key"][0] == "seg"}
    stubs = {(round(i["key"][2][0], 3), round(i["key"][2][1], 3), tap_mm[id(i)]) for i in doc.items if i["key"][0] == "seg" and i["reason"] == "tap"}
    stubs |= {(round(i["key"][3][0], 3), round(i["key"][3][1], 3), tap_mm[id(i)]) for i in doc.items if i["key"][0] == "seg" and i["reason"] == "tap"}
    necked = [h for h in gate["width_hits"] if (round(h["at"][0], 3), round(h["at"][1], 3), round(h["mm"], 3)) in stubs]
    assert len(necked) == TAP_NECKED[name], (name, necked, TAP_NECKED[name], "TAP_NECKED: `tap._width` necks to the pad's across dimension and the width rule has no exemption")
    # And pcbc says so itself, once per necked stub, rather than leaving it to a DRC warning nobody
    # attributed (finding 7).
    assert sum(1 for n in route["notes"] if n.startswith("style: tap ") and "necks to" in n) == TAP_NECKED[name], (name, [n for n in route["notes"] if "necks to" in n])


@pytest.mark.kicad
@pytest.mark.parametrize("name", UNFINISHED)
def test_an_unfinished_example_is_a_move_and_never_a_package(tmp_path: Path, name: str):
    """N0's row for the boards the native router does not finish (docs/native-plan.md §5): the error
    starts `unrouted:`, one move per failed link naming what is in the way by its `board.py` line and
    a problem file for each, KiCad's open nets are exactly the router's, **0 DRC errors** on the
    emitted board, no traceback, no `fab/` and no route stamp. Measured 2026-09-25: `VBUS` on both,
    at the USB-C connector J1."""
    import json

    src = EXAMPLES / name
    board = tmp_path / f"{name}.py"
    board.write_text((src / f"{name}.py").read_text())
    shutil.copytree(src / "components", tmp_path / "components")
    result = build_job(board, upto="fab", force=True)
    err = str(result.get("error"))
    assert err.startswith("unrouted: VBUS") and "Traceback" not in err, err
    assert result["unrouted"] == ["VBUS"] and not result.get("ok"), result.get("unrouted")
    route = next(s for s in result["steps"] if s["stage"] == "route")
    assert set(route["unconnected_nets"]) == set(result["unrouted"]), (route["unconnected_nets"], result["unrouted"])
    assert not [k for k in route["drc_by_type"] if k.startswith("error:")], route["drc_by_type"]
    assert not [k for k in route["drc_by_type"] if k.endswith(("track_dangling", "via_dangling"))], route["drc_by_type"]
    layout = tmp_path / "layout" / name
    assert not (layout / "fab").exists() and not (layout / "inputs.json").exists()
    assert result["plan"][-1] == "fab" and [s["stage"] for s in result["steps"]][-1] == "route", "fab never ran"
    # Every generated piece of an unfinished net is left out, and one closing line says so (the
    # refuters' D1: 13 `Seg`s of VBUS stood in c3_usb's layout.gen.py while VBUS was "unrouted").
    links = [m for m in result["moves"] if "cannot reach" in m]
    tail = [m for m in result["moves"] if m not in links]
    assert tail == [m for m in tail if m.startswith("VBUS: unfinished, so none of its generated copper")] and len(tail) == 1, tail
    gen = (layout / "routed" / "layout.gen.py").read_text()
    assert '("VBUS"' not in gen, "no VBUS object in layout.gen.py"
    # One move per missing link (the refuters counted three moves for node's two).
    assert len(links) == sum(t - d for d, t in route["links"].values()), (len(links), route["links"])
    for move in links:
        way = next(ln for ln in move.splitlines() if ln.strip().startswith("In the way:"))
        # Each blocker named by the line that put it there: a board.py or layout.core.py line number,
        # never "has no Place()" for a part that has one (the refuters: "J1 pad at 32.9,38.7" was
        # read as ref "J1 pad at 32").
        for blocker in way.split(":", 1)[1].split(";"):
            assert re.search(r"(board|layout\.core)\.py:\d+|a net with no NetReq line", blocker), (blocker, move)
        assert "has no Place()" not in way, way
        f = move.split("Problem file: ")[1].split("/")[-1].strip()
        doc = json.loads((layout / "routed" / "unrouted" / f).read_text())
        assert doc["net"] == "VBUS" and doc["scene_near"], doc
    # The gates still run on the emitted board when it stops before fab (docs/native-plan.md, item 10),
    # so what they say is pinned here too (the second refutation round, major 4). The planes: every
    # plane one island at its recorded area, from the gate inside `pcbc build` and from the board.
    from pcbc.route_verify import plane_area, plane_islands

    routed = (layout / "routed" / "layout.kicad_pcb").read_text()
    want = UNFINISHED_PLANES[name]
    assert route["planes"]["fails"] == [], route["planes"]["fails"]
    assert route["planes"]["islands"] == {k: v[0] for k, v in want.items()}, route["planes"]["islands"]
    assert all(abs(route["planes"]["area_mm2"][k] - v[1]) <= 0.05 for k, v in want.items()) and set(route["planes"]["area_mm2"]) == set(want), route["planes"]["area_mm2"]
    assert {f"{n} {L}": k for (n, L), k in plane_islands(routed).items()} == {k: v[0] for k, v in want.items()}
    assert {f"{n} {L}": round(a, 2) for (n, L), a in plane_area(routed).items()}.keys() == want.keys()
    # The thermal array's verdicts, and the two numbers the gate may fail on.
    thermal = route["thermal"]
    got = {r["pad"]: (r["want"], r["got"], r["in_pad"], r["in_plane"], r["pitch_mm"], r["theta_c_per_w"], r["rise_c"], r["verdict"]) for r in thermal["arrays"]}
    assert got == UNFINISHED_THERMAL[name], (name, thermal["arrays"])
    assert thermal["fails"] == [] and thermal["blockers"] == 0, thermal
    assert thermal["via_in_pad_ours"] == thermal["via_in_pad_inside"] == sum(v[1] for v in UNFINISHED_THERMAL[name].values()), thermal
    # What the dangling sweep removed: nothing on either board (major 7; ds2's five are in test_patterns).
    assert route["route_stats"]["dangling_removed"] == [], route["route_stats"]["dangling_removed"]
    # Same-net copper laid over itself: what `route_native.merge_overlaps` resolved, and 0 mm left (the
    # third refutation round measured c3_usb 4.622 mm over 2 pairs — USB_DP along itself, USB_DN folded
    # back — and node 0.974 mm, GND's two overlapping taps and a USB_DP fold; `fix3/acc/table.md`).
    assert route["route_stats"]["overlap_mm"] == 0.0, route["route_stats"]["overlaps_merged"]
    done = sorted((r["net"], "folded" if "folded" in r else "dropped" if "dropped" in r else "trimmed") for r in route["route_stats"]["overlaps_merged"])
    assert done == OVERLAPS[name], route["route_stats"]["overlaps_merged"]
    # The router's open links against KiCad's, net by net: VBUS's six, and nothing else anywhere.
    assert route["open_links"] == {"router": {"VBUS": 6}, "kicad": {"VBUS": 6}}, route["open_links"]
    if name == "node":
        # And the gate can fail on an inner plane: a 3V3 track laid across In1.Cu, edge to edge, and
        # KiCad's refill cuts the GND plane in two. `_plane_gate` must say so; a plane reader blind to
        # the inner layers would find nothing here.
        from boardtext import append_items, segment
        from pcbc.build import _plane_gate
        from pcbc.netcheck import kicad_drc

        w_mm, h_mm = compile_design(load_board(board)).board_size_mm
        cut = layout / "routed" / "cut.kicad_pcb"
        cut.write_text(append_items(routed, [segment(0.0, h_mm / 2.0 + 0.3, w_mm, h_mm / 2.0 + 0.3, 1.0, "In1.Cu", "3V3", "cut-1", locked=False)]))
        for ext in (".kicad_pro", ".kicad_dru"):
            shutil.copy((layout / "routed" / "layout").with_suffix(ext), cut.with_suffix(ext))
        kicad_drc(cut, refill=True)  # refills and saves
        gate = _plane_gate(cut.read_text(), None)
        assert gate["islands"]["GND In1.Cu"] >= 2 and any(f.startswith("the GND plane on In1.Cu is ") for f in gate["fails"]), gate
