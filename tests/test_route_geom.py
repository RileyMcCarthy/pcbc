"""The exact-geometry core: one shape, one distance, one path form.

`docs/r2-design.md` A.1, A.2 and A.5, one assertion per rule, plus the property tests of D.3 —
which are the point of this slice and not an extra, because every pattern R2 writes and R3's maze
router are judged by `clears` and nothing else. Every pinned number carries its reference in the
assertion message, so a drift names the rule it left.

The independent judge in this file is deliberately written a different way from the module: the
module decides overlap with exact integer predicates and measures with vertex-to-edge distances;
`_brute_dist2` below casts rays and runs Ericson's clamped-parametric segment-segment solver. Two
implementations agreeing is evidence; one implementation agreeing with itself is not.
"""

from __future__ import annotations

import json
import math
import os
import random
from pathlib import Path

import pytest

from pcbc.route_geom import (
    EPS_MM,
    MAX_PTS,
    MICRO_MM,
    NM,
    Path as GeomPath,
    Shape,
    aabb,
    box_grow,
    circle_shape,
    clears,
    clip_len_in_box,
    gap,
    hole_shape,
    hull,
    hull_dist2,
    is_octilinear,
    octant,
    octile_corner,
    octile_path,
    oval_shape,
    path_mm,
    poly_shape,
    q,
    qp,
    rect_shape,
    roundrect_shape,
    seg_lengths,
    track_shape,
    turn_ok,
    via_shape,
)

SEED = 20260920  # the day the design was written; fixed so CI is deterministic
CASES = 2000  # D.3's number, and the size of the golden-vector fixture R3 replays
VECTORS = Path(__file__).resolve().parent / "fixtures" / "geom_vectors.json"

# The compiled clearances of the five boards (docs/r2-design.md A.3), so the random requirements
# are the numbers the router will really be handed and not a made-up range.
NEEDS = (0.0, 0.0889, 0.1, 0.127, 0.155, 0.16, 0.18, 0.2, 0.25, 0.5)


# --- the independent judge ------------------------------------------------------------------------


def _seg_seg_d2(p1, q1, p2, q2) -> float:
    """Squared distance between two segments, Ericson's clamped-parametric solver (Real-Time
    Collision Detection 5.1.9). Independent of the module's vertex-to-edge decomposition."""
    d1 = (q1[0] - p1[0], q1[1] - p1[1])
    d2 = (q2[0] - p2[0], q2[1] - p2[1])
    r = (p1[0] - p2[0], p1[1] - p2[1])
    a = d1[0] * d1[0] + d1[1] * d1[1]
    e = d2[0] * d2[0] + d2[1] * d2[1]
    f = d2[0] * r[0] + d2[1] * r[1]
    def clamp(v: float) -> float:
        return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)

    if a <= 0.0 and e <= 0.0:
        return r[0] * r[0] + r[1] * r[1]
    if a <= 0.0:
        s, t = 0.0, clamp(f / e)
    else:
        c = d1[0] * r[0] + d1[1] * r[1]
        if e <= 0.0:
            t, s = 0.0, clamp(-c / a)
        else:
            b = d1[0] * d2[0] + d1[1] * d2[1]
            denom = a * e - b * b
            s = clamp((b * f - c * e) / denom) if denom != 0.0 else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, clamp(-c / a)
            elif t > 1.0:
                t, s = 1.0, clamp((b - c) / a)
    cx = (p1[0] + s * d1[0]) - (p2[0] + t * d2[0])
    cy = (p1[1] + s * d1[1]) - (p2[1] + t * d2[1])
    return cx * cx + cy * cy


def _ray_inside(p, poly) -> bool:
    """Even-odd ray casting, which is how the rest of the world tests point-in-polygon."""
    if len(poly) < 3:
        return False
    x, y = p
    inside = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _loop(pts):
    if len(pts) < 2:
        return ()
    if len(pts) == 2:
        return ((pts[0], pts[1]),)
    return tuple((pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))


def _brute_dist2(a, b) -> float:
    """The truth, computed another way: 0 when either hull holds a vertex of the other, else the
    minimum over every edge pair."""
    if any(_ray_inside(p, a) for p in b) or any(_ray_inside(p, b) for p in a):
        return 0.0
    if len(a) == 1 and len(b) == 1:
        return (a[0][0] - b[0][0]) ** 2 + (a[0][1] - b[0][1]) ** 2
    if len(a) == 1:
        return min(_seg_seg_d2(a[0], a[0], e[0], e[1]) for e in _loop(b))
    if len(b) == 1:
        return min(_seg_seg_d2(e[0], e[1], b[0], b[0]) for e in _loop(a))
    return min(_seg_seg_d2(e[0], e[1], f[0], f[1]) for e in _loop(a) for f in _loop(b))


def _inside(s: Shape, p) -> bool:
    """Is the point inside `hull(s.pts) + disc(s.r)`? The tolerance is one nanometre, the grid the
    shape's own points sit on."""
    return math.sqrt(hull_dist2((p,), s.pts)) <= s.r + NM


# --- a random scene -------------------------------------------------------------------------------


def _rand_shape(rng: random.Random, cx: float, cy: float) -> Shape:
    """One obstacle of a random kind at roughly (cx, cy), with the sizes a real board carries."""
    kind = rng.choice(("rect", "roundrect", "circle", "oval", "poly", "track", "via", "drill"))
    rot = rng.choice((0.0, 45.0, 90.0, 135.0, 180.0, 270.0, rng.uniform(0.0, 360.0)))
    w = rng.uniform(0.15, 2.5)
    h = rng.uniform(0.15, 2.5)
    if kind == "rect":
        return rect_shape(cx, cy, w, h, rot)
    if kind == "roundrect":
        return roundrect_shape(cx, cy, w, h, rng.choice((0.0, 0.243902, 0.25, 0.5)), rot)
    if kind == "circle":
        return circle_shape(cx, cy, w)
    if kind == "oval":
        return oval_shape(cx, cy, w, h, rot)
    if kind == "poly":
        n = rng.randint(3, 7)
        pts = [(cx + rng.uniform(-w, w), cy + rng.uniform(-h, h)) for _ in range(n)]
        return poly_shape(pts, rng.choice((0.0, 0.1)))
    if kind == "track":
        return track_shape((cx, cy), (cx + rng.uniform(-3, 3), cy + rng.uniform(-3, 3)), w / 4.0)
    if kind == "via":
        return via_shape((cx, cy), rng.choice((0.35, 0.5, 0.6, 0.8)))
    return hole_shape(cx, cy, 0.799998, rng.choice((None, 1.199998, 1.499997)), rot)


def _shift(s: Shape, dx: float, dy: float) -> Shape:
    """The same shape somewhere else. Translation preserves convexity and winding, and both the
    points and the offset are already on the grid, so this stays a legal `Shape`."""
    return Shape(tuple(qp((x + dx, y + dy)) for x, y in s.pts), s.r)


def _reach(s: Shape) -> float:
    return max(math.hypot(x, y) for x, y in s.pts) + s.r


def _pairs(n: int):
    """n seeded (a, b, need) triples.

    The two shapes are deliberately placed so most pairs land in the band where the answer is a
    decision and not a foregone conclusion: b is pushed out along a random bearing to just about
    where it grazes a. A corpus of shapes scattered at random over a board answers `clears` with
    "obviously yes" nine times in ten and proves nothing.
    """
    rng = random.Random(SEED)
    out = []
    for _ in range(n):
        cx, cy = rng.uniform(0.0, 40.0), rng.uniform(0.0, 40.0)
        a = _rand_shape(rng, 0.0, 0.0)
        b = _rand_shape(rng, 0.0, 0.0)
        bearing = rng.uniform(0.0, 2.0 * math.pi)
        want = rng.uniform(-0.5, 0.5) if rng.random() < 0.7 else rng.uniform(0.5, 3.0)
        ux, uy = math.cos(bearing), math.sin(bearing)
        d = max(_reach(a) + _reach(b), 0.0)
        a = _shift(a, cx, cy)
        b = _shift(b, cx + d * ux, cy + d * uy)
        # One correction along the bearing lands the pair near the gap we asked for: the whole
        # point of the corpus is the band where accept and refuse are a millimetre apart.
        have = math.sqrt(hull_dist2(a.pts, b.pts)) - a.r - b.r
        back = have - want
        b = _shift(b, -back * ux, -back * uy)
        out.append((a, b, rng.choice(NEEDS)))
    return out


# --- A.1 one shape primitive, one row per KiCad item ----------------------------------------------


def test_a_track_segment_is_a_capsule():
    """A.1 row 1: KiCad's SHAPE_SEGMENT is the centreline offset by half the width, caps included."""
    s = track_shape((10.0, 10.0), (13.0, 10.0), 0.25)
    assert s.pts == ((10.0, 10.0), (13.0, 10.0)), "a segment's hull is its two centreline ends"
    assert s.r == 0.125, "A.1: r = width / 2"
    assert _inside(s, (9.9, 10.0)), "the round cap is copper: 0.1 mm past the end, inside 0.125"
    assert not _inside(s, (13.2, 10.0)), "0.2 mm past the end is outside a 0.125 mm cap"


def test_a_via_and_a_round_drill_are_one_point_and_a_radius():
    """A.1 rows 2 and 3."""
    assert via_shape((5.0, 5.0), 0.8) == Shape(((5.0, 5.0),), 0.4), "via ring: r = size / 2"
    assert hole_shape(5.0, 5.0, 0.4) == Shape(((5.0, 5.0),), 0.2), "round PTH drill: r = drill / 2"


def test_an_oval_drill_is_a_capsule_not_a_circle():
    """A.1 row 4. The USB-C legs on both USB boards carry `(drill oval 0.799998 1.499997)`; a
    scalar read of that under-models the hole by 0.35 mm at each end (docs/r2-design.md E.1)."""
    s = hole_shape(10.0, 10.0, 0.799998, 1.499997)
    assert s.r == pytest.approx(0.399999), "capsule radius = min(w, h) / 2"
    assert s.pts == ((10.0, 9.65), (10.0, 10.35)), "end-cap centres at +-(h - w) / 2 = 0.35"
    assert _inside(s, (10.0, 10.749)), "the hole really does reach 0.75 mm along its long axis"
    assert not _inside(s, (10.0, 10.751)), "and stops there"


def test_rect_roundrect_circle_and_oval_pads_are_exact():
    """A.1 rows 5 to 8, at the rratios the five boards actually carry (0, 0.243902, 0.25)."""
    assert rect_shape(0.0, 0.0, 2.0, 1.0).pts == (
        (-1.0, -0.5),
        (1.0, -0.5),
        (1.0, 0.5),
        (-1.0, 0.5),
    ), "a rect pad is its four corners with no offset"
    rr = roundrect_shape(0.0, 0.0, 2.0, 1.0, 0.25)
    assert rr.r == 0.25, "roundrect: rad = rratio * min(w, h) = 0.25 * 1.0"
    assert rr.pts == ((-0.75, -0.25), (0.75, -0.25), (0.75, 0.25), (-0.75, 0.25)), "corners inset by rad"
    assert roundrect_shape(0.0, 0.0, 2.0, 1.0, 0.0) == rect_shape(0.0, 0.0, 2.0, 1.0), "rratio 0 is a rect"
    assert circle_shape(1.0, 2.0, 1.2) == Shape(((1.0, 2.0),), 0.6)
    assert oval_shape(0.0, 0.0, 2.0, 1.0) == Shape(((-0.5, 0.0), (0.5, 0.0)), 0.5), "stadium, exact"
    assert oval_shape(0.0, 0.0, 1.0, 1.0) == Shape(((0.0, 0.0),), 0.5), "w == h degenerates to a circle"


def test_a_trapezoid_pad_is_its_four_true_corners():
    """A.1 row 9: no offset, and the hull of a convex quad is the quad."""
    s = poly_shape([(-1.0, -0.5), (1.0, -0.5), (0.6, 0.5), (-0.6, 0.5)])
    assert s.r == 0.0
    assert len(s.pts) == 4 and (0.6, 0.5) in s.pts, "the true corners, not a bounding box"
    assert not _inside(s, (0.9, 0.45)), "the sloped side is where the copper stops"


def test_a_chamfered_roundrect_is_the_unchamfered_one_and_that_is_a_superset():
    """A.1 row 10. A chamfer only ever removes copper from a corner, so ignoring it can make the
    obstacle bigger and never smaller — the right side of A.1's only rule."""
    s = roundrect_shape(0.0, 0.0, 2.0, 1.0, 0.25)
    # just inside the 0.25 mm corner fillet, and past a 0.3 mm chamfer's cut line u + v = 0.9
    chamfer_corner = (0.92, 0.42)
    assert _inside(s, chamfer_corner), "the superset keeps the corner the chamfer would have cut"


def test_a_custom_primitive_is_the_hull_of_its_points_plus_half_its_stroke():
    """A.1 row 11, on the real pad that made `pads.py` necessary: `J1.A4B9` on c3_usb declares
    `(size 0.005 0.005)` — a 5 um obstacle — and draws this nine-point concave `gr_poly` with a
    `(width 0.1)` stroke, i.e. 0.7 x 1.4 mm of copper
    (examples/c3_usb/layout/c3_usb/placed/layout.kicad_pcb, the `pad "A4B9"` block; A.1)."""
    poly = [
        (0.300076, 0.650037),
        (0.300076, -0.649935),
        (0.000051, -0.649935),
        (0.000051, -0.64991),
        (-0.299898, -0.64991),
        (-0.299898, 0.650062),
        (0.000127, 0.650062),
        (0.000127, 0.650037),
    ]
    s = poly_shape(poly, 0.1)
    assert len(s.pts) == 6, (
        "the C-shaped eight-point outline becomes a six-point hull: the notch is filled (a "
        "superset) and the export's own 25 nm wiggles survive as real hull vertices"
    )
    b = aabb(s)
    assert round(b[2] - b[0], 6) == 0.699974, "0.599974 mm of poly + 0.1 mm of stroke"
    assert round(b[3] - b[1], 6) == 1.399997, "1.299997 + 0.1: the 1.4 mm the pad really draws"
    assert _inside(s, (0.0, 0.699)), "0.7 mm from the anchor is copper, and (size 0.005) missed it"
    assert not _inside(s, (0.0, 0.7001)), "and no more than that"


def test_a_concave_outline_becomes_its_hull_which_is_a_superset():
    """A.1 row 11's honest half: the hull fills a concavity, so a custom pad can be bigger than its
    copper. That costs a fit and is reported as a move; the other direction would be unsound."""
    s = poly_shape([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (1.0, 0.5), (0.0, 2.0)])
    assert len(s.pts) == 4, "the reflex point is swallowed by the hull"
    assert _inside(s, (1.0, 1.5)), "the notch is modelled as copper: a superset, never a subset"


def test_a_keepout_and_a_lane_strip_are_their_box_corners():
    """A.1 rows 12 and 13: a policy obstacle is exact, because pcbc drew the box itself."""
    assert rect_shape(5.0, 5.0, 4.0, 2.0).pts == ((3.0, 4.0), (7.0, 4.0), (7.0, 6.0), (3.0, 6.0))


def test_a_hull_is_quantised_convex_and_capped_at_sixteen_points():
    """A.1's `pts` invariant, and the one rule that makes the core sound."""
    s = hull([(1.00000049, 2.0), (3.0, 2.0), (2.0, 3.0), (2.0, 2.0)])
    assert s == ((1.0, 2.0), (3.0, 2.0), (2.0, 3.0)), "quantised first, then hulled; interior dropped"
    ring = [(math.cos(i / 32 * 2 * math.pi), math.sin(i / 32 * 2 * math.pi)) for i in range(32)]
    reduced = hull(ring)
    assert len(reduced) == MAX_PTS, f"a hull is capped at {MAX_PTS} points"
    for p in ring:
        assert _inside(Shape(reduced), p), "the cap reduces to a superset, it never truncates"
    assert _area(reduced) < 3.25, (
        f"the reduction gave away {_area(reduced):.4f} mm2 where the inscribed 32-gon holds "
        "3.1365 and its bounding box 4.0: it fell back to the box instead of extending edges"
    )
    for n in (17, 64, 200):
        many = [(math.cos(i / n * 2 * math.pi), math.sin(i / n * 2 * math.pi)) for i in range(n)]
        cut = hull(many)
        assert len(cut) == MAX_PTS and _area(cut) < 3.25, f"{n}-gon reduced badly"
        for p in many:
            assert _inside(Shape(cut), p), f"{n}-gon: {p} fell outside its own reduction"
    with pytest.raises(ValueError, match="strictly convex"):
        Shape(((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (0.0, 1.0)))
    with pytest.raises(ValueError, match="1 nm grid"):
        Shape(((0.0000001, 0.0),))
    with pytest.raises(ValueError, match="1..16 points"):
        Shape(())


def test_a_pads_corners_turn_the_way_pcb_place_turns_its_centre():
    """The geometry core has to rotate a pad exactly as the rest of pcbc does, or a turned pad's
    copper lands somewhere its centre is not. KiCad negates the angle (`copper._rotate`, which
    `pcb_place.Foot.pad_world` calls); right angles are returned exactly, because
    `math.cos(math.radians(90))` is 6.1e-17 and that stray term misses the grid by a nanometre."""
    from pcbc.copper import _rotate
    from pcbc.route_geom import _place, _rot_cs

    for deg in (0.0, 30.0, 45.0, 90.0, 137.5, 180.0, 270.0, -45.0, 451.0):
        want = _rotate(0.7, -0.3, deg)
        got = _place(0.0, 0.0, ((0.7, -0.3),), deg)[0]
        assert got == pytest.approx(want, abs=1e-12), f"{deg} deg turns the wrong way"
    assert _rot_cs(90.0) == (0.0, -1.0), "a right angle is exact, not 6.1e-17 off"
    assert _rot_cs(270.0) == (0.0, 1.0) and _rot_cs(360.0) == (1.0, 0.0)
    assert rect_shape(4.0, 4.0, 2.0, 1.0, 90.0).pts == (
        (3.5, 3.0),
        (4.5, 3.0),
        (4.5, 5.0),
        (3.5, 5.0),
    ), "a rect pad at 90 degrees is exactly axis-aligned, to the nanometre"


def _area(pts) -> float:
    return abs(
        sum(
            pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
            for i in range(len(pts))
        )
    ) / 2.0


def test_aabb_carries_the_offset_and_box_grow_grows_every_side():
    s = track_shape((1.0, 1.0), (4.0, 1.0), 0.4)
    assert aabb(s) == (0.8, 0.8, 4.2, 1.2), "the box of a capsule includes r on every side"
    assert box_grow((0.0, 0.0, 1.0, 1.0), 0.5) == (-0.5, -0.5, 1.5, 1.5)


# --- A.2 distance ---------------------------------------------------------------------------------


def test_hull_dist2_is_zero_exactly_when_shapes_touch_or_overlap():
    a = rect_shape(0.0, 0.0, 2.0, 2.0)
    assert hull_dist2(a.pts, rect_shape(2.0, 0.0, 2.0, 2.0).pts) == 0.0, "edge to edge is touching"
    assert hull_dist2(a.pts, rect_shape(1.0, 0.0, 2.0, 2.0).pts) == 0.0, "overlapping"
    assert hull_dist2(a.pts, rect_shape(0.0, 0.0, 0.5, 0.5).pts) == 0.0, "one hull inside the other"
    assert hull_dist2(a.pts, rect_shape(0.0, 0.0, 6.0, 0.5).pts) == 0.0, "a cross: only edges meet"
    assert hull_dist2(a.pts, rect_shape(3.0, 0.0, 2.0, 2.0).pts) == 1.0, "1 mm apart, squared"


def test_clears_is_one_sided_so_a_shape_that_only_just_fits_is_refused():
    """A.2 property 3: EPS_MM is added to every requirement and never subtracted, so pcbc is
    stricter than KiCad by exactly 100 nm and never hands the arbiter a coin flip."""
    a = via_shape((0.0, 0.0), 0.8)
    b = via_shape((1.2, 0.0), 0.8)  # centres 1.2 apart, rings 0.4: 0.4 mm of air
    assert gap(a, b) == 0.4, "0.4 mm of copper-to-copper air"
    assert clears(a, b, 0.4 - EPS_MM), "below the requirement by EPS: accepted"
    assert not clears(a, b, 0.4), "exactly at the requirement: refused, because EPS is one-sided"
    assert not clears(a, b, 0.4 + EPS_MM)


def test_clears_reads_hole_to_hole_edge_to_edge_as_kicad_does():
    """A.4 rule 3, which is the number `clears` has to get right for free: KiCad measures
    hole_to_hole between hole *edges*, so two 0.3 mm drills need 0.8 mm centre to centre on 2L
    (docs/r2-design.md A.4) — writing it as `centres >= hole_to_hole` is short by a diameter."""
    a = hole_shape(0.0, 0.0, 0.3)
    assert gap(a, hole_shape(0.8, 0.0, 0.3)) == 0.5, "0.8 mm centres is 0.5 mm edge to edge"
    assert clears(a, hole_shape(0.8001, 0.0, 0.3), 0.5), "0.8 mm + EPS_MM of centres clears"
    assert not clears(a, hole_shape(0.8, 0.0, 0.3), 0.5), "exactly 0.8 is refused: EPS is one-sided"
    assert not clears(a, hole_shape(0.5, 0.0, 0.3), 0.5), (
        "the wrong reading, `centres >= hole_to_hole`, would accept 0.5 mm centres and fire on "
        "the very first pair of taps (docs/r2-design.md A.4 rule 3)"
    )


def test_gap_is_a_report_number_and_says_so_when_shapes_overlap():
    a, b = rect_shape(0.0, 0.0, 2.0, 2.0), rect_shape(3.0, 0.0, 1.0, 1.0)
    assert gap(a, b) == 1.5, "2.5 - 1.0: 4 dp, for a report line"
    assert gap(circle_shape(0.0, 0.0, 2.0), circle_shape(0.5, 0.0, 2.0)) == -1.5, (
        "offset shapes overlapping: negative, by the radii the centres do not account for"
    )
    assert gap(rect_shape(0.0, 0.0, 2.0, 2.0), rect_shape(1.0, 0.0, 2.0, 2.0)) == 0.0, (
        "two hulls with no offset overlapping report 0.0, not a penetration depth: hull_dist2 "
        "stops at 0. This is exactly why gap is a report number and clears is the decision"
    )
    assert not clears(rect_shape(0.0, 0.0, 2.0, 2.0), rect_shape(1.0, 0.0, 2.0, 2.0), 0.0), (
        "and the decision gets it right where the report line cannot"
    )


def test_no_transcendental_reaches_the_accept_path():
    """A.2 property 2, pinned on the bytecode rather than on a promise: `sqrt`, `hypot` and the
    trigonometry live in `gap`, in messages and in the bar, never in a decision."""
    import pcbc.route_geom as rg

    banned = {"sqrt", "hypot", "dist", "sin", "cos", "tan", "atan2", "radians", "pow", "fsum"}
    for fn in (rg.clears, rg.hull_dist2, rg._pt_seg_d2, rg._in_hull, rg._cross, rg._inm, rg._edges, rg._iedges):
        names = set(fn.__code__.co_names)
        assert not (names & banned), f"{fn.__name__} reaches for {sorted(names & banned)}"
    assert "sqrt" in rg.gap.__code__.co_names, "gap is where the square root belongs"


def test_the_one_division_in_the_accept_path_is_the_projection():
    """A.2's wording, counted in the bytecode: comparisons are squared, and division appears once,
    in the vertex-to-edge projection. A degenerate (zero-length) edge never even reaches it."""
    import dis

    import pcbc.route_geom as rg

    def divisions(fn):
        return sum(1 for i in dis.get_instructions(fn) if i.opname == "BINARY_OP" and i.argrepr == "/")

    assert divisions(rg._pt_seg_d2) == 1, "the projection's t, and nothing else"
    assert divisions(rg.hull_dist2) == 0 and divisions(rg.clears) == 0 and divisions(rg._cross) == 0
    assert rg._pt_seg_d2(0.0, 1.0, 5.0, 5.0, 5.0, 5.0) == 25.0 + 16.0, "a point-edge, no division"


# --- A.5 paths ------------------------------------------------------------------------------------


def test_is_octilinear_is_exact_on_nanometres_not_a_tolerance():
    assert is_octilinear(((0.0, 0.0), (1.0, 0.0), (2.0, 1.0), (2.0, 5.0)))
    assert not is_octilinear(((0.0, 0.0), (1.0, 0.999999))), "one nanometre off 45 is off 45"
    assert is_octilinear(((0.0, 0.0), (1.0, 1.0)))
    assert not is_octilinear(((20.75, 18.6385), (21.8, 18.65))), (
        "the tilted c3_usb escape stub S1 fixed (docs/r2-measurements.md), 0.6275 deg"
    )


def test_octant_is_the_compass_in_kicads_y_down_frame():
    """A.5: 0 east, then one step per 45 degrees the way y grows, so up-and-right is 7."""
    o = (0.0, 0.0)
    assert octant(o, (1.0, 0.0)) == 0
    assert octant(o, (1.0, 1.0)) == 1
    assert octant(o, (0.0, 1.0)) == 2
    assert octant(o, (-1.0, 1.0)) == 3
    assert octant(o, (-1.0, 0.0)) == 4
    assert octant(o, (-1.0, -1.0)) == 5
    assert octant(o, (0.0, -1.0)) == 6
    assert octant(o, (1.0, -1.0)) == 7, "up-right is octant 7, the one turn_ok has to wrap"
    with pytest.raises(ValueError, match="zero-length"):
        octant(o, o)


DIRS = {0: (1, 0), 1: (1, 1), 2: (0, 1), 3: (-1, 1), 4: (-1, 0), 5: (-1, -1), 6: (0, -1), 7: (1, -1)}


def test_turn_ok_wraps():
    """A.5: `min(d, 8 - d) <= 1`, pinned on all 64 octant pairs. A leg going up-right (7) followed
    by one going east (0) is a legal 45-degree turn and `abs(7 - 0) = 7` would reject it."""
    for i, (ix, iy) in sorted(DIRS.items()):
        for j, (jx, jy) in sorted(DIRS.items()):
            mid = (10.0, 10.0)
            pts = ((mid[0] - ix, mid[1] - iy), mid, (mid[0] + jx, mid[1] + jy))
            assert octant(pts[0], pts[1]) == i and octant(pts[1], pts[2]) == j, pts
            d = abs(i - j)
            want = min(d, 8 - d) <= 1
            assert turn_ok(pts) is want, f"octants {i} -> {j}: min({d}, {8 - d}) <= 1 is {want}"
    assert turn_ok(((0.0, 0.0), (1.0, 0.0))) is True, "two points have no interior vertex"
    assert turn_ok(((0.0, 0.0), (1.0, 0.0), (0.0, 0.0))) is False, "a reversal is d = 4"


def test_seg_lengths_path_mm_and_clip_len_in_box():
    pts = ((0.0, 0.0), (3.0, 0.0), (3.0, 4.0))
    assert seg_lengths(pts) == (3.0, 4.0)
    assert path_mm(pts) == 7.0
    assert clip_len_in_box((0.0, 0.0), (10.0, 0.0), (2.0, -1.0, 5.0, 1.0)) == 3.0
    assert clip_len_in_box((0.0, 0.0), (10.0, 0.0), (2.0, 5.0, 5.0, 9.0)) == 0.0, "misses the box"
    assert clip_len_in_box((1.0, 1.0), (2.0, 2.0), (0.0, 0.0, 9.0, 9.0)) == pytest.approx(
        math.sqrt(2.0)
    ), "wholly inside is the whole segment"


def test_a_path_holds_its_points_on_the_grid():
    p = GeomPath(((0.0, 0.0), (1.0, 1.0)), 0.2, "F.Cu")
    assert p.w == 0.2 and p.layer == "F.Cu"
    with pytest.raises(ValueError, match="1 nm grid"):
        GeomPath(((0.0, 0.0), (1.0000001, 1.0)), 0.2, "F.Cu")
    with pytest.raises(ValueError, match="at least 2 points"):
        GeomPath(((0.0, 0.0),), 0.2, "F.Cu")


def test_octile_path_is_octilinear_by_construction_either_way_round():
    """A.5's first sentence: 0/45/90 by construction, not by snapping afterwards."""
    a, b = (0.0, 0.0), (5.0, 2.0)
    assert octile_path(a, b) == ((0.0, 0.0), (2.0, 2.0), (5.0, 2.0)), "diagonal first"
    assert octile_path(a, b, diagonal_first=False) == ((0.0, 0.0), (3.0, 0.0), (5.0, 2.0))
    assert octile_path(a, (4.0, 4.0)) == (a, (4.0, 4.0)), "already diagonal: no corner"
    assert octile_path(a, (4.0, 0.0)) == (a, (4.0, 0.0)), "already straight: no corner"
    assert octile_corner((0.0, 0.0), (5.0, 2.0)) == (2.0, 2.0)


# --- D.3 property tests ---------------------------------------------------------------------------


def test_property_no_false_accept():
    """D.3, first bullet, over CASES seeded pairs: whenever `clears` says yes, the independent
    judge agrees the air is at least the requirement. This is the property the whole design rests
    on — a false accept is copper KiCad will reject after the board is written."""
    checked = 0
    tight = 0
    for a, b, need in _pairs(CASES):
        mine = hull_dist2(a.pts, b.pts)
        brute = _brute_dist2(a.pts, b.pts)
        if math.sqrt(mine) - a.r - b.r < 0.5:
            tight += 1
        assert mine <= brute + 1e-12, f"the module measured further apart than the truth: {a} {b}"
        assert abs(math.sqrt(mine) - math.sqrt(brute)) < 1e-9, f"disagreement on {a} {b}"
        if clears(a, b, need):
            checked += 1
            assert math.sqrt(brute) - a.r - b.r >= need, (
                f"false accept at need={need}: real gap "
                f"{math.sqrt(brute) - a.r - b.r} on {a} {b}"
            )
    assert checked > CASES // 4, f"only {checked} of {CASES} pairs cleared; the corpus is degenerate"
    assert tight > CASES // 2, (
        f"only {tight} of {CASES} pairs are within 0.5 mm of each other: the corpus drifted out of "
        "the band where clears is a decision, and a property test over easy cases proves nothing"
    )


def test_property_no_false_accept_against_dense_sampling():
    """The same claim again with no shared idea at all: sample both boundaries densely and measure
    point to point. Sampling can only ever over-estimate the distance, so this is a cross-check on
    the judge rather than on the module, which is why it runs over a smaller slice."""
    checked = 0
    for a, b, _need in _pairs(200):
        if hull_dist2(a.pts, b.pts) == 0.0:
            continue
        checked += 1
        sa = _boundary(a, 240)
        sb = _boundary(b, 240)
        sampled = min(math.dist(p, r) for p in sa for r in sb)
        exact = math.sqrt(hull_dist2(a.pts, b.pts)) - a.r - b.r  # not gap(): that rounds to 4 dp
        assert sampled >= exact - 1e-9, "sampling found copper closer than the module measured"
    assert checked > 100, f"only {checked} of 200 pairs were separated enough to sample"


def _boundary(s: Shape, n: int):
    """Roughly n points on and just inside the boundary of `hull(s.pts) + disc(s.r)`: each edge
    offset both ways by r, plus a ring of 64 around every vertex. Points that land inside the shape
    do no harm — the nearest copper to another shape is always on the boundary, so they can only
    push the sampled minimum up, and this test only ever catches the minimum being too high."""
    out = []
    pts = s.pts
    edges = _loop(pts) if len(pts) > 1 else ()
    for (x1, y1), (x2, y2) in edges:
        nx, ny = y2 - y1, -(x2 - x1)
        ln = math.hypot(nx, ny) or 1.0
        nx, ny = nx / ln * s.r, ny / ln * s.r
        for i in range(n // max(len(edges), 1)):
            t = i / max(n // max(len(edges), 1) - 1, 1)
            out.append((x1 + (x2 - x1) * t + nx, y1 + (y2 - y1) * t + ny))
            out.append((x1 + (x2 - x1) * t - nx, y1 + (y2 - y1) * t - ny))
    if s.r > 0.0:
        for x, y in pts:
            for i in range(64):
                a = i / 64 * 2 * math.pi
                out.append((x + s.r * math.cos(a), y + s.r * math.sin(a)))
    if not out:
        out = [pts[0]]
    return out


# One entry per row of A.1's table: the Shape, and a predicate for the TRUE copper written straight
# from KiCad's own definition, independently of the constructor under test.
def _true_shapes():
    def seg(p, a, b):
        return math.sqrt(_seg_seg_d2(p, p, a, b))

    yield (
        "track segment",
        track_shape((2.0, 3.0), (6.0, 5.5), 0.25),
        (1.8, 2.8, 6.2, 5.7),
        lambda p: seg(p, (2.0, 3.0), (6.0, 5.5)) <= 0.125,
    )
    yield (
        "via ring",
        via_shape((4.0, 4.0), 0.8),
        (3.5, 3.5, 4.5, 4.5),
        lambda p: math.dist(p, (4.0, 4.0)) <= 0.4,
    )
    yield (
        "round drill",
        hole_shape(4.0, 4.0, 0.4),
        (3.7, 3.7, 4.3, 4.3),
        lambda p: math.dist(p, (4.0, 4.0)) <= 0.2,
    )
    yield (
        "oval drill",
        hole_shape(4.0, 4.0, 0.799998, 1.499997),
        (3.5, 3.1, 4.5, 4.9),
        lambda p: seg(p, (4.0, 3.65), (4.0, 4.35)) <= 0.399999,
    )
    yield (
        "circle pad",
        circle_shape(4.0, 4.0, 1.2),
        (3.3, 3.3, 4.7, 4.7),
        lambda p: math.dist(p, (4.0, 4.0)) <= 0.6,
    )
    yield (
        "oval pad",
        oval_shape(4.0, 4.0, 2.0, 1.0, 30.0),
        (2.9, 2.9, 5.1, 5.1),
        lambda p: seg(p, _rot((4.0, 4.0), (-0.5, 0.0), 30.0), _rot((4.0, 4.0), (0.5, 0.0), 30.0)) <= 0.5,
    )
    yield (
        "rect pad",
        rect_shape(4.0, 4.0, 1.6, 0.9, 30.0),
        (3.0, 3.0, 5.0, 5.0),
        lambda p: _local(p, 30.0, (4.0, 4.0), 0.8, 0.45),
    )
    for rratio in (0.0, 0.243902, 0.25, 0.5):
        yield (
            f"roundrect pad rratio {rratio}",
            roundrect_shape(4.0, 4.0, 1.6, 0.9, rratio, 30.0),
            (3.0, 3.0, 5.0, 5.0),
            _roundrect_true(4.0, 4.0, 1.6, 0.9, rratio, 30.0),
        )
    yield (
        "trapezoid pad",
        poly_shape([(3.0, 3.5), (5.0, 3.5), (4.6, 4.5), (3.4, 4.5)]),
        (2.9, 3.4, 5.1, 4.6),
        _poly_true([(3.0, 3.5), (5.0, 3.5), (4.6, 4.5), (3.4, 4.5)], 0.0),
    )
    yield (
        "chamfered roundrect",  # the chamfered copper is a subset of the unchamfered shape
        roundrect_shape(4.0, 4.0, 1.6, 0.9, 0.25, 30.0),
        (3.0, 3.0, 5.0, 5.0),
        _roundrect_true(4.0, 4.0, 1.6, 0.9, 0.25, 30.0, chamfer=0.3),
    )
    yield (
        "custom pad primitive",  # a concave gr_poly with its stroke: the hull is the superset
        poly_shape([(3.0, 3.0), (5.0, 3.0), (5.0, 5.0), (4.0, 3.6), (3.0, 5.0)], 0.1),
        (2.9, 2.9, 5.1, 5.1),
        _poly_true([(3.0, 3.0), (5.0, 3.0), (5.0, 5.0), (4.0, 3.6), (3.0, 5.0)], 0.05),
    )
    yield (
        "keepout / lane strip",
        rect_shape(4.0, 4.0, 3.0, 1.29),
        (2.4, 3.3, 5.6, 4.7),
        lambda p: _local(p, 0.0, (4.0, 4.0), 1.5, 0.645),
    )


def _rot(c, local, deg):
    a = math.radians(-deg)  # KiCad's convention, as copper._rotate applies it
    cs, sn = math.cos(a), math.sin(a)
    return (c[0] + local[0] * cs - local[1] * sn, c[1] + local[0] * sn + local[1] * cs)


def _unrot(p, c, deg):
    a = math.radians(deg)
    cs, sn = math.cos(a), math.sin(a)
    dx, dy = p[0] - c[0], p[1] - c[1]
    return (dx * cs - dy * sn, dx * sn + dy * cs)


def _local(p, deg, c, hx, hy):
    u, v = _unrot(p, c, deg)
    return abs(u) <= hx and abs(v) <= hy


def _roundrect_true(cx, cy, w, h, rratio, deg, chamfer=0.0):
    rad = rratio * min(w, h)
    hx, hy = w / 2.0 - rad, h / 2.0 - rad

    def inside(p):
        u, v = _unrot(p, (cx, cy), deg)
        cu = min(max(u, -hx), hx)
        cv = min(max(v, -hy), hy)
        if math.hypot(u - cu, v - cv) > rad + 1e-12:
            return False
        if chamfer and (u - (w / 2.0 - chamfer)) + (v - (h / 2.0 - chamfer)) > 0:
            return False  # KiCad cuts the corner; the Shape keeps it, which is the superset
        return True

    return inside


def _poly_true(pts, half_stroke):
    def inside(p):
        if _ray_inside(p, pts):
            return True
        return any(math.sqrt(_seg_seg_d2(p, p, e[0], e[1])) <= half_stroke for e in _loop(pts))

    return inside


def test_property_superset_for_every_shape_kind():
    """D.3, second bullet: for every row of A.1's table, 500 points drawn from the TRUE copper —
    computed independently from KiCad's own definition of that item — all lie inside the `Shape`.
    This is the only rule the core has, and the one a pattern's soundness is inherited from."""
    rng = random.Random(SEED + 2)
    for name, shape, box, true in _true_shapes():
        found = 0
        tries = 0
        while found < 500:
            tries += 1
            assert tries < 200_000, f"{name}: could not sample its true copper"
            p = (rng.uniform(box[0], box[2]), rng.uniform(box[1], box[3]))
            if not true(p):
                continue
            found += 1
            assert _inside(shape, p), f"{name}: {p} is copper KiCad draws and the Shape misses it"


def test_property_superset_on_random_polygons():
    """The same rule where it is easiest to break: a random outline, concave half the time."""
    rng = random.Random(SEED + 3)
    for _ in range(300):
        n = rng.randint(3, 9)
        cx, cy = rng.uniform(0.0, 30.0), rng.uniform(0.0, 30.0)
        pts = [(cx + rng.uniform(-2, 2), cy + rng.uniform(-2, 2)) for _ in range(n)]
        stroke = rng.choice((0.0, 0.1))
        s = poly_shape(pts, stroke)
        true = _poly_true(pts, stroke / 2.0)
        for _ in range(40):
            p = (cx + rng.uniform(-2.2, 2.2), cy + rng.uniform(-2.2, 2.2))
            if true(p):
                assert _inside(s, p), f"{p} is copper and {s} misses it"


def test_property_a_reduced_hull_still_holds_every_point_it_came_from():
    """The 16-point ceiling of A.1, over 400 seeded outlines of 17 to 60 points: the reduction is
    a superset every time, and it stays tight rather than collapsing to the bounding box. The
    superset rule is the only rule the core has, and this is the one code path that can break it
    by a nanometre rather than by a design decision."""
    rng = random.Random(SEED + 7)
    boxed = 0
    for _ in range(400):
        n = rng.randint(17, 60)
        cx, cy = rng.uniform(0.0, 50.0), rng.uniform(0.0, 50.0)
        rx, ry = rng.uniform(0.2, 4.0), rng.uniform(0.2, 4.0)
        turn = rng.uniform(0.0, 2.0 * math.pi)
        pts = [
            (cx + rx * math.cos(turn + i / n * 2 * math.pi), cy + ry * math.sin(turn + i / n * 2 * math.pi))
            for i in range(n)
        ]
        cut = hull(pts)
        assert len(cut) <= MAX_PTS, f"{len(cut)} points survived the {MAX_PTS} ceiling"
        for q_ in pts:
            assert _inside(Shape(cut), q_), f"{q_} was in the outline and is not in its reduction"
        if len(cut) == 4 and n > 20:
            boxed += 1
    assert boxed == 0, f"{boxed} of 400 outlines fell back to their bounding box"


def test_property_quantisation_is_idempotent_and_survives_the_text_kicad_writes():
    """D.3, and A.2 property 1: `q` is a fixed point, and `f"{v:.6f}"` — the format the board file
    is written with — parses back to the same double. The geometry pcbc checks is the geometry
    KiCad parses, bit for bit."""
    rng = random.Random(SEED + 4)
    vals = [rng.uniform(-200.0, 200.0) for _ in range(4000)]
    vals += [i * 5e-7 for i in range(-200, 201)]  # exactly on and beside the half-nanometre ties
    vals += [-0.0, 0.0, 1e-9, -1e-9, 59.9999995]
    for v in vals:
        one = q(v)
        assert q(one) == one, f"q is not idempotent at {v!r}"
        assert math.copysign(1.0, one) > 0 or one != 0.0, f"q left a negative zero at {v!r}"
        text = f"{one:.6f}"
        assert float(text) == one, f"{one!r} does not survive {text}"
        assert f"{q(float(text)):.6f}" == text, "emit -> parse -> emit is a fixed point"
        assert abs(one - v) <= 0.5 * NM + 1e-15, f"q moved {v!r} more than half a nanometre"
    for _ in range(500):
        p = (rng.uniform(-50.0, 50.0), rng.uniform(-50.0, 50.0))
        assert qp(qp(p)) == qp(p)


def test_property_a_diagonal_through_a_rounded_midpoint_stays_exactly_diagonal():
    """A.5's second construction rule, and D.3: compute the midpoint in nanometres, round it once,
    then *re-derive* the far endpoint from the rounded corner. Round the endpoint instead and the
    45 becomes 44.98 — which is exactly how every fanout stub came to be tilted (S1)."""
    rng = random.Random(SEED + 5)
    for _ in range(CASES):
        a = qp((rng.uniform(0.0, 60.0), rng.uniform(0.0, 45.0)))
        b = qp((rng.uniform(0.0, 60.0), rng.uniform(0.0, 45.0)))
        mid = qp(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))  # rounded once, and it may move
        legs = octile_path(a, mid) + octile_path(mid, b)[1:]
        assert is_octilinear(legs), f"{a} -> {mid} -> {b} left 0/45/90"
        assert turn_ok(octile_path(a, mid)) and turn_ok(octile_path(mid, b)), "45 at the corner"
        for (x1, y1), (x2, y2) in zip(legs, legs[1:]):
            dx, dy = round((x2 - x1) * 1e6), round((y2 - y1) * 1e6)
            if dx and dy:
                assert abs(dx) == abs(dy), f"a diagonal leg off by {abs(abs(dx) - abs(dy))} nm"


def test_property_the_module_is_deterministic():
    """The same shapes give the same answers, and the answers do not depend on the order the
    points were handed over: `hull` sorts, and every loop here is over a tuple."""
    rng = random.Random(SEED + 6)
    for _ in range(300):
        pts = [(rng.uniform(0, 5), rng.uniform(0, 5)) for _ in range(rng.randint(3, 8))]
        first = poly_shape(pts, 0.1)
        rng.shuffle(pts)
        assert poly_shape(pts, 0.1) == first, "hull is order-independent"
    for a, b, need in _pairs(200):
        assert hull_dist2(a.pts, b.pts) == hull_dist2(a.pts, b.pts)
        assert clears(a, b, need) == clears(a, b, need)
        assert gap(a, b) == gap(a, b)


# --- the golden vectors R3 replays ----------------------------------------------------------------


def _vector_doc():
    """The fixture, built here so the file is a pure function of the seed and this code."""
    cases = []
    for a, b, need in _pairs(CASES):
        d2 = hull_dist2(a.pts, b.pts)
        cases.append(
            {
                "a": {"pts": [list(p) for p in a.pts], "r": a.r},
                "b": {"pts": [list(p) for p in b.pts], "r": b.r},
                "need": need,
                "dist2": d2,
                "gap": gap(a, b),
                "clears": clears(a, b, need),
            }
        )
    return {
        "note": (
            "Golden vectors for pcbc's exact-geometry core (docs/r2-design.md A.2). Generated by "
            "tests/test_route_geom.py from random.Random(20260920); R3's Rust port replays this "
            "file unchanged. Distances are squared millimetres; gap is 4 dp; clears carries "
            "EPS_MM = 1e-4 on top of need. Regenerate with PCBC_WRITE_GEOM_VECTORS=1 pytest "
            "tests/test_route_geom.py -k vectors."
        ),
        "seed": SEED,
        "eps_mm": EPS_MM,
        "cases": cases,
    }


def _vector_text(doc) -> str:
    """One case per line, compact: the fixture is 2000 rows and a diff has to be readable."""
    head = {k: v for k, v in doc.items() if k != "cases"}
    body = ",\n  ".join(json.dumps(c, separators=(",", ":")) for c in doc["cases"])
    return json.dumps(head, indent=1, sort_keys=True)[:-2] + ',\n "cases": [\n  ' + body + "\n ]\n}\n"


def test_the_golden_vectors_replay_bit_for_bit():
    """D.3, last bullet. The cheapest thing in the design and what makes R3 a port rather than a
    rewrite: 2000 shape pairs with their exact answers, replayed here on every run."""
    doc = _vector_doc()
    if os.environ.get("PCBC_WRITE_GEOM_VECTORS"):
        VECTORS.write_text(_vector_text(doc))
    stored = json.loads(VECTORS.read_text())
    assert stored["seed"] == SEED and stored["eps_mm"] == EPS_MM
    assert len(stored["cases"]) == CASES, f"D.3 asks for {CASES} vectors"
    for i, case in enumerate(stored["cases"]):
        a = Shape(tuple(tuple(p) for p in case["a"]["pts"]), case["a"]["r"])
        b = Shape(tuple(tuple(p) for p in case["b"]["pts"]), case["b"]["r"])
        assert hull_dist2(a.pts, b.pts) == case["dist2"], f"vector {i}: distance moved"
        assert gap(a, b) == case["gap"], f"vector {i}: gap moved"
        assert clears(a, b, case["need"]) == case["clears"], f"vector {i}: the verdict moved"
    assert _vector_text(doc) == VECTORS.read_text(), (
        "the fixture is out of date: PCBC_WRITE_GEOM_VECTORS=1 regenerates it"
    )


def test_the_constants_are_the_ones_the_rest_of_pcbc_uses():
    """`MICRO_MM` is re-exported so there is one of it (A.1), not a second copy that can drift."""
    from pcbc.copper_bar import MICRO_MM as BAR_MICRO_MM

    assert MICRO_MM == BAR_MICRO_MM == 0.2, "copper_bar.MICRO_MM and KiCad's track_segment_length"
    assert NM == 1e-6 and EPS_MM == 1e-4 and MAX_PTS == 16
