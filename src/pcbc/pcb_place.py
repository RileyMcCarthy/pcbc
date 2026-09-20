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
from dataclasses import dataclass, replace

from .copper import _rotate
from .css import Rect, rotate_local_bounds
from .geom import _iter_tagged, footprint_box_local
from .layout import content_rect, footprints_by_ref, resolve_keepout, resolve_place, resolve_regions
from .model import BoardSpec, Design, PlaceSpec

_PAD_AT = re.compile(r"\(at\s+([0-9.+-]+)\s+([0-9.+-]+)(?:\s+([0-9.+-]+))?\)")
_PAD_SIZE = re.compile(r"\(size\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_PAD_NUM = re.compile(r'\(pad\s+"([^"]*)"')
_PAD_NET = re.compile(r'\(net\s+(?:\d+\s+)?"([^"]*)"\)')

GAP = 0.2  # courtyard to courtyard, mm
EDGE_CLEAR = 0.3  # copper to board edge (JLC asks 0.2; routing wants a lane)
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


@dataclass
class Foot:
    ref: str
    box: tuple[float, float, float, float]  # local courtyard
    pads: list[Pad]
    at: tuple[float, float] | None = None
    rot: float = 0.0

    def world_box(self, at=None, rot=None) -> tuple[float, float, float, float]:
        at = at or self.at
        rot = self.rot if rot is None else rot
        x0, y0, x1, y1 = rotate_local_bounds(*self.box, rot)
        return (at[0] + x0, at[1] + y0, at[0] + x1, at[1] + y1)

    def pad_world(self, pad: Pad, at=None, rot=None) -> tuple[float, float]:
        at = at or self.at
        rot = self.rot if rot is None else rot
        dx, dy = _rotate(pad.x, pad.y, rot)
        return (at[0] + dx, at[1] + dy)

    def center(self) -> tuple[float, float]:
        b = self.world_box()
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def parse_foot(ref: str, block: str) -> Foot:
    pads: list[Pad] = []
    for pad in _iter_tagged(block, "pad"):
        num = _PAD_NUM.match(pad)
        at = _PAD_AT.search(pad)
        size = _PAD_SIZE.search(pad)
        if not num or not at or not size:
            continue
        net = _PAD_NET.search(pad)
        w, h = float(size.group(1)), float(size.group(2))
        if round(float(at.group(3) or 0)) % 180 == 90:
            w, h = h, w
        pads.append(Pad(num.group(1), float(at.group(1)), float(at.group(2)), w, h, net.group(1) if net else ""))
    crt = footprint_box_local(block, "courtyard")
    pb = footprint_box_local(block, "pads")
    box = (min(crt[0], pb[0]), min(crt[1], pb[1]), max(crt[2], pb[2]), max(crt[3], pb[3]))
    return Foot(ref, box, pads)


def parse_refpin(spec: str) -> tuple[str, str]:
    if "." not in spec:
        raise ValueError(f"{spec!r}: write REF.PIN, e.g. U1.VIN")
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
            try:
                tref, tpin = parse_refpin(p.to)
            except ValueError as e:
                fails.append(f"Place({p.ref!r}): {e}")
                continue
            if tref not in refs:
                fails.append(f"Place({p.ref!r}, to={p.to!r}): no part {tref}")
                continue
            if tpin not in parts[tref].part.pins:
                fails.append(f"Place({p.ref!r}, to={p.to!r}): {tref} has no pin {tpin} (pins: {', '.join(parts[tref].part.pins)})")
                continue
            net = _pin_net(design, tref, tpin)
            if p.ref in parts and net not in parts[p.ref].pins.values():
                fails.append(f"Place({p.ref!r}, to={p.to!r}): {p.ref} has no pin on net {net}")
            if p.toward and p.toward not in _DIRS:
                fails.append(f"Place({p.ref!r}): toward must be one of {', '.join(_DIRS)}")
        if p.edge and p.edge not in _EDGE_OUT:
            fails.append(f"Place({p.ref!r}): edge must be one of {', '.join(_EDGE_OUT)}")
        if p.side != "F" and (p.to or p.edge):
            fails.append(f"Place({p.ref!r}): side='B' is not placed by relation yet; use CSS")
    return fails


# ---------------------------------------------------------------- engine


def _board_of(job) -> BoardSpec:
    return BoardSpec(size_mm=job.board_size_mm, padding=job.padding, layers=job.layers, stackup=job.stackup, pcb=job.pcb, planes=job.planes)


def _overlap(a, b, gap: float = 0.0) -> bool:
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


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


def _edge_css(spec: PlaceSpec, foot: Foot) -> PlaceSpec:
    """`edge=` as CSS: flush with that edge (minus overhang), centred along it unless told."""
    out = _EDGE_OUT[spec.edge]
    rot = spec.rot if spec.rot_set else _rot_for_face(_face(foot), out)
    css: dict = {"position": "absolute", "rot": rot, "from_box": "courtyard"}
    along_set = any(getattr(spec, k) is not None for k in ("left", "right", "top", "bottom"))
    css[spec.edge] = -float(spec.overhang)
    if spec.edge in ("top", "bottom"):
        if not along_set:
            css.update(left=0, right=0, margin_left="auto", margin_right="auto")
    else:
        if not along_set:
            css.update(top=0, bottom=0, margin_top="auto", margin_bottom="auto")
    return replace(spec, **css)


FID_INSET = 4.0
FID_HALF = 1.25  # Fiducial_1mm_Mask2mm courtyard


def fiducial_spots(size_mm: tuple[float, float], taken: list[Foot], want: int = 3) -> list[tuple[str, float, float]]:
    """Three fiducials, corners first then edge middles, wherever no anchor sits. The AI never places these."""
    w, h = size_mm
    i = FID_INSET
    candidates = [(w - i, i), (w - i, h - i), (i, h - i), (i, i), (w / 2, i), (w / 2, h - i), (i, h / 2), (w - i, h / 2)]
    spots: list[tuple[str, float, float]] = []
    for x, y in candidates:
        box = (x - FID_HALF, y - FID_HALF, x + FID_HALF, y + FID_HALF)
        if any(_overlap(box, f.world_box(), GAP) for f in taken if f.at is not None):
            continue
        spots.append((f"FID{len(spots) + 1}", x, y))
        if len(spots) == want:
            break
    return spots


def resolve_places(design: Design, job, pcb_text: str) -> tuple[list[PlaceSpec], list[str], list[tuple[str, float, float]]]:
    """Every Place() as an absolute (at, rot). Anchors by CSS, then fiducials in the free corners,
    then relations. Returns (places, moves, fiducial spots)."""
    board = _board_of(job)
    regions = resolve_regions(board, job.regions)
    content = content_rect(board)
    blocks = footprints_by_ref(pcb_text)
    feet: dict[str, Foot] = {ref: parse_foot(ref, blk) for ref, blk in blocks.items()}
    nets_of = _pins_of(design)
    for ref, foot in feet.items():
        for pad in foot.pads:
            pad.net = nets_of.get(ref, {}).get(pad.num, pad.net)
    keepouts = [resolve_keepout(ko, board, regions) for ko in job.keepouts]
    values = {inst.ref: (inst.value or inst.part.value or "") for inst in design.instances}
    order = {p.ref: i for i, p in enumerate(job.places)}  # file order: the first Place() on a pin gets the closest spot
    first_on: dict[tuple[str, str], int] = {}
    for p in job.places:
        if p.to:
            first_on.setdefault(parse_refpin(p.to), order[p.ref])
    moves: list[str] = []
    resolved: dict[str, PlaceSpec] = {}
    placed: list[Foot] = []

    def settle(spec: PlaceSpec, at: tuple[float, float], rot: float) -> PlaceSpec:
        foot = feet[spec.ref]
        foot.at, foot.rot = (round(at[0], 4), round(at[1], 4)), rot % 360
        placed.append(foot)
        done = replace(spec, at=foot.at, rot=foot.rot, position="absolute", from_box="origin", left=foot.at[0], top=foot.at[1], right=None, bottom=None,
                       margin_left=0, margin_right=0, margin_top=0, margin_bottom=0, translate_x=None, translate_y=None)
        resolved[spec.ref] = done
        return done

    from .sexp import footprint_at

    specced = {p.ref for p in job.places}
    for ref, foot in sorted(feet.items()):
        if ref in specced:
            continue
        at = footprint_at(blocks[ref])
        if at is not None:
            foot.at, foot.rot = (at[0], at[1]), at[2]
            placed.append(foot)  # a fiducial, or anything the seed put down: keep off it
    pending: list[PlaceSpec] = []
    for spec in job.places:
        if spec.ref not in feet:
            continue  # apply reports missing refs
        if spec.to:
            pending.append(spec)
            continue
        s = _edge_css(spec, feet[spec.ref]) if spec.edge else spec
        rp = resolve_place(s, board, blocks[spec.ref], regions)
        if rp.at is None:
            continue
        settle(rp, rp.at, rp.rot)

    # Fiducials take the free corners once the anchors are down; relations keep off them.
    spots = fiducial_spots(board.size_mm, placed) if not any(r.startswith("FID") for r in feet) else []
    for ref, x, y in spots:
        placed.append(Foot(ref, (-FID_HALF, -FID_HALF, FID_HALF, FID_HALF), [Pad("", 0.0, 0.0, 1.0, 1.0, "")], at=(x, y), rot=0.0))
    if 0 < len(spots) < 3:
        moves.append(f"only {len(spots)} fiducial spot(s) are free: the anchors cover the other corners and edge middles; JLC wants 3")

    # Relations in dependency order: a part attached to a pending part waits for it.
    guard = 0
    while pending:
        guard += 1
        ready = [p for p in pending if parse_refpin(p.to)[0] in resolved or parse_refpin(p.to)[0] not in {q.ref for q in pending}]
        if not ready or guard > 64:
            names = ", ".join(sorted(p.ref for p in pending))
            raise ValueError(f"Place(to=...) cycle or missing target among {names}")
        ready.sort(key=lambda p: (first_on[parse_refpin(p.to)], _farads(values.get(p.ref, "")), _area(feet[p.ref].box), order[p.ref]))
        spec = ready[0]
        pending.remove(spec)
        tref, tpin = parse_refpin(spec.to)
        if tref not in feet or feet[tref].at is None:
            moves.append(f"{spec.ref}: its target {tref} has no place (Place(to=) needs the target placed by CSS or by relation)")
            continue
        at, rot, note = _attach(spec, feet[spec.ref], feet[tref], _pin_net(design, tref, tpin) or "", placed, keepouts, content, regions)
        settle(spec, at, rot)
        if note:
            moves.append(note)
    return [resolved.get(p.ref, p) for p in job.places], moves, spots


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


def _attach(spec: PlaceSpec, part: Foot, target: Foot, net: str, placed: list[Foot], keepouts, content: Rect, regions) -> tuple[tuple[float, float], float, str | None]:
    tpads = [p for p in target.pads if p.net == net]
    apads = [p for p in part.pads if p.net == net]
    if not tpads or not apads:
        raise ValueError(f"Place({spec.ref!r}, to={spec.to!r}): no pad on net {net!r} on both parts")
    gap = GAP if spec.gap is None else float(spec.gap)
    tc = target.center()
    limit = regions[spec.parent] if spec.parent else Rect(content.x0 + EDGE_CLEAR, content.y0 + EDGE_CLEAR, content.x1 - EDGE_CLEAR, content.y1 - EDGE_CLEAR)
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
        if spec.toward:
            dirs = [_DIRS[spec.toward]]
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
                    tbox = target.world_box()
                    # Along e, the part's courtyard must start past the target's courtyard + gap.
                    if e[0]:
                        edge = tbox[2] + gap if e[0] > 0 else tbox[0] - gap
                        near = cb[0] if e[0] > 0 else cb[2]
                        d0 = (edge - (tw[0] + near - ao[0])) * e[0]
                    else:
                        edge = tbox[3] + gap if e[1] > 0 else tbox[1] - gap
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
                            clash = _clashes(box, others, keepouts, limit, gap)
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
                    score = (d + 0.5 * lean + 0.02 * ri + 0.5 * di, at, rot)
                    if _TRACE == spec.ref:
                        print(f"  {spec.ref} dir={e} rot={rot:g} pad={ap.num} d={d:.2f} lean={lean:.2f} score={score[0]:.2f} at=({at[0]:.2f},{at[1]:.2f})")
                    if best is None or score < best:
                        best = score
    if best is not None:
        return best[1], best[2], None
    if stuck_best is None:
        raise ValueError(f"Place({spec.ref!r}, to={spec.to!r}): nowhere to stand")
    d, at, rot, clash = stuck_best
    what = ", ".join(sorted(clash))
    return at, rot, (
        f"{spec.ref}: no clear spot next to {spec.to} within {_REACH:g} mm: {what} in the way; "
        f"Place({spec.ref!r}, to={spec.to!r}, toward=...) picks another side, or move {what}"
    )


def _laterals(reach: float = 4.0):
    yield 0.0
    lat = _STEP
    while lat <= reach + 1e-9:
        yield lat
        yield -lat
        lat += _STEP


def _clashes(box, others: list[Foot], keepouts, limit: Rect, gap: float) -> list[str]:
    hits: list[str] = []
    if not _inside(box, limit):
        hits.append("the board edge")
    for f in others:
        if _overlap(box, f.world_box(), gap):
            hits.append(f.ref)
    for i, ko in enumerate(keepouts):
        if _overlap(box, ko, 0.0):
            hits.append(f"keepout {i + 1}")
    return hits


# ---------------------------------------------------------------- report


def layout_report(design: Design, job, pcb_text: str) -> list[str]:
    """Moves, in words, from the placed board: overlaps, off-board, far decaps, connectors off the edge."""
    board = _board_of(job)
    content = content_rect(board)
    blocks = footprints_by_ref(pcb_text)
    feet: dict[str, Foot] = {}
    from .sexp import footprint_at

    nets_of = _pins_of(design)
    for ref, blk in blocks.items():
        f = parse_foot(ref, blk)
        at = footprint_at(blk)
        if at is None:
            continue
        f.at, f.rot = (at[0], at[1]), at[2]
        for pad in f.pads:
            pad.net = nets_of.get(ref, {}).get(pad.num, pad.net)
        feet[ref] = f
    issues: list[str] = []
    refs = sorted(feet)
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            if _overlap(feet[a].world_box(), feet[b].world_box()):
                issues.append(f"{a} and {b} overlap (courtyards): give one of them its own spot or Place(to=) the other")
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
    for ref in refs:
        f = feet[ref]
        inst = inst_by.get(ref)
        if inst is None or not ref.startswith("C") or len(f.pads) != 2:
            continue
        nets = {p.net for p in f.pads}
        pnet = next((n for n in nets if n in power), None)
        if pnet is None or not (nets & ground):
            continue
        cpad = next(p for p in f.pads if p.net == pnet)
        cw = f.pad_world(cpad)
        nearest = None
        for ic in sorted(ics):
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
    for (ic, pin), caps in sorted(by_pin.items()):
        caps.sort()
        for i, (dmm, ref) in enumerate(caps):
            limit = DECAP_MM if i == 0 else DECAP_NEXT_MM
            if dmm > limit:
                what = "a decoupling cap" if i == 0 else "the next cap on that pin"
                issues.append(
                    f"{ref} is {dmm:.1f} mm from {ic}.{pin}: {what} belongs within {limit:g} mm; Place({ref!r}, to=\"{ic}.{pin}\")"
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
                issues.append(f"{net}: {ref}.{pin} is {dmm:.1f} mm from {r2}.{pin2}, its nearest pad on the net; NetReq max_mm={cn.max_length_mm:g}: Place({ref!r}, to=\"{r2}.{pin2}\") or bring them together")
    w, h = board.size_mm
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
            over = max(EDGE_CLEAR - (px - hw / 2), EDGE_CLEAR - (py - hh / 2), (px + hw / 2) - (w - EDGE_CLEAR), (py + hh / 2) - (h - EDGE_CLEAR), 0.0)
            near = max(near, over)
        if near > 1e-3:
            issues.append(f"{ref}'s pads come within {EDGE_CLEAR:g} mm of the board edge: copper needs {EDGE_CLEAR:g} mm; move it in, or Place({ref!r}, edge=...) if it is a connector")
    for ref in refs:
        if not ref.startswith("J"):
            continue
        box = feet[ref].world_box()
        dist = min(box[0], box[1], w - box[2], h - box[3])
        if dist > EDGE_MM:
            issues.append(f"{ref} is {dist:.1f} mm from the nearest board edge: Place({ref!r}, edge=\"bottom\") puts a connector on one")
    return issues


def _placed_on_edge(ref: str, job) -> bool:
    return any(p.ref == ref and (p.edge or p.overhang) for p in job.places)


def _pin_name(inst, pad: str) -> str:
    for name, pin in inst.part.pins.items():
        if pad in pin.pads:
            return name
    return pad
