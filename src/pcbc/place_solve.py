"""Warnings from the netlist's compiled requirements, on the placement already made.

The placer that runs in `pcbc pcb` and `pcbc build` (`pcb_place.resolve_places`)
does not read them. An anchor stays where it was written, at rotation 0 unless
`rotate=` was set. A `Place(to=...)` turns a two-pin part so its pad faces the
pin it hangs from. File order picks which part gets the closest spot. Current,
voltage and impedance never enter that choice.

`constraints.py` has already turned `amps` into a width, `volts` into a
clearance, and `z_diff_ohm` into a pair gap. `route_checks.route_aware_report`
then measures the placed board against the budgets those numbers became
(airwire, keep-out, loop, pair skew, the corridor a wide track needs) and
prints a move. This module does not add another one. A move is an edit, and
an edit the tool invented is a second layout for someone to copy.

The one fact that report does not state: a part whose `rotate=` was not
written, with a pad sitting on the far side of its anchor from the rest of
that net. The warning names the part, the net, the distance, and the width
and clearance the net was compiled to. It does not name a new angle or a
`Place()` line.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .pcb_place import Foot


@dataclass(frozen=True)
class NetNeed:
    """What one net asks of the placement. Every field is a compiled number."""

    net: str
    kind: str
    width_mm: float
    clearance_mm: float
    vias: bool
    layers: tuple[str, ...]
    keep_away: tuple[tuple[str, float], ...]
    pair_with: str | None
    pair_gap_mm: float | None
    loop_mm2: float | None
    airwire_max_mm: float | None


def pull(need: NetNeed) -> float:
    """How much one millimetre of this net costs, from its compiled width and clearance.

    Width is the current's answer and clearance is the voltage's. A net that
    cannot change layer pays twice, because the path has no other floor.
    """
    pitch = max(need.width_mm, 0.0) + max(need.clearance_mm, 0.0)
    weight = max(pitch, 0.1)
    if not need.vias or len(need.layers) <= 1:
        weight *= 2.0
    return weight


def needs_of(cs) -> list[NetNeed]:
    """One `NetNeed` per compiled constraint, in net order."""
    out: list[NetNeed] = []
    for c in cs.constraints:
        pair = c.pair
        out.append(
            NetNeed(
                net=c.net,
                kind=c.kind,
                width_mm=float(c.width_mm.value),
                clearance_mm=float(c.clearance_mm.value),
                # docs/physics-subjects.md PR 5: the pull doubles when any link of the net may not change layer.
                vias=bool(c.via.allowed) and all(lk.via.allowed and len(lk.layers) > 1 for p in c.paths for lk in p.links),
                layers=tuple(c.layers),
                keep_away=tuple((k.other, float(k.mm)) for k in c.keep_away),
                pair_with=pair.partner if pair is not None else None,
                pair_gap_mm=float(pair.gap_mm.value) if pair is not None else None,
                loop_mm2=float(c.loop_mm2.value) if c.loop_mm2 is not None else None,
                airwire_max_mm=c.airwire_max_mm,
            )
        )
    return out


def locks_of(job) -> dict[str, tuple[bool, bool]]:
    """`ref -> (position fixed, rotation fixed)`.

    A written anchor holds its point. `rotate=` is the only thing that holds
    the spin; `locked=True` without it does not.
    """
    out: dict[str, tuple[bool, bool]] = {}
    for spec in job.places:
        position = bool(spec.locked or spec.edge or spec.has_css())
        out[spec.ref] = (position, bool(spec.rot_set))
    return out


def facing_notes(feet: list[Foot], needs: list[NetNeed], locks: dict[str, tuple[bool, bool]] | None = None) -> list[str]:
    """Pads on the far side of a part whose spin was not written.

    Compared with the same pad spun half a turn about the anchor, which is a
    property of this pose, not a pose to apply. A part with `rotate=` set is
    silent: that spin was chosen.
    """
    locks = locks or {}
    by_net = {n.net: n for n in needs}
    pads: dict[str, list[tuple[Foot, object, float, float]]] = {}
    for foot in feet:
        if foot.at is None or foot.ref.startswith("FID"):
            continue
        for pad in foot.pads:
            if not pad.net or not pad.num:
                continue
            x, y = foot.pad_world(pad)
            pads.setdefault(pad.net, []).append((foot, pad, x, y))
    notes: list[tuple[str, str, str]] = []
    for net, sites in pads.items():
        need = by_net.get(net)
        for foot, pad, x, y in sites:
            if locks.get(foot.ref, (False, False))[1]:
                continue
            others = [(ox, oy, of) for of, _op, ox, oy in sites if of.ref != foot.ref]
            if not others:
                continue
            fx, fy, other = min(others, key=lambda s: math.hypot(s[0] - x, s[1] - y))
            ax, ay = foot.at
            dist = math.hypot(x - fx, y - fy)
            mx, my = 2.0 * ax - x, 2.0 * ay - y
            other_side = math.hypot(mx - fx, my - fy)
            if dist <= other_side + 0.4:
                continue
            width = f"{need.width_mm:.2f} mm wide, clearance {need.clearance_mm:.2f} mm" if need is not None else "no NetReq"
            notes.append(
                (
                    foot.ref,
                    net,
                    f"{foot.ref}: {net} pad is {dist:.1f} mm from {other.ref}, on the far side of the anchor "
                    f"(the other side of the body is {dist - other_side:.1f} mm closer). {width}. rot= was not written",
                )
            )
    notes.sort()
    return [text for _ref, _net, text in notes]
