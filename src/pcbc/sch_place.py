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
_HAT_H = 4.8


def text_w(s: str) -> float:
    """Width of KiCad 1.27 mm text, roughly."""
    return max(len(s), 1) * 1.27 * 0.95 + 0.4


def hat_zone(part, pin, net: str, gnd: bool) -> tuple[float, float, float, float]:
    """Where this pin's power symbol will want to be: on the pin end when the
    pin already points the symbol's way, else 2.54 mm out along the pin.
    (A longer lane was tried and pushed connectors 17 mm away: when a symbol
    is boxed in, the report names the hanger to move instead.)"""
    ex, ey = pin_world(part, pin)
    ox, oy = pin_outward(part, pin)
    vertical = abs(oy) > abs(ox)
    natural = vertical and ((gnd and oy > 0) or (not gnd and oy < 0))
    hx, hy = (ex, ey) if natural else (ex + ox * 2.54, ey + oy * 2.54)
    hw = max(1.5, text_w(net) / 2.0)
    return (hx - hw, hy, hx + hw, hy + _HAT_H) if gnd else (hx - hw, hy - _HAT_H, hx + hw, hy)


def two_pin(part) -> bool:
    return bool(part.pins) and (getattr(part, "kind", "") in ("r", "c", "l", "d") or len(part.pins) <= 2)


def text_zone(part) -> tuple[float, float, float, float]:
    """Where a 2-pin part's Reference/Value will want to be: right of a vertical
    part, above a horizontal one."""
    x0, y0, x1, y1 = body_aabb(part)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    w = max(text_w(part.ref), text_w(getattr(part, "display", "") or ""))
    vertical = abs(pin_outward(part, part.pins[0])[1]) > 0.5
    if vertical:
        return (x1 + 0.8, cy - 1.8, x1 + 0.8 + w, cy + 1.8)
    return (cx - w / 2.0, y0 - 3.5, cx + w / 2.0, y0 - 0.2)


def keepout_boxes(part, kinds: dict[str, str] | None) -> list[tuple[float, float, float, float]]:
    """Body plus the room each power symbol will take, as separate boxes (one
    box around all of them would claim the space under the whole part). Parts
    are kept out of each other's boxes, so what is drawn later has somewhere
    to go. A pin that an attach already wires needs no symbol; text is soft
    and finds its own side."""
    boxes = [body_aabb(part)]
    if two_pin(part):
        # Half a name's lane on each side of a standing part (above and below
        # a lying one): two neighbours then leave a lane between them, and a
        # part still fits a slot the width of its body plus one lane.
        bx0, by0, bx1, by1 = boxes[0]
        if abs(pin_outward(part, part.pins[0])[1]) > 0.5:
            boxes.append((bx0 - 0.8, by0, bx1 + 0.8, by1))
        else:
            boxes.append((bx0, by0 - 0.8, bx1, by1 + 0.8))
    wired = getattr(part, "wired_pins", set())
    for pin in part.pins:
        if pin.number in wired:
            continue
        net = getattr(pin, "net", "") or ""
        kind = (kinds or {}).get(net, "")
        if kind in ("power", "ground"):
            boxes.append(hat_zone(part, pin, net, kind == "ground"))
    return boxes


def keepout_aabb(part, kinds: dict[str, str] | None) -> tuple[float, float, float, float]:
    boxes = keepout_boxes(part, kinds)
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


_keepout_memo: dict = {}


def _keepout(part, kinds) -> list[tuple[float, float, float, float]]:
    key = (id(part), part.x, part.y, part.rot, getattr(part, "mirror", None), len(getattr(part, "wired_pins", ())))
    boxes = _keepout_memo.get(key)
    if boxes is None:
        if len(_keepout_memo) > 4096:
            _keepout_memo.clear()
        boxes = _keepout_memo[key] = keepout_boxes(part, kinds)
    return boxes


def _clash(a, b, kinds, pad: float = 0.25) -> bool:
    """Any keepout box of one touching any of the other. Bodies may sit close -
    a cap right under its node - so the pad is small; the symbols each part
    will need are already among the boxes."""
    return any(_boxes_overlap(x, y, pad) for x in _keepout(a, kinds) for y in _keepout(b, kinds))


def _mark_wired(part, our, other, op) -> None:
    """An attach draws a wire between these two pins: neither needs a symbol."""
    for p, pin in ((part, our), (other, op)):
        wired = getattr(p, "wired_pins", None)
        if wired is None:
            wired = set()
            p.wired_pins = wired
        wired.add(pin.number)


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
    # KiCad connects on a 1.27 mm grid; library pins sit on it, so the origin must too.
    part.x, part.y = round(at[0] / 1.27) * 1.27, round(at[1] / 1.27) * 1.27
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
    _, tpin = parse_refpin(spec.along or "?.1")
    op = _find_pin(other, tpin)
    # side= is relative to the parent as drawn. "bottom" off the TOP pin of a
    # part that hangs from its row (a second cap on the same node) cannot
    # share that node from below, so it means beside it, away from whatever
    # the parent is attached to - and the sibling then hangs the same way.
    # "bottom" off the BOTTOM pin (the tap of a divider) stays below.
    parallel = False
    p_dir = getattr(other, "attach_dir", None)
    if side in ("top", "bottom") and other.pins and abs(pin_outward(other, other.pins[0])[1]) > 0.5:
        opx, opy = pin_world(other, op)
        ys = [pin_world(other, pn)[1] for pn in other.pins]
        far_end = (side == "bottom" and opy <= min(ys) + 0.05) or (side == "top" and opy >= max(ys) - 0.05)
        if far_end and p_dir and abs(p_dir[0]) > 0.5:
            side = "left" if p_dir[0] < 0 else "right"
            parallel = True
    sx, sy = _SIDES[side]
    ox0, oy0, ox1, oy1 = world_aabb(other)
    owx, owy = pin_world(other, op)
    two_pin = len(part.pins) <= 2
    if spec.rotate_set or spec.mirror:
        part.rot, part.mirror = float(spec.rot), spec.mirror
    elif parallel and two_pin:
        part.rot, part.mirror = other.rot, getattr(other, "mirror", None)
    elif spec.side and two_pin:
        # Turn to face the sibling (a cap under a wire node, a switch beside it).
        part.rot, part.mirror = _best_pose(_attach_pin(spec, part, other, op), (-sx, -sy), part)
    elif two_pin:
        part.rot, part.mirror = other.rot, getattr(other, "mirror", None)
    else:
        part.rot, part.mirror = 0.0, None
    our = _attach_pin(spec, part, other, op)
    _mark_wired(part, our, other, op)
    facing = bool(spec.side) and two_pin and not spec.rotate_set and not parallel
    blockers: list[str] = []
    node_named = (kinds or {}).get(getattr(our, "net", ""), "net") in ("power", "ground") or (
        op.number in getattr(other, "wired_pins", set()) and our.number in getattr(part, "wired_pins", set())
    )
    if spec.align:
        # Land our pin on that pin's row (stacking up/down) or column (sideways):
        # a straight wire can then join the two, no label or symbol needed.
        aref, apin = parse_refpin(spec.align)
        target = next((o for o in occupied if o.ref == aref), other if other.ref == aref else None)
        if target is None:
            raise ValueError(f"SchPlace({part.ref!r}): align={spec.align!r} names a part that is not placed yet")
        ax, ay = pin_world(target, _find_pin(target, apin))
        gap = abs(ay - owy) if sy else abs(ax - owx)
        if gap < 2.54:
            raise ValueError(f"SchPlace({part.ref!r}): align={spec.align!r} is on the same row as {other.ref}.{op.name}")
        facing = two_pin and not spec.rotate_set
    elif spec.gap is not None:
        gap = float(spec.gap)
    elif facing and node_named:
        # Hangs off a node that already carries the symbol or the label
        # (it is wired on to something else): nothing to fit on this wire.
        gap = 2.54
    else:
        gap = _auto_gap(part, our, other, kinds)
    plx, ply = lib_to_sheet(our.lx, our.ly, part.rot, part.mirror)
    for i in range(24):
        d = gap + i * 1.27
        if facing:
            # Turned toward the sibling's pin: gap is pin to pin, i.e. the wire.
            part.x = owx + sx * d - plx
            part.y = owy + sy * d - ply
            hits = [o.ref for o in occupied if _clash(part, o, kinds)]
            if i == 0:
                blockers = hits
            if not hits:
                if i >= 10:
                    part.shoved_mm = getattr(part, "shoved_mm", 0.0) + i * 1.27
                    part.shoved_by = blockers[0] if blockers else "?"
                part.attach_dir = (sx, sy)
                part.attach_wire = ((owx, owy), pin_world(part, our))
                part.placed = True
                return
            continue
        part.x, part.y = other.x, other.y
        a0, a1, a2, a3 = world_aabb(part)
        # Beside or below the parent's box, then the pin snapped outward onto
        # the 1.27 mm grid (the box edge is wherever the graphics end).
        if sy > 0:
            part.x = owx - plx
            part.y = math.ceil((oy1 + d + (part.y - a1) + ply) / 1.27) * 1.27 - ply
        elif sy < 0:
            part.x = owx - plx
            part.y = math.floor((oy0 - d - (a3 - part.y) + ply) / 1.27) * 1.27 - ply
        elif sx > 0:
            part.x = math.ceil((ox1 + d + (part.x - a0) + plx) / 1.27) * 1.27 - plx
            part.y = owy - ply
        else:
            part.x = math.floor((ox0 - d - (a2 - part.x) + plx) / 1.27) * 1.27 - plx
            part.y = owy - ply
        hits = [o.ref for o in occupied if _clash(part, o, kinds)]
        if i == 0:
            blockers = hits
        if not hits:
            if i >= 10:
                part.shoved_mm = getattr(part, "shoved_mm", 0.0) + i * 1.27
                part.shoved_by = blockers[0] if blockers else "?"
            part.attach_dir = (sx, sy)
            part.attach_wire = ((owx, owy), pin_world(part, our))
            part.placed = True
            return
    part.x, part.y = owx - plx, oy1 + gap + part.hh
    part.stuck_on = set(blockers)
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
    _mark_wired(part, our, other, op)
    gap = float(spec.gap) if spec.gap is not None else _auto_gap(part, our, other, kinds)

    # Poses to try at each distance, best first. A 2-pin part on a horizontal
    # pin lies along it (a series element) or hangs from the row: down when
    # its far end is ground, up when its far end is a supply - what an
    # engineer draws for a decoupling cap or a pull-up. An IC only mirrors.
    facing = _best_pose(our, face, part)
    if spec.rotate_set or spec.mirror:
        poses = [(float(spec.rot), spec.mirror)]
    elif part.kind in ("r", "c", "d") and abs(step[1]) < 0.5 and not face_park:
        far = next((getattr(pn, "net", "") for pn in part.pins if pn is not our), "")
        far_kind = (kinds or {}).get(far, "net")
        down = _best_pose(our, (0.0, -1.0), part)  # our pin on top, body below the row
        up = _best_pose(our, (0.0, 1.0), part)  # our pin underneath, body above
        # What an engineer draws: a cap to ground hangs down from the row, a
        # pull-up stands up to its supply; a signal part takes the free side -
        # up off the top pin of a side, down off the bottom one - and only
        # lies along the pin when neither is called for.
        same_side = [
            pin_world(other, pn)[1]
            for pn in other.pins
            if abs(pin_outward(other, pn)[0] - step[0]) < 0.5 and abs(pin_outward(other, pn)[1] - step[1]) < 0.5
        ]
        top = oy <= min(same_side) + 0.05
        bottom = oy >= max(same_side) - 0.05
        if far_kind == "ground":
            poses = [down, up, facing]
        elif far_kind == "power":
            poses = [up, down, facing]
        elif top and not bottom:
            poses = [up, facing, down]
        elif bottom and not top:
            poses = [down, facing, up]
        else:
            poses = [facing, down, up]
    elif part.kind in ("r", "c", "d") and abs(step[1]) >= 0.5 and not face_park:
        # Off a pin that points up or down (an op-amp's supply), the part lies
        # sideways from a short stub - the decoupling cap beside the pin - on
        # the side away from the target's other pins.
        right = _best_pose(our, (-1.0, 0.0), part)  # our pin points at the node, body to the right
        left = _best_pose(our, (1.0, 0.0), part)
        others_x = [pin_world(other, pn)[0] for pn in other.pins if pn is not op]
        prefer_right = not others_x or (sum(others_x) / len(others_x)) <= ox
        sideways = [right, left] if prefer_right else [left, right]
        far = next((getattr(pn, "net", "") for pn in part.pins if pn is not our), "")
        far_kind = (kinds or {}).get(far, "net")
        # A chain continues in line - an LED below a resistor on to ground, a
        # part above a pin on to its supply; anything else steps aside.
        in_line = (step[1] > 0 and far_kind == "ground") or (step[1] < 0 and far_kind == "power")
        poses = [facing] + sideways if in_line else sideways + [facing]
        if not in_line and (kinds or {}).get(getattr(our, "net", ""), "net") in ("power", "ground") and spec.gap is None:
            gap = min(gap, 5.08)  # a symbol, not a label, goes on this stub
    else:
        poses = [facing]
    # The first pose at the asked gap, then a stagger of a few grid steps,
    # then the other poses, then longer slides.
    _STAGGER = 10  # grid steps a part may step out before it changes pose or is reported (12.7 mm: a three-part staircase)
    tries = [(i, pose) for pose in poses[:1] for i in range(_STAGGER)]
    tries += [(i, pose) for pose in poses[1:] for i in range(_STAGGER)]
    tries += [(i, pose) for i in range(_STAGGER, 64) for pose in poses]

    bx0, by0, bx1, by1 = body_aabb(other)
    chosen = False
    blockers: list[str] = []
    wire_blockers: list[str] = []
    tx = ty = 0.0
    for i, (rot, mirror) in tries:
        dist = gap + i * 1.27
        if face_park:
            # Parked off a face: that edge is wherever the graphics end, so the
            # parked coordinate is snapped outward to the 1.27 mm grid (the
            # pin's own coordinate is already on it).
            if spec.side == "left":
                tx, ty = math.floor((bx0 - dist) / 1.27) * 1.27, oy
            elif spec.side == "right":
                tx, ty = math.ceil((bx1 + dist) / 1.27) * 1.27, oy
            elif spec.side == "top":
                tx, ty = ox, math.floor((by0 - dist) / 1.27) * 1.27
            else:
                tx, ty = ox, math.ceil((by1 + dist) / 1.27) * 1.27
        else:
            tx = ox + step[0] * dist
            ty = oy + step[1] * dist
        part.rot, part.mirror = rot, mirror
        plx, ply = lib_to_sheet(our.lx, our.ly, rot, mirror)
        part.x, part.y = tx - plx, ty - ply
        body_hits = [o.ref for o in occupied if _clash(part, o, kinds)]
        wire_hits = _wire_conflicts(part, ((ox, oy), (tx, ty)), occupied)
        if not blockers and not wire_blockers:
            blockers, wire_blockers = body_hits, wire_hits
        if not body_hits and not wire_hits:
            chosen = True
            if i >= _STAGGER:
                # Slid well past the asked gap to clear a neighbour: say so, a
                # long slide is a placement to change, not a fix.
                part.shoved_mm = getattr(part, "shoved_mm", 0.0) + i * 1.27
                part.shoved_by = (blockers or wire_blockers or ["?"])[0]
            break
    if not chosen:
        # Nowhere along this attach is clear: stay where it was asked, so the
        # collision is visible where it is, and say what is in the way.
        part.rot, part.mirror = poses[0]
        plx, ply = lib_to_sheet(our.lx, our.ly, part.rot, part.mirror)
        tx, ty = ox + step[0] * gap, oy + step[1] * gap
        part.x, part.y = tx - plx, ty - ply
        part.stuck_on = set(blockers)
        part.stuck_wire = (f"{other.ref}.{op.name}", sorted(set(wire_blockers)))
    part.attach_wire = ((ox, oy), (tx, ty))
    part.attach_dir = step
    part.attach_ref = other.ref
    part.placed = True


def _seg_hits_box(x0: float, y0: float, x1: float, y1: float, box, pad: float = 0.3) -> bool:
    bx0, by0, bx1, by1 = box
    return not (
        max(x0, x1) < bx0 - pad or min(x0, x1) > bx1 + pad or max(y0, y1) < by0 - pad or min(y0, y1) > by1 + pad
    )


def _wire_conflicts(part, wire, others: list) -> list[str]:
    """Placed hangers' attach wires through our graphics, or ours through
    theirs: a body across a neighbour's wire is as bad as a body on a body."""
    (ax, ay), (bx, by) = wire
    core = core_aabb(part)
    out: list[str] = []
    for o in others:
        w = getattr(o, "attach_wire", None)
        if w and _seg_hits_box(w[0][0], w[0][1], w[1][0], w[1][1], core):
            out.append(o.ref)
            continue
        if _seg_hits_box(ax, ay, bx, by, core_aabb(o)):
            out.append(o.ref)
    return out


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

    def _rigidity(spec: SchPlaceSpec) -> int:
        part = by_ref[spec.ref]
        return 0 if part.kind not in ("r", "c", "d") else 1

    # Parts with one pose (an IC, an inductor, a connector) go first, so a
    # part that can hang from its row is the one that yields.
    pending = sorted((s for s in design.sch_places if s.has_attach()), key=_rigidity)
    guard = 0
    while pending:
        guard += 1
        if guard > 64:
            raise ValueError("SchPlace pin-attach cycle: " + ", ".join(s.ref for s in pending))
        nxt: list[SchPlaceSpec] = []
        for spec in pending:
            target = spec.to or spec.along or ""
            tref, tpin = parse_refpin(target)
            aref = parse_refpin(spec.align)[0] if spec.align else None
            if tref not in placed or (aref and aref not in placed and aref in by_ref):
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
    _separate(parts, {s.ref for s in design.sch_places if not s.has_attach()}, kinds)


def _axis(part, other) -> tuple[float, float]:
    dx, dy = getattr(part, "attach_dir", None) or (0.0, 0.0)
    if abs(dx) < 0.1 and abs(dy) < 0.1:
        dx, dy = part.x - other.x, part.y - other.y
        if abs(dx) >= abs(dy):
            return (1.0 if dx >= 0 else -1.0), 0.0
        return 0.0, (1.0 if dy >= 0 else -1.0)
    return dx, dy


def _steps_to_clear(mover, hold, others: list, kinds, limit: int = 24) -> int | None:
    """How many 1.27 mm steps along its attach axis until mover's keepout
    clears hold's and everyone else's. None if that never happens within the
    limit - then it stays put and the report says so."""
    dx, dy = _axis(mover, hold)
    x0, y0 = mover.x, mover.y
    try:
        for k in range(1, limit + 1):
            mover.x, mover.y = x0 + 1.27 * dx * k, y0 + 1.27 * dy * k
            if not any(_clash(mover, o, kinds) for o in others if o is not mover):
                return k
        return None
    finally:
        mover.x, mover.y = x0, y0


def _separate(parts: list, css_refs: set[str], kinds: dict[str, str] | None = None) -> None:
    """Push attached parts apart until their keepouts (body plus the power
    symbols they will need) no longer overlap. Of the two, the one that clears
    everything with the smaller shove along its own attach axis moves, so pin
    alignment survives and the wire only gets longer. A part that cannot clear
    within 30 mm is left where it is rather than marched across the sheet."""
    for _ in range(48):
        moved = False
        for i, a in enumerate(parts):
            for b in parts[i + 1 :]:
                if not _clash(a, b, kinds):
                    continue
                options = []
                for mover, hold in ((a, b), (b, a)):
                    if mover.ref in css_refs:
                        continue
                    k = _steps_to_clear(mover, hold, parts, kinds)
                    if k is not None:
                        options.append((k, mover, hold))
                if not options:
                    # Neither can clear along its attach: left as is, reported.
                    for m in (a, b):
                        if m.ref not in css_refs:
                            stuck = getattr(m, "stuck_on", set())
                            stuck.add(b.ref if m is a else a.ref)
                            m.stuck_on = stuck
                    continue
                k, mover, hold = min(options, key=lambda t: t[0])
                dx, dy = _axis(mover, hold)
                mover.x += 1.27 * dx * k
                mover.y += 1.27 * dy * k
                # Remember how far and because of whom: a long shove is a
                # placement the board file should change, not a fix.
                mover.shoved_mm = getattr(mover, "shoved_mm", 0.0) + 1.27 * k
                mover.shoved_by = hold.ref
                mover.stuck_on = set()
                mover.stuck_wire = None
                moved = True
        if not moved:
            return
