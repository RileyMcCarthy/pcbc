"""`spine` — a power net with three or more pads and no plane (`docs/r2-design.md` B.4).

The power nets are **37 % of all routed copper** on the five boards and the last big block: buck is
86 % power and carries the worst detour on any board, c3_usb 66 %, ds2 38 %. Left to KRT they are
routed like signals — the soft rule `width_power` counts every place a power track was necked below
its class, 39 times on buck, 51 on c3_usb, 15 on node — and a necked power track is not a style
preference, it is the IPC-2221 and IPC-2152 arithmetic R1 already compiled being ignored. Driving
that count to zero is what this pattern is for.

Two forms, in B.4's order:

- **comb** — one straight trunk at the class width with a perpendicular rib to every pad. The
  trunk's offset is not guessed: `free_intervals` (A.8) returns *every* lane the trunk could sit in
  at once, and the candidates are those lanes nearest the pads' median, at most six of them. Because
  the sweep answers the whole question at once, the refusal can say a number the AI can act on —
  how many lanes there were, the widest, and where — instead of "it did not fit". Measured: the comb
  fits **no** net on any of the five boards, and the `style:` line says so rather than leaving it
  silent.
- **backbone** — the stations ordered along their own principal axis and linked pairwise with B.0's
  shared link builder, at the trunk width. A middle station is a T: the link ends on the pad and the
  next one starts there, and KiCad joins tracks that touch. A link that fails splits the spine and
  **both halves are kept** — partial copper is legal, useful and honest, and `net_open` hands what
  is left to KRT.

**The spine never necks, and that is the whole point.** B.0's "a pattern never degrades" is sharper
here than anywhere else: a trunk narrower than its class is the bug this pattern exists to fix, so
every trunk piece and every backbone link is written at the class width or not at all. The only
copper allowed to neck is a comb's rib **into a bypass pad** — R-I3's allowance for the last
millimetre into a decoupling cap, and nothing else: a rib to an IC's power pin, a connector or a
series passive is the trunk width, because what a branch carries is a property of the pin and not of
the pad count (`_rib_width`, `shunt_stations`, and the S7 review's findings 4 and 11). A neck that
does happen is a `style:` line, never a silence.

**No layer change** (R2's stated non-goal, and H.4): a spine is single-layer, so R-I2's `per_change`
via budget never arises. A net whose pads do not all share one copper layer gets the links it can.

**One deviation from B.4, and it is `WIDE_MM`'s docstring**: B.4 uses that width only to admit a net
that is not `power`, and S7 applies it to every spine. ds2 is why, and the trade it names is the one
this pattern makes everywhere — a spine is copper locked before the router sees the board, and it
costs the corridor it takes.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from ..blocking import move_line
from ..copper_bar import airwire_mm
from ..route_checks import _principal_order
from ..route_emit import Piece
from ..route_geom import MICRO_MM, Pt, gap, octile_path, path_mm, track_shape
from ..route_scene import Clash, Scene, _project, blocked, free_intervals, pad_exits
from ..stackup import current_width_mm
from . import (
    MAX_LINKS,
    PatternCtx,
    PatternResult,
    Refusal,
    Terminal,
    _mm,
    _nm,
    lane_ok,
    legs_of,
    legs_ok,
    link_candidates,
    mitre,
    pieces_of,
    shape_ok,
    terminals,
    toward_first,
)

REASON = "spine"

CONNECTS = True
"""A spine's trunk and its branches join the pads of the net it is on (`docs/stitch-plan.md` §2k).
See `hop.CONNECTS`."""

WIDE_MM = 0.4
"""How wide a net's class has to be before it gets a spine at all.

B.4 uses this number one way — a net with three or more pads that is *not* `power` is a spine when
it is at least this wide — and S7 applies it to every spine, which is a **deviation from B.4** with
a measurement behind it.

A spine is copper locked before KRT sees the board, and it costs the corridor it takes. What it buys
is the difference between the class width and what a maze router would have necked to, so on a net
whose class is 0.25 mm against a fab floor of 0.127 the pattern is spending a corridor to save
0.123 mm of copper width. ds2 is that board — two layers, a dense TSSOP, and **six** `vias=False`
single-layer analog nets whose only corridors are the ones a spine would take — and it is the board
where the trade was measured: at every locality cap tried (6, 8, 12 mm and none) ds2's 0.25 mm spines
broke the gate outright, `AIN0` or `AIN1` losing its path to `U1` entirely or `3V3`, `GND` and `VSS`
finding no path on any layer. C.1 predicts it — the constrained nets are step 3 and the spine is
step 5, but until `chain` (S6) lands step 3 is KRT's and runs *after* the whole pre stage — and the
number that separates a board where a spine pays from one where it does not is the width it is
protecting. `docs/r2-measurements.md` S7, and it is S7's first open issue.

It is also the Power class's own width on c3_usb, node and the DS2 Addon's 4-layer sibling, so
"wide enough that a maze router necking it is a warning" and "wide enough to be worth a corridor" are
the same number rather than two."""

AXES = ("x", "y", "u", "v")
"""B.4's trunk axes, in B.4's tie-breaking order. `u = x + y` and `v = x - y` are the two diagonals,
in `route_scene._project`'s frame: no sqrt(2) on any coordinate, so 4 dp stays exact and the sqrt(2)
is carried by the half-widths and the spreads instead."""

ACROSS = {"x": "y", "y": "x", "u": "v", "v": "u"}
"""The axis a trunk's **offset** is measured on, which is the other one.

A lane returned by `free_intervals` is an interval of `across` coordinates at a fixed `along` span, so
printing one as `x=2.922` when the trunk runs along `x` names the wrong axis for the one number a
person or an agent acts on. Every shipped spine refusal did (finding 14): buck's `VIN` printed
`x=2.922` for a `y`; c3_usb's `3V3` printed `x=29.190` for a `y` near the bottom edge; c3_usb's
`VBUS` printed `v=56.605` for a `u`, a value the `v` axis cannot take over that span at all.
`docs/r2-design.md` B.4 has the correct form — "0.430 mm at **y=13.100**" for a trunk along x."""

MAX_OFFSETS = 6
"""B.4 item 3's bound on the comb: at most six trunk offsets are tried for a net, and they are the
free lanes nearest the pads' median. The bound is declared here and nowhere else."""

COMB_DETOUR_MAX = 1.6
"""How far past the net's airwire a whole comb may run, as a ratio.

Without it the comb is not a pattern but a bad route. On buck's `VIN` **no** free lane on F.Cu
overlaps the pads' own row — they sit at y 6.45 to 11.98 and the lanes are 0.69-5.54, 16.40-19.41
and 22.59-24.31 once `trunk_window` takes the trunk's own half width off the board's own window — so
the nearest lane centre is y 3.12, and the comb it gives is a 17.8 mm trunk
hugging the board edge with five ribs 3.5 to 9.1 mm long: **52.7 mm** of 0.781 mm copper for a
21.2 mm airwire, 2.49x, walling off the top of the board for a net the backbone then links in 21 mm.

1.6 is above every comb a person would draw and below that one. The one comb in this repo that does
fit — ds2's `VDDA`, at 25.1 mm against a 16.9 mm airwire — is **1.49x**, and it is the reason this
constant is not lower. `docs/r2-measurements.md` S7."""

LINK_DETOUR_MAX = 1.5
"""`hop.DETOUR_MAX`, for the same reason and with the same argument: a backbone link that walks
twice the distance between its two stations is a routing problem wearing a pattern's clothes, and
the honest thing is to refuse and let the maze router have it."""

RIB_REACH_MM = 1.5
"""How far along the trunk a blocked rib's one `ell` retry may look for a free landing, in mm.

`tap.TAP_REACH_MM`'s number and, until a comb on a real board measures its own, `tap`'s
justification as well: it is the reach that buys a pad the room to clear one neighbour's keep-out
without letting a branch wander into being a route. H.8 records that a derived reach —
`k * fanout_lane(stack, clearance, pitch)` — is what both constants should become."""


# --- which nets this pattern has something to say about -------------------------------------------


@dataclass(frozen=True)
class SpineSpec:
    """One power net, with the arithmetic that decides how wide its copper has to be.

    `width` is what the trunk is written at and `trunk_need` is what IPC asks of it; the two are the
    same number on every net these boards carry, and `run` refuses rather than writing copper when
    they are not (`rule: ampacity`). `branch_need` is the floor under a rib's neck: one pad's share
    of the net's current, by the same curve, so R-I3's allowance to neck into a pad can never take
    a branch below what the branch carries.
    """

    net: str
    layer: str  # the one copper layer the whole spine runs on; "" when the pads share none
    stations: tuple[Terminal, ...]
    width: float  # the class width: what a trunk and every backbone link is written at
    amps: float  # what the net carries (Constraint.current)
    share: float  # an even 1/pads of it — a placeholder for a bypass pad's ripple, see `spine_widths`
    trunk_need: float  # max(IPC-2221 external, IPC-2152 with modifiers) at `amps`
    branch_need: float  # the same curve at `share`
    why: str  # the arithmetic, for the report and the refusal
    airwire: float  # the MST over the stations: the shortest copper that could connect them
    shunts: frozenset[str] = frozenset()  # the station owners a rib is allowed to neck into


def _layer(ctx: PatternCtx, net: str, stations: tuple[Terminal, ...]) -> str:
    """The one copper layer every station shares, intersected with the net's declared `layers` in the
    constraint's order — `hop._layer` over a whole net rather than over a pair, so a
    `NetReq(layers=[...])` is honoured and not merely avoided."""
    shared = frozenset(ctx.scene.layers)
    for t in stations:
        shared = shared & t.layers
    c = ctx.cs.by_net(net)
    order = list(c.layers) if c is not None and c.layers else list(ctx.scene.layers)
    for layer in order:
        if layer in shared:
            return layer
    return ""


def shunt_stations(ctx: PatternCtx, stations: tuple[Terminal, ...]) -> frozenset[str]:
    """Which stations are a **bypass pad** — the only ones whose rib may be narrower than the trunk.

    A rib carries what its pad draws, and `amps / pads` is not that number for any pad on any of
    these boards (findings 4, 6 and 11). buck's `VIN` is the case: five pads, and `J_IN.1` (the input
    connector) and `U1.3` (the TPS54202's VIN pin) carry the whole 2 A in series while `R_EN.1`, a
    100 k pull-up, carries 12 V / 100 k = **0.12 mA**. Dividing the rail evenly understated the two
    that matter by five and overstated the one that does not by four orders of magnitude, and it did
    it where it lands: `_rib_width` returned 0.532 mm for `U1.3` — a SOT-23-6's pad width, arrived at
    by luck of package geometry — which carries 1.515 A of that pin's 2 A.

    pcbc cannot read a pin's role from the netlist yet (R1's `Constraint.current` has no per-pad
    share), so this is the one population it *can* name, and it is `tap`'s own: a **declared
    two-terminal passive whose other pad is on a plane or return net**. That is a decoupling or
    bypass cap and nothing else — a series resistor's other pin is a signal, an IC pin is not a
    passive, and a two-pin power connector is not a declared passive (which is what keeps buck's
    `J_IN` out, and it sources the whole rail). Every other station gets the trunk width, and when
    the trunk width does not fit beside its neighbours `blocked` refuses the comb — which is B.0's
    "a pattern never degrades" enforced by the clearance table rather than by a second rule.
    """
    from ..fab import passive_refs

    passives = passive_refs(ctx.design)
    pads_by_ref: dict[str, set[tuple[str, str]]] = {}
    for it in ctx.scene.items:
        if it.kind == "pad" and it.owner:
            pads_by_ref.setdefault(it.owner.split(".")[0], set()).add((it.owner, it.net))
    returns = set(ctx.scene.plane_of)
    out = set()
    for t in stations:
        siblings = pads_by_ref.get(t.ref, set())
        others = {n for owner, n in siblings if owner != t.owner}
        if t.ref in passives and len(siblings) == 2 and others & returns:
            out.add(t.owner)
    return frozenset(out)


def spine_widths(scene: Scene, cs, net: str, stations: tuple[Terminal, ...], shunts: frozenset[str] = frozenset()) -> tuple[float, float, float, float, str]:
    """(width, share, trunk_need, branch_need, why) — the current this spine has to carry.

    One source for both numbers, and it is R1's: `Constraint.current.amps` is what the net carries
    and `stackup.current_width_mm` is the curve — `max(IPC-2221B external, IPC-2152 with the board
    and plane modifiers)` — that `Constraint.width_mm` was itself derived from. So `trunk_need` is
    not a second opinion about the same question; it is the same arithmetic asked again where the
    copper is actually written, which is what makes "the trunk carries everything downstream of it"
    a check rather than a claim.

    **`share` is a placeholder and the `why` string now says so.** It is `amps / pads`, and no pad on
    any of these boards draws it: buck's `VIN` reported "0.15 mm for one pad's 0.4 A" on every build
    while its five pads draw 2 A, 2 A, ripple, ripple and 0.12 mA (finding 6). It survives as the
    **floor** under a bypass pad's rib, where an honest number does not exist until a `Pin(amps=)`
    does, and it applies to nothing else: `shunt_stations` says which pads it applies to and the
    string names them, so the assumption is printed rather than implied.
    """
    c = cs.by_net(net)
    stack = scene.stack
    pads = len(stations)
    width = float(c.width_mm.value) if c is not None else stack.track_min
    cur = c.current if c is not None else None
    amps = cur.amps if cur is not None else 0.0
    dt = cur.temp_rise_c if cur is not None else 10.0
    plane_h = cur.plane_h_mm if cur is not None else None
    share = round(amps / max(pads, 1), 4)
    trunk_need = current_width_mm(amps, dt, stack, plane_h).value if amps > 0 else 0.0
    branch_need = current_width_mm(share, dt, stack, plane_h).value if share > 0 else 0.0
    cls = c.class_name if c is not None else "net"
    where = ", ".join(sorted(shunts)) if shunts else "no pad here"
    if amps <= 0:
        why = f"the {cls} class's {width:g} mm, which is the width the class declares; {net} carries no current a NetReq named"
    else:
        why = (
            f"the {cls} class's {width:g} mm trunk against the {trunk_need:g} mm IPC asks for {net}'s whole {amps:g} A, "
            f"and every rib at that width too except into a bypass pad ({where}), which may neck to {branch_need:g} mm "
            f"— an even 1/{pads} share of {amps:g} A, a placeholder and not a current any pad was declared to draw"
        )
    return (width, share, trunk_need, branch_need, why)


def specs(ctx: PatternCtx) -> tuple[SpineSpec, ...]:
    """Every net this pattern will try, `sorted(net)` — B.0's order for a net-driven pattern.

    B.4's population: a net with at least three pads and no plane that is `power`, or `switch_node`,
    or simply wide (`WIDE_MM`). A plane net is the tap pattern's (B.3) and needs no spine — a plane
    *is* the spine; a pair is R4's; a declared bus member belongs to the ribbon (B.5).
    """
    scene = ctx.scene
    out: list[SpineSpec] = []
    for net in sorted({it.net for it in scene.items if it.kind == "pad" and it.net}):
        if net in scene.plane_of:
            continue
        c = ctx.cs.by_net(net)
        if c is None or c.pair is not None:
            continue
        if c.group is not None and c.group.kind == "bus":
            continue
        stations = terminals(scene, net)
        if len(stations) < 3:
            continue
        shunts = shunt_stations(ctx, stations)
        width, share, trunk_need, branch_need, why = spine_widths(scene, ctx.cs, net, stations, shunts)
        if width < WIDE_MM:
            continue
        out.append(
            SpineSpec(
                shunts=shunts,
                net=net,
                layer=_layer(ctx, net, stations),
                stations=stations,
                width=width,
                amps=c.current.amps if c.current is not None else 0.0,
                share=share,
                trunk_need=trunk_need,
                branch_need=branch_need,
                why=why,
                airwire=round(airwire_mm([anchor(t) for t in stations]), 4),
            )
        )
    return tuple(out)


def anchor(t: Terminal) -> Pt:
    """Where a rib or a link meets this station: the centre of the **primitive**, not of everything
    the pad draws. `tap.anchor`'s rule and `tap.anchor`'s reason — a USB-C shield pad and node's QFN
    thermal pad draw several shapes, and the centre of all of them is a point that may sit between
    them, so a rib from there is neither axis-aligned nor necessarily on copper."""
    return t.item.at()


# --- the axis frame, exactly ----------------------------------------------------------------------


def axis_of(p: Pt, axis: str) -> tuple[float, float]:
    """(along, across) of a point — `route_scene._project`'s frame asked of a point rather than a box."""
    x, y = p
    if axis == "x":
        return (x, y)
    if axis == "y":
        return (y, x)
    if axis == "u":
        return (x + y, x - y)
    return (x - y, x + y)


def iaxis(p: Pt, axis: str) -> tuple[int, int]:
    """`axis_of` in integer nanometres, which is where every construction here actually happens."""
    x, y = _nm(p[0]), _nm(p[1])
    if axis == "x":
        return (x, y)
    if axis == "y":
        return (y, x)
    if axis == "u":
        return (x + y, x - y)
    return (x - y, x + y)


def world(along: int, across: int, axis: str) -> Pt:
    """The world point at (along, across) nanometres. The caller guarantees the parity (`even_across`)."""
    if axis == "x":
        return (_mm(along), _mm(across))
    if axis == "y":
        return (_mm(across), _mm(along))
    u, v = (along, across) if axis == "u" else (across, along)
    return (_mm((u + v) // 2), _mm((u - v) // 2))


def even_across(along: int, across: int, axis: str) -> int:
    """The `across` nanometre a world point at this `along` can actually be built from.

    On `x` and `y` every pair is a world point already. On a diagonal the world point is
    `((u + v) / 2, (u - v) / 2)`, so `u + v` must be **even**; `across` is stepped back by the one
    nanometre that makes it so.

    One nanometre is a thousandth of the narrowest track any board here writes. What it moves is a
    rib's landing: 0.5 nm off the trunk's centreline instead of on it, and therefore still 63500 nm
    inside the trunk's own copper — which is where the connection is made in any case, because
    KiCad joins tracks that **touch**, not tracks whose endpoints are equal. The alternative is to
    round the foot and emit a leg that is 45 degrees to within a rounding error, which
    `is_octilinear` refuses outright and should.
    """
    if axis in ("x", "y"):
        return across
    return across - ((along + across) & 1)


# --- the comb ---------------------------------------------------------------------------------------


def trunk_axis(scene: Scene, spec: SpineSpec) -> str:
    """B.4 item 1: the axis with the larger spread of the pads' centres; `x, y, u, v` breaks a tie.

    A tie is "within `scene.grid`", so two spreads a router could not tell apart do not make the
    board a function of the last bit of a float. The diagonal spreads are divided by sqrt(2) before
    they are compared, because `_project` carries `u = x + y` unscaled and a raw `u` spread is
    sqrt(2) times the distance it represents — comparing it against `x` directly would pick a
    diagonal trunk for every net on the board.
    """
    spreads: dict[str, float] = {}
    for axis in AXES:
        vals = [axis_of(anchor(t), axis)[0] for t in spec.stations]
        scale = math.sqrt(2.0) if axis in ("u", "v") else 1.0
        spreads[axis] = (max(vals) - min(vals)) / scale
    best = max(spreads.values())
    for axis in AXES:
        if spreads[axis] > best - scene.grid:
            return axis
    return AXES[0]


def trunk_span(spec: SpineSpec, axis: str) -> tuple[float, float]:
    """B.4 item 2: the stations' projections onto the axis, extended at each end by that pad's own
    half size so the trunk reaches the copper it is there to serve.

    The extension is what keeps every rib strictly **inside** the trunk rather than on its end: a
    rib landing exactly on a trunk endpoint is a 90 degree corner between two connected segments,
    which is the warning `dru.py` writes `pcbc_geometry_angles` to catch. Taking the pads' boxes
    rather than their centres gives it for free.
    """
    lo = min(_project(t.item.box(), axis)[0][0] for t in spec.stations)
    hi = max(_project(t.item.box(), axis)[0][1] for t in spec.stations)
    return (lo, hi)


def trunk_window(outline: tuple[float, float, float, float], axis: str, span: tuple[float, float], half: float) -> tuple[float, float]:
    """Which offsets a trunk across `span` can take and still be **on the board**.

    On `x` and `y` this is `_project(outline, axis)[1]` shrunk by the trunk's half width, because the
    across extent of a rectangle does not depend on where along it you stand. On a diagonal it does,
    and `_project` is the wrong function to ask: it returns the min and max of `u` over the
    rectangle's four *corners*, which is exactly right as a conservative obstacle extent and wrong as
    a containment window. Measured on c3_usb's `VBUS` (finding 13): the window it gave was
    `(0.6, 69.4)`, the two offsets tried were `u=56.605` and `u=66.983`, and all four trunk endpoints
    they produce are off a board that ends at x=39.7, y=29.7 — one of them at x=45.0. Both of the
    comb's at-most-six candidates were spent on lanes that cannot hold a trunk, and the refusal then
    said "2 free lanes there could take it" one line above naming the board edge as the blocker.

    The attainable set is linear in `along`, so intersecting it at the span's two ends is exact for
    everything between them. For `u` (along `u = x + y`, across `v = x - y`) a world point is
    `x = (u + v)/2, y = (u - v)/2`, so `v` is in `[max(2x0 - u, u - 2y1), min(2x1 - u, u - 2y0)]`;
    for `v` the same algebra gives `u` in `[max(2x0 - v, 2y0 + v), min(2x1 - v, 2y1 + v)]`. An empty
    window is returned as a zero-width interval, which `free_intervals` reads as no lane at all.
    """
    x0, y0, x1, y1 = outline
    pad = half * (math.sqrt(2.0) if axis in ("u", "v") else 1.0)
    if axis in ("x", "y"):
        lo, hi = _project(outline, axis)[1]
        return (lo + pad, hi - pad) if hi - pad >= lo + pad else (lo + pad, lo + pad)
    ends = []
    for along in (min(span), max(span)):
        if axis == "u":
            ends.append((max(2 * x0 - along, along - 2 * y1), min(2 * x1 - along, along - 2 * y0)))
        else:
            ends.append((max(2 * x0 - along, 2 * y0 + along), min(2 * x1 - along, 2 * y1 + along)))
    lo = max(e[0] for e in ends) + pad
    hi = min(e[1] for e in ends) - pad
    return (lo, hi) if hi >= lo else (lo, lo)


def trunk_offsets(scene: Scene, spec: SpineSpec, axis: str, free: tuple[tuple[float, float], ...]) -> tuple[float, ...]:
    """B.4 item 3's candidate list, in B.4's order and bounded at `MAX_OFFSETS`.

    First every free lane that **contains a pad's own perpendicular coordinate** — the trunk then
    runs through the pad row and that pad's rib has length zero, which is the comb a human draws.
    Then the centre of every lane that is still unused. Both groups nearest the pads' median first,
    because the median is the row the ribs are shortest from and there is no cost function anywhere
    in a pattern: the order is the answer.
    """
    across = [axis_of(anchor(t), axis)[1] for t in spec.stations]
    median = statistics.median(across)
    hit: list[float] = []
    used: set[tuple[float, float]] = set()
    for a in sorted(set(across), key=lambda v: (abs(v - median), v)):
        for lane in free:
            if lane[0] <= a <= lane[1]:
                hit.append(round(a, 4))
                used.add(lane)
                break
    rest = sorted(
        (round((lo + hi) / 2.0, 4) for lo, hi in free if (lo, hi) not in used),
        key=lambda v: (abs(v - median), v),
    )
    return tuple((hit + rest)[:MAX_OFFSETS])


def _trunk(ctx: PatternCtx, spec: SpineSpec, axis: str, span: tuple[float, float], off: float) -> tuple[Piece, ...]:
    """The trunk: one straight piece across `span` at perpendicular offset `off`."""
    o = _nm(off)
    lo, hi = _nm(span[0]), _nm(span[1])
    if axis in ("u", "v"):
        # The two ends carry the same parity rule as a rib's foot (`even_across`), and they are pushed
        # **outward** to satisfy it so the span never shrinks below the pads it has to cover.
        lo -= (lo + o) & 1
        hi += (hi + o) & 1
    a, b = world(lo, o, axis), world(hi, o, axis)
    return pieces_of((a, b), (spec.width,), net=spec.net, reason=REASON, layer=spec.layer, owner=spec.stations[0].owner, board=ctx.board)


def _rib_width(ctx: PatternCtx, spec: SpineSpec, t: Terminal, axis: str) -> float:
    """B.4 item 5: a rib into a **bypass pad**, and only that, may neck to the pad's across
    dimension (R-I3).

    Everything else — an IC's power pin, a connector pin, a series passive — gets the trunk width,
    because the current a branch carries is a property of the pin and not of the pad count, and the
    only pad pcbc can currently prove draws less than the rail is a decoupling cap's
    (`shunt_stations`). This is the rule the S7 review found returning 0.532 mm for buck's `U1.3` —
    the TPS54202's VIN pin, which draws the whole 2 A of the rail — against the 0.781 mm its class
    declares; 0.532 mm of 1 oz copper carries **1.513 A** on the IPC-2221B external curve at that
    net's own 10 C rise, so the rule under-sized by a third the one branch that carries everything
    (findings 4 and 11).

    A necked bypass rib is still floored twice and capped once: never under the fab's `track_min`,
    never under `branch_need` (which is a placeholder — `spine_widths` says so in the string it
    prints), and never over the class width.
    """
    if t.owner not in spec.shunts:
        return spec.width
    box = t.item.box()
    # "Across" is across the RIB, which runs perpendicular to the trunk: on an `x` trunk the rib is
    # vertical, so the pad dimension it has to fit into is the horizontal one.
    if axis == "x":
        across = box[2] - box[0]
    elif axis == "y":
        across = box[3] - box[1]
    else:
        # A diagonal rib meets both pad edges at 45, so the width that fits inside the pad is the
        # smaller edge times sqrt(2) — never more than either edge would allow on its own.
        across = min(box[2] - box[0], box[3] - box[1]) * math.sqrt(2.0)
    neck = max(min(spec.width, across), ctx.scene.stack.track_min, spec.branch_need)
    return round(min(neck, spec.width), 4)


def _comb(ctx: PatternCtx, spec: SpineSpec) -> tuple[dict, list[Clash]]:
    """Every comb candidate, in B.4's order; the first that clears wins.

    Returns the winner (or the reason there is none) and the blockers seen, so a refusal can name the
    widest lane there was as well as what was sitting in it.
    """
    scene = ctx.scene
    axis = trunk_axis(scene, spec)
    span = trunk_span(spec, axis)
    window = trunk_window(scene.outline, axis, span, spec.width / 2.0)
    free = free_intervals(axis, span, window, spec.width / 2.0, spec.net, scene, spec.layer)
    offsets = trunk_offsets(scene, spec, axis, free)
    mine = frozenset(it.id for it in scene.items if it.owner in {t.owner for t in spec.stations})
    refs = tuple(t.ref for t in spec.stations)
    seen: list[Clash] = []
    tried = 0
    ratio = 0.0
    out = {"pieces": (), "tried": 0, "axis": axis, "span": span, "free": free, "offsets": offsets, "ratio": 0.0, "notes": (), "candidate": ""}
    for off in offsets:
        trunk = _trunk(ctx, spec, axis, span, off)
        if not trunk or trunk[0].mm < MICRO_MM:
            continue
        tried += 1
        clash = blocked(scene, trunk, spec.net, ignore=mine)
        if clash is not None:
            seen.append(clash)
            continue
        pieces: list[Piece] = list(trunk)
        notes: list[str] = []
        ok = True
        for t in spec.stations:
            rib, note, bad = _rib(ctx, spec, axis, off, t, trunk[0], mine)
            if bad is not None:
                seen.append(bad)
            if rib is None:
                ok = False
                break
            pieces.extend(rib)
            notes.extend(note)
        if not ok:
            continue
        r = sum(p.mm for p in pieces) / spec.airwire if spec.airwire > 0 else 1.0
        if r > COMB_DETOUR_MAX:
            ratio = r if ratio == 0.0 else min(ratio, r)
            continue
        fits, _why = lane_ok(scene, pieces, refs)
        if not fits:
            continue
        out.update({"pieces": tuple(pieces), "candidate": f"comb along {axis} at {off:g} on {spec.layer}", "notes": tuple(notes)})
        break
    out["tried"] = tried
    out["ratio"] = ratio
    return (out, seen)


def _rib(
    ctx: PatternCtx, spec: SpineSpec, axis: str, off: float, t: Terminal, trunk: Piece, mine: frozenset[int]
) -> tuple[tuple[Piece, ...] | None, tuple[str, ...], Clash | None]:
    """B.4 item 4: the straight from the pad to its foot of perpendicular on the trunk, then one
    `ell` retry along the trunk when that foot is blocked, then nothing and the comb loses the offset.

    A rib shorter than `MICRO_MM` is not written: the trunk is already running through the pad. That
    is checked and not assumed — `gap` of the two shapes, the same question the self-check would ask
    — because a pad *near* the trunk and not touching it has no legal rib at all (KiCad's own
    `track_segment_length (min 0.2mm)` would count one), and silently writing nothing there is how a
    pattern leaves a pad unconnected and lets the gate find it.
    """
    scene = ctx.scene
    w = _rib_width(ctx, spec, t, axis)
    at = anchor(t)
    along, across = iaxis(at, axis)
    o = even_across(along, _nm(off), axis)
    foot = world(along, o, axis)
    note: tuple[str, ...] = ()
    if w < spec.width - 1e-9:
        c = ctx.cs.by_net(spec.net)
        note = (
            f"style: spine {spec.net}: {t.owner}'s rib necks to {w:g} mm, the pad's across dimension, against the "
            f"{c.class_name if c is not None else 'class'}'s {spec.width:g} mm — it is a bypass pad, so what it draws "
            f"is ripple and the floor under it is {spec.branch_need:g} mm, an even 1/{len(spec.stations)} share of "
            f"{spec.amps:g} A and not a declared current",
        )
    if math.dist(at, foot) < MICRO_MM:
        if t.item.copper is not None and gap(track_shape(trunk.a, trunk.b, trunk.w), t.item.copper) <= 0.0:
            return ((), (), None)  # the trunk runs through the pad: this rib has already happened
        return (None, (), None)
    straight = pieces_of((at, foot), (w,), net=spec.net, reason=REASON, layer=spec.layer, owner=t.owner, board=ctx.board)
    bad = blocked(scene, straight, spec.net, ignore=mine)
    if bad is None:
        return (straight, note, None)
    # The one `ell` retry: step along the trunk, nearer first, and turn in. The corner is a right
    # angle between two legs of the trunk's own frame, so it is mitred like every other corner pcbc
    # writes rather than emitted and then counted by `pcbc_geometry_angles`.
    steps = int(math.floor(RIB_REACH_MM / scene.grid + 1e-9))
    lo, hi = _nm(min(trunk_span(spec, axis))), _nm(max(trunk_span(spec, axis)))
    for k in range(1, steps + 1):
        for sign in (1, -1):
            a2 = along + sign * k * _nm(scene.grid)
            if not lo <= a2 <= hi:
                continue
            elbow = world(a2, even_across(a2, across, axis), axis)
            land = world(a2, even_across(a2, _nm(off), axis), axis)
            pts = mitre(_dedupe((at, elbow, land)))
            if pts is None or not shape_ok(pts):
                continue
            pieces = pieces_of(pts, [w] * (len(pts) - 1), net=spec.net, reason=REASON, layer=spec.layer, owner=t.owner, board=ctx.board)
            if not legs_ok(legs_of(pieces)):
                continue
            if blocked(scene, pieces, spec.net, ignore=mine) is None:
                return (pieces, note, bad)
    return (None, (), bad)


# --- the backbone -----------------------------------------------------------------------------------


def station_order(spec: SpineSpec) -> tuple[Terminal, ...]:
    """B.4 (b): the stations along their own first principal axis, `route_checks._principal_order`.

    The same function that orders a declared `Chain()`'s stops, for the same reason — it is the line
    the pads lie closest to, so consecutive pairs are the pairs a person would join."""
    order = _principal_order([anchor(t) for t in spec.stations])
    return tuple(spec.stations[i] for i in order)


def _backbone(ctx: PatternCtx, spec: SpineSpec) -> tuple[tuple[Piece, ...], list[tuple[str, str]], list[tuple[Terminal, Terminal, Clash | None, str]], int]:
    """Consecutive stations linked pairwise with B.0's builder, at the trunk width.

    **One run, grown a link at a time**, and not a bag of independent links. A middle station is a
    T-junction whose two links share that pad's centre exactly, so `route_verify.paths_of` re-assembles
    them into one run and asks `turn_ok` at the station — and where the chain bends there, the two
    links leave by adjacent sides and meet at a right angle. Judging each link alone writes that
    corner and then fails pcbc's own self-check. Growing the run instead means the corner passes
    through `mitre` like every other corner pcbc writes, and the whole run so far is re-judged each
    time, because a mitre moves copper that was already accepted.

    A link that fails ends the run and starts a new one: B.4 says a pad whose link fails splits the
    spine into two components and **both** are kept, and `net_open` then hands what is left to KRT.
    """
    stations = station_order(spec)
    mine = frozenset(it.id for it in ctx.scene.items if it.owner in {t.owner for t in spec.stations})
    runs: list[tuple[Piece, ...]] = []
    joins: list[tuple[str, str]] = []
    failed: list[tuple[Terminal, Terminal, Clash | None, str]] = []
    head: tuple[Pt, ...] = ()
    grown: tuple[Piece, ...] = ()
    served: list[str] = []
    tried = 0
    for a, b in zip(stations, stations[1:]):
        got, n, worst, why = _link(ctx, spec, a, b, mine, head, tuple(served) or (a.ref,))
        tried += n
        if got is None:
            failed.append((a, b, worst, why))
            if grown:
                runs.append(grown)
            head, grown, served = (), (), []
            continue
        head, grown = got
        served = sorted({*served, a.ref, b.ref})
        joins.append((a.owner, b.owner))
    if grown:
        runs.append(grown)
    return (tuple(p for run in runs for p in run), joins, failed, tried)


def _link(
    ctx: PatternCtx, spec: SpineSpec, a: Terminal, b: Terminal, mine: frozenset[int], head: tuple[Pt, ...], refs: tuple[str, ...]
) -> tuple[tuple[tuple[Pt, ...], tuple[Piece, ...]] | None, int, Clash | None, str]:
    """One more station on the run: B.0's `exits(a) x exits(b) x link_candidates`, first that clears.

    `hop.run` without the neck and without the narrowing — the width is the class width from end to
    end, so a link that only fits narrow does not fit. `head` is the run this link extends, and the
    candidate is judged as the whole run it will be, which is the same rule `shape_ok` states for a
    link and its own two stubs, one station further out.
    """
    scene = ctx.scene
    exits_a = toward_first(pad_exits(scene, a.item, spec.width, spec.layer), a, b)
    exits_b = toward_first(pad_exits(scene, b.item, spec.width, spec.layer), b, a)
    span = math.dist(anchor(a), anchor(b))
    was = path_mm(head) if len(head) >= 2 else 0.0
    worst: tuple[float, Clash] | None = None
    tried = 0
    why = ""
    for _name, pts in link_shapes(a, b, exits_a, exits_b):
        raw = _dedupe(head + pts)
        full = mitre(raw)
        if full is None or not shape_ok(full):
            continue
        pieces = pieces_of(full, [spec.width] * (len(full) - 1), net=spec.net, reason=REASON, layer=spec.layer, owner=a.owner, board=ctx.board)
        merged = legs_of(pieces)
        if not legs_ok(merged):
            continue
        tried += 1
        if span > 0 and (path_mm(merged) - was) / span > LINK_DETOUR_MAX:
            why = why or "detour"
            continue
        fits, _lane = lane_ok(scene, pieces, (*refs, a.ref, b.ref))
        if not fits:
            why = why or "lane"
            continue
        clash = blocked(scene, pieces, spec.net, ignore=mine)
        if clash is None:
            return ((merged, pieces), tried, None, "")
        if worst is None or (clash.need - clash.have) > worst[0]:
            worst = (clash.need - clash.have, clash)
    return (None, tried, worst[1] if worst else None, why or ("no candidate" if not tried else "copper"))


def crowded(a: Terminal, b: Terminal, exits_a, exits_b) -> bool:
    """Are these two pads closer together than their own two exits are from their centres?

    A.7 puts an exit `half + the widest clearance this net owes on the footprint + width/2` out, and
    that distance grows with the width — so on the widest copper pcbc writes, two neighbouring pads
    of the same net routinely sit **inside** each other's exits. Measured at 0.781 mm on buck, and
    re-taken from a build for the S7 review, which found three of these numbers invented (finding
    22): `C_IN1.1 -> U1.3` are 1.7160 mm apart with their widest exits **1.3156 and 1.1266** mm out,
    `U1.3 -> C_IN2.1` 1.5432 mm with 1.1266 and 1.3156, and on `5V` `L1.2 -> C_OUT1.1` 2.1620 mm with
    2.4006 and 1.3156. Every exit-to-exit candidate for those links doubles back on itself, `mitre`
    refuses the hairpin, and three quarters of buck's `VIN` and `5V` went to KRT for a reason that is
    about the exits and not about the board.

    `C_IN2.1 -> R_EN.1` (3.0093 mm against 1.3156 + 0.9106) used to be listed here as a fourth case
    and is **not** one: it measures roomy, gets six candidates rather than 112, and still fails with
    `no candidate`. It is a link this change did not help, which is worth saying and is not evidence
    for it. `docs/r2-measurements.md` S7.

    The **widest** exit on each pad, not the nearest: the question is whether any exit pair can
    cross, and it is the pair facing each other that does. On buck's `C_OUT1.1 -> C_OUT2.1` the two
    pads are 2.2500 mm apart, their nearest exits 1.0906 mm out and the two facing ones 1.3156 mm, so
    the nearest-exit reading calls them roomy (2.1812 < 2.25) and every exit-to-exit candidate then
    doubles back, while the widest reading (2.6312 > 2.25) calls them crowded, which they are.
    """
    if not exits_a or not exits_b:
        return True
    da = max(math.dist(anchor(a), e.at) for e in exits_a)
    db = max(math.dist(anchor(b), e.at) for e in exits_b)
    return math.dist(anchor(a), anchor(b)) < da + db


def link_shapes(a: Terminal, b: Terminal, exits_a, exits_b) -> tuple[tuple[str, tuple[Pt, ...]], ...]:
    """B.0's `exits(a) x exits(b) x link_candidates`, with **three** more in front of it: the straight
    line between the two pad centres and the two octile paths between them, offered only where the
    two pads are `crowded`.

    `pad_exits` says a pattern never starts a track at a pad centre and aims at another pad centre,
    and the reason it gives is real — buck's straight `EN` centre line passes 0.0892 mm from
    `U1.4 [FB]`. But that reason is a *clearance*, and a clearance is what `blocked` judges, on this
    candidate exactly as on every other: the centre line is not exempt from anything, it is simply
    the one shape the exit enumeration cannot express when the exits cross. It is tried **first**
    there, because when it clears it is also the shortest copper that could join the two pads, and
    it is not offered at all when the exits do not cross, so no link that the exits can express is
    decided by it.

    The bound is 4 x 4 x 7 + 3 = **115** per link, and it is declared here and nowhere else.
    """
    out: list[tuple[str, tuple[Pt, ...]]] = []
    if crowded(a, b, exits_a, exits_b) and anchor(a) != anchor(b):
        # Three shapes, because two crowded pads are not usually axis-aligned: the straight line
        # when it happens to be 0/45/90 (`shape_ok` drops it otherwise), then the two octile paths
        # `route_geom` already builds — the 45 taken first, and the axis taken first.
        out.append(("centre", (anchor(a), anchor(b))))
        out.append(("centre-Z", octile_path(anchor(a), anchor(b), diagonal_first=True)))
        out.append(("centre-L", octile_path(anchor(a), anchor(b), diagonal_first=False)))
    for ea in exits_a:
        for eb in exits_b:
            for name, pts in link_candidates(ea, eb):
                out.append((name, (anchor(a), ea.at) + pts[1:-1] + (eb.at, anchor(b))))
    return tuple(out)


def _dedupe(pts: tuple[Pt, ...]) -> tuple[Pt, ...]:
    out: list[Pt] = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return tuple(out)


# --- one net ----------------------------------------------------------------------------------------


def joined(spec: SpineSpec, pieces: tuple[Piece, ...]) -> tuple[tuple[tuple[str, str], ...], int]:
    """(the station pairs this copper joins, how many pairs that is) — counted off the **copper**.

    Not off the `_link` calls that returned something. The two disagree in both directions and the S7
    report published both mistakes on one board (findings 5, 8, 15, 17): c3_usb's `VBUS` was recorded
    as making 1 of 6 links while `run` emits no `VBUS` copper at all, and its `3V3` as 4 of 7 while
    the nine pieces it does write join **five** pairs — a link that runs over a third station of its
    own net connects it for free, and no count of successful calls can see that. A union-find over
    the stations and the emitted pieces is what the board does, so it is what gets reported.

    `gap(...) <= 0` is the same touch test `_rib` already uses to decide a zero-length rib, and it is
    KiCad's own rule: tracks that touch are connected, whether or not their endpoints are equal.
    """
    shapes: list[tuple[frozenset[str], object]] = [(t.layers, t.item.copper) for t in spec.stations]
    for p in pieces:
        if p.kind != "seg" or p.b is None:
            continue
        shapes.append((frozenset({p.layer if isinstance(p.layer, str) else p.layer[0]}), track_shape(p.a, p.b, p.w)))
    parent = list(range(len(shapes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            (la, sa), (lb, sb) = shapes[i], shapes[j]
            if sa is None or sb is None or not (la & lb):
                continue
            if gap(sa, sb) <= 0.0:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups: dict[int, list[str]] = {}
    for i, t in enumerate(spec.stations):
        groups.setdefault(find(i), []).append(t.owner)
    pairs = tuple(
        (a, b) for members in (sorted(g) for g in groups.values()) if len(members) > 1 for a, b in zip(members, members[1:])
    )
    return (tuple(sorted(pairs)), len(spec.stations) - len(groups))


def _result(ctx: PatternCtx, spec: SpineSpec, pieces: tuple[Piece, ...], **kw) -> dict:
    """The three numbers every `PatternResult` from this pattern carries, from the copper it wrote."""
    pairs, made = joined(spec, pieces)
    mm = sum(p.mm for p in pieces)
    return {
        "joins": pairs,
        "links": (made, len(spec.stations) - 1),
        "coverage": round(mm / spec.airwire, 4) if spec.airwire > 0 else 0.0,
        **kw,
    }


def run(ctx: PatternCtx, spec: SpineSpec) -> PatternResult:
    """One net: the comb if it fits, the backbone if it does not, and what is left for KRT."""
    if not spec.layer:
        return PatternResult(reason=REASON, net=spec.net, refusal=_refuse_layer(ctx, spec))
    if spec.amps > 0 and spec.width + 1e-9 < spec.trunk_need:
        # The one guard that is about the electrics rather than the geometry, and it is checked
        # before a millimetre is written: a trunk at a class width narrower than IPC asks for the
        # net's own current is exactly the failure `copper.power_ampacity_failures` gates the build
        # on, and writing it here would be pcbc committing it deliberately.
        return PatternResult(reason=REASON, net=spec.net, refusal=_refuse_amps(ctx, spec))
    comb, seen = _comb(ctx, spec)
    if comb["pieces"]:
        return PatternResult(
            reason=REASON,
            net=spec.net,
            pieces=comb["pieces"],
            **_result(ctx, spec, comb["pieces"], candidate=comb["candidate"], tried=comb["tried"], notes=comb["notes"]),
        )
    pieces, _calls, failed, tried = _backbone(ctx, spec)
    notes = (_comb_note(spec, comb),) if pieces else ()
    if not failed:
        return PatternResult(
            reason=REASON,
            net=spec.net,
            pieces=tuple(pieces),
            **_result(ctx, spec, tuple(pieces), candidate=f"backbone of {len(spec.stations)} stations on {spec.layer}", tried=comb["tried"] + tried, notes=notes),
        )
    return PatternResult(
        reason=REASON,
        net=spec.net,
        pieces=tuple(pieces),
        **_result(
            ctx,
            spec,
            tuple(pieces),
            candidate=f"backbone of {len(spec.stations)} stations on {spec.layer}" if pieces else "",
            tried=comb["tried"] + tried,
            notes=notes,
            refusal=_refuse(ctx, spec, comb, seen, failed, tried),
        ),
    )


def _comb_note(spec: SpineSpec, comb: dict) -> str:
    """Why the net came out as a backbone and not as a comb. A `style:` line rather than a refusal,
    because the net **is** routed: a refusal is what pcbc did not write, and inflating the count
    with a form that was tried and beaten would make C.6's exact counts mean something else."""
    widest = max((hi - lo for lo, hi in comb["free"]), default=0.0)
    lanes = f"widest free lane {widest:.3f} mm" if comb["free"] else "no free lane at all"
    return (
        f"style: spine {spec.net}: the comb along {comb['axis']} did not fit "
        f"({len(comb['offsets'])} of at most {MAX_OFFSETS} lane offsets tried, {lanes}), "
        f"so its {len(spec.stations)} stations are linked as a backbone instead"
    )


# --- the refusal ------------------------------------------------------------------------------------


def _other(ctx: PatternCtx, layer: str) -> str:
    layers = [L for L in ctx.scene.layers if L != layer]
    return layers[-1] if layers else layer


def _refuse_layer(ctx: PatternCtx, spec: SpineSpec) -> Refusal:
    where = ", ".join(f"{t.owner} on {'/'.join(sorted(t.layers)) or 'no copper layer'}" for t in spec.stations[:3])
    return Refusal(
        pattern=REASON,
        net=spec.net,
        what=f"{len(spec.stations)} stations",
        clash=None,
        rule="no layer",
        hard=False,
        move="spine "
        + move_line(
            spec.net,
            f"{len(spec.stations)} stations",
            "one trunk at the class width",
            f"its pads share no copper layer ({where}), and R2 puts no via on a spine (rule: no layer)",
            f'Moves: NetReq("{spec.net}", layers=["F.Cu"]) names the layer the trunk runs on.',
            sep="\n  ",
        ),
    )


def _refuse_amps(ctx: PatternCtx, spec: SpineSpec) -> Refusal:
    return Refusal(
        pattern=REASON,
        net=spec.net,
        what=f"{len(spec.stations)} stations",
        clash=None,
        rule="ampacity",
        hard=False,
        move="spine "
        + move_line(
            spec.net,
            f"{len(spec.stations)} stations",
            f"one trunk at the class width, {spec.why}",
            f"the class width {spec.width:g} mm is under the {spec.trunk_need:g} mm IPC-2221/IPC-2152 ask for "
            f"{spec.amps:g} A, so any trunk pcbc wrote would be the neck it exists to remove (rule: ampacity)",
            f'Moves: NetReq("{spec.net}", width={spec.trunk_need:g}) widens the class to what it carries; '
            f'or NetReq("{spec.net}", amps=...) says what it really carries.',
            sep="\n  ",
        ),
    )


def _refuse(ctx: PatternCtx, spec: SpineSpec, comb: dict, seen: list[Clash], failed, tried: int) -> Refusal:
    """B.4's refusal, and this is where `free_intervals` earns its place.

    **Soft** (C.6), by `hop._refuse`'s argument: KRT's own `{class}_nets` step honours a `vias=False`
    or single-layer `NetReq` with the same numbers, so a refused spine costs a route pcbc would have
    drawn wider — never the constraint itself. What it does cost is written down rather than waved
    at: the net falls back to being routed like a signal, which is the `width_power` warning this
    pattern was built to remove.

    The line says the trunk it wanted, the widest lane there actually was and the width that lane
    needed to be, names the two blockers sitting in it worst first, and ends in a `board.py` edit.
    """
    a, b, clash, why = failed[0]
    need = spec.width + 2.0 * ctx.scene.table.between(spec.net, "")[0]
    head = f"the trunk along {comb['axis']} across {comb['span'][0]:g}..{comb['span'][1]:g} does not fit on {spec.layer} at {spec.width:g} mm: "
    if comb["free"]:
        # `free_intervals` was asked at the trunk's own half width, so every lane it returned is
        # already wide enough — what beat them is where they are, and the count and the first one
        # tried are the numbers that say so.
        wide, at = max(((hi - lo, (lo + hi) / 2.0) for lo, hi in comb["free"]))
        head += (
            f"{len(comb['free'])} free lanes there could take it (the widest is {wide:.3f} mm at {ACROSS[comb['axis']]}={at:.3f}) and "
            f"none of the {comb['tried']} tried, nearest the pads' median first, clears"
        )
    else:
        # B.4's own sentence, and its number: the widest gap there is at all, against the width a
        # trunk plus its two clearances needs. Asked at half width zero, so it is the room the board
        # has rather than the room this trunk fits in.
        raw = free_intervals(comb["axis"], comb["span"], trunk_window(ctx.scene.outline, comb["axis"], comb["span"], 0.0), 0.0, spec.net, ctx.scene, spec.layer)
        wide = max((hi - lo for lo, hi in raw), default=0.0)
        at = max(((hi - lo, (lo + hi) / 2.0) for lo, hi in raw), default=(0.0, 0.0))[1]
        head += f"the widest free lane there is {wide:.3f} mm at {ACROSS[comb['axis']]}={at:.3f}, and the trunk needs {need:.3f} mm"
    by_pair: dict[tuple[str, int], Clash] = {}
    for c in seen:
        key = (c.rule, c.item.id)
        if key not in by_pair or (c.need - c.have) > (by_pair[key].need - by_pair[key].have):
            by_pair[key] = c
    worst = sorted(by_pair.values(), key=lambda c: (-(c.need - c.have), c.item.id, c.rule))[:2]
    if worst:
        head += ".\n  In the way of the trunk, worst first: " + "; ".join(
            f"{c.item.label()} at {c.at[0]:g},{c.at[1]:g} leaves {c.have:.3f} mm of the {c.need:.3f} mm {c.why} needs (rule: {c.rule})"
            for c in worst
        )
    links = ", ".join(f"{x.owner}->{y.owner}" for x, y, _c, _w in failed)
    head += (
        f"\n  The backbone linked {len(spec.stations) - 1 - len(failed)} of its {len(spec.stations) - 1} links and left {links} "
        f"({'rule: ' + (clash.rule if clash is not None else why)})"
    )
    head += f"\n  Tried {comb['tried']} trunk offsets of at most {MAX_OFFSETS} and {tried} backbone candidates of at most {len(spec.stations) - 1} x (4 x 4 x {MAX_LINKS} + 3)"
    toward = _toward(anchor(a)[0] - anchor(b)[0], anchor(a)[1] - anchor(b)[1])
    return Refusal(
        pattern=REASON,
        net=spec.net,
        what=f"{a.owner}->{b.owner}",
        clash=clash,
        rule=(clash.rule if clash is not None else why),
        hard=False,
        move="spine "
        + move_line(
            spec.net,
            f"{len(spec.stations)} stations from {spec.stations[0].owner} to {spec.stations[-1].owner}",
            f"one trunk at {spec.width:g} mm ({spec.why})",
            head,
            f'Moves: Place("{a.ref}", toward="{toward}") opens the lane the trunk wants; '
            f'or NetReq("{spec.net}", layers=["{_other(ctx, spec.layer)}"]) takes the spine to the other side; '
            f"or NetReq(\"{spec.net}\", amps={max(spec.amps, 0.0):g}) narrows the trunk to what it really carries.",
            sep="\n  ",
        ),
    )


def _toward(dx: float, dy: float) -> str:
    """`route_checks._quantise`'s answer and the vocabulary `Place(toward=)` accepts, in KiCad's
    y-down frame."""
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "down" if dy >= 0 else "up"
