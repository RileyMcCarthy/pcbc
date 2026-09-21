"""`stitch` — copper whose absence leaves nothing unconnected (`docs/stitch-plan.md` R-S1, S4).

Every other pattern in this package connects something: a hop joins two pads, a spine feeds a rail,
a tap welds a pad to its plane. Take any of their copper away and a pad is unreached. Take a stitch
away and the board still works — worse, but connected. That difference is the whole reason this
module can run in the `final` stage, **after every KRT step**, where nothing can react to it: a
stitch that loses its site costs a shield or a barrel, not a pad, so no router step has to follow it
and `route.krt_plan` schedules none (`patterns.FINAL`).

**One carrier ships in this slice: `parallel`.** A power rail that changes layer through a single
barrel is limited by that barrel, and a barrel's rating is a **count**, not a width — `via_amps` is
the drill and the plating, both the fab's, so no `Place()` and no shorter path moves another ampere
through it. `stackup.vias_per_change` has compiled the count since R1 and until this module nothing
placed the second via. `sites_lattice` (thermal, S6) and `sites_along` (guard, S7) are the other two
carriers `StitchSpec` is shaped for; neither is built here.

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

from ..ampacity import VIA_PARALLEL_MM
from ..blocking import move_line
from ..route_emit import Piece, seg_piece, via_piece
from ..route_geom import EPS_MM, MICRO_MM, Pt, clears, gap, q, via_shape
from ..route_scene import Clash, Scene, antipad_clash, blocked
from ..sexp import stable_uuid
from ..stackup import via_amps, vias_per_change
from . import PatternCtx, PatternResult, Refusal, _mm, _nm, lane_ok

REASON = "stitch"

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
`PatternPlan.done` — which `krt_plan` drops nets from and `signals` writes `!NET` for — say something
false about a net no pattern routed. A **parallel** rung is on its own net and still connects no pad
to anything: the anchor was already joined to the rail and the twin is a second path beside it, so
"this net is now done" would be a claim about pad connectivity that the copper does not make.
`net_open` would answer the same before and after, which is exactly the sense in which a rung is
redundant copper (R-S1).

Read as an attribute by `pattern_copper`, never with a `getattr` default: see `hop.CONNECTS`."""

CARRIERS = ("parallel",)
"""The carriers this module ships. `StitchSpec.carrier` is shaped for five (`docs/stitch-plan.md`
§1.3) and three write copper; `thermal` is S6's and `guard` is S7's, and neither has a site generator
here. A spec naming one of them is a programming error rather than a refusal — `run` raises — because
a carrier that is not built has no candidate list at all, and silently emitting nothing is how a
pattern comes to pass its own tests by doing nothing."""

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
    unit: str  # "A" | "K/W" | "mm"
    floor_ok: bool  # is `measure` still above the floor the spec must not go under?
    why: str

    def line(self) -> str:
        """The one `style:` note a partial spec emits. One line, never one per missing site."""
        verdict = "above its floor" if self.floor_ok else "under its floor"
        return (
            f"style: stitch {self.why}: {self.got} of {self.want} placed, "
            f"{self.measure:g} {self.unit} of {self.target:g} {self.unit} ({verdict})"
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


def _site_name(spec: StitchSpec, at: Pt) -> str:
    """Which candidate this is, for the report: `"right 0.9"`. Derived from the two points rather
    than carried alongside them, so the candidate list stays a plain tuple of `Pt`."""
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


def specs(ctx: PatternCtx) -> tuple[StitchSpec, ...]:
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
    difference is not cosmetic at this stage and it is the largest single number in the slice:
    measured on node at `patterns_final`, the two plane zones are **written but not yet filled**, so
    `poured_planes` returns `()` and using it would have handed this pattern 13 `3V3` groups and 30
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
    net, reason = spec.net, REASON
    uid = (ctx.board, "pcbc", reason, net, f"{at[0]:.6f}", f"{at[1]:.6f}")
    out: list[Piece] = [via_piece(net, reason, at, spec.via[0], spec.via[1], owner=spec.owner, uuid=stable_uuid(*uid, "via"))]
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
    scene = ctx.scene
    placed: list[Pt] = []
    pieces: list[Piece] = []
    seen: list[Clash] = []
    worst: tuple[float, Clash] | None = None
    tried = 0
    lane_why = ""
    merged = 0
    crowded = 0
    for at in spec.sites:
        if len(placed) == spec.need:
            break
        cand = _pieces(ctx, spec, at)
        if cand[1].mm < MICRO_MM:
            continue  # a link KiCad's own `track_segment_length` rule would count; the ring is wider
        tried += 1
        if _merges(scene, spec, at):
            merged += 1
            continue
        why = _crowds(ctx, spec, at, placed)
        if why:
            crowded += 1
            continue
        clash = _in_a_pad(scene, spec, at) or blocked(scene, cand, spec.net) or antipad_clash(scene, at, spec.via[0], spec.net)
        if clash is not None:
            seen.append(clash)
            if worst is None or (clash.need - clash.have) > worst[0]:
                worst = (clash.need - clash.have, clash)
            continue
        fits, lane = lane_ok(scene, cand, ())
        if not fits:
            lane_why = lane_why or lane
            continue
        placed.append(at)
        pieces.extend(cand)
    got = len(placed)
    if got == spec.need:
        return PatternResult(
            reason=REASON,
            net=spec.net,
            pieces=tuple(pieces),
            candidate=", ".join(_site_name(spec, at) for at in placed),
            tried=tried,
        )
    if not spec.required:
        # R-S3's other half: a target applies partially and reports the shortfall, in **one** note.
        return PatternResult(reason=REASON, net=spec.net, pieces=tuple(pieces), tried=tried, notes=(_cover(ctx, spec, got).line(),))
    return PatternResult(
        reason=REASON,
        net=spec.net,
        refusal=_refuse(ctx, spec, got, worst[1] if worst else None, seen, tried, lane_why, merged, crowded),
        tried=tried,
    )


def _cover(ctx: PatternCtx, spec: StitchSpec, got: int) -> Coverage:
    """What a partial `parallel` spec achieved, in amperes — the unit the shortfall was stated in."""
    c = ctx.cs.by_net(spec.net)
    dt = c.current.temp_rise_c if (c is not None and c.current is not None) else 10.0
    amps = float(c.current.amps) if (c is not None and c.current is not None) else 0.0
    one = via_amps(spec.via[1], ctx.scene.stack.via_plating_mm, dt)
    have = round(one * (got + 1), 3)  # the anchor plus the rungs that fitted
    return Coverage(want=spec.need, got=got, measure=have, target=round(amps, 3), unit="A", floor_ok=have + 1e-9 >= amps, why=spec.owner)


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
    net is asked at `via_to_same_net_smd_pad`, which is the number `route.py` hands KRT as
    `--same-net-pad-clearance` on every step that may place a via.
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
