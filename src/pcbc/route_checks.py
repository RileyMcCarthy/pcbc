"""Route-aware placement checks (docs/r1-design.md section F): what the placed board already says
about the copper before a track exists.

Called from `place.place_job` after `layout_report`. Pure and deterministic: refs sorted, pads in
footprint order, no copper read. Every message names refs, pins and numbers and ends in an edit to
`board.py`. Distances are pad edge to pad edge (the AABB gap of `Pad.w/h` turned by the footprint's
rotation) unless a check says otherwise. Each check is one function over `Ctx`.

F.1 airwire length and skew (R-L4)      `check_airwires`
F.2 chain order, no stubs (R-X4, R-A2)  `check_chains`
F.3 corridor for wide tracks (R-I4)     `check_corridors`
F.4 decap loop and hot loop (R-D1, R-D2) `check_loops`
F.5 keep-away (R-D3, R-A1)              `check_keep_away`
F.6 reference plane (R-Z3)              `check_reference`
F.7 isolation line (R-V2)               `check_isolation`
F.8 ground bridge (docs/stitch-plan.md) `check_bridge`
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .compile import CompiledJob
from .copper import _rotate
from .constraints import DEFAULT_LOOP_MM2, ConstraintSet, Constraint, IsolationSpec
from .css import Rect, rotate_local_bounds
from .layout import content_rect, resolve_keepout, resolve_regions
from .model import Design, KeepoutSpec
from .pcb_place import GAP, Foot, Pad, _board_of, _pin_name, lane_rules
from .stackup import is_outer

WIDE_MM = 0.4  # a class this wide gets the corridor test (F.3)
CHAIN_HOP_MM = 8.0  # a chain hop the placement made (pcb_place._REACH): its corridor is checked; a longer one is the router's
CORRIDOR_GRID_MM = 0.1
CORRIDOR_COARSE_MM = 0.2  # when the width is over 1 mm: the escape hatch of H.8
CORRIDOR_COARSE_OVER_MM = 1.0
_DIRS = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0)}


@dataclass
class Ctx:
    design: Design
    job: CompiledJob
    cs: ConstraintSet
    feet: dict[str, Foot]  # parsed, world-posed, pads' nets bound (as layout_report builds them)
    content: Rect
    regions: dict[str, Rect]
    keepouts: list[KeepoutSpec]
    pads_on: dict[str, list[tuple[str, str, float, float, float, float]]]  # net -> (ref, num, x, y, w, h) world
    keepout_boxes: list[tuple[str, tuple[float, float, float, float], tuple[str, ...]]] = field(default_factory=list)
    pad_layers: dict[str, list[frozenset[str]]] = field(default_factory=dict)  # ref -> per Foot.pads entry, its copper layers
    insts: dict = field(default_factory=dict)  # ref -> Instance


def build_ctx(design: Design, job: CompiledJob, posed) -> Ctx:
    """The placed parts as `layout_report` reads them (`posed`: `foot_native.PosedFoot`s, each
    `.kicad_mod` posed by its `Pose`; `pcb_place.posed_feet_of` is the one reading), plus what the
    route checks need. No board text is read (`docs/direction.md` §1)."""
    from .pcb_place import posed_feet_of

    if getattr(job, "outline", None) is None:
        # A derived board's outline is set by placement (R) and the fit (O): with none the escape grid would span the frame.
        raise ValueError("the board has no Outline() and this job was not placed: a derived outline is set by the place stage (docs/board-outline.md B.3)")
    board = _board_of(job)
    content = content_rect(board)
    regions = resolve_regions(board, job.regions)
    feet = posed_feet_of(job, posed)
    pad_layers: dict[str, list[frozenset[str]]] = {pf.ref: _pad_layers(pf) for pf in posed}
    pads_on: dict[str, list[tuple[str, str, float, float, float, float]]] = {}
    for ref in sorted(feet):
        f = feet[ref]
        for p in f.pads:
            if p.net and p.num:
                x0, y0, x1, y1 = _pad_box(f, p)
                pads_on.setdefault(p.net, []).append((ref, p.num, (x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0))
    keepout_boxes = [(ko.name, tuple(resolve_keepout(ko, board, regions)), tuple(ko.no)) for ko in job.keepouts]
    cs = job.constraints
    if cs is None:
        from .constraints import compile_constraints

        cs = compile_constraints(design)
    return Ctx(
        design=design,
        job=job,
        cs=cs,
        feet=feet,
        content=content,
        regions=regions,
        keepouts=list(job.keepouts),
        pads_on=pads_on,
        keepout_boxes=keepout_boxes,
        pad_layers=pad_layers,
        insts={inst.ref: inst for inst in design.instances},
    )


def route_aware_report(design: Design, job: CompiledJob, posed) -> tuple[list[str], list[str]]:
    """(moves, style notes) from the placed parts (`posed`): sections F.1 to F.7, in that order."""
    ctx = build_ctx(design, job, posed)
    moves: list[str] = []
    notes: list[str] = []
    moves += check_airwires(ctx)
    moves += check_chains(ctx)
    moves += check_corridors(ctx)
    moves += check_station_escapes(ctx)
    m, n = check_loops(ctx)
    moves += m
    notes += n
    moves += check_keep_away(ctx)
    m, n = check_reference(ctx)
    moves += m
    notes += n
    moves += check_isolation(ctx)
    moves += check_bridge(ctx)
    return moves, notes


# ---------------------------------------------------------------- geometry helpers


def _pad_layers(pf) -> list[frozenset[str]]:
    """Per pad (in `foot_native.foot_of`'s order, which is the `.kicad_mod`'s): the copper layers it sits
    on. A through hole is on every copper layer; an SMD pad on the layers its `(layers ...)` names, or
    its footprint's side when it names none."""
    side = "B.Cu" if pf.layer == "B.Cu" else "F.Cu"
    out: list[frozenset[str]] = []
    for sp in pf.lib.pads:
        if "thru_hole" in sp.kind:
            out.append(frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"}))
            continue
        layers = set()
        for tok in sp.layers.replace('"', "").split():
            if tok.endswith(".Cu"):
                layers.add(tok)
            elif tok == "*.Cu":
                layers.update({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"})
        out.append(frozenset(layers) if layers else frozenset({side}))
    return out


def _pad_box(f: Foot, p: Pad) -> tuple[float, float, float, float]:
    """World AABB of a pad: `Pad.w/h` turned by the footprint's rotation."""
    x, y = f.pad_world(p)
    x0, y0, x1, y1 = rotate_local_bounds(-p.w / 2.0, -p.h / 2.0, p.w / 2.0, p.h / 2.0, f.rot)
    return (x + x0, y + y0, x + x1, y + y1)


def _box_of(site: tuple[str, str, float, float, float, float]) -> tuple[float, float, float, float]:
    _ref, _num, x, y, w, h = site
    return (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)


def _gap(a, b) -> float:
    """Edge-to-edge distance of two AABBs (0 when they overlap)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _seg_seg_dist(p1, p2, q1, q2) -> float:
    if _segs_cross(p1, p2, q1, q2):
        return 0.0
    return min(_pt_seg_dist(p1, q1, q2), _pt_seg_dist(p2, q1, q2), _pt_seg_dist(q1, p1, p2), _pt_seg_dist(q2, p1, p2))


def _pt_seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segs_cross(p1, p2, q1, q2) -> bool:
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0


def _seg_rect_dist(a, b, rect) -> float:
    """Distance from segment a-b to an AABB; 0 when the segment enters it."""
    x0, y0, x1, y1 = rect
    if x0 <= a[0] <= x1 and y0 <= a[1] <= y1:
        return 0.0
    if x0 <= b[0] <= x1 and y0 <= b[1] <= y1:
        return 0.0
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return min(_seg_seg_dist(a, b, corners[i], corners[(i + 1) % 4]) for i in range(4))


def _quantise(dx: float, dy: float) -> str:
    """The nearest of right/left/down/up (KiCad y grows downward)."""
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "down" if dy >= 0 else "up"


def _g(x: float) -> str:
    return f"{x:g}"


def _mst_edges(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """Prim's tree over pad centres (the same tree `copper_bar.airwire_mm` sums), as index pairs."""
    n = len(points)
    if n < 2:
        return []
    in_tree = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0.0
    edges: list[tuple[int, int]] = []
    for _ in range(n):
        i = min((k for k in range(n) if not in_tree[k]), key=lambda k: (best[k], points[k], k))
        in_tree[i] = True
        if parent[i] >= 0:
            edges.append((parent[i], i))
        for k in range(n):
            if not in_tree[k]:
                d = math.hypot(points[k][0] - points[i][0], points[k][1] - points[i][1])
                if d < best[k]:
                    best[k], parent[k] = d, i
    return edges


def _mst_mm(points: list[tuple[float, float]]) -> float:
    return sum(math.hypot(points[a][0] - points[b][0], points[a][1] - points[b][1]) for a, b in _mst_edges(points))


def _edge_mst_mm(boxes: list[tuple[float, float, float, float]]) -> float:
    """The copper a net needs: Prim's tree over the pads' edge-to-edge gaps (a track ends at a pad's
    edge; a 6 mm inductor's pad centre is not copper the track must reach)."""
    n = len(boxes)
    if n < 2:
        return 0.0
    in_tree = [False] * n
    best = [math.inf] * n
    best[0] = 0.0
    total = 0.0
    for _ in range(n):
        i = min((k for k in range(n) if not in_tree[k]), key=lambda k: (best[k], boxes[k], k))
        in_tree[i] = True
        total += best[i]
        for k in range(n):
            if not in_tree[k]:
                d = _gap(boxes[i], boxes[k])
                if d < best[k]:
                    best[k] = d
    return total


def _structural(ctx: Ctx, ref: str) -> bool:
    """True when this part's position is the board's structure rather than a choice to revisit: a
    connector on an edge, or a multi-pin IC or module placed by CSS. Asking the AI to re-relate
    one of those is asking it to undo the floorplan; a two-pad passive pinned by CSS is fair game."""
    spec = next((p for p in ctx.job.places if p.ref == ref), None)
    if spec is None or spec.to:
        return False
    foot = ctx.feet.get(ref)
    pads = len([p for p in foot.pads if p.num]) if foot else 0
    return bool(spec.edge) or pads > 4


def _outliers(sites: list) -> list[tuple[float, str]]:
    """(distance to the nearest pad of another part, ref) for every part on the net, worst first."""
    out: dict[str, float] = {}
    for i, (ref, _num, x, y, _w, _h) in enumerate(sites):
        others = [(x2, y2) for j, (r2, _n2, x2, y2, _w2, _h2) in enumerate(sites) if j != i and r2 != ref]
        if not others:
            continue
        near = min(math.hypot(x - x2, y - y2) for x2, y2 in others)
        out[ref] = max(out.get(ref, 0.0), near)
    return [(d, r) for r, d in out.items()]


def _pin(ctx: Ctx, ref: str, num: str) -> str:
    inst = ctx.insts.get(ref)
    return _pin_name(inst, num) if inst is not None else num


def _refpin_pad(ctx: Ctx, spec: str) -> tuple[str, Pad] | None:
    """`"REF.PIN"` (a pin name as Place(to=) takes it, or a pad number) -> (ref, the Foot's pad)."""
    ref, pin = spec.split(".", 1)
    f = ctx.feet.get(ref)
    if f is None:
        return None
    inst = ctx.insts.get(ref)
    nums: tuple[str, ...] = (pin,)
    if inst is not None and pin in inst.part.pins:
        nums = inst.part.pins[pin].pads
    for num in nums:
        for p in f.pads:
            if p.num == num:
                return ref, p
    return None


def _ics(ctx: Ctx) -> list[str]:
    return sorted(ref for ref, f in ctx.feet.items() if len([p for p in f.pads if p.num]) > 2)


def _ic_pad_on(ctx: Ctx, net: str, exclude: str = "") -> tuple[str, str] | None:
    """(ref, pin name) of the net's pad on the part with the most pads (the IC), if any."""
    best = None
    for ref in _ics(ctx):
        if ref == exclude:
            continue
        f = ctx.feet[ref]
        for p in f.pads:
            if p.net == net and p.num:
                n = len(f.pads)
                if best is None or n > best[0]:
                    best = (n, ref, p.num)
                break
    return (best[1], _pin(ctx, best[1], best[2])) if best else None


def _side_of_pin(f: Foot, p: Pad) -> str:
    """The side of a footprint a pad sits on: where a part attached to that pin goes."""
    cx, cy = f.center()
    px, py = f.pad_world(p)
    return _quantise(px - cx, py - cy)




# ---------------------------------------------------------------- F.1 airwires


def _airwire_src(ctx: Ctx, c: Constraint) -> str:
    if c.req_index >= 0 and ctx.design.netreqs[c.req_index].max_mm is not None:
        return f"NetReq line {c.line}"
    if c.pair is not None:
        # A NetReq naming a Pair's two nets gives them its max_mm= (refutation one-home r1, defect 1).
        import fnmatch

        r = next((r for r in ctx.design.netreqs if r.max_mm is not None and any(fnmatch.fnmatchcase(c.net, x) for x in r.nets)), None)
        if r is not None:
            return f"NetReq line {r.line}"
    return f"preset {c.kind}"


def _airwire_budget(ctx: Ctx, c: Constraint) -> tuple[float, str] | None:
    """The pad-to-pad MST this net's placement is allowed, in mm, and where the number came from.

    Two sources, and **neither is a per-kind guess**. The first is the declared one: `NetReq(max_mm=)`
    or the preset's own `airwire_mm`. The second is a derivation that invents nothing — the MST over a
    net's pads is a **lower bound** on the copper that connects them, which is `docs/r1-design.md`
    line 830 read from the other side ("`max_mm` never becomes a length rule (the maze path is longer
    than the airwire)"), so a placement whose MST already exceeds the net's **routed** length budget
    cannot be routed inside that budget by any router. `length_max_mm` is therefore a placement budget
    one stage early, for free.

    That second source is what gives a fast kind a number without one being made up, wherever the
    board or the compiler already has one: `i2c`'s routed budget is fully derived today —
    `(pf_max - 10 pF x pins) / C_per_mm` from UM10204 rev 7 section 7.1 and the exact per-mm
    capacitance of the class width on the board's own stackup (`constraints._numbers`,
    `stackup.i2c_max_mm`) — and `usb_hs` and `spi` have one whenever the board writes
    `NetReq(length_mm=)`.
    """
    if c.airwire_max_mm is not None:
        return c.airwire_max_mm, _airwire_src(ctx, c)
    if c.length_max_mm is not None:
        src = c.length_max_mm.source
        inner = f"{src.formula} {src.ref}".strip()
        return c.length_max_mm.value, f"routed length {inner}, and the MST is a lower bound on it"
    return None


FAST_KINDS = ("clock", "i2c")
"""The net kinds named after a signalling standard; a Pair or Bus member is fast too (`_unbudgeted_fast`).

The kinds named after a signalling standard. Every one of them carries `PRESETS[kind].airwire_mm
is None`, and that is not fixed here.

Measured 2026-09-22, `PRESETS`: `switch_node` 8.0, `feedback` 15.0, `analog` 25.0, `sense` 25.0 —
and `None` for all four of these, for `generic` and for `power`. **The only nets on the five boards
that no placement budget has ever looked at are the fast ones.** On fresh placements of the five,
node's `USB_DP` spans **39.124 mm** of pad-to-pad MST over 5 pads and `USB_DN` **38.971 mm** on a
75.000 mm board diagonal; c3_usb's are 20.747 and 20.645 on a 50.000 mm diagonal. Nothing asked, so
node's router was handed the two ends of a 42.7 mm centre-to-centre span and drew the elbow that
started this.

**They still get no preset number, because neither candidate derivation survives being worked out.**

*From the signal.* The arithmetic exists and is already cited in this repo — `stitch.knee_ghz`
(`KNEE_OF_RISE / t_rise`, Johnson & Graham) and `cavity_velocity` — but it needs an edge rate, and
`Constraint.rise_ps` is documented as "carried, never derived, and never defaulted":
`docs/stitch-plan.md` section 8 item 3 refused four per-kind `rise_ps` defaults and
`ConstraintSet.fastest_edge` returns `None` on all five boards because none declares one. Supplying
one per kind here is that refusal reversed under a different name. It is also arithmetically
unstable: at a **hypothetical** 500 ps edge — the illustration, not a number this module ships —
the microstrip velocity this repo computes is **170.19 mm/ns** on node's
`jlcpcb_4l_1oz` F.Cu over In1.Cu (h 0.2104, `microstrip` eeff 3.1028) and **172.56 mm/ns** on
c3_usb's `jlcpcb_2l_1oz` F.Cu over B.Cu (h 1.53, eeff 3.0183), so the electrically-short length is
**14.18 mm** at the `t_r*v/6` convention and **42.55 mm** at `t_r*v/2` — a **3x** spread decided by
which textbook divisor is quoted, with node's 39.124 failing one and passing the other. And the
criterion is the wrong one anyway: both pair nets declare `z_diff_ohm=90`, so they are transmission
lines **by construction**, and "shorter than this and you may treat it as lumped" is not "longer than
this is a defect".

*From the board.* Refuted by measurement. If the shipped numbers were a fraction of the board
diagonal they would move with the board and they do not: 8.0 is 0.170 of blinky's and buck's 47.170,
0.160 of c3_usb's 50.000, 0.107 of node's 75.000 and 0.150 of ds2's 53.424 — a 1.59x spread across
five boards for one constant. At a **fixed** board the three numbers stand at 8 : 15 : 25, so what
separates them is the kind and not the geometry, which is the hypothesis' own claim inverted. It is
also the wrong variable: a hot loop's budget is set by di/dt and a feedback node's by pickup, and
neither says a bigger board may have a worse one. The one board-sized number in the repo is a
`NetReq(max_mm=30)` **declaration** on ds2, which `docs/copper-plan.md` records as a 20 that "was a
guess that failed on a 47 mm board" — evidence that the board's author scales to the board, through
the mechanism that already exists for it.

So a fast kind's budget comes from `_airwire_budget` when the board or the compiler has one, and
from `_unbudgeted_fast` — a printed move — when it does not.
"""


def _unbudgeted_fast(ctx: Ctx) -> list[str]:
    """F.1, second half: a fast net that **no budget reaches**, with the span nobody checked.

    The first half compares an MST to a number. This one fires where there is no number: a net of a
    `FAST_KINDS` kind whose `NetReq` gives neither `max_mm=` nor `length_mm=`, and whose preset gives
    no `airwire_mm` either. Refusing to invent the number is only half a refusal; the other half is
    saying so **with the measurement attached**, which is the house rule that a refusal is a move
    ending in a `board.py` edit.

    It is deliberately not a `Place()` move. The defect a missing budget names is a missing
    declaration, and pcbc cannot know how long an ESP32-C3's USB run may be without being told what
    drives it. The line names the subjects that may carry a length (`docs/physics-subjects.md` E21: a
    Path's `length_mm=`), as facts.

    Measured 2026-09-22 on fresh placements: it fires on **four nets of two boards** — c3_usb's
    `USB_DP`/`USB_DN` (20.747 / 20.645 mm) and node's (39.124 / 38.971 mm). blinky, buck and ds2
    declare no fast kind at all and print nothing.
    """
    out: list[str] = []
    for c in ctx.cs.constraints:
        group = c.group if c.group is not None and c.group.kind == "bus" else None
        if (c.kind not in FAST_KINDS and c.pair is None and group is None) or c.airwire_max_mm is not None or c.length_max_mm is not None:
            continue
        if any(p.length_mm is not None for p in c.paths):
            continue
        sites = ctx.pads_on.get(c.net, [])
        if len(sites) < 2:
            continue
        mst = _edge_mst_mm([_box_of(s) for s in sites])
        lines = [f"Path line {p.line}" for p in c.paths]
        # docs/physics-subjects.md PR 6: a pair's length may also be written once on its Pair line.
        if c.pair is not None and c.pair.line:
            lines.append(f"Pair line {c.pair.line}")
        if lines:
            where = f"no length_mm= on {' or '.join(lines)}"
        else:
            where = f"no Path of {c.net} writes length_mm="
        out.append(f"{c.net}: {mst:.1f} mm of airwire (MST of {len(sites)} pads, edge to edge) and no budget checks it: {where}")
    return out


def check_airwires(ctx: Ctx) -> list[str]:
    """F.1: the MST of a net's pads against `max_mm`; the spread of a pair's or bus's member MSTs
    against the skew budget. The router can only add length."""
    out: list[str] = []
    cs = ctx.cs
    for c in cs.constraints:
        budget = _airwire_budget(ctx, c)
        if budget is None:
            continue
        limit, limit_src = budget
        sites = ctx.pads_on.get(c.net, [])
        if len(sites) < 2:
            continue
        mst = _edge_mst_mm([_box_of(s) for s in sites])
        if mst <= limit + 1e-9:
            continue
        # The outlier: the pad farthest from its nearest other pad; it belongs next to the IC's pin.
        worst = None
        for i, (ref, num, x, y, _w, _h) in enumerate(sites):
            near = min(math.hypot(x - x2, y - y2) for j, (r2, _n2, x2, y2, _w2, _h2) in enumerate(sites) if j != i and r2 != ref) if any(r2 != ref for r2, *_ in sites) else 0.0
            if worst is None or near > worst[0]:
                worst = (near, ref, num)
        ref = worst[1] if worst else sites[0][0]
        if _structural(ctx, ref):
            # The module and the edge connector are the floorplan: naming one asks the AI to undo
            # what it placed on purpose. Take the farthest part that is not structural instead.
            movable = sorted(((near, r) for near, r in _outliers(sites) if not _structural(ctx, r)), reverse=True)
            if movable:
                ref = movable[0][1]
        ic = _ic_pad_on(ctx, c.net, exclude=ref)
        if ic is None:
            other = next((s for s in sites if s[0] != ref), None)
            target = f"{other[0]}.{_pin(ctx, other[0], other[1])}" if other else ""
        else:
            target = f"{ic[0]}.{ic[1]}"
        out.append(
            f"{c.net}: {mst:.1f} mm of airwire (MST of {len(sites)} pads, edge to edge) over max_mm={_g(limit)} ({limit_src}); "
            f"the route is longer still; {ref} is the pad farthest from the rest" + (f" ({_attached(ctx, ref)})" if ref in ctx.feet else "") + (f"; {target} is the nearest IC pin on {c.net}" if ic is not None else "")
        )
    out += _unbudgeted_fast(ctx)
    # pairs and buses: member MSTs must agree within the skew budget
    seen: set[tuple[str, ...]] = set()
    for c in cs.constraints:
        members: tuple[str, ...]
        if c.pair is not None and c.pair.partner:
            members = (c.net, c.pair.partner)
            if c.req_index >= 0:
                req_nets = ctx.design.netreqs[c.req_index].nets
                if len(req_nets) == 2 and set(req_nets) == set(members):
                    members = tuple(req_nets)
            budget = c.pair.skew_mm.value
            src = c.pair.skew_mm.source
            source = f"{src.formula} {src.ref}" if src.formula != "preset" else f"preset {src.ref}"
        elif c.group is not None and c.group.kind == "bus" and c.group.match_mm is not None:
            members = c.group.members
            budget = c.group.match_mm
            source = c.group.source
        else:
            continue
        key = tuple(sorted(members))
        if key in seen:
            continue
        seen.add(key)
        msts = {}
        for m in members:
            sites = ctx.pads_on.get(m, [])
            if len(sites) >= 2:
                msts[m] = _mst_mm([(x, y) for _r, _n, x, y, _w, _h in sites])
        if len(msts) < 2:
            continue
        longest = max(members, key=lambda m: (msts.get(m, -1.0), m))
        shortest = min(members, key=lambda m: (msts.get(m, math.inf), m))
        spread = msts[longest] - msts[shortest]
        if spread <= budget + 1e-9:
            continue
        out.append(
            f"{longest}/{shortest}: airwires {msts[longest]:.1f} and {msts[shortest]:.1f} mm differ by {spread:.1f} mm, "
            f"over the skew budget {_g(budget)} mm ({source})"
        )
    out += _path_airwires(ctx)
    return out


def _station_xy(ctx: Ctx, net: str, station: tuple[str, ...]) -> tuple[float, float] | None:
    for pid in station:
        ref, _, num = pid.partition(".")
        for r, n, x, y, _w, _h in ctx.pads_on.get(net, []):
            if r == ref and n == num:
                return (x, y)
    return None


def _path_airwires(ctx: Ctx) -> list[str]:
    """F.1 for paths (`docs/net-path-physics.md` §4.5): each path's station-to-station airwire against its
    `Path(length_mm=)` or its kind's preset (the airwire is a lower bound on the routed length, so a placement
    already over it cannot be routed inside it). A warning stating the facts, never a pose."""
    out: list[str] = []
    for c in ctx.cs.constraints:
        for p in c.paths:
            if p.length_mm is None:
                continue
            pts = [_station_xy(ctx, c.net, st) for st in p.stations]
            if any(pt is None for pt in pts):
                continue
            hops = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]  # type: ignore[arg-type]
            total = sum(hops)
            if total <= p.length_mm + 1e-9:
                continue
            far = max(range(len(hops)), key=lambda i: (hops[i], -i)) + 1
            who = p.written[far]
            ref = who.split(".", 1)[0]
            lk = p.links[0]
            lay = lk.layers[0] if lk.layers else "F.Cu"
            req = next((q for q in ctx.design.paths if q.net == c.net and q.line == p.line), None)
            if req is not None and req.length_mm is not None:
                src = f"length_mm={_g(p.length_mm)} on that line"
            elif c.pair is not None and c.pair.length_mm is not None:
                src = f"length_mm={_g(p.length_mm)} on Pair line {c.pair.line}"
            else:
                src = f"kind {p.kind}'s {_g(p.length_mm)} mm"
            out.append(
                f"{c.net} path {' -> '.join(p.written)} (Path line {p.line}): {total:.1f} mm between stations, over {src}; "
                f"{_g(lk.width_on(lay))} mm wide on {lay}, clearance {_g(c.clearance_mm.value)} mm; {who} is the farthest station ({_attached(ctx, ref)})"
            )
    return out




# ---------------------------------------------------------------- F.2 chains


def _corridor_half(c: Constraint) -> float:
    return c.width_mm.value / 2.0 + c.clearance_mm.value


def _corridor_hits(ctx: Ctx, net: str, half: float, a: tuple[str, Pad], b: tuple[str, Pad]) -> list[tuple[float, str, str]]:
    """Other-net pads whose AABB lies in the corridor a-b (the endpoints' own footprints are their
    land, not a stub): (distance along the corridor, "REF.num", net)."""
    fa, fb = ctx.feet[a[0]], ctx.feet[b[0]]
    pa, pb = fa.pad_world(a[1]), fb.pad_world(b[1])
    hits: list[tuple[float, str, str]] = []
    for ref in sorted(ctx.feet):
        if ref in (a[0], b[0]):
            continue
        f = ctx.feet[ref]
        nearest = None  # one line per part: its pad nearest the feed line
        for p in f.pads:
            if not p.num or p.net == net:
                continue
            box = _pad_box(f, p)
            d = _seg_rect_dist(pa, pb, box)
            if d < half - 1e-9 and (nearest is None or d < nearest[0] - 1e-9):
                x, y = f.pad_world(p)
                t = (x - pa[0]) * (pb[0] - pa[0]) + (y - pa[1]) * (pb[1] - pa[1])
                nearest = (d, t, f"{ref}.{p.num}", p.net)
        if nearest is not None:
            hits.append(nearest[1:])
    hits.sort()
    return hits


def check_chains(ctx: Ctx) -> list[str]:
    """F.2: a `Path`'s pads project in order along the feed and no other net's pad sits in the
    corridor between consecutive pads; a usb_hs or sense net with 3+ pads and no Path must have its
    pads on one line, else the message says to write one."""
    out: list[str] = []
    cs = ctx.cs
    chained = {ch.net for ch in ctx.design.paths}
    for ch in ctx.design.paths:
        c = cs.by_net(ch.net)
        if c is None:
            continue
        pads = [_refpin_pad(ctx, spec) for spec in ch.pads]
        if any(p is None for p in pads):
            continue  # compile refused it already
        pts = [ctx.feet[r].pad_world(p) for r, p in pads]  # type: ignore[misc]
        x0, y0 = pts[0]
        xn, yn = pts[-1]
        length = math.hypot(xn - x0, yn - y0)
        if length < 1e-9:
            continue
        dx, dy = (xn - x0) / length, (yn - y0) / length
        ts = [round((px - x0) * dx + (py - y0) * dy, 4) + 0.0 for px, py in pts]
        ordered = True
        for i in range(len(ts) - 1):
            if ts[i] >= ts[i + 1] - 1e-9:
                ordered = False
                # docs/net-path-physics.md §4.5: a statement and the line to edit, never a pose.
                ref_i, ref_j = pads[i][0], pads[i + 1][0]  # type: ignore[index]
                placed = "; ".join(dict.fromkeys(_attached(ctx, r) for r in (ref_i, ref_j)))
                out.append(
                    f"Path line {ch.line}: {ch.pads[i]} projects after {ch.pads[i + 1]} along the feed (t = {ts[i]:.1f} vs {ts[i + 1]:.1f} mm), "
                    f"and the stations are fed in the order Path line {ch.line} writes them ({placed})"
                )
                break
        if not ordered:
            continue
        for i in range(len(pads) - 1):
            if math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) > CHAIN_HOP_MM + 1e-9:
                continue  # a hop across the board: the router detours; only a placed hop must be straight
            # The corridor at the link's own width (§4.5), else the net's.
            lk = cs.link(ch.net, f"{pads[i][0]}.{pads[i][1].num}", f"{pads[i + 1][0]}.{pads[i + 1][1].num}") if hasattr(cs, "link") else None  # type: ignore[index]
            half = (lk.widest() / 2.0 + c.clearance_mm.value) if lk is not None else _corridor_half(c)
            for _t, who, net in _corridor_hits(ctx, ch.net, half, pads[i], pads[i + 1]):  # type: ignore[arg-type]
                ref = who.split(".", 1)[0]
                out.append(f"{ch.net} path (Path line {ch.line}): {who} ({net or 'no net'}) lies in the corridor between {ch.pads[i]} and {ch.pads[i + 1]} ({_attached(ctx, ref)})")
    # implicit chains: one pad per part, on a line
    for c in cs.constraints:
        if c.kind != "sense" or c.net in chained:  # a pair member names its Path or is refused (E13, PR 6)
            continue
        sites = ctx.pads_on.get(c.net, [])
        first: dict[str, tuple[str, str, float, float]] = {}
        for ref, num, x, y, _w, _h in sites:
            first.setdefault(ref, (ref, num, x, y))
        if len(first) < 3:
            continue
        pads = _implicit_chain(ctx, c.net, sorted(first))
        (_r0, _n0, ax, ay), (_rn, _nn, bx, by) = pads[0], pads[-1]
        half = _corridor_half(c)
        stub = any(_pt_seg_dist((x, y), (ax, ay), (bx, by)) > half + 1e-9 for _r, _n, x, y in pads[1:-1])
        if not stub:
            continue
        names = ", ".join(f"{r}.{n}" for r, n, _x, _y in pads)
        # No order chosen for the author (§4.5): the order the placement suggests is a pose.
        out.append(f"{c.net} has {len(pads)} pads ({names}): a third pad off the line is a stub, and no Path gives their feed order")
    return out


def _place_spec(ctx: Ctx, ref: str):
    return next((p for p in ctx.design.places if p.ref == ref), None)


def _place_ref(ctx: Ctx, ref: str) -> str:
    """` (layout.core.py:79)`: where the part the message names is placed, when board.py says."""
    spec = _place_spec(ctx, ref)
    line = getattr(spec, "line", 0) if spec is not None else 0
    return f" (layout.core.py:{line})" if line else ""


def _attached(ctx: Ctx, ref: str) -> str:
    """Where `ref`'s position comes from, as the line that wrote it: `layout.core.py:75 attaches C_OUT1 to
    L1.2`, `layout.core.py:70 puts J_IN on the left edge`, `layout.core.py:66 places U1`, or `C9 has no Place()
    (the generator placed it)`. Refutation round 2: this said `no Place() attaches U1` of an anchor the
    author placed absolutely, instead of naming the line that did."""
    spec = _place_spec(ctx, ref)
    if spec is None:
        return f"{ref} has no Place() (the generator placed it)"
    line = getattr(spec, "line", 0)
    who = f"layout.core.py:{line}" if line else "its Place()"
    if getattr(spec, "to", None):
        return f"{who} attaches {ref} to {spec.to}"
    if getattr(spec, "edge", None):
        return f"{who} puts {ref} on the {spec.edge} edge"
    return f"{who} places {ref}"


def _implicit_chain(ctx: Ctx, net: str, refs: list[str]) -> list[tuple[str, str, float, float]]:
    """One pad per part in line order: the parts sorted along the first principal axis of their
    centres, a connector first when one ends the line (the feed comes from it), and on each part the
    pad on the net nearest the pad before it."""
    centres = [ctx.feet[r].center() for r in refs]
    order = _principal_order(centres)
    conns = [k for k, i in enumerate(order) if refs[i].startswith("J")]
    if len(conns) == 1 and conns[0] not in (0, len(order) - 1):
        # The feed comes from the connector, so it is an end of the chain, not a stop in the
        # middle: move it to the nearer end and keep the rest in their order.
        k = conns.pop()
        i = order.pop(k)
        order = ([i] + order) if k <= len(order) / 2 else (order + [i])
    elif order and refs[order[-1]].startswith("J") and not refs[order[0]].startswith("J"):
        order = order[::-1]
    out: list[tuple[str, str, float, float]] = []
    prev: tuple[float, float] | None = None
    for k, i in enumerate(order):
        ref = refs[i]
        sites = [s for s in ctx.pads_on[net] if s[0] == ref]
        if prev is None:
            nxt = centres[order[k + 1]] if k + 1 < len(order) else centres[i]
            site = min(sites, key=lambda s: (math.hypot(s[2] - nxt[0], s[3] - nxt[1]), s[1]))
        else:
            site = min(sites, key=lambda s: (math.hypot(s[2] - prev[0], s[3] - prev[1]), s[1]))
        out.append((ref, site[1], site[2], site[3]))
        prev = (site[2], site[3])
    return out


def _principal_order(pts: list[tuple[float, float]]) -> list[int]:
    """Indices sorted along the first principal axis of the points (the line they lie closest to)."""
    n = len(pts)
    mx = sum(x for x, _y in pts) / n
    my = sum(y for _x, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _y in pts)
    syy = sum((y - my) ** 2 for _x, y in pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    ux, uy = math.cos(theta), math.sin(theta)
    if ux < 0 or (abs(ux) < 1e-12 and uy < 0):
        ux, uy = -ux, -uy
    return sorted(range(n), key=lambda i: ((pts[i][0] - mx) * ux + (pts[i][1] - my) * uy, pts[i], i))


# ---------------------------------------------------------------- F.3 corridors


class _Grid:
    """A bitmap over the content rect: 0 free, 1 obstacle, 2+ a flooded component's label."""

    def __init__(self, content: Rect, grid: float):
        self.x0, self.y0, self.grid = content.x0, content.y0, grid
        self.w = max(1, math.ceil(round(content.w / grid, 6)))
        self.h = max(1, math.ceil(round(content.h / grid, 6)))
        self.rows = [bytearray(self.w) for _ in range(self.h)]
        self.next_label = 2

    def cells(self, box, dil: int = 0) -> tuple[int, int, int, int] | None:
        i0 = math.floor(round((box[0] - self.x0) / self.grid, 6)) - dil
        i1 = math.ceil(round((box[2] - self.x0) / self.grid, 6)) + dil
        j0 = math.floor(round((box[1] - self.y0) / self.grid, 6)) - dil
        j1 = math.ceil(round((box[3] - self.y0) / self.grid, 6)) + dil
        i0, i1 = max(0, i0), min(self.w, i1)
        j0, j1 = max(0, j0), min(self.h, j1)
        if i0 >= i1 or j0 >= j1:
            return None
        return i0, j0, i1, j1

    def fill(self, box, value: int, dil: int = 0) -> None:
        c = self.cells(box, dil)
        if c is None:
            return
        i0, j0, i1, j1 = c
        run = bytes([value]) * (i1 - i0)
        for j in range(j0, j1):
            self.rows[j][i0:i1] = run

    def label_in(self, box) -> int:
        c = self.cells(box)
        if c is None:
            return -1
        i0, j0, i1, j1 = c
        for j in range(j0, j1):
            m = max(self.rows[j][i0:i1])
            if m >= 2:
                return m
        return 0

    def has(self, box, label: int) -> bool:
        c = self.cells(box)
        if c is None:
            return False
        i0, j0, i1, j1 = c
        for j in range(j0, j1):
            if label in self.rows[j][i0:i1]:
                return True
        return False

    def flood_from(self, box) -> int:
        """Scanline flood from the free cells of `box`; the new label, or 0 when none is free."""
        c = self.cells(box)
        if c is None:
            return 0
        i0, j0, i1, j1 = c
        seeds = []
        for j in range(j0, j1):
            k = self.rows[j].find(0, i0, i1)
            if k >= 0:
                seeds.append((k, j))
        if not seeds:
            return 0
        if self.next_label > 255:  # labels are bytes: past 254 components, forget them all and start over
            table = bytes([0 if v >= 2 else v for v in range(256)])
            for j in range(self.h):
                self.rows[j][:] = self.rows[j].translate(table)
            self.next_label = 2
        label = self.next_label
        self.next_label += 1
        rows, h = self.rows, self.h
        mark = bytes([label])
        stack = seeds
        while stack:
            x, y = stack.pop()
            row = rows[y]
            if row[x] != 0:
                continue
            xa = len(row[:x].rstrip(b"\x00"))
            xb = x + len(row[x:]) - len(row[x:].lstrip(b"\x00"))
            row[xa:xb] = mark * (xb - xa)
            for ny in (y - 1, y + 1):
                if 0 <= ny < h:
                    nrow = rows[ny]
                    k = nrow.find(0, xa, xb)
                    while k >= 0:
                        stack.append((k, ny))
                        e = k + len(nrow[k:xb]) - len(nrow[k:xb].lstrip(b"\x00"))
                        k = nrow.find(0, e, xb)
        return label


def _lane_strips(f: Foot) -> dict[str, tuple[float, float, float, float]]:
    """A closed row's fanout lanes as world strips, by side.

    One owner, two questions (`docs/r2-design.md` A.7): this raster asks "does a channel exist end
    to end for this net's trunk" and fills the whole strip unless the net owns an escape in it,
    while the router asks about one piece — may it cross a lane (yes) or run along it (no). The
    rectangle they argue about has to be the same one, so it lives in `route_scene` and both
    callers import it.
    """
    from .route_scene import lane_strips

    return lane_strips(f)


def check_corridors(ctx: Ctx) -> list[str]:
    """F.3: a net whose class is 0.4 mm or wider needs a channel of width + 2 clearances between
    every MST-adjacent pair of its pads on at least one allowed outer layer. Rasterised: other-net
    pads (through holes on both layers), keepouts, closed rows' fanout lanes and the edge band,
    each dilated by the channel's half width; the net's own pads are always passable.

    On a net whose paths carry a field or a kind (`docs/net-path-physics.md` §4.5) the declared links
    are checked first, each at its own width on its own layers, then an MST over the off-path pads and
    the paths' stations at the net's width (never between two stations of one path: that is a link)."""
    out: list[str] = []
    cs = ctx.cs
    stack = cs.stackup
    outer = [lay for lay in stack.copper_layers() if is_outer(lay)]
    for c in cs.constraints:
        sites = ctx.pads_on.get(c.net, [])
        if len(sites) < 2:
            continue
        by_id = {f"{s[0]}.{s[1]}": s for s in sites}
        jobs: list[tuple[float, list[str], list[tuple], str]] = []  # (width, layers, [(site a, site b)], head)
        fielded = c.fielded() if hasattr(c, "fielded") else False
        if fielded:
            same: set[frozenset] = set()
            for p in c.paths:
                st = [x for s_ in p.stations for x in s_]
                same |= {frozenset((x, y)) for x in st for y in st if x != y}
                for lk in p.links:
                    if lk.join:
                        continue
                    a = next((by_id[x] for x in lk.a if x in by_id), None)
                    b = next((by_id[x] for x in lk.b if x in by_id), None)
                    lays = [lay for lay in lk.layers if is_outer(lay)]
                    if a is None or b is None or not lays or lk.widest() < WIDE_MM:
                        continue
                    jobs.append((lk.widest(), lays, [(a, b)], f"{c.net} path {lk.written[0]} -> {lk.written[1]} ({lk.source}): "))
            if c.width_mm.value >= WIDE_MM:
                lays = [lay for lay in c.layers if is_outer(lay)]
                pts = [(x, y) for _r, _n, x, y, _w, _h in sites]
                edges = [(sites[ia], sites[ib]) for ia, ib in _mst_edges(pts) if frozenset((f"{sites[ia][0]}.{sites[ia][1]}", f"{sites[ib][0]}.{sites[ib][1]}")) not in same]
                if lays and edges:
                    jobs.append((c.width_mm.value, lays, edges, ""))
        else:
            w = c.width_mm.value
            layers = [lay for lay in c.layers if is_outer(lay)]
            if w < WIDE_MM or not layers:
                continue
            pts = [(x, y) for _r, _n, x, y, _w, _h in sites]
            jobs.append((w, layers, [(sites[ia], sites[ib]) for ia, ib in _mst_edges(pts)], ""))
        for w, layers, edges, head in jobs:
            out += _corridor_job(ctx, c, w, layers, edges, sites, outer, head)
    return out


def _corridor_job(ctx: Ctx, c: Constraint, w: float, layers: list[str], edges, sites, outer, head: str) -> list[str]:
    """F.3's raster for one width on some layers, asked of the given pad pairs."""
    out: list[str] = []
    stack = ctx.cs.stackup
    clr = c.clearance_mm.value
    half = w / 2.0 + clr
    grid = CORRIDOR_COARSE_MM if w > CORRIDOR_COARSE_OVER_MM else CORRIDOR_GRID_MM
    dil = math.ceil(round(half / grid, 6))
    edge_band = math.ceil(round(stack.edge_clearance / grid, 6)) + dil
    grids: dict[str, _Grid] = {}
    obstacles: dict[str, list[tuple[str, tuple[float, float, float, float]]]] = {}
    for lay in layers:
        g = _Grid(ctx.content, grid)
        obs: list[tuple[str, tuple[float, float, float, float]]] = []
        band = bytes([1]) * g.w
        for j in range(g.h):
            if j < edge_band or j >= g.h - edge_band:
                g.rows[j][:] = band
            else:
                g.rows[j][:edge_band] = b"\x01" * edge_band
                g.rows[j][g.w - edge_band :] = b"\x01" * edge_band
        for ref in sorted(ctx.feet):
            f = ctx.feet[ref]
            pl = ctx.pad_layers.get(ref, [])
            for k, p in enumerate(f.pads):
                if p.net == c.net:
                    continue
                on = pl[k] if k < len(pl) else frozenset({"F.Cu"})
                if lay not in on:
                    continue
                box = _pad_box(f, p)
                g.fill(box, 1, dil)
                obs.append((f"{ref}.{_pin(ctx, ref, p.num)}" if p.num else f"{ref}'s hole", box))
            for side, strip in _lane_strips(f).items():
                if any(p.net == c.net and f.escape.get(p.num) == side for p in f.pads):
                    continue  # the net's own escape via sits in this lane
                g.fill(strip, 1, dil)
                obs.append((f"{ref}'s fanout lane", strip))
        for name, box, _no in ctx.keepout_boxes:
            g.fill(box, 1, dil)
            obs.append((f"Keepout {name}", box))
        e, r = stack.edge_clearance, ctx.content
        for band in ((r.x0, r.y0, r.x1, r.y0 + e), (r.x0, r.y1 - e, r.x1, r.y1), (r.x0, r.y0, r.x0 + e, r.y1), (r.x1 - e, r.y0, r.x1, r.y1)):
            obs.append(("the board edge", band))
        for s_ in sites:
            g.fill(_box_of(s_), 0)
        grids[lay] = g
        obstacles[lay] = obs
    for a, b in edges:
        box_a, box_b = _box_of(a), _box_of(b)
        reached = False
        for lay in layers:
            g = grids[lay]
            label = g.label_in(box_a)
            if label == 0:
                label = g.flood_from(box_a)
            if label > 0 and g.has(box_b, label):
                reached = True
                break
        if reached:
            continue
        msg = _corridor_message(ctx, c, a, b, layers, outer, obstacles, half, w, pose=not head)
        if head:
            # A path's link: its own line heads the message; the layers edit is the Path's, not a NetReq's.
            msg = head + msg[len(c.net) + 2 :].split("; or NetReq(")[0]
        out.append(msg)
    return out


def _side(a, b, box) -> int:
    """Which side of the line a-b a box's centre lies on (the sign of the cross product)."""
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return 1 if _orient(a, b, (cx, cy)) >= 0 else -1


def _corridor_message(ctx: Ctx, c: Constraint, a, b, layers, outer, obstacles, half: float, w: float | None = None, pose: bool = True) -> str:
    w, clr = (c.width_mm.value if w is None else w), c.clearance_mm.value
    pa, pb = (a[2], a[3]), (b[2], b[3])
    near: dict[str, tuple[float, tuple[float, float, float, float]]] = {}
    for lay in layers:
        for name, box in obstacles[lay]:
            d = _seg_rect_dist(pa, pb, box)
            if name not in near or d < near[name][0]:
                near[name] = (d, box)
    ranked = sorted(near.items(), key=lambda kv: (kv[1][0], kv[0]))
    if len(ranked) >= 2:
        # the pinch: the nearest obstacle and, across the line from it, the one it leaves the
        # narrowest gap with (the second nearest when nothing sits across)
        n1, (_d1, b1) = ranked[0]
        side1 = _side(pa, pb, b1)
        across = [(_gap(b1, b), n, b) for n, (_d, b) in ranked[1:] if _side(pa, pb, b) != side1]
        if across:
            _g2, n2, b2 = min(across, key=lambda t: (t[0], t[1]))
        else:
            n2, (_d2, b2) = ranked[1]
        pinch = f"{n1} and {n2} pinch it to {_gap(b1, b2):.2f} mm"
    elif ranked:
        n1, (_d1, b1) = ranked[0]
        pinch = f"{n1} blocks it"
    else:
        n1, b1 = "", (0.0, 0.0, 0.0, 0.0)
        pinch = "the board edge boxes it"
    # Facts only (docs/direction.md §7b): what pinches the channel and the lines that placed it.
    placed = []
    for name in (n1, *( [n2] if len(ranked) >= 2 else [])):
        ref = name.split(".", 1)[0] if name and "." in name and not name.startswith(("Keepout ", "the board edge")) else ""
        if ref and ref in ctx.feet:
            placed.append(_attached(ctx, ref))
    missing = [lay for lay in outer if lay not in c.layers]
    lays = f"; {c.net} may use {', '.join(c.layers)} only ({_layers_src(ctx, c)})" if missing else ""
    return (
        f"{c.net}: no {w:.2f} mm channel from {a[0]}.{_pin(ctx, a[0], a[1])} to {b[0]}.{_pin(ctx, b[0], b[1])} on {' or '.join(layers)} "
        f"({_g(w)} width + 2 x {_g(clr)} clearance = {2 * half:.2f} mm): {pinch}" + (f" ({'; '.join(dict.fromkeys(placed))})" if placed else "") + lays
    )


def _layers_src(ctx: Ctx, c: Constraint) -> str:
    req = ctx.design.netreqs[c.req_index] if c.req_index >= 0 else None
    return f"NetReq line {c.line}" if req is not None and req.layers is not None else f"preset {c.kind}"


# ---------------------------------------------------------------- F.3b a path's station escape

ESCAPE_DIRS = 16  # directions a track may leave a station pad in, tried at the link's width


def _ray_to_edge(w: float, h: float, ux: float, uy: float) -> float:
    """From an AABB's centre along unit (ux, uy), the distance to its edge."""
    tx = (w / 2.0) / abs(ux) if abs(ux) > 1e-12 else math.inf
    ty = (h / 2.0) / abs(uy) if abs(uy) > 1e-12 else math.inf
    return min(tx, ty)


def check_station_escapes(ctx: Ctx) -> list[str]:
    """Can a track of the link's width leave each station pad at all? (Refutation round 1, defect 6:
    the route found "no free cell next to one of the pads" with nothing said before copper.) For each
    link and each of its two stations, on each of the link's layers the pad is on, a stub is tried in
    `ESCAPE_DIRS` directions: from the pad's centre to `width/2 + clearance` past its edge, `width` wide,
    against every other-net pad on that layer at what this net owes it (`ClearanceTable.between`: the
    clearance, or a keep-away) and the board edge. A station none of whose pads has one free direction
    is a warning that names the pads in the way, the rule, and the line to edit; never a pose."""
    from .constraints import clearance_table

    out: list[str] = []
    table = clearance_table(ctx.cs)
    edge = ctx.cs.stackup.edge_clearance
    b = ctx.content
    inner = (b.x0 + edge, b.y0 + edge, b.x1 - edge, b.y1 - edge)
    by_layer: dict[str, list[tuple[str, str, str, tuple[float, float, float, float]]]] = {}
    for ref in sorted(ctx.feet):
        f = ctx.feet[ref]
        pl = ctx.pad_layers.get(ref, [])
        for k, p in enumerate(f.pads):
            on = pl[k] if k < len(pl) else frozenset({"F.Cu"})
            for lay in on:
                by_layer.setdefault(lay, []).append((ref, p.num, p.net or "", _pad_box(f, p)))
    seen: set[tuple[str, str]] = set()
    for c in ctx.cs.constraints:
        for p in c.paths:
            for lk in p.links:
                if lk.join:
                    continue
                for st, written in ((lk.a, lk.written[0]), (lk.b, lk.written[1])):
                    if (lk.key, written) in seen:
                        continue
                    seen.add((lk.key, written))
                    tried = []
                    free = False
                    for pid in st:
                        ref, _, num = pid.partition(".")
                        f = ctx.feet.get(ref)
                        if f is None:
                            continue
                        idx = next((k for k, q in enumerate(f.pads) if q.num == num), None)
                        if idx is None:
                            continue
                        pl = ctx.pad_layers.get(ref, [])
                        on = pl[idx] if idx < len(pl) else frozenset({"F.Cu"})
                        box = _pad_box(f, f.pads[idx])
                        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
                        for lay in [x for x in lk.layers if x in on]:
                            w = lk.width_on(lay)
                            clr = c.clearance_mm.value
                            hits: dict[tuple[str, str], tuple[float, float, str, str]] = {}
                            for d in range(ESCAPE_DIRS):
                                th = 2.0 * math.pi * d / ESCAPE_DIRS
                                ux, uy = math.cos(th), math.sin(th)
                                reach = _ray_to_edge(box[2] - box[0], box[3] - box[1], ux, uy) + w / 2.0 + clr
                                tip = (cx + ux * reach, cy + uy * reach)
                                ok = inner[0] + w / 2.0 <= tip[0] <= inner[2] - w / 2.0 and inner[1] + w / 2.0 <= tip[1] <= inner[3] - w / 2.0
                                if not ok:
                                    hits.setdefault(("the board edge", ""), (0.0, edge, "edge clearance", ""))
                                for oref, onum, onet, obox in by_layer.get(lay, []):
                                    if onet == c.net or not onet:
                                        continue
                                    need, why = table.between(c.net, onet)
                                    gap = _seg_rect_dist((cx, cy), tip, obox) - w / 2.0
                                    if gap < need - 1e-9:
                                        ok = False
                                        key = (oref, onum)
                                        if key not in hits:
                                            hits[key] = (_gap(box, obox), need, why, onet)
                                if ok:
                                    free = True
                                    break
                            if free:
                                break
                            tried.append((pid, lay, w, hits))
                        if free:
                            break
                    if free or not tried:
                        continue
                    pid, lay, w, hits = tried[0]
                    ref = pid.split(".")[0]
                    ranked = sorted(hits.items(), key=lambda kv: (kv[1][0] - kv[1][1], kv[0]))
                    # What is close enough to matter: inside the rule plus one track and its clearance.
                    close = [kv for kv in ranked if kv[1][0] - kv[1][1] < w + c.clearance_mm.value]
                    ranked = close or ranked[:1]
                    named = []
                    movers = []
                    both = []
                    for (oref, onum), (gap, need, why, onet) in ranked[:3]:
                        if oref == "the board edge":
                            named.append(f"the board edge ({_g(need)} mm)")
                            continue
                        src = why
                        if "keep-away" in why:
                            for a_, b_ in ((onet, c.net), (c.net, onet)):
                                cc = ctx.cs.by_net(a_)
                                kk = next((k for k in (cc.keep_away if cc is not None else ()) if k.other == b_), None)
                                if kk is not None and cc.line:
                                    src = f"keep_clear_of {b_} on NetReq line {cc.line}"
                                    if oref == ref:
                                        both.append((oref, onet, cc.line, kk.mm))
                                    break
                        named.append(f"{oref}.{_pin(ctx, oref, onum)} ({onet}) is {gap:.2f} mm from it and needs {_g(need)} mm ({src})")
                        if oref != ref:
                            movers.append(oref)
                    head = f"{c.net} path {lk.written[0]} -> {lk.written[1]} ({lk.source}): no {_g(w)} mm track can leave {pid} ({written}) on {lay}"
                    # Facts only (refutation round 2: when the pads in the way were the station's own
                    # part's, this named that part as the thing to move). Who is in the way, how far,
                    # by what rule; a pad of the station's own part is said to be one.
                    own = [f"{oref} carries both {c.net} and {onet}" for oref, onet, _line, _mm in dict.fromkeys(both)]
                    placed = [_attached(ctx, m) for m in dict.fromkeys(movers)]
                    out.append(f"{head}: {'; '.join(named + own + placed)}")
    return out


# ---------------------------------------------------------------- F.4 loops


def _shoelace(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def _loop_area(pts: list[tuple[float, float]]) -> float:
    """The area a current loop encloses, the polygon in path order (shoelace). A path that crosses
    itself (a cap turned so its ground pad sits past the rail pad) encloses two lobes the shoelace
    cancels; that loop is measured by its convex hull instead."""
    n = len(pts)
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _segs_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                return _shoelace(_hull(pts))
    return _shoelace(pts)


def _hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ps = sorted(set(pts))
    if len(ps) < 3:
        return ps
    lower: list[tuple[float, float]] = []
    for p in ps:
        while len(lower) >= 2 and _orient(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(ps):
        while len(upper) >= 2 and _orient(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _net_kinds(ctx: Ctx) -> tuple[set[str], set[str]]:
    """(power nets, ground nets) as the netlist declares them (`Power()`, `Ground()`)."""
    power = {n for n, net in ctx.design.nets.items() if getattr(net, "kind", "") == "power"}
    ground = {n for n, net in ctx.design.nets.items() if getattr(net, "kind", "") == "ground"}
    return power, ground


def _pad_on(f: Foot, net: str, near: tuple[float, float] | None = None) -> Pad | None:
    """The footprint's pad on `net`, the nearest to `near` when there are several."""
    pads = [p for p in f.pads if p.net == net and p.num]
    if not pads:
        return None
    if near is None:
        return pads[0]
    return min(pads, key=lambda p: (math.hypot(f.pad_world(p)[0] - near[0], f.pad_world(p)[1] - near[1]), p.num))


def _loop_src(c: Constraint | None, kind: str) -> str:
    if c is None or c.loop_mm2 is None:
        return f"preset {kind}, pcbc default"
    src = c.loop_mm2.source
    if src.formula == "preset":
        return f"preset {src.ref}, pcbc default"
    return f"{src.formula} {src.ref}"


def check_loops(ctx: Ctx) -> tuple[list[str], list[str]]:
    """F.4: the decap loop (IC supply pad, cap supply pad, cap ground pad, the IC's nearest ground
    pad) as a style note over the power budget; the hot loop of every switch_node net as a move."""
    moves: list[str] = []
    notes: list[str] = []
    cs = ctx.cs
    power, ground = _net_kinds(ctx)
    ics = _ics(ctx)
    # Declared loops first (docs/net-path-physics.md §6): the area the named pads enclose, in the order
    # written. A lower bound (pad centres, no vertical leg): over budget is definite, under is not proof.
    named: set[str] = set()
    for lp in getattr(cs, "loops", ()):
        named.update(lp.parts)
        pts = []
        for pid in lp.pads:
            ref, num = pid.split(".", 1)
            f = ctx.feet.get(ref)
            pad = next((q for q in f.pads if q.num == num), None) if f is not None else None
            if pad is None:
                break
            pts.append(f.pad_world(pad))
        if len(pts) != len(lp.pads):
            continue
        area = _loop_area(pts)
        budget = lp.budget.value
        src = f"Loop line {lp.line}" if lp.budget.source.formula == "Loop" else f"pcbc default; {lp.budget.source.note}"
        head = f"loop {' -> '.join(lp.written)} (Loop line {lp.line})"
        if area <= budget + 1e-9:
            notes.append(f"{head}: pads enclose at least {area:.1f} mm2 of {_g(budget)} (shoelace of pad centres; no vertical leg)")
            continue
        # The station farthest from its neighbours in the loop, and the attachment already written.
        n = len(pts)
        far = max(range(n), key=lambda i: (math.dist(pts[i], pts[(i - 1) % n]) + math.dist(pts[i], pts[(i + 1) % n]), -i))
        nb = (far + 1) % n if math.dist(pts[far], pts[(far + 1) % n]) >= math.dist(pts[far], pts[(far - 1) % n]) else (far - 1) % n
        ref = lp.written[far].split(".", 1)[0]
        ref_nb = lp.written[nb].split(".", 1)[0]
        placed = "; ".join(dict.fromkeys(_attached(ctx, r) for r in (ref, ref_nb)))
        moves.append(
            f"{head} encloses {area:.1f} mm2 over the {_g(budget)} mm2 budget ({src}); {lp.written[far]} is {math.dist(pts[far], pts[nb]):.1f} mm "
            f"from {lp.written[nb]} ({placed})"
        )
    for ref in sorted(ctx.feet):
        f = ctx.feet[ref]
        if not ref.startswith("C") or len(f.pads) != 2:
            continue
        if ref in named:
            continue  # a declared loop names this cap: the guess does not second-guess it
        nets = {p.net for p in f.pads}
        pnet = next((n for n in sorted(nets) if n in power), None)
        gnet = next((n for n in sorted(nets) if n in ground), None)
        if pnet is None or gnet is None:
            continue
        cp, cg = _pad_on(f, pnet), _pad_on(f, gnet)
        assert cp is not None and cg is not None
        cpw, cgw = f.pad_world(cp), f.pad_world(cg)
        nearest = None
        for ic in ics:
            ip = _pad_on(ctx.feet[ic], pnet, cpw)
            if ip is None:
                continue
            ipw = ctx.feet[ic].pad_world(ip)
            d = math.hypot(ipw[0] - cpw[0], ipw[1] - cpw[1])
            if nearest is None or d < nearest[0]:
                nearest = (d, ic, ip)
        if nearest is None:
            continue
        _d, ic, ip = nearest
        ig = _pad_on(ctx.feet[ic], gnet, cgw)
        if ig is None:
            continue
        ipw, igw = ctx.feet[ic].pad_world(ip), ctx.feet[ic].pad_world(ig)
        area = _loop_area([ipw, cpw, cgw, igw])
        c = cs.by_net(pnet)
        budget = c.loop_mm2.value if c is not None and c.loop_mm2 is not None else DEFAULT_LOOP_MM2["power"]
        if area > budget + 1e-9:
            notes.append(
                f"{pnet}: decap loop {ic}.{_pin(ctx, ic, ip.num)} -> {ref}.{cp.num} -> {ref}.{cg.num} -> {ic}.{_pin(ctx, ic, ig.num)} "
                f"encloses {area:.1f} mm2 over the {_g(budget)} mm2 budget ({_loop_src(c, 'power')}); {_attached(ctx, ref)}"
            )
    for c in cs.constraints:
        if c.kind != "switch_node":
            continue
        sw = c.net
        # the IC: the part with a SW pad and a pad on a power rail that is not ground; the most pads wins
        ic_ref = None
        for ref in sorted(ctx.feet):
            f = ctx.feet[ref]
            nets = {p.net for p in f.pads if p.num}
            if sw in nets and any(n in power and n not in ground for n in nets):
                n = len([p for p in f.pads if p.num])
                if ic_ref is None or n > ic_ref[0]:
                    ic_ref = (n, ref)
        if ic_ref is None:
            continue
        ic = ic_ref[1]
        fic = ctx.feet[ic]
        rails = sorted(n for n in {p.net for p in fic.pads if p.num} if n in power and n not in ground and n != sw)
        icc = fic.center()
        best = None  # (distance of the cap to the IC, rail, cin ref)
        for rail in rails:
            for ref in sorted(ctx.feet):
                f = ctx.feet[ref]
                if not ref.startswith("C") or len(f.pads) != 2:
                    continue
                nets = {p.net for p in f.pads}
                if rail not in nets or not (nets & ground):
                    continue
                cx, cy = f.center()
                d = math.hypot(cx - icc[0], cy - icc[1])
                if best is None or d < best[0]:
                    best = (d, rail, ref)
        if best is None:
            continue
        _d, rail, cin = best
        if cin in named and ic in named:
            continue  # a declared loop names this hot loop's parts: it replaces the guess
        fcin = ctx.feet[cin]
        gnet = next(n for n in sorted({p.net for p in fcin.pads}) if n in ground)
        cin_rail, cin_gnd = _pad_on(fcin, rail), _pad_on(fcin, gnet)
        assert cin_rail is not None and cin_gnd is not None
        ic_rail = _pad_on(fic, rail, fcin.pad_world(cin_rail))
        ic_sw = _pad_on(fic, sw)
        assert ic_rail is not None and ic_sw is not None
        low = None
        for ref in sorted(ctx.feet):
            if ref == ic:
                continue
            f = ctx.feet[ref]
            n = len([p for p in f.pads if p.num])
            nets = {p.net for p in f.pads if p.num}
            if n in (2, 3) and sw in nets and (nets & ground):
                low = ref
                break
        if low is not None:
            flow = ctx.feet[low]
            lg = next(n for n in sorted({p.net for p in flow.pads}) if n in ground)
            low_sw, low_gnd = _pad_on(flow, sw), _pad_on(flow, lg)
            assert low_sw is not None and low_gnd is not None
            chain = [(cin, cin_rail), (ic, ic_rail), (ic, ic_sw), (low, low_sw), (low, low_gnd), (cin, cin_gnd)]
        else:
            ic_gnd = _pad_on(fic, gnet, fcin.pad_world(cin_gnd))
            if ic_gnd is None:
                continue
            chain = [(cin, cin_rail), (ic, ic_rail), (ic, ic_gnd), (cin, cin_gnd)]
        pts = [ctx.feet[r].pad_world(p) for r, p in chain]
        area = _loop_area(pts)
        budget = c.loop_mm2.value if c.loop_mm2 is not None else DEFAULT_LOOP_MM2["switch_node"]
        if area <= budget + 1e-9:
            notes.append(f"{sw}: hot loop {area:.1f} mm2 (budget {_g(budget)})")
            continue
        far = cin
        if low is not None:
            dc = math.hypot(fcin.center()[0] - icc[0], fcin.center()[1] - icc[1])
            dl = math.hypot(ctx.feet[low].center()[0] - icc[0], ctx.feet[low].center()[1] - icc[1])
            far = low if dl > dc else cin
        ic_pin_pad = ic_rail if far == cin else ic_sw
        far_pin = _pin(ctx, ic, ic_pin_pad.num)
        path = " -> ".join(f"{r}.{_pin(ctx, r, p.num)}" for r, p in chain)
        # docs/net-path-physics.md §6: no budget fitted to the placement being judged and no pose; the
        # far part, its distance, and the attachment board.py already writes for it.
        ffar = ctx.feet[far]
        far_pad = cin_rail if far == cin else _pad_on(ffar, sw)
        d = math.dist(ffar.pad_world(far_pad), fic.pad_world(ic_pin_pad)) if far_pad is not None else 0.0
        spec = _place_spec(ctx, far)
        if spec is not None and getattr(spec, "to", None) == f"{ic}.{far_pin}":
            att = f"layout.core.py:{getattr(spec, 'line', 0)} attaches {far} there"
        else:
            att = "; ".join(dict.fromkeys(_attached(ctx, r) for r in (far, ic)))
        moves.append(
            f"{sw}: hot loop {path} encloses {area:.1f} mm2 over the {_g(budget)} mm2 budget ({_loop_src(c, 'switch_node')}); "
            f"{far}.{far_pad.num if far_pad is not None else '?'} is {d:.1f} mm from {ic}.{far_pin} ({att})"
        )
    return moves, notes


# ---------------------------------------------------------------- F.5 keep-away


def check_keep_away(ctx: Ctx) -> list[str]:
    """F.5: the edge-to-edge distance between a net's pads and the other net's pads, and the
    courtyards of the parts carrying it. A part carrying both nets is its rating, not layout. A keep-away is
    net to net (`docs/physics-subjects.md` PR 7, E8): a Path has none of its own."""
    out: list[str] = []
    for c in ctx.cs.constraints:
        for ka in c.keep_away:
            mine = ctx.pads_on.get(c.net, [])
            theirs = ctx.pads_on.get(ka.other, [])
            if not mine or not theirs:
                continue
            both = {s[0] for s in mine} & {s[0] for s in theirs}
            mine = [s for s in mine if s[0] not in both]
            theirs = [s for s in theirs if s[0] not in both]
            their_parts = sorted({s[0] for s in theirs})
            best_pad = None
            best_crt = None
            for a in mine:
                ba = _box_of(a)
                for b in theirs:
                    d = _gap(ba, _box_of(b))
                    if best_pad is None or d < best_pad[0] - 1e-9:
                        best_pad = (d, a, b)
                for ref in their_parts:
                    if ref == a[0]:
                        continue
                    d = _gap(ba, ctx.feet[ref].world_box())
                    if best_crt is None or d < best_crt[0] - 1e-9:
                        best_crt = (d, a, ref)
            hit = None
            if best_pad is not None and best_pad[0] < ka.mm - 1e-9:
                d, a, b = best_pad
                hit = (d, a, f"{b[0]}.{b[1]} ({ka.other})", b[0])
            elif best_crt is not None and best_crt[0] < ka.mm - 1e-9:
                d, a, ref = best_crt
                hit = (d, a, f"{ref}'s courtyard ({ka.other})", ref)
            if hit is None:
                continue
            d, a, what, oref = hit
            src = f"NetReq line {c.line}" if ka.source.formula == "NetReq" else f"preset {c.kind}, NetReq line {c.line}"
            # away from the offender: the quantised direction from its pad (or courtyard) to ours
            if best_pad is not None and hit[2].startswith(oref + "."):
                ox, oy = best_pad[2][2], best_pad[2][3]
            else:
                ox, oy = ctx.feet[oref].center()
            toward = _quantise(a[2] - ox, a[3] - oy)
            del toward
            placed = "; ".join(dict.fromkeys(_attached(ctx, r) for r in (a[0], oref)))
            out.append(f"{c.net}: {a[0]}.{_pin(ctx, a[0], a[1])} is {d:.2f} mm from {what}, under keep_clear_mm={_g(ka.mm)} ({src}) ({placed})")
    return out


_TAN8 = math.tan(math.pi / 8.0)


# ---------------------------------------------------------------- F.6 reference plane


def check_reference(ctx: Ctx) -> tuple[list[str], list[str]]:
    """F.6: on four layers every pad of a controlled-impedance net lies over its reference plane
    (inside the content rect minus the edge clearance, outside any copper keepout); on two layers
    the B.Cu pour is the reference, and a keepout under the straight line between the pads is a
    style note."""
    moves: list[str] = []
    notes: list[str] = []
    cs = ctx.cs
    stack = cs.stackup
    edge = stack.edge_clearance
    inner = Rect(ctx.content.x0 + edge, ctx.content.y0 + edge, ctx.content.x1 - edge, ctx.content.y1 - edge)
    copper_kos = [(name, box) for name, box, no in ctx.keepout_boxes if "copper" in no]
    for c in cs.constraints:
        # A pair's reference is its net's; a single-ended impedance is a Path's link's (docs/physics-subjects.md PR 5).
        zlink = next((lk for lk in cs.links_of(c.net) if lk.z_se and lk.reference), None)
        reference = c.reference if c.reference is not None else (zlink.reference[0][1] if zlink is not None else None)
        if reference is None:
            continue
        if c.pair is not None:
            controlled = c.pair.controlled
            what = "the pair"
        elif zlink is not None:
            controlled = True
            what = "the track"
        else:
            continue
        if not controlled:
            continue
        sites = ctx.pads_on.get(c.net, [])
        if stack.layers > 2:
            plane_net = next((n for n, lay in ctx.job.planes if lay == reference), None)
            plane = f"{reference} {plane_net}" if plane_net else reference
            named: set[str] = set()  # one line per part: its first pad off the plane says it
            for ref, num, x, y, w, h in sites:
                if ref in named:
                    continue
                box = (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)
                if not (box[0] >= inner.x0 - 1e-6 and box[1] >= inner.y0 - 1e-6 and box[2] <= inner.x1 + 1e-6 and box[3] <= inner.y1 + 1e-6):
                    named.add(ref)
                    moves.append(
                        f"{c.net}: {ref}.{num} sits within {_g(edge)} mm of the board edge: no {plane} under it, {what} has no reference there ({_attached(ctx, ref)})"
                    )
                    continue
                for name, ko in copper_kos:
                    if _overlap(box, ko):
                        named.add(ref)
                        moves.append(
                            f"{c.net}: {ref}.{num} sits in Keepout {name}: no {plane} under it, {what} has no reference there ({_attached(ctx, ref)})"
                        )
                        break
        else:
            pts = [(x, y) for _r, _n, x, y, _w, _h in sites]
            for ia, ib in _mst_edges(pts):
                a, b = sites[ia], sites[ib]
                for name, ko in copper_kos:
                    if _seg_rect_dist((a[2], a[3]), (b[2], b[3]), ko) <= 1e-9:
                        notes.append(
                            f"{c.net}: the line from {a[0]}.{a[1]} to {b[0]}.{b[1]} crosses Keepout {name}; "
                            f"the B.Cu pour is {what}'s reference and has a hole there"
                        )
    return moves, notes




# ---------------------------------------------------------------- F.7 isolation


def _part_sides(ctx: Ctx, roots: tuple[str, str]) -> dict[str, str | None]:
    """ref -> the Isolation side it is on, from `Place(parent=)` or inherited through `Place(to=)`
    (the rule compile used: no geometry)."""
    place_of = {p.ref: p for p in ctx.design.places}
    parent_of = {r.name: r.parent for r in ctx.design.regions}
    out: dict[str, str | None] = {}
    for inst in ctx.design.instances:
        cur = inst.ref
        seen: set[str] = set()
        region: str | None = None
        while cur in place_of and cur not in seen:
            seen.add(cur)
            p = place_of[cur]
            if p.parent:
                region = p.parent
                break
            if p.to and "." in p.to:
                cur = p.to.split(".", 1)[0]
                continue
            break
        rseen: set[str] = set()
        side: str | None = None
        while region is not None and region not in rseen:
            if region in roots:
                side = region
                break
            rseen.add(region)
            region = parent_of.get(region)
        out[inst.ref] = side
    return out


def check_isolation(ctx: Ctx) -> list[str]:
    """F.7: the two Regions keep the C.8 gap along their separating axis; every part's courtyard
    stays on its side unless it is in `across=`, and an `across` part spans the gap with no pad in
    it; the compile-time rule area matches the placed gap."""
    out: list[str] = []
    specs: dict[tuple[str, str], IsolationSpec] = {(s.req.a, s.req.b): s for s in ctx.cs.isolation_specs}
    for iso in ctx.design.isolations:
        tag = f"Isolation {iso.a}/{iso.b}"
        ra, rb = ctx.regions.get(iso.a), ctx.regions.get(iso.b)
        if ra is None or rb is None:
            continue
        spec = specs.get((iso.a, iso.b))
        if ra.x1 <= rb.x0 or rb.x1 <= ra.x0:
            axis = "x"
            low, high = (ra, rb) if ra.x1 <= rb.x0 else (rb, ra)
            gap = high.x0 - low.x1
            far_name = iso.b if high is rb else iso.a
            corridor = (low.x1, ctx.content.y0, high.x0, ctx.content.y1)
            shift = "left"
        elif ra.y1 <= rb.y0 or rb.y1 <= ra.y0:
            axis = "y"
            low, high = (ra, rb) if ra.y1 <= rb.y0 else (rb, ra)
            gap = high.y0 - low.y1
            far_name = iso.b if high is rb else iso.a
            corridor = (ctx.content.x0, low.y1, ctx.content.x1, high.y0)
            shift = "top"
        else:
            out.append(f"{tag}: Regions overlap in both axes; no straight line separates them")
            continue
        if spec is not None and gap < spec.gap_min_mm - 1e-9:
            if spec.gap_min_mm == spec.creepage_mm.value:
                need = f"{_g(spec.creepage_mm.value)} mm creepage ({spec.creepage_mm.source.ref})"
            elif spec.gap_min_mm == spec.clearance_mm.value:
                need = f"{_g(spec.clearance_mm.value)} mm clearance ({spec.clearance_mm.source.ref})"
            else:
                need = f"{_g(spec.gap_min_mm)} mm gap (slot=True: {_g(spec.clearance_mm.value)} mm clearance, at least 1 mm for the slot)"
            del far_name, shift
            out.append(f"{tag} ({_g(iso.volts)} V): Regions are {gap:.1f} mm apart, {spec.gap_min_mm - gap:.1f} mm short of the {need}")
        sides = _part_sides(ctx, (iso.a, iso.b))
        across = set(iso.across)
        for ref in sorted(ctx.feet):
            f = ctx.feet[ref]
            crt = f.world_box()
            if ref in across:
                spans = crt[0] <= low.x1 + 1e-6 and crt[2] >= high.x0 - 1e-6 if axis == "x" else crt[1] <= low.y1 + 1e-6 and crt[3] >= high.y0 - 1e-6
                if not spans:
                    side = sides.get(ref) or iso.a
                    if axis == "x":
                        edge = "right" if side == (iso.a if low is ra else iso.b) else "left"
                    else:
                        edge = "bottom" if side == (iso.a if low is ra else iso.b) else "top"
                    del edge
                    out.append(f"{tag}: {ref} is in across= but does not span the {gap:.1f} mm gap ({_attached(ctx, ref)})")
                    continue
                for p in f.pads:
                    if not p.num:
                        continue
                    box = _pad_box(f, p)
                    inside = low.x1 < box[0] and box[2] < high.x0 if axis == "x" else low.y1 < box[1] and box[3] < high.y0
                    if inside:
                        lo, hi = (corridor[0], corridor[2]) if axis == "x" else (corridor[1], corridor[3])
                        out.append(f"{tag}: {ref}.{p.num} is in across= but lies in the gap ({lo:.1f}..{hi:.1f} mm): copper in the corridor is copper across the barrier ({_attached(ctx, ref)})")
                        break
                continue
            side = sides.get(ref)
            if side is None:
                continue  # compile refused it
            rect = ra if side == iso.a else rb
            if crt[0] >= rect.x0 - 1e-6 and crt[1] >= rect.y0 - 1e-6 and crt[2] <= rect.x1 + 1e-6 and crt[3] <= rect.y1 + 1e-6:
                continue
            if _overlap(crt, corridor):
                out.append(f"{tag}: {ref} ({side}) crosses the corridor ({_attached(ctx, ref)})")
            else:
                out.append(f"{tag}: {ref} ({side}) lies outside Region {side} ({_attached(ctx, ref)})")
        area = next((a for a in ctx.cs.rule_areas if a.name == f"ISO_{iso.a}_{iso.b}"), None)
        if area is not None:
            placed = (round(low.x1, 4), round(high.x0, 4)) if axis == "x" else (round(low.y1, 4), round(high.y0, 4))
            recorded = (area.box[0], area.box[2]) if axis == "x" else (area.box[1], area.box[3])
            if placed != recorded:
                out.append(f"{tag}: rule area {area.name} spans {recorded[0]:g}..{recorded[1]:g} mm but the Regions are at {placed[0]:g}..{placed[1]:g}")
    return out


# ---------------------------------------------------------------- F.8 ground bridge


BRIDGE_AXES: tuple[tuple[str, float, float], ...] = (
    ("x", 1.0, 0.0),
    ("y", 0.0, 1.0),
    ("u", 0.7071067811865476, 0.7071067811865476),
    ("v", 0.7071067811865476, -0.7071067811865476),
)
"""The four directions a straight line could separate two domains along, as unit normals.

`check_isolation`'s separating-axis test asked **x and y**, because an `Isolation` separates two
`Region`s and a Region is an axis-aligned rectangle. Two grounds are not Regions, so the two
diagonals are asked as well — the same four axes `route_geom` routes on, and the only four a
45-degree world can put a moat along. A pair that overlaps on all four has no straight line between
it, and `docs/stitch-plan.md` §8 item 5 refuses to build a moat for exactly that reason.

The projection is of pad **centres** and not of pad boxes, and that is the conservative direction:
a centre interval is contained in its box interval, so "the centres overlap" implies "the boxes
overlap" and never the reverse. Measured on the DS2 Addon, 2026-09-21 — centres overlap on all four
(x 24.480, y 15.444, u 20.610, v 16.797 mm) and so, a fortiori, do the boxes (26.180, 16.394,
22.679, 18.653 mm). The centre numbers are the ones `docs/stitch-plan.md` §2(n) recorded.
"""


def _axis_spans(points: list[tuple[float, float]]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for name, ax, ay in BRIDGE_AXES:
        vals = [px * ax + py * ay for px, py in points]
        out[name] = (min(vals), max(vals))
    return out


def _closest_cross_ref(ctx: Ctx, a: str, b: str) -> tuple[float, str, str] | None:
    """The nearest pad of `a` to a pad of `b` **on a different footprint**, edge to edge.

    Different footprints, because the answer is "where could a tie part go", and no part goes
    between two pins of one TSSOP. Measured on the DS2 Addon: the overall closest GND/VSS pads are
    `U1.4` and `U1.5`, 0.3070 mm apart on a 0.65 mm pitch — a distance that names the reason the two
    grounds have to be tied somewhere and not a place to tie them; the closest pair on two different
    parts is `J1.2` and `C7.2`, 2.6675 mm apart, and that is a place.
    """
    best: tuple[float, str, str] | None = None
    for sa in ctx.pads_on.get(a, []):
        for sb in ctx.pads_on.get(b, []):
            if sa[0] == sb[0]:
                continue
            key = (_gap(_box_of(sa), _box_of(sb)), f"{sa[0]}.{sa[1]}", f"{sb[0]}.{sb[1]}")
            if best is None or key < best:
                best = key
    return best


def _reach_to(ctx: Ctx, net: str, other: str, skip: frozenset[str]) -> tuple[float, str, str] | None:
    """How close `net` gets to `other`, and at which of `net`'s pads — with `skip`'s parts left out.

    `skip` is the tie's own ref, and leaving it out on **both** sides is what makes this well
    defined. A tie has one pad in each domain, so counting it as a target would answer "which pad of
    VSS is nearest to GND?" with "the one next to the tie's GND pad" for whichever domain the tie
    happens to sit closer to — a measurement of where the tie is, asked in order to judge where the
    tie is. Take the tie out and the question is the board's: where do these two domains come
    closest to each other on their own?
    """
    best: tuple[float, str, str] | None = None
    for sa in ctx.pads_on.get(net, []):
        for sb in ctx.pads_on.get(other, []):
            if sb[0] in skip or sa[0] == sb[0]:
                continue
            key = (_gap(_box_of(sa), _box_of(sb)), f"{sa[0]}.{sa[1]}", f"{sb[0]}.{sb[1]}")
            if best is None or key < best:
                best = key
    return best


def check_bridge(ctx: Ctx) -> list[str]:
    """F.8: what the placed board says about two grounds — where a line between them could go, and
    where the part that ties them should sit.

    Two moves, and neither ever writes copper (`docs/stitch-plan.md` §8 item 4).

    1. **No axis separates them.** For every pair of `Ground()` nets, the four-axis overlap of their
       pad centres. It is a move rather than a note because the edit it ends in is real: either the
       two domains are meant to be one (`Net`/`Power` one of them) or they are meant to be two and
       the placement has to say so. It is the geometric half of `circuit._untied_grounds`, which
       says the same thing about the netlist; this half is what proves there is no moat to build.
    2. **The tie is not where the domains meet.** With the tie's own part taken out of the board on
       both sides (`_reach_to`), each ground has a pad that comes nearest to the other ground. The
       tie's pad should be that pad, on each side. When it is not, some other part of the domain
       reaches closer to the other one than the tie does, every return current goes round to the tie
       to get home, and the coupling at the closer place competes with the tie it was supposed to
       replace. No tolerance and no distance threshold: it is an `argmin`, so it is a fact about the
       placement rather than a number somebody chose.

    Silent on all four example boards: each declares exactly one `Ground()`, so the pair loop has
    nothing to iterate. Measured on the DS2 Addon 2026-09-21, where the first move fires.
    """
    out: list[str] = []
    kinds = {n.name: n.kind for n in ctx.design.nets.values()}
    grounds = sorted(n for n, k in kinds.items() if k == "ground")
    if len(grounds) < 2:
        return out
    ties = {frozenset((sp.a, sp.b)): sp for sp in ctx.cs.bridges}
    for i, a in enumerate(grounds):
        for b in grounds[i + 1 :]:
            pa = [(x, y) for _r, _n, x, y, _w, _h in ctx.pads_on.get(a, [])]
            pb = [(x, y) for _r, _n, x, y, _w, _h in ctx.pads_on.get(b, [])]
            if not pa or not pb:
                continue
            sa, sb = _axis_spans(pa), _axis_spans(pb)
            spans = []
            apart = []
            for name, _ax, _ay in BRIDGE_AXES:
                lo = max(sa[name][0], sb[name][0])
                hi = min(sa[name][1], sb[name][1])
                if hi >= lo:
                    spans.append(f"{name} [{sa[name][0]:.3f},{sa[name][1]:.3f}] vs [{sb[name][0]:.3f},{sb[name][1]:.3f}] overlap {hi - lo:.3f} mm")
                else:
                    apart.append(f"{name} clear by {lo - hi:.3f} mm")
            spec = ties.get(frozenset((a, b)))
            if spec is None or not spec.pads:
                if apart:
                    continue  # a straight line does separate them; `Isolation` is the statement for that
                near = _closest_cross_ref(ctx, a, b)
                where = f"{near[1]} and {near[2]} come closest, {near[0]:.4f} mm apart" if near else "no two pads of them share a layer"
                out.append(
                    f"{a}/{b}: no axis separates the two grounds — {'; '.join(spans)}. "
                    f"No straight line separates them, and no Bridge() names a part that ties them: {where}."
                )
                continue
            skip = frozenset({spec.tie_ref})
            for net, other, own in ((a, b, spec.pads[0]), (b, a, spec.pads[1])):
                got = _reach_to(ctx, net, other, skip)
                if got is None or got[1] == own:
                    continue
                out.append(
                    f"{a}/{b}: {spec.tie_ref} ties them at {own}, but {net} comes closest to {other} at {got[1]} — "
                    f"{got[0]:.4f} mm from {got[2]}, with {spec.tie_ref} itself left out of the measurement on both sides. "
                    f"Every return current goes round to {spec.tie_ref} to get home while the two grounds touch at {got[1]} ({_attached(ctx, spec.tie_ref)})."
                )
    return out


__all__ = [
    "BRIDGE_AXES",
    "FAST_KINDS",
    "Ctx",
    "build_ctx",
    "check_airwires",
    "check_bridge",
    "check_chains",
    "check_corridors",
    "check_isolation",
    "check_keep_away",
    "check_loops",
    "check_reference",
    "route_aware_report",
]
