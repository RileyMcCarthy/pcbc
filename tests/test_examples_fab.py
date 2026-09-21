"""Every example board goes from board.py to a JLC package: placed, routed, judged by KiCad, fabbed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pcbc.build import build_job
from pcbc.compile import compile_design
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
    #
    # Re-recorded 2026-09-20 for R2 S7, the slice where pcbc lays the power nets itself: `spine`
    # (B.4) runs after the fanout and before KRT, at the class width and never narrower
    # (`docs/r2-measurements.md` S7). `PCBC_PATTERNS=off` still reproduces the S1b row on all five
    # boards exactly, so every move below is the spine and nothing else.
    #
    # **Two ceilings go up on buck and they are the finding, not a footnote**: micro 37 -> **96** and
    # `VIN`'s detour 2.09 -> **2.55**. 18 mm of 0.781 mm copper — the widest on any board here — is
    # locked across the middle of buck before KRT starts, and KRT answers with staircases and a
    # longer `VIN`. What it buys is `width_power` 39 -> 23 and off-45 2 -> 1 and `angles` 32 -> 28;
    # what it costs is in DETOURS and here, per F.3 item 7. c3_usb's `angles` 122 -> **130** and its
    # routed length are the same trade on a board where the pair comes out **better** (`USB_DN`
    # 1.81 -> 1.65). node improves on every count but `angles` (98 -> 100).
#
# Re-recorded 2026-09-21 for the stitching slice S4, and **one number in this table moves: node's
# `angles`, 100 -> 102.** Its `vias`, `off45`, `micro` and `detour` are byte-identical, as are all
# four numbers on the other three boards. The two warnings are the one rung `patterns/stitch.py`
# places: a link leaves the anchor via at 90 degrees to the `VBUS` track leaving the same barrel, and
# KiCad reads a branch at a via as a corner. It is not a corner by pcbc's own reading —
# `route_verify.paths_of` ends a run of copper at a junction on purpose, so `turn_ok` is never asked
# of a branch, and every tap stub and spine rib on these boards is one. A filter honouring the rule
# was written and measured: node's anchor is a straight-through layer change (`VBUS` up on F.Cu, down
# on In1.Cu), so all eight ring directions are 0 or 90 degrees to one of the two, **no site can
# satisfy it**, and forcing the nearest thing to it gave `angles` 104 and a via inside `U2`'s
# courtyard against a `COURTYARD` of 0. 102 is the cheapest true number. `patterns/stitch.py::run`.
#
# **Measured after the fact, and it settles what this ceiling counts**: of node's 102 `track_angle`
# warnings, **98 sit at a via** and 4 do not, on a board with 74 vias and 62 plane welds. So the
# number is overwhelmingly a census of branches at vias — every tap stub is one — and not of corners
# that break the rule. The rung adds two of the same kind that 98 others already are. A ceiling that
# is 96 % one phenomenon is worth knowing about before somebody reads a move in it.
    #
    # **Every ceiling here is met with zero margin, and that is deliberate** (S7 review, finding 23).
    # KRT and KiCad are both pinned to an exact version, so the board is a function of this repo: a
    # count that moves is a routing change somebody should read, not noise. The assertion is `<=`, so
    # an improvement passes silently and only a regression fails — which is what a ceiling is for.
    "blinky": {"vias": 0, "off45": 0, "micro": 0, "detour": 1.04, "angles": 0},
    "buck": {"vias": 1, "off45": 1, "micro": 96, "detour": 2.55, "angles": 28},
    "c3_usb": {"vias": 7, "off45": 11, "micro": 188, "detour": 1.70, "angles": 130},
    "node": {"vias": 11, "off45": 25, "micro": 54, "detour": 1.82, "angles": 102},
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
    "c3_usb": {("GND", "B.Cu"): 1053.06},
    # node re-pinned 2026-09-21 for S4, **with its arithmetic**: one parallel rung on `VBUS` is one
    # through via crossing both inner planes, and a foreign via takes a disc of `pi * (dia/2 +
    # clearance)^2` out of each. At node's numbers that is `pi * (0.175 + 0.2)^2` = **0.4418 mm2**,
    # and the two planes came back **0.44 mm2** smaller each — 2533.29 -> 2532.85 and 2520.84 ->
    # 2520.40 — which is the predicted disc to the last digit the 2 dp report can show. Both are
    # still **one island**, which is the number the whole `final` stage is gambling on: the gate's
    # `--refill-zones` is the only thing that reads the board after this stage, `antipad_clash`
    # predicts the neck per candidate and `plane_islands` measures it after, and neither moved.
    # node re-pinned again 2026-09-21 for S6, and only the **foreign** plane moves: `("3V3","In2.Cu")`
    # 2520.4 -> **2515.05**, `("GND","In1.Cu")` byte-identical at 2532.85. The array is on GND, so its
    # twelve barrels land *in* the GND plane and carve nothing out of it; every one of them is a
    # foreign via in the 3V3 plane below and takes an antipad out of that one. The arithmetic:
    # 12 x pi * (0.175 + 0.2)^2 = **5.3014 mm2** of discs against a measured **5.35**, and the 0.9 %
    # over is KiCad's own outer arc approximation — at its 0.005 mm default an r = 0.375 antipad is a
    # ~19-gon **circumscribing** the circle, which is (n/pi)*tan(pi/n) = 1.009 times the disc's area,
    # and the direction is the safe one (it clears more than the rule asks, never less).
    # Both planes are still **one island**, which is the number the whole `final` stage is gambling
    # on: no two antipads touch (closest centres 0.850101 mm against a 0.75 mm antipad diameter — a
    # 0.100101 mm web against the zone's own 0.1 mm `min_thickness`), `antipad_clash` predicted it per
    # candidate and `plane_islands` measured it after the gate refilled.
    "node": {("GND", "In1.Cu"): 2532.85, ("3V3", "In2.Cu"): 2515.05},
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
# Re-recorded for S7. Four move on buck and five on c3_usb, and the two directions are the slice:
# `VIN` 2.09 -> **2.55**, buck's `5V` 1.12 -> **1.14**, c3_usb's `3V3` 1.38 -> **1.53**, its `BOOT`
# 1.05 -> **1.18** and its `GND` 1.44 -> **1.48** are all KRT routing round locked power copper —
# the last two were unnamed here until the S7 review counted them (finding 23) — while `USB_DN`
# 1.81 -> **1.65** and `USB_DP` 1.62 -> **1.56** are the pair getting a straighter run. ds2 and node
# barely move.
#
# `USB_DN`'s improvement is **not** attributed to `VBUS` (finding 18): pcbc writes no `VBUS` copper on
# c3_usb at all, and `VBUS`'s own detour is unmoved. The net whose corridor changed is `3V3`, whose
# 25.1 mm backbone is locked before KRT's signals step and whose own detour rose to pay for it.
DETOURS = {
    "blinky": {"LED": 1.04},
    "buck": {"5V": 1.14, "BOOT": 1.0, "EN": 1.09, "FB": 1.01, "GND": 1.3, "SW": 1.52, "VIN": 2.55},
    "c3_usb": {"3V3": 1.53, "BOOT": 1.18, "CC1": 1.2, "CC2": 1.08, "EN": 1.04, "GND": 1.48, "LED": 1.19, "LED_A": 0.79, "USB_DN": 1.65, "USB_DP": 1.56, "VBUS": 1.7},
    "node": {
        "3V3": 0.13, "BOOT": 1.12, "CC1": 1.0, "CC2": 1.24, "DRV": 1.08, "EN": 1.09, "GATE": 1.03, "GND": 0.43,
        "LED": 1.12, "LED_A": 1.0, "LOAD": 1.0, "SCL": 1.04, "SDA": 1.09, "T_DIV": 1.03, "T_OUT": 1.72,
        "USB_DN": 1.82, "USB_DP": 1.53, "VBUS": 1.26,
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
    # S6: c3_usb declares `Thermal("U1.49")` and gains nine barrels under the ESP32's exposed land.
    # They carry their own reason rather than `stitch`, because `route_verify.parallel_joined` walks
    # every `stitch` via asking what anchor it is a twin of, and an array barrel has none.
    "c3_usb": {"fanout": 5, "tap": 34, "thermal": 9},
    # S4: node gains the stitching slice's first copper — one `parallel` rung on `VBUS`, a 0.35/0.2
    # twin beside the barrel at (28,33.4). Three of its `VBUS` via groups are under-rated and one has
    # a site; the other two are refused (`STITCH_REFUSED`). The other three boards gain nothing, and
    # for a structural reason rather than a tuned one: blinky and buck carry no via at all on an
    # unpoured power net, and c3_usb's 0.5 A sits under one 0.3 mm barrel's 0.707 A, so
    # `vias_per_change` is 1 and every group it has is already rated.
    "node": {"stitch": 1, "tap": 62, "thermal": 12},
}


# C.6: the refusal count per board, **exact**, so a new refusal is a test failure and cannot drift
# into being ignored. Zero hard refusals everywhere — a hop refusal is never hard, because KRT's own
# constrained `*_nets` step honours the same intent (C.6, and `patterns/hop.py`'s `_refuse`).
REFUSED = {
    "blinky": {},
    "buck": {"hop": 2, "spine": 2},
    "c3_usb": {"hop": 1, "spine": 2, "tap": 2},
    "node": {"hop": 1, "spine": 1, "stitch": 2, "tap": 2},
}
"""A spine refusal is soft for `hop._refuse`'s reason: KRT's own `{class}_nets` step honours the same
intent with the same numbers, so a refused spine costs a route pcbc would have drawn wider and never
costs the constraint. One per net that the backbone could not finish, never one per failed link.

**The `hop`, `spine` and `tap` counts are byte-identical through S4**, which is the `final` stage's
claim tested rather than argued: the stitch writes copper after every KRT step, so nothing routes
after it and nothing can react to it. The `stitch: 2` on node is the new population being counted, not
a pattern that got worse — and it is exactly **one refusal per spec**, which is the rule
`docs/stitch-plan.md` §1.3 says this dict exists to enforce: node's two refused groups tried 32 sites
each and a per-site refusal would have put 64 lines here."""

# B.4's refusals, net by net, with the rule that decided each — the `TAP_REFUSED` treatment for the
# spine. A spine refusal names the first link the backbone could not make.
SPINE_REFUSED = {
    "blinky": [],
    "buck": [("C_OUT2.1->J_OUT.1", "copper"), ("J_IN.1->C_IN1.1", "copper")],
    "c3_usb": [("U1.3->C_MCU.1", "no candidate"), ("J1.A4B9->U3.5", "copper")],
    "node": [("U2.1->C_VBUS_HF.1", "no candidate")],
}

# B.3's refusals, pad by pad, with the rule that decided each. A tap refusal is soft: KRT's own
# `plane_taps` step runs for exactly these nets and welds exactly these pads, which is why node's
# `06_plane_taps` adds two vias to the post stage's 62 and c3_usb's pads fall to the pour.
TAP_REFUSED = {
    "blinky": [],
    "buck": [],
    "c3_usb": [("C_EN.2", "copper"), ("R_CC1.2", "copper")],
    "node": [("C_VBUS.2", "copper"), ("U1.51", "edge")],
}

# `docs/stitch-plan.md` S4's refusals, group by group, with the rule that decided each — the
# `TAP_REFUSED` treatment for the parallel carrier, and **one line per spec, never one per site**.
# node's three under-rated `VBUS` groups are the whole population on all five boards; one has a site
# and two are walled in by the copper KRT put around them, each having tried all 32 of its ring.
STITCH_REFUSED = {
    "blinky": [],
    "buck": [],
    "c3_usb": [],
    "node": [("VBUS via at (27.8,36.3)", "copper"), ("VBUS via at (31.3,38.5)", "copper")],
}

# The rungs pcbc wrote, as (net, twin, anchor, centre-to-centre mm, the layers it is joined on).
# `route_verify.parallel_joined` is the one check in this feature that can fail a build, and it is
# the one nothing else on the board can make: `ampacity._via_clusters` counts two barrels 0.9 mm
# apart as a parallel pair with **no connectivity test**, KiCad's unconnected check is pad to pad, and
# `netcheck.check_copper` never reads a segment or a via. So a twin joined to nothing would make
# pcbc's own measurement report twice the ampacity of a board carrying no more current.
RUNGS = {
    "blinky": [],
    "buck": [],
    "c3_usb": [],
    "node": [("VBUS", (28.9, 33.4), (28.0, 33.4), 0.9, ["B.Cu", "F.Cu"])],
}

# D.4: what pcbc owns, exact per board — {reason: (segments, vias)}. A pattern that stops claiming a
# net shows up here before it shows up in the bar.
OWNS = {
    "blinky": {"hop": (5, 0), "tap": (1, 1)},
    "buck": {"spine": (10, 0), "tap": (6, 6)},
    # S6's array is `(0, n)`: a barrel and **no** segment. A rung needs two links because
    # `ampacity._via_clusters` would otherwise count a twin joined to nothing; an array via is already
    # inside its own land's copper on one layer and inside its own net's pour on the other, so a
    # segment would be a third path between two points already shorted.
    "c3_usb": {"fanout": (5, 5), "hop": (11, 0), "spine": (9, 0), "tap": (34, 34), "thermal": (0, 9)},
    # S4's rung is `(2, 1)`: one via and **one link segment on each of the two layers it spans**,
    # unconditionally and at the class width. That 2 is the whole of `docs/stitch-plan.md` §2(e) —
    # `ampacity._via_clusters` is single-linkage on distance with no connectivity test, so a bare
    # twin joined to nothing would double the reported ampacity of a board carrying no more current.
    "node": {"hop": (11, 0), "spine": (9, 0), "stitch": (2, 1), "tap": (62, 62), "thermal": (0, 12)},
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
#
# **Re-recorded 2026-09-20 for R2 S7, and `width_power` is the number the slice exists for.** It
# counts every place a power track was necked below its class, and the spine writes power copper at
# the class width or refuses: buck 39 -> **23**, c3_usb 51 -> **40**, node 15 -> **14**. Nothing else
# moves. What remains is the leftover: KRT still routes the links the backbone could not make, plus
# GND's own leftover on every board and node's whole USB pair (`width_usb`, an R4 item).
SOFT = {
    "blinky": {"width_power": 0},
    "buck": {"width_power": 23},
    "c3_usb": {"width_power": 40, "vias_usb_dn": 0, "vias_usb_dp": 1, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
    "node": {"width_usb": 46, "width_power": 14, "vias_usb_dn": 0, "vias_usb_dp": 0, "skew_usb_dn_usb_dp": 1, "uncoupled_usb": 1},
}

# Findings 5, 8, 15 and 17: what each spine actually joined and how much of the net it wrote, off the
# **copper** rather than off the `_link` calls that returned something. `{net: (made, needed,
# coverage)}` — `made` is `stations - components` over the emitted pieces and the station pads, and
# `coverage` is the spine's millimetres over the net's airwire.
#
# The S7 table published `c3_usb` as spining two nets with `VBUS` at "1 of 6". `VBUS` makes **0** and
# writes no copper at all, and `3V3` makes **5** of 7 rather than 4, because a link that runs over a
# third station of its own net connects it for free. Both numbers are now generated where the build
# reads them, so the ledger cannot drift from the board again.
SPINE_LINKS = {
    "blinky": {},
    "buck": {"5V": (3, 4, 0.6386), "VIN": (2, 4, 0.1593)},
    "c3_usb": {"3V3": (5, 7, 0.683), "VBUS": (0, 6, 0.0)},
    "node": {"VBUS": (3, 6, 0.4771)},
}

# D.4 one key deeper: the census per reason **and net**, exact. A net listed as spined that writes no
# copper now fails a test rather than a table (findings 5, 8, 17).
SPINE_NETS = {
    "blinky": {},
    "buck": {"5V": (7, 14.6184), "VIN": (3, 3.3831)},
    "c3_usb": {"3V3": (9, 25.1315)},
    "node": {"VBUS": (9, 8.8192)},
}

# Findings 1, 3, 7, 12 and 20: what each power net's worst pad-to-pad path can actually carry, vias
# included (`ampacity.power_bottlenecks`). **A ledger, not a gate** — it is the number the old
# `power_ampacity_failures` was offered as proof of and could not see, because it read the net's
# widest track. Three of these are under what the net declares and R2 cannot fix them: the copper
# that necks is KRT's leftover. `docs/r2-measurements.md` S7.
#
# `carries` is a floor that must rise and `under_mm` a ceiling that must fall. Against the
# `PCBC_PATTERNS=off` board the spine moves two of them a long way — buck's `5V` 0.707 -> 1.999 A and
# its `VIN` 0.536 -> 1.21 A (its 2 A path `J_IN.1 -> U1.3` goes 0.127 -> 0.781 mm) — and leaves the
# two nets it refused exactly where they were.
BOTTLENECK = {
    "blinky": {},
    "buck": {"5V": (1.999, 0.0), "VIN": (1.21, 19.394)},
    "c3_usb": {"3V3": (1.231, 0.0), "VBUS": (0.536, 24.068)},
    "node": {"LOAD": (1.231, 0.0), "VBUS": (0.527, 6.476)},
}
UNDER = {
    "blinky": [],
    "buck": ["VIN"],
    "c3_usb": ["VBUS"],
    "node": ["VBUS"],
}
"""Which nets are not carrying what they declare, and how. `VIN` and node's `VBUS` are `under
current`; c3_usb's `VBUS` is `under floor` — 0.127 mm carries 0.536 A of its 0.5 A on the IPC curve
with 7 % margin, while pcbc's own manufacturability floor for that current is 0.150 mm.

**S4's honest unknown, settled by measurement and neither of the two answers it was offered.**
`docs/stitch-plan.md` S7 asks whether the parallel carrier takes node's verdict to `ok` or merely
flips `via -> track`. It does neither: `carries` stays at **0.527** and `kind` stays `via`.
`power_bottlenecks` walks the **worst** pad-to-pad path, so a rail is as good as its worst barrel —
and of node's three under-rated `VBUS` groups the one with a site is not one of the two that bind.
Simulated on the same routed board with all three rungs written in, the walk reads **0.745 A** and
flips to `track` at a 0.2 mm neck on B.Cu, still `under current`: so even the optimistic answer was
`via -> track`, `UNDER["node"]` keeps its entry on any reading, and "only blinky has nothing to say"
is safe. What closes node's via half is two placements this board has no room for, and the two
`stitch` moves name what is in the way (`STITCH_REFUSED`)."""

# Finding 16: same-net copper closer than the process floor with bare laminate across it — the one
# clearance question neither pcbc (`route_scene._pair_clashes` skips same-net pairs) nor KiCad (which
# exempts them outright) asks. A **ceiling**, and the class is pre-existing rather than the spine's:
# the `PCBC_PATTERNS=off` boards measure buck 3, c3_usb 17, node 22 against these.
# **Re-pinned 2026-09-21 for S6, downward, because the check was over-reporting and the array is what
# exposed it.** `closest_points` is the minimum over vertex-to-edge pairs in both directions: it
# measures the separation of two *boundaries* and has no containment test in it, so a shape inside
# another comes back as a positive gap. A `Thermal()` barrel is via-in-pad by definition and a 0.5 mm
# ring in a 1.45 mm land reported 0.0749 mm — the margin of land copper around the barrel, which is
# copper and not laminate — nine times on c3_usb. Adding the containment skip removes those nine and
# **seventeen that were already there**: every one of them a track landing on its own pad, where the
# track's flank runs beside the pad's edge at the point the two overlap.
#
# Measured on the S4 boards and the S6 boards, which give **identical** surviving sets: c3_usb 22 ->
# **5**, node 16 -> **2**, and the array adds **none**. What survives is exactly the finding the check
# was built for (S7 review, finding 16): node's leftover sitting 0.0501 mm from the locked `VBUS`
# spine on a board whose process floor is 0.0889 mm, with real laminate between them. blinky's 0
# cannot fall; buck's 1 is left as the ceiling it is until a build measures it.
SAME_NET = {"blinky": 0, "buck": 1, "c3_usb": 5, "node": 2}

# Finding 10: the leftover's own half of the trade, per spined net — `(spine mm, leftover mm)`. The
# per-net `routed_mm` and `detour` are over **all** the copper on a net, so a spine that adds to
# KRT's route rather than replacing it looks like a win in every number that exists. Against the
# `PCBC_PATTERNS=off` leftover (buck `5V` 24.233, `VIN` 44.304; c3_usb `3V3` 40.381; node `VBUS`
# 24.786) the trade is 0.87 leftover millimetres saved per millimetre of spine on buck's `5V`, 0.37
# on c3_usb's `3V3`, 1.16 on node's `VBUS` — and **-1.88** on buck's `VIN`, which writes 3.38 mm and
# costs 6.37. `docs/r2-measurements.md` S7 records why that one is kept.
LEFTOVER = {
    "blinky": {},
    "buck": {"5V": (14.618, 11.586), "VIN": (3.383, 50.675)},
    # c3_usb's `VBUS` is the control: its 3.2 mm is the **fanout's**, not a spine's — the spine
    # refused all six of its links — and its leftover is the whole rest of the net.
    "c3_usb": {"3V3": (25.132, 31.09), "VBUS": (3.2, 43.833)},
    "node": {"VBUS": (8.819, 14.521)},
}


# `docs/stitch-plan.md` S1 — the two populations, counted before anything is designed around them.
# Both dicts are recorded against the **checked-in** routed boards (2026-09-20), including the DS2
# Addon's, read-only; `tests/test_route_verify_stitch.py` is where they are asserted, because a
# coordinate is a property of the copper KRT chose on the run that wrote that file and this test
# re-routes from scratch. What the build test asserts is that both counts ran inside `pcbc build`.
#
# RETURNS: every via on a net carrying `Constraint.reference`, as (net, at, verdict, nearest
# reference-net via in mm). **Not one of the eleven is a distance question.** node is four layers
# with GND on In1.Cu and 3V3 on In2.Cu, so a through via takes the track from copper referenced to
# GND to copper referenced to 3V3 — the return has to change *net*, and no via joins two nets.
# c3_usb is two layers with one pour, so the via lands the track in the GND pour's own layer and
# there is no second plane to reach. blinky, buck and ds2 declare no controlled-impedance net at
# all, so the population there is **empty and stays empty** — that is the entry, not a missing row.
# This is why `docs/stitch-plan.md` §8 ships no return-via placer: the population is zero for a
# structural reason no routing improvement fixes.
#
# **Re-measured on fresh builds in a temp directory, 2026-09-20, and the verdicts hold where the
# counts do not.** node's `USB_DN` and `USB_DP` come out of `pcbc build` with **no via at all** — the
# pair never leaves F.Cu — so node's seven rows below are a property of the stale checked-in artifact
# (`docs/stitch-plan.md` §2(o): that routed directory has no `patterns_post` step, so it predates
# S5). c3_usb comes out with **five**, still every one of them `lost`. The count is the router's; the
# verdict is the stackup's, and it is the verdict this dict exists to record.
RETURNS = {
    "blinky": (),
    "buck": (),
    "c3_usb": (
        ("USB_DN", (15.05, 1.15), "lost", 2.9547),
        ("USB_DN", (17.45, 19.45), "lost", 3.1851),
        ("USB_DP", (15.05, 2.75), "lost", 1.6279),
        ("USB_DP", (19.5, 20.35), "lost", 4.4433),
    ),
    "node": (
        ("USB_DN", (16.3, 12.5), "net_change", 2.1932),
        ("USB_DN", (18.1, 14.3), "net_change", 2.3537),
        ("USB_DN", (30.2, 29.7), "net_change", 1.8028),
        ("USB_DN", (30.8, 38.5), "net_change", 4.9649),
        ("USB_DN", (31.9, 31.5), "net_change", 3.6688),
        ("USB_DP", (29.75, 36.5), "net_change", 3.6719),
        ("USB_DP", (30.75, 36.5), "net_change", 4.5774),
    ),
    "ds2": (),
}

# PARALLEL: every parallel via group on an **unpoured** power rail, as {net: ((at, n, need), ...)}.
# `need` is `stackup.vias_per_change(amps, drill, plating, temp_rise)` — R1 has compiled that number
# since `ViaSpec.per_change` and nothing had ever compared it to the vias a finished board has.
#
# **Technique 1's whole population is node's `VBUS`**: four singleton groups of a 0.2 mm drill, each
# carrying `via_amps(0.2)` = 0.527 A of a 1 A rail, each wanting `vias_per_change(1.0, 0.2)` = 2 —
# four rungs. Every other group on every other board is `n >= need` by arithmetic: c3_usb's 0.5 A and
# ds2's 0.1 A both sit under one 0.3 mm barrel's 0.707 A, so `need` is 1 and a singleton is rated.
# blinky and buck carry no via at all on an unpoured power net, so their entry is `{}` and means it.
#
# The **poured** rails are exempt for the reason `power_bottlenecks` exempts them: the plane is the
# conductor and a via into it is a tap carrying one pad's share. That exemption is load-bearing, not
# tidy — without it buck's `GND` reads five under-rated groups against its 2 A and node's `GND` and
# `3V3` read sixteen and seven, all of them taps into the pour that carries the current.
#
# **The count is four here and three on a fresh build**, measured 2026-09-20 in a temp directory:
# node's `VBUS` comes out of `pcbc build` with three vias at (27.8,36.3), (28,33.4) and (31.3,38.5),
# still all singletons, still all short by one. `docs/stitch-plan.md` §2(o) tells S4 to re-measure
# rather than copy the four, and this is the measurement that says why: the *count* is the router's,
# the *shortfall per group* is the stackup's — `vias_per_change(1.0, 0.2)` is 2 whatever KRT does.
PARALLEL = {
    "blinky": {},
    "buck": {},
    "c3_usb": {
        "3V3": (((16.25, 11.9), 1, 1), ((18.4, 9.7), 1, 1)),
        "VBUS": (((17.35, 20.45), 1, 1), ((18.35, 23.55), 1, 1), ((19.5, 23.95), 1, 1)),
    },
    "node": {"VBUS": (((26.9, 34.7), 1, 2), ((28.0, 38.6), 1, 2), ((32.3, 34.9), 1, 2), ((32.3, 36.4), 1, 2))},
    "ds2": {
        "3V3": (((19.05, 16.5), 1, 1), ((22.2, 7.55), 1, 1)),
        "VDDA": (((22.85, 8.05), 1, 1),),
        "VSS": (
            ((4.65, 18.85), 1, 1),
            ((8.35, 15.15), 1, 1),
            ((22.85, 16.35), 1, 1),
            ((23.2, 13.2), 1, 1),
            ((30.1, 3.25), 1, 1),
            ((30.9, 4.05), 1, 1),
            ((35.4, 7.9), 1, 1),
            ((36.2, 8.7), 1, 1),
            ((36.4, 12.5), 1, 1),
            ((40.6, 16.05), 1, 1),
        ),
    },
}


# `docs/stitch-plan.md` S6's array, as the finished board has it: `{pad: (want, got, in_pad,
# in_plane, closest pair mm, K/W, C, verdict)}`. Exact, and every number in it is a different claim.
#
# `want` is the compiler's, from `board.py` and the stackup alone: `ceil(theta_barrel / (rise/W))`
# with `theta_barrel = L/(k*A)` on the plating annulus. `got` is what fitted. `in_pad` and `in_plane`
# are the two containments the technique is made of and the only fatal thing in
# `build._thermal_gate`: a barrel outside its land still reads as connected to every check on the
# board — KiCad's unconnected-items is pad to pad, `netcheck.check_copper` never sees a via, and
# `verify_copper` asks about clearance, angle and size — and moves none of the heat it was placed
# for. The pitch is `patterns.stitch.thermal_pitch`'s derived number to the nanometre, read off the
# **board file** rather than the 4-dp sidecar.
#
# Measured 2026-09-21, and both boards reach their budget:
#   c3_usb  2 layers, one GND pour, no foreign plane. 231.1 K/W per 0.3 mm barrel over a 1.6 mm
#           board, 9 wanted at 0.35 W in 10 C, 36 sites, **9 placed**, 25.68 K/W, 8.99 C.
#   node    4 layers. 334.2 K/W per 0.2 mm barrel over 1.5862 mm — **worse per via on the thinner
#           board**, because the drill is 0.2 and the annulus goes with the circumference — 12
#           wanted, 36 sites, **12 placed**, 27.85 K/W, 9.75 C.
THERMAL = {
    "blinky": {},
    "buck": {},
    "c3_usb": {"U1.49": (9, 9, 9, 9, 0.800101, 25.68, 8.99, "served")},
    "node": {"U1.49": (12, 12, 12, 12, 0.850101, 27.85, 9.75, "served")},
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
    assert stitch_mm == ({"VBUS": 1.8} if RUNGS[name] else {}), (name, stitch_mm)
    # B.4's own rule, asked of the built board — and asked so that it **can** fail, which it could
    # not before the S7 review (findings 1, 7, 12, 20). The gate is handed the copper pcbc wrote, off
    # the sidecar: a hop, a spine trunk or a backbone link narrower than its class is a bug a pattern
    # committed, and the two reasons R-I3 exempts (`tap`, `fanout`) are the pad necks `TAP_NECKED`
    # already counts. The probe below shows it firing.
    from pcbc.ampacity import power_bottlenecks
    from pcbc.copper import power_ampacity_failures
    from pcbc.fab import _owned_copper

    job = compile_design(load_board(board))
    routed_pcb = tmp_path / "layout" / name / "routed" / "layout.kicad_pcb"
    routed = routed_pcb.read_text()
    owned_cu = _owned_copper(routed_pcb)
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
        assert fab_step["power"] == [
            "VIN: declared 2 A, carries 1.21 A through its narrowest 0.3905 mm track at "
            "13.75,9.075 on F.Cu (19.394 mm of this net is narrower than its 0.781 mm class; "
            "path C_IN1.1->R_EN.1). Place() the parts either side of that copper closer together, "
            'or change amps= on the NetReq that already declares "VIN"'
        ], fab_step["power"]
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
    # R-X4's **V** half, same wiring: every declared `Chain()` on this board was read off the copper
    # KRT finished (`route_verify.chain_order` via `build._chain_gate`). The **verdicts** are pinned
    # against the checked-in boards in `test_route_verify_chains.py`, not here, because this test
    # routes the board afresh and a verdict is a property of the copper KRT chose this run; what is
    # asserted here is that the gate ran on every chain and that no build gets past it with a
    # violated order it owns. c3_usb and node's chains are `USB_DP`/`USB_DN`, which carry a `Pair`,
    # so B.2 hands them to R4 and the gate reports rather than stops — `notes`, never `fails`.
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
