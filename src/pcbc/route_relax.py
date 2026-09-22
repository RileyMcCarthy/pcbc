"""Whole-board octilinear string-pull: the last thing that touches the copper.

A maze router draws the path it found, not the path a person would draw. KRT's output on a fresh
`buck` is 175 segments for 149.3 mm of copper — 96 of them shorter than `MICRO_MM`, 1.031 corners
and **50.9 degrees of turning per millimetre**, against the hand-routed DS2 Addon's 0.336 and 17.2.
That gap is not homotopy and it is not net order: it is the staircase a grid leaves behind. This
module pulls each run of copper taut inside the corridor the router already proved, and hands the
same connectivity back with a tenth of the turning.

**It is not a pattern.** A pattern proposes copper where there was none and `pattern_copper` ends in
`write_pieces`, which only appends (`docs/stitch-plan.md` R-S1). This *replaces* — `route_emit.
strip_segments` exists for this module and for nothing else — so R-S1's redundancy argument does not
reach it and a different one has to. The argument is **positional plus contact**: `route.krt_plan`
schedules nothing after this step, so there is no router left to react to what it writes
(`docs/quality-plan.md` section 6), and every piece of copper that was touching another piece is
still touching it, by a rule checked per candidate and then checked again over the whole board with
`route_scene.components` — which is the same predicate `net_open` uses to decide whether a net still
needs a router. Connectivity is proved here, not hoped for.

**Three decisions, each of which two independent prototypes got wrong first** (`docs/quality-plan.md`
section 3, C2-C4):

*The accept rule is lexicographic, and relative to the chain in hand.* See `quality`. "Strictly
shorter and no more corners" is the rule that looks right and it accepted **0 of 21 chains** on
buck, because collapsing a monotone staircase into two legs is exactly length-neutral.

*The preserve set is the geometric contact set, not the endpoints.* See `holds`. Freezing the
endpoints and the degree != 2 vertices looks correct in review and is **known-wrong**: it broke
`5V`, `VBUS`, `AIN0` and `VSS` across three boards and took c3_usb's `track_dangling` from 1 to 2.

*What may move is `leftover`, `hop`, `fanout` and `tap`, and nothing else.* See `RELAXABLE`. A
relaxed guard is no longer a fixed offset from the path it shields, and `build.py`'s gates rebuild
their `Piece`s from the sidecar's geometry and re-check semantics that were true by construction at
emission time.

There are two kinds of move, both in `_moves`: the string-pull proper, which replaces the inside of a
run with the octilinear path between two of its vertices, and the **trim**, which drops a leading or
trailing leg and draws nothing. A pull rewrites only the *inside* of a run, and a maze router's worst
corners are at its ends — the doubled-back spur where a run lands on a stub and goes back along it
before turning away — so the trim is the move that reaches them, **by moving an endpoint**. On buck
it is worth 2.0 deg/mm on its own (20.4 without the two trim yields, 18.3 with) and it is what takes
the off-45 count to zero; without it buck misses this slice's 20 deg/mm bar.

Moving an endpoint is also the one thing in this module that can ship a board that fails in
fabrication, so the trim is not gated by "the copper still touches" — that bound is a rounded end cap
grazing a pad outline, and the first version of this pass reached it, leaving buck's `EN` track with
23.5 um of overlap in `R_EN.2` where KRT had left 198.5. It is gated by `Holds`, which requires the
run's **centreline** to stay inside the copper it joins, and on a **pad** to stay `clearance_min`
inside it. That costs buck 0.4 deg/mm against the grazing version (17.9 -> 18.3) and *gains* ds2 0.1
(15.4 -> 15.2), which is what decided the centreline half; the pad floor costs blinky, buck and node
nothing at all, c3_usb 0.10 deg/mm and ds2 0.07, and it is what keeps two of the three joints this
pass had cut to 76-118 um off the boards outright — c3_usb's `C_EN.2` does not retract at all now
(0.2700 -> **0.2800** mm, deeper than it started) and neither does ds2's `R5.2` (0.5075 ->
**0.5125**) — while c3_usb's `U1.23` still goes 0.2 -> 0.1, because it is a **square** pad whose
`Shape` carries `r = 0` and the floor has nothing to be spent out of (`Holds`).

Determinism comes from four things and not from the absence of search: a fixed chain order, a fixed
scan order, **first**-accept rather than best-accept, and every coordinate through `route_geom.q`
exactly once (`octile_path` quantises; everything read off the board is already on the grid, where
`q` is the identity). Two runs of a board produce byte-identical output; `docs/quality-plan.md`
section 6 is where that house rule was changed to allow this pass to exist at all.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .route_emit import Piece, piece_text, replace_segments, seg_key, seg_piece
from .route_geom import MICRO_MM, Pt, Shape, gap, hull_dist2, octile_path, qp, track_shape, via_shape
from .route_scene import blocked, build_scene, components
from .sexp import stable_uuid

if TYPE_CHECKING:  # pragma: no cover
    from .compile import CompiledJob
    from .constraints import ConstraintSet
    from .model import Design

__all__ = ["RELAXABLE", "RelaxResult", "Chain", "chains_of", "orphan_copper", "quality", "relax_board"]

RELAXABLE = ("fanout", "hop", "leftover", "tap")
"""The reasons whose geometry this pass may rewrite (`docs/quality-plan.md` C4).

Everything else pcbc writes is skipped, and the reason is not caution — it is that `build.py`'s
`_guard_gate`, `_thermal_gate`, `_barrel_gate` and `_chain_gate` rebuild `Piece`s from the sidecar's
**geometry** keys and re-check semantics (`guard_cover`, `thermal_budget`, `parallel_joined`,
`chain_order`) that were satisfied by construction when the copper was emitted. A guard is a fixed
offset from the path it shields; move it and it is no longer that, and the gate is right to say so.
`spine`, `guard`, `stitch`, `thermal`, `chain` and `plane` are therefore out of reach, as is any
copper `route.lock_copper` locked on a constrained `NetReq` (see `_skip_nets`).

These four are in reach for the opposite reason: no gate reads their geometry for meaning. A
`leftover` run is KRT's route and pcbc owns nothing about it but its shape; a `hop`, a `fanout` stub
and a `tap` stub are judged by what they connect, not by the path they take to do it.

**This pass does move a run's endpoints** — the `trim` in `_moves` retracts one — and an earlier
version of this paragraph said it did not. That sentence is why nobody looked at what `Holds` was
asking of a trimmed end, and a trim then pulled buck's `EN` track back until it had 23.5 um of
overlap left in `R_EN.2`. What holds a stub to its pad is `Holds`, whose rule is that the copper's
**centreline** stays `stackup.clearance_min` inside the pad's, and not any claim made here."""

_ITER_CAP = 400
"""Passes over one chain before the relaxer gives up on it. A bound, not a tuning knob: `quality`
strictly decreases on every accept and is bounded below, so a chain terminates on its own. The cap
is what keeps a bug in `quality` from becoming a hang, and no chain on any board in the repo has
come within an order of magnitude of it (buck's worst is 6)."""


@dataclass(frozen=True)
class Chain:
    """One maximal run of copper with no junction in it, and the segments it came from.

    The key is `(net, layer, width, reason)` and each of the four is load-bearing. Net and layer
    because copper only joins copper on its own net and its own layer. **Width, because
    `docs/quality-plan.md` section 6 keeps "a pattern never degrades: class width or nothing" and
    satisfies it structurally here** — a width change is a chain boundary, so this pass cannot alter
    a track's width even by accident. **Reason, for the same structural reason one step up**: a
    `leftover` run that happens to meet a `spine` is two chains, so `RELAXABLE` is enforced by the
    grouping rather than by a test somebody can forget, and a relaxed piece's census label is the
    label the piece it replaced carried.
    """

    net: str
    layer: str
    width: float
    reason: str
    pts: tuple[Pt, ...]
    keys: tuple[tuple, ...]  # `route_emit.seg_key` of every segment this chain replaces


@dataclass(frozen=True)
class RelaxResult:
    """What the step hands back: the board, the rewritten ownership, and the numbers."""

    text: str
    owned: tuple[Piece, ...] = ()
    owned_steps: tuple[str, ...] = ()
    moves: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    refusals: tuple[dict, ...] = ()
    stats: dict = field(default_factory=dict)
    wall_ms: int = 0


# --- the potential function -----------------------------------------------------------------------


def _turn_deg(a: Pt, c: Pt, b: Pt) -> float:
    """How far the path turns at `c`, in degrees, 0 for straight-on and 180 for a reversal."""
    v1 = (c[0] - a[0], c[1] - a[1])
    v2 = (b[0] - c[0], b[1] - c[1])
    l1, l2 = math.hypot(*v1), math.hypot(*v2)
    if l1 < 1e-12 or l2 < 1e-12:
        return 0.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
    return math.degrees(math.acos(cos))


def quality(pts: tuple[Pt, ...], ends: tuple[tuple[Pt, ...], tuple[Pt, ...]] = ((), ())) -> tuple:
    """`Q = (off-45 legs, turns over 45 degrees, micro legs, corners, length)`, lexicographic.

    **This is `docs/quality-plan.md` C2 and it is the one contradiction the panel settled by
    measurement.** The competing rule — "accept when the candidate is strictly shorter and has no
    more corners" — reads as the obviously safe one and it accepted **0 of buck's 21 chains**: a
    monotone staircase and the two legs that replace it have exactly the same length, which is the
    whole reason a router leaves staircases behind. Every term above length has to come first, and
    in this order, because each is a thing KiCad counts and a person sees: off-45 is copper pcbc's
    own `pcbc_geometry_angles` rule cannot describe, a turn over 45 degrees is a `track_angle`
    warning, a leg under `MICRO_MM` is a `track_segment_length` warning, and a corner is a corner.

    Accept is `Q(candidate) < Q(current)`, **compared against the chain in hand and never against an
    absolute standard**. An absolute test — "is this candidate octilinear, micro-free and smooth?" —
    rejected 865 of 1214 candidates on the same boards, because the input is a router's output and
    almost every improvement on it is still imperfect.

    `ends` carries the neighbouring vertices at the chain's two ends, on copper this chain does not
    own, and the turns against them are counted here. Without it a chain is scored blind to the
    corner it makes where it meets the rest of its net, and ds2's `track_angle` warnings went
    **10 -> 12** while every chain's own score improved.

    Strictly decreasing on every accept and bounded below, so a chain terminates. The length term is
    rounded to 9 dp — a nanometre is 1e-6 mm — so two paths that differ only in float noise compare
    equal instead of trading places forever.
    """
    bad = micro = 0
    for a, b in zip(pts, pts[1:]):
        dx, dy = round((b[0] - a[0]) * 1e6), round((b[1] - a[1]) * 1e6)
        if dx == 0 and dy == 0:
            bad += 1
            continue
        if dx != 0 and dy != 0 and abs(dx) != abs(dy):
            bad += 1
        if math.hypot(b[0] - a[0], b[1] - a[1]) < MICRO_MM:
            micro += 1
    sharp = corners = 0
    for i in range(1, len(pts) - 1):
        t = _turn_deg(pts[i - 1], pts[i], pts[i + 1])
        if t > 45.5:
            sharp += 1
        if t > 0.5:
            corners += 1
    for end, nbrs in ((0, ends[0]), (-1, ends[1])):
        here = pts[end]
        nxt = pts[1] if end == 0 else pts[-2]
        for far in nbrs:
            # `far -> here -> nxt` is a path like any other and `_turn_deg` reads it as one: a
            # neighbour straight across from this end is a turn of 0, a neighbour at right angles is
            # a turn of 90. (Writing `180 - _turn_deg(...)` here inverts the term and quietly pays
            # the relaxer to put right angles at its junctions; `test_route_relax.py` pins it.)
            t = _turn_deg(far, here, nxt)
            if t > 45.5:
                sharp += 1
            if t > 0.5:
                corners += 1
    return (bad, sharp, micro, corners, round(math.fsum(math.dist(a, b) for a, b in zip(pts, pts[1:])), 9))


# --- the preserve set -----------------------------------------------------------------------------


def _touches(shape: Shape, pts: tuple[Pt, ...], w: float) -> bool:
    """Does this polyline's copper touch `shape`'s? `route_scene._touch`'s predicate, asked of a run.

    `gap(...) <= 0.0` is pcbc's own definition of "connected" — it is what `route_scene.components`
    unions on and therefore what `net_open` means by a net that still needs a router — so preserving
    it is preserving exactly the thing the tool itself calls connectivity. The house rule that
    `clears()` decides and `gap()` only reports is about **clearance**, where the question is how
    much air there is; this is the opposite question and `_touch` already answers it this way.

    Used to **select** the preserve set and never to accept a candidate: "they touch" is the weakest
    true statement about a joint and `_reach2` below is what a candidate is held to. See `Holds`.
    """
    return any(gap(shape, track_shape(a, b, w)) <= 0.0 for a, b in zip(pts, pts[1:]))


def _reach2(shape: Shape, pts: tuple[Pt, ...], w: float) -> float:
    """How close this run's **centreline** comes to `shape`'s hull, squared, in mm².

    The depth of a joint and the air in a clearance are the same measurement read in opposite
    directions, so this is `route_geom.hull_dist2` and nothing else: `reach <= shape.r + w/2` is
    exactly "the copper touches" and `reach <= shape.r` is exactly "the centreline is inside the
    copper". Squared, because that is how the core decides — `clears` has no square root in its
    accept path and neither has this. `gap` still reports the same number in millimetres for the
    move lines, which is the whole of the division of labour the house rule asks for.
    """
    return min(hull_dist2(shape.pts, track_shape(a, b, w).pts) for a, b in zip(pts, pts[1:]))


def _poly_pt_d(pts: tuple[Pt, ...], p: Pt) -> float:
    """Distance from a point to the polyline's centreline."""
    best = math.inf
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        d2 = dx * dx + dy * dy
        t = 0.0 if d2 <= 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d2))
        best = min(best, math.dist(p, (a[0] + t * dx, a[1] + t * dy)))
    return best


def _div_round(n: int, d: int) -> int:
    """`n / d` rounded to the nearest integer, ties to even — `route_geom.q`'s tie rule, in exact
    integer arithmetic so the answer is a function of the two integers and of nothing else.

    It is exact rather than `round(n / d)` because `_seg_meet` below promises to be symmetric in its
    two segments, and the two argument orders reach the same rational by different expressions: only
    rounding the rational itself, rather than a float that already lost bits, makes them land on the
    same nanometre."""
    if d < 0:
        n, d = -n, -d
    whole, rem = divmod(n, d)
    if 2 * rem > d or (2 * rem == d and whole % 2):
        whole += 1
    return whole


def _seg_meet(p1: Pt, p2: Pt, p3: Pt, p4: Pt) -> Pt | None:
    """Where two segments properly cross, on KiCad's 1 nm grid, or `None` when they do not.

    The **decision** is `route_geom.hull_dist2`, which is the module that owns exact overlap: two
    two-point hulls are two segments, and it answers 0.0 exactly when they meet — by the same
    integer orientation predicate it uses for every other pair of copper shapes. Re-deriving a
    crossing test here would make a fourth reader of "do these two touch" and the reason this
    function exists at all is that there were already two (`gap`, and `_seg_closest`'s projections)
    and they disagreed.

    Only the *point* is computed here, and only for the case the projections cannot name. The
    arithmetic is on integer nanometres with one exactly-rounded division per axis, so the result is
    already on the grid: `route_geom.q` is the identity on it and the caller's single `qp` stays the
    one quantisation the house rule allows.
    """
    if hull_dist2((p1, p2), (p3, p4)) != 0.0:
        return None
    ax, ay, bx, by, cx, cy, dx_, dy_ = (round(v * 1e6) for p in (p1, p2, p3, p4) for v in p)
    rx, ry = bx - ax, by - ay
    sx, sy = dx_ - cx, dy_ - cy
    den = rx * sy - ry * sx
    if den == 0:
        return None  # parallel: collinear touching or overlapping, which the projections name exactly
    t_num = (cx - ax) * sy - (cy - ay) * sx
    if not (0 <= t_num <= den if den > 0 else den <= t_num <= 0):
        return None  # the lines meet outside this segment: a T-junction, again exact by projection
    return (_div_round(ax * den + t_num * rx, den) / 1e6, _div_round(ay * den + t_num * ry, den) / 1e6)


def _seg_closest(p1: Pt, p2: Pt, p3: Pt, p4: Pt) -> tuple[Pt, Pt, float]:
    """The closest pair between two segments: their crossing point when they properly cross, and
    otherwise the best of the four endpoint-on-the-other-segment projections.

    **The crossing case is not a refinement, it is the difference between an anchor and no anchor.**
    Projection alone returns a *positive* number for two segments that cross — `_seg_closest(
    (-1,0),(1,0),(0,-1),(0,1))` gave 1.0 where `gap` on the same pair of 0.2 mm tracks gives -0.2 —
    and `holds` reads a positive number past `reach` as "these two are not in contact" and records
    nothing at all. Both chains of a crossing then relaxed as if they were strangers, and on blinky's
    `LED` net two chains whose only join was a crossing came apart: **0 orphaned copper items before,
    1 after**, with `components` calling the net one group either way, because it groups pads and the
    orphan has none. The docstring this replaces said a proper crossing "is already contact by any
    reading"; it is, and this is now the reading the code uses.

    **Symmetric in its two arguments**, which is the property the anchor rule rests on: both chains
    of a contact derive the same witness point from the same pair of segments, so each may keep its
    own half of the distance without the two drifting apart. `_seg_meet` keeps that promise through
    exact integer arithmetic; the projection branch keeps it through the caller ordering the two
    segments canonically before calling (`holds`), because the tie-break below reads the pair in
    argument order and two collinear overlapping segments have a tie at distance zero at both ends.
    """
    hit = _seg_meet(p1, p2, p3, p4)
    if hit is not None:
        return (hit, hit, 0.0)
    best: tuple[Pt, Pt, float] | None = None
    for p, a, b, flip in ((p1, p3, p4, False), (p2, p3, p4, False), (p3, p1, p2, True), (p4, p1, p2, True)):
        dx, dy = b[0] - a[0], b[1] - a[1]
        d2 = dx * dx + dy * dy
        t = 0.0 if d2 <= 0.0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d2))
        foot = (a[0] + t * dx, a[1] + t * dy)
        d = math.dist(p, foot)
        pair = (foot, p, d) if flip else (p, foot, d)
        if best is None or d < best[2] or (d == best[2] and (pair[0], pair[1]) < (best[0], best[1])):
            best = pair
    assert best is not None
    return best


@dataclass(frozen=True)
class Holds:
    """What a chain must still be touching when it stops moving — `docs/quality-plan.md` C3.

    **The endpoint-only design is known-wrong and it is written down here because it looks correct
    in review.** "Freeze the two ends and any vertex where three tracks meet, then pull the middle"
    is what both independent prototypes shipped first, and both broke real nets on real boards: one
    took c3_usb's `track_dangling` from 1 to 2, the other broke `5V`, `VBUS`, `AIN0` and `VSS`
    across three boards. Two implementations failing the same way and converging on the same fix is
    the strongest single piece of evidence in the four designs.

    What it misses is that copper does not connect at vertices, it connects where it **overlaps**. A
    track crossing another track of its own net mid-span, a stub landing halfway along a pad, a run
    passing over its own net's via — none of those has a vertex anywhere near the contact, and every
    one of them is a connection KiCad counts. So the preserve set is the geometric contact set:
    every pad, every via and every other piece of same-net track copper whose shape the chain
    currently touches, with a witness point for each. `shapes` and `anchors` are the two halves of
    that set and `holds` is where each is chosen.

    **"Still touching" is not the requirement, and asking only for it ships a board that can fail in
    fabrication.** The first version of this class required `gap(...) <= 0.0` and no more, which a
    rounded end cap satisfies while merely grazing the pad outline. The `trim` move in `_moves` moves
    a run's ends, so that bound is reachable and the pass reached it: buck's `EN` track retracted
    from **0.1985 mm of overlap into `R_EN.2` to 0.0235 mm**, and 23.5 um is inside JLCPCB's etch
    tolerance — the joint can open on a board that pcbc, KiCad's DRC and `components` all call
    connected. (It also made the reported win a lie: buck's `EN` detour "improved" 1.09 -> 0.77, a
    ratio below 1, which can only mean the copper no longer spans the pad centres it joins.)

    So each preserved shape carries **`hold2`, the largest centreline reach it will accept**, and the
    number is the board's rather than a constant of this module's choosing:

        hold2 = max((shape.r - floor)**2, reach2_of_the_input)   # a pad, floor = stackup.clearance_min
        hold2 = max( shape.r**2,          reach2_of_the_input)   # a via, an immovable track

    `shape.r` is the offset radius of the thing being joined — a pad's corner radius, a via's radius,
    the half-width of an immovable track — so `reach <= shape.r` says **the run's centreline is
    inside the other copper**, which is the hand-routing rule for a joint and the only one that
    survives etch. Nothing here is tuned; delete both terms and the numbers in the paragraph above
    come back.

    **`floor` is on a pad only, and it is here because the centreline rule alone was not enough.**
    The sentence that stood here said "this pass may never make a joint shallower than it found it",
    and that is not what `max(shape.r**2, ...)` says: the `max` raises the bound only where the
    *input* was already outside the copper, so a joint that started well inside a pad could legally
    retract to the copper's outer edge — and did. Measured on the five boards, 2026-09-21: a `trim`
    drops the leg that runs to a pad's centre and the surviving run joins the pad by clipping it, so
    **27 of 302 pads lost overlap depth**, three of them to less than the board's own
    `stackup.clearance_min` — c3_usb's `C_EN.2` **0.2700 -> 0.0760 mm** and `U1.23` 0.2 -> 0.1
    against a 0.127 mm floor, ds2's `R5.2` **0.5075 -> 0.1175 mm**. 76 um is three times the 23.5 um
    the paragraph above calls unshippable and it is still thinner than the smallest feature either
    fab is being asked to hold. So a pad joint must keep `clearance_min` of centreline **inside** the
    pad's copper.

    **The floor can only be spent out of `shape.r`, and on a square pad there is none to spend.**
    `_reach2` is `hull_dist2`, which is 0 for *any* point inside the hull, so the only thing a bound
    of 0 can say is "the centreline is in the outline" — and a `rect` pad's `Shape` carries `r = 0`,
    so its whole copper **is** its hull. c3_usb's `U1.23` and `J1.A7` are rects (0.8 x 0.4 and
    0.3 x 1.3 mm), which is why `U1.23` still goes 0.2 -> **0.1 mm** of penetration with the floor
    on: the run slides along the pad's edge and stays inside it, and no bound expressible here
    forbids that. A rule that could would need a signed depth rather than a distance, and it is the
    first thing to build if a square pad ever opens in fabrication.

    **What the floor costs, measured both ways on fresh builds of all five boards:** blinky, buck and
    node come out *byte-identical*; c3_usb pays **one accept (38 -> 37), one segment, 0.28 mm of GND
    leftover and 0.10 deg/mm** (32.99 -> 33.09) and ds2 pays **one segment, 0.56 mm and 0.07 deg/mm**
    (14.67 -> 14.74). What it buys is two of those joints back outright — `C_EN.2` and `R5.2` stop
    retracting at all, ending at 0.2800 and 0.5125 mm, *deeper* than the copper the router handed in
    — so c3_usb's shallowest pad overlap goes 0.0760 -> 0.1000 mm and ds2's 0.1175 -> 0.1515, and
    **no** pad with a rounded outline is left shallower than `clearance_min` inside its copper.
    `U1.23` is the third and the floor cannot reach it, for the reason above.

    The floor is **not** applied to a via or to an immovable track, and the reason is that neither is
    a solder joint: a track meeting a track is one conductor continuing, and its input reach is
    already 0 wherever the two share a vertex or cross, so a floor there would forbid moves without
    protecting anything a fab can open.

    The `max` with the input's own reach is what keeps the rule honest on copper the router already
    left grazing: this pass may never make a joint shallower than **the rule** allows and is never
    asked for one deeper than it, so every input satisfies its own requirement by construction and no
    board can be frozen by it.

    **What it does not cover, said out loud: a filled zone.** A track lying inside a same-net pour is
    connected to it and no `Item` in the scene says so — `route_scene` holds pads, holes, tracks,
    vias, keepouts, lanes and the edge, and a zone's fill polygon is none of them. That contact is
    therefore checked by the arbiter and not by this rule: `route_scene.components` joins a net
    through the plane only via its vias, and KiCad's own unconnected-items count is what would report
    a run that lost its pour. Measured zero on buck, ds2 and blinky; a net that reaches its plane
    through a *track* alone, with no via and no pad on the plane's layer, is the case to look at
    first if that ever changes.
    """

    shapes: tuple[tuple[Shape, float], ...] = ()
    anchors: tuple[tuple[Pt, float], ...] = ()

    def ok(self, pts: tuple[Pt, ...], w: float, tally: dict) -> bool:
        for shape, hold2 in self.shapes:
            # `+ 1e-12 mm**2` is not a margin, it is a last-bit tie-break: a candidate that reuses
            # one of the input's own legs must compare equal to the requirement that leg produced
            # rather than lose to it. On the square it is worth `sqrt(hold2 + 1e-12) - sqrt(hold2)`
            # of reach, which is 3.7 femtometres against blinky's `R1.2` (`hold2 = 0.135**2`) and at
            # its very worst — `hold2 = 0`, a square pad whose copper the centreline must land in —
            # exactly **one nanometre**, which is the grid KiCad stores and the smallest distance
            # the board file can express at all.
            if _reach2(shape, pts, w) > hold2 + 1e-12:
                tally["hold"] = tally.get("hold", 0) + 1
                return False
        for wit, d0 in self.anchors:
            if _poly_pt_d(pts, wit) > d0 + 1e-9:
                tally["anchor"] = tally.get("anchor", 0) + 1
                return False
        return True


def orphan_copper(scene, net: str) -> float:
    """Pieces of this net's track and via copper that reach no pad of it — what `components` cannot
    see, and therefore what the whole-board self-check was missing.

    `components` returns the net's **pads**, partitioned. Copper that comes adrift while every pad
    stays connected changes nothing it reports, so the self-check built on it is structurally blind
    to a whole class of damage — and the class is not hypothetical. A `_seg_closest` that could not
    see a proper crossing let blinky's `LED` net split into two runs joined by nothing: **0 orphaned
    items before, 1 after**, `components(LED)` one group either way. That is the `track_dangling
    1 -> 2` failure `docs/quality-plan.md` C3 records two prototypes dying of, and it reached the
    shipped rule too. So the board is asked this second question, and before is compared with after.

    The items and the touch predicate are `route_scene.components`': `_touch` is imported rather
    than restated, because two readers of one geometric question is the defect this pass has already
    paid for once (`route_emit.strip_segments`). Only the question asked of the groups differs.

    It is a **comparison** and never an absolute, because a track lying inside a same-net pour is
    connected to it and no `Item` says so (`Holds`): such a track reads as orphaned on both sides of
    the pass and cancels. What cannot cancel is copper this pass set adrift.
    """
    from .route_scene import _touch

    mine = [it for it in scene.items if it.net == net and it.kind in ("pad", "track", "via")]
    parent = list(range(len(mine)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for i in range(len(mine)):
        for j in range(i + 1, len(mine)):
            if _touch(mine[i], mine[j]):
                union(i, j)
    plane = scene.plane_of.get(net)
    if plane is not None:
        on_plane = [i for i, it in enumerate(mine) if it.kind == "via" and plane in it.layers]
        for i in on_plane[1:]:
            union(on_plane[0], i)
    grounded = {find(i) for i, it in enumerate(mine) if it.kind == "pad"}
    # **Millimetres, not items**, and the second adversarial pass is why. Reducing the item count is
    # this pass's whole purpose, so a count comparison gives the guard headroom proportional to how
    # well the pass worked: measured, one connected 8-segment island that touches no pad reads 8
    # before and 1 after, and those 7 counts of slack hid the very defect the guard was added for —
    # with the island present, the blinky `LED` crossing that orphans a run went 8 -> 2 and the
    # guard stayed silent. Length is the invariant the pass does not change: merging eight collinear
    # legs into one leaves the same millimetres adrift, while setting a run adrift adds its own.
    return round(sum(_extent(it) for i, it in enumerate(mine) if it.kind != "pad" and find(i) not in grounded), 4)


def _extent(it) -> float:
    """How much copper an item is, in millimetres of its own longest dimension.

    A track's `Shape` is a capsule, so this is its length plus its two caps; a via's hull is a point,
    so it is the barrel's diameter. It does not have to be an area — it has to be a quantity this
    pass cannot reduce by tidying, which a **count** demonstrably is (see `orphan_copper`)."""
    if it.copper is None or not it.copper.pts:
        return 0.0
    pts = it.copper.pts
    span = max(math.dist(a, b) for a in pts for b in pts) if len(pts) > 1 else 0.0
    # Centreline only for a track, because the caps are what breaks the invariant: eight collinear
    # 0.5 mm legs and the one 4 mm leg they tidy into are the same copper, and they must measure the
    # same. Counting each leg's two caps made them 5.6 and 4.2, which is the count version's slack
    # in millimetres rather than items. A via's hull is a point, so its span is zero and its barrel
    # diameter is what it is.
    return span if span > 0.0 else 2.0 * it.copper.r


def holds(chain: Chain, pads: list, vias: list, others: list[dict], movable: frozenset, *, floor: float = 0.0) -> Holds:
    """The contact set of one chain, computed once against the board as it stands.

    Two rules, and which one applies turns on whether the *other* side of the contact can itself
    move. Against copper that cannot — every pad, every via, and every track in a chain this pass
    will not or cannot rewrite (`movable` is the keys of the chains with an interior to pull) — the
    rule is direct and exact: **the candidate must reach as far into that shape as `hold2` says**,
    wherever along it. A run is therefore free to slide *along* the stub or the pad it lands on,
    which is how the doubled-back spur a maze router leaves at a junction gets removed rather than
    preserved; what it may not do is back out of one, which is what the first version of this rule
    let a `trim` do (`Holds`).

    `_hold2` is where the requirement is read off the board rather than chosen: the larger of the
    shape's own offset radius — the reach at which the run's centreline is inside its copper, less
    `floor` on a pad, where the centreline has to be that far *inside* — and the reach the input
    already had. Squared, because `Holds.ok` decides on squares. `floor` is `stackup.clearance_min`
    and `Holds` is where the three joints that made it necessary are measured; it defaults to 0.0 so
    that a caller asking only "is the centreline in the copper" still gets exactly that question.

    Against copper that can move, the direct rule is not available — the other side is not where it
    will end up — so both sides keep their own distance to a **shared witness point**, derived from
    the same pair of segments in a canonical order so the two derivations cannot disagree. Each
    distance was at most half the overlap that produced the witness, so two halves that never grow
    still meet. This is the conservative half of the rule and it pins a junction exactly.
    """

    def _hold2(sh: Shape, keep: float = 0.0) -> tuple[Shape, float]:
        want = max(0.0, sh.r - keep)
        return (sh, max(want * want, _reach2(sh, chain.pts, chain.width)))

    shapes = [_hold2(it.copper, floor) for it in pads if chain.layer in it.layers and it.copper is not None and _touches(it.copper, chain.pts, chain.width)]
    shapes += [_hold2(sh) for sh in (via_shape(qp(v["at"]), v["size"]) for v in vias) if _touches(sh, chain.pts, chain.width)]
    anchors: list[tuple[Pt, float]] = []
    own = frozenset(chain.keys)
    for s in others:
        key = seg_key(s["layer"], s["start"], s["end"], s["width"])
        if key in own:
            continue
        a, b = qp(s["start"]), qp(s["end"])
        if a == b:
            continue
        if key not in movable:
            shape = track_shape(a, b, s["width"])
            if _touches(shape, chain.pts, chain.width):
                shapes.append(_hold2(shape))
            continue
        reach = (chain.width + s["width"]) * 0.5
        for u, v in zip(chain.pts, chain.pts[1:]):
            first, second = ((u, v), (a, b)) if (u, v) <= (a, b) else ((a, b), (u, v))
            pa, pb, d = _seg_closest(first[0], first[1], second[0], second[1])
            if d > reach:
                continue
            wit = qp(((pa[0] + pb[0]) * 0.5, (pa[1] + pb[1]) * 0.5))
            anchors.append((wit, _poly_pt_d(chain.pts, wit)))
    return Holds(tuple(shapes), tuple(sorted(set(anchors))))


# --- chains ---------------------------------------------------------------------------------------


def _straighten(pts: tuple[Pt, ...]) -> tuple[Pt, ...]:
    """Consecutive duplicates and collinear interior vertices dropped.

    **This changes the segment count and not one nanometre of copper.** Two collinear segments of one
    width on one net are the same copper as the single segment that spans them, so every clearance,
    every contact and every `Holds` witness is untouched, while `track_segment_length` loses a
    warning and the board loses a vertex no person would have drawn. It is applied to the chain the
    relaxer is handed as well as to every candidate, so the score a chain starts from is the score of
    the copper it actually is.

    Collinearity is decided on integer nanometres — the cross product of the two deltas is exactly
    zero or it is not — and a *reversal* (collinear, opposite direction) is kept: that vertex is
    where the copper doubles back, and dropping it would delete a leg.
    """
    out: list[Pt] = []
    for p in pts:
        if out and out[-1] == p:
            continue
        if len(out) >= 2:
            a, c = out[-2], out[-1]
            ux, uy = round((c[0] - a[0]) * 1e6), round((c[1] - a[1]) * 1e6)
            vx, vy = round((p[0] - c[0]) * 1e6), round((p[1] - c[1]) * 1e6)
            if ux * vy == uy * vx and ux * vx + uy * vy > 0:
                out[-1] = p
                continue
        out.append(p)
    return tuple(out)


def _moves(cur: tuple[Pt, ...]):
    """Every candidate shape for one run, in the fixed order first-accept depends on.

    `(candidate, new_copper)`, where `new_copper` is `None` when the candidate writes none — the
    generator's first two moves **delete** a leg and draw nothing, so there is no clearance question
    to ask about them and `blocked` is not called.

    Those two are the trims, and they are here because a maze router's junctions are where its worst
    corners live: a run that lands on a stub and then doubles back 0.377 mm along it before turning
    away is three of buck's seven remaining bad corners, and no pull can touch it, because a pull
    rewrites the inside of a run and this is its end.

    **A trim moves an endpoint, and it is the only move here that does.** Say it plainly, because
    the docstrings that used to surround this one all claimed the opposite and that is why nobody
    checked what the trim was being held to. It was being held to "the copper still touches", which
    a rounded end cap satisfies while grazing a pad outline — measured on buck: `R_EN.2`'s overlap
    198.5 um -> 23.5 um, inside JLCPCB's etch tolerance, with pcbc, KiCad DRC and
    `route_scene.components` all still calling the joint connected. `Holds` now requires the run's
    **centreline** to stay inside whatever it joins — and `clearance_min` inside a **pad**, which is
    what stopped the same trim cutting c3_usb's `C_EN.2` to 76 um — which is the hand rule for a
    joint and is what lets the run slide *along* a stub or a pad, removing the spur, without backing
    out of it.

    Then the string-pull proper: replace `cur[i..j]` with the two-leg octilinear path between its
    ends, `i` ascending, `j` descending, diagonal leg first and then last. A run's two ends are never
    in the interior of a pull, so a chain boundary moves only when a trim is separately accepted
    for it.
    """
    if len(cur) >= 3:
        yield _straighten(tuple(cur[1:])), None
        yield _straighten(tuple(cur[:-1])), None
    n = len(cur)
    for i in range(0, n - 2):
        for j in range(n - 1, i + 1, -1):
            if j - i < 2:
                continue
            for diagonal_first in (True, False):
                try:
                    sub = octile_path(cur[i], cur[j], diagonal_first=diagonal_first)
                except ValueError:
                    continue  # the two ends are one point: a closed run, and not a route
                yield _straighten(tuple(cur[:i]) + sub + tuple(cur[j + 1 :])), sub


def chains_of(segs: list[dict], reason_of) -> tuple[list[Chain], list[dict]]:
    """Every maximal junction-free run, grouped by `(net, layer, width, reason)`, in a fixed order.

    Walked over **segment indices** rather than over points, so a board carrying two identical
    segments keeps both: a point-keyed walk silently merges them and the pass would then delete
    copper it never accounted for. A run stops wherever more than two of the group's segments meet,
    so a junction is a chain boundary and never the interior of a pull.

    **A chain boundary is not a fixed point of the pass**, and an earlier version of this sentence
    said it was. The `trim` in `_moves` retracts a chain's leading or trailing leg, which moves the
    end; what keeps that safe is `Holds`, not the walk. Chain boundaries are only where the pass
    stops *straightening*.

    Returns the chains and the segments no chain claimed, so the caller can assert the partition:
    every segment on the board is in exactly one of the two lists.
    """
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, s in enumerate(segs):
        groups[(s["net"], s["layer"], round(s["width"], 4), reason_of(s))].append(i)
    out: list[Chain] = []
    left: list[dict] = []
    for key in sorted(groups):
        net, layer, width, reason = key
        idx = sorted(groups[key])
        if reason not in RELAXABLE:
            left += [segs[i] for i in idx]
            continue
        ends: dict[int, tuple[Pt, Pt]] = {}
        adj: dict[Pt, list[int]] = defaultdict(list)
        for i in idx:
            a, b = qp(segs[i]["start"]), qp(segs[i]["end"])
            if a == b:
                continue  # a zero-length segment is not a leg; `copper_bar` counts it and nothing moves it
            ends[i] = (a, b)
            adj[a].append(i)
            adj[b].append(i)
        used: set[int] = set()

        def walk(start: Pt, first: int) -> Chain:
            pts, mine, here, seg = [start], [], start, first
            while True:
                used.add(seg)
                mine.append(seg)
                a, b = ends[seg]
                here = b if a == here else a
                pts.append(here)
                nxt = [k for k in adj[here] if k not in used]
                if len(adj[here]) != 2 or len(nxt) != 1:
                    break
                seg = nxt[0]
            return Chain(net, layer, width, reason, tuple(pts), tuple(seg_key(layer, segs[k]["start"], segs[k]["end"], segs[k]["width"]) for k in mine))

        for start in sorted(adj):  # every open end first, in coordinate order
            if len(adj[start]) == 2:
                continue
            for first in sorted(adj[start]):
                if first not in used:
                    out.append(walk(start, first))
        for start in sorted(adj):  # then whatever is left, which is a closed loop, opened at its lowest point
            for first in sorted(adj[start]):
                if first not in used:
                    out.append(walk(start, first))
        left += [segs[i] for i in idx if i not in used]
    return out, left


# --- the pass -------------------------------------------------------------------------------------


def _skip_nets(job: "CompiledJob", design: "Design") -> frozenset[str]:
    """Nets this pass will not touch a chain of, however the copper on them is labelled.

    Two kinds, and both are `docs/quality-plan.md` C4 read one level up from the reason:

    **The constrained nets** — `NetReq(vias=False)` or a single-layer `NetReq` — are exactly the nets
    `route.krt_plan` routes on their own layers while the board is empty and `route.lock_copper` then
    locks, so that no later step can finish them with the vias their `NetReq` forbids. Their copper is
    `leftover` by reason and untouchable by intent, and the lock in the file is the intent's only
    mark. Derived from the same `job.nets` scan `krt_plan` uses so the two cannot drift; the lock is
    read as well, belt and braces, in `relax_board._reason_of`.

    **Differential pairs.** Not named in C4 — no board the panel measured has one where it matters —
    and skipped here on the argument C4 makes for the gates: the pair step is the only one that emits
    matched geometry, and `skew_*` and `uncoupled_*` are rules in millimetres that were satisfied by
    construction when the two nets were routed together. Relaxing one half of a pair on its own is
    exactly the move that breaks them. **Unmeasured on this slice's two boards**, because neither
    buck nor ds2 declares a pair; c3_usb does, and slice 2 owns it.
    """
    from fnmatch import fnmatch

    out: set[str] = set()
    for cn in job.nets:
        if cn.autoroute == "diff_pair" or cn.vias is False or len(cn.layers) == 1:
            out.update(n for n in design.nets if any(fnmatch(n, p) for p in cn.patterns))
    return frozenset(out)


def relax_board(
    design: "Design",
    job: "CompiledJob",
    cs: "ConstraintSet",
    text: str,
    board: str,
    *,
    owned=(),
    owned_steps=(),
) -> RelaxResult:
    """Pull every relaxable run of copper on this board taut, and hand back the rewritten board.

    `owned` is every piece pcbc has written in this route, in emission order, and `owned_steps` names
    the step file that wrote each one. Both come back rewritten: a relaxed `tap` stub is still a
    `tap` — `copper_bar` reads a piece's reason off `route.bar_key(p)` and would call pcbc's own
    copper leftover the moment it moved — but the step that wrote its present geometry is this one,
    and `copper.json` says so.

    A chain that does not move is not rewritten at all: its bytes, its uuid and its position in the
    file are the ones the step before this one wrote.
    """
    from .copper_bar import segments as bar_segments, vias as bar_vias

    t0 = time.perf_counter()
    scene = build_scene(design, job, cs, text)
    segs = bar_segments(text)
    vias = bar_vias(text)
    skip = _skip_nets(job, design)
    owned = tuple(owned)
    owned_steps = tuple(owned_steps) + ("",) * (len(owned) - len(owned_steps))
    reason_at = {seg_key(p.layer, p.a, p.b, p.w): (p.reason, i) for i, p in enumerate(owned) if p.kind == "seg"}

    def _reason_of(s: dict) -> str:
        """A segment's reason, and the two ways this pass declines to touch it.

        A locked segment pcbc does not own is `route.lock_copper`'s work on a constrained net, and
        reading it off the file is the same fact `_skip_nets` derives from `job.nets`. Both are here
        because they fail differently — the net list goes stale if `krt_plan` changes and this does
        not, the lock goes stale if some future step locks something new — and a pass that rewrites
        copper should need both to agree before it moves anything.
        """
        hit = reason_at.get(seg_key(s["layer"], s["start"], s["end"], s["width"]))
        if hit is not None:
            return hit[0]
        if s["net"] in skip or s["locked"]:
            return "locked"
        return "leftover"

    chains, untouched = chains_of(segs, _reason_of)
    claimed = sum(len(c.keys) for c in chains)
    if claimed + len(untouched) != len(segs):  # pragma: no cover - the partition is the contract
        raise ValueError(f"relax: {len(segs)} segments split into {claimed} chained and {len(untouched)} left")

    pads_by_net: dict[str, list] = defaultdict(list)
    for it in scene.items:
        if it.kind == "pad" and it.net:
            pads_by_net[it.net].append(it)
    vias_by_net: dict[str, list] = defaultdict(list)
    for v in vias:
        vias_by_net[v["net"]].append(v)
    by_nl: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in segs:
        by_nl[(s["net"], s["layer"])].append(s)
    # The scene item behind each segment, so a chain that moves can take its old copper out of every
    # later chain's way. Without it two chains that both move are each judged against where the other
    # used to be, which is the one way this pass could write a clearance error KiCad then finds.
    track_id: dict[tuple, list[int]] = defaultdict(list)
    for it in scene.items:
        if it.kind == "track" and it.copper is not None and len(it.copper.pts) == 2:
            track_id[seg_key(next(iter(it.layers)), it.copper.pts[0], it.copper.pts[1], it.copper.r * 2.0)].append(it.id)
    # Both halves of the self-check's "before", taken while the scene is still the input's: the loop
    # below `scene.add`s every chain it moves, so a snapshot taken later would be of a board that is
    # half relaxed.
    was = {net: components(scene, net) for net in sorted({c.net for c in chains})}
    was_loose = {net: orphan_copper(scene, net) for net in sorted(was)}

    dead: set[int] = set()
    tally: dict[str, int] = {}
    stats: dict = dict(chains=len(chains), moved=0, accepts=0, tried=0, passes=0, mm_saved=0.0)
    final: list[Chain] = []
    # A run with no interior has no move (`_moves` yields nothing under three points), so its copper
    # is as fixed as a spine's and `holds` may use the direct contact rule against it.
    movable = frozenset(k for c in chains if len(c.pts) >= 3 for k in c.keys)
    for chain in chains:
        others = by_nl[(chain.net, chain.layer)]
        hold = holds(chain, pads_by_net[chain.net], vias_by_net[chain.net], others, movable, floor=cs.stackup.clearance_min)
        own = frozenset(chain.keys)
        ends = tuple(
            tuple(
                sorted(
                    {
                        (qp(s["end"]) if qp(s["start"]) == end else qp(s["start"]))
                        for s in others
                        if seg_key(s["layer"], s["start"], s["end"], s["width"]) not in own and end in (qp(s["start"]), qp(s["end"]))
                    }
                )
            )
            for end in (chain.pts[0], chain.pts[-1])
        )
        cur = _straighten(chain.pts)
        best = quality(cur, ends)
        improved, passes = True, 0
        while improved and passes < _ITER_CAP:
            improved = False
            passes += 1
            for cand, drawn in _moves(cur):
                stats["tried"] += 1
                if len(cand) < 2:
                    continue
                score = quality(cand, ends)
                if score >= best:
                    tally["cost"] = tally.get("cost", 0) + 1
                    continue
                if not hold.ok(cand, chain.width, tally):
                    continue
                if drawn is not None:
                    probe = [seg_piece(chain.net, "relax", chain.layer, a, b, chain.width) for a, b in zip(drawn, drawn[1:])]
                    if blocked(scene, probe, chain.net, ignore=frozenset(dead)) is not None:
                        tally["blocked"] = tally.get("blocked", 0) + 1
                        continue
                cur, best, improved = cand, score, True
                stats["accepts"] += 1
                break
        stats["passes"] += passes
        got = Chain(chain.net, chain.layer, chain.width, chain.reason, cur, chain.keys)
        final.append(got)
        if cur != chain.pts:
            stats["moved"] += 1
            stats["mm_saved"] += math.fsum(math.dist(a, b) for a, b in zip(chain.pts, chain.pts[1:])) - math.fsum(math.dist(a, b) for a, b in zip(cur, cur[1:]))
            for key in chain.keys:
                dead.update(track_id.get(key, ()))
            scene.add(scene.item_of(seg_piece(got.net, got.reason, got.layer, a, b, got.width)) for a, b in zip(cur, cur[1:]))
    stats["mm_saved"] = round(stats["mm_saved"], 4)
    stats["rejected"] = dict(sorted(tally.items()))

    # --- emit -------------------------------------------------------------------------------------
    keys: list[tuple] = []
    items: list[str] = []
    stood_in: dict[int, list[Piece]] = {}
    consumed: set[int] = set()
    out_segs = 0
    for before, after in zip(chains, final):
        if after.pts == before.pts:
            out_segs += len(before.keys)
            continue
        keys += list(after.keys)
        mine = sorted({reason_at[k][1] for k in after.keys if k in reason_at})
        consumed.update(mine)
        owner = owned[mine[0]].owner if mine else ""
        made: list[Piece] = []
        for a, b in zip(after.pts, after.pts[1:]):
            uid = stable_uuid(board, "relax", after.reason, after.net, after.layer, f"{a[0]:.6f}", f"{a[1]:.6f}", f"{b[0]:.6f}", f"{b[1]:.6f}", f"{after.width:g}")
            piece = seg_piece(after.net, after.reason, after.layer, a, b, after.width, owner=owner, uuid=uid)
            items.append(piece_text(piece, locked=after.reason != "leftover"))
            made.append(piece)
            out_segs += 1
        if mine:
            stood_in[mine[0]] = made
    new_owned: list[Piece] = []
    new_steps: list[str] = []
    for i, p in enumerate(owned):
        if i in stood_in:
            new_owned += stood_in[i]
            new_steps += ["relax"] * len(stood_in[i])
        elif i not in consumed:
            new_owned.append(p)
            new_steps.append(owned_steps[i])
    out_text = replace_segments(text, keys, items) if keys else text

    # --- the whole-board self-check ---------------------------------------------------------------
    # Per candidate this pass keeps contact by a witness rule; here it asks the board. `components` is
    # what `net_open` reads, so a net whose pads fall into the same number of connected groups is a
    # net pcbc itself calls no less connected than it was — which is the claim the whole step rests
    # on, checked rather than argued. `pattern_copper` raises on its own self-check for this reason,
    # and this raises for the same one: a broken net is not a board move, it is a bug in this module.
    #
    # And `orphan_copper` beside it, because `components` groups **pads** and is therefore blind to
    # copper that comes adrift while every pad stays connected — the exact damage a crossing-blind
    # `_seg_closest` did to blinky's `LED`, invisible to the check above by construction. Same
    # precedent, same verdict: a board this pass disconnected copper on is a bug in this module.
    if keys:
        after_scene = build_scene(design, job, cs, out_text)
        for net in sorted(was):
            now = components(after_scene, net)
            if len(now) > len(was[net]):
                raise ValueError(f"relax broke {net}: its pads were {len(was[net])} connected group(s) and are now {len(now)}. This is a pcbc bug, not a board move")
            loose = orphan_copper(after_scene, net)
            if loose > was_loose[net] + 1e-6:
                raise ValueError(
                    f"relax orphaned copper on {net}: {was_loose[net]} piece(s) of its track/via copper reached no pad "
                    f"of it before and {loose} do now, while its pads stayed in {len(now)} group(s). This is a pcbc bug, "
                    "not a board move"
                )
        stats["orphans_in"] = sum(was_loose.values())
        stats["orphans_out"] = sum(orphan_copper(after_scene, net) for net in sorted(was))
    stats["segments_in"] = len(segs)
    stats["segments_out"] = len(untouched) + out_segs
    # **Ask the board, not the decision.** `segments_out` above is a statement about what this pass
    # chose to do; the two lines below are what the file actually carries. They are separate on
    # purpose: defect 1 was `strip_segments` silently degrading to an append, which left every old
    # staircase on the board under its taut replacement — 203 segments where a replace gives 73 —
    # while `segments_out` reported the decision's number and no self-check could see it, because
    # duplicate copper only ever *merges* connectivity groups. This comparison catches that class
    # outright, and it is the cheapest check in the module.
    from .copper_bar import _SEG

    on_board = len(_SEG.findall(out_text))
    if on_board != stats["segments_out"]:
        raise ValueError(
            f"relax wrote {on_board} segments but decided on {stats['segments_out']}: the replace did not "
            f"replace. This is a pcbc bug, not a board move — see `route_emit.strip_segments`."
        )
    stats["skipped_segments"] = len(untouched)
    note = (
        f"relax: {stats['moved']} of {len(chains)} chains pulled taut, {len(segs)} segments -> {stats['segments_out']}, "
        f"{stats['mm_saved']:g} mm shorter; {len(untouched)} segments skipped (a reason outside {'/'.join(RELAXABLE)}, or a constrained net)"
    )
    return RelaxResult(
        text=out_text,
        owned=tuple(new_owned),
        owned_steps=tuple(new_steps),
        notes=(note,),
        stats=stats,
        wall_ms=int(round((time.perf_counter() - t0) * 1000)),
    )
