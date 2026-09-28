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
from typing import Sequence

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
_PROP = re.compile(r"\(property\s+(pad_prop_\w+)\)")
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
    prop: frozenset[str] = frozenset()
    """KiCad's own `(property pad_prop_*)` tokens on this pad, verbatim and unabridged.

    One token matters to pcbc today and it is the one that says a pad is a **land for heat rather
    than for a pin**: `pad_prop_heatsink`. Measured across the five boards 2026-09-21, exactly nine
    pads carry it and they are the nine 1.45 x 1.45 mm blocks KiCad writes for the ESP32-C3-MINI's
    exposed pad, all numbered 49, all on GND, on c3_usb and on node alike — 18 pads in all and not
    one anywhere else. That is the entire population `Thermal()` can name on this repo's boards, and
    reading the property is how the refusal for `Thermal("U1.3")` knows that `U1.3` is a signal pin
    and not a land (`patterns.stitch._thermal_refuse`).

    Read rather than inferred from size, because size cannot answer it: buck's `L1.1` is 1.2 x 1.45
    mm — larger in one axis than a block of node's heatsink — and it is an inductor terminal, where a
    via wicks the joint. The property is a **declaration in the footprint**, which is the only place
    the fact lives. A `frozenset` and not a bool because KiCad defines four of these tokens
    (`pad_prop_bga`, `pad_prop_fiducial_glob`, `pad_prop_fiducial_loc`, `pad_prop_testpoint`,
    `pad_prop_heatsink`, `pad_prop_castellated`) and a reader that collapses them to one question
    would have to be widened by the next reader rather than asked a second question."""

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


@dataclass(frozen=True)
class PadSpec:
    """One `(pad ...)` as a file writes it, every field `PadGeom` and `pcb_place.Foot` are made from.

    `x`, `y` are the pad's position in its footprint's frame (a board file and a `.kicad_mod` write
    the same numbers). `angle` is the pad's angle **as the file writes it**: in a `.kicad_mod` the
    library angle, in a board file the library angle plus the footprint's (KiCad's board-file
    convention). `foot_native.posed` turns the first into the second, so one set of functions reads
    both. Parsed once per pad by `pad_spec`; nothing downstream re-reads the pad's text.
    """

    num: str
    kind: str  # "smd" | "thru_hole" | "np_thru_hole" | "connect"
    shape: str  # KiCad's own token
    x: float
    y: float
    angle: float
    w: float
    h: float
    rratio: float = 0.0
    delta: tuple[float, float] = (0.0, 0.0)  # `(rect_delta dy dx)`, a trapezoid's shear
    prims: tuple[tuple[str, tuple[Pt, ...], float], ...] = ()  # a custom pad's (tag, points, stroke)
    custom: tuple[float, float] | None = None  # `pcb_place._custom_size`: the primitives' extent
    drill: tuple[bool, float, float | None] | None = None  # (oval, w, h)
    layers: str = ""  # the raw tokens of `(layers ...)`
    mask_margin: float = 0.0
    props: frozenset[str] = frozenset()
    net: str = ""


def pad_spec(pad: str) -> PadSpec | None:
    """One `(pad ...)` s-expression read into a `PadSpec`, or None when it has no number, position or
    size (the same three `pad_geoms` has always required)."""
    head = _HEAD.match(pad)
    pat = _AT.search(pad)
    size = _SIZE.search(pad)
    if not head or not pat or not size:
        return None
    rr = _RRATIO.search(pad)
    dm = re.search(r"\(rect_delta\s+([-0-9.e+]+)\s+([-0-9.e+]+)\)", pad)
    prims: list[tuple[str, tuple[Pt, ...], float]] = []
    i = pad.find("(primitives")
    if i >= 0:
        for tag in ("gr_poly", "gr_rect", "gr_line", "gr_circle"):
            for prim in _iter_tagged(pad[i:], tag):
                pts = tuple((float(a), float(b)) for a, b in _PRIM_PTS.findall(prim))
                if not pts:
                    continue
                wm = _PRIM_WIDTH.search(prim)
                prims.append((tag, pts, float(wm.group(1)) if wm else 0.0))
    cw, ch = custom_size(pad)
    drill = _DRILL.search(pad)
    lm = _LAYERS.search(pad)
    mm = _MASK_MARGIN.search(pad)
    netm = _NET.search(pad)
    return PadSpec(
        num=head.group(1),
        kind=head.group(2),
        shape=head.group(3),
        x=float(pat.group(1)),
        y=float(pat.group(2)),
        angle=_f(pat.group(3)),
        w=float(size.group(1)),
        h=float(size.group(2)),
        rratio=_f(rr.group(1)) if rr else 0.0,
        delta=(float(dm.group(1)), float(dm.group(2))) if dm else (0.0, 0.0),
        prims=tuple(prims),
        custom=(cw, ch) if cw is not None else None,
        drill=(bool(drill.group(1)), float(drill.group(2)), float(drill.group(3)) if drill.group(3) else None) if drill else None,
        layers=lm.group(1) if lm else "",
        mask_margin=float(mm.group(1)) if mm else 0.0,
        props=frozenset(_PROP.findall(pad)),
        net=netm.group(1) if netm else "",
    )


def pad_specs(block: str) -> tuple[PadSpec, ...]:
    """Every pad of one footprint block (a board file's or a `.kicad_mod`'s), in file order."""
    return tuple(s for s in (pad_spec(p) for p in _iter_tagged(block, "pad")) if s is not None)


def _copper_shapes(sp: PadSpec, cx: float, cy: float, rot: float) -> tuple[Shape, ...]:
    """A.1's table, one branch per row. Every branch is exact except the two named supersets."""
    shape, w, h = sp.shape, sp.w, sp.h
    if shape == "custom":
        prims = _primitive_shapes(sp, cx, cy, rot)
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
        return (roundrect_shape(cx, cy, w, h, sp.rratio, rot),)
    if shape == "trapezoid":
        # The four true corners: `(rect_delta dy dx)` shears the box, and the hull of the sheared
        # corners is exact because a trapezoid is convex.
        dy, dx = sp.delta
        hx, hy = w / 2.0, h / 2.0
        local = ((-hx - dy / 2.0, -hy + dx / 2.0), (hx + dy / 2.0, -hy - dx / 2.0), (hx - dy / 2.0, hy + dx / 2.0), (-hx + dy / 2.0, hy - dx / 2.0))
        return (poly_shape(tuple((cx + px, cy + py) for px, py in (_rot_pt(lx, ly, rot) for lx, ly in local))),)
    return (rect_shape(cx, cy, w, h, rot),)


def _primitive_shapes(sp: PadSpec, cx: float, cy: float, rot: float) -> tuple[Shape, ...]:
    """One `Shape` per `(gr_poly)` / `(gr_rect)` / `(gr_line)` primitive of a custom pad.

    The USB-C shield pad declares `(size 0.005 0.005)` and draws an eight-point concave `gr_poly`
    spanning 0.599974 x 1.299997 mm with a `(width 0.1)` stroke: 0.700 x 1.400 mm of real copper on
    four pads of c3_usb and node, which every check was blind to before S1b. The hull of a concave
    outline is a superset, which is the side of A.1's rule this has to be on.
    """
    out: list[Shape] = []
    for tag, pts, stroke in sp.prims:
        world = tuple((cx + px, cy + py) for px, py in (_rot_pt(x, y, rot) for x, y in pts))
        if tag == "gr_rect" and len(world) == 2:
            (x0, y0), (x1, y1) = world  # the two opposite corners, already turned
            a, b = _rot_pt(pts[0][0], pts[1][1], rot), _rot_pt(pts[1][0], pts[0][1], rot)
            world = ((x0, y0), (cx + a[0], cy + a[1]), (x1, y1), (cx + b[0], cy + b[1]))
        out.append(poly_shape(world, stroke))
    return tuple(out)


def _hole(sp: PadSpec, cx: float, cy: float, rot: float) -> Shape | None:
    if sp.drill is None:
        return None
    oval, a, b = sp.drill
    if oval:  # (drill oval w h): a capsule in the pad's frame, not a scalar
        return hole_shape(cx, cy, a, b, rot)
    return hole_shape(cx, cy, a)


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
    return geoms_of(pad_specs(block), at, side, ref=ref, nets=nets, layers=layers)


def geoms_of(
    specs: Sequence[PadSpec],
    at: tuple[float, float, float],
    side: str = "F",
    *,
    ref: str = "",
    nets: dict[str, str] | None = None,
    layers: tuple[str, ...] = DEFAULT_LAYERS,
) -> tuple[PadGeom, ...]:
    """`pad_geoms` over parsed pads: each `PadSpec.angle` is the pad's absolute angle (a board file's,
    or `foot_native.posed`'s), `at` the footprint's `(x, y, rot)`."""
    fx, fy, frot = at
    out: list[PadGeom] = []
    for sp in specs:
        lx, ly = sp.x, sp.y
        if side == "B":
            lx = -lx
        dx, dy = _rot_pt(lx, ly, frot)
        cx, cy = qp((fx + dx, fy + dy))
        rot = sp.angle
        # The file is the authority on what its own copper is bound to, because that is what KiCad
        # judges; `nets` only fills in a pad the file left unbound (a `.kicad_mod` binds none).
        net = sp.net or (nets or {}).get(sp.num, "")
        cu, mask = _expand_layers(sp.layers, layers, side)
        copper = () if sp.kind == "np_thru_hole" else _copper_shapes(sp, cx, cy, rot)
        out.append(
            PadGeom(
                ref=ref,
                num=sp.num,
                net=net,
                kind=sp.kind,
                shape=sp.shape,
                at=(cx, cy),
                rot=rot,
                copper=copper,
                hole=_hole(sp, cx, cy, rot),
                cu_layers=cu,
                mask_layers=mask,
                mask_margin=sp.mask_margin,
                prop=sp.props,
            )
        )
    return tuple(out)
