"""Escapes for closed pad rows, written by pcbc before any router runs.

A pad nothing can pass between (`Foot.closed`) can only leave straight out. Every such pad on an
unconstrained net gets a stub straight out and a via in the fanout lane placement kept free
(`Foot.lane`), neighbours staggered so their holes keep the fab's hole-to-hole; the copper is
locked, so the lane is spent on escapes and no router runs a track along it. Constrained nets
(no vias, one layer) and differential pairs keep their pads bare and leave between the vias or
under the body. KRT's own `qfn_fanout.py` staggered by copper clearance alone (a 0.65 mm row:
holes 0.42 mm apart, JLC wants 0.5) and, when its second row did not fit the lane, hunted past
the decoupling caps with 3.5 mm stubs that grazed their pads. Here the geometry is the stackup's
numbers and nothing else, and the same board gives the same copper.
"""

from __future__ import annotations

import math
import re
from fnmatch import fnmatch

from .compile import CompiledJob
from .copper import _rotate
from .layout import footprints_by_ref
from .model import Design
from .pcb_place import _EDGE_OUT, _pins_of, lane_rules, parse_foot
from .sexp import footprint_at, stable_uuid
from .stackup import fanout_stagger

_LAYER = re.compile(r'\n\t\t\(layer "([^"]+)"\)')


def krt_grid(job: CompiledJob) -> float:
    """KRT's routing grid for this board; the plan passes the same number as `--grid-step`."""
    return 0.05 if job.layers <= 2 else 0.1


def _excluded(design: Design, job: CompiledJob) -> set[str]:
    """Nets that keep their pads bare: no vias, one layer, or routed as a pair."""
    out: set[str] = set()
    for cn in job.nets:
        if cn.autoroute == "diff_pair" or cn.vias is False or len(cn.layers) == 1:
            out.update(n for n in design.nets if any(fnmatch(n, p) for p in cn.patterns))
    return out


def _width(job: CompiledJob, net: str, fallback: float) -> float:
    cn = next((c for c in job.nets if any(fnmatch(net, p) for p in c.patterns)), None)
    cls = next((c for c in job.classes if cn is not None and c.name == cn.class_name), None)
    return cls.track_width_mm if cls else fallback


def _snap_out(v: float, sign: float, grid: float) -> float:
    """On the router's grid, rounded away from the pad so no clearance shrinks."""
    q = v / grid
    return round((math.ceil(q - 1e-9) if sign > 0 else math.floor(q + 1e-9)) * grid, 4)


def _segment(x1: float, y1: float, x2: float, y2: float, w: float, layer: str, net: str, uid: str) -> str:
    return (
        f"\n\t(segment\n\t\t(start {x1:.6f} {y1:.6f})\n\t\t(end {x2:.6f} {y2:.6f})\n\t\t(width {w:g})\n\t\t(locked yes)\n"
        f'\t\t(layer "{layer}")\n\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n'
    )


def _via(x: float, y: float, size: float, drill: float, net: str, uid: str) -> str:
    return (
        f"\n\t(via\n\t\t(at {x:.6f} {y:.6f})\n\t\t(size {size:g})\n\t\t(drill {drill:g})\n"
        f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(locked yes)\n\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n'
    )


def fanout_copper(design: Design, job: CompiledJob, text: str, board: str = "board") -> tuple[str, list[dict]]:
    """The placed board with an escape stub and via on every closed-row pad of an unconstrained
    net, locked. Returns (text, one note per via: ref, pad, net, via)."""
    stack, clearance = lane_rules(job)
    grid = krt_grid(job)
    nets_of = _pins_of(design)
    count: dict[str, int] = {}
    for pins in nets_of.values():
        for net in pins.values():
            count[net] = count.get(net, 0) + 1
    excluded = _excluded(design, job)
    default = next((c.track_width_mm for c in job.classes if c.name == "Default"), stack.track_min)
    items: list[str] = []
    notes: list[dict] = []
    for ref, block in sorted(footprints_by_ref(text).items()):
        at = footprint_at(block)
        if at is None:
            continue
        foot = parse_foot(ref, block)
        foot.at, foot.rot = (at[0], at[1]), at[2]
        for pad in foot.pads:
            pad.net = nets_of.get(ref, {}).get(pad.num, pad.net)
        foot.lane(stack, clearance)
        if not foot.closed:
            continue
        lm = _LAYER.search(block)
        layer = lm.group(1) if lm else "F.Cu"
        for side in sorted(set(foot.escape.values())):
            row = [p for p in foot.pads if foot.escape.get(p.num) == side]
            along = (lambda p: p.x) if side in ("top", "bottom") else (lambda p: p.y)
            row.sort(key=along)
            pitch = min((along(b) - along(a) for a, b in zip(row, row[1:])), default=0.0)
            stagger = fanout_stagger(stack, clearance, pitch) if pitch else 0.0
            nx, ny = _EDGE_OUT[side]
            wx, wy = _rotate(nx, ny, foot.rot)  # the escape direction on the board
            for i, p in enumerate(row):  # i over the whole row: neighbours alternate even with bare pads between
                net = p.net
                if not net or count.get(net, 0) < 2 or net in excluded:
                    continue
                half_out = p.h / 2.0 if side in ("top", "bottom") else p.w / 2.0
                across = p.w if side in ("top", "bottom") else p.h
                d = half_out + stack.clearance_min + stack.via_diameter / 2.0
                dx, dy = _rotate(p.x + nx * d, p.y + ny * d, foot.rot)
                vx, vy = foot.at[0] + dx, foot.at[1] + dy
                px, py = foot.pad_world(p)
                # On the router's grid: the first via line snapped away from the pads, the second
                # line snapped away from the first (snapping each on its own shrank the stagger by
                # a grid step and left holes 0.49 mm apart), the across coordinate to the nearest.
                # Along the escape, snap away from the pad so the distance only grows. ACROSS the
                # escape, keep the pad's own coordinate: snapping it to the router's grid moved the
                # via up to half a grid step sideways and tilted every stub by up to a degree
                # (measured: 14 stubs off 0/45/90 across c3_usb, node and ds2). Pattern copper does
                # not need to sit on KRT's grid; it is locked, so KRT reads it as an obstacle and
                # never has to land on it.
                if abs(wx) > 0.5:
                    vx = _snap_out(vx, wx, grid)
                    if i % 2:
                        vx = _snap_out(vx + wx * stagger, wx, grid)
                    vy = round(py, 4)
                else:
                    vy = _snap_out(vy, wy, grid)
                    if i % 2:
                        vy = _snap_out(vy + wy * stagger, wy, grid)
                    vx = round(px, 4)
                width = max(min(_width(job, net, default), across), stack.track_min)
                items.append(_segment(px, py, vx, vy, width, layer, net, stable_uuid(board, "fanout", ref, p.num, "stub")))
                items.append(_via(vx, vy, stack.via_diameter, stack.via_drill, net, stable_uuid(board, "fanout", ref, p.num, "via")))
                notes.append({"ref": ref, "pad": p.num, "net": net, "via": (vx, vy)})
    if not items:
        return text, []
    body = text.rstrip()
    if not body.endswith(")"):
        raise ValueError("not a board file")
    return body[:-1].rstrip() + "\n" + "".join(items) + ")\n", notes
