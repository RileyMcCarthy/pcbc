"""Resolve SchPlace() into symbol (x, y, rot). No packer — missing SchPlace is a check error."""

from __future__ import annotations

import math

from .css import Rect, origin_from_border, resolve_rect
from .layout import content_rect, resolve_regions, style_from
from .model import BoardSpec, Design, SchPlaceSpec

# Schematic sheet in mm (A3). Independent of Board() PCB size.
SHEET = BoardSpec(size_mm=(420.0, 297.0), padding=(12.7, 12.7, 12.7, 12.7))


def parse_refpin(spec: str) -> tuple[str, str]:
    ref, sep, pin = spec.partition(".")
    if not sep or not pin:
        raise ValueError(f"expected 'Ref.PIN', got {spec!r}")
    return ref, pin


def _find_pin(part, name: str):
    for p in part.pins:
        if p.name == name or p.number == name:
            return p
    raise ValueError(
        f"{part.ref}: no pin {name!r} (have "
        f"{', '.join(sorted({p.name for p in part.pins}))})"
    )


def _attach_pin(spec: SchPlaceSpec, part, other, other_pin, face: tuple[float, float] | None = None):
    """Which of our pins goes next to the target pin: the one on the same net,
    and of several (an ESD array's pass-through pair) the one facing the target."""
    if spec.pin:
        return _find_pin(part, spec.pin)
    net = getattr(other_pin, "net", "")
    shared = [p for p in part.pins if net and getattr(p, "net", "") == net]
    if shared:
        if face and len(shared) > 1 and part.kind == "box":
            def facing(p) -> float:
                ox, oy = pin_outward(part, p)
                return ox * face[0] + oy * face[1]

            return max(shared, key=facing)
        return shared[0]
    if spec.along:
        try:
            return _find_pin(part, other_pin.number)
        except ValueError:
            return part.pins[0]
    have = ", ".join(f"{p.name}={p.net}" for p in part.pins if getattr(p, "net", ""))
    raise ValueError(
        f"SchPlace({part.ref!r}): no pin of {part.ref} is on net {net!r} like "
        f"{other.ref}.{other_pin.name} (have {have}). Pass pin= to hang an "
        f"unconnected pin there on purpose."
    )


def _rot_xy(lx: float, ly: float, rot: float) -> tuple[float, float]:
    r = rot % 360.0
    if abs(r - 180) < 1:
        return -lx, -ly
    if abs(r - 90) < 1:
        return -ly, lx
    if abs(r - 270) < 1:
        return ly, -lx
    return lx, ly


def lib_to_sheet(lx: float, ly: float, rot: float, mirror: str | None = None) -> tuple[float, float]:
    """Library offset (Y up) → sheet offset (Y down) for a symbol placed at
    ``rot`` with an optional ``(mirror x|y)``.

    KiCad rotates in the library frame, mirrors Y onto the sheet, then applies
    the instance mirror in sheet coordinates: ``x`` flips top/bottom, ``y``
    flips left/right (checked against kicad-cli for every combination).
    """
    rx, ry = _rot_xy(lx, ly, rot)
    dx, dy = rx, -ry
    if mirror == "x":
        dy = -dy
    elif mirror == "y":
        dx = -dx
    return dx, dy


def pin_world(part, pin) -> tuple[float, float]:
    """Electrical end of a pin in sheet coordinates."""
    dx, dy = lib_to_sheet(pin.lx, pin.ly, part.rot, getattr(part, "mirror", None))
    return part.x + dx, part.y + dy


def pin_outward(part, pin) -> tuple[float, float]:
    """Unit vector (sheet frame) from the pin end away from the body."""
    rad = math.radians(pin.rot)  # pin rot points toward the body
    ox, oy = lib_to_sheet(-math.cos(rad), -math.sin(rad), part.rot, getattr(part, "mirror", None))
    n = math.hypot(ox, oy) or 1.0
    return ox / n, oy / n


# Poses in order of preference: as drawn, mirrored (text stays readable),
# then turned. A 2-pin part may take any; a bigger symbol only mirrors.
_POSES_2PIN = [(0.0, None), (0.0, "y"), (0.0, "x"), (180.0, None), (90.0, None), (270.0, None), (90.0, "y"), (270.0, "y")]
_POSES_BOX = [(0.0, None), (0.0, "y"), (0.0, "x")]


def _best_pose(pin, want: tuple[float, float], part) -> tuple[float, str | None]:
    """Rotation and mirror whose outward direction for ``pin`` best matches
    ``want`` (sheet frame). Ties keep the earlier, plainer pose."""
    poses = _POSES_2PIN if len(part.pins) <= 2 else _POSES_BOX
    best, best_dot = poses[0], -9.0
    rad = math.radians(pin.rot)
    for r, m in poses:
        ox, oy = lib_to_sheet(-math.cos(rad), -math.sin(rad), r, m)
        n = math.hypot(ox, oy) or 1.0
        d = (ox / n) * want[0] + (oy / n) * want[1]
        if d > best_dot + 1e-9:
            best, best_dot = (r, m), d
    return best


_SIDES = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "top": (0.0, -1.0),
    "bottom": (0.0, 1.0),
}

# Full-box clearance (includes Reference/Value). Body-only attach can be tighter.
_PAD = 5.08
_BODY_PAD = 2.54


def _local_box(part) -> tuple[float, float, float, float]:
    return getattr(part, "bbox", (-part.hw, -part.hh, part.hw, part.hh))


def _apply_css(spec: SchPlaceSpec, part, regions: dict[str, Rect]) -> None:
    st = style_from(spec)
    if spec.parent:
        if spec.parent not in regions:
            raise ValueError(f"SchPlace({part.ref!r}): unknown SchRegion {spec.parent!r}")
        cb = regions[spec.parent]
    else:
        cb = content_rect(SHEET)
    local = _local_box(part)
    rect = resolve_rect(
        cb,
        st,
        intrinsic_w=local[2] - local[0],
        intrinsic_h=local[3] - local[1],
        who=f"SchPlace({part.ref!r})",
    )
    at = origin_from_border(rect, local, st.rotate)
    part.x, part.y = at
    part.rot = float(st.rotate)
    part.mirror = spec.mirror
    part.placed = True


def _aabb_of(part, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    xs, ys = [], []
    for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        dx, dy = lib_to_sheet(cx, cy, part.rot, getattr(part, "mirror", None))
        xs.append(part.x + dx)
        ys.append(part.y + dy)
    return min(xs), min(ys), max(xs), max(ys)


def world_aabb(part) -> tuple[float, float, float, float]:
    """Graphics + pins + visible Reference/Value in sheet coordinates."""
    return _aabb_of(part, getattr(part, "bbox", (-part.hw, -part.hh, part.hw, part.hh)))


def body_aabb(part) -> tuple[float, float, float, float]:
    """Graphics + pins + pin text, not Reference/Value. Used for pin-attach."""
    box = getattr(part, "body_bbox", None) or getattr(
        part, "bbox", (-part.hw, -part.hh, part.hw, part.hh)
    )
    return _aabb_of(part, box)


def core_aabb(part) -> tuple[float, float, float, float]:
    """Graphics only (no pins, no text). Wires must not cross this."""
    box = getattr(part, "core_bbox", None) or getattr(part, "body_bbox", None) or (
        -part.hw,
        -part.hh,
        part.hw,
        part.hh,
    )
    return _aabb_of(part, box)


_GRID = 1.27


def _auto_gap(part, our, other, kinds: dict[str, str] | None) -> float:
    """Pin-to-pin distance when the board file gives none.

    Off an IC pin: 10.16 mm, room for a label on the wire and for whatever
    else hangs off that node (a second cap, its symbol, its text). Off another
    2-pin part: just enough wire for the net's label, or the grid minimum for
    a power net (its symbol goes on the far pin instead).
    """
    net = getattr(our, "net", "") or ""
    power = (kinds or {}).get(net, "net") in ("power", "ground")
    need = 2.54 if power else max(len(net), 1) * 1.27 * 0.95 + 2.54
    need = max(5.08, math.ceil(need / _GRID) * _GRID)
    if getattr(other, "kind", "") == "box" or part.kind not in ("r", "c", "l", "d"):
        return max(10.16, need)
    return need


def _apply_along(spec: SchPlaceSpec, part, other, occupied: list, kinds=None) -> None:
    side = spec.side if spec.side in _SIDES else "bottom"
    sx, sy = _SIDES[side]
    ox0, oy0, ox1, oy1 = world_aabb(other)
    _, tpin = parse_refpin(spec.along or "?.1")
    op = _find_pin(other, tpin)
    owx, owy = pin_world(other, op)
    two_pin = len(part.pins) <= 2
    if spec.rotate_set or spec.mirror:
        part.rot, part.mirror = float(spec.rot), spec.mirror
    elif spec.side and two_pin:
        # Turn to face the sibling (a cap under a wire node, a switch beside it).
        part.rot, part.mirror = _best_pose(_attach_pin(spec, part, other, op), (-sx, -sy), part)
    elif two_pin:
        part.rot, part.mirror = other.rot, getattr(other, "mirror", None)
    else:
        part.rot, part.mirror = 0.0, None
    our = _attach_pin(spec, part, other, op)
    gap = float(spec.gap) if spec.gap is not None else _auto_gap(part, our, other, kinds)
    plx, ply = lib_to_sheet(our.lx, our.ly, part.rot, part.mirror)
    facing = bool(spec.side) and two_pin and not spec.rotate_set
    for i in range(24):
        d = gap + i * 1.27
        if facing:
            # Turned toward the sibling's pin: gap is pin to pin, i.e. the wire.
            part.x = owx + sx * d - plx
            part.y = owy + sy * d - ply
            if not any(_overlap_body(part, o, pad=1.0) for o in occupied):
                part.attach_dir = (sx, sy)
                part.placed = True
                return
            continue
        part.x, part.y = other.x, other.y
        a0, a1, a2, a3 = world_aabb(part)
        if sy > 0:
            part.x = owx - plx
            part.y = oy1 + d + (part.y - a1)
        elif sy < 0:
            part.x = owx - plx
            part.y = oy0 - d - (a3 - part.y)
        elif sx > 0:
            part.x = ox1 + d + (part.x - a0)
            part.y = owy - ply
        else:
            part.x = ox0 - d - (a2 - part.x)
            part.y = owy - ply
        if not any(_overlap(part, o) for o in occupied):
            part.attach_dir = (sx, sy)
            part.placed = True
            return
    part.x, part.y = owx - plx, oy1 + gap + part.hh
    part.attach_dir = (sx, sy)
    part.placed = True


def _apply_attach(spec: SchPlaceSpec, part, other, other_pin_name: str, occupied: list, kinds=None) -> None:
    if spec.along:
        _apply_along(spec, part, other, occupied, kinds)
        return
    op = _find_pin(other, other_pin_name)
    ox, oy = pin_world(other, op)

    if spec.side and spec.side in _SIDES:
        step = _SIDES[spec.side]
        face_park = True
    else:
        step = pin_outward(other, op)
        face_park = False
    face = (-step[0], -step[1])
    part.rot, part.mirror = 0.0, None
    our = _attach_pin(spec, part, other, op, face)
    gap = float(spec.gap) if spec.gap is not None else _auto_gap(part, our, other, kinds)

    if spec.rotate_set or spec.mirror:
        part.rot, part.mirror = float(spec.rot), spec.mirror
    else:
        # A 2-pin part turns or mirrors to face the target; an IC only mirrors.
        part.rot, part.mirror = _best_pose(our, face, part)

    plx, ply = lib_to_sheet(our.lx, our.ly, part.rot, part.mirror)
    bx0, by0, bx1, by1 = body_aabb(other)
    chosen = False
    for i in range(64):
        dist = gap + i * 1.27
        if face_park:
            if spec.side == "left":
                tx, ty = bx0 - dist, oy
            elif spec.side == "right":
                tx, ty = bx1 + dist, oy
            elif spec.side == "top":
                tx, ty = ox, by0 - dist
            else:
                tx, ty = ox, by1 + dist
        else:
            tx = ox + step[0] * dist
            ty = oy + step[1] * dist
        part.x, part.y = tx - plx, ty - ply
        if not any(_overlap_body(part, o) for o in occupied):
            chosen = True
            break
    if not chosen:
        part.x = ox + step[0] * 50.8 - plx
        part.y = oy + step[1] * 50.8 - ply
    part.attach_dir = step
    part.placed = True


def _boxes_overlap(a, b, pad: float) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (
        ax1 + pad < bx0
        or bx1 + pad < ax0
        or ay1 + pad < by0
        or by1 + pad < ay0
    )


def _overlap(a, b, pad: float = _PAD) -> bool:
    return _boxes_overlap(world_aabb(a), world_aabb(b), pad)


def _overlap_body(a, b, pad: float = _BODY_PAD) -> bool:
    return _boxes_overlap(body_aabb(a), body_aabb(b), pad)


def apply_sch_places(design: Design, parts: list) -> None:
    """Set x/y/rot on schematic parts from design.sch_places."""
    by_ref = {p.ref: p for p in parts}
    specs = {s.ref: s for s in design.sch_places}
    missing = [p.ref for p in parts if p.ref not in specs]
    if missing:
        raise ValueError("no SchPlace() for " + ", ".join(missing))
    extra = [s.ref for s in design.sch_places if s.ref not in by_ref]
    if extra:
        raise ValueError("SchPlace() for unknown ref " + ", ".join(extra))

    regions = resolve_regions(SHEET, design.sch_regions)
    kinds = {n.name: n.kind for n in design.nets.values()}
    placed: set[str] = set()

    for spec in design.sch_places:
        part = by_ref[spec.ref]
        if spec.has_attach():
            continue
        if not spec.has_css():
            raise ValueError(
                f"SchPlace({spec.ref!r}) needs CSS left/top/... or pin= and to="
            )
        _apply_css(spec, part, regions)
        placed.add(spec.ref)

    pending = [s for s in design.sch_places if s.has_attach()]
    guard = 0
    while pending:
        guard += 1
        if guard > 64:
            raise ValueError("SchPlace pin-attach cycle: " + ", ".join(s.ref for s in pending))
        nxt: list[SchPlaceSpec] = []
        for spec in pending:
            target = spec.to or spec.along or ""
            tref, tpin = parse_refpin(target)
            if tref not in placed:
                nxt.append(spec)
                continue
            if tref not in by_ref:
                raise ValueError(f"SchPlace({spec.ref!r}): {tref} is not on the sheet")
            _apply_attach(
                spec,
                by_ref[spec.ref],
                by_ref[tref],
                tpin,
                [by_ref[r] for r in placed],
                kinds,
            )
            placed.add(spec.ref)
        if len(nxt) == len(pending) and nxt:
            raise ValueError(
                "SchPlace pin-attach waiting on unplaced "
                + ", ".join((s.to or s.along or s.ref) for s in nxt)
            )
        pending = nxt
    _separate(parts, {s.ref for s in design.sch_places if not s.has_attach()})


def _separate(parts: list, css_refs: set[str]) -> None:
    """Nudge attached parts whose graphics or pins overlap another symbol.

    Moves go along the part's own attach axis, so the pin alignment that
    to=/along= just made survives (the wire only gets longer). Text is placed
    later around whatever is here, so it does not count as overlap.
    """
    for _ in range(48):
        moved = False
        for i, a in enumerate(parts):
            for b in parts[i + 1 :]:
                if not _overlap_body(a, b, pad=1.0):
                    continue
                mover, hold = (b, a) if a.ref in css_refs and b.ref not in css_refs else (a, b)
                if mover.ref in css_refs and hold.ref in css_refs:
                    continue
                dx, dy = getattr(mover, "attach_dir", None) or (0.0, 0.0)
                if abs(dx) < 0.1 and abs(dy) < 0.1:
                    dx, dy = mover.x - hold.x, mover.y - hold.y
                    if abs(dx) >= abs(dy):
                        dx, dy = (1.0 if dx >= 0 else -1.0), 0.0
                    else:
                        dx, dy = 0.0, (1.0 if dy >= 0 else -1.0)
                mover.x += 1.27 * dx
                mover.y += 1.27 * dy
                moved = True
        if not moved:
            return
