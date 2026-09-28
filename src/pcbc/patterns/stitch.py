"""`stitch` — copper whose absence leaves nothing unconnected (`docs/stitch-plan.md` R-S1, S4).

Every other pattern in this package connects something: a hop joins two pads, a spine feeds a rail,
a tap welds a pad to its plane. Take any of their copper away and a pad is unreached. Take a stitch
away and the board still works — worse, but connected. That difference is the whole reason this
module can run in the `final` stage, **after the router**, where nothing can react to it: a stitch
that loses its site costs a shield or a barrel, not a pad, so no router step has to follow it and
`route_native.route_stage` runs none (only the relaxer, which does not move a stitch: `RELAXABLE`).

**Four carriers ship, and `CARRIERS` runs them least-free-first.** `parallel` (S4) doubles a barrel a
power rail is limited by — a barrel's rating is a **count**, not a width, since `via_amps` is the
drill and the plating, both the fab's, so no `Place()` and no shorter path moves another ampere
through it, and `stackup.vias_per_change` had compiled that count since R1 with nothing placing the
second via. `thermal` (S6) lays a lattice under an exposed pad at the pitch the antipads allow.
`guard` (S7) offsets two shield tracks beside a run pcbc routed and stitches them to the pour.
`plane` (S8) ties two pours of **one** net across the cavity between them, on an edge ring and an
interior lattice, at lambda/20 of the board's own declared fastest edge — R-E1, whose population did
not exist until `route_scene.plane_targets` learned to read a declared `planes=` on every stackup.

**The rung, and why it is not a bare via.** `ampacity._via_clusters` is single-linkage on distance
with **no connectivity test**, and `net_nodes` then hands every member `cluster_size x via_amps`. A
twin dropped 0.9 mm from an anchor and joined to nothing would therefore **double the reported
ampacity of a board carrying no more current** — pcbc gaming its own measurement. So a rung is a via
**plus one link segment on each of the two layers it spans, at the class width, unconditionally**
(`docs/stitch-plan.md` §2e), and `route_verify.parallel_joined` is the check that it cannot be
anything less. Measured on node, the one board with a population: the links cost sites. Of the three
under-rated `VBUS` groups on a fresh build, a bare via has a site at 2 of them and a rung at **1** —
and on the stale checked-in board §2(q) ran its ladder on the bare via and reported all four served,
where the rung serves two. The rung rule is expensive and it is not optional.

**The twin matches the anchor's size, not the net's class via** (`docs/stitch-plan.md` §2f). A mixed
cluster reports `n x via_amps(the drill the walk happens to read)`, and on node the class via is
0.8/0.4 while the anchor is the fab's 0.35/0.2: at the Power class's 0.8 mm ring two vias need
1.080 mm between centres against `ampacity.VIA_PARALLEL_MM` of 1.0, so a class-size partner **cannot
be placed at all**. One via must mean one thing.

**The two limits are the arithmetic's and the constant's, and neither is invented.** The nearest a
twin may sit is `ClearanceTable.via_pitch` — `hole_to_hole` between the drills, `between` between the
rings — and the furthest is `ampacity.VIA_PARALLEL_MM`, the measurement's own constant: a via beyond
it is copper the cluster walk will not count, so the pattern *cannot* place one. On node that window
is [0.700, 1.000] and the binding number inside it is a third one the pattern does not construct but
`route_scene.antipad_clash` judges — two 0.35 mm rings 0.85 mm apart leave exactly the 3V3 plane's
0.1 mm `min_thickness` between their antipads — which is why every site that fits on node fits at
0.9 mm and none at 0.8.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..ampacity import VIA_PARALLEL_MM
from ..constraints import ConstraintSet
from ..moves import move_line
from ..route_emit import Piece, seg_piece, via_piece
from ..route_geom import EPS_MM, MICRO_MM, Pt, clears, gap, q, track_shape, via_shape
from ..route_scene import Clash, Item, Run, Scene, antipad_clash, blocked, clear_runs, net_runs
from ..sexp import stable_uuid
from ..stackup import C_MM_PER_NS, Stackup, via_amps, vias_per_change
from . import PatternCtx, PatternResult, Refusal, _mm, _nm, lane_ok, mitre, pieces_of, shape_ok

if TYPE_CHECKING:  # pragma: no cover - the budget is carried, never constructed here
    from ..constraints import ThermalSpec

REASON = "stitch"

GUARD_REASON = "guard"
"""The reason a guard's tracks and stitch vias carry, and it is a third entry beside `REASON` and
`THERMAL_REASON` for `THERMAL_REASON`'s reasons exactly.

`route_verify.parallel_joined` walks every `reason == "stitch"` via and asks what it is a twin of; a
guard barrel has no anchor, so under one shared reason `build._barrel_gate`'s one fatal check would
fire on copper it was never about — which is what measurably happened to the thermal array before S6
split it. `route_emit.REASONS` has carried `"guard"` since R1, `copper_bar.totals["vias_pattern"]` is
already `{reason: n}`, and `route_verify.BRANCH_REASONS` gains it so a stitch via is judged as the fab's
standard via rather than as the ground class's, which on a `Power` class `GND` is a different number.

It is also what keeps a guard out of the route metrics: `copper_bar.REDUNDANT` holds the reasons whose
millimetres are not part of the net's route, and a shield beside a track is the R-S1 case in its
purest form — a 0.127 mm ground guard counted as `GND` copper would make `DETOURS` and
`ampacity.power_bottlenecks` both report a rail that got worse."""

THERMAL_REASON = "thermal"
"""The reason a `thermal` barrel carries, and it is **not** `REASON`.

One module, two carriers, two reasons — and the split is forced by the census rather than chosen for
tidiness. `route_verify.parallel_joined` walks every `reason == "stitch"` via out of the sidecar and
asks whether it is joined to the anchor it is a twin of; an array barrel has no anchor and no link,
so under one shared reason the first build put **nine** "a parallel via is not parallel" failures on
c3_usb and stopped, which is `build._barrel_gate`'s one fatal check firing on copper it was never
about (measured 2026-09-21, and it is why this constant exists).

Two reasons buy three more things that a discriminator inside the gate would not:

- `VIAS_PATTERN` and `OWNS` say **what the copper is for** per board — `{"tap": 34, "thermal": 9}`
  reads as a board, `{"stitch": 10}` reads as an accident.
- `route_verify._via_rules` asks a `stitch` via to match its **anchor's** size (section 2(f): one via
  must mean one thing in a cluster) and a `BRANCH_REASONS` via to be the fab's standard. An array
  barrel is the second, so with `"thermal"` in `BRANCH_REASONS` it is judged by the rule it belongs
  to instead of by whichever GND via the router happened to leave within a millimetre of it.
- `copper_bar.REDUNDANT` holds both out of the route metrics for one reason (R-S1) and names them
  both, so a reader of `DETOURS` can see the whole of what was excluded.

`patterns.FINAL` still registers the **module** as `"stitch"`; `_STAGES` maps a stage to modules and
`_census` keys on the **piece**, so the two live at different levels and neither has to know about the
other."""

# `ampacity.VIA_PARALLEL_MM`, imported rather than restated. It is not a reach constant this pattern
# chose: it is the distance the **measurement** treats as parallel, so a twin beyond it is copper
# `_via_clusters` will not count toward the rail and `power_bottlenecks` will never see. The pattern
# therefore *cannot* place one, whatever the board would allow. `tap` had to invent `TAP_REACH_MM`
# because nothing else knew how far a tap may step; this carrier is bounded by the very thing it is
# trying to move, which is the strongest form the house rule on numbers can take.

CONNECTS = False
"""A stitch's copper does not join the pads of the net it claims (`docs/stitch-plan.md` §2k).

The two halves of that are different for the two kinds of carrier and both end in `False`. A guard
(S7) writes `GND` copper to shield *another* net, so `claimed.add(res.net)` would make
`PatternPlan.done` say something false about a net no pattern routed. A **parallel** rung is on its own net and still connects no pad
to anything: the anchor was already joined to the rail and the twin is a second path beside it, so
"this net is now done" would be a claim about pad connectivity that the copper does not make.
`net_open` would answer the same before and after, which is exactly the sense in which a rung is
redundant copper (R-S1).

Read as an attribute by `pattern_copper`, never with a `getattr` default: see `hop.CONNECTS`."""

CARRIERS = ("parallel", "thermal", "guard", "plane")
"""The carriers this module ships, in the order `final` runs them (`docs/stitch-plan.md` C.1's least
free first). `parallel` has a ring of eight within 1 mm of one point — near-zero freedom, and it is
the only *required* one; `thermal` owns a region nothing else wants; `guard` owns a corridor, writes
**tracks** as well as barrels, and is the only carrier that reads another pattern's copper as input
geometry; `plane` owns the **whole board** — a lattice and an edge ring over every square millimetre
two pours of one net face each other across — which is the most freedom any carrier here has and puts
it last by the same rule that put `parallel` first. A spec naming a carrier that is not in this tuple is a programming error
rather than a refusal — `run` raises — because a carrier with no site generator has no candidate list
at all, and silently emitting nothing is how a pattern comes to pass its own tests by doing nothing."""

@dataclass(frozen=True)
class StitchSpec:
    """One thing the module has to place, with its candidate list already derived.

    A spec is the unit of the **refusal**, and that is the field that matters most here: one refusal
    per spec, never one per site (`docs/stitch-plan.md` §1.3). `REFUSED` in `test_examples_fab.py` is
    an exact number per board, and a per-site refusal would put 32 lines into it for one via that did
    not fit — which passes the pattern's own tests and destroys the ledger.
    """

    carrier: str  # "parallel" | "thermal" | "guard" — see CARRIERS
    net: str  # the net the vias carry
    sites: tuple[Pt, ...]  # the fixed, finite, ordered candidate list
    need: int  # how many vias this spec must place
    required: bool  # True: all-or-nothing; False: partial, with a Coverage record
    via: tuple[float, float]  # (diameter, drill) — the anchor's, never the class's (§2f)
    joins: tuple[str, str]  # the two layers every via must land in real copper on
    owner: str  # "VBUS via at (28,33.4)" — what the report and the refusal name
    why: str  # the arithmetic, for the report and the refusal
    bound: int  # the declared candidate bound: len(sites), stated so a test can pin it
    at: Pt = (0.0, 0.0)  # the anchor: where the links start (carrier "parallel")
    width: float = 0.0  # the link width: the class, never narrowed (§5)
    budget: "ThermalSpec | None" = None
    """The compiled `Thermal()` a `thermal` spec is placing, `None` for every other carrier.

    Carried whole rather than unpacked into fields because the *budget* is the compiler's and the
    *placement* is this module's, and a spec that copied the count out would have two readings of one
    number. `Coverage` asks it what `got` barrels achieve (`ThermalSpec.rise_of`) and the refusal
    quotes its line; nothing here recomputes a K/W."""

    pours: tuple[str, ...] = ()
    """Every layer this net is poured on (carrier `plane`), in stackup order; `()` for the rest.

    `joins` is the **pair whose cavity set the pitch** (`cavity_pair`) and this is the whole set,
    because the two answer different questions. Every via pcbc writes is a through via, so one lattice
    ties all of them and the reach test has to ask each outer pour separately whether its fill will
    arrive (`_raster`); the gate then asks the finished board whether every barrel landed in every
    one of them (`route_verify.plane_stitch`). Carrying the pair alone would check two layers of a
    three-layer pour and call it done."""

    blocks: tuple = ()
    """Carrier `thermal`: the land's pads, one entry per `(pad ...)` it is drawn as (the ESP32-C3-MINI's
    `49` is nine), each the boxes of that pad's primitives; `()` for the rest. A land drawn as several
    pads gets **a barrel in every one of them** before any gets a second (`run`): a block with none
    moves no heat through the board, and the gate on the finished board fails such a land with the
    blocker that emptied it (`route_verify.ThermalArray.bare`, the third refutation round)."""

    pitch: float = 0.0
    """The lattice's centre-to-centre, mm (carrier `plane`); 0.0 for the rest.

    Carried rather than recomputed because `Coverage` states the shortfall in the pitch's own terms —
    the highest edge the surviving lattice is still a lambda/20 stitch for — and a second call to
    `stitch_pitch` inside the note would be a second reading of one number."""

    guard: "GuardPath | None" = None
    """The run a `guard` spec shields and the two offset polylines it shields it with, `None` for
    every other carrier — and `None` on a `guard` spec too when the net's copper is not pcbc's (a
    core line, or none yet), which is the deferral of `docs/r2-design.md` B.6 and the one spec in this
    module built in order to write nothing.

    Carried whole for `budget`'s reason one step further: the *path* is an earlier stage's copper and
    the *offset* is arithmetic over the compiled clearances, so a spec that flattened them into fields
    would hold two readings of one geometry. `_run_guard` asks it for its stretches; nothing else
    recomputes an offset."""


@dataclass(frozen=True)
class Coverage:
    """What a **target** spec managed, when it is allowed to manage part of it (R-S3).

    R-S3 splits the patterns in two. A spec whose count is an *electrical requirement* — a rail that
    cannot carry its current through one barrel — is all-or-nothing: half the rungs is a rail that
    still cannot carry its current, so the honest answer is no copper and one sentence. A spec whose
    count is a *target* — a thermal array's conductance, a guard's stitch pitch — applies partially
    and says by how much, because half a thermal array really is half the conductance.

    **Coverage is a count and degradation is a geometry change, and the two are different axes.**
    B.0's "a pattern never degrades" keeps its full force here: a target spec may write *fewer* vias
    and never one at the wrong pitch, size, width or net. An implementer who trades one for the other
    — narrowing a link so a rung fits — has broken the rule that makes a pattern's copper
    predictable, and the record below cannot express it, deliberately.

    `parallel` sets `required=True` on every spec (`specs`), so nothing in this slice fills one in.
    It is exercised by `tests/test_stitch.py::test_a_target_spec_applies_partially_and_says_so`,
    which hands `run` a spec with `required=False`, rather than left as untested code waiting for S6.
    """

    want: int
    got: int
    measure: float  # what the copper that fitted achieves
    target: float  # what the spec was asked for
    unit: str  # "A" | "C" | "mm"
    floor_ok: bool  # did the copper that fitted meet the spec's own budget?
    why: str
    blocked: str = ""
    """What stopped the rest, in one clause, or "" when everything the spec asked for fitted.

    A target emits **one** note and no refusal, so this clause is the only place a reader is told
    *why* an array is short — and "8 of 9 placed" without it is a number nobody can act on. It is a
    summary rather than a blocker list on purpose: `REFUSED` counts refusals and this is not one, and
    a per-site line here would put thirty-six of them in a report that has room for one."""

    def line(self) -> str:
        """The one `style:` note a partial spec emits. One line, never one per missing site.

        The verdict word follows the **unit**, because the two carriers' measures run in opposite
        directions and a single sentence would be wrong for one of them: a rail wants **more**
        amperes than its target and a land wants **fewer** degrees than its budget. Getting this
        backwards prints "above its floor" on an array that is overheating, which is the one kind of
        report that is worse than no report."""
        if self.unit in ("K/W", "C"):
            verdict = "inside its budget" if self.floor_ok else "over its budget"
        else:
            verdict = "above its floor" if self.floor_ok else "under its floor"
        tail = f"; {self.blocked}" if self.blocked else ""
        return (
            f"style: stitch {self.why}: {self.got} of {self.want} placed, "
            f"{self.measure:g} {self.unit} of {self.target:g} {self.unit} ({verdict}){tail}"
        )


# --- the site generators ---------------------------------------------------------------------------


_RING = (
    ("right", (1, 0)),
    ("left", (-1, 0)),
    ("down", (0, 1)),
    ("up", (0, -1)),
    ("down-right", (1, 1)),
    ("down-left", (-1, 1)),
    ("up-right", (1, -1)),
    ("up-left", (-1, -1)),
)
"""The eight directions a twin may sit in, in `route_scene._SIDE_ORDER`'s order and then its
diagonals in the same order taken pairwise.

**Eight is forced, not chosen.** The link segment runs from the anchor to the twin, and
`route_verify.verify_copper` asks `is_octilinear` of every path pcbc writes — so a twin anywhere but
on one of the board's own eight headings is copper the pattern's own self-check raises on. The ring
is the octilinear discipline read as a candidate list."""


def sites_ring(ctx: PatternCtx, at: Pt, net: str, via: tuple[float, float], reach: float) -> tuple[Pt, ...]:
    """Carrier `parallel`: the ring of twin positions around one anchor, nearest first.

    `8 x len(radii)` sites in a fixed order — **distance first, direction second**, which is the
    opposite of `tap._sites` and for the opposite reason. A tap's first question is which *side* of
    its pad is open, because the pad has sides; a twin has none, and the only thing that separates
    one site from another electrically is how certainly the cluster walk will call it parallel. That
    walk is `math.dist(...) <= VIA_PARALLEL_MM`, so nearer is better with no ambiguity at all, and
    the list is ordered by it.

    The radii are the grid steps from `ClearanceTable.via_pitch` — `hole_to_hole` between the two
    drills, `between` between the two rings, whichever binds — up to `reach`, which is
    `ampacity.VIA_PARALLEL_MM`. Neither end is invented and neither is a reach constant this module
    had to name, unlike `tap.TAP_REACH_MM`: the near end is what the fab allows and the far end is
    what the measurement counts. Measured on node, `via_pitch("VBUS","VBUS",0.2,0.35,0.2,0.35)` is
    **0.700** and `reach` is 1.0, so the ladder is 0.7/0.8/0.9/1.0 and **32 sites** — the same 32
    `docs/stitch-plan.md` §2(q) enumerated by hand, and this function reproduces that measurement's
    per-via counts exactly on the board it was taken from.

    **The first radius sits exactly on `hole_to_hole` and `clears` refuses it**, because `clears`
    demands `need + EPS_MM` and 0.700 mm between two 0.2 mm drills leaves exactly the 0.5 mm the fab
    asks for. That is the boundary failing in the safe direction — a site refused too strictly costs
    a rung and prints a move, a site accepted too loosely costs a board — and the next candidate is a
    whole grid step away, a thousand times `EPS_MM`, so nothing marginal ever decides an answer here.

    **A 45 degree offset is floored to the nanometre, never rounded** (`_offsets`). At `reach` = 1.0
    the rounded half-diagonal is 0.707107 mm, which puts the twin 1.0000003 mm from its anchor — a
    third of a nanometre outside `VIA_PARALLEL_MM`, and therefore a via `_via_clusters` would not
    count. Copper that the measurement ignores is precisely what this carrier exists to not write.
    """
    dia, drill = via
    near = ctx.scene.table.via_pitch(net, net, drill, dia, drill, dia)
    grid = ctx.scene.grid
    out: list[Pt] = []
    r = near
    while r <= reach + 1e-9:
        for _name, d in _offsets(r):
            out.append((_mm(_nm(at[0]) + d[0]), _mm(_nm(at[1]) + d[1])))
        r = q(r + grid)
    return tuple(out)


def _offsets(rad: float) -> tuple[tuple[str, tuple[int, int]], ...]:
    """One radius as eight integer-nanometre offsets, in `_RING`'s order.

    Built in nanometres rather than in millimetres so the two coordinates of a diagonal move by the
    **same** integer: `q(x + d)` and `q(y + d)` can land a nanometre apart when x and y have
    different fractional parts, and a 45 degree leg that is off by one nanometre is not a 45 degree
    leg — it is the tilt `link_candidates` was written in integers to avoid (A.5).
    """
    n = _nm(rad)
    d = int(math.floor(rad * math.sqrt(0.5) * 1e6))  # floored: see `sites_ring`
    return tuple((name, (n * s[0], n * s[1]) if 0 in s else (d * s[0], d * s[1])) for name, s in _RING)


def thermal_pitch(scene: Scene, net: str, via: tuple[float, float]) -> tuple[float, str]:
    """Carrier `thermal`: the centre-to-centre a lattice of `net` vias is laid on, and why.

    **`antipad_clash`'s own arithmetic turned from a test into a constructor**, which is
    `docs/stitch-plan.md` section 2(h) and the single most useful correction the six technique designs
    made. R-T1 says "a grid of vias at the stackup's hole-to-hole" and that number is **wrong**, not
    merely loose: measured on node 2026-09-21, two 0.35 mm rings 0.700 mm apart cut antipads of radius
    `0.175 + 0.2` = 0.375 in the 3V3 plane, so the web between them is `0.700 - 0.750` = **-0.050 mm**
    and KiCad deletes the copper between them. The derived pitch is `2 * clearance + min_thickness +
    ring` = **0.8501** and leaves `+0.1001 mm`, a tenth of a micron over the zone's own
    `min_thickness`. And it places the **same number of vias**: a 1.45 mm block admits
    `1 + floor(1.100 / 0.700)` = 2 per axis and `1 + floor(1.100 / 0.8501)` = 2 per axis. R-T1's number
    writes identical copper and destroys the plane for nothing.

    Three terms, all of them somebody else's:

    - `ClearanceTable.via_pitch(net, net, ...)` — `hole_to_hole` between the two drills, `between`
      between the two rings, whichever binds. The same call `sites_ring` makes for the near end of the
      parallel ladder, so the two carriers cannot disagree about how close two vias of one net may be.
    - `antipad_pitch` — the foreign pour's neck, `2 * max(zone clearance, class clearance) +
      min_thickness + diameter`, and **zero when there is no foreign pour**, which is why a two-layer
      board's pitch is the first term alone.
    - `EPS_MM`, for `route_scene.pad_exits`' own reason in its own words: a candidate placed at
      exactly `need` is copper this module's own judge refuses, because `clears` demands
      `need + EPS_MM`. One nanometre is not a fudge factor; it is the boundary failing in the safe
      direction.

    Measured 2026-09-21 on the two boards that declare one:

        c3_usb  2L, one GND pour, no foreign plane   max(0.8000, 0) + EPS  ->  0.8001 mm
        node    4L, GND on In1.Cu, 3V3 on In2.Cu     max(0.7000, 0.85) + EPS -> 0.8501 mm

    The pitch does double duty and that is the strongest argument for deriving it. `PatternCtx`
    promises the scene is mutated only between patterns and never mid-candidate, so a pattern placing
    nine vias in one call **cannot** judge via 5 against vias 1-4 with `blocked`. It does not have to:
    these are exactly the two rules one array via could break against another, so within one call the
    sites clear each other **by construction**. A swept pitch would have to be re-judged against
    itself, and there would be nothing to judge it with.
    """
    dia, drill = via
    near = scene.table.via_pitch(net, net, drill, dia, drill, dia)
    anti, anti_why = antipad_pitch(scene, net, dia)
    if anti > near:
        return (next_nm(anti + EPS_MM), anti_why)
    return (next_nm(near + EPS_MM), f"the fab's hole_to_hole between two {drill:g} mm drills and the class clearance between two {dia:g} mm rings")


def next_nm(v: float) -> float:
    """One nanometre past `v`, on KiCad's grid — `docs/stitch-plan.md` section 2(i)'s own fix.

    The guard design measured this and it is the sharpest small lesson in the six technique designs:
    **at exactly `need + EPS_MM`, `clears` loses on the float boundary.** `clears` compares
    `hull_dist2 >= (need + radii + EPS_MM)**2` and `EPS_MM` arrives there through a different
    sequence of roundings than it does through a pitch, so two ways of writing the same number
    disagree in the last bit. The guard's own geometry sits exactly on the clearance on every leg,
    which is why that design met it first; a lattice at the process floor is the second.

    **It is not hypothetical here and the boundary was crossed both ways in one afternoon.** Measured
    2026-09-21 with `q(pitch + EPS_MM)`: the two-layer array placed its nine barrels and passed
    `verify_copper`, and the four-layer one placed twelve and **failed its own self-check** —
    `hole_to_hole` reported "0.5001 mm of 0.5 mm", a pair that reads as clearing and does not, six
    times. The two boards differ only in where their coordinates sit relative to a binary fraction.
    One nanometre — a ten-thousandth of the clearance it protects, and four orders below any fab's
    tolerance — makes the answer a property of the arithmetic instead of of the address.

    A separate function rather than a `+ 1e-6` at the call site because S7's guard offset needs the
    same thing (`docs/stitch-plan.md` section 2(i): `next_nm(w/2 + between + max(track_min,
    via_dia)/2 + EPS_MM)`), and because "one nanometre past" is a claim that has to survive `q`'s own
    tie rule: `_nm` is the exact integer nanometre count, so this is integer arithmetic and not a
    float nudge that might round back."""
    return _mm(_nm(v) + 1)


def sites_lattice(ctx: PatternCtx, boxes: tuple[tuple[float, float, float, float], ...], pitch: float, dia: float) -> tuple[Pt, ...]:
    """Carrier `thermal`: a centred grid inside **every** primitive of a land, one rank at a time.

    A module's exposed pad is not one rectangle. Measured: KiCad writes the ESP32-C3-MINI's as **nine**
    1.45 x 1.45 mm blocks all numbered 49, all carrying `pad_prop_heatsink`, on c3_usb and on node
    alike — so a generator that took the owner's bounding box would offer sites in the 0.525 mm gaps
    between blocks, where there is no copper to be in a pad of. `pads.PadGeom.copper` is a tuple per
    primitive for exactly this reason and `route_scene.pad_items` keeps it one `Item` per primitive;
    this walks them.

    Per primitive the count is `1 + floor((extent - dia) / pitch)` per axis, centred, which is the
    largest grid whose **rings** fit inside the copper rather than whose centres do. Measured on a
    1.45 mm block: `1 + floor(0.95 / 0.8001)` = 2 on c3_usb and `1 + floor(1.100 / 0.8501)` = 2 on
    node, so **2 x 2 x 9 = 36 sites** on both boards, which is the number `docs/stitch-plan.md` S6
    predicted for c3_usb and reproduces for node.

    **The order is rank-major and block-minor, and that is the whole difference between an array and a
    clump.** Block-major would put the first four vias in the first block, the next four in the
    second, and c3_usb's nine would land in two and a quarter of its nine blocks. Taking each block's
    best site first, then each block's second-best, gives **one via per block** for the first nine —
    which is what a hand layout of a nine-block QFN land does, and the arithmetic and the practice
    agreeing is the best evidence the model is not nonsense. Within a block the order is distance from
    that block's centre, then `(y, x)`: on a 2 x 2 all four are equidistant, so the tie-break decides
    and it decides the same way every time.
    """
    step = _nm(pitch)
    ranks: list[list[Pt]] = []
    for box in boxes:
        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        nx = 1 + int(math.floor(max(0.0, (box[2] - box[0]) - dia) / pitch + 1e-9))
        ny = 1 + int(math.floor(max(0.0, (box[3] - box[1]) - dia) / pitch + 1e-9))
        # **Integer nanometres, and the reason is `next_nm`'s reason one step further on.** Written as
        # `q(cx + (i - (nx-1)/2) * pitch)` the two sites of a 2-wide row are `q(cx - pitch/2)` and
        # `q(cx + pitch/2)`, and a half-nanometre offset rounds **both inward** to the same even
        # nanometre: measured 2026-09-21 on node's placed board, a 0.700101 mm pitch came out 0.7001
        # mm apart on the file and `verify_copper` refused all six pairs for `hole_to_hole` — the
        # nanometre `next_nm` had just added was quantised away again. Anchoring the first site and
        # stepping by an exact integer makes every neighbour's separation the pitch to the nanometre;
        # the grid's centre may sit half a nanometre off the primitive's, which nothing can measure.
        x0 = _nm(cx) - ((nx - 1) * step) // 2
        y0 = _nm(cy) - ((ny - 1) * step) // 2
        pts: list[Pt] = []
        for j in range(ny):
            for i in range(nx):
                pts.append((_mm(x0 + i * step), _mm(y0 + j * step)))
        pts.sort(key=lambda pt: (round(math.hypot(pt[0] - cx, pt[1] - cy), 6), pt[1], pt[0]))
        ranks.append(pts)
    out: list[Pt] = []
    for k in range(max((len(r) for r in ranks), default=0)):
        for r in ranks:
            if k < len(r):
                out.append(r[k])
    return tuple(out)


# --- carrier `plane`: the pitch, the cavity, and the lattice over two facing pours ------------------


PLANE_REASON = "plane"
"""The reason a plane-stitch barrel carries, and it is a **fourth** entry beside `REASON`,
`THERMAL_REASON` and `GUARD_REASON` for the reason the first three split.

`route_verify.parallel_joined` walks every `reason == "stitch"` via and asks what it is a twin of; a
plane barrel has no anchor and no link, so under the parallel reason `build._barrel_gate`'s one fatal
check would fire on copper it was never about — which is measurably what happened to the thermal
array before S6 split it and to the guard before S7 did. The same three arguments hold a fourth time:
`VIAS_PATTERN` reads as a board (`{"tap": 62, "plane": 48}`) rather than as an accident,
`route_verify.BRANCH_REASONS` gains it so a barrel with no anchor is judged as the fab's standard via
instead of against whichever via the router left within a millimetre, and `copper_bar.REDUNDANT`
names it so a reader of `DETOURS` can see the whole of what was excluded.

It is **not** in `ampacity.NOT_A_RAIL`, and that is the same distinction S7 drew there: a guard is
copper beside a net whose current is not in question, while a plane barrel is a second path between
two pours of the net it is on — conduction, exactly like a rung and a thermal barrel."""


KNEE_OF_RISE = 0.5
"""`f_knee = KNEE_OF_RISE / t_rise` — the knee frequency of an edge, from its rise time.

Above the knee a digital edge's spectrum falls off fast enough that the interconnect stops caring;
below it, it does not. The 0.5 is Johnson & Graham's rule (*High-Speed Digital Design*, section 1.3)
and it is a **named engineering rule with a citation**, which is a different kind of number from the
one `docs/stitch-plan.md` section 8 item 3 refuses: that refusal is of *invented per-kind rise-time
defaults* — "pcbc default" guesses for `clock`, `spi`, `i2c` and `switch_node` that a pitch would
then scale linearly with. The rise time here is never guessed; it is `NetReq(rise_ps=)`, a board
fact, and a board that does not state one gets a refusal naming the missing keyword rather than a
number this module made up.

It is a module constant rather than a literal for `RING_SAMPLES`' reason: a reader who wants to argue
with the ratio should be able to find it, change it, and see exactly which pitch moved."""

PITCH_FRACTION = 20.0
"""How many stitch pitches go into a wavelength at the knee: `pitch = lambda_knee / PITCH_FRACTION`.

**This is the number R-E1 wrote down as "5 mm" and the panel flagged as unjustified**, and the whole
of `docs/stitch-plan.md` section 8 item 1's objection to the technique was that the pitch had no
basis. Lambda/20 is the common rule and lambda/10 the loose one; 20 is taken here because it is the
conservative of the two and because the cost of the tighter number is measurable (`route_verify
.plane_area`) while the cost of the looser one is not.

What it is **not** is a resonance calculation. A stitched cell of side `p` has its first
parallel-plate mode at `v / (2p)`, which at `p = lambda_knee / 20` sits ten times above the knee —
so lambda/20 is a margin, not a boundary, and saying so is the difference between deriving a pitch
and dressing one up. What the derivation buys over "5 mm" is that every other term in it is the
board's: the rise time is the author's declaration, the velocity is `Stackup.dielectric_between`'s
series Dk, and the answer moves when the board moves. Measured 2026-09-21 on node's
In1.Cu/In2.Cu core (Dk 4.6) at a 500 ps edge: **6.9892 mm**, which R-E1's folklore 5 mm approximates
as lambda/28 for no stated reason."""


def cavity_pair(stack: Stackup, layers: Sequence[str]) -> tuple[str, str]:
    """Which two of a net's poured layers set the stitch pitch: the pair with the **highest series
    Dk**, and on a tie the pair that is furthest apart, and on a tie the first in layer order.

    Every via pcbc writes is a through via, so one lattice ties every pour of one net at once and
    there is only ever one pitch to choose. The choice is not arbitrary: a parallel-plate cavity's
    mode frequency is `v / 2L` with `v = c / sqrt(Dk)` and **does not depend on the plate separation
    at all**, so the only term a pair contributes is its Dk — and the highest Dk is the slowest wave,
    the shortest wavelength and therefore the tightest pitch. Picking the worst pair is the same
    direction every other choice in this module takes when two numbers disagree.

    Measured 2026-09-21 on `jlcpcb_4l_1oz`, a net poured on In1.Cu and In2.Cu: the only pair, Dk
    **4.6** across 1.065 mm of core. On `jlcpcb_2l_1oz` with a front and a back pour: F.Cu/B.Cu, Dk
    **4.6** across 1.53 mm. The two stackups agree to four figures, which is why the pitch this
    derivation produces is a property of the **edge rate** and barely one of the board.
    """
    ordered = [lay for lay in stack.copper_layers() if lay in set(layers)]
    best: tuple[float, float, str, str] | None = None
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            h, dk = stack.dielectric_between(ordered[i], ordered[j])
            key = (dk, h, ordered[i], ordered[j])
            if best is None or (key[0], key[1]) > (best[0], best[1]):
                best = key
    if best is None:
        raise ValueError(f"cavity_pair needs two poured layers, got {tuple(layers)!r}")
    return (best[2], best[3])


def cavity_velocity(stack: Stackup, pair: tuple[str, str]) -> float:
    """Signal velocity in the cavity between two poured layers, mm/ns: `c / sqrt(series Dk)`.

    `Stackup.dielectric_between` is the only input and it is measured rather than named — the series
    Dk of everything the span crosses, `h_total / sum(h_i / dk_i)`, which on a 4-layer core is 4.6 and
    on a 2-layer board is 4.6. `stackup.C_MM_PER_NS` is the same constant the length and impedance
    arithmetic already uses, so a wavelength here and a delay there cannot disagree."""
    _h, dk = stack.dielectric_between(*pair)
    return C_MM_PER_NS / math.sqrt(dk)


def knee_ghz(rise_ps: float) -> float:
    """The knee frequency of a `rise_ps` edge, GHz. `KNEE_OF_RISE / t_rise`, with `t_rise` in ns."""
    return KNEE_OF_RISE / (rise_ps / 1000.0)


def stitch_pitch(scene: Scene, cs: ConstraintSet, pair: tuple[str, str], net: str, via: tuple[float, float]) -> tuple[float, str] | str:
    """Carrier `plane`: the centre-to-centre a lattice of `net` vias ties two facing pours on — or,
    when the board has not said how fast it is, **the sentence naming what is missing**.

    R-E1 says *"plane edges stitched every 5 mm when two ground pours face each other across
    layers"* and `docs/stitch-plan.md` section 8 item 1 refused the whole technique partly because
    that 5 mm has no basis. This is the arithmetic that replaces it, and every term in it is somebody
    else's number:

        f_knee  = KNEE_OF_RISE / t_rise            Johnson & Graham; t_rise is NetReq(rise_ps=)
        v       = C_MM_PER_NS / sqrt(Dk)           Stackup.dielectric_between, series Dk
        lambda  = v / f_knee
        pitch   = lambda / PITCH_FRACTION          the lambda/20 rule, stated and citable
        floor   = thermal_pitch                    hole_to_hole, the class clearance, the antipads

    **What the board does not supply, this refuses to invent.** Measured 2026-09-21, not one of the
    five boards declares `rise_ps` on any net, `PRESETS` sets none, and there is no default anywhere
    in the compiler — so `ConstraintSet.fastest_edge` returns `None` on all five and this returns a
    sentence instead of a number. That is `docs/stitch-plan.md` section 8 item 3 honoured rather than
    worked around: four per-kind "pcbc default" rise times would be four invented numbers that this
    pitch scales linearly with, and a pitch derived from a guess is folklore with extra steps.

    **The floor is the other half and it is the one that can refuse a declared edge.** A lattice at a
    pitch below `thermal_pitch` is two barrels whose antipads have merged, which deletes the very
    plane the stitch exists to tie (section 2(h)'s measured -0.050 mm). So an edge fast enough to ask
    for less than the process allows gets the process's number and a sentence saying the stitch is
    coarser than the edge wanted — never a lattice that eats the pour. Measured 2026-09-21, both
    floors are far below anything these boards could carry:

        tests/fixtures/planes/planes.py  2L, GND on F.Cu + B.Cu   floor 0.800101  binds below  57.2 ps
        node with GND on In1.Cu + In2.Cu 4L, no foreign pour      floor 0.700101  binds below  50.1 ps

    (node as it ships pours `3V3` on In2.Cu, which would put an antipad term in the floor and take it
    to 0.8501 — but that configuration is two pours of two *different* nets and gets no lattice at
    all, which is `docs/stitch-plan.md` section 2(l)'s whole point.)

    `next_nm` on the way out is not the clearance nudge it is elsewhere — a pitch this far above the
    floor has no boundary to lose on — it is the **quantisation**: `sites_grid` and `sites_along` step
    in whole nanometres, so the pitch has to be one, and rounding *up* means the lattice never steps
    shorter than the derivation asked for.

    Returns `(pitch, why)` or a one-sentence reason string.
    """
    fast = cs.fastest_edge()
    if fast is None:
        return (
            "no net declares NetReq(rise_ps=), and a stitch pitch is a fraction of the wavelength at the "
            "board's fastest edge; pcbc will not invent an edge rate"
        )
    who, rise_ps = fast
    v = cavity_velocity(scene.stack, pair)
    f = knee_ghz(rise_ps)
    lam = v / f
    want = lam / PITCH_FRACTION
    floor, floor_why = thermal_pitch(scene, net, via)
    why = (
        f"lambda/{PITCH_FRACTION:g} at the {rise_ps:g} ps edge {who} declares: knee {f:g} GHz "
        f"({KNEE_OF_RISE:g}/t_rise), {v:.4f} mm/ns in the Dk {scene.stack.dielectric_between(*pair)[1]:g} "
        f"between {pair[0]} and {pair[1]}, lambda {lam:.4f} mm"
    )
    if want < floor:
        return (floor, f"{floor_why} — the process floor, coarser than the {want:.4f} mm {why} asks for")
    return (next_nm(want), why)


# --- carrier `guard`: the shield, its offset, and the stretches it survives on ----------------------


@dataclass(frozen=True)
class GuardPath:
    """One run of a guarded net, and the two lines a shield would run on beside it.

    `docs/r2-design.md` B.6: *two ground tracks parallel to the guarded path, mitred at the corners,
    at offset d, at `stack.track_min`, plus stitch vias every `stitch_mm` along each side.* Everything
    in that sentence that can be decided before the board is looked at is decided here; which parts of
    those two lines are actually clear is `_run_guard`'s question and `route_scene.clear_runs`'s
    answer.

    `pts` is the **guarded** path and `sides` are the two offsets of it, in `_SIDES`' order. The
    guarded path is carried as well as the offsets because the report measures against it: a coverage
    of "18.2 of 24.0 mm" is 18.2 mm of shield against 24.0 mm of the thing being shielded, and that
    second number is not a property of either offset line (they are longer on an outside corner and
    shorter on an inside one).
    """

    net: str  # the net being shielded
    layer: str
    pts: tuple[Pt, ...]  # the guarded run, in order
    width: float  # the guarded net's class width — the w of the offset arithmetic
    offset: float  # d
    offset_why: str
    sides: tuple[tuple[str, tuple[tuple[Pt, ...], ...]], ...]  # ("left", flank pieces), ("right", ...)
    stitch_mm: float
    ids: tuple[int, ...] = ()
    """The scene items this run is made of, for `clear_runs`' `ignore`.

    The offset was computed from this copper's own class width and the compiled clearance, to the
    nanometre. Asking `clear_runs` about it a second time asks a *bounding box* question about a
    number that is already solved exactly, and on a diagonal leg that answer is badly wrong in the
    pessimistic direction — measured, a 45 degree track's square bounding box reports it as blocking
    the whole of its own flank. `blocked` still judges every finished stretch against this copper, so
    what is dropped here is a bad proposal and never a decision (`route_scene.clear_runs`)."""

    @property
    def mm(self) -> float:
        return sum(math.dist(self.pts[i], self.pts[i + 1]) for i in range(len(self.pts) - 1))


_SIDES = (("left", -1), ("right", 1))
"""The two flanks, in the order they are written, and they are named by the **turn** rather than by
the compass because a path turns and the compass does not: `left` is the side a walker of the path
from `pts[0]` to `pts[-1]` has on their left, which is `(-uy, ux)` negated in KiCad's y-down frame.
A name like "north" is only true of a horizontal run, and B.6's own worked example ("C4 blocks 5.8 mm
on the north side") is a sentence about one particular board rather than about a guard."""


def guard_offset(scene: Scene, cs: ConstraintSet, net: str, ground: str) -> tuple[float, str]:
    """How far from the guarded centreline the shield sits, and where every term came from.

    **This is not B.6's formula, and the correction is measured** (`docs/stitch-plan.md` section 2(i)).
    B.6 writes `d = w/2 + max(between(guard_net, net), spacing_w * w)`, and that formula is wrong in
    three ways, two of them measurable on a board in this repo:

    - it omits the **guard track's own half-width**, so the copper it places is `track_min/2` closer
      to the guarded net than the clearance it just computed;
    - it omits the **stitch via's ring**, which is the widest thing the guard puts at that offset —
      0.5 mm against a 0.127 mm track on the DS2 Addon's stackup, so B.6's via cannot sit on B.6's
      own track;
    - `spacing_w` is a crosstalk rule between two *signal* nets and is measured not even monotone
      (ds2's `AIN3` goes 59.9 % clear to 28.2 % when the rule is widened to 3W), so `max()`-ing it in
      makes the guard further away the more the author asks for.

    So the terms are `w/2` (the guarded net's class width, from the compiler), `between(ground, net)`
    (the clearance table, which composes the class numbers with any voltage row), and
    `max(track_min, via_diameter)/2` (the stackup: the half-width of the widest thing the guard puts
    at this offset). `next_nm` on top, for its own reason — at exactly `need + EPS_MM` `clears` loses
    on the float boundary, and a guard is the first pattern whose whole geometry sits *exactly* on the
    clearance on every leg, which is why that design met the problem first.

    Measured 2026-09-21 on the five boards and the fixture: the DS2 Addon's analog class (0.2 mm wide)
    gives **0.550101**, node's gives **0.475101**, and the fixture's `SIG` (0.16 mm, the class floor)
    gives **0.530101** — of which 0.25 mm is the via ring alone, i.e. very nearly half the offset is
    the term B.6 leaves out.
    """
    c = cs.by_net(net)
    w = round(float(c.width_mm.value), 4) if c is not None else scene.stack.track_min
    bet, why = scene.table.between(ground, net)
    fat = max(scene.stack.track_min, scene.stack.via_diameter)
    d = next_nm(w / 2.0 + bet + fat / 2.0 + EPS_MM)
    return (
        d,
        f"{w:g} mm of {net} halved, {bet:g} mm between {ground} and {net} ({why}), and half the "
        f"{fat:g} mm ring a stitch via puts on the guard",
    )


def guard_min_mm(scene: Scene) -> float:
    """The shortest stretch of guard worth writing, and it is a **derived** number rather than a
    constant (`docs/stitch-plan.md` S7 names a `GUARD_MIN_MM`; the house rule that numbers come from
    the stackup is what makes it a function instead).

    A guard track is only ground copper because a stitch via welds it to the ground pour. A stretch
    too short to hold one via therefore holds nothing that makes it ground: it is a floating island on
    a named net, which passes KiCad's unconnected-items check (pad to pad), passes
    `netcheck.check_copper` (pad bindings inside footprint blocks, never a segment) and passes
    `verify_copper` (clearance, angle and size) — the exact silent failure `route_verify.guard_cover`
    exists for. So the floor is one via's diameter: 0.5 mm on the two-layer stackups here, 0.35 mm on
    node's.

    A stretch that is long enough and still gets no via is dropped anyway (`_run_guard`); this is the
    cheap half of the same rule, applied before a candidate is built rather than after.
    """
    return scene.stack.via_diameter


def _sgn(v: int) -> int:
    return (v > 0) - (v < 0)


_AXIS_OF = {(1, 0): "x", (-1, 0): "x", (0, 1): "y", (0, -1): "y", (1, 1): "u", (-1, -1): "u", (1, -1): "v", (-1, 1): "v"}
"""Which of `free_intervals`' four axes an octilinear leg runs along. A path pcbc wrote is 0/45/90 by
construction (A.5), so this is total over the directions that can occur and a `KeyError` here would
mean the geometry core had let a tilted leg through."""


def _along(axis: str, at: tuple[int, int]) -> int:
    """The along-coordinate of a point, in nanometres of that axis' own units."""
    x, y = at
    return {"x": x, "y": y, "u": x + y, "v": x - y}[axis]


def _across(axis: str, at: tuple[int, int]) -> int:
    """The across-coordinate: `free_intervals`' perpendicular, in the same units."""
    x, y = at
    return {"x": y, "y": x, "u": x - y, "v": x + y}[axis]


def offset_path(pts: Sequence[Pt], d: float, side: int) -> tuple[tuple[Pt, ...], ...]:
    """One flank of B.6's pair: the path moved `d` sideways, in **integer nanometres** throughout, cut
    wherever the offset cannot turn the corner.

    Every vertex is exact, and getting that right is the difference between a guard and a tilted
    approximation of one. Three constructions, and only the first is obvious:

    - **A leg's own offset** is `d` along the perpendicular. For an axis leg that is `d` in one
      coordinate. For a **45 degree** leg it is `d / sqrt(2)` in each, which is not a whole number of
      nanometres, so it is `ceil(nm(d) * sqrt(0.5))` — ceiling, so the guard is never *closer* than the
      clearance the offset was computed to hold. Rounding it the other way puts copper inside a
      clearance the compiler said was needed, which is the one direction this may never fail in.
    - **A corner's offset vertex is the intersection of the two offset lines**, never the translation
      of the original vertex. Translating gives two different points at one corner and the chord
      between them is not octilinear — measured on the fixture, an axis leg meeting a 45 at
      `d = 0.530101` leaves a chord of (375038, 155063) nm, which `is_octilinear` refuses and
      `verify_copper` would have raised on. The intersection is exact because `turn_ok` has already
      forbidden anything sharper than 135 degrees, so two consecutive legs are always one axis and one
      diagonal and the determinant of that pair is +-1.
    - **A corner the offset cannot turn cuts the flank in two**, and this is not an edge case — it is
      what happens at *every* corner of *every* path pcbc writes. `patterns.MITRE_MM` is 0.2, so a
      quarter turn is written as two legs joined by a 45 degree chord of exactly `0.2 * sqrt(2)` =
      **0.2828 mm**, and offsetting the inside of a 135 degree turn by `d` shortens the leg between it
      and its neighbour by `2 * d * tan(22.5 deg)` = **0.828 d**. So the chord survives only while
      `d < 0.342 mm`, and the smallest offset any board here computes is node's **0.4196** — measured
      2026-09-21, the fixture's `SIG2` and every other mitred pcbc path folds its inside flank at the
      first corner. The honest answer is two stretches, each ending at the corner's own perpendicular
      foot (`corner + perp * d`, exact and not overlapping), and the partial rule then reports it. A
      self-intersecting flank, or a guard refused outright for having turned a corner, would both be
      worse and neither would be true.
    """
    ip = [(_nm(x), _nm(y)) for x, y in pts]
    if len(ip) < 2:
        return ()
    dn = _nm(d)
    diag = math.ceil(dn * math.sqrt(0.5))
    lines: list[tuple[tuple[int, int], tuple[int, int], tuple[int, int]]] = []
    for i in range(len(ip) - 1):
        (ax, ay), (bx, by) = ip[i], ip[i + 1]
        ux, uy = _sgn(bx - ax), _sgn(by - ay)
        px, py = -uy, ux  # the left normal in KiCad's y-down frame
        step = diag if (px and py) else dn
        off = (side * px * step, side * py * step)
        lines.append(((ax + off[0], ay + off[1]), (bx + off[0], by + off[1]), (ux, uy)))
    out: list[tuple[Pt, ...]] = []
    chain: list[tuple[int, int]] = [lines[0][0]]
    for k in range(1, len(lines)):
        a0, b0, u0 = lines[k - 1]
        a1, b1, u1 = lines[k]
        det = u0[0] * u1[1] - u0[1] * u1[0]
        num = (a1[0] - a0[0]) * u1[1] - (a1[1] - a0[1]) * u1[0]
        hit = None if (det == 0 or num % det) else (a0[0] + (num // det) * u0[0], a0[1] + (num // det) * u0[1])
        if hit is not None and _forward(chain[-1], hit, u0) and _forward(hit, b1, u1):
            chain.append(hit)
            continue
        chain.append(b0)  # the corner's own perpendicular foot on this leg
        out.append(tuple((_mm(x), _mm(y)) for x, y in chain))
        chain = [a1]
    chain.append(lines[-1][1])
    out.append(tuple((_mm(x), _mm(y)) for x, y in chain))
    return tuple(p for p in out if len(p) >= 2 and len(set(p)) == len(p))


def _forward(a: tuple[int, int], b: tuple[int, int], u: tuple[int, int]) -> bool:
    """Does `a -> b` still run the way its leg does, and is it a leg at all? The one test that
    separates a corner the offset turns from one it folds through."""
    return a != b and (_sgn(b[0] - a[0]), _sgn(b[1] - a[1])) == u


def sites_along(ctx: PatternCtx, path: Sequence[Pt], pitch: float) -> tuple[Pt, ...]:
    """Carrier `guard`: B.6's stitch sites — every `pitch` along one flank, and one at each end.

    Read literally off B.6, and the literal reading is also the right one: *"stitch vias every
    `stitch_mm` along each side starting from the guarded path's first vertex and one via at each
    end"*. Arc length 0 is the first end, so the rule is arc lengths `0, pitch, 2*pitch, ...` plus the
    far end, and the ends come for free at both ends of the list rather than being bolted on.

    **Anchored at the first vertex and stepped in integer nanometres**, which is S6's finding applied
    one carrier on: arc length along a 45 degree leg is irrational, so a site is placed by flooring
    the target into a whole number of the leg's own steps — each step being 1 nm on an axis and
    `sqrt(2)` nm on a diagonal — and the last site is the leg's own end vertex exactly. Flooring keeps
    every site inside the leg it belongs to; anchoring at the start keeps the answer a function of the
    path rather than of where the walk happened to accumulate its rounding.

    The sites are on the **flank**, not on the guarded path: they are where copper goes. B.6's
    "starting from the guarded path's first vertex" is what fixes the phase, and the flank's own first
    vertex is that vertex offset, so the two readings place the same vias.
    """
    ip = [(_nm(x), _nm(y)) for x, y in path]
    legs: list[tuple[tuple[int, int], tuple[int, int], int, float]] = []
    arc = [0.0]
    for i in range(len(ip) - 1):
        a, b = ip[i], ip[i + 1]
        ux, uy = _sgn(b[0] - a[0]), _sgn(b[1] - a[1])
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        per = math.sqrt(2.0) if (ux and uy) else 1.0
        legs.append((a, (ux, uy), n, per))
        arc.append(arc[-1] + n * per / 1e6)
    total = arc[-1]
    if total <= 0.0 or pitch <= 0.0:
        return ()
    targets = [k * pitch for k in range(int(math.floor(total / pitch + 1e-9)) + 1)]
    if total - targets[-1] > 1e-9:
        targets.append(total)
    out: list[Pt] = []
    seen: set[Pt] = set()
    for t in targets:
        i = 0
        while i < len(legs) - 1 and t > arc[i + 1] + 1e-9:
            i += 1
        a, u, n, per = legs[i]
        k = min(n, max(0, int(math.floor((t - arc[i]) * 1e6 / per + 1e-9))))
        pt = (_mm(a[0] + k * u[0]), _mm(a[1] + k * u[1]))
        if pt not in seen:
            seen.add(pt)
            out.append(pt)
    return tuple(out)



def sites_grid(rect: tuple[float, float, float, float], pitch: float, dia: float) -> tuple[Pt, ...]:
    """Carrier `plane`: the interior lattice, centred in `rect`, row-major, in integer nanometres.

    `sites_lattice`'s arithmetic asked of a board instead of of a pad land, and the integer
    nanometres are S6's finding verbatim: written as `q(cx + (i - (nx-1)/2) * pitch)` the two sites
    of a 2-wide row are `q(cx - pitch/2)` and `q(cx + pitch/2)`, and a half-nanometre offset rounds
    **both inward** to the same even nanometre — measured on node's placed board, a 0.700101 mm pitch
    came out 0.7001 mm apart on the file and `verify_copper` refused all six pairs for `hole_to_hole`.
    Anchoring the first site and stepping by an exact integer makes every neighbour's separation the
    pitch to the nanometre. A lattice over a 60 x 45 mm board has far more pairs than a lattice over a
    1.45 mm pad, so the finding matters more here, not less.

    The count per axis is `1 + floor((extent - dia) / pitch)` — the largest grid whose **rings** fit
    inside the rectangle rather than whose centres do — and the grid is centred, so a board grows its
    lattice symmetrically rather than from one corner.

    Row-major, y then x, and the order is only ever a tie-break: a plane spec's `need` is its whole
    candidate list (`_plane_specs`), so every site is tried and the order decides only which of two
    sites too close to each other survives `_crowds`. Row-major is the order a reader can predict
    from the numbers, which is what a fixed candidate list is for.
    """
    step = _nm(pitch)
    if step <= 0:
        return ()
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    nx = 1 + int(math.floor(max(0.0, (rect[2] - rect[0]) - dia) / pitch + 1e-9))
    ny = 1 + int(math.floor(max(0.0, (rect[3] - rect[1]) - dia) / pitch + 1e-9))
    x0 = _nm(cx) - ((nx - 1) * step) // 2
    y0 = _nm(cy) - ((ny - 1) * step) // 2
    return tuple((_mm(x0 + i * step), _mm(y0 + j * step)) for j in range(ny) for i in range(nx))


def plane_rect(scene: Scene, net: str, dia: float) -> tuple[float, float, float, float]:
    """The rectangle a plane stitch may put a via **centre** in: `Scene.outline`, inset by the ring
    and by the pour's own `min_thickness`.

    `Scene.outline` is "the content rect inset by `stack.edge_clearance` exactly" (A.4 rule 5), which
    is where a via's *copper* may reach, so a via's centre has to stand its own radius further in.
    That much is the edge rule. The second term is **measured, and the measurement is the one thing in
    this slice that only the gate could have found**:

        built with `outline + dia/2` and routed end to end, 2026-09-21, on
        tests/fixtures/planes/planes.py
          the fill came back at 0.3005 mm from the board edge, not 0.3000
          every one of the 20 **ring** barrels had its outer ring sample at exactly 0.3000
          `route_verify.plane_stitch` -> "20 of 41 lattice via(s) do not land in every pour they tie"
          the build stopped, and `pour_raster` had said all 20 were reachable

    `pour_raster` does not model the board edge at all — it skips `kind in ("lane", "edge")` and
    rasters `scene.outline` itself — so the prediction is optimistic by exactly the width of the
    pour's own retreat, and a candidate on the boundary is a candidate the refill puts half a micron
    outside its plane. The honest inset is the pour's **`min_thickness`**, read off the board's own
    `Scene.zone_rules`: copper thinner than that is what KiCad is entitled to remove from a fill, so a
    ring with less than `min_thickness` of pour around it is standing where the fill need not be.
    Measured on the fixture, both pours carry `min_thickness` 0.1, which puts the ring at 0.65 mm and
    0.0995 mm inside the fill — and all 41 barrels then land in both pours.

    It is a derived number and not a margin somebody chose: no zone, no term.
    """
    x0, y0, x1, y1 = scene.outline
    thick = max([r.min_thickness for r in scene.zone_rules if r.net == net], default=0.0)
    # In integer nanometres, for `sites_grid`'s reason one step earlier: the rectangle is what both
    # generators anchor on, so a corner half a nanometre off a whole number would put the ring and the
    # lattice on two different grids and `_crowds` would then be judging a rounding error.
    r = _nm(dia / 2.0 + thick)
    return (_mm(_nm(x0) + r), _mm(_nm(y0) + r), _mm(_nm(x1) - r), _mm(_nm(y1) - r))


def sites_plane(ctx: PatternCtx, net: str, pitch: float, dia: float) -> tuple[Pt, ...]:
    """Carrier `plane`: **the edge ring first, then the interior lattice** — R-E1's own two halves in
    R-E1's own order.

    R-E1 is one sentence — *"plane edges stitched every 5 mm when two ground pours face each other
    across layers"* — and it names the **edge**. The edge is not decoration on a lattice: an unstitched
    plane pair is a parallel-plate cavity whose open perimeter is the aperture it radiates from, and a
    ring is what closes it. So the ring is proposed first, and C.1's least-free-first rule agrees for
    once with the letter of the requirement: a ring site is on one line with one degree of freedom
    along it, an interior site is one of a grid of alternatives.

    Both halves are on the same pitch, which is the whole point of deriving it — two pitches would be
    two numbers where the arithmetic supplies one.

    Deduplicated in order: on a board whose side is close to a whole number of pitches the corner
    site and the first lattice site can coincide, and a repeated candidate would be judged twice and
    counted twice in `bound`.
    """
    rect = plane_rect(ctx.scene, net, dia)
    ring_path = ((rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3]), (rect[0], rect[1]))
    ring = sites_along(ctx, ring_path, pitch)
    out: list[Pt] = []
    seen: set[Pt] = set()
    for pt in ring + sites_grid(rect, pitch, dia):
        if pt in seen:
            continue
        seen.add(pt)
        out.append(pt)
    return tuple(out)


def _land(scene: Scene, ids: tuple[str, ...]) -> tuple[Item, ...]:
    """Every pad primitive of a land, in scene order — `Scene.items` is sorted and the ids are
    assigned after the sort, so this is the same tuple on every run of every machine."""
    return tuple(it for it in scene.items if it.kind == "pad" and it.owner in ids and it.copper is not None)


def _site_name(spec: StitchSpec, at: Pt) -> str:
    """Which candidate this is, for the report: `"right 0.9"`. Derived from the two points rather
    than carried alongside them, so the candidate list stays a plain tuple of `Pt`."""
    if spec.carrier in ("thermal", "plane"):
        return f"{at[0]:g},{at[1]:g}"
    dx, dy = at[0] - spec.at[0], at[1] - spec.at[1]
    for name, s in _RING:
        if (dx > 0) == (s[0] > 0) and (dx < 0) == (s[0] < 0) and (dy > 0) == (s[1] > 0) and (dy < 0) == (s[1] < 0):
            return f"{name} {math.hypot(dx, dy):.4g}"
    return f"{math.hypot(dx, dy):.4g}"


# --- which groups this pattern has something to say about -------------------------------------------


def _vias_of(scene: Scene, net: str) -> list[dict]:
    """Every via of one net already on the board, as `ampacity`'s own via row, sorted by position.

    Read off the **scene** rather than off the board text, because that is what a pattern is handed
    and because the scene's vias are the ones `blocked` will judge the candidate against. The keys
    are `ampacity._VIA`'s so `route_verify._clusters` can be asked the same question about them with
    no second reading of what a via is.
    """
    out = []
    for it in scene.items:
        if it.kind != "via" or it.net != net or it.copper is None or it.hole is None:
            continue
        out.append({"at": it.at(), "size": round(it.copper.r * 2.0, 4), "drill": round(it.hole.r * 2.0, 4)})
    return sorted(out, key=lambda v: v["at"])


def _vias_in_land(scene: Scene, net: str, boxes: tuple, dia: float) -> tuple[Pt, ...]:
    """The centres of every via of `net` already on the board whose ring (`dia`) lies inside one
    primitive of the land (`boxes`, the primitives' boxes): the barrels a locked core array already
    put there, which `_thermal_specs` counts toward the array instead of placing again."""
    r = dia / 2.0
    out = []
    for v in _vias_of(scene, net):
        x, y = v["at"]
        if any(b[0] + r <= x <= b[2] - r and b[1] + r <= y <= b[3] - r for b in boxes):
            out.append((x, y))
    return tuple(out)


def specs(ctx: PatternCtx) -> tuple[StitchSpec, ...]:
    """Every spec this module has, in `CARRIERS`' order: the parallel groups, the thermal lands, the
    guarded runs.

    The order **is** the stage's order. `pattern_copper` adds each spec's copper to the scene before
    the next spec runs, so "parallel then thermal" and "thermal then parallel" are different boards;
    C.1's rule is least free first, and a ring of eight sites within 1 mm of one fixed point has less
    freedom than a lattice that owns a region nothing else wants, which in turn has less than a
    corridor two tracks long. The first two populations do not overlap on any board here — a parallel
    spec's net is by definition **unpoured** and a thermal spec's net must be poured — so that half of
    the order is arithmetic rather than a measurement. `guard` comes after them because it is the only carrier
    that writes tracks and the only one that reads another pattern's copper as input geometry, so it
    is the one whose footprint the other two would have to step around. `plane` is last because it
    owns the whole board: its lattice is the only candidate list that overlaps every other carrier's
    region, so it is the one that must step around them rather than the other way about, and a plane
    spec's net is by definition **poured** — which is exactly the set a parallel spec's net is not.
    """
    return _parallel_specs(ctx) + _thermal_specs(ctx) + _guard_specs(ctx) + _plane_specs(ctx)


def _thermal_specs(ctx: PatternCtx) -> tuple[StitchSpec, ...]:
    """One spec per compiled `Thermal()`, in `board.py` order.

    Everything that could be decided without the board was decided by `constraints.ThermalSpec` —
    the net, the barrel's K/W, the count, the plane, the four refusals — and everything here is a
    question about geometry: which primitives the land draws, what pitch their antipads allow, and
    where inside them a via may sit. That split is why `pcbc check` can print the budget on a board
    that has never been placed.

    Measured 2026-09-21 on the five boards: the population is **whatever declares one**, and what can
    declare one is narrow. c3_usb and node each carry an ESP32-C3-MINI whose exposed pad is nine
    `pad_prop_heatsink` blocks on GND; blinky, buck and the DS2 Addon carry **no pad with that
    property at all** (buck's largest SMD land is `L1.1`, an inductor terminal — a passive, and the
    case the compile refusal exists for). So this writes nothing on three of the five for want of a
    land, not for want of a tuned bound.
    """
    cs = ctx.cs
    out: list[StitchSpec] = []
    for budget in cs.thermals:
        prims = _land(ctx.scene, budget.ids)
        pitch, why = thermal_pitch(ctx.scene, budget.net, budget.via)
        boxes = tuple(it.box() for it in prims)
        sites = sites_lattice(ctx, boxes, pitch, budget.via[0]) if boxes else ()
        # Barrels of this net already inside the land — a `layout.core.py` array locked verbatim from
        # a previous build's `layout.gen.py` — are the array, or the start of it: they count toward
        # `need` and their sites are spent. Measured before this (third review D2): every copper line
        # of c3_usb locked verbatim gave a second nine-via array on top of the first, 68 drills for 59.
        have = _vias_in_land(ctx.scene, budget.net, boxes, budget.via[0])
        sites = tuple(at for at in sites if all(math.hypot(at[0] - v[0], at[1] - v[1]) >= budget.via[0] for v in have))
        blocks = _land_blocks(prims)
        # One barrel in every pad of the land is the floor, whatever the budget asks: a pad with none
        # moves no heat through the board (the third refutation round: at 0.35 W the nine-block land
        # asked for 9 and got 9 in 6 blocks, three bare).
        bare = sum(1 for blk in blocks if not any(_in_boxes(v, blk, budget.via[0]) for v in have))
        need = len(sites) if budget.fill else max(0, budget.need - len(have), bare)
        layer = _land_layer(ctx.scene, prims)
        out.append(
            StitchSpec(
                carrier="thermal",
                net=budget.net,
                sites=sites,
                need=need,
                required=False,
                via=budget.via,
                joins=(layer, budget.plane),
                owner=budget.pad,
                why=(
                    f"{budget.need} x {budget.via[1]:g} mm barrel at {budget.theta_via.value:g} K/W each carries "
                    f"{budget.watts:g} W within {budget.rise_c:g} C ({budget.theta_array():g} K/W, "
                    f"{budget.rise_of(budget.need):g} C), on a {pitch:g} mm lattice ({why})"
                ),
                bound=len(sites),
                at=prims[0].at() if prims else (0.0, 0.0),
                width=0.0,  # an array via carries no link; see `_pieces`
                budget=budget,
                blocks=blocks,
            )
        )
    return tuple(out)


def _land_blocks(prims: tuple[Item, ...]) -> tuple:
    """The land's pads, in scene order: per `(pad ...)` (owner and `Item.block`), its primitives' boxes."""
    out: dict[tuple[str, int], list] = {}
    for it in prims:
        out.setdefault((it.owner, it.block), []).append(it.box())
    return tuple(tuple(v) for v in out.values())


def _in_boxes(at: Pt, boxes, dia: float) -> bool:
    """Is a ring of `dia` at `at` wholly inside one of `boxes`?"""
    r = dia / 2.0
    return any(b[0] + r <= at[0] <= b[2] - r and b[1] + r <= at[1] <= b[3] - r for b in boxes)


def _block_of(spec: StitchSpec, at: Pt) -> int:
    """Which pad of a thermal land a site sits in (its index in `spec.blocks`), -1 for none."""
    for k, boxes in enumerate(spec.blocks):
        if any(b[0] <= at[0] <= b[2] and b[1] <= at[1] <= b[3] for b in boxes):
            return k
    return -1


def _land_layer(scene: Scene, prims: tuple[Item, ...]) -> str:
    """The outer copper layer a land sits on — `tap._outer`'s question, asked of the primitives."""
    have: frozenset[str] = frozenset()
    for it in prims:
        have = have | it.layers
    for layer in scene.layers:
        if layer in ("F.Cu", "B.Cu") and layer in have:
            return layer
    return "F.Cu"


def _parallel_specs(ctx: PatternCtx) -> tuple[StitchSpec, ...]:
    """Every under-rated parallel via group on an unpoured power rail, `sorted(net, anchor)`.

    The population is `route_verify.via_parallelism`'s, asked of the scene instead of the finished
    file: a group of vias within `ampacity.VIA_PARALLEL_MM` of one another whose count is below
    `stackup.vias_per_change` at the net's declared current. One spec per **group**, because that is
    the unit the measurement works in — `_via_clusters` rates a member by its own cluster's size, so
    a rail with three lone barrels is three separate shortfalls and not one.

    **A poured net is exempt, and the exemption is read from the compiled job rather than from the
    file.** `scene.plane_of` is `route_scene.plane_targets`, which `poured_planes`' own docstring
    names as "the right source for a pattern deciding where to put copper"; `poured_planes` reads
    filled zones and is the right source for a check reading a board KiCad has already refilled. The
    difference is not cosmetic at this stage and it is the largest single number in the slice: at
    the `final` stage no pour is filled yet (KiCad fills the emitted board), so
    `poured_planes` would return `()` and using it would have handed this pattern 13 `3V3` groups and 30
    `GND` groups — forty-three rungs of redundant copper through both inner planes, on two rails
    whose conductor is the plane and whose vias are taps carrying one pad's share. The compiled
    answer says `GND -> In1.Cu` and `3V3 -> In2.Cu` at every stage of the route, which is the fact
    the exemption is about.

    Measured on the five boards, 2026-09-21: the population is **node's `VBUS` and nothing else** —
    three singleton groups of a 0.2 mm barrel carrying `via_amps(0.2)` = 0.527 A of a 1 A rail, each
    wanting `vias_per_change(1.0, 0.2)` = 2, so one rung each. blinky and buck carry no via at all on
    an unpoured power net; c3_usb's `3V3`/`VBUS` and the DS2 Addon's `3V3`/`VDDA`/`VSS` are 0.5 A and
    0.1 A against one 0.3 mm barrel's 0.707 A, so `per_change` is 1 and every group they have is
    rated by arithmetic rather than by luck. **This module writes nothing on four of the five.**
    """
    scene = ctx.scene
    cs = ctx.cs
    stack = scene.stack
    out: list[StitchSpec] = []
    for c in sorted(cs.constraints, key=lambda c: c.net):
        if c.current is None or c.net in scene.plane_of:
            continue
        mine = _vias_of(scene, c.net)
        if not mine:
            continue
        dt = c.current.temp_rise_c
        width = round(float(c.width_mm.value), 4)
        for group in _clusters_of(mine):
            # The **weakest** barrel rates the group, which is `route_verify.Parallel.drill`'s
            # reading of the same cluster: a group is only as good as its thinnest plating. The
            # **anchor's** size is what a new via copies (§2f); the two are the same number on every
            # group in this repo, because every group here is single-drilled.
            drill = min(mine[i]["drill"] for i in group)
            need = vias_per_change(c.current.amps, drill, stack.via_plating_mm, dt)
            add = need - len(group)
            if add <= 0:
                continue
            anchor = mine[group[0]]
            one = via_amps(drill, stack.via_plating_mm, dt)
            at = anchor["at"]
            via = (anchor["size"], anchor["drill"])
            sites = sites_ring(ctx, at, c.net, via, VIA_PARALLEL_MM)
            out.append(
                StitchSpec(
                    carrier="parallel",
                    net=c.net,
                    sites=sites,
                    need=add,
                    required=True,
                    via=via,
                    joins=("F.Cu", "B.Cu"),
                    owner=f"{c.net} via at ({at[0]:g},{at[1]:g})",
                    why=(
                        f"{len(group)} x {drill:g} mm barrel carries {one * len(group):g} A of {c.net}'s "
                        f"{c.current.amps:g} A at {dt:g} C, and vias_per_change asks {need}"
                    ),
                    bound=len(sites),
                    at=at,
                    width=width,
                )
            )
    return tuple(out)


def _owned_runs(ctx: PatternCtx, net: str) -> tuple[tuple[Pt, ...], ...]:
    """The runs of `net` that **pcbc itself routed**, in `paths_of`' order.

    B.6's precondition, asked the only way it can be asked honestly: off `PatternCtx.owned`, the list
    of pieces pcbc has written in this route. The scene cannot answer it — a scene is a board and a
    board has no memory of who wrote what (a core line is locked copper pcbc did not write).

    `route_verify.paths_of` does the walk, and it is the right walk rather than a convenient one: it
    groups by `(net, reason, layer)` and **ends a chain at a junction**, so a spine's rib and its trunk
    are two runs and each is guarded as the straight thing it is. A run that pcbc wrote and a core
    line extends comes back as pcbc's half alone, which is exactly the copper pcbc may offset.
    """
    from ..route_verify import paths_of

    mine = [p for p in ctx.owned if p.kind == "seg" and p.net == net]
    return tuple(paths_of(mine))


def _guard_specs(ctx: PatternCtx) -> tuple[StitchSpec, ...]:
    """One spec per run of each `Guard()`'s net, in `board.py` order, then `paths_of`' order.

    Two populations, and the honest one is the second.

    **The run pcbc owns** becomes a spec with a `GuardPath`: the two offset polylines, the offset's
    arithmetic, and the stitch sites along each flank. **A net pcbc did not route** becomes a spec
    with no path at all, which `run` turns into B.6's deferral sentence and no copper: a net whose
    copper is a `layout.core.py` line, or that the router left unrouted (natively every routed net's
    copper is pcbc's, the router's included).

    And where a pcbc run does exist, the flanks are mostly not there. Measured at the pre/mid stage —
    an upper bound, because `final` sees every piece of the router's copper too — the fraction of both flanks
    that `clear_runs` reports clear at the derived offset is: blinky `LED` **90.4 %** (an almost empty
    board), node `LOAD` 65.3 %, c3_usb `3V3` 24-79 % over its four runs, the DS2 Addon's ten hops
    **7.2 to 15.5 %**, and node's `LED` and c3_usb's `LED` have one flank at **0.000 mm**. A hop
    between two adjacent pads is flanked by the neighbouring pads of the same two footprints, which is
    why. `tests/fixtures/guard/guard.py` is the board where a guard has room, and section 7.1 is why it
    has to be a board and not a unit test.
    """
    cs = ctx.cs
    scene = ctx.scene
    out: list[StitchSpec] = []
    for c in sorted((c for c in cs.constraints if c.guard_stitch_mm is not None), key=lambda c: c.net):
        ground = c.guard_ground or "GND"
        pitch = float(c.guard_stitch_mm or 0.0)
        via = (scene.stack.via_diameter, scene.stack.via_drill)
        runs = _owned_runs(ctx, c.net)
        if not runs:
            out.append(
                StitchSpec(
                    carrier="guard", net=ground, sites=(), need=0, required=False, via=via,
                    joins=("F.Cu", scene.plane_of.get(ground, "B.Cu")),
                    owner=c.net,
                    why=f"a {ground} guard stitched every {pitch:g} mm",
                    bound=0, at=(0.0, 0.0), width=scene.stack.track_min, guard=None,
                )
            )
            continue
        d, why = guard_offset(scene, cs, c.net, ground)
        w = round(float(c.width_mm.value), 4)
        for path in runs:
            layer = _run_layer(ctx, c.net, path)
            sides = tuple((name, offset_path(path, d, sgn)) for name, sgn in _SIDES)
            gp = GuardPath(
                net=c.net, layer=layer, pts=path, width=w, offset=d, offset_why=why, sides=sides,
                stitch_mm=pitch, ids=_run_ids(scene, c.net, layer, path),
            )
            sites = tuple(pt for _name, flank in sides for poly in flank for pt in sites_along(ctx, poly, pitch))
            out.append(
                StitchSpec(
                    carrier="guard", net=ground, sites=sites, need=len(sites), required=False, via=via,
                    joins=(layer, scene.plane_of.get(ground, "B.Cu")),
                    owner=f"{c.net} guard",
                    why=(
                        f"two {scene.stack.track_min:g} mm {ground} tracks {d:g} mm off {gp.mm:.4g} mm of {c.net} "
                        f"({why}), stitched every {pitch:g} mm"
                    ),
                    bound=len(sites), at=path[0], width=scene.stack.track_min, guard=gp,
                )
            )
    return tuple(out)



def _plane_specs(ctx: PatternCtx) -> tuple[StitchSpec, ...]:
    """One spec per net this board pours on **two or more layers** — R-E1's population, `sorted(net)`.

    *"Plane edges stitched every 5 mm when two ground pours face each other across layers."* The
    population is exactly the nets `route_scene.plane_targets` gives more than one layer, and until
    S8's enabling change **no board could have one** (`docs/stitch-plan.md` section 2(l)): the five
    boards pour one net once, or two *different* nets (node), between which a via is a short. A
    declared `planes=` is read on every stackup (`route_scene.plane_targets`), and that is what creates
    the population (`tests/fixtures/planes`).

    **One spec per net, not per layer pair, because every via pcbc writes is a through via.** A single
    lattice ties every pour of one net at once, so a net poured on three layers still gets one
    candidate list; `cavity_pair` picks which two set the pitch and `pours` carries the rest for the
    reach test and the gate.

    `need` is the whole candidate list and `required` is False: R-E1 asks for a *pitch*, not a count,
    so "as many of these as fit" is the requirement read literally, and a lattice that loses sites to
    the copper already on the board is a coarser stitch rather than a failed one (R-S3: a target
    applies partially and says by how much). `Coverage` states the shortfall as the highest edge the
    surviving lattice is still a lambda/20 stitch for, which is the unit the pitch was derived in.
    """
    from ..route_scene import plane_targets

    scene = ctx.scene
    stack = scene.stack
    by_net: dict[str, list[str]] = {}
    for net, lay in plane_targets(ctx.job):
        by_net.setdefault(net, []).append(lay)
    out: list[StitchSpec] = []
    for net in sorted(by_net):
        lays = tuple(lay for lay in stack.copper_layers() if lay in set(by_net[net]))
        if len(lays) < 2:
            continue
        via = (stack.via_diameter, stack.via_drill)
        pair = cavity_pair(stack, lays)
        got = stitch_pitch(scene, ctx.cs, pair, net, via)
        if isinstance(got, str):
            # No edge rate: the spec exists so the refusal has a subject, and it carries no sites at
            # all. `_plane_refusal` asks `ConstraintSet.fastest_edge` the same question again rather
            # than reading a flag off here, so there is one reading of "does this board say how fast
            # it is" and not two.
            out.append(
                StitchSpec(
                    carrier="plane", net=net, sites=(), need=0, required=True, via=via, joins=pair,
                    owner=f"{net} on {' + '.join(lays)}", why=got, bound=0, pours=lays,
                )
            )
            continue
        pitch, why = got
        sites = sites_plane(ctx, net, pitch, via[0])
        out.append(
            StitchSpec(
                carrier="plane", net=net, sites=sites, need=len(sites), required=False, via=via, joins=pair,
                owner=f"{net} on {' + '.join(lays)}",
                why=f"a {pitch:g} mm lattice and edge ring ({why})",
                bound=len(sites), pours=lays, pitch=pitch,
            )
        )
    return tuple(out)


def _plane_refusal(ctx: PatternCtx, spec: StitchSpec) -> Refusal | None:
    """The one thing a plane stitch refuses, and it refuses it rather than guessing: **the board has
    not said how fast it is.**

    `docs/stitch-plan.md` section 8 item 3 threw out `DEFAULT_RISE_PS` and the four "pcbc default"
    per-kind edge rates that came with it, on the grounds that a pitch scaling linearly with an
    invented number is folklore with arithmetic on top. That refusal survives S8 intact, and this is
    where it is enforced: `NetReq(rise_ps=)` has no default in `language.py`, no entry in `PRESETS`
    and no fallback in `constraints.py`, so `ConstraintSet.fastest_edge` is `None` on a board that
    never says — and a `None` here produces a sentence with an edit in it instead of a lattice.

    Soft, and for `tap._refuse`'s reason: nothing is unconnected without this copper. A plane pair
    with no stitch is a worse board and a connected one, which is exactly what R-S1 says redundant
    copper is.

    Measured 2026-09-21: all five boards return `None` from `fastest_edge`, and none of the five has
    a net poured on two layers either — so this refusal fires on zero real boards and on the fixture
    only when the fixture is asked to prove it.
    """
    if spec.carrier != "plane" or ctx.cs.fastest_edge() is not None:
        return None
    fast = sorted({c.net for c in ctx.cs.constraints if c.kind in ("usb_hs", "clock", "spi", "pair")})
    hint = f'NetReq("{fast[0]}", ..., rise_ps=...)' if fast else "the NetReq of the fastest net on this board"
    return Refusal(
        pattern=PLANE_REASON,
        net=spec.net,
        what=spec.owner,
        clash=None,
        rule="no_edge_rate",
        hard=False,
        move="plane "
        + move_line(
            spec.net,
            f"{spec.owner}, two pours of one net facing each other across {spec.joins[0]}/{spec.joins[1]}",
            f"a stitch lattice at lambda/{PITCH_FRACTION:g} of the board's fastest edge (R-E1)",
            spec.why,
            f"Moves: add rise_ps= to {hint} — the fastest edge rate this board's parts really produce, "
            f"in picoseconds, off their datasheets; pcbc has no default for it and will not invent one",
            sep="\n  ",
        ),
    )


def plane_reach(sites: Sequence[Pt], placed: Sequence[Pt], pitch: float) -> float:
    """The **span across the widest unstitched hole** the surviving lattice leaves, in mm — the
    number that stands where the pitch stands when the lattice is complete.

    **A measurement of the copper, not of the count**, and that is the difference between a note a
    reader can act on and a number they cannot. "43 of 50 placed" is compatible with seven sites lost
    one at a time all over the board, which barely changes the stitch, and with seven lost in one
    block, which is a hole several pitches across in the middle of the cavity. This separates them.

    Two steps, and the second is the one worth stating. First the **hole radius**: the furthest any
    site the lattice wanted a via at is from the nearest via that exists — 0 when every site was
    placed, `pitch` when a single interior site was lost (its four neighbours are each a pitch away),
    `2 * pitch` when a 3 x 3 block was. Then **twice that**, because a hole of radius `r` is a span of
    `2r` between the copper on its two sides: one missing interior site leaves its two row neighbours
    exactly two pitches apart, which is the row's own spacing read off the board. The floor is
    `pitch`, since a lattice that lost nothing still only stitches at its own pitch.

    So a complete lattice answers `pitch` and reports exactly the knee frequency it was derived from,
    and every hole halves, quarters or worse the frequency the note claims. The conservative
    direction, and the one a reader can check against the board with a ruler.

    Exact and O(refused x placed), which on these boards is tens by tens. A lattice that placed
    **nothing** answers `inf`, which `_cover` turns into 0 GHz rather than into a division by zero.
    """
    keep = set(placed)
    left = [s for s in sites if s not in keep]
    if not placed:
        return math.inf if left else pitch
    radius = 0.0
    for s in left:
        radius = max(radius, min(math.dist(s, p) for p in placed))
    return max(pitch, 2.0 * radius)


def _run_ids(scene: Scene, net: str, layer: str, path: Sequence[Pt]) -> tuple[int, ...]:
    """The scene items that **are** this run — `route_scene.net_runs` asked of the board, matched to
    the pieces pcbc wrote by their shared endpoints.

    Two readings of one run, and both are needed: `ctx.owned` says *who wrote it*, which no board file
    records, and `net_runs` says *which items it is* in the scene the candidates are judged against,
    which no list of pieces records. They are matched on the endpoints rather than trusted to agree,
    so a run a core line extends past pcbc's copper contributes only the scene items whose ends lie on
    the path pcbc owns.
    """
    want = set(path)
    out: list[int] = []
    for run in net_runs(scene, net, layer):
        for i, item_id in enumerate(run.ids):
            if run.pts[i] in want or run.pts[i + 1] in want:
                out.append(item_id)
    return tuple(sorted(set(out)))


def _run_layer(ctx: PatternCtx, net: str, path: Sequence[Pt]) -> str:
    """Which layer this run is on — read off pcbc's own pieces, not guessed from the class."""
    for p in ctx.owned:
        if p.kind == "seg" and p.net == net and isinstance(p.layer, str) and p.a in (path[0], path[-1]):
            return p.layer
    return ctx.scene.layers[0] if ctx.scene.layers else "F.Cu"


def _clusters_of(vs: list[dict]) -> tuple[tuple[int, ...], ...]:
    """`route_verify._clusters`, imported at call time so the pattern and the report cannot drift.

    One import rather than a fourth walk of the same single-linkage question. `ampacity._via_clusters`
    answers it in sizes for the ampacity walk, `route_verify._clusters` in membership for the report,
    and `test_route_verify_stitch.py` already holds those two together on every board; this pattern
    needs the membership, so it asks the one that has it.
    """
    from ..route_verify import _clusters

    return _clusters(vs)


# --- the rung ---------------------------------------------------------------------------------------


def _reason(spec: StitchSpec) -> str:
    """`"thermal"` for an array barrel, `"guard"` for a shield, `"plane"` for a lattice barrel,
    `"stitch"` for a rung. See `THERMAL_REASON`, `GUARD_REASON` and `PLANE_REASON`."""
    return {"thermal": THERMAL_REASON, "guard": GUARD_REASON, "plane": PLANE_REASON}.get(spec.carrier, REASON)


def _pieces(ctx: PatternCtx, spec: StitchSpec, at: Pt) -> tuple[Piece, ...]:
    """One rung: the via, then one link segment on each of the two layers the via spans.

    **Unconditionally, at the class width, and never narrowed** (`docs/stitch-plan.md` §2e, §5). The
    via is what carries the current; the links are what make it true. Without them
    `ampacity._via_clusters` — single-linkage on distance, with no connectivity test of any kind —
    would still count the twin and hand both barrels `2 x via_amps`, so a board carrying no more
    current would report twice the ampacity. That is the one failure mode a measurement cannot
    catch about itself, and `route_verify.parallel_joined` is the check that pcbc cannot commit it.

    The links run **centre to centre**. A shorter segment stopping on the anchor's ring edge would
    clash less and connect on a boundary — two shapes exactly touching, which every other question
    on this board is allowed a `EPS_MM` of slack about — and connection is the one thing here that
    must not be decided at a tolerance.
    """
    net, reason = spec.net, _reason(spec)
    uid = (ctx.board, "pcbc", reason, net, f"{at[0]:.6f}", f"{at[1]:.6f}")
    out: list[Piece] = [via_piece(net, reason, at, spec.via[0], spec.via[1], owner=spec.owner, uuid=stable_uuid(*uid, "via"))]
    if spec.carrier in ("thermal", "plane"):
        # **A thermal barrel gets no link, and that is not an omission.** A rung needs one because
        # `ampacity._via_clusters` would otherwise count a twin joined to nothing; an array via is
        # already inside its own pad's copper on one layer and inside its own net's pour on the
        # other, so it is joined by two areas of copper that were there before it and a segment would
        # be a third path between two points already shorted. `route_verify.thermal_budget` checks
        # both containments rather than trusting either. A **plane** barrel is the same sentence with
        # two pours instead of a pad and a pour: it stands inside its own net's copper on every layer
        # it crosses before it exists, and a link between two points already shorted by two planes
        # would be a third path that only takes pour area away (`route_verify.plane_area` measures
        # exactly that). `route_verify.plane_stitch` checks every containment rather than trusting any.
        return tuple(out)
    for layer in spec.joins:
        out.append(seg_piece(net, reason, layer, spec.at, at, spec.width, owner=spec.owner, uuid=stable_uuid(*uid, "link", layer)))
    return tuple(out)


def run(ctx: PatternCtx, spec: StitchSpec) -> PatternResult:
    """One spec. The first `spec.need` sites that clear are the answer; nothing else is consulted.

    `tap.run`'s walk with two edits (`docs/stitch-plan.md` §1.3): accept until `len(placed) ==
    spec.need`, and then split on `required`. A **required** spec that fell short emits *nothing* —
    half the rungs a rail needs is a rail that still cannot carry its current, and copper that buys
    no amperes is copper the next reader has to explain. A **target** spec emits what fitted plus
    exactly one `style:` note carrying its `Coverage`.

    Five questions per site, and they are `tap`'s five with the two that cannot apply left out
    honestly rather than stubbed. `blocked` (which carries `hole_to_hole` against the anchor's own
    drill, so the near end of the ladder is judged by the fab's rule and not only bounded by it),
    `antipad_clash` (the binding one on node), `lane_ok`, and two the anchor makes necessary:
    `_merges` — a twin that joins two groups invalidates the *other* group's spec, which was computed
    from the board before any of this copper existed — and `_crowds`, the same via-to-via arithmetic
    asked against the rungs this call has already placed, because `PatternCtx` promises the scene
    does not move under a candidate and a spec placing two rungs would otherwise judge neither
    against the other. `tap._in_a_pad`'s question is asked as `_in_a_pad` here with no exemption at
    all: a tap exempts the one primitive it is welding, and a rung welds nothing, so every pad of its
    own net is a pad a via may not sit in. `tap`'s pour raster is not asked, and the reason is
    structural: a rung's net has no pour — a poured net is exempt from this pattern entirely
    (`specs`) — so there is no fill to predict and nothing to be reached by.

    **The sixth question that was built, measured, and taken out again: the corner.** A link leaves
    the anchor, so KiCad reads it and whatever else ends at that via as connected track and measures
    the interior angle between them — and node's one rung comes out at 90 degrees to the `VBUS` track
    leaving the same barrel, which is two new `pcbc_geometry_angles` warnings and takes
    `BAR["node"]["angles"]` from 100 to **102**. A filter on the ring honouring `dru.py`'s own
    `(constraint track_angle (min 135))` was written and run against the board, and the board answered
    it: that anchor is a **straight-through layer change** — a `VBUS` track leaving it upward on F.Cu
    and another leaving it downward on In1.Cu — so every one of the eight directions is 0 or 90
    degrees to one of the two and **no site on the ring can satisfy the rule at all**. Honouring it
    anyway moved the first clear site from `right` to `up`, collinear with one track and antiparallel
    to the other: **104** warnings instead of 102, and a via inside `U2`'s courtyard on a board whose
    `COURTYARD` was zero. So the rule is left to KiCad to count and the ceiling is re-pinned with this
    paragraph as its arithmetic.

    It is also not pcbc's own reading of its own rule. `route_verify.paths_of` walks a run of copper
    and stops at a junction — "a vertex where three pieces meet is a junction and ends the chains that
    reach it, which is what a chain, a spine rib or a tap stub looks like" — so `turn_ok`, the same
    135 degrees in the geometry core, is deliberately never asked of a **branch**. A rung is a branch,
    and so is every tap stub and every spine rib on these boards.
    """
    if spec.carrier not in CARRIERS:
        raise ValueError(f"{spec.carrier!r} is not a carrier this module ships ({CARRIERS}); see CARRIERS")
    if spec.carrier == "guard":
        # The one carrier that writes tracks. The walk below is a via walk — a fixed ring or lattice of
        # points, five questions each, the first `need` that clear — and a guard's candidate list is a
        # pair of *lines* cut into stretches, so it has its own. Dispatched here rather than made a
        # second `run` so that `pattern_copper` still calls one function per module and a spec is still
        # the unit of the refusal (section 1.3).
        return _run_guard(ctx, spec)
    scene = ctx.scene
    land = _land_refusal(ctx, spec) or _plane_refusal(ctx, spec)
    if land is not None:
        return PatternResult(reason=_reason(spec), net=spec.net, refusal=land, tried=0)
    # Three predicates instead of one `thermal` flag, because four carriers make three different
    # questions out of what used to be one. `linked` is "does this barrel carry link segments and an
    # anchor" — the parallel rung alone, and it is what makes `_merges`, the straight-line half of
    # `_crowds` and the micro-length guard apply. `thermal` is "is this via deliberately inside a pad
    # of its own net", which is the one carrier `_in_a_pad` must not be asked about. `rastered` is
    # "does this via have to be reached by a fill that does not exist yet", which is every carrier
    # that lands in a pour rather than in a track.
    linked = spec.carrier == "parallel"
    thermal = spec.carrier == "thermal"
    rasters = _raster(ctx, spec)
    placed: list[Pt] = []
    pieces: list[Piece] = []
    seen: list[Clash] = []
    worst: tuple[float, Clash] | None = None
    tried = 0
    lane_why = ""
    merged = 0
    crowded = 0
    unreached = 0
    per_block: dict[int, list] = {}  # thermal: block -> [placed, sites tried, worst clash]

    def walk():
        """The sites in their order — except a thermal land's: every pad of the land first, each one's
        sites in order until one takes a barrel, then the rest in rank-major order. So a pad whose best
        site is blocked (a track under the top row of the ESP32's land) takes its next site before any
        other pad takes a second barrel, and a pad left bare is one where every site was tried."""
        if not (thermal and len(spec.blocks) > 1):
            yield from spec.sites
            return
        order: dict[int, list] = {}
        for at in spec.sites:
            order.setdefault(_block_of(spec, at), []).append(at)
        done: set = set()
        for k, sites in order.items():
            if k < 0:
                continue
            for at in sites:
                done.add(at)
                yield at
                if per_block.get(k, [0])[0]:
                    break
        yield from (at for at in spec.sites if at not in done)

    for at in walk():
        if len(placed) == spec.need:
            break
        blk = per_block.setdefault(_block_of(spec, at), [0, 0, None]) if thermal else None
        if blk is not None:
            blk[1] += 1
        cand = _pieces(ctx, spec, at)
        if linked and cand[1].mm < MICRO_MM:
            continue  # a link KiCad's own `track_segment_length` rule would count; the ring is wider
        tried += 1
        if linked and _merges(scene, spec, at):
            merged += 1
            continue
        why = "" if thermal else _crowds(ctx, spec, at, placed)
        if why:
            crowded += 1
            continue
        # `_in_a_pad` is asked of a rung and **never** of an array, and the two answers are the same
        # rule read from its two ends. A rung welds nothing, so every pad of its own net is a pad a
        # via may not sit in; an array's whole technique is the via **inside** the land, which is why
        # `Thermal()` carries the fab declaration (`Stackup.via_fill`) and why the compiler refuses
        # the statement outright on a passive's pad, where no fill saves the joint. A.4 rule 1 skips
        # a same-net pair either way, so `blocked` has nothing to say about a via in its own land and
        # the containment is checked where it can be measured: `route_verify.thermal_budget`.
        clash = (None if thermal else _in_a_pad(scene, spec, at)) or blocked(scene, cand, spec.net) or antipad_clash(scene, at, spec.via[0], spec.net)
        if clash is not None:
            seen.append(clash)
            if worst is None or (clash.need - clash.have) > worst[0]:
                worst = (clash.need - clash.have, clash)
            if blk is not None and (blk[2] is None or (clash.need - clash.have) > (blk[2].need - blk[2].have)):
                blk[2] = clash
            continue
        fits, lane = lane_ok(scene, cand, ())
        if not fits:
            lane_why = lane_why or lane
            continue
        if any(not r.reaches(at, spec.via[0] / 2.0) for r in rasters):
            # D.5, and `docs/stitch-plan.md` section 4 will not ship without it: an outer pour is
            # written before this stage but **not filled** until KiCad fills the emitted board, so a
            # site the fill will not reach is a barrel welded to a pad on one layer and
            # to nothing on the other. It is the array's far end that would be missing, which is the
            # half `thermal_budget` measures and the half the whole technique is for.
            unreached += 1
            continue
        placed.append(at)
        pieces.extend(cand)
        if blk is not None:
            blk[0] += 1
    got = len(placed)
    bare_notes = _bare_notes(ctx, spec, per_block) if thermal and len(spec.blocks) > 1 else ()
    if got == spec.need and not bare_notes:
        return PatternResult(
            reason=_reason(spec),
            net=spec.net,
            pieces=tuple(pieces),
            candidate=", ".join(_site_name(spec, at) for at in placed),
            tried=tried,
            # **The one carrier whose complete answer still gets a note.** A `plane` spec's `need` is
            # its whole candidate list, so "it placed them all" is the *good* outcome rather than the
            # silent one, and the number a reader wants — the highest edge this lattice is still a
            # lambda/20 stitch for — exists either way. Every other carrier reports only a shortfall,
            # because for them a complete answer is what `candidate` already says.
            notes=(_cover(ctx, spec, got, "", placed).line(),) if spec.carrier == "plane" else (),
        )
    if not spec.required:
        # R-S3's other half: a target applies partially and reports the shortfall, in **one** note —
        # and a thermal land with a bare pad one more per bare pad, naming what emptied it.
        stopped = _shortfall(spec, tried, worst[1] if worst else None, unreached, lane_why, crowded)
        cover = (_cover(ctx, spec, got, stopped, placed).line(),) if got < spec.need else ()
        return PatternResult(reason=_reason(spec), net=spec.net, pieces=tuple(pieces), tried=tried, notes=cover + bare_notes)
    return PatternResult(
        reason=REASON,
        net=spec.net,
        refusal=_refuse(ctx, spec, got, worst[1] if worst else None, seen, tried, lane_why, merged, crowded),
        tried=tried,
    )


# --- carrier `guard`: the walk ----------------------------------------------------------------------


DEFERRED = (
    "guard {net}: deferred - the net's copper is not pcbc's (a core line, or none yet), so pcbc cannot "
    "offset a path it does not own."
)
"""B.6's deferral, verbatim, and it is a **note** rather than a `Refusal`.

Every refusal in this repo is a move ending in a `board.py` edit (`moves.move_line`), and there is
no edit that makes pcbc route the DS2 Addon's `AIN0`: it is `kind="analog"`, which compiles to one
layer and `via.allowed=False`, and the pattern that would route a constrained net is the maze router
R3 has not written. The only "edit" a refusal could offer is *delete the `Guard()` line*, which is not
a repair, and it would put an entry into `REFUSED` — an exact pinned number per board — on every board
that guards a net pcbc does not route, which measured 2026-09-21 is every board that could declare one.

So it reports, which is what B.6 says it does: "It reports, and this is the whole message.\""""


def _run_guard(ctx: PatternCtx, spec: StitchSpec) -> PatternResult:
    """B.6's shield: two offset tracks, the stretches of them that are clear, and the vias that weld
    them to the pour.

    **The one pattern allowed to apply partially, and R-S3 says why:** a guard is a shield and not a
    connection, so a blocked stretch is dropped rather than failing the pattern, and one `Coverage`
    note says how much of the run is guarded. The unit of dropping is a **stretch** — a maximal run of
    one flank that `clear_runs` reports clear — and never a candidate offset, a narrower track or a
    coarser pitch: B.0's "a pattern never degrades" keeps its full force on geometry, and `Coverage`
    deliberately cannot express a geometry change (see `Coverage`).

    Four things are asked of every stretch, in this order, and the first three are the ones the
    proposer cannot answer for itself:

    1. it is at least `guard_min_mm` long — a stretch too short to hold one stitch via is an island of
       ground copper that nothing welds, which is `route_verify.guard_cover`'s whole subject;
    2. `shape_ok` — 0/45/90 and no turn sharper than 135 degrees, asked of the copper as written;
    3. `blocked` — the decision, against the real board. `clear_runs` is a slab sweep over bounding
       boxes and is therefore a *proposal*: it can only be more pessimistic than `clears` about a
       rotated pad, never more optimistic, so a stretch it offers can still lose here and one it
       withholds was never legal. `clears` decides; `clear_runs` reports.
    4. `lane_ok` — a guard runs *along* things for a living, which is exactly what a fanout lane
       forbids, so this is the rule most likely to take a flank on a board with a closed row.

    Then the vias, and a via is what makes a stretch ground at all: each site must land on a stretch
    that was accepted, clear `blocked` and `antipad_clash`, stay out of a lane, and on two layers be
    somewhere `pour_raster` says the fill will reach — D.5's rule, and the half of the technique that
    a board without it fails silently (blinky's routed board has no `(zone ...)` block at all, so
    sixteen stitch vias there would weld to nothing and every existing check would pass them).

    **Finally, a stretch that ended with no via is deleted again**, and this is the step that makes
    the whole pattern honest rather than decorative. Ground copper welded to nothing is invisible to
    every judge on the board: KiCad's unconnected-items check is pad to pad, `netcheck.check_copper`
    reads pad bindings inside footprint blocks and never a segment, and `verify_copper` asks about
    clearance, angle and size. It would look like a guard, measure like a guard in the copper bar, and
    shield nothing.
    """
    gp = spec.guard
    if gp is None:
        return PatternResult(reason=GUARD_REASON, net=spec.net, notes=(DEFERRED.format(net=spec.owner),))
    scene = ctx.scene
    if spec.net not in scene.plane_of:
        return PatternResult(reason=GUARD_REASON, net=spec.net, refusal=_guard_refuse_pour(ctx, spec), tried=0)
    floor = guard_min_mm(scene)
    stretches: list[tuple[str, tuple[Pt, ...], tuple[Piece, ...]]] = []
    dropped: list[str] = []
    tried = 0
    for name, flank in gp.sides:
        for poly in flank:
            for pts in _stretches(ctx, spec, poly):
                tried += 1
                mm = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
                if mm + 1e-9 < floor:
                    dropped.append(f"{mm:.3f} mm on the {name} is under the {floor:g} mm one stitch via needs")
                    continue
                shaped = mitre(pts)
                if shaped is None or not shape_ok(shaped):
                    dropped.append(f"a {mm:.3f} mm stretch on the {name} is not 0/45/90 once mitred")
                    continue
                pieces = pieces_of(shaped, [spec.width] * (len(shaped) - 1), net=spec.net, reason=GUARD_REASON, layer=gp.layer, owner=spec.owner, board=ctx.board)
                clash = blocked(scene, pieces, spec.net)
                if clash is not None:
                    dropped.append(f"{clash.item.label()} leaves {clash.have:.3f} mm of {clash.need:.3f} mm on the {name} (rule: {clash.rule})")
                    continue
                fits, lane = lane_ok(scene, pieces, ())
                if not fits:
                    dropped.append(f"a stretch on the {name} {lane} (rule: lane)")
                    continue
                stretches.append((name, shaped, pieces))
    rasters = _raster(ctx, spec)
    held: dict[int, list[Piece]] = {}
    for at in spec.sites:
        k = _on_a_stretch(stretches, at, spec.width)
        if k is None:
            continue
        cand = (via_piece(spec.net, GUARD_REASON, at, spec.via[0], spec.via[1], owner=spec.owner, uuid=stable_uuid(ctx.board, "pcbc", GUARD_REASON, spec.net, f"{at[0]:.6f}", f"{at[1]:.6f}", "via")),)
        if blocked(scene, cand, spec.net) is not None or antipad_clash(scene, at, spec.via[0], spec.net) is not None:
            continue
        if not lane_ok(scene, cand, ())[0]:
            continue
        if any(not r.reaches(at, spec.via[0] / 2.0) for r in rasters):
            continue
        held.setdefault(k, []).append(cand[0])
    pieces: list[Piece] = []
    guarded = 0.0
    got = 0
    for k, (name, pts, segs) in enumerate(stretches):
        mm = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        if k not in held:
            dropped.append(f"a {mm:.3f} mm stretch on the {name} got no stitch via, so nothing would weld it to the {spec.net} pour")
            continue
        # The shield ends on its outermost vias (fourth review, C4). A track end that overhangs the
        # last via touches nothing, which is KiCad's `track_dangling`, and the build never ships a
        # dangling track: before this, the route stage's dangling sweep took every guard track off
        # the fixture board and then every stitch via it had orphaned, and the guard gate reported
        # "pcbc wrote no guard copper". A stretch with one via has no track that is not dangling.
        cut = _between_vias(pts, [v.a for v in held[k]])
        if cut is None:
            dropped.append(f"a {mm:.3f} mm stretch on the {name} got one stitch via; a shield track ends on a via at each end, so it needs two")
            continue
        if cut != pts:
            if not shape_ok(cut):
                dropped.append(f"a {mm:.3f} mm stretch on the {name} is not 0/45/90 once cut to its outer vias")
                continue
            pts = cut
            mm = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            segs = pieces_of(pts, [spec.width] * (len(pts) - 1), net=spec.net, reason=GUARD_REASON, layer=gp.layer, owner=spec.owner, board=ctx.board)
        pieces.extend(segs)
        pieces.extend(held[k])
        guarded += mm
        got += len(held[k])
    if got == spec.need and not dropped:
        return PatternResult(reason=GUARD_REASON, net=spec.net, pieces=tuple(pieces), candidate=", ".join(n for n, _p, _s in stretches), tried=tried)
    cover = Coverage(
        want=spec.need, got=got, measure=round(guarded, 3), target=round(2.0 * gp.mm, 3), unit="mm",
        floor_ok=got > 0 and guarded + 1e-9 >= floor, why=spec.owner, blocked="; ".join(dropped[:2]),
    )
    return PatternResult(reason=GUARD_REASON, net=spec.net, pieces=tuple(pieces), tried=tried, notes=(cover.line(),))


def _between_vias(pts: Sequence[Pt], vias: Sequence[Pt]) -> tuple[Pt, ...] | None:
    """`pts` cut to the stretch between the first and last of `vias` along it (each via taken at its
    nearest point on the polyline, on KiCad's 1 nm grid), or None when that is a point.

    Why: a track's end is joined only to what it lands on, so a shield whose end runs past its last
    stitch via is dangling copper by KiCad's rule (`track_dangling`), however well its vias weld the
    rest of it. Cut at the vias, both ends of every guard track land inside a via."""
    legs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    run = [0.0]
    for a, b in legs:
        run.append(run[-1] + math.dist(a, b))
    params = []
    for v in vias:
        best = None
        for i, (a, b) in enumerate(legs):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((v[0] - a[0]) * dx + (v[1] - a[1]) * dy) / L2))
            p = (a[0] + t * dx, a[1] + t * dy)
            d = math.dist(p, v)
            if best is None or d < best[0] - 1e-12:
                best = (d, run[i] + t * math.dist(a, b), i, p)
        params.append(best)
    lo = min(params, key=lambda b: b[1])
    hi = max(params, key=lambda b: b[1])
    if hi[1] - lo[1] < 1e-6:
        return None

    if lo[1] < 1e-6 and hi[1] > run[-1] - 1e-6:
        return tuple(pts)
    out = [(q(lo[3][0]), q(lo[3][1]))] + [pts[j] for j in range(lo[2] + 1, hi[2] + 1)] + [(q(hi[3][0]), q(hi[3][1]))]
    clean: list[Pt] = []
    for p in out:
        if not clean or math.dist(clean[-1], p) > 1e-6:
            clean.append(p)
    return tuple(clean) if len(clean) >= 2 else None


def _stretches(ctx: PatternCtx, spec: StitchSpec, poly: Sequence[Pt]) -> tuple[tuple[Pt, ...], ...]:
    """One flank cut into the maximal runs of it that `clear_runs` reports clear.

    Leg by leg, in the flank's own order, and a run continues **through a corner** when the previous
    leg was clear to its end and the next is clear from its start — so a guard that turns writes one
    mitred run and not two abutting ones, which is what the copper bar counts and what
    `route_verify.paths_of` walks.

    Everything in the conversion is integer: `clear_runs` returns 4 dp millimetres of the axis' own
    coordinate (a whole number of hundreds of nanometres), a leg's along-coordinate advances by
    exactly +-1 per nanometre step on an axis and +-2 on a diagonal, so an interval becomes a whole
    number of steps by ceiling its near end and flooring its far one. Inward on both, for
    `free_intervals`' reason in `free_intervals`' words: never wider than the truth.
    """
    ip = [(_nm(x), _nm(y)) for x, y in poly]
    half = spec.width / 2.0
    layer = spec.guard.layer if spec.guard is not None else ctx.scene.layers[0]
    out: list[tuple[Pt, ...]] = []
    chain: list[tuple[int, int]] = []

    def flush() -> None:
        if len(chain) >= 2:
            out.append(tuple((_mm(x), _mm(y)) for x, y in chain))
        chain.clear()

    for i in range(len(ip) - 1):
        a, b = ip[i], ip[i + 1]
        u = (_sgn(b[0] - a[0]), _sgn(b[1] - a[1]))
        axis = _AXIS_OF[u]
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        per = _along(axis, u)  # +-1 on an axis leg, +-2 on a diagonal one
        ca = _along(axis, a)
        ivs = clear_runs(
            axis, (_mm(ca), _mm(_along(axis, b))), _mm(_across(axis, a)), half, spec.net, ctx.scene, layer,
            ignore=frozenset(spec.guard.ids) if spec.guard is not None else frozenset(),
        )
        ranges: list[tuple[int, int]] = []
        for lo, hi in ivs:
            k0, k1 = sorted(((round(lo * 1e6) - ca) / per, (round(hi * 1e6) - ca) / per))
            s0, s1 = max(0, math.ceil(k0 - 1e-9)), min(n, math.floor(k1 + 1e-9))
            if s1 > s0:
                ranges.append((s0, s1))
        ranges.sort()
        for j, (s0, s1) in enumerate(ranges):
            start = (a[0] + s0 * u[0], a[1] + s0 * u[1])
            end = (a[0] + s1 * u[0], a[1] + s1 * u[1])
            if j == 0 and s0 == 0 and chain and chain[-1] == start:
                chain.append(end)  # through the corner: one mitred run, not two abutting ones
            else:
                flush()
                chain.extend((start, end))
            if s1 != n:
                flush()
        if not ranges:
            flush()
    flush()
    return tuple(out)


def _on_a_stretch(stretches: Sequence[tuple[str, tuple[Pt, ...], tuple[Piece, ...]]], at: Pt, width: float) -> int | None:
    """Which accepted stretch this site sits on, or None.

    A via off every stretch has no track to weld, so it is not a candidate at all: the site list is
    the **whole** flank, from `sites_along`, and the stretches are what survived of it. This is the
    question that turns "B.6's pitch along the guarded path" into "the vias this guard may actually
    place" without moving a single one of them — which is what B.0's never-degrade rule asks for, and
    the reason the pitch never adapts to the stretches it finds.

    The probe is a disc of the **track's** width rather than the via's, because the question is
    whether the site is on the centreline the track was drawn along; the via's own clearance to
    everything else is `blocked`'s, one line up in `_run_guard`."""
    probe = via_shape(at, width)
    for k, (_name, pts, _segs) in enumerate(stretches):
        for i in range(len(pts) - 1):
            if gap(probe, track_shape(pts[i], pts[i + 1], width)) <= 0.0:
                return k
    return None


def _guard_refuse_pour(ctx: PatternCtx, spec: StitchSpec) -> Refusal:
    """The one guard refusal, and it is the blinky trap: the net the shield is made of has no pour.

    `docs/stitch-plan.md` section 3 keeps this as a **pattern** refusal rather than a compile one,
    because its move is a `Board(...)` edit that reads better beside the geometry — and because the
    fact it rests on is `route_scene.plane_targets`, which is a property of the compiled job and the
    stackup together. Measured: blinky's routed board carries **zero** `(zone ...)` blocks, so a guard
    there would place its vias into a pour that was never written and every existing check would pass
    them (section 6 row 3).
    """
    ground = spec.net
    return Refusal(
        pattern=GUARD_REASON,
        net=ground,
        what=spec.owner,
        clash=None,
        rule="no_pour",
        hard=False,
        move="guard "
        + move_line(
            ground,
            spec.owner,
            f"two {ctx.scene.stack.track_min:g} mm {ground} tracks stitched to the {ground} pour",
            f"{ground} has no plane or pour on this board, so every stitch via would weld to nothing "
            f"and the two tracks would be islands on a named net that KiCad's pad-to-pad unconnected "
            f"check cannot see (rule: no_pour)",
            f'Moves: NetReq("{ground}", kind="power") gives a two-layer board its back pour; or '
            f'Board(planes=[("{ground}", "In1.Cu")]) on four layers; or drop the Guard("{spec.owner}") line',
            sep="\n  ",
        ),
    )


def _raster(ctx: PatternCtx, spec: StitchSpec) -> tuple:
    """Every pour this spec's via has to be reached by, as a predicted reach; `()` for the rest.

    `tap._raster`'s body and its reason, asked for the barrels: no pour is filled while the route
    stage runs (KiCad fills the emitted board), so for an outer-layer pour the raster is the only thing
    between a barrel and an island.

    **A tuple rather than one raster, because a plane spec has two pours and both of them decide.**
    A `thermal` spec lands in one pad and one pour and asks about the pour; a `plane` spec's whole
    point is the pair, so a via reached by the front pour and not the back is a via that ties one
    plane to nothing — the same orphan `route_verify.guard_cover` exists for, one layer over. Only
    the outer layers are predicted: an inner plane carries no signal copper on its layer
    (`route_native.route_layers`), and `plane_islands` and `plane_area` measure it after the fill,
    which is the stronger check.
    """
    from ..route_verify import pour_raster

    if spec.carrier == "plane":
        return tuple(pour_raster(ctx.scene, spec.net, lay) for lay in spec.pours if lay in ("F.Cu", "B.Cu"))
    if spec.carrier == "parallel":
        # A rung's net is by definition unpoured (`_parallel_specs` skips a net in `scene.plane_of`),
        # so there is no fill to predict and nothing to be reached by. Asked and answered here rather
        # than left to `joins[1]` happening not to name an outer layer.
        return ()
    plane = spec.joins[1]
    if plane not in ("F.Cu", "B.Cu"):
        return ()
    return (pour_raster(ctx.scene, spec.net, plane),)


def _land_refusal(ctx: PatternCtx, spec: StitchSpec) -> Refusal | None:
    """The fifth `Thermal()` refusal — the one the compiler cannot make, because it is about geometry.

    `docs/stitch-plan.md` section 3 lists five things `Thermal()` refuses and puts all five in
    `cs.refusals`. Four of them are there (`constraints._thermal_refusals`); this one is not, and the
    reason is the house rule that a refusal lives where its measurement lives: *"the pad carries no
    `pad_prop_heatsink` and is narrower than `pitch + dia`"* is two facts about a footprint, and
    `constraints.py` has never opened one. Here the primitives are `Item`s in the scene with
    `pads.PadGeom.prop` carried on them, and the pitch is the derived one rather than the plan's
    guess, so the sentence can name real numbers.

    **Both halves, and the `and` is load-bearing.** A land that declares `pad_prop_heatsink` is a land
    whatever its size: the footprint has said what it is for, and a single barrel under a small
    thermal tab is a real, common layout. A pad that declares nothing and is too narrow to hold two
    barrels at the derived pitch is a **pin** — measured on the ESP32-C3-MINI, `U1.3` is
    0.85 x 0.30 mm and 48 of its 49 pads are that shape — and an array under one of those is a
    typo for the land next to it.

    Nothing on these five boards reaches it: all 18 `pad_prop_heatsink` pads in the repo are the two
    ESP32 lands, and both declare it. It is a fixture's refusal, pinned now rather than discovered by
    the first board that mistypes a pad number.
    """
    if spec.carrier != "thermal" or spec.budget is None:
        return None
    prims = _land(ctx.scene, spec.budget.ids)
    if not prims:
        return _thermal_refuse(ctx, spec, f"no pad of {spec.budget.ref} on this board is numbered {'/'.join(spec.budget.nums) or '?'}", "no candidate")
    if any("pad_prop_heatsink" in it.prop for it in prims):
        return None
    pitch, _why = thermal_pitch(ctx.scene, spec.net, spec.via)
    need = round(pitch + spec.via[0], 4)
    widest = max(prims, key=lambda it: min(it.box()[2] - it.box()[0], it.box()[3] - it.box()[1]))
    b = widest.box()
    across = round(min(b[2] - b[0], b[3] - b[1]), 4)
    if across + 1e-9 >= need:
        return None
    return _thermal_refuse(
        ctx,
        spec,
        f"{spec.owner} draws {len(prims)} primitive(s), the widest {round(b[2] - b[0], 4):g} x {round(b[3] - b[1], 4):g} mm, "
        f"and none of them carries pad_prop_heatsink; {across:g} mm across will not hold two "
        f"{spec.via[0]:g} mm rings at the {pitch:g} mm this board's antipads allow, which needs {need:g} mm (rule: not_a_land)",
        "not_a_land",
    )


def _thermal_refuse(ctx: PatternCtx, spec: StitchSpec, blockers: str, rule: str) -> Refusal:
    """One refusal for a `Thermal()` whose land is not one. Soft, and the move is the statement itself.

    Soft for `tap._refuse`'s reason taken one step further: the board connected that pad before this
    pattern existed — `tap` welded it and this slice deliberately did not change that
    (`docs/stitch-plan.md` section 2(g)) — so a refused array costs conductance and never a
    connection. What it must not do is place the array anyway.
    """
    b = spec.budget
    assert b is not None
    heats = sorted({it.owner for it in ctx.scene.items if it.kind == "pad" and "pad_prop_heatsink" in it.prop and it.owner.startswith(f"{b.ref}.")})
    did_you = f'; did you mean Thermal("{heats[0]}")?' if heats else f"; {b.ref} declares no pad_prop_heatsink pad at all"
    return Refusal(
        pattern=THERMAL_REASON,
        net=spec.net,
        what=spec.owner,
        clash=None,
        rule=rule,
        hard=False,
        move="thermal "
        + move_line(
            spec.net,
            f"{spec.owner}, declared on Thermal line {b.req.line}",
            f"{b.need} x {b.via[0]:g}/{b.via[1]:g} barrel(s) under it ({spec.why})",
            blockers,
            f'Moves: drop the Thermal("{spec.owner}") line{did_you}',
            sep="\n  ",
        ),
    )


BARE = "bare pad"
"""The words every bare-pad note of a thermal land starts its clause with (`_bare_notes`); the gate
on the finished board quotes the notes that carry them (`build._thermal_gate`)."""


def _bare_notes(ctx: PatternCtx, spec: StitchSpec, per_block: dict) -> tuple[str, ...]:
    """One note per pad of a thermal land left with no barrel: where it is, how many of its sites were
    tried, the worst blocker and the edit that moves it (`_moves`). Empty when every pad has one."""
    out = []
    for k, boxes in enumerate(spec.blocks):
        placed, tried, clash = per_block.get(k, [0, 0, None])
        if placed:
            continue
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        at = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        n = sum(1 for s in spec.sites if _block_of(spec, s) == k)
        if clash is not None:
            why = f"all {tried} of its {n} site(s) tried, worst blocker {clash.item.label()} at {clash.at[0]:g},{clash.at[1]:g} leaving {clash.have:.3f} mm of {clash.need:.3f} mm (rule: {clash.rule})"
        elif n == 0:
            why = f"no {spec.via[0]:g} mm ring fits inside it"
        else:
            why = f"{tried} of its {n} site(s) tried and none cleared"
        out.append(f"thermal {spec.owner}: {BARE} at ({at[0]:g},{at[1]:g}) (pad {k + 1} of the land's {len(spec.blocks)}) has no barrel: {why}. {_bare_moves(ctx, spec, clash)}")
    return tuple(out)


def _bare_moves(ctx: PatternCtx, spec: StitchSpec, clash: Clash | None) -> str:
    """The edits that give a bare pad of a thermal land its barrel, each ending in a `board.py` line:
    take the blocker off its layer (it is another net's copper; a `NetReq(layers=)` routes it on the
    other side), or turn or move the land's part so the land clears it."""
    ref = spec.owner.split(".")[0]
    edits = []
    if clash is not None and clash.item.reason == "core":
        edits.append(f"move {clash.item.owner} off the land or delete it (it is a lock, so pcbc routes around it and never moves it)")
    elif clash is not None and clash.item.kind in ("track", "via") and clash.item.net and clash.item.net != spec.net:
        layer = sorted(clash.item.layers)[0] if len(clash.item.layers) == 1 else "this layer"
        other = "F.Cu" if layer == "B.Cu" else _other(ctx)
        edits.append(f'NetReq("{clash.item.net}", layers=["{other}"]) takes {clash.item.owner} off {layer} under the land')
    elif clash is not None and clash.item.kind == "pad" and "." in clash.item.owner:
        edits.append(f'move {clash.item.owner.split(".")[0]} (its Place()) off the land')
    edits.append(f'turn or move {ref} (its Place(), e.g. rotate=) so the land clears it')
    return "Moves: " + "; or ".join(edits) + "."


def _shortfall(spec: StitchSpec, tried: int, clash: Clash | None, unreached: int, lane: str, crowded: int = 0) -> str:
    """One clause saying why a **target** ran out of sites: the worst blocker, or the pour, or the ring.

    The worst-first ordering of `_refuse`, compressed to a single blocker, because a target emits one
    note and a refusal emits a paragraph. Everything in it is the same measurement the refusal would
    have printed — `Clash.have`, `Clash.need` and the rule of A.4 that decided it — so a short array
    and a refused rung are read the same way."""
    if tried >= spec.bound and spec.bound:
        where = f"all {spec.bound} sites tried"
    else:
        where = f"{tried} of {spec.bound} sites tried"
    if clash is not None:
        return f"{where}, worst blocker {clash.item.label()} at {clash.at[0]:g},{clash.at[1]:g} leaving {clash.have:.3f} mm of {clash.need:.3f} mm (rule: {clash.rule})"
    if unreached:
        return f"{where}, the pour does not reach {unreached} of them (rule: pour_reach)"
    if lane:
        return f"{where}, every site left {lane} (rule: lane)"
    if crowded:
        # The one shortfall with no blocker at all: the spec's own earlier vias took the site. A
        # lattice and a ring are two candidate lists on one pitch with different phases, so the row
        # nearest an edge lands a fraction of a pitch from the ring beside it and loses — which costs
        # a candidate and no coverage, because the via that won is where the lost one wanted to be.
        return f"{where}, {crowded} of them within {spec.via[0]:g} mm of a barrel this lattice had already placed (rule: via_pitch)"
    return f"{where}, and the land has no more room"


def _cover(ctx: PatternCtx, spec: StitchSpec, got: int, blocked_why: str = "", placed: Sequence[Pt] = ()) -> Coverage:
    """What a partial spec achieved, in the unit its shortfall was stated in."""
    if spec.carrier == "plane":
        # **The measure is a frequency, and it is the same arithmetic the pitch was derived from read
        # backwards.** A count would be unreadable here: "48 of 63" is compatible with fifteen sites
        # lost one at a time, which is almost no change, and with fifteen lost in one block, which is
        # a hole four pitches across in the middle of the cavity. `plane_reach` separates those two,
        # and dividing the wavelength by it gives the highest knee this lattice is still a
        # lambda/PITCH_FRACTION stitch for — against the knee the board's own fastest edge asks for.
        # More is better here, so `Coverage.line`'s floor wording is the right one unchanged.
        v = cavity_velocity(ctx.scene.stack, spec.joins)
        fast = ctx.cs.fastest_edge()
        want = knee_ghz(fast[1]) if fast is not None else 0.0
        worst = plane_reach(spec.sites, placed, spec.pitch)
        have = 0.0 if not math.isfinite(worst) else round(v / (PITCH_FRACTION * worst), 3)
        return Coverage(
            want=spec.need, got=got, measure=have, target=round(want, 3), unit="GHz",
            floor_ok=have + 1e-9 >= want, why=f"{spec.net} plane", blocked=blocked_why,
        )
    if spec.carrier == "thermal" and spec.budget is not None:
        b = spec.budget
        # **The measure is the rise, not the count**, because the rise is what the author budgeted and
        # because the relationship is a reciprocal: going from 9 barrels to 8 is 11 % fewer vias and
        # 12.5 % more degrees, and a note that reported the count would let a reader do that
        # arithmetic wrong. `floor_ok` is `<=` here and `>=` for a rail — see `Coverage.line`.
        rise = b.rise_of(got)
        return Coverage(want=spec.need, got=got, measure=rise, target=round(b.rise_c, 3), unit="C", floor_ok=rise <= b.rise_c + 1e-9, why=spec.owner, blocked=blocked_why)
    c = ctx.cs.by_net(spec.net)
    dt = c.current.temp_rise_c if (c is not None and c.current is not None) else 10.0
    amps = float(c.current.amps) if (c is not None and c.current is not None) else 0.0
    one = via_amps(spec.via[1], ctx.scene.stack.via_plating_mm, dt)
    have = round(one * (got + 1), 3)  # the anchor plus the rungs that fitted
    return Coverage(want=spec.need, got=got, measure=have, target=round(amps, 3), unit="A", floor_ok=have + 1e-9 >= amps, why=spec.owner, blocked=blocked_why)


# --- the two questions the anchor makes necessary ---------------------------------------------------


def _merges(scene: Scene, spec: StitchSpec, at: Pt) -> bool:
    """Would a twin here pull a **second** group of this net into the anchor's?

    `specs` derives every group's shortfall from the board as it stands, once, before a millimetre of
    this copper exists — that is what makes the candidate list fixed. A twin within
    `VIA_PARALLEL_MM` of a via outside its own group merges the two under single linkage, and the
    other group's spec — already computed, still to run — becomes a statement about a board that no
    longer exists: its `n` is wrong, its `need - n` is wrong, and it would place a rung for a
    shortfall that the merge has already covered or made worse.

    Refused per **site**, not per spec: the ring has eight directions and a merge usually rules out
    one or two of them, so the honest answer is to take the next site and to say how many were lost
    that way only if the spec ends with nothing (`_refuse`).
    """
    for it in scene.items:
        if it.kind != "via" or it.net != spec.net or it.copper is None:
            continue
        here = it.at()
        if math.dist(here, spec.at) <= VIA_PARALLEL_MM:
            continue  # the anchor's own group: joining it is the point
        if math.dist(here, at) <= VIA_PARALLEL_MM:
            return True
    return False


def _crowds(ctx: PatternCtx, spec: StitchSpec, at: Pt, placed: list[Pt]) -> str:
    """Why this site cannot sit beside a rung **this same call** has already placed; "" when it can.

    `PatternCtx` promises the scene is mutated between patterns and never under a candidate, so a
    spec placing more than one rung cannot ask `blocked` about its own earlier ones — they are not on
    the board yet. `docs/stitch-plan.md` §5 answers that for the thermal lattice by deriving the
    pitch so the sites clear each other *by construction*; the same two rules are asked here directly
    instead, because a ring is not a lattice and its sites are not uniformly spaced.

    Two rules, both already stated elsewhere in this file. The pitch is `via_pitch` composed with the
    antipad limit `antipad_clash` enforces against the board. And the **turn**: two links on one
    layer meet at the anchor, `route_verify.paths_of` walks them as a single path through it, and
    `turn_ok` refuses anything sharper than 135 degrees — so a second rung must be the exact opposite
    of a first, which makes the three vias a straight line and the two links one run of copper.
    Nothing in this repo needs a second rung (`specs`: every shortfall on every board is exactly
    one), so this is arithmetic rather than a measurement, and it is asserted in
    `tests/test_stitch.py` rather than by a board.
    """
    if not placed:
        return ""
    dia, drill = spec.via
    pitch = ctx.scene.table.via_pitch(spec.net, spec.net, drill, dia, drill, dia)
    for other in placed:
        d = math.dist(other, at)
        if d + 1e-9 < pitch + EPS_MM:
            return f"{d:.4f} mm from the rung at ({other[0]:g},{other[1]:g}), against the {pitch:g} mm two vias of one net keep"
        if spec.carrier == "plane":
            # **The pitch half only.** The straight-line rule below is about two links leaving one
            # anchor — `route_verify.paths_of` walks them as a single path through the via and
            # `turn_ok` refuses anything sharper than 135 degrees — and a lattice barrel has no link
            # and no anchor, so there is no path through it to turn. What a lattice does need is the
            # near limit, and it needs it for a reason a lattice alone has: the interior grid and the
            # edge ring are two candidate lists on one pitch with **different phases**, so a ring site
            # and the first interior row can land closer than `via_pitch` on a board whose side is not
            # a whole number of pitches. `sites_lattice`'s "the sites clear each other by
            # construction" is true within one grid and false between two.
            continue
        ax, ay = _nm(at[0]) - _nm(spec.at[0]), _nm(at[1]) - _nm(spec.at[1])
        bx, by = _nm(other[0]) - _nm(spec.at[0]), _nm(other[1]) - _nm(spec.at[1])
        # Integer nanometres, because "these three vias are in a straight line" is a question a
        # float cross product answers differently for the same copper (A.5, `link_candidates`).
        if ax * by - ay * bx != 0 or ax * bx + ay * by > 0:
            return f"a {spec.net} link to ({other[0]:g},{other[1]:g}) already leaves the anchor, and the two do not make one straight run"
    return ""


_QUERY_PAD_MM = 3.0
"""`tap._QUERY_PAD_MM` and `route_scene._MAX_NEED`'s number, for the same reason: every clearance
these boards compile is 0.0889 to 0.5 and the index answer is a superset either way."""


def _in_a_pad(scene: Scene, spec: StitchSpec, at: Pt) -> Clash | None:
    """`tap._in_a_pad`'s question, with **no exemption**: a via inside a pad of its own net.

    A.4 rule 1 skips a same-net pair on purpose, and `fab.via_in_pad_blockers` refuses the board
    anyway, because a via inside a passive's pad wicks the joint whatever net it is on. A tap exempts
    the one primitive it is welding — it has to, that is the connection it exists to make. A rung
    welds nothing: the twin's job is to sit beside a barrel, not on a pad, so every pad of its own
    net is asked at `via_to_same_net_smd_pad`, the number every via pcbc places keeps from a same-net
    SMD pad.

    **A `plane` barrel is asked the same question and for the same reason**, which is worth saying
    because a plane lattice's net is a poured net and most of its pads are the pour's: a ground pad is
    connected to the pour by the fill and by nothing this via adds, so a lattice barrel inside one
    buys no stitching and costs a solder joint. Only `thermal` is exempt, and only because the via
    inside the land **is** its technique (`run`).
    """
    need = scene.table.via_to_same_net_smd_pad()
    ring = via_shape(at, spec.via[0])
    box = (at[0], at[1], at[0], at[1])
    seen: set[int] = set()
    for layer in scene.layers:
        for i in scene.query(layer, box, need + spec.via[0] + _QUERY_PAD_MM):
            if i in seen:
                continue
            seen.add(i)
            it = scene.items[i]
            if it.kind != "pad" or it.net != spec.net or it.copper is None:
                continue
            if not clears(ring, it.copper, need):
                return Clash(it, gap(ring, it.copper), need, it.at(), "via_in_pad", "stackup.clearance_min (same-net pad)")
    return None


# --- the refusal ------------------------------------------------------------------------------------


def antipad_pitch(scene: Scene, net: str, dia: float) -> tuple[float, str]:
    """The closest two vias of `net` may sit without merging their antipads in a foreign pour.

    `route_scene.antipad_clash`'s own arithmetic, read as a number instead of as a verdict — for the
    **report only**, the way `gap()` is: the decision stays with `antipad_clash`, which asks it of
    the real zones and the real neighbours. What it is for is the sentence a refusal has to say when
    every site on the ring failed the same rule, because "there is no room" and "there is no room and
    here is the pitch that would have had room" are different messages.

    `2 * max(zone clearance, class clearance) + min_thickness + dia` — measured on node's 3V3 plane,
    `2 x 0.2 + 0.1 + 0.35` = **0.850 mm**, against a `VIA_PARALLEL_MM` of 1.0: a window 0.15 mm wide
    in which exactly one grid step falls. On two layers there is no inner plane and the only pour is
    the back one, whose antipad is cut at the same two clearances around a via twice the diameter, so
    the same expression comes out at or above the far limit and **the window is empty** — which is
    why this carrier has no two-layer population and why its refusal there names `Board(layers=4)`.
    """
    best = (0.0, "")
    for z in scene.zone_rules:
        if z.net == net or z.layer not in scene.layers or z.min_thickness <= 0.0:
            continue
        one = max(z.pad_clearance, scene.table.between(z.net, net)[0])
        v = round(2 * one + z.min_thickness + dia, 6)
        if v > best[0]:
            best = (v, f"the {z.net} plane on {z.layer}, whose min_thickness is {z.min_thickness:g} mm")
    return best


def _moves(ctx: PatternCtx, spec: StitchSpec, clash: Clash | None) -> str:
    """The edits, in the order an author should consider them. Every one ends in a `board.py` line.

    A tap's refusal moves the pad's **own** footprint, because a tap is one pad's business. A rung
    has no footprint: its anchor is a via the router placed, and the only parts that can move are the
    ones in the way. So the first edit names the blocker's owner, and where the blocker carries no
    footprint — a track — the edit is the one `tap._moves` uses for the same case, a `NetReq` that
    takes it off this layer.

    The last edit is always `amps=`, and it is the only one that changes the *requirement* rather
    than the room: `vias_per_change` is `ceil(amps / via_amps(drill))`, so a rail declared for a
    connector's rating rather than for its load asks for barrels it does not need. It is offered last
    because it is the edit that can be wrong for the right reason.
    """
    edits: list[str] = []
    pitch, why = antipad_pitch(ctx.scene, spec.net, spec.via[0])
    if pitch >= VIA_PARALLEL_MM - 1e-9:
        edits.append(
            f'Board(layers=4) is the only edit that opens a window at all: {why} needs {pitch:g} mm between '
            f"two {spec.via[0]:g} mm rings and a parallel via is one the measurement counts only within "
            f"{VIA_PARALLEL_MM:g} mm, so on this stackup there is no distance that is both"
        )
    if clash is not None:
        ref = clash.item.owner.split(".")[0]
        dx, dy = spec.at[0] - clash.at[0], spec.at[1] - clash.at[1]
        toward = ("right" if dx >= 0 else "left") if abs(dx) >= abs(dy) else ("down" if dy >= 0 else "up")
        if "." in clash.item.owner and clash.item.kind == "pad":
            edits.append(f'Place("{ref}", toward="{toward}") takes {clash.item.owner} off the ring')
        elif clash.item.net:
            edits.append(f'NetReq("{clash.item.net}", layers=["{_other(ctx)}"]) takes {clash.item.owner} off this layer')
        else:
            edits.append(f"move {clash.item.owner} itself, which carries no net and so cannot be routed away")
    c = ctx.cs.by_net(spec.net)
    amps = float(c.current.amps) if (c is not None and c.current is not None) else 0.0
    one = via_amps(spec.via[1], ctx.scene.stack.via_plating_mm, c.current.temp_rise_c if (c is not None and c.current is not None) else 10.0)
    edits.append(
        f'change amps= on the NetReq that already declares "{spec.net}": {amps:g} A over one '
        f"{spec.via[1]:g} mm barrel's {one:g} A is what asks for the second via"
    )
    return "Moves: " + "; or ".join(edits) + "."


def _other(ctx: PatternCtx) -> str:
    """The far outer layer, for a `NetReq(layers=...)` edit — `tap._other`'s answer."""
    layers = [L for L in ctx.scene.layers if L != "F.Cu"]
    return layers[-1] if layers else "B.Cu"


def _refuse(ctx: PatternCtx, spec: StitchSpec, got: int, clash: Clash | None, seen: list[Clash], tried: int, lane: str, merged: int, crowded: int) -> Refusal:
    """**One** refusal for the whole spec, worst blocker first, ending in a `board.py` edit.

    Soft, for `tap._refuse`'s reason taken one step further: a refused rung costs the board nothing
    it had before. The rail carried one barrel's worth before this pattern existed and it carries one
    barrel's worth after — `ampacity.power_moves` already prints the shortfall, and
    `build._barrel_gate` reports it again on the finished board. What a hard refusal here would do is
    stop a board that builds today on a fault whose repair the tool has just been unable to make,
    which is C.6's argument and `--strict-power`'s precedent. `--strict-patterns` still turns it into
    a stop for the author who wants one.

    **One line per blocker, that blocker's worst site, at most two** — `tap._refuse`'s shape, because
    the second blocker is usually the reason the first cannot simply be moved. The site count is
    stated as the two numbers that derive it, so a reader can check the window rather than trust it.
    """
    by_pair: dict[tuple[str, int], Clash] = {}
    for c in seen:
        key = (c.rule, c.item.id)
        if key not in by_pair or (c.need - c.have) > (by_pair[key].need - by_pair[key].have):
            by_pair[key] = c
    worst = sorted(by_pair.values(), key=lambda c: (-(c.need - c.have), c.item.id, c.rule))[:2]
    if worst:
        blockers = "worst first: " + "; ".join(
            f"{c.item.label()} at {c.at[0]:g},{c.at[1]:g} leaves {c.have:.3f} mm of the {c.need:.3f} mm {c.why} needs (rule: {c.rule})" for c in worst
        )
    elif merged:
        blockers = f"every site would pull a second {spec.net} via group into this one (rule: cluster_merge)"
    elif crowded:
        blockers = "every site left is too close to a rung this spec already placed (rule: crowded)"
    elif lane:
        blockers = f"every site {lane}, and a fanout lane may be crossed, not run along and not sat in (rule: lane)"
    else:
        blockers = "no site is even a link segment long (rule: no candidate)"
    pitch, _why = antipad_pitch(ctx.scene, spec.net, spec.via[0])
    near = ctx.scene.table.via_pitch(spec.net, spec.net, spec.via[1], spec.via[0], spec.via[1], spec.via[0])
    blockers += (
        f"\n  Tried {tried} of {spec.bound} sites (8 directions, {spec.bound // 8} pitches {ctx.scene.grid:g} mm apart "
        f"from {near:g} mm to {VIA_PARALLEL_MM:g} mm), placed {got} of {spec.need}"
    )
    if pitch:
        blockers += f"; two {spec.via[0]:g} mm rings need {pitch:g} mm not to merge their antipads in {_why}"
    rule = clash.rule if clash is not None else ("cluster_merge" if merged else ("crowded" if crowded else ("lane" if lane else "no candidate")))
    return Refusal(
        pattern=REASON,
        net=spec.net,
        what=spec.owner,
        clash=clash,
        rule=rule,
        hard=False,
        move="stitch "
        + move_line(
            spec.net,
            spec.owner,
            f"{spec.need} more {spec.via[0]:g}/{spec.via[1]:g} barrel(s) within {VIA_PARALLEL_MM:g} mm of it ({spec.why})",
            blockers,
            _moves(ctx, spec, clash),
            sep="\n  ",
        ),
    )
