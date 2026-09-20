"""A pad's true copper, read from the board file the way KiCad draws it.

`pcb_place.parse_foot` reads a pad as one `(size w h)` box, which is all placement needs and is
not what a clearance decision needs: an oval pad is a stadium, a roundrect is its inset corners
offset by the corner radius, a `(drill oval 0.8 1.5)` is a capsule 0.35 mm longer at each end than
the scalar read of it, and a custom pad's `(size ...)` is the anchor KiCad snaps to rather than the
copper it draws. This module is the second, richer view of the same block: `PadGeom` is what
`route_scene` turns into obstacles and what every pattern is judged against. `parse_foot` and
`Foot` are untouched and stay the placement view (`docs/r2-design.md` E.1, E.3).

Every shape here is exact or a strict superset, per row of A.1's table, and the two supersets are
named at the call site: a chamfered roundrect is read as the roundrect (a chamfer only removes
copper from a corner), and a concave custom outline is read as its hull. The census this covers,
counted across the five placed boards: 363 smd (165 rect with the 5 through-hole ones, 156
roundrect, 32 oval, 36 circle, 8 custom), 30 thru_hole and 4 np_thru_hole, 397 pads in all.

Pure and deterministic: a block in, a tuple of frozen dataclasses out, nothing read from disk and
no dict iterated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .geom import _iter_tagged
from .route_geom import (
    Pt,
    Shape,
    aabb,
    circle_shape,
    hole_shape,
    oval_shape,
    poly_shape,
    qp,
    rect_shape,
    roundrect_shape,
)

_HEAD = re.compile(r'\(pad\s+"([^"]*)"\s+(\S+)\s+(\S+)')
_AT = re.compile(r"\(at\s+([-0-9.e+]+)\s+([-0-9.e+]+)(?:\s+([-0-9.e+]+))?\)")
_SIZE = re.compile(r"\(size\s+([-0-9.e+]+)\s+([-0-9.e+]+)\)")
_DRILL = re.compile(r"\(drill\s+(oval\s+)?([-0-9.e+]+)(?:\s+([-0-9.e+]+))?")
_LAYERS = re.compile(r"\(layers([^)]*)\)")
_RRATIO = re.compile(r"\(roundrect_rratio\s+([-0-9.e+]+)\)")
_MASK_MARGIN = re.compile(r"\(solder_mask_margin\s+([-0-9.e+]+)\)")
_NET = re.compile(r'\(net\s+(?:\d+\s+)?"([^"]*)"\)')
_PRIM_PTS = re.compile(r"\(xy\s+([-0-9.e+]+)\s+([-0-9.e+]+)\)")
_PRIM_WIDTH = re.compile(r"\(width\s+([-0-9.e+]+)\)")

DEFAULT_LAYERS = ("F.Cu", "B.Cu")
"""What `*.Cu` expands to when the caller does not say; `build_scene` always says."""

_MIRROR = {"F.Cu": "B.Cu", "B.Cu": "F.Cu", "F.Mask": "B.Mask", "B.Mask": "F.Mask", "F.Paste": "B.Paste", "B.Paste": "F.Paste"}


@dataclass(frozen=True)
class PadGeom:
    """One pad's copper, hole and layers, in world coordinates.

    `copper` is one `Shape` per primitive — a custom pad draws several and each is its own obstacle,
    which is tighter than hulling them together and is the reason the field is a tuple. It is empty
    for an NPTH, which has a hole and no copper at all.
    """

    ref: str
    num: str
    net: str
    kind: str  # "smd" | "thru_hole" | "np_thru_hole" | "connect"
    shape: str  # KiCad's own token: "rect" | "roundrect" | "oval" | "circle" | "custom" | "trapezoid"
    at: Pt  # the pad centre in world mm, quantised
    rot: float  # the pad's absolute rotation; a board file's pad angle already carries the footprint's
    copper: tuple[Shape, ...]
    hole: Shape | None
    cu_layers: frozenset[str]
    mask_layers: frozenset[str]
    mask_margin: float

    @property
    def id(self) -> str:
        """`U1.5` — what a move line calls this pad; a numberless pad is named by its position."""
        return f"{self.ref}.{self.num}" if self.num else f"{self.ref} pad at {self.at[0]:g},{self.at[1]:g}"

    def box(self) -> tuple[float, float, float, float]:
        """The pad's world AABB over every primitive and its hole: for indexing, never a decision."""
        boxes = [aabb(s) for s in self.copper] + ([aabb(self.hole)] if self.hole else [])
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )


def custom_size(pad: str) -> tuple[float | None, float | None]:
    """The (width, height) of a custom pad's real copper, as `pcb_place` reads it — re-exported so
    there is one reader of a custom pad's primitives and the placement view and the routing view
    cannot drift about how big the USB-C shield pads are."""
    from .pcb_place import _custom_size

    return _custom_size(pad)


def _f(s: str | None, default: float = 0.0) -> float:
    return default if s is None else float(s)


def _rot_pt(x: float, y: float, deg: float) -> Pt:
    """`copper._rotate` without the import cycle: KiCad's angle, negated for the y-down frame."""
    from .route_geom import _rot_cs

    c, s = _rot_cs(deg)
    return (x * c - y * s, x * s + y * c)


def _expand_layers(tokens: str, layers: tuple[str, ...], side: str) -> tuple[frozenset[str], frozenset[str]]:
    """`"*.Cu" "*.Mask"`, `"F.Cu" "F.Mask" "F.Paste"`, `"F&B.Cu"` -> the copper and mask sets."""
    cu: set[str] = set()
    mask: set[str] = set()
    for tok in re.findall(r'"([^"]+)"', tokens):
        if side == "B":
            tok = _MIRROR.get(tok, tok)
        if tok == "*.Cu":
            cu.update(layers)
        elif tok == "F&B.Cu":
            cu.update(lay for lay in ("F.Cu", "B.Cu") if lay in layers)
        elif tok.endswith(".Cu"):
            cu.add(tok)
        elif tok == "*.Mask":
            mask.update(("F.Mask", "B.Mask"))
        elif tok.endswith(".Mask"):
            mask.add(tok)
    return (frozenset(cu), frozenset(mask))


def _copper_shapes(pad: str, shape: str, cx: float, cy: float, w: float, h: float, rot: float) -> tuple[Shape, ...]:
    """A.1's table, one branch per row. Every branch is exact except the two named supersets."""
    if shape == "custom":
        prims = _primitive_shapes(pad, cx, cy, rot)
        if prims:
            # The anchor is copper too (KiCad draws it), and it is a rounding error next to the
            # primitives on every board here — but including it can only grow the obstacle, which
            # is the safe direction, and leaving it out would be a subset the day a pad uses it.
            return prims + (rect_shape(cx, cy, w, h, rot),)
        return (rect_shape(cx, cy, w, h, rot),)
    if shape == "circle":
        return (circle_shape(cx, cy, min(w, h)),)
    if shape == "oval":
        return (oval_shape(cx, cy, w, h, rot),)
    if shape == "roundrect":
        # A chamfered roundrect is read as the roundrect: a chamfer only ever removes copper from a
        # corner, so ignoring it makes the obstacle a superset and never a subset (A.1).
        return (roundrect_shape(cx, cy, w, h, _f(_RRATIO.search(pad) and _RRATIO.search(pad).group(1)), rot),)
    if shape == "trapezoid":
        # The four true corners: `(rect_delta dy dx)` shears the box, and the hull of the sheared
        # corners is exact because a trapezoid is convex.
        m = re.search(r"\(rect_delta\s+([-0-9.e+]+)\s+([-0-9.e+]+)\)", pad)
        dy, dx = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
        hx, hy = w / 2.0, h / 2.0
        local = ((-hx - dy / 2.0, -hy + dx / 2.0), (hx + dy / 2.0, -hy - dx / 2.0), (hx - dy / 2.0, hy + dx / 2.0), (-hx + dy / 2.0, hy - dx / 2.0))
        return (poly_shape(tuple((cx + px, cy + py) for px, py in (_rot_pt(lx, ly, rot) for lx, ly in local))),)
    return (rect_shape(cx, cy, w, h, rot),)


def _primitive_shapes(pad: str, cx: float, cy: float, rot: float) -> tuple[Shape, ...]:
    """One `Shape` per `(gr_poly)` / `(gr_rect)` / `(gr_line)` primitive of a custom pad.

    The USB-C shield pad declares `(size 0.005 0.005)` and draws an eight-point concave `gr_poly`
    spanning 0.599974 x 1.299997 mm with a `(width 0.1)` stroke: 0.700 x 1.400 mm of real copper on
    four pads of c3_usb and node, which every check was blind to before S1b. The hull of a concave
    outline is a superset, which is the side of A.1's rule this has to be on.
    """
    i = pad.find("(primitives")
    if i < 0:
        return ()
    out: list[Shape] = []
    for tag in ("gr_poly", "gr_rect", "gr_line", "gr_circle"):
        for prim in _iter_tagged(pad[i:], tag):
            pts = [(float(a), float(b)) for a, b in _PRIM_PTS.findall(prim)]
            if not pts:
                continue
            wm = _PRIM_WIDTH.search(prim)
            stroke = float(wm.group(1)) if wm else 0.0
            world = tuple((cx + px, cy + py) for px, py in (_rot_pt(x, y, rot) for x, y in pts))
            if tag == "gr_rect" and len(world) == 2:
                (x0, y0), (x1, y1) = world  # the two opposite corners, already turned
                a, b = _rot_pt(pts[0][0], pts[1][1], rot), _rot_pt(pts[1][0], pts[0][1], rot)
                world = ((x0, y0), (cx + a[0], cy + a[1]), (x1, y1), (cx + b[0], cy + b[1]))
            out.append(poly_shape(world, stroke))
    return tuple(out)


def _hole(pad: str, cx: float, cy: float, rot: float) -> Shape | None:
    m = _DRILL.search(pad)
    if not m:
        return None
    if m.group(1):  # (drill oval w h): a capsule in the pad's frame, not a scalar
        return hole_shape(cx, cy, float(m.group(2)), float(m.group(3)), rot)
    return hole_shape(cx, cy, float(m.group(2)))


def pad_geoms(
    block: str,
    at: tuple[float, float, float],
    side: str = "F",
    *,
    ref: str = "",
    nets: dict[str, str] | None = None,
    layers: tuple[str, ...] = DEFAULT_LAYERS,
) -> tuple[PadGeom, ...]:
    """Every pad of one footprint block, in world coordinates, in file order.

    `at` is the footprint's own `(at x y rot)`. A pad's `(at x y rot)` in a board file carries the
    footprint's rotation **in its angle but not in its position**, so the position is turned by the
    footprint's rotation and the angle is used as it stands — the convention `parse_foot` documents
    and the one KiCad writes.

    `nets` fills in a pad the block leaves unbound, by pad number: a freshly placed board may not
    carry its bindings yet. The block's own `(net ...)` wins where it has one, because the file is
    what KiCad judges — reading the design over the file makes an audit of an existing board
    disagree with the arbiter about which net a pad is even on.

    `layers` is the board's copper layer list, which is what `*.Cu` expands to; it is a keyword
    because A.1's table does not say what a 4-layer `*.Cu` pad spans and the scene does.
    """
    fx, fy, frot = at
    out: list[PadGeom] = []
    for pad in _iter_tagged(block, "pad"):
        head = _HEAD.match(pad)
        pat = _AT.search(pad)
        size = _SIZE.search(pad)
        if not head or not pat or not size:
            continue
        num, kind, shape = head.group(1), head.group(2), head.group(3)
        lx, ly = float(pat.group(1)), float(pat.group(2))
        if side == "B":
            lx = -lx
        dx, dy = _rot_pt(lx, ly, frot)
        cx, cy = qp((fx + dx, fy + dy))
        rot = _f(pat.group(3))
        w, h = float(size.group(1)), float(size.group(2))
        netm = _NET.search(pad)
        # The board file is the authority on what its own copper is bound to, because that is what
        # KiCad judges; `nets` only fills in a pad the file left unbound, which is what a freshly
        # placed board looks like before `apply` writes the bindings.
        net = (netm.group(1) if netm else "") or (nets or {}).get(num, "")
        cu, mask = _expand_layers(_LAYERS.search(pad).group(1) if _LAYERS.search(pad) else "", layers, side)
        copper = () if kind == "np_thru_hole" else _copper_shapes(pad, shape, cx, cy, w, h, rot)
        mm = _MASK_MARGIN.search(pad)
        out.append(
            PadGeom(
                ref=ref,
                num=num,
                net=net,
                kind=kind,
                shape=shape,
                at=(cx, cy),
                rot=rot,
                copper=copper,
                hole=_hole(pad, cx, cy, rot),
                cu_layers=cu,
                mask_layers=mask,
                mask_margin=float(mm.group(1)) if mm else 0.0,
            )
        )
    return tuple(out)
