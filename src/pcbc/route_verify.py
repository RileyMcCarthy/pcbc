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

from .constraints import ConstraintSet
from .route_emit import Piece
from .route_geom import MICRO_MM, Box, Pt, Shape, aabb, clears, clip_len_in_box, hull_dist2, is_octilinear, legs_ok, q, seg_lengths, track_shape, turn_ok, via_shape
from .route_scene import Item, Scene, clashes
from .sexp import matching_paren

__all__ = [
    "CHAIN_BISECT",
    "POUR_CELL_MM",
    "ChainVerdict",
    "PourRaster",
    "Zone",
    "chain_order",
    "in_zone",
    "lane_overrun",
    "via_in_lane",
    "paths_of",
    "plane_checks",
    "plane_islands",
    "plane_area",
    "poly_area",
    "pour_raster",
    "verify_copper",
    "zones",
]

BRANCH_REASONS = ("fanout",)
"""A pattern via that is a **branch** uses the fab's standard via (`stack.via_diameter`); one that
carries the net's current across layers uses the class via (`Constraint.via`). B.0's rule, and the
argument is that a branch carries one pad's share — a decoupling cap's ripple, not the rail's 1 A —
while `ViaSpec.per_change` applies where the *trunk* changes layer, which R2 never makes it do (a
spine refuses instead).

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
