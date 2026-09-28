"""Escapes for closed pad rows, written by pcbc before any router runs.

A pad nothing can pass between (`Foot.closed`) can only leave straight out. Every such pad on an
unconstrained net gets a stub straight out and a via in the fanout lane placement kept free
(`Foot.lane`), neighbours staggered so their holes keep the fab's hole-to-hole; the copper is
locked, so the lane is spent on escapes and no router runs a track along it. Constrained nets
(no vias, one layer) and differential pairs keep their pads bare and leave between the vias or
under the body. The stagger is the stackup's hole-to-hole, not copper clearance alone (a 0.65 mm
row staggered by clearance puts holes 0.42 mm apart, and JLC wants 0.5). The geometry is the
stackup's numbers and nothing else, and the same board gives the same copper.
"""

from __future__ import annotations

import math
from fnmatch import fnmatch

from .compile import CompiledJob
from .copper import _rotate
from .model import Design
from .pcb_place import _pins_of, _EDGE_OUT
from .route_emit import Piece, seg_piece, via_piece
from .route_scene import Scene
from .sexp import stable_uuid
from .stackup import fanout_stagger



def _excluded(design: Design, job: CompiledJob, claimed: frozenset[str] = frozenset()) -> set[str]:
    """Nets that keep their pads bare: no vias, one layer, routed as a pair, or already carried by a
    pattern. The last is C.1's order: hops run before the fanout, and a closed row's lane is better
    spent on the hop that needed it than on a via the hop then has to start from."""
    out: set[str] = set(claimed)
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


def fanout_pieces(design: Design, job: CompiledJob, board: str, scene: Scene, claimed: frozenset[str] = frozenset()) -> tuple[list[Piece], list[dict]]:
    """The escapes as `Piece`s: one stub and one via per pad of a closed row whose net has another pad.

    The footprints are read **through the scene** (`route_scene.build_scene`, each part's `.kicad_mod`
    posed by its `Pose`), so the `Foot`s the escapes are built from — the closed rows, the escape
    sides, the lane widths, the side the part is on — are the same objects the router is judged
    against, and there is no second reading of a footprint that could disagree with the first.
    """
    stack = scene.stack
    grid = scene.grid
    nets_of = _pins_of(design)
    count: dict[str, int] = {}
    for pins in nets_of.values():
        for net in pins.values():
            count[net] = count.get(net, 0) + 1
    excluded = _excluded(design, job, claimed)
    default = next((c.track_width_mm for c in job.classes if c.name == "Default"), stack.track_min)
    pieces: list[Piece] = []
    notes: list[dict] = []
    for ref, foot in sorted(scene.feet.items()):
        if not foot.closed:
            continue
        if any(pl.ref == ref and (pl.edge or pl.overhang) for pl in job.places):
            # A part standing on a board edge is the board's entry: the parts it feeds are placed
            # around it and its pads already face inward, so an escape via buys nothing and costs
            # room in front of it. This is a policy, not a geometric truth, and it is the measured
            # one: reading the USB-C's shield pads truthfully (they declare 5 um and draw 0.7 x
            # 1.4 mm) brought them into the closed rows, and escaping them cost c3_usb 8 vias and
            # 16.6 mm and pushed the USB pair's detour from 1.81 to 2.04. An IC's supply pins
            # still get their escapes: without those, c3_usb and ds2 do not route at all.
            continue
        layer = foot.layer
        for side in sorted(set(foot.escape.values())):
            row = [p for p in foot.pads if foot.escape.get(p.num) == side]
            along = (lambda p: p.x) if side in ("top", "bottom") else (lambda p: p.y)
            row.sort(key=along)
            pitch = min((along(b) - along(a) for a, b in zip(row, row[1:])), default=0.0)
            stagger = fanout_stagger(stack, _lane_clearance(job), pitch) if pitch else 0.0
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
                # (measured: 14 stubs off 0/45/90 across c3_usb, node and ds2). The router starts
                # from the via wherever it is (`route_native.ViaTerminal`); nothing needs it on a grid.
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
                owner = f"{ref}.{p.num}"
                pieces.append(seg_piece(net, "fanout", layer, (px, py), (vx, vy), width, owner=owner, uuid=stable_uuid(board, "fanout", ref, p.num, "stub")))
                pieces.append(via_piece(net, "fanout", (vx, vy), stack.via_diameter, stack.via_drill, owner=owner, uuid=stable_uuid(board, "fanout", ref, p.num, "via")))
                notes.append({"ref": ref, "pad": p.num, "net": net, "via": (vx, vy)})
    return (pieces, notes)


def _lane_clearance(job: CompiledJob) -> float:
    """The widest class lane clearance, which is what a row's stagger is sized from (`lane_rules`)."""
    from .pcb_place import lane_rules

    return lane_rules(job)[1]
