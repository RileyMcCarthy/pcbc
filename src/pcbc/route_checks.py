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
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .compile import CompiledJob
from .copper import _rotate
from .constraints import DEFAULT_LOOP_MM2, ConstraintSet, Constraint, IsolationSpec
from .css import Rect, rotate_local_bounds
from .geom import _iter_tagged
from .layout import content_rect, footprints_by_ref, resolve_keepout, resolve_regions
from .model import Design, KeepoutSpec
from .pcb_place import GAP, Foot, Pad, _board_of, _pin_name, _pins_of, lane_rules, parse_foot
from .sexp import footprint_at
from .stackup import is_outer

WIDE_MM = 0.4  # a class this wide gets the corridor test (F.3)
CHAIN_HOP_MM = 8.0  # a chain hop the placement made (pcb_place._REACH): its corridor is checked; a longer one is the router's
CORRIDOR_GRID_MM = 0.1
CORRIDOR_COARSE_MM = 0.2  # when the width is over 1 mm: the escape hatch of H.8
CORRIDOR_COARSE_OVER_MM = 1.0
_DIRS = {"right": (1.0, 0.0), "left": (-1.0, 0.0), "down": (0.0, 1.0), "up": (0.0, -1.0)}
_PAD_LAYERS = re.compile(r'\(layers\s+([^)]*)\)')
_FOOT_LAYER = re.compile(r'\n\t\t\(layer "([^"]+)"\)')


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


def build_ctx(design: Design, job: CompiledJob, pcb_text: str) -> Ctx:
    """The placed board as `layout_report` reads it, plus what the route checks need."""
    board = _board_of(job)
    content = content_rect(board)
    regions = resolve_regions(board, job.regions)
    blocks = footprints_by_ref(pcb_text)
    nets_of = _pins_of(design)
    stack, clearance = lane_rules(job)
    feet: dict[str, Foot] = {}
    pad_layers: dict[str, list[frozenset[str]]] = {}
    for ref, blk in blocks.items():
        f = parse_foot(ref, blk)
        at = footprint_at(blk)
        if at is None:
            continue
        f.at, f.rot = (at[0], at[1]), at[2]
        for pad in f.pads:
            pad.net = nets_of.get(ref, {}).get(pad.num, pad.net)
        f.lane(stack, clearance)
        feet[ref] = f
        pad_layers[ref] = _pad_layers(blk)
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


def route_aware_report(design: Design, job: CompiledJob, pcb_text: str) -> tuple[list[str], list[str]]:
    """(moves, style notes) from the placed board: sections F.1 to F.7, in that order."""
    ctx = build_ctx(design, job, pcb_text)
    moves: list[str] = []
    notes: list[str] = []
    moves += check_airwires(ctx)
    moves += check_chains(ctx)
    moves += check_corridors(ctx)
    m, n = check_loops(ctx)
    moves += m
    notes += n
    moves += check_keep_away(ctx)
    m, n = check_reference(ctx)
    moves += m
    notes += n
    moves += check_isolation(ctx)
    return moves, notes


# ---------------------------------------------------------------- geometry helpers


def _pad_layers(block: str) -> list[frozenset[str]]:
    """Per pad (in `parse_foot`'s order): the copper layers it sits on. A through hole is on every
    copper layer; an SMD pad on its footprint's side."""
    side = "F.Cu"
    m = _FOOT_LAYER.search(block)
    if m and m.group(1) == "B.Cu":
        side = "B.Cu"
    out: list[frozenset[str]] = []
    for pad in _iter_tagged(block, "pad"):
        head = pad.split("\n", 1)[0]
        if not re.match(r'\(pad\s+"[^"]*"', head) or "(at" not in pad or "(size" not in pad:
            continue
        if "thru_hole" in head:
            out.append(frozenset({"F.Cu", "B.Cu", "In1.Cu", "In2.Cu"}))
            continue
        lm = _PAD_LAYERS.search(pad)
        layers = set()
        if lm:
            for tok in lm.group(1).replace('"', "").split():
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


def _place_line(ctx: Ctx, ref: str, avoid_net: str, toward: str | None = None) -> str:
    """`Place("R_EN", to="U1.EN", toward="up")`: the part attached to the IC pin on one of its other
    nets; `move R_EN` when it has none."""
    f = ctx.feet.get(ref)
    if f is not None:
        for p in f.pads:
            if not p.net or p.net == avoid_net:
                continue
            ic = _ic_pad_on(ctx, p.net, exclude=ref)
            if ic is None:
                continue
            iref, ipin = ic
            side = toward
            if side is None:
                ipad = next(q for q in ctx.feet[iref].pads if q.net == p.net and q.num)
                side = _side_of_pin(ctx.feet[iref], ipad)
            return f'Place("{ref}", to="{iref}.{ipin}", toward="{side}")'
    return f"move {ref}"


# ---------------------------------------------------------------- F.1 airwires


def _airwire_src(ctx: Ctx, c: Constraint) -> str:
    if c.req_index >= 0 and ctx.design.netreqs[c.req_index].max_mm is not None:
        return f"NetReq line {c.line}"
    return f"preset {c.kind}"


def check_airwires(ctx: Ctx) -> list[str]:
    """F.1: the MST of a net's pads against `max_mm`; the spread of a pair's or bus's member MSTs
    against the skew budget. The router can only add length."""
    out: list[str] = []
    cs = ctx.cs
    for c in cs.constraints:
        if c.airwire_max_mm is None:
            continue
        sites = ctx.pads_on.get(c.net, [])
        if len(sites) < 2:
            continue
        mst = _edge_mst_mm([_box_of(s) for s in sites])
        if mst <= c.airwire_max_mm + 1e-9:
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
            f"{c.net}: {mst:.1f} mm of airwire (MST of {len(sites)} pads, edge to edge) over max_mm={_g(c.airwire_max_mm)} ({_airwire_src(ctx, c)}); "
            f'the route is longer still: Place("{ref}", to="{target}") or raise max_mm'
        )
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
            f"{longest}/{shortest}: airwires {msts[longest]:.1f} and {msts[shortest]:.1f} mm differ by {spread:.1f} mm; "
            f"skew budget {_g(budget)} mm ({source}): {_even_move(ctx, longest, shortest)} evens them, "
            f"or the router adds {max(spread - budget, 0.05):.2f} mm of serpentine"
        )
    return out


def _even_move(ctx: Ctx, a: str, b: str) -> str:
    """The part whose pads on both members throw the spread the most, put back at the nearest
    other part's pad on the long member: `Place("U3", to="J1.A6")`."""
    sites_a = ctx.pads_on.get(a, [])
    sites_b = ctx.pads_on.get(b, [])
    refs = sorted({s[0] for s in sites_a} & {s[0] for s in sites_b})
    if len(refs) < 2:
        return "move the pair's parts together"
    base = abs(_mst_mm([(s[2], s[3]) for s in sites_a]) - _mst_mm([(s[2], s[3]) for s in sites_b]))
    best = None
    for ref in refs:
        pa = [(s[2], s[3]) for s in sites_a if s[0] != ref]
        pb = [(s[2], s[3]) for s in sites_b if s[0] != ref]
        if len(pa) < 2 or len(pb) < 2:
            continue
        gain = base - abs(_mst_mm(pa) - _mst_mm(pb))
        if best is None or gain > best[0] + 1e-9:
            best = (gain, ref)
    if best is None:
        return "move the pair's parts together"
    ref = best[1]
    f = ctx.feet[ref]
    cx, cy = f.center()
    others = sorted((math.hypot(s[2] - cx, s[3] - cy), s[0], s[1]) for s in sites_a if s[0] != ref)
    _d, tref, tnum = others[0]
    return f'Place("{ref}", to="{tref}.{tnum}")'


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
    """F.2: a `Chain`'s pads project in order along the feed and no other net's pad sits in the
    corridor between consecutive pads; a usb_hs or sense net with 3+ pads and no Chain must have its
    pads on one line, else the message says the Chain line to type."""
    out: list[str] = []
    cs = ctx.cs
    chained = {ch.net for ch in ctx.design.chains}
    for ch in ctx.design.chains:
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
        label = f"{ch.net} chain {' -> '.join(ch.pads)}"
        ordered = True
        for i in range(len(ts) - 1):
            if ts[i] >= ts[i + 1] - 1e-9:
                ordered = False
                ref_i = pads[i][0]  # type: ignore[index]
                ref_j, pad_j = pads[i + 1]  # type: ignore[misc]
                what = "the cap" if ref_i.startswith("C") else ref_i
                toward = _quantise(-dx, -dy)
                out.append(
                    f"{label}: {ch.pads[i]} projects after {ch.pads[i + 1]} along the feed (t = {ts[i]:.1f} vs {ts[i + 1]:.1f} mm); "
                    f'{what} must come first: Place("{ref_i}", to="{ref_j}.{_pin(ctx, ref_j, pad_j.num)}", toward="{toward}")'
                )
                break
        if not ordered:
            continue
        half = _corridor_half(c)
        for i in range(len(pads) - 1):
            if math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) > CHAIN_HOP_MM + 1e-9:
                continue  # a hop across the board: the router detours; only a placed hop must be straight
            for _t, who, net in _corridor_hits(ctx, ch.net, half, pads[i], pads[i + 1]):  # type: ignore[arg-type]
                out.append(f"{ch.net} chain: {who} ({net or 'no net'}) lies in the corridor between {ch.pads[i]} and {ch.pads[i + 1]}; move {who.split('.', 1)[0]}")
    # implicit chains: one pad per part, on a line
    for c in cs.constraints:
        if c.kind not in ("usb_hs", "sense") or c.net in chained:
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
        chain = ", ".join(f'"{r}.{n}"' for r, n, _x, _y in pads)
        out.append(f'{c.net} has {len(pads)} pads ({names}): a third pad off the line is a stub; say the order: Chain("{c.net}", {chain})')
    return out


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
    """A closed row's fanout lanes as world strips, by side."""
    if not f.closed or f.keep is None:
        return {}
    b, k = f.world_box(), f.world_keep()
    strips: dict[str, tuple[float, float, float, float]] = {}
    for side in sorted(set(f.escape.values())):
        # the local side, turned by the footprint's rotation, is where the strip lies in the world
        lx, ly = {"top": (0.0, -1.0), "bottom": (0.0, 1.0), "left": (-1.0, 0.0), "right": (1.0, 0.0)}[side]
        wx, wy = _rotate(lx, ly, f.rot)
        wside = _quantise(wx, wy)
        if wside == "up" and k[1] < b[1] - 1e-9:
            strips[side] = (k[0], k[1], k[2], b[1])
        elif wside == "down" and k[3] > b[3] + 1e-9:
            strips[side] = (k[0], b[3], k[2], k[3])
        elif wside == "left" and k[0] < b[0] - 1e-9:
            strips[side] = (k[0], k[1], b[0], k[3])
        elif wside == "right" and k[2] > b[2] + 1e-9:
            strips[side] = (b[2], k[1], k[2], k[3])
    return strips


def check_corridors(ctx: Ctx) -> list[str]:
    """F.3: a net whose class is 0.4 mm or wider needs a channel of width + 2 clearances between
    every MST-adjacent pair of its pads on at least one allowed outer layer. Rasterised: other-net
    pads (through holes on both layers), keepouts, closed rows' fanout lanes and the edge band,
    each dilated by the channel's half width; the net's own pads are always passable."""
    out: list[str] = []
    cs = ctx.cs
    stack = cs.stackup
    outer = [lay for lay in stack.copper_layers() if is_outer(lay)]
    for c in cs.constraints:
        w = c.width_mm.value
        if w < WIDE_MM:
            continue
        layers = [lay for lay in c.layers if is_outer(lay)]
        if not layers:
            continue
        sites = ctx.pads_on.get(c.net, [])
        if len(sites) < 2:
            continue
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
            for s in sites:
                g.fill(_box_of(s), 0)
            grids[lay] = g
            obstacles[lay] = obs
        pts = [(x, y) for _r, _n, x, y, _w, _h in sites]
        for ia, ib in _mst_edges(pts):
            a, b = sites[ia], sites[ib]
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
            out.append(_corridor_message(ctx, c, a, b, layers, outer, obstacles, half))
    return out


def _side(a, b, box) -> int:
    """Which side of the line a-b a box's centre lies on (the sign of the cross product)."""
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return 1 if _orient(a, b, (cx, cy)) >= 0 else -1


def _corridor_message(ctx: Ctx, c: Constraint, a, b, layers, outer, obstacles, half: float) -> str:
    w, clr = c.width_mm.value, c.clearance_mm.value
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
    if n1 == "the board edge":
        fix = f"move {a[0] if _seg_rect_dist(pa, pa, b1) <= _seg_rect_dist(pb, pb, b1) else b[0]} in"
    elif n1.endswith("'s fanout lane"):
        fix = f"move {n1[: -len(chr(39) + 's fanout lane')]}"
    elif n1.startswith("Keepout "):
        fix = f"move {n1}"
    elif n1:
        fix = _place_line(ctx, n1.split(".", 1)[0], c.net)
    else:
        fix = f"move {a[0]}"
    missing = [lay for lay in outer if lay not in c.layers]
    alt = f'; or NetReq("{c.net}", layers={list(missing)!r})'.replace("'", '"') if missing else ""
    return (
        f"{c.net}: no {w:.2f} mm channel from {a[0]}.{_pin(ctx, a[0], a[1])} to {b[0]}.{_pin(ctx, b[0], b[1])} on {' or '.join(layers)} "
        f"({_g(w)} width + 2 x {_g(clr)} clearance = {2 * half:.2f} mm): {pinch}; {fix}{alt}"
    )


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
    for ref in sorted(ctx.feet):
        f = ctx.feet[ref]
        if not ref.startswith("C") or len(f.pads) != 2:
            continue
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
                f"encloses {area:.1f} mm2 over the {_g(budget)} mm2 budget ({_loop_src(c, 'power')}): "
                f'Place("{ref}", to="{ic}.{_pin(ctx, ic, ip.num)}") closes it'
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
        far_pin = _pin(ctx, ic, (ic_rail if far == cin else ic_sw).num)
        suggested = math.ceil(area / 5.0) * 5
        # The NetReq that already names this net, so the AI edits that line instead of writing a
        # second one (two NetReqs on one net is itself a refusal).
        sw_where = f"NetReq line {c.line}" if c.line else f'NetReq("{sw}", kind="switch_node")'
        path = " -> ".join(f"{r}.{_pin(ctx, r, p.num)}" for r, p in chain)
        moves.append(
            f"{sw}: hot loop {path} encloses {area:.1f} mm2 over the {_g(budget)} mm2 budget ({_loop_src(c, 'switch_node')}): "
            f'Place("{far}", to="{ic}.{far_pin}") closes it, or loop_mm2={suggested} on {sw_where} records it as intent'
        )
    return moves, notes


# ---------------------------------------------------------------- F.5 keep-away


def check_keep_away(ctx: Ctx) -> list[str]:
    """F.5: the edge-to-edge distance between a net's pads and the other net's pads, and the
    courtyards of the parts carrying it. A part carrying both nets is its rating, not layout."""
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
            ic = _ic_pad_on(ctx, c.net, exclude=a[0])
            fix = f'Place("{a[0]}", to="{ic[0]}.{ic[1]}", toward="{toward}")' if ic else f"move {a[0]} {toward}"
            relax = math.floor(d * 2.0) / 2.0
            out.append(
                f"{c.net}: {a[0]}.{_pin(ctx, a[0], a[1])} is {d:.2f} mm from {what}; keep_clear_mm={_g(ka.mm)} ({src}): "
                f"{fix}, or keep_clear_mm={_g(relax)} if {oref} must sit there"
            )
    return out


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
        if c.reference is None:
            continue
        if c.pair is not None:
            controlled = c.pair.controlled
            what = "the pair"
        elif c.z_se is not None:
            controlled = True
            what = "the track"
        else:
            continue
        if not controlled:
            continue
        sites = ctx.pads_on.get(c.net, [])
        if stack.layers > 2:
            plane_net = next((n for n, lay in ctx.job.planes if lay == c.reference), None)
            plane = f"{c.reference} {plane_net}" if plane_net else c.reference
            named: set[str] = set()  # one line per part: its first pad off the plane says it
            for ref, num, x, y, w, h in sites:
                if ref in named:
                    continue
                box = (x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)
                if not (box[0] >= inner.x0 - 1e-6 and box[1] >= inner.y0 - 1e-6 and box[2] <= inner.x1 + 1e-6 and box[3] <= inner.y1 + 1e-6):
                    named.add(ref)
                    moves.append(
                        f"{c.net}: {ref}.{num} sits within {_g(edge)} mm of the board edge: no {plane} under it, {what} has no reference there; "
                        f"move {ref} in"
                    )
                    continue
                for name, ko in copper_kos:
                    if _overlap(box, ko):
                        named.add(ref)
                        moves.append(
                            f"{c.net}: {ref}.{num} sits in Keepout {name}: no {plane} under it, {what} has no reference there; "
                            f"move the keepout or {_keepout_escape(ctx, ref, ko)}"
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


def _keepout_escape(ctx: Ctx, ref: str, ko: tuple[float, float, float, float]) -> str:
    """The Place() line that takes `ref` out of a keepout: along its edge for an edge-placed part,
    away from the box for a relation, `move` otherwise."""
    spec = next((p for p in ctx.job.places if p.ref == ref), None)
    f = ctx.feet[ref]
    cx, cy = f.center()
    kx, ky = (ko[0] + ko[2]) / 2.0, (ko[1] + ko[3]) / 2.0
    away = _quantise(cx - kx, cy - ky)
    if spec is not None and spec.edge:
        if spec.edge in ("top", "bottom"):
            if away == "right":
                return f'Place("{ref}", edge="{spec.edge}", left={round(ko[2] + GAP - ctx.content.x0, 1):g})'
            return f'Place("{ref}", edge="{spec.edge}", right={round(ctx.content.x1 - (ko[0] - GAP), 1):g})'
        if away == "down":
            return f'Place("{ref}", edge="{spec.edge}", top={round(ko[3] + GAP - ctx.content.y0, 1):g})'
        return f'Place("{ref}", edge="{spec.edge}", bottom={round(ctx.content.y1 - (ko[1] - GAP), 1):g})'
    if spec is not None and spec.to:
        return f'Place("{ref}", to="{spec.to}", toward="{away}")'
    return f"move {ref} {away}"


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
            out.append(
                f"{tag} ({_g(iso.volts)} V): Regions are {gap:.1f} mm apart; needs {need}: "
                f'Region("{far_name}", {shift}={spec.gap_min_mm - gap:+.1f})'
            )
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
                    out.append(f'{tag}: {ref} is in across= but does not span the gap; Place("{ref}", parent="{side}", {edge}={-gap:.1f})')
                    continue
                for p in f.pads:
                    if not p.num:
                        continue
                    box = _pad_box(f, p)
                    inside = low.x1 < box[0] and box[2] < high.x0 if axis == "x" else low.y1 < box[1] and box[3] < high.y0
                    if inside:
                        lo, hi = (corridor[0], corridor[2]) if axis == "x" else (corridor[1], corridor[3])
                        out.append(f"{tag}: {ref}.{p.num} is in across= but lies in the gap ({lo:.1f}..{hi:.1f} mm): copper in the corridor is copper across the barrier; move {ref}")
                        break
                continue
            side = sides.get(ref)
            if side is None:
                continue  # compile refused it
            rect = ra if side == iso.a else rb
            if crt[0] >= rect.x0 - 1e-6 and crt[1] >= rect.y0 - 1e-6 and crt[2] <= rect.x1 + 1e-6 and crt[3] <= rect.y1 + 1e-6:
                continue
            if _overlap(crt, corridor):
                out.append(f"{tag}: {ref} ({side}) crosses the corridor: move {ref}")
            else:
                out.append(f"{tag}: {ref} ({side}) lies outside Region {side}: move {ref}")
        area = next((a for a in ctx.cs.rule_areas if a.name == f"ISO_{iso.a}_{iso.b}"), None)
        if area is not None:
            placed = (round(low.x1, 4), round(high.x0, 4)) if axis == "x" else (round(low.y1, 4), round(high.y0, 4))
            recorded = (area.box[0], area.box[2]) if axis == "x" else (area.box[1], area.box[3])
            if placed != recorded:
                out.append(f"{tag}: rule area {area.name} spans {recorded[0]:g}..{recorded[1]:g} mm but the Regions are at {placed[0]:g}..{placed[1]:g}")
    return out


__all__ = [
    "Ctx",
    "build_ctx",
    "check_airwires",
    "check_chains",
    "check_corridors",
    "check_isolation",
    "check_keep_away",
    "check_loops",
    "check_reference",
    "route_aware_report",
]
