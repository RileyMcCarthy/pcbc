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
from .route_geom import MICRO_MM, Box, Pt, aabb, clip_len_in_box, is_octilinear, legs_ok, q, seg_lengths, turn_ok
from .route_scene import Item, Scene, clashes
from .sexp import matching_paren

__all__ = [
    "POUR_CELL_MM",
    "PourRaster",
    "Zone",
    "in_zone",
    "lane_overrun",
    "paths_of",
    "plane_checks",
    "plane_islands",
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


def plane_checks(text: str, pieces: Sequence[Piece]) -> list[str]:
    """D.5's first half, asked of a filled board: every tap via lands in its own net's plane.

    A via that clears every rule and sits where the fill retreated is copper that connects nothing,
    and the gate reports it three stages later as an unconnected pad rather than as the tap it was.
    Here it is one sentence naming the pad.
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
            continue  # the net has no filled zone on this board; `net_open` is what speaks for it
        if not any(in_zone(z, p.a) for z in mine):
            where = ", ".join(sorted(z.layer for z in mine))
            out.append(f"tap {p.net}: the via at ({p.a[0]:g},{p.a[1]:g}) for {p.owner} is not inside the {p.net} plane on {where}")
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
