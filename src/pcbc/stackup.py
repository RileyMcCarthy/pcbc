"""Named stackups and electrical → geometry solvers."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Stackup:
    """A named fab stackup: geometry for impedance, and the fab's limits.

    The limits are the one source for the board's constraints (`.kicad_pro`), the net
    classes' vias, the router's floor, and the copper gate. KiCad's own defaults were
    judging the copper before these were written.
    """

    name: str
    layers: int
    h_mm: float  # dielectric under F.Cu (microstrip height)
    t_mm: float  # outer copper thickness
    er: float
    copper_oz: float = 1.0
    track_min: float = 0.127  # mm; JLC 1-2 layer 5 mil
    clearance_min: float = 0.127
    via_drill: float = 0.3  # the standard via, no extra cost
    via_diameter: float = 0.5
    annular_min: float = 0.1
    # Copper to a hole's edge. JLC quotes 10 mil (0.254); KRT keeps tracks off an NPTH by this on
    # its 0.05 mm grid and lands at 0.25, so the gate holds 0.25 (4 um under any fab's tolerance).
    hole_clearance: float = 0.25
    hole_to_hole: float = 0.5
    edge_clearance: float = 0.3  # copper to the board edge


STACKUPS: dict[str, Stackup] = {
    "jlcpcb_4l_1oz": Stackup(
        name="jlcpcb_4l_1oz",
        layers=4,
        h_mm=0.12,  # typical JLC 4L prepreg F.Cu → In1
        t_mm=0.035,
        er=4.5,
        copper_oz=1.0,
        track_min=0.0889,  # 3.5 mil on 4+ layers
        clearance_min=0.0889,
        via_drill=0.2,
        via_diameter=0.35,
        annular_min=0.075,
    ),
    "jlcpcb_2l_1oz": Stackup(
        name="jlcpcb_2l_1oz",
        layers=2,
        h_mm=1.5,
        t_mm=0.035,
        er=4.5,
        copper_oz=1.0,
    ),
}


def hole_floor(stack: Stackup) -> float:
    """The copper clearance at which a track next to a via's ring also clears its hole."""
    return round(stack.hole_clearance - stack.annular_min + 0.005, 4)


def pass_mm(stack: Stackup) -> float:
    """The narrowest gap between two pads that the thinnest track can still pass through."""
    return round(stack.track_min + 2 * stack.clearance_min, 4)


def fanout_stagger(stack: Stackup, clearance: float, pitch: float) -> float:
    """How far past its neighbour's a via must step on a row of this pitch: two holes keep the
    fab's hole-to-hole, two rings keep the class clearance. 0.47 mm on a 0.65 mm row at JLC 2L."""
    need = max(stack.via_drill + stack.hole_to_hole, stack.via_diameter + max(clearance, stack.clearance_min))
    return round(math.sqrt(max(0.0, need * need - pitch * pitch)), 4)


def fanout_lane(stack: Stackup, clearance: float, pitch: float) -> float:
    """The free lane a closed pad row needs outside it: the router keeps a via `clearance_min` off
    its own pad, then the via, then the next row's stagger, then clearance to whatever sits
    beyond. `clearance` is the widest class's. 1.29 mm on a 0.65 mm row at JLC's 2-layer rung."""
    return round(stack.clearance_min + stack.via_diameter + max(clearance, stack.clearance_min) + fanout_stagger(stack, clearance, pitch), 4)


def board_rules(stack: Stackup) -> dict[str, float]:
    """The `.kicad_pro` design_settings.rules block."""
    return {
        "min_clearance": stack.clearance_min,
        "min_track_width": stack.track_min,
        "min_via_diameter": stack.via_diameter,
        "min_via_annular_width": stack.annular_min,
        "min_through_hole_diameter": stack.via_drill,
        "min_hole_clearance": stack.hole_clearance,
        "min_hole_to_hole": stack.hole_to_hole,
        "min_copper_edge_clearance": stack.edge_clearance,
    }


def get_stackup(name: str) -> Stackup:
    if name not in STACKUPS:
        known = ", ".join(sorted(STACKUPS))
        raise KeyError(f"unknown stackup {name!r}. known: {known}")
    return STACKUPS[name]


def microstrip_z0(w_mm: float, stack: Stackup) -> float:
    """IPC-2141-style microstrip (ohms)."""
    h = stack.h_mm
    t = stack.t_mm
    er = stack.er
    we = w_mm + (t / math.pi) * (1 + math.log(4 * math.pi * w_mm / t))
    return (87 / math.sqrt(er + 1.41)) * math.log(5.98 * h / (0.8 * we + t))


def width_for_z0(z0: float, stack: Stackup, wmin: float = 0.09, wmax: float = 2.0) -> float:
    lo, hi = wmin, wmax
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        z = microstrip_z0(mid, stack)
        if z > z0:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 4)


def diff_pair_geometry(z_diff: float, stack: Stackup) -> tuple[float, float]:
    """Width and gap for a loosely-coupled microstrip pair.

    Coupled-line math is stackup-sensitive. For v0 we size each member as
    z_diff/2 single-ended and set gap ≈ 0.75×width, then snap to 0.01 mm.
    Tune against the fab calculator before tape-out.
    """
    w = width_for_z0(z_diff / 2.0, stack)
    gap = max(0.10, round(0.75 * w, 2))
    return w, gap


def ipc2221_width_mm(
    amps: float,
    temp_rise_c: float = 10.0,
    copper_oz: float = 1.0,
    outer: bool = True,
) -> float:
    """IPC-2221 chart fit. Area in mil², 1 oz ≈ 1.378 mil."""
    if amps <= 0:
        return 0.2
    k = 0.048 if outer else 0.024
    dt = max(temp_rise_c, 1.0)
    area = (amps / (k * (dt**0.44))) ** (1 / 0.725)
    thick_mil = 1.378 * copper_oz
    width_mil = area / thick_mil
    return round(max(0.15, width_mil * 0.0254), 3)
