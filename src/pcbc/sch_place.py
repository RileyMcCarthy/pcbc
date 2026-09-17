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


def _rot_xy(lx: float, ly: float, rot: float) -> tuple[float, float]:
    r = rot % 360.0
    if abs(r - 180) < 1:
        return -lx, -ly
    if abs(r - 90) < 1:
        return -ly, lx
    if abs(r - 270) < 1:
        return ly, -lx
    return lx, ly


def pin_world(part, pin) -> tuple[float, float]:
    lx, ly = _rot_xy(pin.lx, pin.ly, part.rot)
    return part.x + lx, part.y + ly


def _stub_delta(rot: float, length: float) -> tuple[float, float]:
    rad = math.radians(rot)
    return (-length * math.cos(rad), -length * math.sin(rad))


def _outward(part, pin) -> tuple[float, float]:
    dx, dy = _stub_delta(pin.rot + part.rot, 1.0)
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def _best_rot(pin, want: tuple[float, float]) -> float:
    best, best_dot = 0.0, -9.0
    for r in (0.0, 90.0, 180.0, 270.0):
        ox, oy = _rot_xy(*_stub_delta(pin.rot, 1.0), r)
        n = math.hypot(ox, oy) or 1.0
        d = (ox / n) * want[0] + (oy / n) * want[1]
        if d > best_dot:
            best, best_dot = r, d
    return best


def _apply_css(spec: SchPlaceSpec, part, regions: dict[str, Rect]) -> None:
    st = style_from(spec)
    if spec.parent:
        if spec.parent not in regions:
            raise ValueError(f"SchPlace({part.ref!r}): unknown SchRegion {spec.parent!r}")
        cb = regions[spec.parent]
    else:
        cb = content_rect(SHEET)
    local = (-part.hw, -part.hh, part.hw, part.hh)
    rect = resolve_rect(
        cb,
        st,
        intrinsic_w=part.hw * 2,
        intrinsic_h=part.hh * 2,
        who=f"SchPlace({part.ref!r})",
    )
    at = origin_from_border(rect, local, st.rotate)
    part.x, part.y = at
    part.rot = float(st.rotate)
    part.placed = True


def _body(part) -> tuple[float, float, float, float]:
    return (
        part.x - part.hw,
        part.y - part.hh,
        part.x + part.hw,
        part.y + part.hh,
    )


def _apply_along(spec: SchPlaceSpec, part, other, occupied: list) -> None:
    gap = float(spec.gap)
    if spec.rotate_set:
        part.rot = float(spec.rot)
    elif part.kind in ("r", "c", "l", "d"):
        part.rot = other.rot
    else:
        part.rot = 0.0
    step_x = other.hw + part.hw + gap
    step_y = other.hh + part.hh + gap
    for dx, dy in ((0.0, 1.0), (0.0, -1.0), (1.0, 0.0), (-1.0, 0.0)):
        part.x = other.x + dx * step_x
        part.y = other.y + dy * step_y
        if not any(_overlap(part, o) for o in occupied):
            part.placed = True
            return
    part.x = other.x
    part.y = other.y + step_y
    part.placed = True


def _apply_attach(spec: SchPlaceSpec, part, other, other_pin_name: str, occupied: list) -> None:
    if spec.along:
        _apply_along(spec, part, other, occupied)
        return
    op = _find_pin(other, other_pin_name)
    ox, oy = pin_world(other, op)
    gap = float(spec.gap)
    our_name = spec.pin or ("1" if any(p.number == "1" for p in part.pins) else part.pins[0].number)
    our = _find_pin(part, our_name)

    lx, ly = _rot_xy(op.lx, op.ly, other.rot)
    bx0, by0, bx1, by1 = _body(other)
    if abs(lx) >= abs(ly):
        if lx < 0:
            base_x, base_y, face, step = bx0 - gap, oy, (1.0, 0.0), (-1.0, 0.0)
        else:
            base_x, base_y, face, step = bx1 + gap, oy, (-1.0, 0.0), (1.0, 0.0)
    else:
        if ly < 0:
            base_x, base_y, face, step = ox, by0 - gap, (0.0, 1.0), (0.0, -1.0)
        else:
            base_x, base_y, face, step = ox, by1 + gap, (0.0, -1.0), (0.0, 1.0)

    if spec.rotate_set:
        part.rot = float(spec.rot)
    elif part.kind in ("r", "c", "l", "d"):
        part.rot = _best_rot(our, face)

    chosen = False
    for i in range(24):
        tx = base_x + step[0] * i * 1.27
        ty = base_y + step[1] * i * 1.27
        plx, ply = _rot_xy(our.lx, our.ly, part.rot)
        part.x, part.y = tx - plx, ty - ply
        if not any(_overlap(part, o) for o in occupied):
            chosen = True
            break
    if not chosen:
        plx, ply = _rot_xy(our.lx, our.ly, part.rot)
        part.x = base_x + step[0] * 12.7 - plx
        part.y = base_y + step[1] * 12.7 - ply
    part.placed = True


def _reach(p) -> tuple[float, float]:
    extra = 3.81 if p.kind == "box" else 0.0
    return p.hw + extra, p.hh + extra


def _overlap(a, b, pad: float = 1.27) -> bool:
    aw, ah = _reach(a)
    bw, bh = _reach(b)
    return not (
        a.x + aw + pad < b.x - bw
        or b.x + bw + pad < a.x - aw
        or a.y + ah + pad < b.y - bh
        or b.y + bh + pad < a.y - ah
    )


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
            )
            placed.add(spec.ref)
        if len(nxt) == len(pending) and nxt:
            raise ValueError(
                "SchPlace pin-attach waiting on unplaced "
                + ", ".join((s.to or s.along or s.ref) for s in nxt)
            )
        pending = nxt
