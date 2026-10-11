"""Copper placement the way the schematic is placed: the AI says where by relation, the tool picks the spot.

`Place("C1", to="U1.VIN")` puts C1's pad on U1.VIN's net right outside U1's courtyard,
on the side that pad faces, turned so the rest of C1 leans toward wherever its other
pads' nets already are. `Place("J1", edge="bottom")` stands a connector on a board edge,
face out. CSS `Place()` (anchors) is resolved first; relational parts follow in
dependency order, biggest courtyard first; a part with no clear spot stays where the
collision is and is reported as a move. Pure and deterministic.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

from .copper import _rotate
from .css import Rect, rotate_local_bounds
from .geom import _iter_tagged, footprint_box_local
from .layout import board_rect, content_rect, resolve_keepout, resolve_place, resolve_regions
from .model import BoardSpec, Design, PlaceSpec
from .stackup import Stackup, fanout_lane, get_stackup, pass_mm

_PAD_AT = re.compile(r"\(at\s+([0-9.+-]+)\s+([0-9.+-]+)(?:\s+([0-9.+-]+))?\)")
_PAD_SIZE = re.compile(r"\(size\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_PAD_NUM = re.compile(r'\(pad\s+"([^"]*)"')
_PAD_NET = re.compile(r'\(net\s+(?:\d+\s+)?"([^"]*)"\)')
_FOOT_AT = re.compile(r"\n\t\t\(at [0-9.+-]+ [0-9.+-]+(?: ([0-9.+-]+))?\)")

GAP = 0.2  # courtyard to courtyard, mm
EDGE_CLEAR = 0.3  # copper to board edge: the default; a board reads its stackup's `edge_clearance` (`edge_clear`)


def edge_clear(job) -> float:
    """Copper to the board edge: the stackup's `edge_clearance`, the one home of that number (docs/board-outline.md
    B.3: `EDGE_CLEAR` and the stackup were two homes for one number; every stackup has 0.3)."""
    return float(get_stackup(job.stackup).edge_clearance)
DECAP_MM = 2.5  # the first decoupling cap on a pin belongs this close to it
DECAP_NEXT_MM = 5.0  # the bulk cap behind it, this close
EDGE_MM = 1.0  # a connector this far from every edge is not on one
_STEP = 0.25
_REACH = 8.0
_DIRS = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0)}
_EDGE_OUT = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "bottom": (0.0, 1.0), "top": (0.0, -1.0)}
_ROTS = (0.0, 90.0, 180.0, 270.0)
_TRACE = __import__("os").environ.get("PCBC_TRACE_PLACE", "")  # a ref: print every candidate it scored


@dataclass
class Pad:
    num: str
    x: float
    y: float
    w: float
    h: float
    net: str
    through: bool = False  # a drilled pad: copper on every layer, not only its footprint's side


@dataclass
class Foot:
    ref: str
    box: tuple[float, float, float, float]  # local courtyard
    pads: list[Pad]
    at: tuple[float, float] | None = None
    rot: float = 0.0
    keep: tuple[float, float, float, float] | None = None  # local courtyard plus the fanout lanes
    closed: frozenset[str] = frozenset()  # pads nothing can pass between: they escape straight out
    escape: dict[str, str] = field(default_factory=dict)  # closed pad -> the side it escapes to
    lanes: dict[str, float] = field(default_factory=dict)  # side -> its fanout lane, mm
    lane_mm: float = 0.0  # the widest of them, for the report
    layer: str = "F.Cu"  # the footprint's own `(layer ...)`: the side its pads escape on

    def world_box(self, at=None, rot=None) -> tuple[float, float, float, float]:
        at = at or self.at
        rot = self.rot if rot is None else rot
        x0, y0, x1, y1 = rotate_local_bounds(*self.box, rot)
        return (at[0] + x0, at[1] + y0, at[0] + x1, at[1] + y1)

    def world_keep(self, at=None, rot=None) -> tuple[float, float, float, float]:
        at = at or self.at
        rot = self.rot if rot is None else rot
        x0, y0, x1, y1 = rotate_local_bounds(*(self.keep or self.box), rot)
        return (at[0] + x0, at[1] + y0, at[0] + x1, at[1] + y1)

    def lane(self, stack: Stackup, clearance: float) -> None:
        """Find the pads nothing can pass between (a 0.65 mm TSSOP row, a 0.5 mm QFN row) and
        widen the keep box outside each such row by the fanout lane its pitch needs: a via just
        past every pad, neighbours staggered so their holes keep the fab's hole-to-hole. A track
        leaving one of those pads can only go straight out, and this is the room it takes: the
        DS2 Addon's decaps sat 0.48 mm above the row and its AVDD pad had no path on any layer."""
        passing = pass_mm(stack)
        pads = [p for p in self.pads if p.num]
        cx, cy = (self.box[0] + self.box[2]) / 2.0, (self.box[1] + self.box[3]) / 2.0
        # For each pad, the sides (axis, sign) on which a neighbour sits too close to pass, and how far.
        tight: dict[str, dict[tuple[str, float], float]] = {}
        for a in pads:
            for b in pads:
                if b is a or b.num == a.num:
                    continue
                gx = abs(a.x - b.x) - (a.w + b.w) / 2.0
                gy = abs(a.y - b.y) - (a.h + b.h) / 2.0
                if max(gx, gy) >= passing:
                    continue
                axis = "x" if gx > gy else "y"
                sign = math.copysign(1.0, (b.x - a.x) if axis == "x" else (b.y - a.y))
                dist = abs(b.x - a.x) if axis == "x" else abs(b.y - a.y)
                near = tight.setdefault(a.num, {})
                near[(axis, sign)] = min(near.get((axis, sign), dist), dist)

        def outward(a: Pad, axis: str) -> str:
            if axis == "x":  # the row runs along x: the pad escapes up or down
                return "top" if a.y < cy else "bottom"
            return "left" if a.x < cx else "right"

        # A pad with tight neighbours on both sides of a row is walled in; its whole row (the end
        # pads included, they sit in the same lane) escapes straight out.
        laned: set[str] = set()
        for a in pads:
            for axis in ("x", "y"):
                if (axis, -1.0) in tight.get(a.num, {}) and (axis, 1.0) in tight.get(a.num, {}):
                    laned.add(outward(a, axis))
        keep = list(self.box)
        closed: set[str] = set()
        self.escape = {}
        self.lanes = {}
        for a in pads:
            for (axis, _sign), pitch in tight.get(a.num, {}).items():
                side = outward(a, axis)
                if side not in laned:
                    continue
                closed.add(a.num)
                self.escape[a.num] = side
                lane = fanout_lane(stack, clearance, pitch)
                self.lanes[side] = max(self.lanes.get(side, 0.0), lane)
                if side == "top":
                    keep[1] = min(keep[1], a.y - a.h / 2.0 - lane)
                elif side == "bottom":
                    keep[3] = max(keep[3], a.y + a.h / 2.0 + lane)
                elif side == "left":
                    keep[0] = min(keep[0], a.x - a.w / 2.0 - lane)
                else:
                    keep[2] = max(keep[2], a.x + a.w / 2.0 + lane)
        self.closed = frozenset(closed)
        self.lane_mm = round(max(self.lanes.values(), default=0.0), 4)
        self.keep = tuple(round(v, 4) for v in keep) if closed else None

    def pad_world(self, pad: Pad, at=None, rot=None) -> tuple[float, float]:
        at = at or self.at
        rot = self.rot if rot is None else rot
        dx, dy = _rotate(pad.x, pad.y, rot)
        return (at[0] + dx, at[1] + dy)

    def center(self) -> tuple[float, float]:
        b = self.world_box()
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def parse_foot(ref: str, block: str) -> Foot:
    """Pads in the footprint's own frame. In a board file a pad's angle already includes the
    footprint's, so a turned module's pads are un-turned by it before width and height are read
    (the ESP32 module at 90 had its 0.8 mm rows read as 0.4 mm gaps: a closed row that was not)."""
    pads: list[Pad] = []
    placed = _FOOT_AT.search(block)
    frot = float(placed.group(1) or 0) if placed else 0.0
    for pad in _iter_tagged(block, "pad"):
        num = _PAD_NUM.match(pad)
        at = _PAD_AT.search(pad)
        size = _PAD_SIZE.search(pad)
        if not num or not at or not size:
            continue
        net = _PAD_NET.search(pad)
        w, h = float(size.group(1)), float(size.group(2))
        cw, ch = _custom_size(pad)
        if cw is not None:
            # A custom pad's `(size ...)` is its anchor, not its copper: the USB-C's shield pads
            # declare 0.005 x 0.005 while their `(primitives (gr_poly ...))` span 0.6 x 1.3 plus a
            # 0.1 stroke. Every check that reads pads was blind to 0.7 x 1.4 mm of copper on four
            # pads of c3_usb and node.
            w, h = max(w, cw), max(h, ch)
        if round(float(at.group(3) or 0) - frot) % 180 == 90:
            w, h = h, w
        pads.append(Pad(num.group(1), float(at.group(1)), float(at.group(2)), w, h, net.group(1) if net else "", through=bool(re.match(r'\(pad\s+"[^"]*"\s+(np_)?thru_hole', pad))))
    crt = footprint_box_local(block, "courtyard")
    pb = footprint_box_local(block, "pads")
    box = (min(crt[0], pb[0]), min(crt[1], pb[1]), max(crt[2], pb[2]), max(crt[3], pb[3]))
    return Foot(ref, box, pads)


_PRIM_XY = re.compile(r"\(xy\s+([-0-9.]+)\s+([-0-9.]+)\)")
_PRIM_WIDTH = re.compile(r"\(width\s+([0-9.]+)\)")


def _custom_size(pad: str) -> tuple[float | None, float | None]:
    """(width, height) of a custom pad's real copper: the bounding box of its primitives' points
    grown by the stroke, in the pad's own frame. (None, None) for every other pad type."""
    i = pad.find("(primitives")
    if i < 0:
        return (None, None)
    body = pad[i:]
    pts = [(float(a), float(b)) for a, b in _PRIM_XY.findall(body)]
    if not pts:
        return (None, None)
    strokes = [float(w) for w in _PRIM_WIDTH.findall(body)]
    grow = max(strokes) if strokes else 0.0
    xs = [x for x, _y in pts]
    ys = [y for _x, y in pts]
    return (round(max(xs) - min(xs) + grow, 6), round(max(ys) - min(ys) + grow, 6))


def parse_refpin(spec: str) -> tuple[str, str]:
    if "." not in spec:
        raise ValueError(f"{spec!r} is not REF.PIN (a part reference, a dot, a pin name or pad number)")
    ref, pin = spec.split(".", 1)
    return ref.strip(), pin.strip()


def _pins_of(design: Design) -> dict[str, dict[str, str]]:
    """ref → {pad number → net} from the design (the seed binds the same)."""
    out: dict[str, dict[str, str]] = {}
    for inst in design.instances:
        m: dict[str, str] = {}
        for pname, net in inst.pins.items():
            pin = inst.part.pins.get(pname)
            if pin:
                for pad in pin.pads:
                    m[str(pad)] = net
        out[inst.ref] = m
    return out


def _pin_net(design: Design, ref: str, pin: str) -> str | None:
    for inst in design.instances:
        if inst.ref == ref:
            if pin in inst.pins:
                return inst.pins[pin]
            p = inst.part.pins.get(pin)
            return None if p is None else inst.pins.get(p.name)
    return None


def validate(design: Design) -> list[str]:
    """What `pcbc check` says about Place(to=…) before any footprint is read."""
    refs = {inst.ref for inst in design.instances}
    parts = {inst.ref: inst for inst in design.instances}
    fails: list[str] = []
    for p in design.places:
        if p.to:
            at = f"layout.core.py:{p.line}" if p.line else f"Place({p.ref!r})"
            try:
                tref, tpin = parse_refpin(p.to)
            except ValueError as e:
                fails.append(f"{at}: the to= of Place({p.ref!r}) is {p.to!r}: {e}")
                continue
            if tref not in refs:
                fails.append(f"{at}: the to= of Place({p.ref!r}) names {p.to}, and there is no part {tref} on the board")
                continue
            pins = parts[tref].part.pins
            if tpin not in pins and tpin not in parts[tref].pins:
                fails.append(f"{at}: the to= of Place({p.ref!r}) names {p.to}, and {tref} has no pin {tpin} (its pins: {', '.join(pins)})")
                continue
            net = _pin_net(design, tref, tpin)
            if not net:
                fails.append(f"{at}: the to= of Place({p.ref!r}) names {p.to}, which is on no net")
                continue
            if p.ref in parts and net not in parts[p.ref].pins.values():
                fails.append(f"{at}: the to= of Place({p.ref!r}) names {p.to}, on net {net}, and {p.ref} has no pin on net {net}")
            if p.toward and p.toward not in _DIRS:
                fails.append(f"Place({p.ref!r}): toward must be one of {', '.join(_DIRS)}")
        if p.edge and p.edge not in _EDGE_OUT:
            fails.append(f"Place({p.ref!r}): edge must be one of {', '.join(_EDGE_OUT)}")
        if p.side != "F" and (p.to or p.edge):
            fails.append(f"Place({p.ref!r}): side='B' is placed by CSS position only; relation placement (to=, edge=) is front side only")
    from .layout import declared_outline

    if declared_outline(design)[0] is None and getattr(design, "core_loaded", None) is not False:
        # B.5-1: with no Outline() an anchor is written in frame coordinates (docs/board-outline.md B.1).
        from .outline import unframed_places

        fails += unframed_places(design.places)
    return fails


# ---------------------------------------------------------------- engine


def _board_of(job, rect=None) -> BoardSpec:
    """The board CSS resolves against: the job's outline (`job.outline`: declared, or R/O in derived mode), or `rect`;
    the derived frame before placement has set one (docs/board-outline.md PR 2)."""
    from .layout import frame_board

    base = BoardSpec(size_mm=None, layers=job.layers, stackup=job.stackup, pcb=job.pcb, planes=job.planes)
    return frame_board(base, rect if rect is not None else getattr(job, "outline", None))


def lane_rules(job) -> tuple[Stackup, float]:
    """(stackup, the widest net class lane clearance): what a closed pad row's lane is sized from.
    A class reads `lane_clearance_mm` (the kind's number without the voltage row, docs/r1-design.md
    A.3), so a 250 V class never widens every closed row's fanout lane; None reads as `clearance_mm`."""
    stack = get_stackup(job.stackup)
    lanes = [c.lane_clearance_mm if getattr(c, "lane_clearance_mm", None) is not None else c.clearance_mm for c in getattr(job, "classes", [])]
    clearance = max(lanes + [stack.clearance_min])
    return stack, clearance


def _overlap(a, b, gap: float = 0.0) -> bool:
    eps = 1e-6  # a part settled exactly on a lane's edge is not in it
    return a[0] < b[2] + gap - eps and b[0] < a[2] + gap - eps and a[1] < b[3] + gap - eps and b[1] < a[3] + gap - eps


def _inside(box, rect: Rect) -> bool:
    return box[0] >= rect.x0 - 1e-6 and box[1] >= rect.y0 - 1e-6 and box[2] <= rect.x1 + 1e-6 and box[3] <= rect.y1 + 1e-6


def _face(foot: Foot) -> tuple[float, float]:
    """The connector's mating side: the courtyard edge farthest from its pads."""
    x0, y0, x1, y1 = foot.box
    if not foot.pads:
        return (0.0, 1.0)
    cx = sum(p.x for p in foot.pads) / len(foot.pads)
    cy = sum(p.y for p in foot.pads) / len(foot.pads)
    sides = {(-1.0, 0.0): cx - x0, (1.0, 0.0): x1 - cx, (0.0, -1.0): cy - y0, (0.0, 1.0): y1 - cy}
    return max(sides, key=lambda k: (sides[k], k))


def _rot_for_face(face: tuple[float, float], out: tuple[float, float]) -> float:
    for rot in _ROTS:
        fx, fy = _rotate(face[0], face[1], rot)
        if abs(fx - out[0]) < 1e-6 and abs(fy - out[1]) < 1e-6:
            return rot
    return 0.0


def _edge_css(spec: PlaceSpec, foot: Foot, clear: float = EDGE_CLEAR, origin=(0.0, 0.0)) -> PlaceSpec:
    """`edge=` as CSS: flush with that edge (minus overhang), centred along it unless told. `left`/`top` are frame
    coordinates (`layout._frame` takes them back to the board's corner), so the inset from a left or top edge and the
    centring are written from `origin`, the outline's corner: Outline(at=(5, 7)) put an edge="left" part 5 mm off it."""
    out = _EDGE_OUT[spec.edge]
    face = _face(foot)
    rot = spec.rot if spec.rot_set else _rot_for_face(face, out)
    css: dict = {"position": "absolute", "rot": rot, "from_box": "courtyard"}
    along_set = any(getattr(spec, k) is not None for k in ("left", "right", "top", "bottom"))
    # Flush with the edge, unless the part's copper would then sit inside the edge clearance
    # (a pin header's pads run right to its courtyard): then it steps in by the difference.
    x0, y0, x1, y1 = foot.box
    crt_face = x1 if face == (1.0, 0.0) else -x0 if face == (-1.0, 0.0) else y1 if face == (0.0, 1.0) else -y0
    reach = max(
        (p.x * face[0] + p.y * face[1] + (p.w / 2 if face[0] else p.h / 2) for p in foot.pads if p.num),
        default=crt_face - clear,
    )
    inset = round(max(-float(spec.overhang), clear - (crt_face - reach)), 3)
    ox, oy = origin
    css[spec.edge] = round(inset + (ox if spec.edge == "left" else oy if spec.edge == "top" else 0.0), 6)
    if spec.edge in ("top", "bottom"):
        if not along_set:
            css.update(left=ox, right=0, margin_left="auto", margin_right="auto")
    else:
        if not along_set:
            css.update(top=oy, bottom=0, margin_top="auto", margin_bottom="auto")
    return replace(spec, **css)


FID_INSET = 4.0
FID_HALF = 1.25  # Fiducial_1mm_Mask2mm courtyard


def fiducial_spots(rect, taken: list[Foot], want: int = 3) -> list[tuple[str, float, float]]:
    """Three fiducials, corners first then edge middles, wherever no anchor sits, `FID_INSET` inside `rect`
    ((x0, y0, x1, y1), or a declared (w, h) from (0, 0)). The AI never places these."""
    if isinstance(rect, Rect):
        rect = (rect.x0, rect.y0, rect.x1, rect.y1)
    x0, y0, x1, y1 = rect if len(rect) == 4 else (0.0, 0.0, *rect)
    i = FID_INSET
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    candidates = [(x1 - i, y0 + i), (x1 - i, y1 - i), (x0 + i, y1 - i), (x0 + i, y0 + i), (mx, y0 + i), (mx, y1 - i), (x0 + i, my), (x1 - i, my)]
    spots: list[tuple[str, float, float]] = []
    for x, y in candidates:
        box = (x - FID_HALF, y - FID_HALF, x + FID_HALF, y + FID_HALF)
        if any(_overlap(box, f.world_box(), GAP) for f in taken if f.at is not None):
            continue
        spots.append((f"FID{len(spots) + 1}", x, y))
        if len(spots) == want:
            break
    return spots


# ---------------------------------------------------------------- a path's exit corridor (decision 4)


@dataclass(frozen=True)
class Corridor:
    """The room one exit of a declared path's end pad needs, in world mm (decision 4, owner 2026-10-01): the
    path's width (or, at a pad narrower than the path, the pad neck's width for `PAD_NECK_MAX_MM`, then the
    path's) plus twice its clearance, from the pad's edge outward along that exit.

    It runs `reach` mm past the pad's edge: to the point where the path can first turn (the end of the neck, or
    the stub past the part's own other-net neighbours' clearance, whichever is farther) plus half the path's
    width and its clearance — the room the turn itself takes. The owner's text gives no length; this one is
    pcbc's (refuter round, 2026-10-02: running it to the board edge called a pad 6 mm from any copper blocked).
    It ends sooner at a station pad of the same path (the path has arrived), and at the part's own other-net
    copper or the board's content edge, which leave the path no room to leave this way (`dead`)."""

    pad: str  # "U1.2"
    side: str  # "right" | "left" | "down" | "up"
    rects: tuple[tuple[float, float, float, float], ...]  # the neck's rect (when there is one), then the full width's
    source: str  # "Path line 60"
    net: str
    width: float  # the path's width on the pad's layer
    neck: float | None  # the neck's width at the pad, when the pad is narrower than the path
    clearance: float
    stations: frozenset[str]  # the path's own pads: never an intrusion
    layers: frozenset[str] = frozenset({"F.Cu"})  # the copper layers the path leaves this pad on
    reach: float = 0.0  # mm past the pad's edge the corridor needs
    length: float = 0.0  # mm past the pad's edge it runs (shorter than `reach` when `end` stops it)
    end: str = ""  # what stops it short of `reach`
    dead: bool = False  # stopped by the part's own other-net copper or the board edge: no room to leave this way
    closed: str = ""  # no stub leaves this side at all: the part's own pad in the way


_SIDES = (("right", (1.0, 0.0)), ("left", (-1.0, 0.0)), ("down", (0.0, 1.0)), ("up", (0.0, -1.0)))
_ALL_CU = frozenset({"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"})


def _pad_rect(foot: Foot, pad: Pad, at=None, rot=None) -> tuple[float, float, float, float]:
    cx, cy = foot.pad_world(pad, at=at, rot=rot)
    x0, y0, x1, y1 = rotate_local_bounds(-pad.w / 2.0, -pad.h / 2.0, pad.w / 2.0, pad.h / 2.0, foot.rot if rot is None else rot)
    return (cx + x0, cy + y0, cx + x1, cy + y1)


def _pad_layers(foot: Foot, pad: Pad) -> frozenset[str]:
    """The copper layers a pad is on: every one for a drilled pad, its footprint's side for a surface pad."""
    return _ALL_CU if pad.through else frozenset({foot.layer})


def _rect_gap(a, b) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _rects_overlap(a, b) -> bool:
    return a[0] < b[2] - 1e-9 and b[0] < a[2] - 1e-9 and a[1] < b[3] - 1e-9 and b[1] < a[3] - 1e-9


def path_ends(job) -> dict[str, list[tuple[str, object, object]]]:
    """{ref: [(pad number, PathSpec, the Link at that end)]} for every pad a declared `Path()` starts or ends on."""
    cs = getattr(job, "constraints", None)
    out: dict[str, list] = {}
    if cs is None:
        return out
    for c in sorted(cs.constraints, key=lambda c: c.net):
        for p in c.paths:
            links = [lk for lk in p.links if not lk.join]
            if not links or len(p.stations) < 2:
                continue
            for st, lk in ((p.stations[0], links[0]), (p.stations[-1], links[-1])):
                for pid in st:
                    ref, _dot, num = pid.partition(".")
                    if (num, p, lk) not in out.setdefault(ref, []):
                        out[ref].append((num, p, lk))
    return out


def pad_corridors(job, foot: Foot, num: str, path, lk, limit: Rect, feet: dict[str, Foot] | None = None) -> list[Corridor]:
    """The exit corridors of `foot`'s pad `num`, the end of `path` at `lk`, one per side. A side whose stub at the
    neck's width from the pad's centre, out past its other-net neighbours' clearance, meets an other-net pad of
    the footprint (`route_scene.pad_exits`' rule) is `closed`. `feet` (the parts placed so far) supplies the
    path's other station pads, where a corridor ends."""
    from .constraints import PAD_NECK_MAX_MM, clearance_table

    cs = job.constraints
    pad = next((q for q in foot.pads if q.num == num), None)
    if pad is None or foot.at is None:
        return []
    c = cs.by_net(path.net)
    clear = float(c.clearance_mm.value) if c is not None else 0.2
    table = clearance_table(cs)
    others = [q for q in foot.pads if q.num != num and q.net and q.net != path.net]
    worst = max([clear] + [table.between(path.net, q.net)[0] for q in others])
    w = float(lk.width_on(foot.layer))
    layers = ((frozenset(lk.layers) & _ALL_CU) or frozenset({foot.layer})) if pad.through else frozenset({foot.layer})
    stations = frozenset(x for st in path.stations for x in st)
    arrive = []
    for oref, of in sorted((feet or {}).items()):
        if oref == foot.ref or of.at is None:
            continue
        for q in of.pads:
            if f"{oref}.{q.num}" in stations and _pad_layers(of, q) & layers:
                arrive.append((f"{oref}.{q.num}", _pad_rect(of, q)))
    r = _pad_rect(foot, pad)
    cx, cy = (r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0
    out: list[Corridor] = []
    for side, (dx, dy) in _SIDES:
        s = dx or dy
        across = (r[3] - r[1]) if dx else (r[2] - r[0])
        half = (r[2] - r[0]) / 2.0 if dx else (r[3] - r[1]) / 2.0
        wn = min(across, w)
        d = half + worst + wn / 2.0 + 1e-4
        ex, ey = cx + dx * d, cy + dy * d
        stub = (min(cx, ex) - (wn / 2.0 if dy else 0.0), min(cy, ey) - (wn / 2.0 if dx else 0.0), max(cx, ex) + (wn / 2.0 if dy else 0.0), max(cy, ey) + (wn / 2.0 if dx else 0.0))
        neck = wn if wn < w - 1e-9 else None
        head = dict(pad=f"{foot.ref}.{num}", side=side, source=lk.source.split(" (")[0], net=path.net, width=w, neck=neck, clearance=clear, stations=stations, layers=layers)
        shut = next((q for q in others if _rect_gap(stub, _pad_rect(foot, q)) < table.between(path.net, q.net)[0]), None)
        if shut is not None:
            out.append(Corridor(rects=(), dead=True, closed=f"{foot.ref}.{shut.num} [{shut.net}]", **head))
            continue
        edge = (r[2] if dx > 0 else r[0]) if dx else (r[3] if dy > 0 else r[1])
        turn = max(PAD_NECK_MAX_MM if neck is not None else 0.0, worst + wn / 2.0)
        reach = turn + w / 2.0 + clear
        far, end, dead = edge + s * reach, "", False
        bound = (limit.x1 if dx > 0 else limit.x0) if dx else (limit.y1 if dy > 0 else limit.y0)
        if (far - bound) * s > 0:
            far, end, dead = bound, "the board's content edge", True
        hw = w / 2.0 + clear
        mid = cy if dx else cx

        def ahead(qr):
            lat = (qr[1], qr[3]) if dx else (qr[0], qr[2])
            if lat[1] <= mid - hw or lat[0] >= mid + hw:
                return None
            near = (qr[0] if dx > 0 else qr[2]) if dx else (qr[1] if dy > 0 else qr[3])
            return None if (near - edge) * s < 0 else near

        # The part's own other-net copper ahead (buck's U1.2 "up" exit meets U1.5 under the body): no room.
        for q in others:
            near = ahead(_pad_rect(foot, q))
            if near is None:
                continue
            stop = near - s * table.between(path.net, q.net)[0]
            if (far - stop) * s > 0:
                far, end, dead = stop, f"{foot.ref}.{q.num} [{q.net}]", True
        # A station pad of the same path ahead: the path has arrived, and needs no room past it.
        for name, qr in arrive:
            near = ahead(qr)
            if near is not None and (far - near) * s > 0:
                far, end, dead = near, f"{name}, a station of this path", False
        start_full = edge + s * (PAD_NECK_MAX_MM if neck is not None else 0.0)

        def band(a: float, b: float, hw: float):
            lo, hi = min(a, b), max(a, b)
            return (lo, cy - hw, hi, cy + hw) if dx else (cx - hw, lo, cx + hw, hi)

        rects = []
        if neck is not None:
            stop_neck = start_full if (far - start_full) * s > 0 else far
            if (stop_neck - edge) * s > 0:
                rects.append(band(edge, stop_neck, neck / 2.0 + clear))
        if (far - start_full) * s > 0:
            rects.append(band(start_full, far, w / 2.0 + clear))
        out.append(Corridor(rects=tuple(tuple(round(v, 4) for v in x) for x in rects), reach=round(reach, 4), length=round(max((far - edge) * s, 0.0), 4), end=end, dead=dead, **head))
    return out


def _past_edge(cor: Corridor, foot: Foot, rect) -> float:
    """How far past the end pad's edge, along the exit, a rect's near side lies (0 when it overlaps the edge)."""
    ref, _dot, num = cor.pad.partition(".")
    pad = next(q for q in foot.pads if q.num == num)
    r = _pad_rect(foot, pad)
    if cor.side == "right":
        d = rect[0] - r[2]
    elif cor.side == "left":
        d = r[0] - rect[2]
    elif cor.side == "down":
        d = rect[1] - r[3]
    else:
        d = r[1] - rect[3]
    return round(max(d, 0.0), 3)


def _shape(cor: Corridor) -> str:
    """`0.532 mm neck + 2 x 0.2 mm for 1 mm, then 0.781 mm + 2 x 0.2 mm to 1.59 mm past U1.2's edge`."""
    from .constraints import PAD_NECK_MAX_MM

    full = f"{cor.width:g} mm + 2 x {cor.clearance:g} mm to {cor.length:g} mm past {cor.pad}'s edge"
    if cor.neck is None:
        return full
    if cor.length <= PAD_NECK_MAX_MM + 1e-9:
        return f"{cor.neck:g} mm neck + 2 x {cor.clearance:g} mm to {cor.length:g} mm past {cor.pad}'s edge"
    return f"{cor.neck:g} mm neck + 2 x {cor.clearance:g} mm for {PAD_NECK_MAX_MM:g} mm, then {full}"


def corridor_hits(part: Foot, at, rot, corridors) -> list[str]:
    """`part`'s pads at (`at`, `rot`) inside any live corridor (not `dead`) on one of its layers, one fact per corridor:
    `U1.2's down exit corridor (SW, Path line 60)`. A pad that is a station of that path is the path's own, never an
    intrusion."""
    hits = []
    for cor in corridors:
        if cor.dead:
            continue
        for q in part.pads:
            if not q.num or f"{part.ref}.{q.num}" in cor.stations or not (_pad_layers(part, q) & cor.layers):
                continue
            pr = _pad_rect(part, q, at=at, rot=rot)
            if any(_rects_overlap(pr, x) for x in cor.rects):
                hits.append(f"{cor.pad}'s {cor.side} exit corridor ({cor.net}, {cor.source})")
                break
    return hits


def corridor_errors(job, feet: dict[str, Foot]) -> list[str]:
    """Decision 4, the declared placement's half: every pad a `Path()` starts or ends on, on a part that is placed,
    none of whose exits is open, at least one of them held on each of its layers by copper of a part a declared
    CSS/absolute/edge `Place()` put there (those are never moved; a `Place(to=)` part is placed around corridors
    and never counts). An exit is also not open when it is `closed` or `dead` (the part's own copper, or the board's
    edge). One error per such pad, facts only, before routing."""
    ends = path_ends(job)
    if not ends:
        return []
    declared = {s.ref: s for s in job.places if not s.to}
    board = _board_of(job)
    content = content_rect(board)
    out: list[str] = []
    for ref in sorted(ends):
        foot = feet.get(ref)
        if foot is None or foot.at is None:
            continue
        for num, path, lk in ends[ref]:
            cors = pad_corridors(job, foot, num, path, lk, content, feet=feet)
            if not cors:
                continue
            lines: list[str] = []
            placed_by = False
            for cor in cors:
                if cor.closed:
                    lines.append(f"{cor.side}: no stub leaves past {cor.closed}, {ref}'s own pad")
                    continue
                if cor.dead:
                    lines.append(f"{cor.side}: it stops at {cor.end} {cor.length:g} mm past {cor.pad}'s edge, short of the {cor.reach:g} mm the corridor needs")
                    continue
                held: dict[str, list[str]] = {}
                for oref in sorted(declared):
                    other = feet.get(oref)
                    if other is None or other.at is None or oref == ref:
                        continue
                    for q in other.pads:
                        if not q.num or f"{oref}.{q.num}" in cor.stations:
                            continue
                        on = _pad_layers(other, q) & cor.layers
                        rect = _pad_rect(other, q)
                        if not on or not any(_rects_overlap(rect, x) for x in cor.rects):
                            continue
                        fact = f"{oref}.{q.num} [{q.net or 'no net'}] (layout.core.py:{declared[oref].line}) {_past_edge(cor, foot, rect):g} mm past {cor.pad}'s edge"
                        for L in sorted(on):
                            held.setdefault(L, []).append(fact)
                if set(held) != set(cor.layers):
                    break
                who = list(dict.fromkeys(f for L in sorted(held) for f in held[L]))
                on_layers = f" on {', '.join(sorted(cor.layers))}" if len(cor.layers) > 1 else ""
                lines.append(f"{cor.side}: {', '.join(who)}{on_layers}, inside its {_shape(cor)}")
                placed_by = True
            else:
                if placed_by:
                    out.append(f"{path.net}: every exit of {ref}.{num}, where {lk.source.split(' (')[0]} {'starts' if f'{ref}.{num}' in path.stations[0] else 'ends'}, is closed: " + "; ".join(lines))
    return out


def resolve_places(design: Design, job, seeds) -> tuple[list[PlaceSpec], list[str], list[tuple[str, float, float]]]:
    """Every Place() as an absolute (at, rot). Anchors by CSS, then fiducials in the free corners,
    then relations. Returns (places, moves, fiducial spots).

    `seeds` is every part at its seed pose (`place_native.seed_feet`: the part's `.kicad_mod`, posed
    where the seed grid puts it) — the Python model; no board text is read (`docs/direction.md` §1)."""
    from .foot_native import foot_of

    board = _board_of(job)
    regions = resolve_regions(board, job.regions)
    content = content_rect(board)
    libs = {pf.ref: pf.lib for pf in seeds}
    seed_at = {pf.ref: (pf.at[0], pf.at[1], pf.rot) for pf in seeds}
    feet: dict[str, Foot] = {}
    stack, clearance = lane_rules(job)
    for pf in seeds:
        foot = foot_of(pf)
        foot.at, foot.rot = None, 0.0
        foot.lane(stack, clearance)
        feet[pf.ref] = foot
    keepouts = [resolve_keepout(ko, board, regions) for ko in job.keepouts]
    values = {inst.ref: (inst.value or inst.part.value or "") for inst in design.instances}
    # File order, and nothing else: the first Place() written gets the closest spot. Grouping by
    # pin (all of DVDD's caps before AVDD's) put a bulk cap where the neighbouring pin's cap had to go.
    order = {p.ref: i for i, p in enumerate(job.places)}
    moves: list[str] = []
    resolved: dict[str, PlaceSpec] = {}
    placed: list[Foot] = []

    def settle(spec: PlaceSpec, at: tuple[float, float], rot: float) -> PlaceSpec:
        foot = feet[spec.ref]
        foot.at, foot.rot = (round(at[0], 4), round(at[1], 4)), rot % 360
        placed.append(foot)
        # The settled pose is a frame coordinate: `parent=None`, or every later resolve of it (`poses_of_places`, apply,
        # check) measured it from the Region's corner a second time (C_IN1 in a Region at top=12 landed at y=26.98, not
        # 14.98). Which Region a part is in is read from `design.places`, which keep their parent.
        done = replace(spec, at=foot.at, rot=foot.rot, position="absolute", from_box="origin", left=foot.at[0], top=foot.at[1], right=None, bottom=None,
                       margin_left=0, margin_right=0, margin_top=0, margin_bottom=0, translate_x=None, translate_y=None, parent=None)
        resolved[spec.ref] = done
        return done

    specced = {p.ref for p in job.places}
    if getattr(job, "outline", None) is None:
        # No Outline() in layout.core.py: the outline is derived from the placement (docs/board-outline.md B.2).
        return _resolve_derived(design, job, board, regions, feet, libs, keepouts, order, moves, resolved, placed, settle)
    for ref, foot in sorted(feet.items()):
        if ref in specced:
            continue
        at = seed_at[ref]
        foot.at, foot.rot = (at[0], at[1]), at[2]
        placed.append(foot)  # a fiducial, or anything the seed put down: keep off it
    pending: list[PlaceSpec] = []
    for spec in job.places:
        if spec.ref not in feet:
            continue  # apply reports missing refs
        if spec.to:
            pending.append(spec)
            continue
        s = _edge_css(spec, feet[spec.ref], edge_clear(job), origin=getattr(board, "origin", None) or (0.0, 0.0)) if spec.edge else spec
        rp = resolve_place(s, board, libs[spec.ref], regions)
        if rp.at is None:
            continue
        settle(rp, rp.at, rp.rot)

    # Fiducials take the free corners once the anchors are down; relations keep off them.
    spots = fiducial_spots(board_rect(board), placed) if not any(r.startswith("FID") for r in feet) else []
    for ref, x, y in spots:
        placed.append(Foot(ref, (-FID_HALF, -FID_HALF, FID_HALF, FID_HALF), [Pad("", 0.0, 0.0, 1.0, 1.0, "")], at=(x, y), rot=0.0))
    if 0 < len(spots) < 3:
        moves.append(f"only {len(spots)} fiducial spot(s) are free: the anchors cover the other corners and edge middles; JLC wants 3")

    # Relations in dependency order: a part attached to a pending part waits for it.
    ends = path_ends(job)
    guard = 0
    while pending:
        guard += 1
        ready = [p for p in pending if parse_refpin(p.to)[0] in resolved or parse_refpin(p.to)[0] not in {q.ref for q in pending}]
        if not ready or guard > 64:
            names = ", ".join(sorted(p.ref for p in pending))
            raise ValueError(f"Place(to=...) cycle or missing target among {names}")
        ready.sort(key=lambda p: order[p.ref])
        spec = ready[0]
        pending.remove(spec)
        tref, tpin = parse_refpin(spec.to)
        if tref not in feet or feet[tref].at is None:
            what = "is no part of the board" if tref not in feet else "has no position (no Place() put it down)"
            moves.append(f"{spec.ref}: {'layout.core.py:' + str(spec.line) if spec.line else 'its Place()'} attaches it to {tref}, which {what}")
            continue
        # Decision 4: the exit corridors of the target's other pads a declared Path() starts or ends on.
        cors = {num: pad_corridors(job, feet[tref], num, path, lk, content, feet=feet) for num, path, lk in ends.get(tref, ())}
        # Postfix B11 (refutation placement r2): the pads the to= names are the ones whose corridors the part may sit
        # in; every other pad of the target keeps its corridors, whichever same-net pad the search anchors on (R4,
        # to=U1.- (pin 3), landed in U1.OUT's (pin 4) exit when the search anchored on pin 4).
        from .paths import station_pads

        anet = _pin_net(design, tref, tpin) or ""
        named = {x.split(".", 1)[1] for x in station_pads(design, anet, spec.to)} if anet else set()
        at, rot, note = _attach(spec, feet[spec.ref], feet[tref], anet, placed, keepouts, content, regions, corridors=cors, named=named, clear=edge_clear(job))
        settle(spec, at, rot)
        if note:
            moves.append(note)
    return [resolved.get(p.ref, p) for p in job.places], moves, spots


def _relate(design, job, feet, placed, keepouts, regions, resolved, order, moves, settle, specs, limits) -> None:
    """Place the relations `specs` in dependency and file order, each searched within `limits` in turn (the first with
    a clear spot wins; the last one's note stands when none has one). `_attach` insets each limit by the edge
    clearance, so a caller wanting an exact box hands it grown by that much."""
    from .paths import station_pads

    ends = path_ends(job)
    pending = list(specs)
    names = {q.ref for q in pending}
    guard = 0
    while pending:
        guard += 1
        ready = [p for p in pending if parse_refpin(p.to)[0] in resolved or parse_refpin(p.to)[0] not in {q.ref for q in pending}]
        if not ready or guard > 64 + len(names):
            raise ValueError(f"Place(to=...) cycle or missing target among {', '.join(sorted(p.ref for p in pending))}")
        ready.sort(key=lambda p: order[p.ref])
        spec = ready[0]
        pending.remove(spec)
        tref, tpin = parse_refpin(spec.to)
        if tref not in feet or feet[tref].at is None:
            what = "is no part of the board" if tref not in feet else "has no position (no Place() put it down)"
            moves.append(f"{spec.ref}: {'layout.core.py:' + str(spec.line) if spec.line else 'its Place()'} attaches it to {tref}, which {what}")
            continue
        anet = _pin_net(design, tref, tpin) or ""
        named = {x.split(".", 1)[1] for x in station_pads(design, anet, spec.to)} if anet else set()
        # A clear spot in an earlier limit wins while it is beside the pin (within DECAP_NEXT_MM, the decoupling rule's
        # reach); farther than that, the later limits are searched too and a spot nearer by more than DECAP_MM takes it
        # (c3_usb: I0 left U1's caps a clear spot 8.9 mm from U1.3V3, and the open board had one 1.6 mm away).
        tpads = [feet[tref].pad_world(p) for p in feet[tref].pads if p.net == anet] or [feet[tref].center()]

        def reach(g):
            f = feet[spec.ref]
            own = [f.pad_world(p, at=g[0], rot=g[1]) for p in f.pads if p.net == anet] or [g[0]]
            return min(math.hypot(a[0] - b[0], a[1] - b[1]) for a in own for b in tpads)

        # The first decoupling cap on an IC pin is held to DECAP_MM (the rule `_issues` checks): a clear spot within it
        # wins in whichever limit has it, so an in-limit spot 3.4 mm away no longer beats an open one 1.57 mm away (buck
        # with R_FB_BOT anchored: C_IN1 3.40 and C_IN2 4.34 mm from U1.VIN, refute:placement 2026-10-07).
        first = _first_decap_limit(design, job, feet, spec, tref, tpin, anet, resolved, reach_of=lambda r: _reach_now(feet, r, anet, tpads))
        got = None
        for lim in limits:
            cors = {num: pad_corridors(job, feet[tref], num, path, lk, lim, feet=feet) for num, path, lk in ends.get(tref, ())}
            g = _attach(spec, feet[spec.ref], feet[tref], anet, placed, keepouts, lim, regions, corridors=cors, named=named, clear=edge_clear(job))
            if first is not None and g[2] is None and reach(g) <= first:
                if got is None or got[2] is not None or reach(got) > first:
                    got = g
                break
            if got is None or (g[2] is None and (got[2] is not None or reach(g) < reach(got) - DECAP_MM)):
                got = g
            if first is None and got[2] is None and reach(got) <= DECAP_NEXT_MM:
                break
        at, rot, note = got
        settle(spec, at, rot)
        if note:
            moves.append(note)


def _reach_now(feet, ref: str, net: str, tpads) -> float:
    """How far a placed part's pads on `net` are from the target pads `tpads`."""
    f = feet[ref]
    own = [f.pad_world(p) for p in f.pads if p.net == net] or [f.center()]
    return min(math.hypot(a[0] - b[0], a[1] - b[1]) for a in own for b in tpads)


def _first_decap_limit(design, job, feet, spec, tref: str, tpin: str, anet: str, resolved, reach_of) -> float | None:
    """DECAP_MM (plus a closed pad row's fanout lane) when `spec` puts the first decoupling cap on an IC's supply pin: a
    two-pad C on a power net and a ground net, attached to a part with more than two pads, and no cap already settled on
    that pin within the limit. None for any other relation (it keeps the DECAP_NEXT_MM reach)."""
    if not spec.ref.startswith("C") or anet not in design.nets or getattr(design.nets[anet], "kind", "") != "power":
        return None
    f = feet[spec.ref]
    if len(f.pads) != 2 or len([p for p in feet[tref].pads if p.num]) <= 2:
        return None
    ground = {n for n, net in design.nets.items() if getattr(net, "kind", "") == "ground"}
    if not ({p.net for p in f.pads} & ground):
        return None
    cs = getattr(job, "constraints", None)
    if spec.ref in {r for lp in (getattr(cs, "loops", ()) if cs is not None else ()) for r in lp.parts}:
        return None
    pads = {p.num for p in feet[tref].pads if p.net == anet}
    extra = feet[tref].lane_mm if pads & set(getattr(feet[tref], "closed", ()) or ()) else 0.0
    limit = DECAP_MM + extra
    for r, other in resolved.items():
        if r == spec.ref or not r.startswith("C") or str(getattr(other, "to", "") or "").split(".", 1)[0] != tref:
            continue
        if feet.get(r) is not None and feet[r].at is not None and any(p.net == anet for p in feet[r].pads) and reach_of(r) <= limit:
            return None
    return limit


def _resolve_derived(design, job, board, regions, feet, libs, keepouts, order, moves, resolved, placed, settle):
    """B.2 (docs/board-outline.md): anchors in the frame, relations limited to the anchors' box, edge groups shaped
    alone and docked, fixed sides, fiducials last; then R (the routing box) and the placement-only fit, set on the job
    (`job.outline` = R, `job.derived`). Raises `outline.OutlineError` with a B.5 fact."""
    from types import SimpleNamespace

    from . import outline as ol

    unplaced = sorted(r for r in feet if r not in {p.ref for p in job.places} and not r.startswith("FID"))
    if unplaced:
        raise ol.OutlineError(
            f"{', '.join(unplaced)} {'has' if len(unplaced) == 1 else 'have'} no Place() in layout.core.py; with no Outline() the outline is "
            f"derived from where the parts are, and {'this has' if len(unplaced) == 1 else 'these have'} no position"
        )
    def anchor(spec):
        rp = resolve_place(spec, board, libs[spec.ref], regions)
        if rp.at is None:
            raise ol.OutlineError(f"{'layout.core.py:' + str(spec.line) + ': ' if spec.line else ''}Place({spec.ref!r}) has no left= or at=: with no Outline() there is no width to centre it in")
        settle(rp, rp.at, rp.rot)

    def relations(specs, limits):
        _relate(design, job, feet, placed, keepouts, regions, resolved, order, moves, settle, specs, limits)

    # A core rule area (`Pour(None, keepout=...)`) counts toward the derived outline as a Keepout does (docs/board-outline.md
    # A.5, B.1): it is in I, an edge group docks outside it, its face may fix a side, and the fit holds it. It reached only
    # the post-route fit before, so one past a side an edge part fixes was left wholly outside Edge.Cuts with no word
    # (refute:outline 2026-10-07).
    rules = ol.rule_areas(design)
    ctx = SimpleNamespace(
        job=job, feet=feet, placed=placed, resolved=resolved, keepouts=keepouts + [b for _k, b in rules],
        keepout_specs=list(job.keepouts) + [k for k, _b in rules],
        region_boxes=[(r.x0, r.y0, r.x1, r.y1) for r in regions.values()], regions=regions, region_specs=list(job.regions),
        edge_clear=edge_clear(job),
        anchor=anchor, relations=relations, settle=lambda spec, at, rot: settle(spec, at, rot),
    )
    d = ol.place_derived(ctx)
    for name, x, y in d.fids:
        placed.append(Foot(name, (-FID_HALF, -FID_HALF, FID_HALF, FID_HALF), [Pad("", 0.0, 0.0, 1.0, 1.0, "")], at=(x, y), rot=0.0))
    if 0 < len(d.fids) < 3:
        moves.append(f"only {len(d.fids)} fiducial spot(s) are free: the anchors cover the other corners and edge middles; JLC wants 3")
    d.A, d.pitch = ol.allowance_of(job)
    d.m = ol.margin_of(job, poured=ol.has_pour(job, design))
    extra = [(f"Keepout {k.name}", b) for k, b in zip(job.keepouts, keepouts)] + [(f"Region {n}", (r.x0, r.y0, r.x1, r.y1)) for n, r in regions.items()]
    extra += [(k.label, b) for k, b in rules]
    # Core copper beyond a free side is design: the side grows to hold it (B.5-5), at placement already, so R holds it.
    core_cu = ol.copper_boxes([c for c in getattr(design, "copper", ()) if c.source == "core"])
    d.fit, d.sides = ol.fit(d, [f for f in placed if not f.ref.startswith("FID")], copper=core_cu, extra=extra)
    d.R = ol.routing_box(d)
    job.outline = d.R
    job.derived = d
    # The relations of the edge groups moved with them after `settle` wrote their poses: the feet are the truth.
    for ref, spec in list(resolved.items()):
        f = feet.get(ref)
        if f is not None and f.at is not None and spec.at != f.at:
            resolved[ref] = replace(spec, at=f.at, left=f.at[0], top=f.at[1])
    return [resolved.get(p.ref, p) for p in job.places], moves, d.fids


def _area(box) -> float:
    return max(box[2] - box[0], 0.0) * max(box[3] - box[1], 0.0)


_CAP = re.compile(r"^\s*([0-9.]+)\s*([pnuµm]?)F?\s*$", re.I)
_SCALE = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0}


def _farads(value: str) -> float:
    """The smallest cap goes closest to the pin; anything that is not a cap value sorts last."""
    m = _CAP.match(value.replace("F", "").replace("f", "") + "F") if value else None
    if not m:
        return float("inf")
    return float(m.group(1)) * _SCALE.get(m.group(2).lower() if m.group(2) != "µ" else "µ", 1.0)


def _attach(spec: PlaceSpec, part: Foot, target: Foot, net: str, placed: list[Foot], keepouts, content: Rect, regions, corridors=None, named=None, clear: float = EDGE_CLEAR) -> tuple[tuple[float, float], float, str | None]:
    tpads = [p for p in target.pads if p.net == net]
    apads = [p for p in part.pads if p.net == net]
    if not tpads or not apads:
        at = f"layout.core.py:{spec.line}: " if spec.line else ""
        lacks = " and ".join(f.ref for f, pads in ((target, tpads), (part, apads)) if not pads)
        raise ValueError(f"{at}the to= of Place({spec.ref!r}) names {spec.to}, on net {net!r}, and {lacks} has no pad on it" if net else f"{at}the to= of Place({spec.ref!r}) names {spec.to}, which is on no net")
    gap = GAP if spec.gap is None else float(spec.gap)
    tc = target.center()
    limit = regions[spec.parent] if spec.parent else Rect(content.x0 + clear, content.y0 + clear, content.x1 - clear, content.y1 - clear)
    others = [f for f in placed if f.ref != part.ref]
    # World pads of everything placed, by net, for the "lean toward your other nets" score.
    known: dict[str, list[tuple[float, float]]] = {}
    for f in others:
        for p in f.pads:
            if p.net:
                known.setdefault(p.net, []).append(f.pad_world(p))
    best = None
    stuck_best = None
    rots = (spec.rot % 360,) if spec.rot_set else _ROTS
    for tp in tpads:
        tw = target.pad_world(tp)
        # Every corridor but those of the pads the to= names: "another pad of that part" (decision 4). Without a
        # named set (a caller that resolved none), the anchor pad's own.
        own = named if named else {tp.num}
        cors = [c for num, cs_ in sorted((corridors or {}).items()) if num not in own for c in cs_]
        if spec.toward:
            dirs = [_DIRS[spec.toward]]
        elif tp.num in target.escape:
            # A closed-row pad escapes straight out through its lane; a part beside the row
            # (R10 left of the DS2 Addon's TSSOP, on the row's own height) needs a hop that runs
            # along the lane under the pads between, and those pads then have no escape.
            dirs = [_EDGE_OUT[target.escape[tp.num]]]
        else:
            dx, dy = tw[0] - tc[0], tw[1] - tc[1]
            first = (math.copysign(1.0, dx), 0.0) if abs(dx) >= abs(dy) else (0.0, math.copysign(1.0, dy))
            second = (0.0, math.copysign(1.0, dy) if dy else 1.0) if abs(dx) >= abs(dy) else (math.copysign(1.0, dx) if dx else 1.0, 0.0)
            dirs = [first, second, (-second[0], -second[1]), (-first[0], -first[1])]
        for di, e in enumerate(dirs):
            for ri, rot in enumerate(rots):
                for ap in apads:
                    ao = _rotate(ap.x, ap.y, rot)
                    cb = rotate_local_bounds(*part.box, rot)
                    ck = rotate_local_bounds(*(part.keep or part.box), rot)
                    tbox, tkeep = target.world_box(), target.world_keep()
                    # Along e, the part's courtyard must start past the target's courtyard + gap,
                    # and past its fanout lane if that side of the target is a closed pad row.
                    if e[0]:
                        edge = max(tbox[2] + gap, tkeep[2]) if e[0] > 0 else min(tbox[0] - gap, tkeep[0])
                        near = cb[0] if e[0] > 0 else cb[2]
                        d0 = (edge - (tw[0] + near - ao[0])) * e[0]
                    else:
                        edge = max(tbox[3] + gap, tkeep[3]) if e[1] > 0 else min(tbox[1] - gap, tkeep[1])
                        near = cb[1] if e[1] > 0 else cb[3]
                        d0 = (edge - (tw[1] + near - ao[1])) * e[1]
                    d0 = max(d0, 0.0)
                    side = (-e[1], e[0])  # across the attach direction: beside whoever took the spot first
                    found = None
                    d = d0
                    while found is None and d <= d0 + _REACH + 1e-9:
                        for lat in _laterals():
                            aw = (tw[0] + e[0] * d + side[0] * lat, tw[1] + e[1] * d + side[1] * lat)
                            at = (aw[0] - ao[0], aw[1] - ao[1])
                            box = (at[0] + cb[0], at[1] + cb[1], at[0] + cb[2], at[1] + cb[3])
                            keep = (at[0] + ck[0], at[1] + ck[1], at[0] + ck[2], at[1] + ck[3])
                            clash = _clashes(box, keep, others, keepouts, limit, gap)
                            if not clash and cors:
                                clash = corridor_hits(part, at, rot, cors)
                            if not clash:
                                found = (at, math.hypot(d, lat))
                                break
                            if stuck_best is None or d < stuck_best[0]:
                                stuck_best = (d, at, rot, clash)
                        d += _STEP
                    if found is None:
                        continue
                    at, d = found
                    lean = 0.0
                    for p in part.pads:
                        if p is ap or not p.net:
                            continue
                        pw = part.pad_world(p, at=at, rot=rot)
                        sites = known.get(p.net) or []
                        if sites:
                            lean += min(math.hypot(pw[0] - s[0], pw[1] - s[1]) for s in sites)
                    # The attach distance is what the decap rule measures; the lean of the other
                    # pads toward their nets is a tie-breaker, not a reason to sit 3 mm from the pin.
                    score = (d + 0.15 * lean + 0.02 * ri + 0.5 * di, at, rot)
                    if _TRACE == spec.ref:
                        print(f"  {spec.ref} dir={e} rot={rot:g} pad={ap.num} d={d:.2f} lean={lean:.2f} score={score[0]:.2f} at=({at[0]:.2f},{at[1]:.2f})")
                    if best is None or score < best:
                        best = score
    if best is not None:
        return best[1], best[2], None
    if stuck_best is None:
        raise ValueError(f"Place({spec.ref!r}, to={spec.to!r}): nowhere to stand")
    d, at, rot, clash = stuck_best
    # Postfix B12 (refutation placement r2): the fallback pose was never tested against the corridors (they are tested
    # only where nothing else clashes), so a part left in a live exit corridor was reported with its keepouts alone.
    own = named if named else set()
    every = [c for num, cs_ in sorted((corridors or {}).items()) if num not in own for c in cs_]
    clash = sorted(set(clash) | set(corridor_hits(part, at, rot, every)))
    what = ", ".join(clash)
    return at, rot, (
        f"{spec.ref}: no clear spot next to {spec.to} within {_REACH:g} mm: {what} in the way; it stands at "
        f"({at[0]:g},{at[1]:g}) rotated {rot:g}"
    )


def _laterals(reach: float = 4.0):
    yield 0.0
    lat = _STEP
    while lat <= reach + 1e-9:
        yield lat
        yield -lat
        lat += _STEP


def _clashes(box, keep, others: list[Foot], keepouts, limit: Rect, gap: float) -> list[str]:
    """Courtyards keep `gap` apart; a fanout lane (`keep` beyond `box`) keeps courtyards out of it."""
    hits: list[str] = []
    if not _inside(box, limit):
        hits.append("the board edge")
    for f in others:
        if _overlap(box, f.world_box(), gap) or _overlap(keep, f.world_box()) or _overlap(box, f.world_keep()):
            hits.append(f.ref)
    for i, ko in enumerate(keepouts):
        if _overlap(box, ko, 0.0):
            hits.append(f"keepout {i + 1}")
    return hits


# ---------------------------------------------------------------- report


def posed_feet_of(job, posed) -> dict[str, Foot]:
    """{ref: Foot} of the placed parts (`foot_native.PosedFoot`s: each `.kicad_mod` posed by its
    `Pose`), laned — the one reading `layout_report`, `route_checks.build_ctx` and the router's scene
    (`route_scene.build_scene`) all make of a part, so no two of them can disagree about its box."""
    from .foot_native import foot_of

    stack, clearance = lane_rules(job)
    out: dict[str, Foot] = {}
    for pf in posed:
        f = foot_of(pf)
        f.lane(stack, clearance)
        out[pf.ref] = f
    return out


def layout_report(design: Design, job, posed) -> list[str]:
    """Moves, in words, from the placed parts (`posed`: `foot_native.PosedFoot`s): overlaps, off-board,
    far decaps, connectors off the edge."""
    board = _board_of(job)
    content = content_rect(board)
    feet = posed_feet_of(job, posed)
    issues: list[str] = []
    refs = sorted(feet)
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            if _overlap(feet[a].world_box(), feet[b].world_box()):
                issues.append(f"{a} and {b} overlap (courtyards)")
                continue
            for ic, part in ((a, b), (b, a)):
                if feet[ic].closed and _overlap(feet[ic].world_keep(), feet[part].world_box()):
                    issues.append(
                        f"{part} sits in {ic}'s fanout lane: nothing passes between {ic}'s pads, so each row needs "
                        f"{feet[ic].lane_mm:g} mm outside it for its escape vias"
                    )
    for ref in refs:
        box = feet[ref].world_box()
        over = max(content.x0 - box[0], content.y0 - box[1], box[2] - content.x1, box[3] - content.y1, 0.0)
        if over > 1e-3 and not _placed_on_edge(ref, job):
            issues.append(f"{ref} sits {over:.1f} mm outside the board")
    inst_by = {inst.ref: inst for inst in design.instances}
    ground = {n for n, net in design.nets.items() if getattr(net, "kind", "") == "ground"}
    power = {n for n, net in design.nets.items() if getattr(net, "kind", "") == "power"}
    ics = {ref for ref, f in feet.items() if len([p for p in f.pads if p.num]) > 2}
    by_pin: dict[tuple[str, str], list[tuple[float, str]]] = {}
    # docs/net-path-physics.md §4.5, §6: a cap a declared Loop() names is that loop's, and a cap whose
    # Place(to=) names a pin of a part with more than two pads on the cap's power net is measured to
    # that pin, not to the nearest guess.
    cs = getattr(job, "constraints", None)
    in_loop = {r for lp in (getattr(cs, "loops", ()) if cs is not None else ()) for r in lp.parts}
    place_of = {pl.ref: pl for pl in design.places}
    attached: dict[str, int] = {}
    for ref in refs:
        f = feet[ref]
        inst = inst_by.get(ref)
        if inst is None or not ref.startswith("C") or len(f.pads) != 2 or ref in in_loop:
            continue
        nets = {p.net for p in f.pads}
        pnet = next((n for n in nets if n in power), None)
        if pnet is None or not (nets & ground):
            continue
        cpad = next(p for p in f.pads if p.net == pnet)
        cw = f.pad_world(cpad)
        nearest = None
        spec = place_of.get(ref)
        to = str(getattr(spec, "to", "") or "")
        if "." in to:
            tref, tpin = to.split(".", 1)
            tinst = inst_by.get(tref)
            if tref in feet and tref in ics and tinst is not None and tpin in tinst.part.pins and tinst.pins.get(tpin) == pnet:
                for p in feet[tref].pads:
                    if p.num in tinst.part.pins[tpin].pads:
                        pw = feet[tref].pad_world(p)
                        dmm = math.hypot(pw[0] - cw[0], pw[1] - cw[1])
                        if nearest is None or dmm < nearest[0]:
                            nearest = (dmm, tref, p.num)
                if nearest is not None:
                    attached[ref] = getattr(spec, "line", 0)
        for ic in sorted(ics) if nearest is None else ():
            for p in feet[ic].pads:
                if p.net != pnet:
                    continue
                pw = feet[ic].pad_world(p)
                dmm = math.hypot(pw[0] - cw[0], pw[1] - cw[1])
                if nearest is None or dmm < nearest[0]:
                    nearest = (dmm, ic, p.num)
        if nearest:
            dmm, ic, num = nearest
            by_pin.setdefault((ic, _pin_name(inst_by[ic], num)), []).append((dmm, ref))
    # Supply pins side by side (AVDD next to DVDD on a 0.65 mm TSSOP) share one decoupling
    # row: the nearest cap of the row is held to 2.5 mm, the others to 5.
    rows: list[tuple[list[tuple[str, str]], list[tuple[float, str]]]] = []
    for (ic, pin), caps in sorted(by_pin.items()):
        num = next((p.pads[0] for n, p in inst_by[ic].part.pins.items() if n == pin), None)
        here = feet[ic].pad_world(next(p for p in feet[ic].pads if p.num == num)) if num else None
        for pins, members in rows:
            if here and any(
                math.hypot(here[0] - feet[oic].pad_world(next(p for p in feet[oic].pads if p.num == onum))[0], here[1] - feet[oic].pad_world(next(p for p in feet[oic].pads if p.num == onum))[1]) <= 1.0
                for oic, onum in pins if oic == ic
            ):
                pins.append((ic, num))
                members.extend((d, r, pin) for d, r in caps)
                break
        else:
            rows.append(([(ic, num)], [(d, r, pin) for d, r in caps]))
    for pins, members in rows:
        members.sort()
        ic = pins[0][0]
        # A closed pad row keeps its fanout lane between the pin and the cap: the cap sits that much further.
        extra = max((feet[oic].lane_mm for oic, num in pins if num in feet[oic].closed), default=0.0)
        for i, (dmm, ref, pin) in enumerate(members):
            limit = (DECAP_MM if i == 0 else DECAP_NEXT_MM) + extra
            if dmm > limit:
                what = "a decoupling cap" if i == 0 else "the next cap on that pin row"
                why = f" ({limit - extra:g} + the {extra:g} mm fanout lane of a closed pad row)" if extra else ""
                if ref in attached:
                    # The attachment is already written: say so and name its line, never repeat it.
                    line = attached[ref]
                    where = f"layout.core.py:{line}" if line else "its Place()"
                    issues.append(f"{ref} is {dmm:.1f} mm from {ic}.{pin}, the pin {where} attaches it to: {what} belongs within {limit:g} mm{why}")
                else:
                    issues.append(
                        f"{ref} is {dmm:.1f} mm from {ic}.{pin}: {what} belongs within {limit:g} mm{why}; no Place(to=) attaches {ref} to {ic}"
                    )
    from fnmatch import fnmatch

    pads_on: dict[str, list[tuple[str, str, float, float]]] = {}
    for ref in refs:
        f = feet[ref]
        for p in f.pads:
            if p.net and p.num:
                x, y = f.pad_world(p)
                pads_on.setdefault(p.net, []).append((ref, p.num, x, y))
    for cn in job.nets:
        if not cn.max_length_mm:
            continue
        for net in sorted(pads_on):
            if not any(fnmatch(net, pat) for pat in cn.patterns):
                continue
            sites = pads_on[net]
            worst = None
            for ref, num, x, y in sites:
                near = min((math.hypot(x - x2, y - y2), r2, n2) for r2, n2, x2, y2 in sites if r2 != ref) if any(r2 != ref for r2, *_ in sites) else None
                if near and (worst is None or near[0] > worst[0]):
                    worst = (near[0], ref, num, near[1], near[2])
            if worst and worst[0] > cn.max_length_mm:
                dmm, ref, num, r2, n2 = worst
                pin = _pin_name(inst_by[ref], num) if ref in inst_by else num
                pin2 = _pin_name(inst_by[r2], n2) if r2 in inst_by else n2
                issues.append(f"{net}: {ref}.{pin} is {dmm:.1f} mm from {r2}.{pin2}, its nearest pad on the net, over NetReq max_mm={cn.max_length_mm:g}")
    # Derived mode: the edges are the placement's own fit (docs/board-outline.md B.3, `pcbc pcb`'s outline).
    fit = (getattr(job, "derived", None) or {}).get("fit")
    ex0, ey0, ex1, ey1 = fit if fit is not None else (lambda r: (r.x0, r.y0, r.x1, r.y1))(board_rect(board))
    ec = edge_clear(job)
    for ref in refs:
        if _placed_on_edge(ref, job):
            continue
        f = feet[ref]
        near = 0.0
        for p in f.pads:
            if not p.num:
                continue
            px, py = f.pad_world(p)
            hw, hh = (p.h, p.w) if f.rot % 180 == 90 else (p.w, p.h)
            over = max(ex0 + ec - (px - hw / 2), ey0 + ec - (py - hh / 2), (px + hw / 2) - (ex1 - ec), (py + hh / 2) - (ey1 - ec), 0.0)
            near = max(near, over)
        if near > ec + 1e-3:
            issues.append(f"{ref}'s pads cross the board edge by {near - ec:.2f} mm: copper needs {ec:g} mm inside it")
        elif near > 1e-3:
            issues.append(f"{ref}'s pads come within {ec:g} mm of the board edge: copper needs {ec:g} mm")
    for ref in refs:
        if not ref.startswith("J"):
            continue
        box = feet[ref].world_box()
        dist = min(box[0] - ex0, box[1] - ey0, ex1 - box[2], ey1 - box[3])
        if dist > EDGE_MM:
            issues.append(f"{ref} is {dist:.1f} mm from the nearest board edge, and a connector is reached from an edge")
    return issues


def _placed_on_edge(ref: str, job) -> bool:
    return any(p.ref == ref and (p.edge or p.overhang) for p in job.places)


def _pin_name(inst, pad: str) -> str:
    for name, pin in inst.part.pins.items():
        if pad in pin.pads:
            return name
    return pad
