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
    legs_ok,
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


def test_q_is_a_fixed_point_of_the_text_format_and_not_the_format_itself():
    """A.2 property 1, corrected. `q` is KiCad's 1 nm grid and `f"{q(v):.6f}"` round-trips exactly;
    `q` is *not* `float(f"{v:.6f}")`, because it rounds the already-rounded product `v * 1e6` while
    the format rounds the exact value of the double. They differ by a nanometre on 38 % of the
    midpoints a pattern computes, which is why the house rule is `f"{q(v):.6f}"` and never
    `f"{v:.6f}"` of a raw computed coordinate."""
    a, b = 21.732048, 21.734519  # both on the grid, an odd number of nanometres apart
    mid = (a + b) / 2.0
    assert q(mid) == 21.733284, "q rounds the product: 21733283.4999... nm goes to 21733284"
    assert f"{mid:.6f}" == "21.733283", (
        "and the text format rounds the double itself, one nanometre the other way: writing a raw "
        "computed coordinate is the 'we rounded on the way out' bug the module claims cannot happen"
    )
    assert float(f"{q(mid):.6f}") == q(mid), "what the house rule guarantees, and all it guarantees"
    rng = random.Random(SEED + 11)
    differ = 0
    for _ in range(2000):
        x = q(rng.uniform(0.0, 60.0))
        y = q(x + (2 * rng.randrange(1, 2000) + 1) * NM)  # an odd number of nanometres away
        m = (x + y) / 2.0  # exactly a half nanometre off the grid
        assert float(f"{q(m):.6f}") == q(m), f"{m!r} does not survive its own text"
        differ += q(m) != float(f"{m:.6f}")
    assert differ > 500, (
        f"only {differ} of 2000 half-nanometre midpoints disagreed with a raw f-string; the rule "
        "this test exists to pin has stopped mattering, which means q changed"
    )


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
        # ...and the answers do not depend on which argument is which. Asserting a call against
        # itself is a tautology; this is the assertion that has content, and it is the one that
        # failed: `clears` summed the radii in call order and flipped 286 of 3240 realistic
        # (clearance, radius, radius) triples, `gap` printed two different 4-dp numbers for 422 of
        # 400 000 pairs (docs/r2-measurements.md, S3).
        assert hull_dist2(a.pts, b.pts) == hull_dist2(b.pts, a.pts), "distance is symmetric"
        assert clears(a, b, need) == clears(b, a, need), "so is the verdict"
        assert gap(a, b) == gap(b, a), "and so is the number the report prints"


# --- what the attacks on the core found (the review of S2) -----------------------------------------


def test_a_star_wound_point_list_is_not_a_hull_even_though_every_triple_turns_left():
    """A.1's invariant is "these points ARE their own hull, in hull order", not "every consecutive
    triple turns left". A pentagram winds through 720 degrees, so every triple turns left; the old
    check accepted it, and `_in_hull` — which every overlap decision rests on — was then wrong by
    0.678 mm on the very first probe."""
    circle = tuple(
        (5.0 + 2.0 * math.cos(math.radians(-90 + 72 * i)), 5.0 + 2.0 * math.sin(math.radians(-90 + 72 * i)))
        for i in range(5)
    )
    star = tuple(qp(circle[(2 * i) % 5]) for i in range(5))
    for i in range(5):  # the necessary-but-not-sufficient test the old validator ran, still true
        o, a, b = star[i], star[(i + 1) % 5], star[(i + 2) % 5]
        cross = (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
        assert cross > 0, "every consecutive triple of a pentagram turns the same way"
    with pytest.raises(ValueError, match="strictly convex CCW hull"):
        Shape(star, 0.0)
    honest = Shape(hull(star), 0.0)
    probe = via_shape((5.0, 3.398146), 0.5)
    assert gap(honest, probe) == -0.25, (
        "the truth for those five points, hulled: the via sits a quarter of a millimetre inside "
        "the copper. Read star-wound, the module answered +0.427893 and cleared it at every "
        "clearance the five boards compile (docs/r2-design.md A.3 tops out at 0.25)"
    )
    assert not clears(honest, probe, 0.0)
    mirrored = tuple((-x, y) for x, y in hull(star))  # the neighbouring case, already caught
    with pytest.raises(ValueError, match="strictly convex CCW hull"):
        Shape(mirrored, 0.0)


def test_the_three_judges_agree_about_a_doubled_point():
    """A.5: `is_octilinear`, `turn_ok` and `Path` judge the same path, so they may not disagree
    about it. A doubled point is a degenerate leg with no direction — `octant` raises on it — and
    chaining two `octile_path` pieces end to end is exactly how one appears at the join."""
    p1 = octile_path((0.0, 0.0), (3.0, 1.0))
    p2 = octile_path((3.0, 1.0), (6.0, 1.0))
    joined = p1 + p2
    assert joined == ((0.0, 0.0), (1.0, 1.0), (3.0, 1.0), (3.0, 1.0), (6.0, 1.0)), "the real join"
    assert is_octilinear(joined) is False, "a zero-length leg is not octilinear: it has no angle"
    assert turn_ok(joined) is False, "and it is not a 45-degree turn either; it used to raise here"
    with pytest.raises(ValueError, match="zero length"):
        GeomPath(joined, 0.2, "F.Cu")
    assert p1 + p2[1:] == ((0.0, 0.0), (1.0, 1.0), (3.0, 1.0), (6.0, 1.0)), "the idiom that is right"
    assert is_octilinear(p1 + p2[1:]) and turn_ok(p1 + p2[1:])
    with pytest.raises(ValueError, match="two distinct points"):
        octile_path((10.0, 10.0), (10.0, 10.0))
    with pytest.raises(ValueError, match="two distinct points"):
        octile_path((10.0, 10.0), (10.0000004, 10.0)), "half a nanometre apart is the same point"


def test_turn_ok_refuses_the_right_angle_an_off_axis_leg_hides():
    """A.5 rule 1. `octant` classifies an off-axis leg by the signs of its deltas alone, so two
    nearly perpendicular legs land in adjacent octants and a 90-degree corner passes the turn test.
    The legs that do it are the tilted escape stubs S1 straightened, measured on the placed boards
    (docs/r2-measurements.md S1), so the precondition is folded into `turn_ok` itself."""
    c3_gnd = ((20.75, 18.6385), (21.8, 18.65), (21.8, 21.0))
    ds2_gpio1 = ((20.22, 15.07), (20.2, 16.35), (18.0, 16.35))
    for pts, deg in ((c3_gnd, 90.6275), (ds2_gpio1, 90.8952)):
        a = math.degrees(math.atan2(pts[0][1] - pts[1][1], pts[0][0] - pts[1][0]))
        b = math.degrees(math.atan2(pts[2][1] - pts[1][1], pts[2][0] - pts[1][0]))
        turn = abs((b - a) % 360.0)
        assert round(min(turn, 360.0 - turn), 4) == deg, "the true interior angle of the S1 stub"
        assert octant(pts[0], pts[1]) != octant(pts[1], pts[2]), "adjacent octants, so d = 1"
        assert is_octilinear(pts) is False, "which is the only thing that catches it"
        assert turn_ok(pts) is False, (
            f"a {deg} degree corner is not a 45-degree turn (docs/r2-design.md A.5 rule 1); "
            "turn_ok alone used to sign it off"
        )
    assert turn_ok(((0.0, 0.0), (1.0, 0.0), (2.0, 1.0))) is True, "a real 45 still passes"


# The 11 legs under MICRO_MM that KiCad drew `track_segment_length` violations on, from 72 octile
# segments over 36 isolated nets judged by pcbc's own rule text (docs/r2-measurements.md, S3).
KICAD_SHORT_LEGS = (0.0262, 0.0886, 0.0928, 0.1082, 0.1176, 0.1209, 0.1315, 0.1540, 0.1590, 0.1827, 0.1832)


def test_legs_ok_is_the_third_judge_and_refuses_exactly_what_kicad_counts():
    """A.5 gains `legs_ok` beside `is_octilinear` and `turn_ok`: `MICRO_MM` was defined and
    documented as KiCad's rule and then used by nothing, while `octile_path` manufactured legs
    under it (2.15 % of random endpoint pairs, 31.20 % of hops under 2 mm). The path stays exact —
    collapsing the short leg would move an endpoint, which A.5 forbids — so this is a refusal a
    pattern reports as a move."""
    from pcbc.dru import rules as dru_rules

    for length in KICAD_SHORT_LEGS:
        assert length < MICRO_MM, f"{length} is one of the 11 KiCad faulted"
        assert legs_ok(((0.0, 0.0), (length, 0.0), (length + 5.0, 0.0))) is False, (
            f"a {length} mm leg is copper pcbc's own pcbc_geometry_segments rule refuses"
        )
    assert legs_ok(((0.0, 0.0), (MICRO_MM, 0.0))) is True, "exactly 0.2 mm is the rule's own (min)"
    assert legs_ok(octile_path((0.0, 0.0), (5.0, 5.0))) is True
    short = octile_path((0.0, 0.0), (5.0, 0.1))
    assert len(short) == 3 and legs_ok(short) is False, "the two-leg form is where they come from"
    assert is_octilinear(short) and turn_ok(short), "and it is exact copper, just too short a leg"
    assert ("pcbc_geometry_segments", f"(constraint track_segment_length (min {MICRO_MM:g}mm))") in [
        (r.name, r.constraint) for r in dru_rules(_a_constraint_set())
    ], "MICRO_MM is the number of pcbc's own rule, not a second copy of it (dru.py E.1)"


def _a_constraint_set():
    """The compiled blinky, for the one test that needs a real `.kicad_dru` rule list."""
    from pcbc.compile import compile_design
    from pcbc.language import load_board

    return compile_design(load_board(Path(__file__).resolve().parent.parent / "examples" / "blinky" / "blinky.py")).constraints


def test_clears_and_gap_do_not_care_which_argument_is_which():
    """The determinism contract this module opens with, pinned on the two cases that broke it: a
    pattern asking `clears(candidate, obstacle)` and a self-check asking `clears(obstacle,
    candidate)` have to agree about the same board."""
    a = circle_shape(0.0, 0.0, 0.10)
    b = circle_shape(0.214, 0.0, 0.15)
    need = 0.0889  # node's 4-layer Default clearance (docs/r2-design.md A.3)
    assert clears(a, b, need) == clears(b, a, need), (
        "0.0889 + 0.05 + 0.075 + EPS_MM against an exact 0.214 mm separation: summing the radii in "
        "call order gave 0.21400000000000002 one way and 0.21399999999999997 the other, and the "
        "two orders disagreed (286 of 3240 realistic triples flipped)"
    )
    big = circle_shape(0.0, 0.0, 1.0)
    small = circle_shape(0.73425, 0.0, 0.15)
    assert gap(big, small) == gap(small, big) == 0.1593, (
        "0.73425 - 0.5 - 0.075 is 0.15925 exactly, which rounds to 0.1593; subtracting the radii "
        "one at a time lost the last bit off the top and printed 0.1592 in one argument order"
    )


def test_gap_never_prints_a_negative_zero_for_shapes_that_touch():
    """`q`'s own rule, applied to the module's one reporting function: 88 010 of 400 000 exactly
    touching pairs underflowed to `-0.0`, and an f-string renders that as a different byte string
    for the same geometry."""
    for ra, rb, sep in ((0.45, 0.1016, 0.5516), (0.4, 0.0889, 0.4889), (0.1, 0.25, 0.35), (0.125, 0.05, 0.175)):
        g = gap(circle_shape(0.0, 0.0, 2 * ra), circle_shape(sep, 0.0, 2 * rb))
        assert g == 0.0 and math.copysign(1.0, g) > 0.0, f"{ra}/{rb} at {sep} printed {g!r}"
        assert f"{g:.4f}" == "0.0000", "which is the byte string a report line is asserted against"


def test_clears_answers_a_requirement_the_shapes_offsets_already_meet():
    """Squaring discards the sign, so a requirement more negative than -(a.r + b.r + EPS_MM) used to
    mean the opposite of what it said. `between()` is a max over non-negative clearances floored at
    `stackup.clearance_min` (docs/r2-design.md A.3), so `need` is never negative today; `clears`
    answers correctly if one ever is, rather than inverting silently in R3's port."""
    a, b = via_shape((0.0, 0.0), 0.5), via_shape((0.2, 0.0), 0.5)
    for need in (-0.4999, -0.5001, -1.0, -5.0):
        assert clears(a, b, need) is True, f"need={need} is met by the shapes' own offsets"
    assert not clears(a, b, 0.0), "and a real requirement on two overlapping vias still refuses"


def test_octile_corner_quantises_its_own_endpoints():
    """A.5 rule 2. The docstring's "already quantised" was a precondition the signature did not
    show: all 50 000 deliberately off-grid pairs came back with a leg that was not 0/45/90 against
    the caller's own points, which is the S1 fanout bug in a second place."""
    a, b = (4e-07, 0.0), (5.0000004, 2.0000004)
    assert octile_corner(a, b) == (2.0, 2.0)
    assert is_octilinear((qp(a), octile_corner(a, b), qp(b))), "exact against the quantised points"
    rng = random.Random(SEED + 12)
    for _ in range(500):
        a = (q(rng.uniform(0.0, 60.0)) + 4e-10, q(rng.uniform(0.0, 45.0)) - 4e-10)
        b = (q(rng.uniform(0.0, 60.0)) - 4e-10, q(rng.uniform(0.0, 45.0)) + 4e-10)
        if qp(a) == qp(b):
            continue
        legs = (qp(a), octile_corner(a, b), qp(b))
        assert is_octilinear(legs), f"{a} -> {b} left 0/45/90 (docs/r2-design.md A.5 rule 2)"
        assert octile_corner(a, b) == octile_corner(qp(a), qp(b)), "qp is idempotent, so this is free"


def test_the_measurement_functions_refuse_a_non_finite_coordinate():
    """`q` raises, `octant` raises — and `clip_len_in_box` returned nan while `path_mm` returned inf.
    A nan loses every comparison it takes part in, so a cost that went non-finite would sort as "not
    worse than anything" instead of failing loudly."""
    for bad in (math.inf, math.nan):
        with pytest.raises(ValueError, match="not finite"):
            clip_len_in_box((0.0, 0.0), (bad, 0.0), (0.0, 0.0, 1.0, 1.0))
        with pytest.raises(ValueError, match="not finite"):
            clip_len_in_box((0.0, 0.0), (1.0, 0.0), (0.0, 0.0, bad, 1.0))
        with pytest.raises(ValueError, match="not finite"):
            path_mm(((0.0, 0.0), (bad, 0.0)))
        with pytest.raises(ValueError, match="not finite"):
            seg_lengths(((0.0, 0.0), (0.0, bad)))


# --- A.1 against the arbiter: the probe ring on real board pads -------------------------------------

PROBE_PADS = (
    ("c3_usb", "J1", "A4B9", "custom gr_poly, concave: the hull is a superset"),
    ("c3_usb", "J1", "1", "oval pad with an oval drill: exact"),
    ("c3_usb", "U1", "49", "chamfered roundrect: the chamfer is ignored, a superset"),
    ("ds2", "J1", "1", "thru_hole rect with a round drill: exact"),
)
PROBE_RING = 8
PROBE_EXACT = 25
"""Of the 32 probes, how many KiCad's `actual` and pcbc's `gap` agree on to 4 dp (measured
2026-09-20, KiCad 10.0.6; `docs/r2-measurements.md` S3). The other seven are the two supersets A.1
declares — the hulled concave shield pad and the ignored chamfer — and the probes that land inside
copper, where KiCad reports a short instead of a clearance and has no `actual` to compare."""


@pytest.mark.kicad
def test_kicad_measures_the_same_air_around_a_real_pad_as_the_shape_model_does():
    """A.1's table, judged by the arbiter on real board pads instead of by this file's own geometry.

    Method: one footprint lifted from a placed board, a ring of short probe tracks around one pad on
    a net of their own, and a single clearance rule far wider than the gap, so KiCad prints its own
    measured `actual` for every probe. The claim is one-sided, because that is what soundness means
    here: **KiCad's air is never less than pcbc's**, so the model is the true copper or a superset
    of it and never a subset. Where the model is exact the two agree to the last digit KiCad prints.
    """
    import json
    import re
    import tempfile

    from pcbc.dru import DruRule, render
    from pcbc.netcheck import kicad_drc
    from pcbc.pads import pad_geoms
    from pcbc.route_geom import track_shape
    from pcbc.sexp import board_footprint_spans, footprint_at, footprint_reference

    root = Path(__file__).resolve().parent.parent
    ds2 = Path.home() / "Documents" / "MaD" / "Hardware" / "DS2Addon" / "pcbc"
    if not ds2.exists():
        pytest.skip("the DS2 Addon is not checked out here; its header is one of the four rows")
    pro = {
        "board": {"design_settings": {"defaults": {}, "rules": {"min_clearance": 0.05, "min_track_width": 0.05}, "rule_severities": {}}},
        "meta": {"filename": "layout.kicad_pcb", "version": 1},
        "net_settings": {"classes": [{"name": "Default", "clearance": 0.05, "track_width": 0.16, "via_diameter": 0.5, "via_drill": 0.3}]},
        "text_variables": {},
    }
    actual = re.compile(r"actual ([0-9.]+) mm")
    agreed = 0
    for board, ref, num, what in PROBE_PADS:
        pcb = (ds2 / "layout" / "ds2_addon" / "placed" / "layout.kicad_pcb") if board == "ds2" else (root / "examples" / board / "layout" / board / "placed" / "layout.kicad_pcb")
        text = pcb.read_text()
        block = next(text[s:e] for s, e in board_footprint_spans(text) if footprint_reference(text[s:e]) == ref)
        geoms = pad_geoms(block, footprint_at(block), ref=ref)
        target = next(g for g in geoms if g.num == num)
        x0, y0, x1, y1 = target.box()
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        reach = max(x1 - x0, y1 - y0) / 2.0 + 0.7
        probes = []
        for i in range(PROBE_RING):
            a = 2.0 * math.pi * i / PROBE_RING
            px, py = cx + reach * math.cos(a), cy + reach * math.sin(a)
            tx, ty = -math.sin(a), math.cos(a)
            probes.append((f"aaaaaaaa-0000-4000-8000-{i:012d}", (round(px - 0.15 * tx, 6), round(py - 0.15 * ty, 6)), (round(px + 0.15 * tx, 6), round(py + 0.15 * ty, 6))))
        segs = "".join(
            f'\t(segment\n\t\t(start {a[0]:.6f} {a[1]:.6f})\n\t\t(end {b[0]:.6f} {b[1]:.6f})\n\t\t(width 0.2)\n'
            f'\t\t(layer "F.Cu")\n\t\t(net 1)\n\t\t(uuid "{u}")\n\t)\n'
            for u, a, b in probes
        )
        nets = sorted({g.net for g in geoms if g.net})
        netlines = '\t(net 1 "PROBE")\n' + "".join(f'\t(net {i + 2} "{n}")\n' for i, n in enumerate(nets))
        doc_text = (
            '(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbc")\n\t(generator_version "0.1")\n'
            "\t(general\n\t\t(thickness 1.6)\n\t)\n\t(paper \"A4\")\n"
            '\t(layers\n\t\t(0 "F.Cu" mixed)\n\t\t(2 "B.Cu" mixed)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
            "\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n\t(net 0 \"\")\n" + netlines + block + "\n" + segs
            + '\t(gr_rect\n\t\t(start -20 -20)\n\t\t(end 80 80)\n\t\t(stroke (width 0.05) (type default))\n\t\t(fill none)\n'
            '\t\t(layer "Edge.Cuts")\n\t\t(uuid "bbbbbbbb-0000-4000-8000-000000000001")\n\t)\n)\n'
        )
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / "layout.kicad_pcb").write_text(doc_text)
            (work / "layout.kicad_pro").write_text(json.dumps(pro, indent=2))
            (work / "layout.kicad_dru").write_text(render([DruRule("probe", "(constraint clearance (min 8mm))", "A.NetName == 'PROBE' || B.NetName == 'PROBE'")]))
            drc = kicad_drc(work / "layout.kicad_pcb", refill=False)
        for u, a, b in probes:
            cand = track_shape(a, b, 0.2)
            mine = round(min(gap(s, cand) for g in geoms for s in g.copper), 4)
            said = [float(actual.search(v["description"]).group(1)) for v in drc.get("violations", []) if v["type"] == "clearance" and any(i["uuid"] == u for i in v["items"])]
            if not said:
                assert mine < 0.0 or not drc, f"{board} {ref}.{num} ({what}): KiCad measured nothing and pcbc says {mine} mm of air"
                continue
            kicad = round(min(said), 4)
            assert kicad >= mine - 1e-4, (
                f"{board} {ref}.{num} ({what}): KiCad measures {kicad} mm and pcbc claims {mine} mm — "
                "pcbc read MORE air than the arbiter, which means the shape is a subset of the copper "
                "and that is the one thing docs/r2-design.md A.1 forbids"
            )
            agreed += kicad == mine
    assert agreed == PROBE_EXACT, (
        f"{agreed} of {PROBE_RING * len(PROBE_PADS)} probes agreed with KiCad to the last digit it "
        f"prints; {PROBE_EXACT} did when this was measured (docs/r2-measurements.md S3), and the "
        "rest are A.1's two declared supersets and the probes that land inside copper"
    )


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
        # The exact ties where Python's round-half-to-even and Rust's `f64::round`
        # (half-away-from-zero) disagree by a nanometre. `q`'s Rust form is
        # `(v * 1e6).round_ties_even() / 1e6`, and a port that reaches for `.round()` fails here
        # instead of quietly emitting different copper.
        "q_ties": [[v, q(v)] for v in (5e-07, 1.5e-06, 2.5e-06, 3.5e-06, 4.5e-06, 5.5e-06, 6.5e-06, 7.5e-06)],
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
    for v, want in stored["q_ties"]:
        assert q(v) == want, f"q moved at the tie {v!r}"
        assert round(v * 1e6) % 2 == 0, (
            f"{v!r} is an exact tie and q takes it to the EVEN nanometre; Rust's f64::round takes "
            "half of these the other way, so the port's q is round_ties_even, never round"
        )
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
