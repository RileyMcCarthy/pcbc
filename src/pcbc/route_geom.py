"""Exact copper geometry: one shape, one distance, one path form.

This is the core every R2 pattern stands on and the module R3's Rust router ports, so it is
stdlib only and imports nothing from pcbc. Three ideas and nothing else:

**One shape.** Every obstacle and every candidate piece of copper is the Minkowski sum
`hull(pts) + disc(r)` (`Shape`). That one form is exact for everything KiCad draws in copper — a
track segment is a capsule, a via is a disc, a roundrect pad is its inset corners offset by the
corner radius — so there is no shape zoo and no per-item special case in the distance function.
The rule that makes the core sound, and the only rule: **a shape is the true copper or a strict
superset of it, never a subset** (`docs/r2-design.md` A.1). Clearing a superset clears the truth.
Refusing because of a superset costs a pattern a fit, which is a design conversation.

**One distance.** `hull_dist2` is a squared distance and `clears` compares squares, so the accept
path has no `sqrt`, no `hypot` and no trigonometry: accept and reject are pure IEEE-754 double
arithmetic and identical on every platform. `gap` is the only function here that takes a square
root, and it exists for report lines, never for a decision.

**One quantisation.** KiCad's unit is 1 nm and `f"{v:.6f}"` is what a board file gets written
with, so every coordinate entering a `Shape` or a `Path` passes through `q` exactly once, at
construction. The geometry pcbc checks is bit-for-bit the geometry KiCad parses; there is no
"we rounded on the way out" class of bug. The predicates that decide overlap (orientation,
containment, segment crossing) run on integer nanometres, so they are exact rather than
merely repeatable.

Everything here is pure and deterministic: the same inputs give byte-identical outputs, there is
no global state, and nothing iterates a dict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Pt = tuple[float, float]
Box = tuple[float, float, float, float]  # x0, y0, x1, y1, always x0 <= x1 and y0 <= y1

NM = 1e-6
"""KiCad's unit, in mm: one nanometre, and the last digit `f"{v:.6f}"` writes."""

EPS_MM = 1e-4
"""0.1 um, added to every requirement and never subtracted, so pcbc is always this much stricter
than KiCad. Three orders above the double-precision error on a 100 mm coordinate (~1e-11 mm) and
below KiCad's own display resolution, so it can never turn a real violation into a pass and never
shows up as a number a human has to reconcile."""

MICRO_MM = 0.2
"""`copper_bar.MICRO_MM`, re-exported so there is one of it: a segment shorter than this is a
grid artefact, not a route. It is also KiCad's `track_segment_length (min 0.2mm)` rule."""

MAX_PTS = 16
"""A hull's point ceiling. Chosen so the worst-case hull-to-hull test stays at 16 x 16 edge pairs
and the Rust port can hold a shape in a fixed-size array; `hull` reduces a richer outline to a
superset within the ceiling rather than refusing it."""


# --- quantisation ---------------------------------------------------------------------------------


def q(v: float) -> float:
    """One coordinate on KiCad's 1 nm grid: `round(v * 1e6) / 1e6`, A.1's formula verbatim.

    A coordinate exactly half a nanometre from the grid goes to the even nanometre, because that is
    what Python's `round` does and the formula is the specification. Which way a tie falls does not
    matter; that it always falls the same way does.

    Negative zero is normalised to zero: `f"{-0.0:.6f}"` writes `-0.000000`, which is a different
    byte string for the same point, and byte-identical output is a contract here.
    """
    if not math.isfinite(v):
        raise ValueError(f"coordinate is not finite: {v!r}")
    n = round(v * 1e6)
    return n / 1e6 if n else 0.0


def qp(p: Pt) -> Pt:
    """One point on KiCad's 1 nm grid."""
    return (q(p[0]), q(p[1]))


def _nm(v: float) -> int:
    """A quantised coordinate as an exact integer count of nanometres."""
    return round(v * 1e6)


def _inm(pts: tuple[Pt, ...]) -> tuple[tuple[int, int], ...]:
    return tuple((round(x * 1e6), round(y * 1e6)) for x, y in pts)


def _mm(n: int) -> float:
    return n / 1e6 if n else 0.0


# --- hulls ----------------------------------------------------------------------------------------


def _cross(o: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> int:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def hull(pts: tuple[Pt, ...] | list[Pt]) -> tuple[Pt, ...]:
    """The convex hull of `pts`, quantised first and wound counter-clockwise in coordinate values.

    Quantise *then* hull, never the other way round: rounding a hull vertex afterwards can push it
    a nanometre outside the edge it sits on and leave a shape that is no longer convex. Collinear
    points are dropped, so the result is strictly convex and `Shape`'s invariant holds.

    KiCad's frame is y-down, so a counter-clockwise winding in the numbers reads as clockwise on
    screen. Which one it is does not matter; that it is always the same one does, because the
    containment predicate reads the sign of the cross product.

    Rounding is to the nearest nanometre, so a shape built from a coordinate that is not already on
    the grid — a corner computed from a half-nanometre half-size, say — can sit up to half a
    nanometre inside its true copper. That is deliberate: matching KiCad's own parse exactly is
    worth more than the half nanometre, and `EPS_MM` is 100 nm of one-sided margin on top, two
    hundred times the worst case. Everything read out of a board file is already on the grid, where
    `q` is the identity and the question does not arise.

    An outline with more than `MAX_PTS` hull vertices is reduced to a superset within the ceiling
    (see `_drop_edge`), never truncated: dropping a vertex would make the shape a subset of the
    copper, which is the one thing this module may not do.
    """
    ip = sorted({(_nm(x), _nm(y)) for x, y in (qp(p) for p in pts)})
    chain = _chain(ip)
    while len(chain) > MAX_PTS:
        cut = _drop_edge(chain)
        chain = _box_chain(ip) if cut is None else _chain(cut)
    if len(chain) >= 3 and any(not _in_hull(p, chain) for p in ip):
        chain = _box_chain(ip)
    return tuple((_mm(x), _mm(y)) for x, y in chain)


def _chain(ip: list[tuple[int, int]] | set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Andrew's monotone chain over sorted, deduplicated integer points. Collinear points are
    popped, so the result is strictly convex; 1 and 2 points come back unchanged."""
    pts = sorted(set(ip))
    if len(pts) <= 2:
        return pts
    lower: list[tuple[int, int]] = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[int, int]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _drop_edge(chain: list[tuple[int, int]]) -> list[tuple[int, int]] | None:
    """One point fewer: delete the edge whose removal adds the least area, and extend its two
    neighbouring edges to where they meet.

    Extending edges only ever grows a convex polygon, so the step keeps the superset rule — but
    only if the new vertex lands on the grid *outside* the exact meeting point. Rounded to nearest
    it lands a fraction inside, and the two vertices being replaced then sit a hair outside the new
    edges, which is a subset by half a nanometre and would send every reduction to the bounding box
    (measured: a 20-point ring reduced to its box on the first step). So `_line_meet` searches
    outward from the rounded point for the first grid point that provably keeps both of them
    inside, with exact integer predicates. Ties break on the lowest index, which is what makes the
    reduction deterministic.
    """
    n = len(chain)
    best: tuple[float, int, tuple[int, int]] | None = None
    for i in range(n):
        a, b, c, d = chain[(i - 1) % n], chain[i], chain[(i + 1) % n], chain[(i + 2) % n]
        v = _line_meet(a, b, c, d)
        if v is None:
            continue
        added = abs(_cross(b, c, v)) / 2.0
        if best is None or (added, i) < (best[0], best[1]):
            best = (added, i, v)
    if best is None:
        return None
    i, v = best[1], best[2]
    return [v] + [chain[(i + 2 + k) % n] for k in range(n - 2)]


_PUSH_NM = 8
"""How far `_line_meet` may walk outward looking for a grid point that keeps the replaced vertices
inside. Measured over ~800 000 calls on seeded rings and ellipses of 17 to 999 points: 46 % land
on the rounded point itself, 53 % one nanometre out, and 1.9 % need 2 to 8. The 0.03 % that find
nothing inside 8 nm return None, and `_drop_edge` simply deletes a different edge instead; only if
every edge fails does `hull` fall back to the bounding box."""


def _line_meet(
    a: tuple[int, int], b: tuple[int, int], c: tuple[int, int], d: tuple[int, int]
) -> tuple[int, int] | None:
    """The grid point that replaces b and c when edge b -> c is deleted: where line ab and line cd
    meet, nudged outward until b and c are provably inside the edges a -> v and v -> d.

    Returns None when the two edges barely turn — `t` is measured in multiples of the edge a -> b,
    so a million of them is parallel for any practical purpose and the point it would put there is
    a worse shape than the bounding box — and when no grid point within `_PUSH_NM` works.
    """
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if den == 0:
        return None
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den
    if not (1.0 <= t <= 1e6):  # the meeting point must lie beyond b, and not at infinity
        return None
    vx, vy = a[0] + t * r[0], a[1] + t * r[1]
    ux = 1 if vx * 2.0 >= b[0] + c[0] else -1  # outward, away from the edge being deleted
    uy = 1 if vy * 2.0 >= b[1] + c[1] else -1
    for k in range(_PUSH_NM + 1):
        for sx, sy in ((0, 0), (ux, 0), (0, uy), (ux, uy)):
            v = (round(vx) + sx * k, round(vy) + sy * k)
            if (
                _cross(a, v, b) >= 0
                and _cross(a, v, c) >= 0
                and _cross(v, d, b) >= 0
                and _cross(v, d, c) >= 0
            ):
                return v
    return None


def _box_chain(ip: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The last resort: the axis-aligned bounding box, a superset by inspection."""
    xs = [p[0] for p in ip]
    ys = [p[1] for p in ip]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    return _chain([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _in_hull(p: tuple[int, int], poly: list[tuple[int, int]] | tuple[tuple[int, int], ...]) -> bool:
    """Is the integer point inside the counter-clockwise integer hull, boundary included? Exact."""
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        if _cross(poly[i], poly[(i + 1) % n], p) < 0:
            return False
    return True


# --- the shape ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    """`hull(pts) + disc(r)`: the true copper of a KiCad item, or a strict superset of it.

    `pts` is already quantised, strictly convex and counter-clockwise — build one with `hull` or
    one of the `*_shape` constructors rather than by hand.
    """

    pts: tuple[Pt, ...]
    r: float = 0.0

    def __post_init__(self) -> None:
        n = len(self.pts)
        if not 1 <= n <= MAX_PTS:
            raise ValueError(f"a Shape holds 1..{MAX_PTS} points, got {n}")
        if not math.isfinite(self.r) or self.r < 0.0:
            raise ValueError(f"offset radius must be >= 0, got {self.r!r}")
        for x, y in self.pts:
            if q(x) != x or q(y) != y:
                raise ValueError(f"Shape point {(x, y)!r} is not on the 1 nm grid; pass it through qp")
        if n >= 3:
            ip = _inm(self.pts)
            for i in range(n):
                if _cross(ip[i], ip[(i + 1) % n], ip[(i + 2) % n]) <= 0:
                    raise ValueError(f"Shape points are not a strictly convex CCW hull: {self.pts!r}")


def _rot_cs(deg: float) -> tuple[float, float]:
    """KiCad's own rotation, as `copper._rotate` applies it: the angle is negated, because a
    positive KiCad angle turns counter-clockwise on screen and the file's frame is y-down.

    Right angles are returned exactly. `math.cos(math.radians(90))` is 6.1e-17, not 0, and that
    stray term turns a rect pad at 90 degrees into a shape whose corners miss the grid by a
    nanometre and whose stub is no longer axis-aligned.
    """
    r = deg % 360.0
    exact = {0.0: (1.0, 0.0), 90.0: (0.0, -1.0), 180.0: (-1.0, 0.0), 270.0: (0.0, 1.0)}
    if r in exact:
        return exact[r]
    a = math.radians(-r)
    return (math.cos(a), math.sin(a))


def _place(cx: float, cy: float, local: tuple[Pt, ...], deg: float) -> tuple[Pt, ...]:
    c, s = _rot_cs(deg)
    return tuple((cx + x * c - y * s, cy + x * s + y * c) for x, y in local)


def rect_shape(cx: float, cy: float, w: float, h: float, rot: float = 0.0) -> Shape:
    """A rect pad, a keepout, a lane strip: the four corners, no offset. Exact."""
    hx, hy = w / 2.0, h / 2.0
    corners = ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
    return Shape(hull(_place(cx, cy, corners, rot)), 0.0)


def roundrect_shape(
    cx: float, cy: float, w: float, h: float, rratio: float, rot: float = 0.0
) -> Shape:
    """A roundrect pad: the four corners inset by the corner radius, offset by that radius. Exact,
    and it is also the shape used for a *chamfered* roundrect, where it is a superset — a chamfer
    only ever removes copper from a corner, so ignoring it can only make the obstacle bigger."""
    rad = rratio * min(w, h)
    if rad <= 0.0:
        return rect_shape(cx, cy, w, h, rot)
    hx, hy = max(w / 2.0 - rad, 0.0), max(h / 2.0 - rad, 0.0)
    corners = ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
    return Shape(hull(_place(cx, cy, corners, rot)), rad)


def circle_shape(cx: float, cy: float, diameter: float) -> Shape:
    """A circular pad, a via ring, a round drill: one point and a radius. Exact."""
    return Shape((qp((cx, cy)),), diameter / 2.0)


def oval_shape(cx: float, cy: float, w: float, h: float, rot: float = 0.0) -> Shape:
    """An oval pad or an `(drill oval w h)` hole: a capsule, so the two end-cap centres offset by
    the half-width. Exact — KiCad draws an oval pad as exactly this stadium."""
    r = min(w, h) / 2.0
    reach = abs(w - h) / 2.0
    local: tuple[Pt, ...] = ((-reach, 0.0), (reach, 0.0)) if w >= h else ((0.0, -reach), (0.0, reach))
    return Shape(hull(_place(cx, cy, local, rot)), r)


def poly_shape(pts: tuple[Pt, ...] | list[Pt], stroke: float = 0.0) -> Shape:
    """A `gr_poly` / `gr_rect` custom-pad primitive, or a trapezoid pad's four true corners.

    The hull of the outline plus half the stroke width. Exact for a convex outline (a trapezoid, a
    rect); a **superset** for a concave one, since the hull fills the concavity. That is the right
    side of the only rule: the USB-C shield pad declares `(size 0.005 0.005)` and draws a
    0.599974 x 1.299997 mm `gr_poly` with a `(width 0.1)` stroke, i.e. 0.700 x 1.400 mm of real
    copper (`docs/r2-design.md` A.1).
    """
    return Shape(hull(tuple(pts)), stroke / 2.0)


def track_shape(a: Pt, b: Pt, width: float) -> Shape:
    """A track segment: its two centreline ends offset by half its width. Exact — KiCad's
    `SHAPE_SEGMENT` is precisely this capsule, round caps included."""
    return Shape(hull((a, b)), width / 2.0)


def via_shape(at: Pt, diameter: float) -> Shape:
    """A via's copper ring. Exact."""
    return circle_shape(at[0], at[1], diameter)


def hole_shape(cx: float, cy: float, w: float, h: float | None = None, rot: float = 0.0) -> Shape:
    """A drilled hole: `(drill d)` is a disc, `(drill oval w h)` is a capsule in the pad's frame.

    Reading an oval drill as a scalar under-models the hole by 0.35 mm at each end on the USB-C
    legs of both USB boards, and rule 2 of A.4 (hole to copper) is the check that catches a via
    parked in that gap.
    """
    if h is None or h == w:
        return circle_shape(cx, cy, w)
    return oval_shape(cx, cy, w, h, rot)


def aabb(s: Shape) -> Box:
    """The shape's axis-aligned bounding box, offset radius included. For indexing only: a box is
    never a clearance answer, because a capsule's box is up to `r` bigger than the capsule."""
    xs = [p[0] for p in s.pts]
    ys = [p[1] for p in s.pts]
    return (min(xs) - s.r, min(ys) - s.r, max(xs) + s.r, max(ys) + s.r)


def box_grow(box: Box, d: float) -> Box:
    """The box grown by `d` on every side."""
    x0, y0, x1, y1 = box
    return (min(x0, x1) - d, min(y0, y1) - d, max(x0, x1) + d, max(y0, y1) + d)


# --- distance -------------------------------------------------------------------------------------


def _pt_seg_d2(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll <= 0.0:
        ex, ey = px - ax, py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / ll  # the one division in the accept path
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    ex = ax + t * dx - px
    ey = ay + t * dy - py
    return ex * ex + ey * ey


def _edges(pts: tuple[Pt, ...]) -> tuple[tuple[Pt, Pt], ...]:
    """A hull's edges: none for a point, one for a segment, n for a polygon."""
    n = len(pts)
    if n < 2:
        return ()
    if n == 2:
        return ((pts[0], pts[1]),)
    return tuple((pts[i], pts[(i + 1) % n]) for i in range(n))


def _iedges(ip: tuple[tuple[int, int], ...]) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    n = len(ip)
    if n < 2:
        return ()
    if n == 2:
        return ((ip[0], ip[1]),)
    return tuple((ip[i], ip[(i + 1) % n]) for i in range(n))


def hull_dist2(a: tuple[Pt, ...], b: tuple[Pt, ...]) -> float:
    """Squared distance between two convex hulls; exactly 0.0 when they touch or overlap.

    Overlap is decided by exact integer predicates — a vertex of one inside the other, or a pair
    of edges properly crossing — so the answer never depends on how the floats rounded. Only the
    separated case does arithmetic, and it is the textbook minimum over vertex-to-edge distances
    in both directions: at most 16 x 16 x 2 point-segment tests, and 2 x 2 for the common case of
    a track capsule against a via.
    """
    ia, ib = _inm(a), _inm(b)
    if len(ia) >= 3:
        for p in ib:
            if _in_hull(p, ia):
                return 0.0
    if len(ib) >= 3:
        for p in ia:
            if _in_hull(p, ib):
                return 0.0
    ea, eb = _edges(a), _edges(b)
    if ea and eb:
        for p1, p2 in _iedges(ia):
            for p3, p4 in _iedges(ib):
                d1 = _cross(p1, p2, p3)
                d2 = _cross(p1, p2, p4)
                d3 = _cross(p3, p4, p1)
                d4 = _cross(p3, p4, p2)
                if d1 * d2 < 0 and d3 * d4 < 0:
                    return 0.0
    if not ea and not eb:
        ex, ey = a[0][0] - b[0][0], a[0][1] - b[0][1]
        return ex * ex + ey * ey
    best = math.inf
    for px, py in a:
        for (ax, ay), (bx, by) in eb:
            d2 = _pt_seg_d2(px, py, ax, ay, bx, by)
            if d2 < best:
                best = d2
    for px, py in b:
        for (ax, ay), (bx, by) in ea:
            d2 = _pt_seg_d2(px, py, ax, ay, bx, by)
            if d2 < best:
                best = d2
    return best


def clears(a: Shape, b: Shape, need: float) -> bool:
    """Do these two shapes keep `need` mm of copper-to-copper air between them?

    The comparison is on squares, so there is no square root in the accept path, and the
    requirement carries `EPS_MM` on top, so pcbc is strictly stricter than KiCad by 100 nm and a
    shape that only just fits is refused rather than handed to the arbiter to argue about.
    """
    want = need + a.r + b.r + EPS_MM
    return hull_dist2(a.pts, b.pts) >= want * want


def gap(a: Shape, b: Shape) -> float:
    """The air between two shapes in mm, 4 dp, **for report lines only — never for a decision.**

    A negative number means they overlap; its magnitude is the offset radii, not the true
    penetration depth, because `hull_dist2` stops at 0.0 once two hulls meet.
    """
    return round(math.sqrt(hull_dist2(a.pts, b.pts)) - a.r - b.r, 4)


# --- paths ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Path:
    """A run of copper on one layer: quantised points, a width and a layer name.

    A `Path` does not enforce 0/45/90 — `is_octilinear` and `turn_ok` are the judges, and
    `route_verify` runs them over every piece. What it does enforce is that the points are already
    on KiCad's grid, so nothing downstream rounds them a second time.
    """

    pts: tuple[Pt, ...]
    w: float
    layer: str

    def __post_init__(self) -> None:
        if len(self.pts) < 2:
            raise ValueError(f"a Path needs at least 2 points, got {len(self.pts)}")
        if not math.isfinite(self.w) or self.w <= 0.0:
            raise ValueError(f"track width must be > 0, got {self.w!r}")
        if not self.layer:
            raise ValueError("a Path needs a layer")
        for x, y in self.pts:
            if q(x) != x or q(y) != y:
                raise ValueError(f"Path point {(x, y)!r} is not on the 1 nm grid; pass it through qp")


def is_octilinear(pts: tuple[Pt, ...]) -> bool:
    """Is every leg horizontal, vertical or at exactly 45 degrees?

    Measured on integer nanometres, so this is exact rather than "within half a degree". KiCad's
    `track_angle` rule and the copper bar's `off_45` both allow a tolerance; pcbc's own copper
    does not need one, because it is built on the grid instead of snapped onto it.
    """
    ip = _inm(pts)
    for (x1, y1), (x2, y2) in zip(ip, ip[1:]):
        dx, dy = x2 - x1, y2 - y1
        if dx != 0 and dy != 0 and abs(dx) != abs(dy):
            return False
    return True


def octant(a: Pt, b: Pt) -> int:
    """The compass octant of the leg a -> b, 0..7, in KiCad's y-down frame.

    0 east, then one step per 45 degrees the way y grows: 1 south-east, 2 south, 3 south-west,
    4 west, 5 north-west, 6 north, 7 north-east. So a leg going up and to the right is 7 and a leg
    going east is 0, which is the pair `turn_ok` has to read as a legal 45-degree turn.

    Defined for an octilinear leg; a leg at some other angle is classified by the signs of its
    deltas alone. A zero-length leg has no direction and raises.
    """
    x1, y1 = _nm(a[0]), _nm(a[1])
    x2, y2 = _nm(b[0]), _nm(b[1])
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        raise ValueError(f"a zero-length leg has no direction: {a!r} -> {b!r}")
    if dy == 0:
        return 0 if dx > 0 else 4
    if dx == 0:
        return 2 if dy > 0 else 6
    if dx > 0:
        return 1 if dy > 0 else 7
    return 3 if dy > 0 else 5


def turn_ok(pts: tuple[Pt, ...]) -> bool:
    """Does the path only ever turn by 45 degrees — interior angle at least 135?

    The octant difference **must wrap**: north-east (7) followed by east (0) is a legal 45-degree
    turn and `abs(7 - 0) = 7` would reject it, so the test is `min(d, 8 - d) <= 1`. All 64 octant
    pairs are pinned by `test_turn_ok_wraps`.
    """
    for i in range(1, len(pts) - 1):
        d = abs(octant(pts[i - 1], pts[i]) - octant(pts[i], pts[i + 1]))
        if min(d, 8 - d) > 1:
            return False
    return True


def seg_lengths(pts: tuple[Pt, ...]) -> tuple[float, ...]:
    """Each leg's length in mm. Not the accept path, so the square roots are free."""
    return tuple(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def path_mm(pts: tuple[Pt, ...]) -> float:
    """The path's total length in mm."""
    return math.fsum(seg_lengths(pts))


def clip_len_in_box(a: Pt, b: Pt, box: Box) -> float:
    """How many mm of the segment a -> b lie inside the axis-aligned box (Liang-Barsky).

    This is how much of a piece runs through a rule area, a fanout lane or a plane's extent —
    a measurement for a report or a cost, never an accept.
    """
    x0, y0, x1, y1 = box
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, r in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0.0:
            if r < 0.0:
                return 0.0
        else:
            t = r / p
            if p < 0.0:
                if t > t1:
                    return 0.0
                if t > t0:
                    t0 = t
            else:
                if t < t0:
                    return 0.0
                if t < t1:
                    t1 = t
    return math.hypot(dx, dy) * (t1 - t0)


def octile_corner(a: Pt, b: Pt, *, diagonal_first: bool = True) -> Pt:
    """The one corner of the two-leg 0/45/90 path from a to b.

    Both legs are derived from the **already quantised** endpoints in integer nanometres, so the
    diagonal leg is exactly diagonal and the straight leg is exactly straight — never "a rounded
    endpoint and whatever angle that leaves". This is A.5's second construction rule: quantise
    once, then derive, so a corner computed from a rounded midpoint still carries an exact 45.

    When a -> b is already octilinear the corner is b itself and `octile_path` drops it.
    """
    ax, ay = _nm(a[0]), _nm(a[1])
    bx, by = _nm(b[0]), _nm(b[1])
    dx, dy = bx - ax, by - ay
    if dx == 0 or dy == 0 or abs(dx) == abs(dy):
        return (_mm(bx), _mm(by))
    run = min(abs(dx), abs(dy))
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    if diagonal_first:
        return (_mm(ax + sx * run), _mm(ay + sy * run))
    return (_mm(bx - sx * run), _mm(by - sy * run))


def octile_path(a: Pt, b: Pt, *, diagonal_first: bool = True) -> tuple[Pt, ...]:
    """The 0/45/90 path from a to b: two points when it is already octilinear, else three.

    `is_octilinear` and `turn_ok` are true of the result by construction, for every a and b.
    """
    a, b = qp(a), qp(b)
    c = octile_corner(a, b, diagonal_first=diagonal_first)
    if c == a or c == b:
        return (a, b)
    return (a, c, b)
