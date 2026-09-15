"""Named stackups and electrical → geometry solvers."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Stackup:
    name: str
    layers: int
    h_mm: float  # dielectric under F.Cu (microstrip height)
    t_mm: float  # outer copper thickness
    er: float
    copper_oz: float = 1.0


STACKUPS: dict[str, Stackup] = {
    "jlcpcb_4l_1oz": Stackup(
        name="jlcpcb_4l_1oz",
        layers=4,
        h_mm=0.12,  # typical JLC 4L prepreg F.Cu → In1
        t_mm=0.035,
        er=4.5,
        copper_oz=1.0,
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
