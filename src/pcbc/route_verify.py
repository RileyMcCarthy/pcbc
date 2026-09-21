"""The exact-geometry self-check: pcbc's own verdict on pcbc's own copper (`docs/r2-design.md` D.1).

Runs unconditionally at the end of each pattern stage, and a non-empty result **raises** rather than
writes. It is not a nicety — it is the reason the gate can be expected to find nothing. KiCad is
still the arbiter (D.6); this is a stricter pre-filter that runs in milliseconds, without KiCad, on
every board, so a pattern that emits bad copper fails in the slice that wrote it rather than in a DRC
report three stages later.

It re-asks every question the patterns already asked, in one pass over the finished copper, because
an ordering bug in the incremental checks cannot hide from a pass that does not depend on the order.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

from .ampacity import VIA_PARALLEL_MM
from .constraints import ConstraintSet
from .route_emit import Piece
from .route_geom import MICRO_MM, Box, Pt, Shape, aabb, clears, clip_len_in_box, hull_dist2, is_octilinear, legs_ok, q, seg_lengths, track_shape, turn_ok, via_shape
from .route_scene import Item, Scene, clashes
from .route_scene import _vias as board_vias
from .sexp import matching_paren
from .stackup import via_amps, vias_per_change

__all__ = [
    "CHAIN_BISECT",
    "COUPLING_CELL_MM",
    "POUR_CELL_MM",
    "RETURN_MM",
    "BridgeTie",
    "ChainVerdict",
    "Parallel",
    "PourRaster",
    "ReturnVia",
    "Zone",
    "bridge_lines",
    "bridge_ties",
    "chain_order",
    "in_zone",
    "lane_overrun",
    "via_in_lane",
    "parallel_lines",
    "parallel_joined",
    "parallel_joined_lines",
    "paths_of",
    "plane_checks",
    "plane_islands",
    "plane_area",
    "poly_area",
    "guard_cover",
    "guard_lines",
    "poured_planes",
    "referenced_nets",
    "pour_raster",
    "return_lines",
    "plane_stitch",
    "plane_stitch_lines",
    "return_vias",
    "verify_copper",
    "via_parallelism",
    "zones",
]

BRANCH_REASONS = ("fanout", "guard", "plane", "stitch", "thermal")
"""A pattern via that is a **branch** uses the fab's standard via (`stack.via_diameter`); one that
carries the net's current across layers uses the class via (`Constraint.via`). B.0's rule, and the
argument is that a branch carries one pad's share — a decoupling cap's ripple, not the rail's 1 A —
while `ViaSpec.per_change` applies where the *trunk* changes layer, which R2 never makes it do (a
spine refuses instead).

`"stitch"` is here as the **fallback** and not as the rule (`docs/stitch-plan.md` §2s). A parallel
rung copies its **anchor's** size — the via KRT already placed, which on node is 0.35/0.2 while the
`Power` class via is 0.8/0.4, and at the class ring two vias need 1.080 mm between centres against an
`ampacity.VIA_PARALLEL_MM` of 1.0, so a class-size partner cannot be placed at all — and `_via_rules`
reads that anchor off the board (`_anchor_via`). What this entry decides is the case where there is
**no** anchor within reach, which is itself a fault `parallel_joined` reports: the via is then
measured against the fab's standard one, because a lone via on a power net is a branch and not a
trunk changing layers, and holding it to a class via nothing asked for would be a second wrong
answer on top of the first.

`"plane"` is one of these for `"guard"`'s reason at the other end of the same argument. A lattice
barrel ties two pours of **one** net and carries no net current across layers — it is the shortest
possible branch, a via whose two ends are the same conductor — and the net it is on is a `Power`
class on every board that pours one, so without the entry it would be stitched with 0.6/0.3 or
0.8/0.4 class barrels. That is not merely oversized: a bigger ring cuts a bigger antipad in the very
plane the lattice exists to tie, and `route_verify.plane_area` measures the difference
(pi*(dia/2 + clearance)^2 per via per crossed plane).

`"guard"` is one of these because a stitch via **is** a branch in the exact sense B.0 means: it
carries no net current across layers at all, it ties a shield to the pour beside it. Without the
entry the fallback below reads `Constraint.via` for the guard's *ground* net, which is a `Power` class
on every board that pours one — 0.6/0.3 on `jlcpcb_2l_1oz` against the fab's 0.5/0.3 — so a guard
would be stitched with power-class barrels for no reason but the name of the net it is made of
(measured on `tests/fixtures/guard/guard.py`, 2026-09-21).

The `tap` is no longer one of these, and H.1 is why: B.0 said "a branch takes the fab's standard via,
full stop" and recorded it as the decision its author was least sure of. The answer is the arithmetic
rather than the rule of thumb — the smallest via whose current rating covers one pad's share, capped
at the net's class via — and it lives in `patterns/tap.py::tap_via` so the pattern and this check
cannot drift apart (A.3's one-source discipline, applied to the via table)."""


def paths_of(pieces: Sequence[Piece]) -> tuple[tuple[Pt, ...], ...]:
    """The segments re-assembled into the runs of copper they are, so the turn rule can be asked.

    Grouped by (net, reason, layer) and walked from every endpoint that is not a plain pass-through;
    a vertex where three pieces meet is a junction and ends the chains that reach it, which is what a
    chain, a spine rib or a tap stub looks like. Sorted throughout, so the answer is a function of the
    copper and not of the order it happened to be emitted in.
    """
    out: list[tuple[Pt, ...]] = []
    groups: dict[tuple[str, str, str], list[Piece]] = {}
    for p in pieces:
        if p.kind != "seg":
            continue
        groups.setdefault((p.net, p.reason, str(p.layer)), []).append(p)
    for key in sorted(groups):
        adj: dict[Pt, list[Pt]] = {}
        for p in sorted(groups[key], key=lambda p: (p.a, p.b)):
            adj.setdefault(p.a, []).append(p.b)
            adj.setdefault(p.b, []).append(p.a)
        seen: set[tuple[Pt, Pt]] = set()
        starts = [pt for pt in sorted(adj) if len(adj[pt]) != 2] or sorted(adj)[:1]
        for start in starts:
            for first in sorted(adj[start]):
                if (start, first) in seen:
                    continue
                chain = [start, first]
                seen.add((start, first))
                seen.add((first, start))
                while len(adj[chain[-1]]) == 2:
                    nxt = [n for n in adj[chain[-1]] if n != chain[-2]]
                    if not nxt or (chain[-1], nxt[0]) in seen:
                        break
                    seen.add((chain[-1], nxt[0]))
                    seen.add((nxt[0], chain[-1]))
                    chain.append(nxt[0])
                out.append(tuple(chain))
    return tuple(out)


def _exact(v: float) -> bool:
    """Is this coordinate an exact multiple of a nanometre, and does it survive the round trip
    through the six decimals KiCad writes and re-reads? Emit -> parse -> emit is a fixed point."""
    return q(v) == v and float(f"{v:.6f}") == v


def verify_copper(scene: Scene, pieces: Sequence[Piece], cs: ConstraintSet, *, ids: Sequence[int] = ()) -> list[str]:
    """Every complaint D.1 can make about this copper, as lines. Empty means the copper is sound.

    `ids` is the scene item id of each piece, in the same order, so a piece is not checked against
    itself: the stage adds each pattern's copper to the scene before the next pattern runs, so by the
    time this is asked every piece is already an obstacle. Without it every piece clashes with its
    own twin at zero distance, and the check that is supposed to find real errors finds only itself.
    """
    out: list[str] = []
    served: dict[tuple[str, str], frozenset[str]] = {}
    for p in pieces:
        key = (p.net, p.reason)
        if key not in served:
            served[key] = served_refs(scene, pieces, p.net, p.reason)
    for n, p in enumerate(pieces):
        ignore = frozenset({ids[n]}) if n < len(ids) else frozenset()
        for c in clashes(scene, [p], p.net, ignore=ignore):
            if c.rule in ("mask",):
                continue  # A.4 rule 4 is advisory in R2: a note, never a refusal
            out.append(f"{p.reason} {p.net}: {_where(p)} breaks {c.rule} against {c.item.label()} ({c.have:g} mm of {c.need:g} mm, {c.why})")
    for path in paths_of(pieces):
        if not is_octilinear(path):
            out.append(f"not 0/45/90: {_pts(path)}")
        elif not turn_ok(path):
            out.append(f"a turn sharper than 135 degrees: {_pts(path)}")
        if not legs_ok(path):
            short = [f"{L:.4f}" for L in seg_lengths(path) if L < MICRO_MM]
            out.append(f"a leg under {MICRO_MM:g} mm ({', '.join(short)}): {_pts(path)}")
    for p in pieces:
        for v in (p.a[0], p.a[1]) + ((p.b[0], p.b[1]) if p.b else ()):
            if not _exact(v):
                out.append(f"{p.reason} {p.net}: {v!r} is not a whole number of nanometres")
        if p.kind == "via":
            out += _via_rules(scene, cs, p, pieces)
            who = via_in_lane(scene, p.a, p.w, served[(p.net, p.reason)] | {p.owner.split(".")[0]})
            if who:
                out.append(f"{p.reason} {p.net}: a via inside {who}, which is a fanout lane (A.7)")
        else:
            run, who = lane_overrun(scene, p.a, p.b, p.w, served[(p.net, p.reason)] | {p.owner.split(".")[0]})
            if who:
                out.append(f"{p.reason} {p.net}: runs {run:g} mm along {who}, which is a fanout lane (A.7)")
    return out


def served_refs(scene: Scene, pieces: Sequence[Piece], net: str, reason: str) -> frozenset[str]:
    """The footprints this run of copper actually **serves**: the ones whose pads it lands on.

    A fanout lane exists so that footprint's own pads can escape, so copper that serves one of those
    pads is what the lane is for — which is the question `route_checks.check_corridors` already asks
    ("unless the net owns an escape in it"). Asking it per piece instead of per run reports the middle
    leg of a two-pad link for running along the lane of the very footprint it is running to.

    Per (net, reason), not per connected run: a net with two disjoint runs of one reason exempts both
    their footprints for both. That is looser than the question the pattern asks itself (`lane_ok`,
    which is handed one link's two terminals), so this check can only fail to catch something the
    pattern already refused — never invent one. Tightening it is a matter of threading the run
    through, and it waits for a pattern that writes disjoint runs on one net (`chain`, S6).
    """
    refs: set[str] = set()
    for p in pieces:
        if p.net != net or p.reason != reason:
            continue
        for pt in (p.a, p.b):
            if pt is None:
                continue
            layer = p.layer if isinstance(p.layer, str) else p.layer[0]
            for i in scene.query(layer, (pt[0], pt[1], pt[0], pt[1]), 0.0):
                it = scene.items[i]
                b = it.box()
                if it.kind == "pad" and it.net == net and b[0] <= pt[0] <= b[2] and b[1] <= pt[1] <= b[3]:
                    refs.add(it.owner.split(".")[0])
    return frozenset(refs)


def lane_budget(scene: Scene, lane: Item, w: float) -> float:
    """`lane_width + w + 2 * grid` (A.7). Long enough for any crossing, short enough that a run along
    the lane is one: forbidding a crossing outright would cut c3_usb's board in half along `J1`'s
    1.4515 mm lane, while running along one is what walled the DS2 Addon's AVDD, DVDD and UART pins
    in (`docs/copper-plan.md` line 191)."""
    b = lane.box()
    return min(b[2] - b[0], b[3] - b[1]) + w + 2 * scene.grid


def lane_overrun(scene: Scene, a: Pt, b: Pt, w: float, exempt: frozenset[str]) -> tuple[float, str]:
    """The worst run this segment makes **along** a fanout lane that is none of `exempt`'s, and whose
    lane it is; `(0.0, "")` when every lane it touches it merely crosses.

    `exempt` is a set of footprint refs, not one ref: a link runs between two pads and both their
    footprints own their own lanes, so asking the question one owner at a time reports a stub for
    running along the very lane its own pad sits in.
    """
    worst, who = 0.0, ""
    for it in sorted(scene.items, key=lambda i: i.id):
        if it.kind != "lane" or it.copper is None or it.owner.split(" ")[0] in exempt:
            continue
        run = clip_len_in_box(a, b, aabb(it.copper))
        if run > lane_budget(scene, it, w) and run > worst:
            worst, who = run, it.owner
    return (round(worst, 4), who)


def via_in_lane(scene: Scene, at: Pt, dia: float, exempt: frozenset[str]) -> str:
    """Whose fanout lane this via's copper sits in, or `""`.

    A segment may **cross** a lane and may not run along one (A.7), and `lane_overrun` measures the
    run. A via has no run to measure: it occupies, so any part of a lane it touches is a lane that
    footprint's own pads can no longer escape through. Until S5's review neither `lane_ok` nor D.1
    asked a via anything — both looped `if p.kind != "seg": continue` — so half of every tap was
    exempt from the rule the DS2 Addon's walled-in AVDD, DVDD and UART pins bought
    (`docs/copper-plan.md`, and `docs/r2-measurements.md` S5r, finding 6: ds2's `C5` tap via sat
    0.1221 mm inside `U1`'s top lane, in `U1.11`'s own escape column).
    """
    ring = via_shape(at, dia)
    for it in sorted(scene.items, key=lambda i: i.id):
        if it.kind != "lane" or it.copper is None or it.owner.split(" ")[0] in exempt:
            continue
        if not clears(ring, it.copper, 0.0):
            return it.owner
    return ""


def _via_rules(scene: Scene, cs: ConstraintSet, p: Piece, pieces_on_net: Sequence[Piece] = ()) -> list[str]:
    """D.1 items 5, 6 and 8: the branch rule, the nets that may carry no via at all, and the layers."""
    out: list[str] = []
    if tuple(p.layer) != ("F.Cu", "B.Cu"):
        out.append(f"{p.reason} {p.net}: a via on {tuple(p.layer)}; no example turns blind and buried vias on (B.0)")
    c = cs.by_net(p.net)
    if c is not None and not c.via.allowed:
        out.append(f"{p.reason} {p.net}: a via on a net whose NetReq forbids vias")
    if c is not None and c.reference:
        out.append(f"{p.reason} {p.net}: a via on a controlled-impedance net (reference {c.reference}); R2 never changes layers on one")
    if p.reason == "tap":
        from .patterns.tap import tap_via

        # The divisor is the vias actually emitted on this net, not the pads the pattern set out to
        # tap: a refused pad is one fewer via sharing the current, so this is the share as built. It
        # can only be larger than the share the pattern sized against, so a via that passes here
        # would have passed there too — the check is the stricter reading of its own rule.
        n = sum(1 for q_ in pieces_on_net if q_.kind == "via" and q_.reason == "tap" and q_.net == p.net)
        want, why, _share = tap_via(scene, cs, p.net, max(n, 1))
    elif p.reason == "stitch" and (anchored := _anchor_via(scene, p, pieces_on_net)) is not None:
        want, why = anchored
    elif p.reason in BRANCH_REASONS:
        want = (scene.stack.via_diameter, scene.stack.via_drill)
        why = "the fab's standard via, because a branch carries one pad's share"
    elif c is not None:
        want = (c.via.diameter_mm, c.via.drill_mm)
        why = f"the {c.class_name} class via, because this one carries the net across layers"
    else:
        want = (scene.stack.via_diameter, scene.stack.via_drill)
        why = "the fab's standard via"
    if (p.w, p.drill) != want:
        out.append(f"{p.reason} {p.net}: a {p.w:g}/{p.drill:g} via where B.0's branch rule says {want[0]:g}/{want[1]:g} ({why})")
    return out


def _anchor_via(scene: Scene, p: Piece, pieces_on_net: Sequence[Piece] = ()) -> tuple[tuple[float, float], str] | None:
    """The via a parallel rung is the twin of, and the size it therefore has to be.

    `docs/stitch-plan.md` §2(f): **one via must mean one thing**. `ampacity.net_nodes` hands every
    member of a cluster `n x via_amps(that member's own drill)`, so a mixed cluster reports the whole
    group at whichever barrel the walk happens to read — and the rung is the one via in it whose size
    pcbc chooses, so it is the one that must not differ.

    The anchor is read off the **scene** rather than taken from the spec, for the reason `tap`'s own
    branch is: a check handed the same numbers the pattern used cannot catch the pattern using the
    wrong ones. It is the nearest via of this net within `ampacity.VIA_PARALLEL_MM` that this run did
    not itself write — every via inside that radius is in the same single-linkage cluster by
    construction, and excluding the pattern's own rungs means a second rung is measured against the
    barrel KRT placed and not against the first rung. `None` when there is none, which hands the
    answer to `BRANCH_REASONS`.
    """
    mine = {(v.a[0], v.a[1]) for v in pieces_on_net if v.kind == "via" and v.reason == p.reason and v.net == p.net}
    best: tuple[float, float, float] | None = None
    for it in scene.items:
        if it.kind != "via" or it.net != p.net or it.copper is None or it.hole is None:
            continue
        at = it.at()
        if at in mine:
            continue
        d = math.dist(at, p.a)
        if d > VIA_PARALLEL_MM or d <= 0.0:
            continue
        if best is None or d < best[0]:
            best = (d, round(it.copper.r * 2.0, 4), round(it.hole.r * 2.0, 4))
    if best is None:
        return None
    return ((best[1], best[2]), f"the anchor's own size: the {p.net} via {best[0]:.4g} mm away that this one is parallel to")


def _where(p: Piece) -> str:
    if p.kind == "via":
        return f"via at ({p.a[0]:g},{p.a[1]:g})"
    return f"{p.layer} ({p.a[0]:g},{p.a[1]:g})-({p.b[0]:g},{p.b[1]:g})"


def _pts(path: Sequence[Pt]) -> str:
    return " -> ".join(f"({x:g},{y:g})" for x, y in path)


# --- D.5: the plane, the islands and the pour -----------------------------------------------------
#
# A pour is not an obstacle (A.4: KiCad's fill retreats from foreign copper, so a track over a pour
# is legal) and that is exactly why it needs its own checks. What a tap can get wrong is not
# clearance, which `clashes` already judges, but **connection**: a via that lands where the fill
# retreats connects nothing, and a fragment the fill cannot reach is deleted outright under
# `island_removal_mode 0`, which turns a pad whose only connection was that fragment into an
# unconnected item the gate fails on.
#
# The two halves run at different times, and that is deliberate. On four layers KRT pours before the
# post stage, so the plane is on the board and `plane_islands` measures the arbiter's own answer
# after the gate refills. On two layers the pour is written near the end, after the signals, so
# there is nothing to measure when the tap is placed and `pour_raster` predicts instead.


@dataclass(frozen=True)
class Zone:
    """One `(zone ...)` as the copper it actually became: its filled polygons, in file order.

    KiCad writes one `(filled_polygon ...)` per **island**, so `len(polys)` is the island count — the
    number D.5 watches, because a plane that was one island and is now two has had a fragment cut
    off by something, and with `island_removal_mode 0` the fragment is deleted rather than kept.
    """

    net: str
    layer: str
    polys: tuple[tuple[Pt, ...], ...]

    def islands(self) -> int:
        return len(self.polys)


_XY = re.compile(r"\(xy ([-0-9.]+) ([-0-9.]+)\)")
_ZONE_NET = re.compile(r'\(net(?:_name)?\s+"([^"]*)"\)')
_ZONE_LAYER = re.compile(r'\(layers?\s+"([^"]+)"')


def zones(text: str) -> tuple[Zone, ...]:
    """Every filled zone on the board, in file order. A zone with no `filled_polygon` is a keepout or
    an unfilled pour and comes back with no polygons, which is itself the answer to "did the gate
    judge a filled board" (`test_examples_fab.py` asserts `filled_polygon` is in the text)."""
    out: list[Zone] = []
    for m in re.finditer(r"\n\t\(zone\b", text):
        end = matching_paren(text, m.start() + 2)
        block = text[m.start() : end + 1]
        head = block.split("(filled_polygon", 1)[0]
        nm = _ZONE_NET.search(head)
        lm = _ZONE_LAYER.search(head)
        polys: list[tuple[Pt, ...]] = []
        for f in re.finditer(r"\(filled_polygon\b", block):
            fend = matching_paren(block, f.start())
            polys.append(tuple((float(a), float(b)) for a, b in _XY.findall(block[f.start() : fend + 1])))
        out.append(Zone(nm.group(1) if nm else "", lm.group(1) if lm else "", tuple(polys)))
    return tuple(out)


def plane_islands(text: str) -> dict[tuple[str, str], int]:
    """`{(net, layer): island count}` for every filled zone, so a slice can pin the number.

    Measured on node before any tap: In1 (GND) and In2 (3V3) are one island each. The check is run
    **once per plane layer a via crosses**, not once for the tapped net: every R2 via is a through
    via, so a GND tap punches a clearance hole in the 3V3 plane on In2.Cu as well as landing in the
    GND plane on In1.Cu, and it is the foreign plane that a tap can quietly cut in two (D.5).
    """
    out: dict[tuple[str, str], int] = {}
    for z in zones(text):
        if not z.polys:
            continue
        key = (z.net, z.layer)
        out[key] = out.get(key, 0) + z.islands()
    return out


def in_zone(z: Zone, at: Pt) -> bool:
    """Is this point inside the zone's filled copper? Ray casting, per island.

    KiCad writes an island with zero-width seams where the fill wraps around an obstacle, and ray
    casting reads a seam as what it is — copper on both sides — so a point in the copper is inside.
    """
    for poly in z.polys:
        if _in_poly(poly, at):
            return True
    return False


def _in_poly(poly: Sequence[Pt], at: Pt) -> bool:
    x, y = at
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            t = (y - y0) / (y1 - y0)
            if x < x0 + t * (x1 - x0):
                inside = not inside
    return inside


RING_SAMPLES = 16
"""How many points of a tap via's ring `plane_checks` asks about, besides its centre.

The check used to ask the **centre** and nothing else, and the via's diameter never entered it — so
a via sitting half out of the pour, welded by a sliver of annulus or by nothing, passed (finding 12).
Sixteen points is 22.5 degrees apart: a retreat of the fill that misses all of them and the centre is
a notch narrower than `r * (1 - cos(11.25 deg))`, 0.3 % of the ring. Measured on the S5 boards, all
62 node taps, all 34 c3_usb taps and both ds2 taps are covered at every one of them."""


def plane_checks(text: str, pieces: Sequence[Piece]) -> list[str]:
    """D.5's first half, asked of a filled board: every tap via lands in its own net's plane.

    A via that clears every rule and sits where the fill retreated is copper that connects nothing,
    and the gate reports it three stages later as an unconnected pad rather than as the tap it was.
    Here it is one sentence naming the pad.

    Two things this asks that it did not before S5's review: the **ring** and not just the centre
    (finding 12), and the **absence** of the plane and not only a via outside one — a tap pattern
    that ran at all implies the plane was supposed to exist, so a net whose zone came back unfilled
    is a sentence rather than a `continue` (finding 17).
    """
    out: list[str] = []
    zs = [z for z in zones(text) if z.polys]
    for p in pieces:
        if p.kind != "via" or p.reason != "tap":
            continue
        # Every R2 via is a through via, so it crosses every copper layer: the zone to land in is
        # any filled zone of its own net, whichever layer that zone is on.
        mine = [z for z in zs if z.net == p.net]
        if not mine:
            out.append(f"tap {p.net}: {p.net} has no filled zone on this board, so the via at ({p.a[0]:g},{p.a[1]:g}) for {p.owner} welds nothing")
            continue
        r = (p.w or 0.0) / 2.0
        ring = [p.a] + [(p.a[0] + r * math.cos(2 * math.pi * k / RING_SAMPLES), p.a[1] + r * math.sin(2 * math.pi * k / RING_SAMPLES)) for k in range(RING_SAMPLES)] if r > 0 else [p.a]
        held = sum(1 for pt in ring if any(in_zone(z, pt) for z in mine))
        if held < len(ring):
            where = ", ".join(sorted(z.layer for z in mine))
            out.append(f"tap {p.net}: the via at ({p.a[0]:g},{p.a[1]:g}) for {p.owner} has {held}/{len(ring)} of its ring inside the {p.net} plane on {where}")
    return out


def poly_area(poly: Sequence[Pt]) -> float:
    """The shoelace area of one filled polygon, in mm2, always positive."""
    a = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def plane_area(text: str) -> dict[tuple[str, str], float]:
    """`{(net, layer): filled copper in mm2}`, rounded to 2 dp, for every filled zone.

    The island **count** cannot see the failure D.5 is documented to watch, and that is arithmetic,
    not an oversight: with `island_removal_mode 0` a cut-off fragment is *deleted*, so it is never
    written as a second `filled_polygon` and the count stays at 1. The count only rises when both
    halves keep a live connection, which is the rarer case. Measured: ds2's shipped board silently
    drops five orphan GND islands totalling 8.52 mm2 and `plane_islands` reports 1; a synthetic wall
    of 38 vias across node's corner deletes 69.44 mm2 of In1.Cu and it still reports 1 (finding 9).

    So the number to pin is the **area**. A fragment that disappears moves it; the count does not.
    """
    out: dict[tuple[str, str], float] = {}
    for z in zones(text):
        if not z.polys:
            continue
        key = (z.net, z.layer)
        out[key] = round(out.get(key, 0.0) + sum(poly_area(poly) for poly in z.polys), 2)
    return out


POUR_CELL_MM = 0.2
"""The cell the pour's reach is rastered at, in mm.

D.5 says `scene.grid`, which is 0.05 on a two-layer board: c3_usb is 45 x 32 mm, so 576 000 cells,
against a whole-stage budget of 2 s (F.3 item 8). At 0.2 mm the same board is 36 000 cells and the
raster costs about a tenth of a second. The cell is only allowed to be coarse in one direction: a
cell counts as blocked when an obstacle touches **any** part of it, so a coarser cell blocks more,
the flooded region is a subset of the true one, and the error refuses a tap that would have worked
rather than accepting one that would not. It is also still four times finer than the 0.5 mm
`hole_to_hole` that decides whether two taps can sit side by side at all."""


@dataclass(frozen=True)
class PourRaster:
    """Where a pour that does not exist yet will be able to flood.

    Two-layer boards only, and the reason is the order: `krt_plan` writes the back pour as
    `gnd_pour`, after the signals, so a tap placed by the post stage is placed into a board whose
    pour has not been poured. The alternative to predicting is discovering, and the thing discovered
    is an unconnected item in the gate (`docs/router-plan.md` section 9 records one already).
    """

    cell: float
    x0: float
    y0: float
    nx: int
    ny: int
    free: frozenset[int]

    def reaches(self, at: Pt, radius: float) -> bool:
        """Can the pour touch a via of this radius here? True when a flooded cell lies against it."""
        r = radius + 2 * self.cell
        i0 = max(int(math.floor((at[0] - r - self.x0) / self.cell)), 0)
        i1 = min(int(math.floor((at[0] + r - self.x0) / self.cell)), self.nx - 1)
        j0 = max(int(math.floor((at[1] - r - self.y0) / self.cell)), 0)
        j1 = min(int(math.floor((at[1] + r - self.y0) / self.cell)), self.ny - 1)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                if i + j * self.nx in self.free:
                    return True
        return False


def pour_raster(scene: Scene, net: str, layer: str, *, cell: float = POUR_CELL_MM) -> PourRaster:
    """The largest region a pour on `layer` for `net` can flood, given the copper already down.

    One pass: every foreign item on that layer is dilated by what the pour owes it — the clearance
    table's own number for copper, `hole_to_copper` for a drill — every cell it touches is blocked,
    and the free cells are labelled 4-connected. The pour is the **largest** free region, which is
    what KRT's own pour does with `island_removal_mode 0` behind it.

    Cached on the scene per (net, layer), because the tap pattern asks it once per pad and the only
    copper added between two pads is a tap: a tap is on the pour's own net, so it is not one of the
    obstacles this raster is built from, and the answer does not move under it.
    """
    cache = getattr(scene, "_pour_cache", None)
    if cache is None:
        cache = {}
        setattr(scene, "_pour_cache", cache)
    key = (net, layer, round(cell, 6))
    if key in cache:
        return cache[key]
    x0, y0, x1, y1 = scene.outline
    nx = max(int(math.ceil((x1 - x0) / cell)), 1)
    ny = max(int(math.ceil((y1 - y0) / cell)), 1)
    blocked_cells = bytearray(nx * ny)
    for it in scene.items:
        if it.kind in ("lane", "edge") or layer not in it.layers:
            continue
        if it.net and it.net == net and it.kind != "hole":
            continue  # the pour's own net is what the pour connects to, not what it retreats from
        for shape, need in ((it.copper, scene.table.between(net, it.net)[0]), (it.hole, scene.table.hole_to_copper())):
            if shape is None:
                continue
            bx = aabb(shape)
            _mark(blocked_cells, nx, ny, x0, y0, cell, (bx[0] - need, bx[1] - need, bx[2] + need, bx[3] + need))
    cache[key] = _largest_region(blocked_cells, nx, ny, cell, x0, y0)
    return cache[key]


def _mark(blocked_cells: bytearray, nx: int, ny: int, x0: float, y0: float, cell: float, box: Box) -> None:
    """Every cell the box touches, not every cell whose centre it covers: a cell is blocked when any
    of it is, which is the direction that refuses rather than accepts (`POUR_CELL_MM`)."""
    i0 = max(int(math.floor((box[0] - x0) / cell)), 0)
    i1 = min(int(math.floor((box[2] - x0) / cell)), nx - 1)
    j0 = max(int(math.floor((box[1] - y0) / cell)), 0)
    j1 = min(int(math.floor((box[3] - y0) / cell)), ny - 1)
    for j in range(j0, j1 + 1):
        row = j * nx
        for i in range(i0, i1 + 1):
            blocked_cells[row + i] = 1


def _largest_region(blocked_cells: bytearray, nx: int, ny: int, cell: float, x0: float, y0: float) -> PourRaster:
    """The biggest 4-connected run of free cells. Iterative, because a 36 000-cell flood recursed is
    a `RecursionError` and this runs inside a build."""
    seen = bytearray(nx * ny)
    best: list[int] = []
    for start in range(nx * ny):
        if blocked_cells[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        region: list[int] = []
        while stack:
            k = stack.pop()
            region.append(k)
            i, j = k % nx, k // nx
            for ni, nj in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if 0 <= ni < nx and 0 <= nj < ny:
                    n = ni + nj * nx
                    if not blocked_cells[n] and not seen[n]:
                        seen[n] = 1
                        stack.append(n)
        if len(region) > len(best):
            best = region
    return PourRaster(cell=cell, x0=x0, y0=y0, nx=nx, ny=ny, free=frozenset(best))


# --- same-net spacing: the one clearance question nobody asks ----------------------------------


SAME_NET_SAMPLES = 9
"""How many points of the closest-approach line are tested for intervening copper. Nine is enough to
catch the case the test exists for — a mitre corner, where the copper between the two pieces is
continuous and every sample lands on it — and cheap enough to run over every pair on a board."""


def _closest_on_seg(p: Pt, a: Pt, b: Pt) -> Pt:
    dx, dy = b[0] - a[0], b[1] - a[1]
    d2 = dx * dx + dy * dy
    if d2 <= 0.0:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / d2))
    return (a[0] + t * dx, a[1] + t * dy)


def closest_points(a, b) -> tuple[Pt, Pt, float]:
    """The two points of bare copper that face each other, and the gap between them.

    `hull_dist2` answers *how far*, never *where*, and the where is what decides whether a
    sub-clearance gap is a real slot or the inside of a mitre. The witness is the minimum over every
    vertex-to-edge pair in both directions — the same enumeration `hull_dist2` walks — then each point
    is pushed toward the other by its own shape's offset radius, which is where the copper's edge
    actually is.
    """
    best = (float("inf"), a.pts[0], b.pts[0])
    for pts_p, pts_q, flip in ((a.pts, b.pts, False), (b.pts, a.pts, True)):
        for p in pts_p:
            for i in range(len(pts_q)):
                s, e = pts_q[i], pts_q[(i + 1) % len(pts_q)]
                w = _closest_on_seg(p, s, e)
                d = math.dist(p, w)
                if d < best[0]:
                    best = (d, w, p) if flip else (d, p, w)
    d, pa, pb = best
    if d <= 1e-12:
        return (pa, pb, -(a.r + b.r))
    ux, uy = (pb[0] - pa[0]) / d, (pb[1] - pa[1]) / d
    return ((pa[0] + ux * a.r, pa[1] + uy * a.r), (pb[0] - ux * b.r, pb[1] - uy * b.r), round(d - a.r - b.r, 4))


def _inside(shape, at: Pt) -> bool:
    from .route_geom import hull_dist2

    return hull_dist2((at,), shape.pts) <= (shape.r + 1e-9) ** 2


def same_net_slots(text: str, floor: float, layers: tuple[str, ...] = ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")) -> list[dict]:
    """Same-net copper on one layer closer than `floor` with **bare laminate** in between.

    The one clearance question neither pcbc nor KiCad asks, and a wide locked spine is the copper most
    likely to be hugged: `route_scene._pair_clashes` skips rules 1 and 4 for a same-net pair by
    design, KiCad exempts same-net pairs from clearance entirely, and KRT treats its own net as free.
    The S7 review measured node's leftover sitting **0.0501 mm** from the locked `VBUS` spine on a
    board whose process floor is 0.0889 mm, and fifteen more such gaps across the five boards
    (finding 16). It is a census, not a rule: the class is pre-existing — the patterns-off node has as
    many — and an etch this fine is a yield question for the fab rather than a DRC error.

    A mitre corner and a continuous run are two pieces of copper 0.05 mm apart with copper between
    them, and they are every false positive here, so they are removed: the closest-approach line is
    sampled and a gap with any other same-net copper across it is not a slot.

    **Where that is approximate, and in which direction.** Two exactly parallel runs have a whole
    segment of equally-close points and `closest_points` returns one of them, so copper bridging the
    pair somewhere else along their length does not discard the gap. It over-reports rather than
    under-reports, which is the right direction for a census, and it is exact on the case it exists
    for: a corner's closest approach is a single point and the mitre leg covers it.
    """
    from .ampacity import ALL_CU, _SEG, _VIA, board_pad_geoms
    from .copper import net_table
    from .route_geom import track_shape

    names = net_table(text)
    items: list[tuple[str, frozenset[str], object, str]] = []
    for m in _SEG.finditer(text):
        net = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        if not net:
            continue
        x1, y1, x2, y2, w = (float(m.group(k)) for k in range(1, 6))
        items.append((net, frozenset({m.group(6)}), track_shape((x1, y1), (x2, y2), w), f"track ({x1:g},{y1:g})-({x2:g},{y2:g}) w{w:g}"))
    for m in _VIA.finditer(text):
        net = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        if not net:
            continue
        at = (float(m.group(1)), float(m.group(2)))
        items.append((net, ALL_CU, via_shape(at, float(m.group(3))), f"via ({at[0]:g},{at[1]:g})"))
    for p in board_pad_geoms(text, layers=layers):
        for s in p.copper:
            items.append((p.net, p.cu_layers, s, f"pad {p.id}"))
    by_net: dict[str, list[int]] = {}
    for i, (net, _l, _s, _w) in enumerate(items):
        if net and not net.startswith("unconnected-"):
            by_net.setdefault(net, []).append(i)
    out: list[dict] = []
    for net, idx in sorted(by_net.items()):
        boxes = {i: aabb(items[i][2]) for i in idx}
        for n, i in enumerate(idx):
            for j in idx[n + 1 :]:
                if not (items[i][1] & items[j][1]):
                    continue
                a, b = boxes[i], boxes[j]
                if a[0] > b[2] + floor or b[0] > a[2] + floor or a[1] > b[3] + floor or b[1] > a[3] + floor:
                    continue
                pa, pb, g = closest_points(items[i][2], items[j][2])
                if not (0.0 < g < floor):
                    continue
                # **One shape inside the other is not a slot, and `closest_points` cannot say so.**
                # It is the minimum over vertex-to-edge pairs in both directions, which measures the
                # separation of two *boundaries* and has no containment test in it: a 0.5 mm via ring
                # sitting in the middle of a 1.45 mm pad comes back as a positive 0.0749 mm — the
                # margin of pad copper left around the barrel — and that is copper, not laminate.
                # Nothing on these boards had a contained same-net pair until `Thermal()`, which is
                # via-in-pad by definition, so the false positive arrived with the array: measured
                # 2026-09-21, it put nine rows into c3_usb's census (22 -> 31) that are the *inside*
                # of a land. The sampling below cannot catch it because the container is one of the
                # two items in the pair and it excludes those.
                if _inside(items[j][2], pa) or _inside(items[i][2], pb):
                    continue
                filled = False
                for k in range(1, SAME_NET_SAMPLES + 1):
                    t = k / (SAME_NET_SAMPLES + 1)
                    at = (pa[0] + (pb[0] - pa[0]) * t, pa[1] + (pb[1] - pa[1]) * t)
                    if any(
                        m not in (i, j) and (items[m][1] & items[i][1] & items[j][1]) and _inside(items[m][2], at)
                        for m in idx
                    ):
                        filled = True
                        break
                if filled:
                    continue
                layer = sorted(items[i][1] & items[j][1])[0]
                out.append({"net": net, "layer": layer, "gap": g, "a": items[i][3], "b": items[j][3], "at": [round(pa[0], 4), round(pa[1], 4)]})
    return sorted(out, key=lambda r: (r["gap"], r["net"], r["layer"], r["a"], r["b"]))


# --- R-X4: a declared chain's order, measured in the copper KRT finished --------------------------
#
# `docs/router-plan.md` line 202 tags R-X4 "**P** (chain), **V**" — a pattern *and* a verification —
# and until this section only the P existed. The chain pattern refused a link it could not write and
# nothing anywhere read the finished board to ask whether the order the author declared had survived
# in it. That absence is what made the pattern's refusal **hard**: the refusal was standing in for a
# gate that was never built, on the premise that KRT would branch and nobody would notice. The
# premise is now measured instead of assumed (`docs/r2-measurements.md`, S6), the refusal is soft,
# and this is the gate.
#
# What it is not. It is not a connectivity check — `netcheck.check_copper` is KiCad's own netlist
# gate and it is order-blind by construction, which is exactly the hole. It is not
# `route_checks.check_chains` either: that measures a straight centre-line corridor between pad
# centres at **placement** time, before any copper exists, and on ds2's `VDDA` it says nothing at all.
#
# One honest note on the citation. R-X4's own text scopes itself to a **high-speed** net ("a track
# that branches to a third pad on a high-speed net is a chain order violation"), while this checks
# every declared `Chain()` — including ds2's `VDDA`, which is Power class. That is deliberate and it
# is wider than the line it is named after: B.2's rule is "the chain goes through its pads in order",
# and a bypass cap fed as a spur is the same defect as a signal stub whatever the class. The widening
# is recorded rather than smuggled, because it also means the old hard refusal was protecting an
# invariant broader than the rule anyone had written down for it.


CHAIN_BISECT = 64
"""Bisection steps used to find where a track's centre line crosses a station's pad outline.

The distance from a point to a convex hull is a convex function of the point, so along a straight
centre line the set of parameters within `d` of one pad primitive is a single closed interval: a
ternary search finds a point inside it and two bisections find its ends. 64 steps of each puts the
ends within `(2/3)**64` (1.6e-12) and `2**-64` of the true crossing — eleven orders below the
nanometre grid every coordinate on the board is quantised to, so the answer is a function of the
geometry and not of the iteration count.

Both ends are taken from the **inside** of the interval (the last parameter known to be within `d`),
so the copper this check removes is never more than the copper the pad covers. Erring that way makes
the check report a spur rather than silently accept one."""


@dataclass(frozen=True)
class ChainVerdict:
    """One declared `Chain()`, judged against the routed board.

    `verdict` is one of:

    - `"held"` — every consecutive pair is joined, and the feed passes **through** every stop
      between the first and the last.
    - `"spur"` — a stop is connected to the net but the feed goes round it: `detail` names which.
    - `"open"` — two consecutive stops are not joined by copper at all. The netlist gate normally
      catches this first; it is here because a chain whose order cannot be judged must say so rather
      than pass.
    - `"poured"` — the net carries a filled zone, so every pad of it is connected through the plane
      and "the order" is not a question the copper can answer. Reported, never failed.
    - `"unresolved"` — a `Chain()` member names no pad of that net on the board. `pcbc check` refuses
      this before the board is drawn; it is reported here rather than silently skipped.

    `owner` is `"chain"` when pcbc's own chain pattern was allowed to route this net and `"R4"` when
    B.2 hands it away (a net carrying a `PairSpec`). It is the whole of what the build gate does with
    a violation — see `build._chain_gate`."""

    net: str
    where: str  # "Chain line 97"
    stations: tuple[str, ...]  # pad ids in the declared order: ("J2.1", "C4.1", "U1.12")
    verdict: str
    owner: str  # "chain" | "R4"
    detail: str = ""
    move: str = ""

    def to_dict(self) -> dict:
        return {
            "net": self.net,
            "where": self.where,
            "stations": list(self.stations),
            "verdict": self.verdict,
            "owner": self.owner,
            "detail": self.detail,
            "move": self.move,
        }


def _at(a: Pt, b: Pt, t: float) -> Pt:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _within_span(a: Pt, b: Pt, shape: Shape, d: float) -> tuple[float, float] | None:
    """The parameters of the centre line `a -> b` lying within `d` mm of `shape`'s copper, or None.

    One interval and never two, because `shape` is convex: see `CHAIN_BISECT`.
    """
    lim = (shape.r + d) ** 2
    box = aabb(shape)
    if min(a[0], b[0]) > box[2] + d or max(a[0], b[0]) < box[0] - d:
        return None
    if min(a[1], b[1]) > box[3] + d or max(a[1], b[1]) < box[1] - d:
        return None

    def f(t: float) -> float:
        return hull_dist2((_at(a, b, t),), shape.pts) - lim

    lo, hi = 0.0, 1.0
    for _ in range(CHAIN_BISECT):
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if f(m1) <= f(m2):
            hi = m2
        else:
            lo = m1
    inner = (lo + hi) / 2.0
    if f(inner) > 0.0:
        if f(0.0) <= 0.0:
            inner = 0.0
        elif f(1.0) <= 0.0:
            inner = 1.0
        else:
            return None
    t0 = 0.0 if f(0.0) <= 0.0 else _edge(f, inner, 0.0)
    t1 = 1.0 if f(1.0) <= 0.0 else _edge(f, inner, 1.0)
    return (t0, t1)


def _edge(f, good: float, bad: float) -> float:
    for _ in range(CHAIN_BISECT):
        mid = (good + bad) / 2.0
        if f(mid) <= 0.0:
            good = mid
        else:
            bad = mid
    return good


def _outside(a: Pt, b: Pt, w: float, shapes: Sequence[Shape], d: float) -> tuple[tuple[Pt, Pt, float], ...]:
    """This track cut into the pieces of it that lie outside every one of `shapes`, dilated by `d`.

    A pad may draw several primitives (a custom pad is several `Shape`s), so the intervals are merged
    before the complement is taken; the pieces come back in centre-line order.
    """
    spans = [s for s in (_within_span(a, b, sh, d) for sh in shapes) if s is not None]
    if not spans:
        return ((a, b, w),)
    spans.sort()
    merged: list[list[float]] = [list(spans[0])]
    for t0, t1 in spans[1:]:
        if t0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], t1)
        else:
            merged.append([t0, t1])
    gaps: list[tuple[float, float]] = []
    cur = 0.0
    for t0, t1 in merged:
        if t0 > cur:
            gaps.append((cur, t0))
        cur = max(cur, t1)
    if cur < 1.0:
        gaps.append((cur, 1.0))
    return tuple((_at(a, b, t0), _at(a, b, t1), w) for t0, t1 in gaps)


def _net_copper(text: str, net: str) -> tuple[tuple[str, frozenset[str], Shape, tuple], ...]:
    """Every track and via on one net, as `(kind, layers, shape, extra)`, in file order.

    A via's layers are the whole copper stack when it spans `F.Cu` to `B.Cu`, which every via R2 or
    KRT writes on these boards does (`_via_rules` item 8 asserts it of pcbc's own). A via that does
    not is read as the two layers it names, which under-connects rather than over-connects — the
    direction that reports a spur rather than inventing a path through one.
    """
    from .ampacity import ALL_CU, _SEG, _VIA
    from .copper import net_table

    names = net_table(text)
    out: list[tuple[str, frozenset[str], Shape, tuple]] = []
    for m in _SEG.finditer(text):
        n = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        if n != net:
            continue
        a = (float(m.group(1)), float(m.group(2)))
        b = (float(m.group(3)), float(m.group(4)))
        w = float(m.group(5))
        out.append(("seg", frozenset({m.group(6)}), track_shape(a, b, w), (a, b, w)))
    for m in _VIA.finditer(text):
        n = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        if n != net:
            continue
        at = (float(m.group(1)), float(m.group(2)))
        span = (m.group(5), m.group(6))
        layers = ALL_CU if span == ("F.Cu", "B.Cu") else frozenset(span)
        out.append(("via", layers, via_shape(at, float(m.group(3))), (at,)))
    return tuple(out)


def _reaches(nodes: Sequence[tuple[str, frozenset[str], Shape, tuple]], src: Sequence[int], dst: Sequence[int]) -> bool:
    """Is any of `src` joined to any of `dst` through touching copper?

    Two pieces touch when `clears(a, b, 0.0)` is False — a gap under `EPS_MM`, which is pcbc's own
    "these are one conductor". Copper pcbc and KRT write meets exactly, so the epsilon decides
    nothing here; it is the same one every other clearance answer on the board is given with.
    """
    boxes = [aabb(s) for _k, _l, s, _e in nodes]
    want = set(dst)
    seen = set(src)
    if seen & want:
        return True
    stack = list(src)
    while stack:
        i = stack.pop()
        bi, li, si = boxes[i], nodes[i][1], nodes[i][2]
        for j in range(len(nodes)):
            if j in seen or not (li & nodes[j][1]):
                continue
            bj = boxes[j]
            if bi[0] > bj[2] or bj[0] > bi[2] or bi[1] > bj[3] or bj[1] > bi[3]:
                continue
            if clears(si, nodes[j][2], 0.0):
                continue
            seen.add(j)
            if j in want:
                return True
            stack.append(j)
    return False


def _station_pads(design, net: str, member: str, pads: dict) -> tuple[str, ...]:
    """One `Chain()` member, `"REF.PIN"`, as the pad ids it names on this board.

    `patterns.chain._members`' resolution, read off the routed file instead of off a scene: a member
    is a pin name as `Place(to=)` takes it, one pin may own several pads, and every pad it names is a
    station. Sorted by `(ref, pad number)` — the order `patterns.terminals` puts them in — so the
    stations a multi-pad pin contributes are in the same order here and there.
    """
    ref, _, pin = member.partition(".")
    inst = next((i for i in getattr(design, "instances", ()) if i.ref == ref), None)
    nums: tuple[str, ...] = (pin,)
    if inst is not None and pin in inst.part.pins:
        nums = tuple(inst.part.pins[pin].pads)
    got = [f"{ref}.{num}" for num in nums if f"{ref}.{num}" in pads and pads[f"{ref}.{num}"].net == net]
    return tuple(sorted(got, key=lambda pid: (pid.split(".")[0], _padnum(pid.split(".", 1)[1]))))


def _padnum(num: str) -> tuple[int, str]:
    return (int(num), "") if num.isdigit() else (1 << 30, num)


def _toward(dx: float, dy: float) -> str:
    """`route_checks._quantise`'s answer and the vocabulary `Place(toward=)` accepts, in KiCad's
    y-down frame. `patterns.chain._toward`'s body, so a gate's move and a refusal's move speak the
    same four words."""
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "down" if dy >= 0 else "up"


def chain_order(text: str, design, cs: ConstraintSet, *, floor: float | None = None) -> tuple[ChainVerdict, ...]:
    """R-X4's verify half: did every **declared** `Chain()`'s order survive in the finished copper?

    One verdict per `Chain()` in board order. The question is asked of the routed board and of
    nothing else — not of pcbc's own pieces, which `verify_copper` already judges, and not of the
    placement, which `route_checks.check_chains` already judges.

    **The definition, in two halves.**

    1. *Joined.* For each consecutive pair `(p_i, p_i+1)` of the declared order there is a path
       through touching copper — every track, via and **pad** of the net — from one to the other.
    2. *Through, not past.* For each **intermediate** stop `p_i` the feed passes through that pad's
       own copper: with `p_i`'s pad copper removed from the board, `p_i-1` no longer reaches
       `p_i+1`. A stop that is still bypassed once its pad is gone was never in the path; it hangs
       off it, which is the stub B.2 forbids and R-X4 names.

    Removing a pad means removing its copper *region*, not its node in a graph, and that distinction
    is the whole of why this check passes the one board it exists for. On ds2's routed `VDDA` the
    graph has a degree-3 vertex at (20.35, 6.45) whose two through-edges are exactly collinear at
    -45 degrees: as a graph it is not a junction the feed turns at, and deleting the vertex leaves
    `J2.1` connected to `U1.12`, so a cut-vertex reading of "the pad is in the path" **fails** a
    board on which the feed does run straight across the cap's pad. Measured (2026-09-20): that
    vertex is **0.4300 mm inside** `C4.1`'s own pad copper and **1.1185 mm** of the trunk's centre
    line lies inside it. Remove the region and the two arms of the trunk are 1.12 mm apart with
    nothing between them; the feed is cut, and the verdict is `held`, which is the truth.

    **The tolerance, and why it is not load-bearing.** A pad's copper is dilated by
    `stackup.clearance_min` before it is removed, so a junction sitting just *outside* a stop still
    counts as feeding through it. The number is the process floor — the same one `chain.stub_need`
    takes for R-X4's pattern half (S6 decision 1) and the one `route.py` hands KRT as
    `--same-net-pad-clearance` — on the argument that copper closer together than a fab's minimum
    clearance is not two separable things. It decides nothing on any board in this repo: swept from
    0.0 mm to 2.0 mm, every one of the five declared chains keeps its verdict, and the nearest flip
    is node's `USB_DN` at 2.5 mm and c3_usb's at 3.0 mm — 28x and 24x their own process floors
    (0.0889 and 0.127 mm). The measured
    gap between the closest pass (ds2's junction, 0.43 mm inside the pad) and the closest genuine
    spur (c3_usb's `U3.3`, off the trunk by millimetres) is about 3 mm wide. `floor` overrides it, for
    the sweep that measures that and for nothing else — the product never passes it.

    **What it does not answer.** A net with a filled zone is `poured` and skipped: every pad of a
    poured net is joined through the plane, so the copper cannot say what order the feed takes. A via
    is an atom — it is removed when its centre lies in the dilated pad and kept otherwise — so a
    via-in-pad reads as part of the pad and a via beside one reads as a separate node; no board here
    has a via near a chain station. And the check reads what the file says, so a chain member that
    names no pad on the board is `unresolved` rather than absent.
    """
    from .ampacity import board_pad_geoms

    floor = float(cs.stackup.clearance_min if floor is None else floor)
    poured = {z.net for z in zones(text) if z.polys}
    by_net: dict[str, dict] = {}
    for p in board_pad_geoms(text):
        by_net.setdefault(p.net, {})[p.id] = p
    out: list[ChainVerdict] = []
    for ch in getattr(design, "chains", ()):
        where = f"Chain line {ch.line}"
        c = cs.by_net(ch.net)
        owner = "R4" if (c is not None and c.pair is not None) else "chain"
        pads = by_net.get(ch.net, {})
        stations: list[str] = []
        missing = ""
        for member in ch.pads:
            got = _station_pads(design, ch.net, member, pads)
            if not got:
                missing = member
                break
            stations.extend(got)
        if missing:
            out.append(ChainVerdict(ch.net, where, tuple(stations), "unresolved", owner, f"{missing} names no pad of {ch.net} on this board", ""))
            continue
        if ch.net in poured:
            out.append(ChainVerdict(ch.net, where, tuple(stations), "poured", owner, f"{ch.net} carries a filled zone, so every pad of it is joined through the plane and the copper cannot say what order the feed takes", ""))
            continue
        out.append(_judge(text, ch, tuple(stations), pads, owner, where, floor))
    return tuple(out)


def _judge(text: str, ch, stations: tuple[str, ...], pads: dict, owner: str, where: str, floor: float) -> ChainVerdict:
    """The two halves of `chain_order`'s definition, for one chain.

    **Every** pad of the net is a node, not only the stations: a pad is copper, and a run that lands
    on some third pad of the same net and leaves it is a path the board really has. Leaving the
    others out would break such a path and report `held` — a violation missed — which is the one
    direction a gate must not err in.
    """
    cop = list(_net_copper(text, ch.net))
    n = len(stations) - 1

    def graph(pieces, without: str = "") -> tuple[list, dict[str, list[int]]]:
        nodes = list(pieces)
        where_: dict[str, list[int]] = {}
        for pid in sorted(pads):
            if pid == without:
                continue
            where_[pid] = []
            for s in pads[pid].copper:
                where_[pid].append(len(nodes))
                nodes.append(("pad", pads[pid].cu_layers, s, (pid,)))
        return nodes, where_

    nodes, idx = graph(cop)
    for i in range(n):
        if not _reaches(nodes, idx[stations[i]], idx[stations[i + 1]]):
            return ChainVerdict(
                ch.net, where, stations, "open", owner,
                f"no copper joins {stations[i]} to {stations[i + 1]}, so link {i + 1} of {n} of this chain is not on the board",
                f'Route it or drop it: {_chain_call(ch)} declares a feed pcbc did not finish and KRT did not close.',
            )
    for i in range(1, len(stations) - 1):
        pid = stations[i]
        want = pads[pid]
        cut: list[tuple[str, frozenset[str], Shape, tuple]] = []
        for kind, layers, shape, extra in cop:
            if not (layers & want.cu_layers):
                cut.append((kind, layers, shape, extra))
                continue
            if kind == "via":
                at = extra[0]
                if any(hull_dist2((at,), s.pts) <= (s.r + floor) ** 2 for s in want.copper):
                    continue
                cut.append((kind, layers, shape, extra))
                continue
            a, b, w = extra
            for p0, p1, ww in _outside(a, b, w, want.copper, floor):
                cut.append(("seg", layers, track_shape(p0, p1, ww), (p0, p1, ww)))
        graph_cut, cidx = graph(cut, without=pid)
        if _reaches(graph_cut, cidx[stations[i - 1]], cidx[stations[i + 1]]):
            prev, nxt = stations[i - 1], stations[i + 1]
            ref = pid.split(".")[0]
            mid = ((pads[prev].at[0] + pads[nxt].at[0]) / 2.0, (pads[prev].at[1] + pads[nxt].at[1]) / 2.0)
            toward = _toward(mid[0] - pads[pid].at[0], mid[1] - pads[pid].at[1])
            return ChainVerdict(
                ch.net, where, stations, "spur", owner,
                f"the copper joins {prev} to {nxt} without crossing {pid}'s pad, so {pid} hangs off the feed instead of sitting in it",
                f'Place("{ref}", toward="{toward}") moves it into the corridor between {prev} and {nxt}; '
                f"or {_chain_call(ch)} is asking for an order this copper does not take — drop {ref} from the chain and feed it locally instead.",
            )
    return ChainVerdict(ch.net, where, stations, "held", owner, "", "")


def _chain_call(ch) -> str:
    """The `Chain(...)` line this chain is — the edit a gate's move ends in. `chain._chain_call`'s
    answer, off the `ChainReq` rather than off a `ChainSpec`, so the gate quotes the author's own
    spelling (`U1.VIN`) and not the pad it resolved to."""
    return f'Chain("{ch.net}", ' + ", ".join(f'"{p}"' for p in ch.pads) + ")"


# --- The two populations, counted before anything is designed around them --------------------------
#
# `docs/stitch-plan.md` S1: "measure, place nothing". Both functions below read a finished board and
# place nothing, ever. They exist because two of the six stitching techniques were argued about for
# a whole panel on the strength of populations nobody had counted, and a technique whose population
# is zero is decoration however good the geometry behind it is.
#
# Technique 5 (the return via) turns out to have a population of **zero** and it is zero for a
# structural reason that no amount of routing improvement fixes — see `return_vias`. Technique 1 (the
# parallel power via) has a population of **one net on one board**, node's `VBUS`, every group of it
# short by exactly one via — see `via_parallelism`. Neither number was available before this slice;
# both are now a pinned dict in `tests/test_examples_fab.py` and asserted in
# `tests/test_route_verify_stitch.py`.


RETURN_MM = VIA_PARALLEL_MM
"""How close a via on the reference net has to be to count as a signal via's return path, in mm.

Borrowed, not invented. `ampacity.VIA_PARALLEL_MM` is already this repo's answer to "were these two
vias placed as one group, or for two different reasons?" — a fab's hole-to-hole plus two annular
rings is about 0.7 mm on these stackups, so 1.0 mm is a group and more is not — and a signal via
with its return via beside it is that same question asked across two nets instead of within one. A
second constant for the same distance is what the house rules forbid, and it would drift.

**Measured 2026-09-20: it decides nothing on any board in this repo, and that is the honest thing to
say about it.** All eleven signal vias on a net carrying `Constraint.reference` are settled by the
*stackup* before any distance is taken — `net_change` on node, `lost` on c3_usb — and the nearest
reference-net via to any of them is **1.6279 mm** (c3_usb `USB_DP` at (15.05,2.75), GND via at
(16.25,3.85)). That is 1.63x this number and 2.03x the tightest a return via could legally be placed
(`ClearanceTable.via_pitch("USB_DP","GND",...)` = 0.8 mm on the two-layer stackup, 0.7 mm on node's).
So a reader deciding whether to trust a verdict here is deciding on the stackup, not on a tolerance.
"""


def poured_planes(text: str) -> tuple[tuple[str, str], ...]:
    """`(net, layer)` for every zone that came back **filled**, sorted — the planes a finished board
    actually has.

    `route_scene.plane_targets` answers the same question from the compiled job, which is the right
    source for a pattern deciding where to put copper. This is the right source for a check reading a
    board KiCad has already filled: a `Board(planes=...)` that poured nothing is not a reference
    plane, and an unfilled zone is not one either (`plane_checks`' finding 17, same stance).

    Measured 2026-09-20, they agree on four boards and **disagree on blinky**, which is the reason to
    ask the file. buck/c3_usb/ds2 both say `(("GND","B.Cu"),)` and node both say
    `(("3V3","In2.Cu"), ("GND","In1.Cu"))`. blinky's compiled job says `(("GND","B.Cu"),)` — `krt_plan`
    schedules `gnd_pour` for every two-layer board with `GND` among its power nets — and blinky's
    routed board holds one segment and **no `(zone ...)` block at all**, so this returns `()`. That is
    the trap `docs/stitch-plan.md` §7.1 names: stitch vias into a pour that was never written, on a
    board where KiCad's pad-to-pad unconnected check would never mention it.
    """
    return tuple(sorted({(z.net, z.layer) for z in zones(text) if z.polys and z.net}))


def _layer_references(text: str, cs: ConstraintSet) -> dict[str, tuple[str, str] | None]:
    """Per copper layer: `(reference layer, reference net)`, or `None` where there is no plane.

    `Stackup.height_to_reference` is the one arithmetic, handed the pours the board really has rather
    than the `planes=` it declares, so `two_layer_pour` is off — on two layers the GND pour is in
    `poured_planes` already and comes back as the layer `"B.Cu"` instead of the compiler's printed
    name `"B.Cu pour"`. Same plane, same height, a name that can be looked up in a net map.
    """
    planes = poured_planes(text)
    by_layer = {lay: net for net, lay in planes}
    out: dict[str, tuple[str, str] | None] = {}
    for lay in cs.stackup.copper_layers():
        found = cs.stackup.height_to_reference(lay, planes, two_layer_pour=False)
        out[lay] = (found[1], by_layer[found[1]]) if found is not None else None
    return out


@dataclass(frozen=True)
class ReturnVia:
    """One via on a net carrying `Constraint.reference`, and what its layer change does to the return.

    `verdict` is one of five, and only two of them are about distance:

    - `net_change` — the copper's reference is a **different net** either side of the via. A via joins
      one net to itself, so no via anywhere carries this return current: what the return wants here is
      a capacitor between the two planes, which is technique 6's business and not a stitch's.
    - `lost` — one of the two layers has no plane at all, so the return has nothing to follow onto.
    - `none` — the reference net is the same either side and the board carries no via on it.
    - `far` — there is one, further than `RETURN_MM`.
    - `served` — there is one within `RETURN_MM`.
    """

    net: str
    at: Pt
    layers: tuple[str, str]
    from_ref: tuple[str, str] | None  # (layer, net) the copper on layers[0] references
    to_ref: tuple[str, str] | None
    verdict: str
    near: tuple[str, Pt, float] | None  # (net, at, mm) of the nearest via on a reference net
    why: str

    def to_dict(self) -> dict:
        return {
            "net": self.net,
            "at": list(self.at),
            "layers": list(self.layers),
            "from_ref": list(self.from_ref) if self.from_ref else None,
            "to_ref": list(self.to_ref) if self.to_ref else None,
            "verdict": self.verdict,
            "near": [self.near[0], list(self.near[1]), self.near[2]] if self.near else None,
            "why": self.why,
        }

    def line(self) -> str:
        near = f"; nearest {self.near[0]} via {self.near[2]:g} mm away at ({self.near[1][0]:g},{self.near[1][1]:g})" if self.near else "; no via on any reference net of this board"
        return f"{self.net} via at ({self.at[0]:g},{self.at[1]:g}) {self.layers[0]}->{self.layers[1]} [{self.verdict}]: {self.why}{near}"


def return_vias(text: str, cs: ConstraintSet) -> tuple[ReturnVia, ...]:
    """R-Z4's **V**, which has never existed: every via on a controlled-impedance net, classified.

    `docs/router-plan.md` line 196 tags R-X1/R-Z4 with a **V** and nothing in this repo has ever read
    a finished board to ask what a signal via does to its return current. `verify_copper`'s D.1 item 6
    asserts pcbc *writes* no via on such a net, which is a statement about pcbc's copper; this is the
    statement about KRT's, and KRT wrote all eleven of them.

    **The finding, and it is why `docs/stitch-plan.md` §8 refuses to ship a return-via placer at all.**
    Measured on the checked-in routed boards, 2026-09-20:

        node    7 vias (USB_DN 5, USB_DP 2)  all `net_change`
        c3_usb  4 vias (USB_DN 2, USB_DP 2)  all `lost`
        blinky, buck, ds2                    no net carries a reference, so nothing to classify

    Not one of the eleven is `served`, `far` or `none` — that is, **not one of them is a distance
    question**. node is four layers with `GND` on In1.Cu and `3V3` on In2.Cu, so a through via takes
    the copper from a plane referenced to GND to a plane referenced to 3V3: the return current has to
    change *net*, and no via joins two nets. c3_usb is two layers with one pour on B.Cu, so a via that
    puts the track on B.Cu has no second plane to reach and the reference is simply gone. A placer
    would have to widen a tolerance until something landed, or drop a via on the GND side of node's
    crossing and call it half done; both are copper that connects nothing while the tool's own report
    blesses it.

    **Re-measured on fresh builds the same day, and the verdicts are the half that holds.** node comes
    out of `pcbc build` with **no via at all** on either pair member — its checked-in routed directory
    has no `patterns_post` step, so it predates S5 and §2(o) already called it stale — and c3_usb comes
    out with **five**, still every one `lost`. So the count belongs to whatever KRT did that run and
    the verdict belongs to the stackup, which is why the verdict is what §8's refusal rests on.

    Report-only, always. There is no move here that is not a board change (`Board(planes=...)` on node,
    `NetReq(layers=["F.Cu"])` on c3_usb), and `docs/stitch-plan.md` S5 owns printing them.
    """
    refs = _layer_references(text, cs)
    watched = set(referenced_nets(cs))
    vias = board_vias(text)
    ref_nets = sorted({net for r in refs.values() if r for net in (r[1],)})
    by_net: dict[str, list[Pt]] = {}
    for v in vias:
        by_net.setdefault(v["net"], []).append(v["at"])
    out: list[ReturnVia] = []
    for v in sorted((v for v in vias if v["net"] in watched), key=lambda v: (v["net"], v["at"])):
        a, b = v["layers"]
        fr, to = refs.get(a), refs.get(b)
        wanted = sorted({r[1] for r in (fr, to) if r} or set(ref_nets))
        near: tuple[str, Pt, float] | None = None
        for name in wanted:
            for at in sorted(by_net.get(name, ())):
                d = round(math.dist(v["at"], at), 4)
                if near is None or (d, at) < (near[2], near[1]):
                    near = (name, at, d)
        if fr is None or to is None:
            gone = a if fr is None else b
            other = b if fr is None else a
            kept = to if fr is None else fr
            if kept is None:
                why = f"neither {a} nor {b} has a plane above or below it, so there is no reference on this board to return to"
            elif kept[0] == gone:
                why = (
                    f"the via lands the copper in the {kept[1]} pour's own layer ({gone}), which cannot be its own "
                    f"reference; {other} was referenced to {kept[1]} on {gone} and there is no second plane to reach"
                )
            else:
                why = f"{gone} has no plane above or below it; {other} is referenced to {kept[1]} on {kept[0]}, and the via leaves that reference behind"
            verdict = "lost"
        elif fr[1] != to[1]:
            why = f"the reference changes net across the via, {fr[1]} on {fr[0]} -> {to[1]} on {to[0]}, and a via joins one net to itself"
            verdict = "net_change"
        elif near is None:
            why = f"the reference is {fr[1]} on {fr[0]} either side, and this board carries no {fr[1]} via at all"
            verdict = "none"
        elif near[2] <= RETURN_MM:
            why = f"the reference is {fr[1]} on {fr[0]} either side, and a {near[0]} via sits within {RETURN_MM:g} mm of it"
            verdict = "served"
        else:
            why = f"the reference is {fr[1]} on {fr[0]} either side, and the nearest {near[0]} via is beyond the {RETURN_MM:g} mm a return via would be placed at"
            verdict = "far"
        out.append(ReturnVia(v["net"], v["at"], (a, b), fr, to, verdict, near, why))
    return tuple(out)


def referenced_nets(cs: ConstraintSet) -> tuple[str, ...]:
    """The nets `return_vias` watches: the ones the compiler gave a `Constraint.reference`.

    Its own function because the *empty* answer has two different causes and a report that cannot
    tell them apart is a report that lies. Measured on the checked-in boards: `("USB_DN","USB_DP")`
    on c3_usb and node, `()` on blinky, buck and ds2 — and `wants_reference` is only set for
    `kind == "usb_hs"` or a declared `z_se_ohm` (`constraints.py:1017`), so `cs.by_net("GND")
    .reference` is `None` on all five. That last fact is what keeps D.1 item 6 correct without a
    change: a return via's net *is* the reference net, so item 6 never had a return via to block.
    """
    return tuple(sorted(c.net for c in cs.constraints if c.reference))


def return_lines(rows: Sequence[ReturnVia], watched: Sequence[str] = ()) -> list[str]:
    """One line per classified via, then one summary line — or the sentence that says which of the two
    empty cases this is, because "nothing printed" and "nothing to print" read the same and are not.

    The two are genuinely different and both happen here. blinky, buck and ds2 declare no
    controlled-impedance net at all, so there is nothing to watch. A freshly built node **does** watch
    `USB_DN` and `USB_DP` and its pair simply never leaves F.Cu, so there is nothing to classify — a
    board that passes for the best possible reason, and a line that said "no net carries a reference
    plane" about it would be false.
    """
    if not rows:
        if watched:
            return [f"returns: {', '.join(sorted(watched))} carry a reference plane and no via at all, so nothing here changes one"]
        return ["returns: no net on this board carries a reference plane, so no via changes one"]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    tally = ", ".join(f"{n} {v}" for v, n in sorted(counts.items()))
    return [r.line() for r in rows] + [f"returns: {len(rows)} via(s) on a referenced net — {tally}"]


def _clusters(vs: Sequence[dict]) -> tuple[tuple[int, ...], ...]:
    """Single-linkage groups of `vs`, by index, at `VIA_PARALLEL_MM`; each group sorted, then all of
    them sorted by their first member's coordinate.

    `ampacity._via_clusters` asks the same question and answers only with the **sizes**, which is all
    the ampacity walk needs to hand each via `n x via_amps`. `via_parallelism` needs the membership —
    which via anchors the group, and which drill in it is the weakest — so it walks the pairs again.
    The predicate and the constant are deliberately the same two lines rather than two readings of
    one idea, and a test holds them together
    (`test_route_verify_stitch.py::test_the_two_cluster_walks_agree_on_every_via_of_every_board`). If
    they ever disagreed, the current pcbc *reports* a cluster carrying and the vias it counts to get
    there would be about different sets of copper, which is exactly the lie `docs/stitch-plan.md`
    §2(e) refuses to let a bare twin tell.
    """
    parent = list(range(len(vs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            if math.dist(vs[i]["at"], vs[j]["at"]) <= VIA_PARALLEL_MM:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups: dict[int, list[int]] = {}
    for i in range(len(vs)):
        groups.setdefault(find(i), []).append(i)
    return tuple(sorted((tuple(sorted(g)) for g in groups.values()), key=lambda g: vs[g[0]]["at"]))


@dataclass(frozen=True)
class Parallel:
    """One parallel group of vias on one power net, and whether it carries what the net declares."""

    net: str
    amps: float  # what `NetReq(amps=)` says this net carries
    at: Pt  # the group's anchor: its first via in sorted order
    n: int  # how many vias are in it
    drill: float
    """The **weakest** barrel in the group, because the group is only as good as its thinnest plating.

    Not what `ampacity.net_nodes` does today: it hands each member `n x via_amps(that member's own
    drill)`, so in a mixed cluster the wide via reports the whole group at its own rating.
    `docs/stitch-plan.md` §6's last bullet calls that a one-line fix that is correct regardless of
    this feature, and it is not this slice's to make; taking the `min` here is the same arithmetic
    read the conservative way, and every group on all five boards is single-drilled anyway (0.2 mm on
    node, 0.3 mm everywhere else), so the two agree on every number recorded."""
    carries: float  # n x via_amps(drill, plating, temp_rise)
    need: int  # stackup.vias_per_change(amps, drill, plating, temp_rise)
    add: int  # need - n, floored at 0: the rungs technique 1 would have to place here

    def to_dict(self) -> dict:
        return {"net": self.net, "amps": self.amps, "at": list(self.at), "n": self.n, "drill": self.drill, "carries": self.carries, "need": self.need, "add": self.add}

    def line(self) -> str:
        verdict = f"needs {self.need}, so {self.add} more" if self.add else "rated"
        return f"{self.net} via group at ({self.at[0]:g},{self.at[1]:g}): {self.n} x {self.drill:g} mm drill carries {self.carries:g} A of {self.amps:g} A [{verdict}]"


def via_parallelism(text: str, cs: ConstraintSet) -> tuple[Parallel, ...]:
    """Technique 1's population: every parallel via group on an unpoured power rail, rated.

    A via is not a track and its current is a **count**, not a width. `stackup.vias_per_change` has
    compiled that count since R1 and `ViaSpec.per_change` appears nowhere outside `constraints.py` —
    nothing has ever compared it to the vias a finished board has. This does, per group, so that the
    parallel carrier of `docs/stitch-plan.md` S4 is designed against a counted population instead of
    an assumed one.

    **A poured net is exempt, for the reason `power_bottlenecks` exempts it** (`zoned`): the plane is
    the rail's conductor and a via into it is a tap carrying one pad's share, not the trunk changing
    layer. The exemption is load-bearing rather than tidy — measured 2026-09-20, without it buck's
    `GND` reads five under-rated groups (5 x 0.707 A against a 2 A rail) and node's `GND` and `3V3`
    read sixteen and seven, and every one of those vias is a tap into the pour that carries the 2 A.

    **Measured on the checked-in routed boards, 2026-09-20, and the population is one net:**

        node    VBUS  1 A   4 groups, all singletons, 0.2 mm drill  0.527 A each, need 2  -> 4 rungs
        c3_usb  3V3   0.5 A 2 singletons, 0.3 mm                    0.707 A, need 1       rated
        c3_usb  VBUS  0.5 A 3 singletons, 0.3 mm                    0.707 A, need 1       rated
        ds2     3V3/VDDA/VSS 0.1 A, 13 singletons, 0.3 mm           0.707 A, need 1       rated
        blinky, buck  no via on any unpoured power net at all                             empty

    node's `VBUS` is the only under-rated rail on any of them, and `vias_per_change(1.0, 0.2)` is 2
    against a group of 1 four times over. ds2 cannot ever trigger it: 0.1 A against `via_amps(0.3)`
    of 0.707 A is `per_change` 1, so every group it has is rated by arithmetic and not by luck.

    **Re-measured on fresh builds the same day: node's `VBUS` comes out with three groups, not four**
    — at (27.8,36.3), (28,33.4) and (31.3,38.5), still singletons, still each short by one. The count
    is the router's and moves with it; the shortfall per group is the stackup's and does not.
    `docs/stitch-plan.md` §2(o) tells S4 to re-measure rather than copy the four, and this is why.

    **This is not the same question as `power_bottlenecks`' `kind == "via"`**, and the difference
    matters to S4. The bottleneck is the worst piece on the worst *path*, so a net whose narrowest
    track is worse than its vias reports `kind == "track"` and the under-rated barrel underneath it is
    invisible — which is exactly node's `VBUS` on these artifacts (`kind=track carries=0.414
    width=0.0889`, the stale pre-S5 boards `docs/stitch-plan.md` §2(o) warns about). Every group is
    rated here whether or not it happens to be the binding piece today, because a track that S4's
    sibling slices widen promotes the barrel behind it to the bottleneck.
    """
    poured = {net for net, _lay in poured_planes(text)}
    stack = cs.stackup
    vias = board_vias(text)
    out: list[Parallel] = []
    for c in sorted(cs.constraints, key=lambda c: c.net):
        if c.current is None or c.net in poured:
            continue
        mine = sorted((v for v in vias if v["net"] == c.net), key=lambda v: v["at"])
        if not mine:
            continue
        dt = c.current.temp_rise_c
        for group in _clusters(mine):
            drill = min(mine[i]["drill"] for i in group)
            carries = round(len(group) * via_amps(drill, stack.via_plating_mm, dt), 3)
            need = vias_per_change(c.current.amps, drill, stack.via_plating_mm, dt)
            out.append(
                Parallel(
                    net=c.net,
                    amps=float(c.current.amps),
                    at=mine[group[0]]["at"],
                    n=len(group),
                    drill=drill,
                    carries=carries,
                    need=need,
                    add=max(0, need - len(group)),
                )
            )
    return tuple(out)


def parallel_lines(rows: Sequence[Parallel]) -> list[str]:
    """The under-rated groups, then a summary — or the sentence that says there are none."""
    short = [r for r in rows if r.add]
    if not rows:
        return ["parallel: no via on any unpoured power net, so there is no group to rate"]
    if not short:
        return [f"parallel: {len(rows)} via group(s) on unpoured power nets, every one rated for its declared current"]
    nets = ", ".join(sorted({r.net for r in short}))
    return [r.line() for r in short] + [
        f"parallel: {len(short)} of {len(rows)} via group(s) under their rail's declared current ({nets}); "
        f"{sum(r.add for r in short)} more via(s) would carry it"
    ]


# --- Technique 1's verify half: a rung that is a rung ----------------------------------------------


@dataclass(frozen=True)
class Rung:
    """One `parallel` twin pcbc wrote, its anchor, and whether the two are one conductor."""

    net: str
    at: Pt  # the twin
    anchor: Pt  # the via it was placed beside
    mm: float  # centre to centre
    joined: tuple[str, ...]  # the layers on which the two are in one connected component
    spans: tuple[str, ...]  # the layers the twin's **barrel** spans, and must therefore be joined on
    linked: tuple[str, ...]  # the layers this run wrote a link segment on, for the report
    owner: str

    @property
    def anchored(self) -> bool:
        return self.anchor != self.at

    @property
    def ok(self) -> bool:
        """Joined on **every layer the barrel spans**, not on every layer a link happened to be
        written for. Asking the weaker question would let a rung that wrote one link pass for having
        written one link, which is a check that can only ever confirm what the pattern did."""
        return self.anchored and bool(self.spans) and all(L in self.joined for L in self.spans)

    def to_dict(self) -> dict:
        return {"net": self.net, "at": list(self.at), "anchor": list(self.anchor), "mm": self.mm, "joined": list(self.joined), "spans": list(self.spans), "linked": list(self.linked), "owner": self.owner}

    def line(self) -> str:
        if not self.anchored:
            return f"{self.net} rung at ({self.at[0]:g},{self.at[1]:g}): no via of its own net within reach of it, so it is a lone barrel and not a parallel one"
        missing = [L for L in self.spans if L not in self.joined]
        if not missing:
            return f"{self.net} rung at ({self.at[0]:g},{self.at[1]:g}): joined to its anchor {self.mm:.4g} mm away on {', '.join(self.spans)}"
        return (
            f"{self.net} rung at ({self.at[0]:g},{self.at[1]:g}) is {self.mm:.4g} mm from its anchor at "
            f"({self.anchor[0]:g},{self.anchor[1]:g}) and joined to it on {', '.join(self.joined) or 'no layer'}, not on "
            f"{', '.join(missing)} — it carries no current the anchor does not already carry, and "
            f"ampacity._via_clusters counts it anyway"
        )


def parallel_joined(text: str, pieces: Sequence[Piece]) -> tuple[Rung, ...]:
    """Every `stitch` via pcbc wrote, against the anchor it is supposed to be parallel to.

    **The single most important check in the feature**, and it exists because the measurement it
    protects cannot protect itself. `ampacity._via_clusters` is single-linkage on `math.dist(...) <=
    VIA_PARALLEL_MM` with **no connectivity test of any kind**, and `net_nodes` then hands every
    member `cluster_size x via_amps(drill)`. A twin dropped 0.9 mm from an anchor and joined to
    nothing would double the reported ampacity of a board carrying no more current — the tool would
    write copper that buys nothing and then report that it worked. Nothing else in the codebase can
    catch that: KiCad's unconnected-items check is pad to pad, so a via on a named net welded to
    nothing passes DRC, `netcheck.check_copper` (which reads pad bindings inside footprint blocks and
    never a segment or a via) and `verify_copper` alike.

    So the question asked here is not distance. It is: **is the twin in the same connected copper
    component as its anchor, on each of the layers its own links claim to join?** Per layer, because
    a via spans the stack and a component walk that lets a path leave through a *different* layer
    would call a twin joined by one link on one side "joined", when what makes two barrels parallel
    is that both ends of both of them are the same two nodes. The walk is `_reaches` over
    `_net_copper`, filtered to the layer — `chain_order`'s own reachability, which is `clears(a, b,
    0.0)`: copper pcbc and KRT write meets exactly, so the epsilon decides nothing.

    The layers asked about are the **barrel's own span**, never the layers a link was written for:
    asking the weaker question would let a rung that wrote one link pass for having written one link.

    Measured on node, 2026-09-21: its one rung at (28.9,33.4) is 0.9 mm from the `VBUS` barrel at
    (28,33.4) and joined to it on **both** F.Cu and B.Cu — and with the two link segments deleted
    from the same board it is joined on **neither**, which is the fixture
    `tests/test_stitch.py::test_a_twin_with_no_links_is_not_parallel_and_the_gate_says_so` pins.
    """
    twins = [p for p in pieces if p.kind == "via" and p.reason == "stitch"]
    if not twins:
        return ()
    mine = {(p.a[0], p.a[1]) for p in twins}
    out: list[Rung] = []
    by_net: dict[str, tuple] = {}
    for p in sorted(twins, key=lambda p: (p.net, p.a)):
        nodes = by_net.setdefault(p.net, _net_copper(text, p.net))
        spans = tuple(sorted(p.layer))
        linked = tuple(sorted({v.layer for v in pieces if v.kind == "seg" and v.reason == "stitch" and v.net == p.net and p.a in (v.a, v.b)}))
        anchor, mm = _nearest_via(nodes, p.a, mine, VIA_PARALLEL_MM)
        if anchor is None:
            out.append(Rung(net=p.net, at=p.a, anchor=p.a, mm=0.0, joined=(), spans=spans, linked=linked, owner=p.owner))
            continue
        joined = []
        for layer in spans:
            # Only this layer's copper: a via spans the stack, so a walk over every layer would call
            # a twin joined by one link on one side "joined", and what makes two barrels parallel is
            # that both ends of both of them are the same two nodes.
            here = [i for i, n in enumerate(nodes) if layer in n[1]]
            src = [i for i in here if nodes[i][0] == "via" and math.dist(nodes[i][3][0], p.a) < 1e-9]
            dst = [i for i in here if nodes[i][0] == "via" and math.dist(nodes[i][3][0], anchor) < 1e-9]
            sub = [nodes[i] for i in here]
            remap = {i: k for k, i in enumerate(here)}
            if src and dst and _reaches(sub, [remap[i] for i in src], [remap[i] for i in dst]):
                joined.append(layer)
        out.append(Rung(net=p.net, at=p.a, anchor=anchor, mm=round(mm, 4), joined=tuple(joined), spans=spans, linked=linked, owner=p.owner))
    return tuple(out)


def _nearest_via(nodes: Sequence[tuple[str, frozenset[str], Shape, tuple]], at: Pt, exclude: set[Pt], reach: float) -> tuple[Pt | None, float]:
    """The nearest via of this net to `at` that pcbc did not write itself, within `reach`.

    `exclude` is every twin of this run, so a pair of rungs around one anchor is each measured
    against the barrel KRT placed rather than against each other — which is the same exclusion
    `_anchor_via` makes and for the same reason: two rungs joined only to one another are a cluster
    the walk counts and the rail does not have.
    """
    best: tuple[Pt | None, float] = (None, 0.0)
    for kind, _layers, _shape, extra in nodes:
        if kind != "via":
            continue
        here = extra[0]
        # Compared as a distance, not as a key: `_net_copper` reads the six decimals KiCad writes
        # and a `Piece` carries the quantised double they were written from, and the two are equal
        # for every coordinate pcbc emits — but a tolerance is the honest way to say "the same via".
        if here == at or any(math.dist(here, m) < 1e-9 for m in exclude):
            continue
        d = math.dist(here, at)
        if d > reach:
            continue
        if best[0] is None or d < best[1]:
            best = (here, d)
    return best


def parallel_joined_lines(rows: Sequence[Rung]) -> list[str]:
    """One line per rung, then the tally — or the sentence that says pcbc wrote none."""
    if not rows:
        return ["barrels: pcbc placed no parallel via on this board"]
    bad = [r for r in rows if not r.ok]
    return [r.line() for r in rows] + [
        f"barrels: {len(rows) - len(bad)} of {len(rows)} rung(s) joined to their anchor on every layer they span"
    ]


# --- Technique 3: the guard, verified where it was written ----------------------------------------
#
# `docs/stitch-plan.md` section 6 row 3 and S7. A guard is the one pattern whose failure is **invisible
# to every other judge on the board**: KiCad's unconnected-items check is pad to pad, so a floating
# track on a named net passes DRC; `netcheck.check_copper` reads pad bindings inside footprint blocks
# and never a segment or a via; `verify_copper` asks about clearance, angle and size. A shield welded
# to nothing looks exactly like a shield, and measures like one in the copper bar.


@dataclass(frozen=True)
class GuardRun:
    """One `Guard()`'s copper as the finished board has it.

    Two different questions, and only the first can fail a build. **Welded** is whether each piece of
    the shield reaches the ground pour: a guard segment is ground copper only because a stitch via
    ties it to the plane, so a stretch whose component holds no via inside a filled zone is an island,
    and that is a pcbc bug rather than a board fact. **Covered** is how much of the run got a shield at
    all, which is R-S3's target half: a blocked stretch is dropped by design, so a coverage under 1 is
    a measurement and never a failure.
    """

    net: str  # the guarded net, from the pieces' owner
    ground: str
    layer: str
    segments: int
    vias: int
    welded: int  # vias whose whole ring is inside a filled zone of the ground net
    reached: int  # segments in a connected component that holds a welded via
    mm: float  # the shield actually written
    zoned: bool  # does the ground net have a filled zone on this board at all?

    @property
    def ok(self) -> bool:
        return self.zoned and self.welded == self.vias and self.reached == self.segments and self.vias > 0

    def to_dict(self) -> dict:
        return {
            "net": self.net, "ground": self.ground, "layer": self.layer, "segments": self.segments,
            "vias": self.vias, "welded": self.welded, "reached": self.reached, "mm": self.mm, "zoned": self.zoned,
        }

    def line(self) -> str:
        if not self.zoned:
            return f"guard {self.net}: {self.ground} has no filled zone on this board, so all {self.mm:g} mm of the shield is floating copper"
        if self.ok:
            return f"guard {self.net}: {self.mm:g} mm of {self.ground} on {self.layer} in {self.segments} stretch(es), all welded by {self.vias} stitch via(s)"
        return (
            f"guard {self.net}: {self.segments - self.reached} of {self.segments} stretch(es) and "
            f"{self.vias - self.welded} of {self.vias} stitch via(s) do not reach the {self.ground} pour — "
            f"{self.mm:g} mm of copper that shields nothing and that no other check on this board can see"
        )


def guard_cover(text: str, pieces: Sequence[Piece]) -> tuple[GuardRun, ...]:
    """Every `guard` piece pcbc wrote, against the pour that is supposed to make it ground.

    The via first: its **ring**, `RING_SAMPLES` points of it and its centre, inside a filled zone of
    its own net — `plane_checks`' own test, asked of a different reason, and asked of the ring because
    a via half out of the pour is welded by a sliver of annulus or by nothing (finding 12, S5's
    review). Then the segments: each is welded when its connected component — `_reaches` over
    `_net_copper` filtered to its layer, the same walk `parallel_joined` uses — contains one of those
    vias. Per layer, because a guard is a flat thing and a component walk that may leave through
    another layer would call a stretch welded on the strength of a via at the other end of the board.

    A `Guard()` that wrote nothing at all has no row here and is not a failure: a net KRT routed is
    B.6's deferral, printed as a note by the pattern, and a run whose every stretch was blocked is
    R-S3's target half. What this catches is copper pcbc **did** write and cannot account for.
    """
    mine = [p for p in pieces if p.reason == "guard"]
    if not mine:
        return ()
    zs = [z for z in zones(text) if z.polys]
    rows: list[GuardRun] = []
    for owner in sorted({p.owner for p in mine}):
        here = [p for p in mine if p.owner == owner]
        net = owner.split(" guard")[0]
        ground = here[0].net
        layer = next((str(p.layer) for p in here if p.kind == "seg"), "F.Cu")
        pour = [z for z in zs if z.net == ground]
        vias = [p for p in here if p.kind == "via"]
        segs = [p for p in here if p.kind == "seg"]
        welded = [p for p in vias if _ring_in_zone(p, pour)]
        nodes = _net_copper(text, ground)
        idx = [i for i, n in enumerate(nodes) if layer in n[1]]
        sub = [nodes[i] for i in idx]
        remap = {i: k for k, i in enumerate(idx)}
        want = {_at4(p.a) for p in welded}
        dst = [remap[i] for i in idx if nodes[i][0] == "via" and _at4(nodes[i][3][0]) in want]
        reached = 0
        for s in segs:
            src = [remap[i] for i in idx if nodes[i][0] == "seg" and _same_seg(nodes[i][3], s)]
            if src and dst and _reaches(sub, src, dst):
                reached += 1
        rows.append(
            GuardRun(
                net=net, ground=ground, layer=layer, segments=len(segs), vias=len(vias),
                welded=len(welded), reached=reached, mm=round(sum(p.mm for p in segs), 4), zoned=bool(pour),
            )
        )
    return tuple(rows)


def _ring_in_zone(p: Piece, pour: Sequence[Zone]) -> bool:
    """`plane_checks`' containment, factored so the tap branch keeps its byte-identical loop."""
    r = (p.w or 0.0) / 2.0
    ring = [p.a] + ([(p.a[0] + r * math.cos(2 * math.pi * k / RING_SAMPLES), p.a[1] + r * math.sin(2 * math.pi * k / RING_SAMPLES)) for k in range(RING_SAMPLES)] if r > 0 else [])
    return bool(pour) and all(any(in_zone(z, pt) for z in pour) for pt in ring)


def _at4(pt: Pt) -> tuple[float, float]:
    """`copper_bar.bar_key`'s rounding, and it is the only spelling that can match here.

    The sidecar's key is `route_emit.piece_key`, rounded to **4 dp** on purpose — it has to survive
    KiCad's refill-and-save and `pin_copper_ids`, so it cannot carry the six decimals the file does.
    Measured 2026-09-21, the board writes the fixture's north flank as `8.819899` where the sidecar
    says `8.8199`, so a `1e-9` distance test — the one `_nearest_via` can afford, because a via centre
    round-trips exactly — matched **none** of four guard segments and `guard_cover` reported every
    stretch as an orphan. Rounding both sides to the key's own precision is exact, and 0.1 um is four
    orders below anything on this board that could be two different pieces of copper."""
    return (round(pt[0], 4), round(pt[1], 4))


def _same_seg(extra: tuple, p: Piece) -> bool:
    """Is this parsed segment the piece pcbc wrote? Both ends, in either order (`bar_key`)."""
    a, b, _w = extra
    return {_at4(a), _at4(b)} == {_at4(p.a), _at4(p.b or p.a)}


def guard_lines(rows: Sequence[GuardRun]) -> list[str]:
    """One line per guarded net, then the tally — or the sentence that says pcbc wrote none."""
    if not rows:
        return ["guards: pcbc wrote no guard copper on this board"]
    bad = [r for r in rows if not r.ok]
    return [r.line() for r in rows] + [f"guards: {len(rows) - len(bad)} of {len(rows)} shield(s) welded to their pour on every piece"]



# --- Technique 2: the plane lattice, verified on the pours it is supposed to tie -------------------
#
# `docs/router-plan.md` R-E1 and `docs/stitch-plan.md` section 8 item 1, S8. One claim, and it is the
# only one on this board that nothing else can make: **every lattice barrel lands in BOTH pours.** A
# via welded to the front plane and standing in a clearance hole in the back one still reads as
# connected — KiCad's unconnected-items check is pad to pad, `netcheck.check_copper` reads pad
# bindings inside footprint blocks and never a via, `verify_copper` asks about clearance, angle and
# size — and it ties the two planes not at all, which is the entire technique. What the gate must not
# fail on is the pour *area*: a lattice costs plane copper by construction and `plane_area` is the
# number that says how much.


@dataclass(frozen=True)
class PlaneStitch:
    """One net's plane lattice as the finished board has it.

    Three numbers, and only the first can fail a build. **Welded** is how many barrels have their
    whole ring inside a filled zone of their own net on **every** layer that net is poured on — the
    claim R-E1 makes and the one nothing else here can check. **Islands** and **area** are what the
    lattice cost the pours it ties: a plane that came back in two pieces has had a fragment cut off it
    (D.5), and the mm2 is what `test_examples_fab.py::PLANES` pins so a flood shows up as a moved
    number rather than as a shrug.
    """

    net: str
    layers: tuple[str, ...]  # every layer this net came back poured on, sorted
    vias: int
    welded: int  # rings inside a filled zone of this net on every one of `layers`
    islands: int  # the **worst** island count of any one of `layers`, not their sum
    area_mm2: float  # the filled copper of every one of `layers`, summed
    zoned: bool  # did this net come back with a filled zone on two or more layers at all?

    @property
    def ok(self) -> bool:
        return self.zoned and self.vias > 0 and self.welded == self.vias

    def to_dict(self) -> dict:
        return {
            "net": self.net, "layers": list(self.layers), "vias": self.vias, "welded": self.welded,
            "islands": self.islands, "area_mm2": self.area_mm2, "zoned": self.zoned,
        }

    def line(self) -> str:
        where = " + ".join(self.layers) if self.layers else "nothing"
        if not self.zoned:
            return (
                f"plane {self.net}: {self.vias} lattice via(s) on a net that came back poured on {where} — "
                f"a lattice needs two facing pours and this board has fewer"
            )
        if self.ok:
            return (
                f"plane {self.net}: {self.vias} lattice via(s), all landing in {where}; "
                f"{self.area_mm2:g} mm2 of pour across {len(self.layers)} layer(s), "
                f"{self.islands} island(s) on the worst of them"
            )
        return (
            f"plane {self.net}: {self.vias - self.welded} of {self.vias} lattice via(s) do not land in "
            f"every pour they tie ({where}) — copper that stitches one plane to nothing, and no other "
            f"check on this board can see it"
        )


def plane_stitch(text: str, pieces: Sequence[Piece]) -> tuple[PlaneStitch, ...]:
    """Every `plane` piece pcbc wrote, against the pours it is supposed to tie together.

    The ring and not the centre, for `plane_checks`' own reason (finding 12): a via half out of the
    pour is welded by a sliver of annulus or by nothing, so `RING_SAMPLES` points of the ring and the
    centre all have to be inside. And **every** poured layer of the net rather than any one of them,
    which is the whole difference between this and `plane_checks`: a tap only has to reach its own
    plane, while a lattice barrel exists to join two, so "inside a filled zone of its own net,
    whichever layer" would pass a via that ties the front pour to a clearance hole in the back one.

    The layers come from the **file** (`zones`), not from `Board(planes=)`: a declared pour that
    poured nothing is not a plane, which is `poured_planes`' own stance and the blinky trap of
    `docs/stitch-plan.md` section 7.1 read one technique over.

    A board where pcbc wrote no lattice has no row here and is not a failure — that is every board in
    this repo, because none of the five pours one net on two layers.
    """
    mine = [p for p in pieces if p.reason == "plane" and p.kind == "via"]
    if not mine:
        return ()
    zs = [z for z in zones(text) if z.polys]
    areas = plane_area(text)
    islands = plane_islands(text)
    rows: list[PlaneStitch] = []
    for net in sorted({p.net for p in mine}):
        here = [p for p in mine if p.net == net]
        lays = tuple(sorted({z.layer for z in zs if z.net == net}))
        welded = sum(1 for p in here if all(_ring_in_zone(p, [z for z in zs if z.net == net and z.layer == lay]) for lay in lays))
        rows.append(
            PlaneStitch(
                net=net,
                layers=lays,
                vias=len(here),
                welded=welded if len(lays) >= 2 else 0,
                islands=max([n for (zn, _lay), n in islands.items() if zn == net], default=0),
                area_mm2=round(sum(a for (zn, _lay), a in areas.items() if zn == net), 2),
                zoned=len(lays) >= 2,
            )
        )
    return tuple(rows)


def plane_stitch_lines(rows: Sequence[PlaneStitch]) -> list[str]:
    """One line per stitched net, then the tally — or the sentence that says pcbc wrote none."""
    if not rows:
        return ["planes: pcbc wrote no plane-stitch copper on this board"]
    bad = [r for r in rows if not r.ok]
    return [r.line() for r in rows] + [f"planes: {len(rows) - len(bad)} of {len(rows)} lattice(s) landing in every pour they tie"]

# --- Technique 4: the thermal array, verified where it was placed ---------------------------------
#
# `docs/stitch-plan.md` §6 row 4 and S6. Two claims, and both of them are about copper pcbc **itself**
# wrote, which is why the gate that runs this is fatal: pcbc's own via-in-pad detector must find every
# via pcbc deliberately put in a pad, and `fab.via_in_pad_blockers` must not have grown. If the first
# fails the array is not where it thinks it is; if the second fails, `Thermal()` let a passive through.


@dataclass(frozen=True)
class ThermalArray:
    """One `Thermal()` land as the finished board has it.

    `in_pad` and `in_plane` are the two containments the technique is made of and they are different
    questions with different failure modes. A via outside its **pad** is a barrel beside the land
    rather than under it: it still reaches the plane, still reads as connected, and moves none of the
    heat the array was placed for, which is the silent failure the whole check exists for. A via
    outside its **plane** is a barrel welded at one end — on two layers that is the fill retreating
    from a pocket (`pour_raster` predicts it at placement and this measures it after the refill), and
    on four it would mean the zone came back unfilled.
    """

    pad: str  # "U1.49"
    net: str
    want: int  # what `constraints.ThermalSpec.need` asked for
    got: int  # the vias on the board
    in_pad: int  # of `got`, how many have their whole ring inside one primitive of the land
    in_plane: int  # of `got`, how many have their whole ring inside the net's zone on the plane layer
    plane: str
    pitch_mm: float  # the closest two barrels of the array sit, centre to centre
    theta_c_per_w: float  # what `got` barrels achieve
    rise_c: float  # at the declared watts
    budget_c: float  # what `Thermal(rise_c=)` asked for

    @property
    def verdict(self) -> str:
        """`served` | `short` | `none` | `adrift`, worst last-resort first when they collide.

        `adrift` beats the count verdicts because it is the only one that is a **bug**: the other
        three are statements about how much room the land had, and `adrift` says a via pcbc placed is
        not where pcbc believes it is.
        """
        if self.got and (self.in_pad < self.got or self.in_plane < self.got):
            return "adrift"
        if self.got == 0:
            return "none"
        return "served" if self.got >= self.want else "short"

    def to_dict(self) -> dict:
        return {
            "pad": self.pad,
            "net": self.net,
            "want": self.want,
            "got": self.got,
            "in_pad": self.in_pad,
            "in_plane": self.in_plane,
            "plane": self.plane,
            "pitch_mm": self.pitch_mm,
            "theta_c_per_w": self.theta_c_per_w,
            "rise_c": self.rise_c,
            "budget_c": self.budget_c,
            "verdict": self.verdict,
        }

    def line(self) -> str:
        return (
            f"thermal {self.pad}: {self.got} of {self.want} barrel(s) on {self.net}, {self.in_pad} inside the land and "
            f"{self.in_plane} inside the {self.net} zone on {self.plane}, closest pair {self.pitch_mm:g} mm — "
            f"{self.theta_c_per_w:g} K/W, {self.rise_c:g} C of a {self.budget_c:g} C budget ({self.verdict})"
        )


def thermal_budget(text: str, pieces: Sequence[Piece], cs: ConstraintSet) -> tuple[ThermalArray, ...]:
    """Every compiled `Thermal()` measured on the finished board, in `board.py` order.

    The pieces are pcbc's own, off `copper.json`, and the containments are asked of the **saved and
    refilled** file — which is the only moment both halves exist: at placement the two-layer pour is
    written but unfilled and `pour_raster` can only predict it.

    **`plane_checks` is deliberately untouched by this** and that is a correction to
    `docs/stitch-plan.md` §6, which asked for it to be generalised to a reason set and made
    `joins`-aware. Measured: it cannot be. `plane_checks` walks `Piece`s reconstructed from the
    sidecar, and a `Piece` carries no `joins` — the field lives on `StitchSpec` and does not survive
    the round trip. Adding `"stitch"` to its reason set without that is worse than useless: it would
    ask node's **parallel** rung, whose net is `VBUS` and which is on an unpoured rail by
    construction, to be inside a `VBUS` zone that does not exist, and fail a build for it. The tap
    branch keeps its byte-identical body, this function owns the array's two containments, and the
    two never meet.

    The ring is `RING_SAMPLES` points plus the centre — `plane_checks`' own sampling and its own
    calibration, so "inside" means the same thing for a tap and for an array.
    """
    from .ampacity import board_pad_geoms

    out: list[ThermalArray] = []
    if cs is None or not cs.thermals:
        return ()
    zs = [z for z in zones(text) if z.polys]
    geoms = board_pad_geoms(text)
    # **The board's coordinates, not the sidecar's.** `route_emit.piece_key` rounds to 4 dp — a tenth
    # of a micron, which is a tenth of the finest grid anything here uses and is right for a key that
    # has to survive KiCad's rewrite. It is not right for a **pitch**: the lattice is laid out to the
    # nanometre (`patterns.stitch.next_nm`), so a 0.850101 mm pitch read off two 4-dp coordinates
    # comes back as 0.8501 or 0.8502 depending on where the land happens to sit on the board.
    # Measured on node 2026-09-21: the same array reported both. The file carries six decimals.
    exact = {(round(v["at"][0], 4), round(v["at"][1], 4)): v["at"] for v in board_vias(text)}
    for spec in cs.thermals:
        ids = set(spec.ids)
        land = [g for g in geoms if f"{g.ref}.{g.num}" in ids]
        mine = [p for p in pieces if p.kind == "via" and p.reason == "thermal" and p.net == spec.net and p.owner == spec.pad]
        zone = [z for z in zs if z.net == spec.net and z.layer == spec.plane]
        in_pad = in_plane = 0
        at_exact = [exact.get((round(p.a[0], 4), round(p.a[1], 4)), p.a) for p in mine]
        for p in mine:
            ring = _ring_of(p)
            if any(all(_inside(shape, pt) for pt in ring) for g in land for shape in g.copper):
                in_pad += 1
            if zone and all(any(in_zone(z, pt) for z in zone) for pt in ring):
                in_plane += 1
        got = len(mine)
        theta = spec.theta_via.value
        out.append(
            ThermalArray(
                pad=spec.pad,
                net=spec.net,
                want=spec.need,
                got=got,
                in_pad=in_pad,
                in_plane=in_plane,
                plane=spec.plane,
                pitch_mm=_closest_pair(at_exact),
                theta_c_per_w=round(theta / got, 2) if got else 0.0,
                rise_c=spec.rise_of(got),
                budget_c=round(spec.rise_c, 2),
            )
        )
    return tuple(out)


def _ring_of(p: Piece) -> list[Pt]:
    """A via's ring as `RING_SAMPLES` points plus its centre — `plane_checks`' own sampling."""
    r = (p.w or 0.0) / 2.0
    if r <= 0:
        return [p.a]
    return [p.a] + [(p.a[0] + r * math.cos(2 * math.pi * k / RING_SAMPLES), p.a[1] + r * math.sin(2 * math.pi * k / RING_SAMPLES)) for k in range(RING_SAMPLES)]


def _closest_pair(pts: Sequence[Pt]) -> float:
    """The nearest two of a handful of points, **6 dp**; 0.0 for fewer than two.

    Six and not four, which is `route_emit.piece_key`'s number, because this is a **pitch**: the
    lattice is laid out to the nanometre (`patterns.stitch.next_nm`) and 4 dp reports the same
    0.850101 mm array as 0.8501 or 0.8502 depending on where the land sits on the board. Measured on
    node 2026-09-21, it reported both.

    Quadratic, and that is the right algorithm here: the largest array any board in this repo places
    is twelve barrels, and a sweep line would be more code than the thing it measures.
    """
    best = 0.0
    for i, a in enumerate(pts):
        for b in pts[i + 1 :]:
            d = math.dist(a, b)
            if best == 0.0 or d < best:
                best = d
    return round(best, 6)


def thermal_lines(rows: Sequence[ThermalArray]) -> list[str]:
    """One line per land, or the sentence that says the board declares none."""
    if not rows:
        return ["thermal: this board declares no Thermal() land"]
    return [r.line() for r in rows]


# --- Technique 6: the ground bridge, verified and never written ------------------------------------
#
# `docs/stitch-plan.md` §6 row 6 and S3. KiCad owns the **short** check — `fab.copper_drc_errors`
# surfaces its `shorting_items` — and pcbc owns the *single-point* check, which KiCad cannot make
# because to KiCad two declared nets are simply separate and a part between them is a part. Nothing
# below writes copper, proposes copper, or asks any gate for an exemption; see `language.Bridge` for
# the two judges a copper tie would each need one from, and why it is refused rather than exempted.


COUPLING_CELL_MM = 0.02
"""The cell two grounds' facing copper is rastered at, in mm.

A numerical-method constant, like `POUR_CELL_MM` and `RING_SAMPLES`, and calibrated the way they
were. The area wanted is the intersection of a union of capsules and round-rects with a filled zone
polygon, and this repo has no polygon-boolean library; a raster is the deterministic answer and the
only question is the cell. Swept on the DS2 Addon's routed board, 2026-09-21, `VSS` copper on F.Cu
against the `GND` pour on B.Cu:

    cell 0.05 -> 20.6500 mm2, 0.5497 pF, 0.12 s
    cell 0.02 -> 20.0520 mm2, 0.5338 pF, 0.60 s
    cell 0.01 -> 20.0627 mm2, 0.5341 pF, 2.20 s
    cell 0.005 -> 20.0083 mm2, 0.5326 pF, 8.50 s

Halving it twice from 0.02 moves the answer by 0.0012 pF (0.2 %) and multiplies the cost by
fourteen. 0.05 is the one that is wrong — cell-centre sampling of a 0.25 mm track at 0.05 mm biases
3 % high. The raster runs only for a pair of grounds with **no** on-board tie and only when one of
them has a filled zone, so it costs nothing at all on every board in this repo but that one.
"""

EPS0_F_PER_M = 8.8541878128e-12
"""The permittivity of free space, CODATA 2018. The one constant in this file that is not a
measurement of a board, and the only number a parallel-plate capacitance needs that the stackup does
not already carry (`Stackup.dielectric_between` supplies both the height and the series Dk)."""


@dataclass(frozen=True)
class BridgeTie:
    """One pair of `Ground()` nets, and what joins them on this finished board.

    `verdict` is one of five:

    - `tied` — exactly one two-pin part joins them and the copper feeds both of its pads.
    - `multi` — more than one part joins them, so the tie is not one point. **The only fatal one.**
    - `open` — a part joins them in the netlist but the copper does not reach one of its pads.
    - `off_board` — declared `kind="off_board"`: the tie is made elsewhere, by assertion.
    - `none` — nothing joins them. This is the DS2 Addon, and it is why the statement exists.
    """

    a: str
    b: str
    tie: str  # "R11", or "" when nothing on this board ties them
    at: tuple[str, ...]  # the two pads the tie ties, or the two that leave the board
    kind: str  # the declared `Bridge(kind=)`, or "" when no `Bridge()` was written
    verdict: str
    others: tuple[str, ...]  # every other two-pin part joining the pair, sorted
    closest: tuple[float, str, str] | None  # (mm, a's copper, b's copper) at their nearest approach
    facing_mm2: float  # copper of one facing a filled zone of the other, 0.0 when not computed
    coupling_pf: float
    why: str
    move: str

    def to_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "tie": self.tie,
            "at": list(self.at),
            "kind": self.kind,
            "verdict": self.verdict,
            "others": list(self.others),
            "closest": [self.closest[0], self.closest[1], self.closest[2]] if self.closest else None,
            "facing_mm2": self.facing_mm2,
            "coupling_pf": self.coupling_pf,
            "why": self.why,
            "move": self.move,
        }

    def line(self) -> str:
        near = f"; closest approach {self.closest[0]:.4f} mm ({self.closest[1]} to {self.closest[2]})" if self.closest else ""
        coup = f"; {self.facing_mm2:g} mm2 of them face each other at {self.coupling_pf:g} pF" if self.coupling_pf else ""
        return f"{self.a}/{self.b} [{self.verdict}]: {self.why}{near}{coup}." + (f" {self.move}" if self.move else "")


def _ground_pairs(design) -> tuple[tuple[str, str], ...]:
    grounds = sorted(n.name for n in getattr(design, "nets", {}).values() if getattr(n, "kind", "") == "ground")
    return tuple((a, b) for i, a in enumerate(grounds) for b in grounds[i + 1 :])


def _pad_fed(text: str, net: str, pad_id: str, by_net: dict, zs: Sequence[Zone]) -> bool:
    """Does the rest of `net`'s copper reach this pad?

    Two ways, and a poured net needs the second: `_reaches` floods touching copper the way
    `chain_order` does, and a net with a filled zone joins every pad that **shares a layer with the
    fill** and sits in it, which no track-and-via flood can see.

    The layer test is the whole of the second branch and `plane_checks` deliberately does not make
    it — there it is a *via*, which spans every copper layer, so "any filled zone of its own net,
    whichever layer" is exact. A pad is not a via: an SMD pad on F.Cu lying over a B.Cu pour of its
    own net is joined to that pour by nothing, and reading it as fed would report `tied` on a board
    whose tie is a floating pad. Measured on the DS2 Addon 2026-09-21: of `GND`'s five pads the two
    through-hole ones (`J1.2`, `J3.3`) sit in the B.Cu pour and the three SMD ones do not, and `VSS`
    has no pour at all so none of its nine do. On that board the branch changes no verdict — the
    flood reaches all fourteen pads as well — and it is written for the pad whose **only** feed is
    the plane, which is exactly what a plane-fed through-hole pad is.
    """
    pads = by_net.get(net, {})
    if pad_id not in pads:
        return False
    mine = [z for z in zs if z.net == net and z.polys and z.layer in pads[pad_id].cu_layers]
    if mine and any(in_zone(z, pads[pad_id].at) for z in mine):
        return True
    others = [p for p in pads if p != pad_id]
    if not others:
        return False
    nodes = list(_net_copper(text, net))
    idx: dict[str, list[int]] = {}
    for pid in sorted(pads):
        idx[pid] = []
        for sh in pads[pid].copper:
            idx[pid].append(len(nodes))
            nodes.append(("pad", pads[pid].cu_layers, sh, (pid,)))
    return _reaches(nodes, idx[pad_id], [i for pid in others for i in idx[pid]])


def _closest_copper(text: str, a: str, b: str, by_net: dict) -> tuple[float, str, str] | None:
    """The nearest approach of two nets' copper, edge to edge, over pieces that share a layer.

    Every track, via and pad, because the question is how close the two domains get anywhere — not
    how close two pads get, which is `route_checks._closest_cross_ref`'s question about where a part
    could go. Measured on the DS2 Addon 2026-09-21: **0.2250 mm**, between the GND track on F.Cu from
    (22.7,13.8) to (24,13.8) and the VSS via at (23.2,13.2) — neither of them a pad, which is why
    the placement's own answer (0.3070 mm, `U1.4` to `U1.5`) is a different number about a different
    thing.
    """

    def pieces(net: str) -> list[tuple[frozenset[str], Shape, str]]:
        out: list[tuple[frozenset[str], Shape, str]] = []
        for kind, layers, shape, extra in _net_copper(text, net):
            if kind == "via":
                what = f"{net} via at ({extra[0][0]:g},{extra[0][1]:g})"
            else:
                what = f"{net} track on {sorted(layers)[0]} ({extra[0][0]:g},{extra[0][1]:g})-({extra[1][0]:g},{extra[1][1]:g})"
            out.append((layers, shape, what))
        for pid, p in sorted(by_net.get(net, {}).items()):
            for sh in p.copper:
                out.append((p.cu_layers, sh, f"{net} pad {pid}"))
        return out

    best: tuple[float, str, str] | None = None
    pb = pieces(b)
    for la, sa, wa in pieces(a):
        box_a = aabb(sa)
        for lb, sb, wb in pb:
            if not (la & lb):
                continue
            box_b = aabb(sb)
            if box_a[0] - box_b[2] > 1.0 or box_b[0] - box_a[2] > 1.0 or box_a[1] - box_b[3] > 1.0 or box_b[1] - box_a[3] > 1.0:
                if best is not None:
                    continue
            d = math.sqrt(max(0.0, hull_dist2(sa.pts, sb.pts))) - sa.r - sb.r
            key = (round(max(d, 0.0), 6), wa, wb)
            if best is None or key < best:
                best = key
    return best


def _zone_spans(polys: Sequence[Sequence[Pt]], y: float) -> list[float]:
    xs: list[float] = []
    for poly in polys:
        n = len(poly)
        for i in range(n):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % n]
            if (y0 > y) != (y1 > y):
                xs.append(x0 + (y - y0) / (y1 - y0) * (x1 - x0))
    xs.sort()
    return xs


def _inside_spans(xs: Sequence[float], x: float) -> bool:
    lo, hi = 0, len(xs)
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo % 2 == 1


def _facing_area(shapes: Sequence[Shape], zone: Zone, cell: float) -> float:
    """mm2 of `shapes`' union that lies over `zone`'s fill, rastered at `cell` — see `COUPLING_CELL_MM`.

    Scanline, not point-in-polygon per cell: the DS2 Addon's GND pour is one 1392-vertex island and
    asking it about 150 000 cells one at a time is the whole cost. The crossings are computed once
    per row and the cell's answer is a binary search in them, which is what makes 0.02 mm affordable.
    """
    rows: dict[int, list[tuple[Shape, tuple[float, float, float, float]]]] = {}
    for sh in shapes:
        box = aabb(sh)
        for j in range(int(math.floor(box[1] / cell)), int(math.ceil(box[3] / cell)) + 1):
            rows.setdefault(j, []).append((sh, box))
    cells = 0
    for j in sorted(rows):
        cy = (j + 0.5) * cell
        xs = _zone_spans(zone.polys, cy)
        if not xs:
            continue
        cols: set[int] = set()
        for sh, box in rows[j]:
            lim = sh.r * sh.r + MICRO_MM * MICRO_MM * 1e-12
            for i in range(int(math.floor(box[0] / cell)), int(math.ceil(box[2] / cell)) + 1):
                if i in cols:
                    continue
                cx = (i + 0.5) * cell
                if hull_dist2(((cx, cy),), sh.pts) <= lim:
                    cols.add(i)
        cells += sum(1 for i in cols if _inside_spans(xs, (i + 0.5) * cell))
    return cells * cell * cell


def _coupling(text: str, cs: ConstraintSet, a: str, b: str, by_net: dict, zs: Sequence[Zone]) -> tuple[float, float]:
    """(facing mm2, pF, one sentence) for two grounds that nothing on this board ties.

    A parallel-plate sum over the layer pairs that actually face each other: every filled zone of one
    net against the other net's copper on a **different** copper layer, at the height and series Dk
    `Stackup.dielectric_between` gives for that pair. No fringing, no proximity — a plate capacitor
    is an underestimate of the coupling, which is the right direction for a number whose job is to
    say "these two are not isolated, they are a capacitor".

    Measured on the DS2 Addon 2026-09-21: **20.052 mm2 of VSS copper on F.Cu faces the 1012.099 mm2
    GND pour on B.Cu across 1.53 mm of FR4 at Dk 4.6 — 0.5338 pF**, and no DC path at all.
    `docs/stitch-plan.md` §6 row 6 recorded 21.600 mm2 and 0.575 pF from the panel; the area does not
    reproduce (the 64.5 % it quotes is of *all* VSS copper, both layers, while only F.Cu faces the
    pour) and these are the numbers this session measured.
    """
    total = 0.0
    farads = 0.0
    for zone in zs:
        if not zone.polys:
            continue
        other = b if zone.net == a else a if zone.net == b else None
        if other is None:
            continue
        for layer in cs.stackup.copper_layers():
            if layer == zone.layer:
                continue
            shapes = [sh for _k, ls, sh, _e in _net_copper(text, other) if layer in ls]
            shapes += [sh for p in by_net.get(other, {}).values() if layer in p.cu_layers for sh in p.copper]
            if not shapes:
                continue
            area = _facing_area(shapes, zone, COUPLING_CELL_MM)
            if area <= 0.0:
                continue
            h, dk = cs.stackup.dielectric_between(zone.layer, layer)
            total += area
            farads += EPS0_F_PER_M * dk * (area * 1e-6) / (h * 1e-3)
    return round(total, 3), round(farads * 1e12, 4)


def bridge_ties(text: str, design, cs: ConstraintSet, *, coupling: bool = True) -> tuple[BridgeTie, ...]:
    """Every pair of `Ground()` nets on a finished board, and what ties them — one row each.

    **The four questions, and (ii) is the one that needs the copper.**

    1. *Joined.* Some two-pin instance has one pad on each net, and the copper feeds both of its
       pads — `_pad_fed`, which is `chain_order`'s `_reaches` plus the plane branch a poured net
       needs. A part that is in the netlist and whose pad no copper reaches is `open`.
    2. *Through, not past.* Removing the declared tie leaves the pair joined by nothing else. This is
       `chain_order`'s "through, not past" construction lifted one level: there the thing removed is
       a pad's copper **region**, here it is a part, because a tie is a part and its two pads do not
       touch each other — there is a component body between them and no cut-vertex test on copper
       would ever see it. `multi` is the failure, and it is the only fatal verdict.
    3. *At the closest approach.* `route_checks.check_bridge` asks this of the placement, where it
       ends in a `Place()` edit. What is here is the copper's own answer: how close the two domains
       get anywhere on the board (`_closest_copper`).
    4. *Coupled anyway.* For a pair nothing ties, the parallel-plate capacitance between them
       (`_coupling`). A split with no DC tie is not an isolated domain; it is a capacitor with no
       defined DC potential across it, and that number is what says so.

    Measured on the DS2 Addon 2026-09-21, which is the board the statement exists for: `GND`/`VSS`,
    verdict **none**, closest approach **0.2250 mm**, **20.052 mm2** of VSS copper facing the GND
    pour at **0.5338 pF**. Silent on all four example boards — each declares one `Ground()`.

    **`netcheck.py` has a zero diff and that is the point of the slice.** `copper_nets` reads pad
    bindings inside footprint blocks and never a `(segment)` or a `(via)`, so a tie made of a part
    changes no binding and the arbiter stays the arbiter.

    `coupling=False` skips question 4 and with it the only expensive thing here — measured, 0.63 s
    with it and 0.02 s without, on the DS2 Addon. `copper_bar` passes it: the bar records *who* ties
    two grounds, and a picofarad is not a number a copper census has any business in.
    """
    from .circuit import _two_pin_ties

    pairs = _ground_pairs(design)
    if not pairs:
        return ()
    from .ampacity import board_pad_geoms

    by_net: dict[str, dict] = {}
    for p in board_pad_geoms(text):
        by_net.setdefault(p.net, {})[p.id] = p
    zs = [z for z in zones(text) if z.polys]
    declared = {frozenset((sp.a, sp.b)): sp for sp in cs.bridges}
    out: list[BridgeTie] = []
    for a, b in pairs:
        spec = declared.get(frozenset((a, b)))
        joined = _two_pin_ties(design, a, b)
        closest = _closest_copper(text, a, b, by_net)
        facing = coupled = 0.0
        kind = spec.kind if spec is not None else ""
        if spec is not None and spec.kind == "off_board":
            out.append(BridgeTie(a, b, "", spec.at, kind, "off_board", tuple(joined), closest, 0.0, 0.0,
                                 f"{a} and {b} are declared tied off this board at {' and '.join(spec.at)} ({spec.why})", ""))
            continue
        tie = spec.tie_ref if spec is not None and spec.tie_ref else (joined[0] if len(joined) == 1 else "")
        others = tuple(r for r in joined if r != tie)
        if not joined:
            if coupling:
                facing, coupled = _coupling(text, cs, a, b, by_net, zs)
            else:
                facing, coupled = 0.0, 0.0
            why = f"nothing on this board joins {a} to {b}: no two-pin part has one pad on each"
            move = (
                f'A split with no DC tie is a capacitor with no defined potential across it. Resistor("R11", "0R", p1={a}, p2={b}) '
                f'then Bridge("{a}", "{b}", at="R11"); or Bridge("{a}", "{b}", at=(...), kind="off_board", why="...") if the tie is made elsewhere.'
            )
            out.append(BridgeTie(a, b, "", (), kind, "none", (), closest, facing, coupled, why, move))
            continue
        if others:
            out.append(BridgeTie(
                a, b, tie, spec.pads if spec is not None else (), kind, "multi", others, closest, 0.0, 0.0,
                f"{', '.join(joined)} all join {a} to {b}, so the tie is {len(joined)} points and not one",
                "Drop all but one, or drop the Bridge() line: a single-point tie that is not single is a ground loop with a part number.",
            ))
            continue
        pads = spec.pads if spec is not None and spec.pads else _tie_pads(design, tie, a, b)
        unfed = [pid for pid, net in zip(pads, (a, b)) if not _pad_fed(text, net, pid, by_net, zs)]
        if unfed:
            out.append(BridgeTie(
                a, b, tie, pads, kind, "open", (), closest, 0.0, 0.0,
                f"{tie} joins {a} to {b} in the netlist but no copper reaches {', '.join(unfed)}",
                "KiCad's own unconnected-items check fails this build before the fab step; the line is here so the reason names the tie.",
            ))
            continue
        out.append(BridgeTie(a, b, tie, pads, kind, "tied", (), closest, 0.0, 0.0,
                             f"{tie} is the one point joining {a} to {b}, {pads[0]} on {a} and {pads[1]} on {b}, and the copper feeds both", ""))
    return tuple(out)


def _tie_pads(design, ref: str, a: str, b: str) -> tuple[str, ...]:
    """`("R11.1", "R11.2")` for the part that ties `a` to `b`, a-side first — the resolution
    `constraints.BridgeSpec` does for a declared `Bridge()`, for a tie nobody declared."""
    inst = next((i for i in getattr(design, "instances", ()) if i.ref == ref), None)
    if inst is None:
        return ()
    got: dict[str, str] = {}
    for pin, net in sorted(inst.pins.items()):
        if net not in (a, b) or net in got:
            continue
        nums = inst.part.pins[pin].pads if pin in inst.part.pins else (pin,)
        if nums:
            got[net] = f"{ref}.{nums[0]}"
    return (got.get(a, ""), got.get(b, "")) if a in got and b in got else ()


def bridge_lines(rows: Sequence[BridgeTie]) -> list[str]:
    """One line per ground pair, in net order — what `build._bridge_gate` prints."""
    return [r.line() for r in rows]
