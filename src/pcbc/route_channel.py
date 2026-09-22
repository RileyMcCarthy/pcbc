"""The congestion map: every channel on a placed board, and what fits through it.

A placed board is a set of obstacles with air between them. **Nothing in pcbc measures that air.**
`route_scene.free_intervals` answers the question a trunk asks — "given this stretch, which
perpendicular offsets are free" — and it answers it along one of four axes through an AABB
(`route_scene._AXES` raises on anything else). It cannot say how much room there is *between two
pads*, which is the number a placement decision needs and the number a topological router is a
search over.

This module says it. Per layer: triangulate the obstacle hull corners, call each triangulation edge
between two different obstacles a **gate**, and compute exactly what a gate can carry from its two
endpoints and `constraints.ClearanceTable`. The output is a report and a JSON block. **There is no
search here, no copper, and no router** — `docs/topo-plan.md` slice 1 owns the map and slices 3 and 4
own what is done with it.

**What a gate is.** Two obstacles, a corner of each, and the straight line between them. Its name is
`((ownerA, cornerA), (ownerB, cornerB))` — `route_scene.Item.owner` plus the index of the corner in
`Shape.pts` — and **never a vertex index**, which is the whole of `docs/topo-plan.md` C-A: moving one
part 0.5 mm keeps 84.7 % of buck's Delaunay edge names under vertex-index naming and 97.1 % under
this one. The name is the thing a stored topology is written in, so it is chosen here, in the module
that mints it, rather than in the module that would have to translate it.

**What a gate can carry.** Two numbers, and they answer different questions:

*`Gate.free` and `Gate.slots`* are the **generic** answer, and they are what the report prints: the
corner-to-corner span, minus each endpoint's own offset radius, minus the worst clearance anything on
this board could owe that endpoint, divided into tracks at `tightest()` — the narrowest track and the
closest spacing this board can legally be routed at. Using the board's own floor rather than a
particular net's class is deliberate and it is what makes `slots == 0` worth printing: it is the
*most* tracks that could ever fit, so a gate this call says holds none holds none for every net on
the board. A wall is a wall.

*`fits(scene, gate, members)`* is the **exact** answer for a known set of crossing nets, and it is
what slice 3's negotiated congestion will call and what the calibration test calls today. It is the
one comparison in this module that decides anything, and it is **integer nanometres on both sides**
(`docs/topo-plan.md` section 4, leak 8: the prototype compared `demand > usable + 1e-9` and that is a
float comparison deciding a route).

**Three modelling choices, each of which changes a number:**

1. *A member on an endpoint's own net pays half a width per shared end, and none for two.* It may
   overlap that endpoint's copper — same net, zero clearance — so only half of it sticks out past the
   corner. Two shared ends and it is the connection itself.
2. *The interior clearance is `max` over the crossing pairs, not the sum over an ordering.* Slot
   *order* is slice 4's; without an order, the max is the only order-independent choice and it is an
   over-estimate of the demand for every ordering, which is the direction that makes the 0-of-875
   calibration a result rather than a tautology.
3. *A keepout owes nothing and a bare drill owes `hole_to_copper`.* The first matches
   `route_scene.free_intervals`, which sets `need = 0.0` for a keepout because a keepout forbids
   copper inside it and touching the boundary is legal. The second is A.4 rule 2, and it is the one
   place this module sees an obstacle `free_intervals` does *not*: an NPTH has no copper at all, so
   `it.copper is None` and a copper-only vertex pass would triangulate straight through a mounting
   hole.

**Two corrections the plan's own text did not survive, both measured here:**

*The gate-clearance filter needs the offset radius.* `docs/topo-plan.md` slice 1 spells SEARCH's R6
as "reject a gate when `hull_dist2(gate_hull, it.copper.pts) == 0.0`". `hull_dist2` compares hull
points and **ignores `Shape.r`**, so that predicate misses every track and every via — a via is one
point with a radius, and its hull is that single point. The shipped predicate is
`hull_dist2(gate, sh.pts) <= sh.r ** 2`, which is the exact "does this segment enter that copper"
test and degenerates to the plan's `== 0.0` for a zero-radius item. An item containing one of the
gate's own endpoints is not a blocker — it is that corner's neighbour, and the channel there has zero
width, which `free` already reports.

*The prototype's headline gate counts are pre-filter numbers.* The census in `docs/topo-plan.md`
slice 1 (buck 196 F.Cu gates, ds2 470, c3_usb 891, node 1105) was taken by a plain Delaunay with
same-owner edges dropped and **no** clearance filter, which is the defect R6 exists to fix. Adding
the filter necessarily moves them. `census()` reports both, `blocked` beside `gates`, so the
prototype's number stays checkable and the shipped number is the one a decision reads.

**Determinism** is `docs/topo-plan.md` section 4, leaks 1-4, and it is structural rather than hoped
for: the vertex map is read as `sorted(by_pt)` and never iterated as a dict; ids come from
`Scene.items`, which is already sorted with ids assigned after the sort, so the corner a shared point
is named by is the lowest-id item's; the insertion order is a coarse-grid boustrophedon that is a
pure function of the points; every predicate is an integer determinant with no epsilon; and gates are
sorted by `((owner, corner), (owner, corner))` — strings and ints, never floats.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from .route_geom import EPS_MM, Pt, Shape, hull_dist2
from .route_scene import Item, Scene

__all__ = [
    "Channels",
    "Escape",
    "Gate",
    "GateKey",
    "census",
    "channels",
    "channels_doc",
    "class_widths",
    "crossed",
    "demand_nm",
    "escapes",
    "fits",
    "moves",
    "report",
    "slack_nm",
    "supply_nm",
    "tightest",
    "triangulate",
]

GateKey = tuple[tuple[str, int], tuple[str, int]]

_MAX_NEED = 3.0
"""How far past a gate's own box `Scene.query` is asked to look for a blocking third item, in mm.
The same number and the same reason as `route_scene._MAX_NEED`: every clearance the five boards
compile is 0.0889 to 0.5 and the widest `keep_clear_mm` any example writes is 3. The index answer is
a superset and the exact predicate runs on what comes back."""

_EDGE_CORNERS = ("NW", "NE", "SE", "SW")
"""The four corners of `Scene.outline`, in the order `(x0,y0), (x1,y0), (x1,y1), (x0,y1)`.

North is **-y**: KiCad's frame is y-down, so `NW` is the top-left corner on screen. They are four
distinct items with four distinct owners rather than one `board edge`, because a shared owner makes
all four outline edges same-owner edges — dropped by the gate filter — and on a near-empty layer that
disconnects the dual graph slice 3 searches (`docs/topo-plan.md` slice 1)."""


def _nm(mm: float) -> int:
    """Millimetres to integer nanometres, rounded to nearest.

    Not `route_geom.q`: `q` is for **coordinates**, and every coordinate this module handles arrived
    through it already — `Shape.__post_init__` refuses a point that is not on the 1 nm grid, so the
    hull corners triangulated here are exactly the numbers a board file carries. What passes through
    here is a *clearance* or a *width* off the constraint compiler, which is a requirement rather
    than a position, and it is converted once, on the way into the one comparison that decides."""
    return int(round(mm * 1e6))


# --- the triangulation ------------------------------------------------------------------------


def _orient(a: tuple[int, int], b: tuple[int, int], c: tuple[int, int]) -> int:
    """>0 when a->b->c turns left. An exact integer determinant; no epsilon, no float."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _incircle(a: tuple[int, int], b: tuple[int, int], c: tuple[int, int], d: tuple[int, int]) -> int:
    """>0 when d is strictly inside the circumcircle of the counter-clockwise triangle abc.

    Exact: Python's ints are arbitrary precision, so the 4x4 determinant is evaluated without
    rounding at any board size. Coordinates are nanometres, so the terms reach 1e24 on a 300 mm
    board and still cost one multiply each.
    """
    adx, ady = a[0] - d[0], a[1] - d[1]
    bdx, bdy = b[0] - d[0], b[1] - d[1]
    cdx, cdy = c[0] - d[0], c[1] - d[1]
    return (
        (adx * adx + ady * ady) * (bdx * cdy - bdy * cdx)
        - (bdx * bdx + bdy * bdy) * (adx * cdy - ady * cdx)
        + (cdx * cdx + cdy * cdy) * (adx * bdy - ady * bdx)
    )


def _insertion_order(pts: Sequence[tuple[int, int]]) -> list[int]:
    """A coarse-grid boustrophedon over the points: a pure function of the point set.

    Incremental Delaunay is O(n log n) only if consecutive insertions are near each other, because
    the point location walks from the last triangle. Sorting by y-cell and then by x-cell, reversing
    x on odd rows, keeps the walk short without a Hilbert curve — and unlike "the order the file
    listed them", it is a property of the geometry, so the triangulation cannot depend on which
    footprint a board happened to declare first. `i` is the final tie-break, and `pts` is already
    sorted, so ties are broken by coordinate (`docs/topo-plan.md` section 4, leak 2).
    """
    n = len(pts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
    cell = max(span // int(max(2, math.isqrt(n))), 1)
    return sorted(range(n), key=lambda i: (pts[i][1] // cell, (pts[i][0] // cell) * (1 if (pts[i][1] // cell) % 2 == 0 else -1), i))


def triangulate(pts: Sequence[tuple[int, int]]) -> tuple[list[tuple[int, int, int]], list[tuple[int, int]]]:
    """Delaunay over distinct integer-nanometre points. Returns (triangles, edges), both sorted.

    Bowyer-Watson with a super-triangle, incremental, walking from the previous triangle. The two
    predicates are exact integer determinants, so the result is a function of the points and not of
    how the floats rounded — which is the reason this is 150 lines of stdlib rather than a scipy
    call. `~/pcbc/.venv` has neither numpy nor scipy and `pyproject.toml` declares no dependencies;
    the largest board in the repo triangulates in under 20 ms.

    A triangle is `(i, j, k)` with `i < j < k`; an edge is `(i, j)` with `i < j`. Both lists are
    sorted, so nothing downstream depends on insertion order.
    """
    n = len(pts)
    if n < 3:
        return ([], [])
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
    d = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
    P = list(pts) + [(cx - 20 * d, cy - 20 * d), (cx + 20 * d, cy - 20 * d), (cx, cy + 20 * d)]
    tv: list[tuple[int, int, int]] = [(n, n + 1, n + 2)]  # vertices, counter-clockwise
    tn: list[list[int]] = [[-1, -1, -1]]  # neighbour opposite each vertex, -1 for none
    dead: list[bool] = [False]
    cur = 0
    for i in _insertion_order(pts):
        p = P[i]
        # 1. locate: walk across the edge the point is outside of, in a fixed side order.
        t = cur if not dead[cur] else next(k for k in range(len(tv) - 1, -1, -1) if not dead[k])
        for _ in range(4 * len(tv) + 64):
            a, b, c = tv[t]
            if _orient(P[a], P[b], p) < 0 and tn[t][2] >= 0:
                t = tn[t][2]
                continue
            if _orient(P[b], P[c], p) < 0 and tn[t][0] >= 0:
                t = tn[t][0]
                continue
            if _orient(P[c], P[a], p) < 0 and tn[t][1] >= 0:
                t = tn[t][1]
                continue
            break
        else:  # pragma: no cover - the walk is bounded by the triangle count
            t = next(k for k, v in enumerate(tv) if not dead[k] and _orient(P[v[0]], P[v[1]], p) >= 0 and _orient(P[v[1]], P[v[2]], p) >= 0 and _orient(P[v[2]], P[v[0]], p) >= 0)
        # 2. the cavity: every triangle whose circumcircle contains p, breadth first from `t`.
        # `seen` is a set of ints and is only ever asked for membership; the order `bad` comes out
        # in is the order the explicit stack produced, which is a list and therefore fixed.
        bad: list[int] = []
        stack = [t]
        seen = {t}
        while stack:
            x = stack.pop()
            bad.append(x)
            for nb in tn[x]:
                if nb < 0 or nb in seen or dead[nb]:
                    continue
                a, b, c = tv[nb]
                if _incircle(P[a], P[b], P[c], p) > 0:
                    seen.add(nb)
                    stack.append(nb)
        # 3. the cavity boundary, then one new triangle per boundary edge.
        boundary: list[tuple[int, int, int]] = []
        for x in bad:
            for e in range(3):
                nb = tn[x][e]
                if nb < 0 or nb not in seen:
                    boundary.append((tv[x][(e + 1) % 3], tv[x][(e + 2) % 3], nb))
        for x in bad:
            dead[x] = True
        fresh: list[int] = []
        for u, w, nb in boundary:
            k = len(tv)
            tv.append((i, u, w))
            tn.append([nb, -1, -1])
            dead.append(False)
            fresh.append(k)
            if nb >= 0:
                for e in range(3):
                    if {tv[nb][(e + 1) % 3], tv[nb][(e + 2) % 3]} == {u, w}:
                        tn[nb][e] = k
                        break
        # 4. link the new triangles to each other along their two radial edges.
        byedge: dict[tuple[int, int], tuple[int, int]] = {}
        for k in fresh:
            a, b, c = tv[k]
            for e, (x, y) in enumerate(((b, c), (c, a), (a, b))):
                key = (x, y) if x < y else (y, x)
                hit = byedge.pop(key, None)
                if hit is None:
                    byedge[key] = (k, e)
                else:
                    tn[k][e] = hit[0]
                    tn[hit[0]][hit[1]] = k
        cur = fresh[0] if fresh else t
    tris: list[tuple[int, int, int]] = []
    edges: set[tuple[int, int]] = set()
    for k, v in enumerate(tv):
        if dead[k] or max(v) >= n:
            continue
        a, b, c = sorted(v)
        tris.append((a, b, c))
        edges.update(((a, b), (a, c), (b, c)))
    return (sorted(tris), sorted(edges))


def _hull_size(pts: Sequence[tuple[int, int]]) -> int:
    """How many of these points lie on the convex hull boundary, **collinear ones included**.

    Euler's formula for a triangulation of `n` points with `h` on the boundary is
    `t = 2n - h - 2` and `e = 3n - h - 3`, and `h` there counts every boundary point — so this is
    Andrew's monotone chain with the collinear pop removed (`< 0` where `route_geom._chain` has
    `<= 0`). `route_geom.hull` drops collinear points on purpose, because a `Shape` must be strictly
    convex; the Euler check needs the other count, so it is computed here and `route_geom` is left
    alone.
    """
    ps = sorted(set(pts))
    if len(ps) <= 2:
        return len(ps)
    lower: list[tuple[int, int]] = []
    for p in ps:
        while len(lower) >= 2 and _orient(lower[-2], lower[-1], p) < 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[int, int]] = []
    for p in reversed(ps):
        while len(upper) >= 2 and _orient(upper[-2], upper[-1], p) < 0:
            upper.pop()
        upper.append(p)
    return len(lower) + len(upper) - 2


# --- the obstacles ----------------------------------------------------------------------------


def _shape_of(it: Item) -> Shape | None:
    """The shape this item occupies a channel with: its copper, or its drill when it has no copper.

    A bare NPTH is the only item with no copper at all, and it is an obstacle to every track on
    every layer (A.4 rule 2). A copper-only vertex pass triangulates straight through a mounting
    hole, which is exactly the kind of channel that looks open and is not.
    """
    return it.copper if it.copper is not None else it.hole


def _edge_items(scene: Scene) -> list[Item]:
    """`Scene.outline`'s four corners, as four one-point items with four distinct owners."""
    x0, y0, x1, y1 = scene.outline
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return [
        Item(-1 - i, "edge", "", frozenset(scene.layers), Shape((c,), 0.0), None, None, f"board edge {_EDGE_CORNERS[i]}")
        for i, c in enumerate(corners)
    ]


def _need(scene: Scene, net: str, it: Item) -> float:
    """The clearance copper on `net` owes this item at a gate endpoint, in mm.

    Four cases and every number comes from the stackup or the clearance table:
    the board edge is `table.edge()`; a keepout owes nothing, because it forbids copper inside it
    and the boundary is legal (this is `route_scene.free_intervals`' own rule); a bare drill is
    `table.hole_to_copper()`, A.4 rule 2; everything else is `table.between`, which is the same call
    `route_scene.clashes` decides with, so this module cannot drift from the rules KiCad is handed.
    """
    if it.kind == "edge":
        return scene.table.edge()
    if it.kind == "keepout":
        return 0.0
    if it.kind == "hole":
        return scene.table.hole_to_copper()
    return scene.table.between(net, it.net)[0]


def _end_need(scene: Scene, net: str, ref: int) -> float:
    """`_need` addressed by a `Gate` endpoint reference: a negative id is one of the four outline
    corners, which are synthesised per call and are not in `Scene.items`."""
    return scene.table.edge() if ref < 0 else _need(scene, net, scene.items[ref])


# --- the gates --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """One channel: two obstacle corners and the straight line between them.

    `span_nm` is `floor(|b - a|)` in nanometres, taken with `math.isqrt` on the exact integer squared
    distance — an integer, exact on every platform, and never more than the truth. Every other length
    in this dataclass is derived from it.
    """

    key: GateKey
    layer: str
    a: Pt
    b: Pt
    ref_a: int  # `Scene.items` id, or -1..-4 for an outline corner
    ref_b: int
    owner_a: str
    owner_b: str
    net_a: str
    net_b: str
    kind_a: str
    kind_b: str
    r_a: float
    r_b: float
    span_nm: int
    free_nm: int
    """The **most** room any net on this board could have across this gate: the span, less both
    offset radii, less the *smallest* clearance `between` can return for either endpoint. Negative
    when the two obstacles are closer than the tightest clearance on the board allows — a wall with a
    magnitude, which is what a `Place()` move needs. `slots` counts it, and `slots == 0` is therefore
    a proof: no net gets through here."""
    sure_nm: int
    """The room a net has across this gate **whatever it is**: the same span less the *largest*
    clearance either endpoint could owe anything on the board. `holds` counts it, and `holds >= 1`
    is the complementary proof: every net on this board fits through here.

    The two differ by more than a rounding: on buck `free` says 0 of 196 F.Cu channels are walls and
    `sure` says 31, because `sure` charges every channel the board's worst clearance pair at both
    ends. Both are true and they answer different questions — see `census`."""
    slots: int
    holds: int

    @property
    def free(self) -> float:
        return round(self.free_nm / 1e6, 4)

    @property
    def sure(self) -> float:
        return round(self.sure_nm / 1e6, 4)

    @property
    def length(self) -> float:
        return round(self.span_nm / 1e6, 4)

    def label(self) -> str:
        """How a report names this channel: the two owners, in key order."""
        return f"{self.owner_a} .. {self.owner_b}"


@dataclass(frozen=True)
class Channels:
    """One layer's congestion map."""

    layer: str
    gates: tuple[Gate, ...]
    verts: int
    tris: int
    edges: int
    """The raw triangulation's counts, before any gate filtering: `verts - edges + (tris + 1) == 2`."""
    hull: int
    inside: int  # triangulation edges dropped because both corners belong to one obstacle
    blocked: int  # triangulation edges dropped because a third item's copper crosses them
    wall_ms: int

    def by_key(self) -> dict[GateKey, Gate]:
        return {g.key: g for g in self.gates}


def _blocking(scene: Scene, layer: str, a: Pt, b: Pt, skip: tuple[int, int]) -> bool:
    """Does a third item's copper cross the open channel between these two corners?

    The exact predicate, and the one place `docs/topo-plan.md`'s own wording had to change:
    `hull_dist2` compares hull *points* and ignores `Shape.r`, so `== 0.0` misses every track and
    every via. A segment enters `hull(pts) + disc(r)` exactly when its squared distance to the hull
    is at most `r ** 2`, and for `r == 0` that is the plan's test unchanged.

    An item containing one of the gate's own endpoints is skipped: it is that corner's neighbour, not
    a blocker, and the channel there has zero width, which `free_nm` already reports as a negative
    number. Without it, two pad primitives of one custom pad that share a vertex would delete every
    gate leaving that vertex.
    """
    box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
    seg = (a, b)
    # `EPS_MM` and not `_MAX_NEED`: this asks whether copper *touches* the channel, not whether it
    # clears it, and `Scene.query` buckets an item by `it.box()`, which already includes its offset
    # radius — so an item whose copper reaches the segment shares a cell with it, and the only slack
    # needed is enough to survive `aabb`'s own float noise at a cell boundary. Asking for 3 mm
    # instead returned 21 candidates per gate on node and cost 0.56 s of a 0.62 s pass; this costs
    # 0.04 s and returns the same answer as a brute-force scan of every item on ds2's 731 edges.
    #
    # **There is no box test after this one**, and there was: `it.box()` is raw float arithmetic, so
    # ds2's `J4.2` has `y0 = 0.30000000000000004` where its copper starts at `0.3` — and an AABB
    # reject on that number dropped the one item blocking the gate along the top board edge. A float
    # comparison deciding what an exact predicate is for is the bug this module opens by refusing.
    for i in scene.query(layer, box, EPS_MM):
        if i in skip:
            continue
        it = scene.items[i]
        if it.kind in ("lane", "edge") or layer not in it.layers:
            continue
        sh = _shape_of(it)
        if sh is None:
            continue
        r2 = sh.r * sh.r
        if hull_dist2(seg, sh.pts) > r2:
            continue
        if hull_dist2((a,), sh.pts) <= r2 or hull_dist2((b,), sh.pts) <= r2:
            continue  # this item owns one of the two corners
        return True
    return False


def channels(scene: Scene, *, layers: Iterable[str] | None = None) -> tuple[Channels, ...]:
    """The congestion map of a placed (or partly routed) board, one `Channels` per copper layer.

    Read `Scene.items` in id order and never as a dict: `build_scene` sorts the items and assigns ids
    after the sort, so the lowest-id item is the canonical owner of a corner two obstacles share, and
    the gate names are a function of the board rather than of the order a file listed its footprints.
    """
    want = tuple(layers) if layers is not None else scene.layers
    nets = sorted({it.net for it in scene.items if it.net})
    tw, tc = tightest(scene)
    w_nm, c_nm = _nm(tw), _nm(tc)
    # The clearance an endpoint costs depends only on its **net**, never on which item carries it, so
    # both bounds memoise on the net name: ~50 nets rather than ~600 items, and `between` is called
    # once per ordered pair for the whole board instead of once per gate endpoint.
    lo: dict[str, float] = {}
    hi: dict[str, float] = {}

    def _ends(it: Item) -> tuple[float, float]:
        """(smallest, largest) clearance any net on this board could owe this endpoint.

        `_need` is the only place the four-case rule lives; this folds it over the board's nets. For
        an endpoint whose requirement does not depend on the crossing net at all — the outline, a
        keepout, a bare drill — the two are the same number and the fold is skipped.
        """
        if it.kind in ("edge", "keepout", "hole"):
            v = _need(scene, "", it)
            return (v, v)
        if it.net not in lo:
            vals = [_need(scene, n, it) for n in nets] or [_need(scene, "", it)]
            lo[it.net], hi[it.net] = min(vals), max(vals)
        return (lo[it.net], hi[it.net])

    out: list[Channels] = []
    edge_items = _edge_items(scene)
    for layer in want:
        t0 = time.perf_counter()
        # nm point -> (item id, corner index, the corner in mm). The **mm coordinate is carried, not
        # recovered**: it came off `Shape.pts`, which `Shape.__post_init__` guarantees is on the 1 nm
        # grid, so it has already been through `route_geom.q` exactly once and dividing an integer
        # back by 1e6 would be a second quantisation of a number that needs none. `_nm` is injective
        # on grid values, so every item mapping to one key carries the same mm point.
        by_pt: dict[tuple[int, int], tuple[int, int, Pt]] = {}
        obs: dict[int, Item] = {}
        for it in list(scene.items) + edge_items:
            if it.kind == "lane" or layer not in it.layers:
                continue
            sh = _shape_of(it)
            if sh is None:
                continue
            if it.kind == "edge" and it.id >= 0:
                continue  # the scene's own whole-outline rect; the four corners stand in for it
            obs[it.id] = it
            for c, p in enumerate(sh.pts):
                by_pt.setdefault((_nm(p[0]), _nm(p[1])), (it.id, c, p))
        pts = sorted(by_pt)
        if len(pts) < 3:
            out.append(Channels(layer, (), len(pts), 0, 0, len(pts), 0, 0, 0))
            continue
        owner = [by_pt[p] for p in pts]
        tris, edges = triangulate(pts)
        gates: list[Gate] = []
        inside = 0
        walled = 0
        for ia, ib in edges:
            ra, ca, pa = owner[ia]
            rb, cb, pb = owner[ib]
            if ra == rb:
                inside += 1
                continue
            ita, itb = obs[ra], obs[rb]
            ka = (ita.owner, ca)
            kb = (itb.owner, cb)
            if kb < ka:
                ka, kb, ia, ib, pa, pb, ita, itb = kb, ka, ib, ia, pb, pa, itb, ita
            if _blocking(scene, layer, pa, pb, (ita.id, itb.id)):
                walled += 1
                continue
            dx, dy = pts[ib][0] - pts[ia][0], pts[ib][1] - pts[ia][1]
            span = math.isqrt(dx * dx + dy * dy)
            sa, sb = _shape_of(ita), _shape_of(itb)
            (loa, hia), (lob, hib) = _ends(ita), _ends(itb)
            bare = span - _nm(sa.r) - _nm(sb.r)
            free = bare - _nm(loa) - _nm(lob)
            sure = bare - _nm(hia) - _nm(hib)
            gates.append(
                Gate(
                    key=(ka, kb),
                    layer=layer,
                    a=pa,
                    b=pb,
                    ref_a=ita.id,
                    ref_b=itb.id,
                    owner_a=ita.owner,
                    owner_b=itb.owner,
                    net_a=ita.net,
                    net_b=itb.net,
                    kind_a=ita.kind,
                    kind_b=itb.kind,
                    r_a=sa.r,
                    r_b=sb.r,
                    span_nm=span,
                    free_nm=free,
                    sure_nm=sure,
                    slots=_slots(free, w_nm, c_nm),
                    holds=_slots(sure, w_nm, c_nm),
                )
            )
        gates.sort(key=lambda g: g.key)
        out.append(
            Channels(
                layer=layer,
                gates=tuple(gates),
                verts=len(pts),
                tris=len(tris),
                edges=len(edges),
                hull=_hull_size(pts),
                inside=inside,
                blocked=walled,
                wall_ms=int(round((time.perf_counter() - t0) * 1000)),
            )
        )
    return tuple(out)


def tightest(scene: Scene) -> tuple[float, float]:
    """The narrowest track and the closest spacing **this board** can legally be routed at.

    The width is `stackup.track_min`, the fab floor. The clearance is the smallest number
    `ClearanceTable.between` can return for a real pair, which is the smallest *class* clearance and
    not `stackup.clearance_min`: `between` floors at the stackup and then takes a max over the two
    nets' classes, and a net with no `NetReq` gets the Default class — so on buck nothing is ever
    routed closer than 0.16 mm even though the fab floor is 0.127. Using the floor would make
    `slots` over-count by one on 8 of buck's 196 F.Cu channels, which is a *false accept* in the
    direction a placement oracle must not have.
    """
    cs = getattr(scene.cs, "classes", ())
    return (scene.stack.track_min, min([c.clearance_mm for c in cs] or [scene.stack.clearance_min]))


def _slots(free_nm: int, w_nm: int, c_nm: int) -> int:
    """How many tracks of `w_nm` at `c_nm` fit in `free_nm`.

    Integer arithmetic throughout — a floor division on floats is a decision made in IEEE-754
    (`docs/topo-plan.md` section 4, leak 8). With `tightest`'s numbers this is the *most* tracks that
    could ever fit, so `slots == 0` is a statement about every net on the board and not about one of
    them. A wall is a wall.
    """
    if free_nm < w_nm:
        return 0
    return 1 + (free_nm - w_nm) // (w_nm + c_nm)


# --- the exact rule ---------------------------------------------------------------------------


def supply_nm(gate: Gate, nets: Iterable[str]) -> int:
    """How much of the corner-to-corner span is air, given who is crossing, in nanometres.

    An endpoint's own offset radius comes off the span — that is copper, not channel — **unless** one
    of the crossing nets is that endpoint's own net, in which case it may sit on top of it and the
    radius is channel again.
    """
    ns = frozenset(nets)
    lo = 0 if (gate.net_a and gate.net_a in ns) else _nm(gate.r_a)
    hi = 0 if (gate.net_b and gate.net_b in ns) else _nm(gate.r_b)
    return gate.span_nm - lo - hi


def demand_nm(scene: Scene, gate: Gate, members: Sequence[tuple[str, float]]) -> int:
    """What `members` — `(net, width_mm)` pairs — need across this gate, in nanometres.

    `sum(width) + (k - 1) * max-pairwise-clearance + the two end clearances`, with a member on an
    endpoint's own net charged half a width per shared end and none for two (it may overlap that
    endpoint's copper, so only half of it sticks out past the corner; two shared ends and it *is* the
    connection). The interior term is a `max` rather than a sum over an ordering because slot order
    is slice 4's — see this module's docstring, choice 2.

    `members` is read in sorted order, so the answer is a function of the set and not of the order the
    caller happened to collect it in.
    """
    ms = sorted(members)
    ns = [n for n, _ in ms]
    k = len(ms)
    if k == 0:
        return 0
    mid = max([scene.table.between(x, y)[0] for i, x in enumerate(ns) for y in ns[i + 1 :]] or [0.0])
    total = (k - 1) * _nm(mid)
    for n, w in ms:
        ends = (1 if (gate.net_a and n == gate.net_a) else 0) + (1 if (gate.net_b and n == gate.net_b) else 0)
        total += _nm(w) if ends == 0 else (_nm(w) // 2 if ends == 1 else 0)
    seen = frozenset(ns)
    for net_e, ref in ((gate.net_a, gate.ref_a), (gate.net_b, gate.ref_b)):
        if net_e and net_e in seen:
            continue  # a member owns this end: it may sit on that copper and owes it nothing
        total += _nm(max(_end_need(scene, n, ref) for n in ns))
    return total


def fits(scene: Scene, gate: Gate, members: Sequence[tuple[str, float]]) -> bool:
    """Can this gate carry these `(net, width_mm)` members at once?

    **The one comparison in this module that decides anything, and it is integer nanometres on both
    sides.** The prototype compared `demand > usable + 1e-9`; that is a float comparison deciding a
    route, and `docs/topo-plan.md` section 4 leak 8 calls it a real defect. There is no epsilon here
    because there is nothing to absorb: both sides are exact integers.
    """
    return demand_nm(scene, gate, members) <= supply_nm(gate, (n for n, _ in members))


def crossed(chan: Channels, a: Pt, b: Pt) -> tuple[Gate, ...]:
    """Every gate of this layer the straight segment `a -> b` touches or crosses, in key order.

    `hull_dist2 == 0.0` is `route_geom`'s own exact overlap answer for two two-point hulls — a proper
    crossing, an endpoint landing on the gate, or two collinear overlapping segments all read 0.0 —
    so "crosses" here is the same predicate the rest of pcbc decides overlap with, and touching
    counts. An axis-aligned box test runs first: it is a superset and it takes the calibration pass
    from O(segments x gates) exact tests down to the pairs that can possibly meet.
    """
    lo = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
    seg = (a, b)
    out: list[Gate] = []
    for g in chan.gates:
        gb = (min(g.a[0], g.b[0]), min(g.a[1], g.b[1]), max(g.a[0], g.b[0]), max(g.a[1], g.b[1]))
        if gb[2] < lo[0] or gb[0] > lo[2] or gb[3] < lo[1] or gb[1] > lo[3]:
            continue
        if hull_dist2(seg, (g.a, g.b)) == 0.0:
            out.append(g)
    return tuple(out)


# --- what it says about a board ---------------------------------------------------------------


def census(maps: Sequence[Channels]) -> dict:
    """The per-layer histogram: gates, walls, single-file channels, and the narrowest.

    **Two histograms, because there are two true answers and the plan's table is the other one.**
    `slots` counts `Gate.free`, the best case — the smallest clearance `ClearanceTable.between` can
    return at either end — so it is an upper bound and `slots["0"]` is a count of channels **no net
    on this board fits through**. `holds` counts `Gate.sure`, the worst case, so it is a lower bound
    and `holds["1"]` and up are channels **every** net fits through.

    The prototype in `docs/topo-plan.md` slice 1 printed only the worst case, and its headline —
    "roughly a fifth of every board is wall" — is an artefact of that: measured here, buck has
    **31 of 196** F.Cu channels that not every net fits and **0** that nothing fits, and ds2 has
    **28 of 454** and **0**. The wall claim survives on the two boards that carry a dense connector
    (c3_usb 52, node 39) and does not survive on the two that do not. Both numbers ship, labelled.

    `gates` is the shipped count — after the R6 clearance filter. `raw` beside it is
    `gates + blocked`, which is the number the prototype printed, kept so this census is checkable
    against the plan's own table rather than only against itself.

    **`Channels.wall_ms` is deliberately not in here.** This dict is written into `copper.json`, and a
    wall-clock number in a recorded artifact makes the file a different file on every run — measured:
    with `ms` in the census, buck's `channels` block hashed differently on 2 of 3 consecutive runs
    while the gates themselves were byte-identical every time. The timing is on the `Channels` for a
    caller that wants to print it.
    """
    doc: dict = {}
    for ch in maps:
        slots: dict[str, int] = {}
        holds: dict[str, int] = {}
        for g in ch.gates:
            slots[str(min(g.slots, 4))] = slots.get(str(min(g.slots, 4)), 0) + 1
            holds[str(min(g.holds, 4))] = holds.get(str(min(g.holds, 4)), 0) + 1
        narrow = min(ch.gates, key=lambda g: (g.free_nm, g.key)) if ch.gates else None
        doc[ch.layer] = {
            "verts": ch.verts,
            "tris": ch.tris,
            "edges": ch.edges,
            "gates": len(ch.gates),
            "raw": len(ch.gates) + ch.blocked,
            "inside_hull": ch.inside,
            "blocked": ch.blocked,
            "slots": {k: slots.get(k, 0) for k in ("0", "1", "2", "3", "4")},
            "holds": {k: holds.get(k, 0) for k in ("0", "1", "2", "3", "4")},
            "narrowest_mm": narrow.free if narrow is not None else None,
            "narrowest": narrow.label() if narrow is not None else None,
        }
    return doc

@dataclass(frozen=True)
class Escape:
    """A pad's widest way out, and what its net needs there.

    **The decisive question a placed board can answer on its own.** Every net has to leave every pad
    it lands on, and the only ways out of a pad are the channels incident on that pad's own corners.
    That is a *local* fact — it needs no homotopy, no net order and no router — so a pad whose every
    incident channel refuses its own net is a placement refusal and not a routing difficulty, and it
    carries a magnitude in millimetres.

    It is also the question `docs/topo-plan.md` slice 1 asks in its own words: "the narrowest channel
    `VBUS` must cross is 0.200 mm between `J1.B8` and `J1.A5`, and it needs 0.450" is a channel
    between two pads of one connector, which is a `VBUS` pad's way out of the row it sits in.

    The straight line between two pads is deliberately **not** measured. It crosses whatever happens
    to be in the way, and a route goes around: measured on buck, the straight line of 19 of 21
    airwires crosses a channel narrower than the net needs, on a board that routes with zero
    unconnected items. A "move" fired by that is noise. Which channels a route crosses is its
    homotopy class, and choosing one is slice 3.

    `slack` is the exact rule's own arithmetic — `supply_nm - demand_nm` for this one net alone — so a
    negative number is the width the channel is short by, and not an estimate of it.
    """

    net: str
    pad: str  # the pad's owner, e.g. "J1.B8"
    layer: str  # the layer whose best way out is widest, among the ones this net and pad share
    slack: float  # mm; negative is sealed
    at: str  # the widest gate's label
    ways: int  # how many channels are incident on this pad on that layer
    open_ways: int  # how many of them this net fits through
    width: float  # the track width the answer was computed at


def _incident(chan: Channels) -> dict[int, list[Gate]]:
    """Every gate, filed under each of its two endpoint item ids. Lists, in gate-key order."""
    out: dict[int, list[Gate]] = {}
    for g in chan.gates:  # already sorted by key
        out.setdefault(g.ref_a, []).append(g)
        out.setdefault(g.ref_b, []).append(g)
    return out


def slack_nm(scene: Scene, gate: Gate, net: str, width: float) -> int:
    """`supply_nm - demand_nm` for one net crossing this gate alone, in nanometres.

    The same two functions `fits` compares, subtracted instead — so a report line and the decision it
    reports can never disagree, and the number printed is the one the rule used.
    """
    return supply_nm(gate, (net,)) - demand_nm(scene, gate, ((net, width),))


def escapes(scene: Scene, maps: Sequence[Channels], widths: dict[str, float], *, layers_of: dict[str, tuple[str, ...]] | None = None) -> tuple[Escape, ...]:
    """Every net pad's widest way out, on the best layer it has one.

    A pad is taken once per owner — a custom pad draws several primitives and they are one pad — and
    every primitive's incident channels count as that pad's, because copper leaving any of them has
    left the pad. `Scene.items` is read in id order, so the primitives of a pad group in a fixed
    order and the answer does not depend on which one the board file listed first.

    The layer reported is the **best** one: the widest way out among the layers the pad is on and the
    net's `NetReq` allows. A refusal has to hold on every layer the net may take, or it is not a
    refusal — and `layers_of` is where the net's own declaration enters, straight off
    `Constraint.layers`.
    """
    by_layer = {ch.layer: ch for ch in maps}
    inc = {ch.layer: _incident(ch) for ch in maps}
    prims: dict[tuple[str, str], list[Item]] = {}
    for it in scene.items:
        if it.kind == "pad" and it.net:
            prims.setdefault((it.net, it.owner), []).append(it)
    out: list[Escape] = []
    for (net, pad), items in sorted(prims.items()):
        w = widths.get(net, scene.stack.track_min)
        allowed = frozenset(layers_of.get(net, scene.layers)) if layers_of else frozenset(scene.layers)
        best: tuple[int, str, Gate, int, int] | None = None
        for layer in scene.layers:
            if layer not in allowed or layer not in by_layer:
                continue
            here = [g for it in items if layer in it.layers for g in inc[layer].get(it.id, ())]
            if not here:
                continue
            scored = sorted(((slack_nm(scene, g, net, w), g.key, g) for g in here), key=lambda t: (-t[0], t[1]))
            top = scored[0]
            cand = (top[0], layer, top[2], len(here), sum(1 for s, _, _ in scored if s >= 0))
            if best is None or cand[0] > best[0]:
                best = cand
        if best is None:
            continue
        s, layer, g, ways, open_ways = best
        out.append(Escape(net=net, pad=pad, layer=layer, slack=round(s / 1e6, 4), at=g.label(), ways=ways, open_ways=open_ways, width=w))
    return tuple(out)


def moves(es: Sequence[Escape]) -> tuple[str, ...]:
    """A `Place()` move per pad that no channel on any layer it may use lets its net out of.

    `docs/topo-plan.md` house rule: every refusal is a move ending in a `board.py` edit. This one has
    a magnitude in millimetres and names the two obstacles that make the wall, and it is computed
    from a **placed** board in milliseconds, with no router and no KRT run.
    """
    out: list[str] = []
    for e in sorted(es, key=lambda e: (e.slack, e.net, e.pad)):
        if e.slack >= 0.0:
            continue
        out.append(
            f"Place(): {e.pad} has no way out on {e.net}. Its widest channel on {e.layer} is "
            f"{e.at}, and a {e.width:g} mm {e.net} track with its clearances is {-e.slack:g} mm too wide for it "
            f"({e.ways} channel{'s' if e.ways != 1 else ''} touch this pad and none fit). "
            f"Move one of the two apart, or give {e.net} a narrower class."
        )
    return tuple(out)


def channels_doc(scene: Scene, *, widths: dict[str, float] | None = None, layers_of: dict[str, tuple[str, ...]] | None = None, maps: Sequence[Channels] | None = None) -> dict:
    """The `channels` block of `copper.json`: the census, the tightest escapes, and the moves.

    A function of the placed board and nothing else, so it is worth recording next to the copper: it
    says what the router was *handed*, which is the half of a routing failure `copper.json` has never
    carried. A board that comes back unrouted has a census here saying whether it was ever routable.
    """
    maps = channels(scene) if maps is None else maps
    es = escapes(scene, maps, widths or {}, layers_of=layers_of)
    tight = sorted(es, key=lambda e: (e.slack, e.net, e.pad))[:10]
    return {
        "layers": census(maps),
        "escapes": [
            {"net": e.net, "pad": e.pad, "layer": e.layer, "slack": e.slack, "at": e.at, "ways": e.ways, "open": e.open_ways, "width": e.width}
            for e in tight
        ],
        "sealed": sum(1 for e in es if e.slack < 0.0),
        "moves": list(moves(es)),
    }


def report(scene: Scene, *, widths: dict[str, float] | None = None, layers_of: dict[str, tuple[str, ...]] | None = None, maps: Sequence[Channels] | None = None) -> list[str]:
    """The printed congestion map: one block per layer, then the tightest pad escapes, then moves.

    A pure function of the scene — **no wall clock in it**, for the reason `census` gives: a caller
    that wants the timing takes it off `Channels.wall_ms` or times this call, and a test can then
    assert the lines.
    """
    maps = channels(scene) if maps is None else maps
    tw, tc = tightest(scene)
    lines: list[str] = []
    for ch in maps:
        c = census([ch])[ch.layer]
        if not ch.gates and not ch.verts:
            lines.append(f"{ch.layer}: nothing on this layer")
            continue
        lines.append(
            f"{ch.layer}: {c['gates']} channels over {c['verts']} corners "
            f"({c['tris']} triangles, {c['inside_hull']} inside an obstacle, {c['blocked']} crossing one)"
        )
        s, h = c["slots"], c["holds"]
        at = f"at {tw:g} / {tc:g} mm"
        lines.append(f"  {at}, best case: {s['0']} take nothing, {s['1']} take one track, {s['2']} two, {s['3']} three, {s['4']} four or more")
        lines.append(f"  {' ' * len(at)}, any net:   {h['0']} refuse some net, {h['1']} take one track, {h['2']} two, {h['3']} three, {h['4']} four or more")
        if c["narrowest"] is not None:
            lines.append(f"  narrowest: {c['narrowest_mm']:g} mm between {c['narrowest']}")
    es = escapes(scene, maps, widths or {}, layers_of=layers_of)
    tight = sorted(es, key=lambda e: (e.slack, e.net, e.pad))[:5]
    if tight:
        lines.append(f"pad escapes: {len(es)} pads measured, tightest first (the widest channel out of each, on its best layer)")
        for e in tight:
            lines.append(f"  {e.pad} [{e.net}] on {e.layer}: {e.slack:+g} mm at {e.at} ({e.open_ways} of {e.ways} channels fit a {e.width:g} mm track)")
    mv = moves(es)
    if mv:
        lines.append(f"sealed: {len(mv)} pad{'s' if len(mv) > 1 else ''} with no way out on any layer their net may use")
        for m in mv:
            lines.append(f"  - {m}")
    else:
        lines.append("sealed: none — every pad has a channel its own net fits through")
    return lines


def class_widths(job) -> dict[str, float]:
    """Each net's class track width, off the constraint compiler. Never a guess.

    The same number `krt_plan` puts on the command line, so a channel this module calls too narrow is
    too narrow for the track the router will actually draw. A net with no `NetReq` has no entry and
    the caller falls back to `stackup.track_min`, which is what KRT is handed for it.
    """
    cls = {c.name: c.track_width_mm for c in (job.classes or ())}
    cs = getattr(job, "constraints", None)
    if cs is None:
        return {}
    return {c.net: cls[c.class_name] for c in cs.constraints if c.class_name in cls}
