"""The native router: a cost field made only of derived numbers, searched on an octilinear lattice.

Started from the cost-field spike (`docs/direction.md` §5). The objective of every path is the net's
own cost, in millimetres of its own copper:

    cost(path) = length_mm
               + sum over corners of  pitch_mm * tan(theta / 2)        [the bend term]
               + vias * via_mm                                          [the layer term]

    pitch_mm = width_mm + clearance_mm                                  (both `constraints.Derived`)
    via_mm   = per_change * pi * (via_diameter / 2 + clearance)^2 / pitch_mm

The bend term is exact geometry: two octilinear paths offset by `pitch` differ in length at a corner of
turn theta by exactly `pitch * tan(theta/2)`, so a corner costs the copper it forces on the next track
in the same channel. The three octilinear turns use the exact values tan 22.5 = sqrt(2) - 1,
tan 45 = 1, tan 67.5 = sqrt(2) + 1 (`math.sqrt` is correctly rounded on every IEEE platform; `math.tan`
is not guaranteed to be), so two machines price a corner identically.

**`route_scene.blocked` is the only legality judge**, on every stub, every pulled run and every via;
the lattice (`Slack`) is a proposal. On top of `blocked`, a router run obeys three rules the scene
leaves to its callers: it may not run **along** a fanout lane (`lane_overrun`), a
via may not sit in a lane (`via_in_lane`) nor in an SMD pad of its own net (fab refuses via-in-pad on
a passive), and a via may not merge its antipad with a neighbour's in a plane it does not join
(`antipad_clash`).

Determinism: no wall clock, no randomness; the A* heap is ordered by `(f, g, seq)` with a monotonic
`seq`; the search budget is a count of node expansions; every coordinate goes through
`route_geom.q` once.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .route_emit import seg_piece, via_piece
from .route_geom import MICRO_MM, aabb, clears, clip_len_in_box, hull_dist2, octile_path, qp, via_shape
from .route_scene import _MAX_NEED, antipad_clash, blocked

DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
SQ2 = math.sqrt(2.0)
TAN_HALF = {0: 0.0, 1: SQ2 - 1.0, 2: 1.0, 3: SQ2 + 1.0}
"""tan(theta/2) for an octilinear turn of k * 45 degrees, exactly (k = 4 is a U-turn: refused)."""
UTURN = 1e6
NECK_STEP_MM = 0.1
"""The pad neck's length is tried in these steps past the pad's edge, shortest first, up to
`constraints.PAD_NECK_MAX_MM` (decision 1)."""
BUDGET = 200000
"""Node expansions one A* search may make before it gives up (a count, never seconds)."""


REFUSED = {
    "spacing_w": "Constraint.spacing_w (3W/5W) says how far from an aggressor is preferred, but nothing compiles what a millimetre of encroachment is worth in millimetres of copper. Not priced.",
    "keep_away": "KeepAway.mm is a hard distance already inside ClearanceTable.between: a bound, not a cost.",
    "pair.skew_mm": "A budget on |len(DP)-len(DN)| with no compiled exchange rate against copper. Reported, not priced.",
    "pair.uncoupled_mm": "A budget on uncoupled run with no compiled price per millimetre. Reported, not priced.",
    "loop_mm2": "An area budget on a loop of two nets: not a property of one path's cost.",
    "reference / z_se / z_diff": "Reaches geometry as width_mm and gap_mm, which are priced; leaving the reference plane has no compiled price.",
    "airwire_max_mm": "A placement budget by its own docstring, never a routing cost.",
    "length_max_mm": "A bound KiCad checks (a length rule, severity warning); the router reports it, it does not price it.",
}


@dataclass(frozen=True)
class NetCost:
    """The compiled requirements of one net, as the terms of its cost and the bounds on its search."""

    net: str
    width_mm: float
    clearance_mm: float
    layers: tuple[str, ...]
    via_allowed: bool
    via_count_max: int | None
    via_dia: float
    via_drill: float
    via_per_change: int
    length_max_mm: float | None
    pair_partner: str | None = None
    pair_gap_mm: float | None = None
    pair_width_mm: float | None = None
    pair_skew_mm: float | None = None
    pair_uncoupled_mm: float | None = None
    prov: dict = field(default_factory=dict, compare=False)
    layer_widths: tuple[tuple[str, float], ...] = ()
    """The width per layer when it differs by layer (a current over a plane on one layer and none on
    the other, `docs/net-path-physics.md` §2); `()` when every layer has `width_mm`. Read through
    `width_on`: every router read of a width is the searched layer's (§4.2)."""
    link: str = field(default="", compare=False)
    """The `Link.key` this cost is for, or `""` for the net's own (off-path) links."""
    group: object = field(default=None, compare=False)
    """The `constraints.ViaGroup` a layer change of this link places (§4.3), or None for one via."""
    keep_away: tuple = field(default=(), compare=False)
    """The link's keep-aways (`constraints.KeepAway`), the net's plus its path's."""
    rule: object = field(default=None, compare=False)
    """The junction rule (`route_scene.JunctionRule`) a same-net item is judged by on a net whose paths
    carry a field or kind (§3.2), or None: same-net copper is never an obstacle."""

    def width_on(self, layer: str | None) -> float:
        if layer is not None:
            for lay, w in self.layer_widths:
                if lay == layer:
                    return w
        return self.width_mm

    def pitch_on(self, layer: str | None) -> float:
        return self.width_on(layer) + self.clearance_mm

    def turn_on(self, k: int, layer: str | None) -> float:
        if k >= 4:
            return UTURN
        return self.pitch_on(layer) * TAN_HALF[k]

    def bend_on(self, theta_deg: float, layer: str | None) -> float:
        if theta_deg <= 0.5:
            return 0.0
        k = theta_deg / 45.0
        if abs(k - round(k)) < 1e-6:
            return self.turn_on(int(round(k)), layer)
        if theta_deg >= 179.0:
            return UTURN
        return self.pitch_on(layer) * math.tan(math.radians(theta_deg) / 2.0)

    def via_mm_on(self, layer: str | None) -> float:
        """The layer term of one change: one via (or, with a group, `count` of them) displacing the
        channel copper of the layer it leaves."""
        if not self.via_allowed:
            return math.inf
        r = self.via_dia / 2.0 + self.clearance_mm
        n = self.group.count if self.group is not None else self.via_per_change
        return n * math.pi * r * r / self.pitch_on(layer)

    @property
    def pitch_mm(self) -> float:
        return self.width_mm + self.clearance_mm

    @property
    def via_mm(self) -> float:
        if not self.via_allowed:
            return math.inf
        r = self.via_dia / 2.0 + self.clearance_mm
        return self.via_per_change * math.pi * r * r / self.pitch_mm

    def turn_mm(self, k: int) -> float:
        """The bend term of a turn of k * 45 degrees (k in 0..4)."""
        if k >= 4:
            return UTURN
        return self.pitch_mm * TAN_HALF[k]

    def bend_mm(self, theta_deg: float) -> float:
        """The bend term of any turn, for a run that is not on the lattice (a pulled path is octilinear,
        so this lands on an exact `TAN_HALF` value; anything else is priced by the same identity)."""
        if theta_deg <= 0.5:
            return 0.0
        k = theta_deg / 45.0
        if abs(k - round(k)) < 1e-6:
            return self.turn_mm(int(round(k)))
        if theta_deg >= 179.0:
            return UTURN
        return self.pitch_mm * math.tan(math.radians(theta_deg) / 2.0)


def net_cost(cs, net: str) -> NetCost:
    """`NetCost` from the compiled constraint of `net`, or the Default class when no `NetReq` names it
    (KiCad gives such a net the Default net class and `ClearanceTable` falls back to it the same way)."""
    c = cs.by_net(net)
    if c is None:
        d = next(k for k in cs.classes if k.name == "Default")
        return NetCost(
            net=net, width_mm=d.track_width_mm, clearance_mm=d.clearance_mm, layers=tuple(cs.stackup.copper_layers()),
            via_allowed=True, via_count_max=None, via_dia=d.via_diameter_mm, via_drill=d.via_drill_mm, via_per_change=1,
            length_max_mm=None,
            prov={"width_mm": f"{d.track_width_mm} mm (class Default; no NetReq names {net})", "clearance_mm": f"{d.clearance_mm} mm (class Default)"},
        )
    return NetCost(
        net=net,
        width_mm=c.width_mm.value,
        clearance_mm=c.clearance_mm.value,
        layers=tuple(c.layers),
        via_allowed=c.via.allowed,
        via_count_max=c.via.count_max,
        via_dia=c.via.diameter_mm,
        via_drill=c.via.drill_mm,
        via_per_change=c.via.per_change,
        length_max_mm=c.length_max_mm.value if c.length_max_mm else None,
        pair_partner=c.pair.partner if c.pair else None,
        pair_gap_mm=c.pair.gap_mm.value if c.pair else None,
        pair_width_mm=c.pair.width_mm.value if c.pair else None,
        pair_skew_mm=c.pair.skew_mm.value if c.pair else None,
        pair_uncoupled_mm=c.pair.uncoupled_mm.value if c.pair else None,
        prov={"width_mm": c.width_mm.line(), "clearance_mm": c.clearance_mm.line(), "layers": f"{c.layers} (Constraint.layers)"},
        # A net carries no current of its own (docs/physics-subjects.md PR 3): one width, no via group;
        # a path's link brings its own through `link_nc`.
        keep_away=tuple(c.keep_away),
    )


def _layer_widths(base: float, widths) -> tuple[tuple[str, float], ...]:
    """`()` when every layer has `base` (so a net or a field-less link compares equal to today's)."""
    got = tuple((lay, d.value) for lay, d in widths)
    return got if any(abs(w - base) > 1e-12 for _lay, w in got) else ()


def link_cost(cs, net: str, pad_a: str, pad_b: str) -> NetCost:
    """The cost of one link (`docs/net-path-physics.md` §4.2): the link's width per layer, layers, vias
    allowed, via size (KD 13 of `docs/physics-subjects.md`), via group and keep-aways when `pad_a`-`pad_b`
    is a declared link of a path, else the net's (`net_cost`). Clearance and the via budget are the
    net's. A field-less link is `==` its net's cost."""
    base = net_cost(cs, net)
    if cs is None or not hasattr(cs, "link"):
        return base
    lk = cs.link(net, pad_a, pad_b)
    if lk is None:
        return base
    return link_nc(base, lk)


def link_nc(base: NetCost, lk) -> NetCost:
    """`base` (the net's) with `lk`'s numbers."""
    from dataclasses import replace as _replace

    first = lk.layers[0] if lk.layers else None
    w0 = lk.width_on(first) if first else base.width_mm
    prov = dict(base.prov)
    prov["link"] = f"{lk.key} ({lk.source})"
    if lk.layers_src:
        prov["layers"] = f"{lk.layers} ({lk.layers_src})"
    if lk.vias_src:
        prov["vias"] = lk.vias_src
    return _replace(
        base,
        width_mm=w0,
        layer_widths=_layer_widths(w0, lk.width),
        layers=tuple(lk.layers),
        via_allowed=lk.via.allowed,
        via_count_max=lk.via.count_max,
        via_dia=lk.via.diameter_mm,  # KD 13: a current link's own via size (docs/physics-subjects.md)
        via_drill=lk.via.drill_mm,
        via_per_change=lk.via.per_change,
        link=lk.key,
        group=lk.group,
        keep_away=tuple(lk.keep_away),
        prov=prov,
    )


# --- geometry helpers ------------------------------------------------------------------------------


def path_mm(pts) -> float:
    return math.fsum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def _dir8(a, b) -> int:
    dx, dy = b[0] - a[0], b[1] - a[1]
    sx = 0 if abs(dx) < 1e-9 else (1 if dx > 0 else -1)
    sy = 0 if abs(dy) < 1e-9 else (1 if dy > 0 else -1)
    try:
        return DIRS.index((sx, sy))
    except ValueError:
        return -1


def _turn_k(d0: int, d1: int) -> int:
    if d0 < 0 or d1 < 0:
        return 0
    k = abs(d0 - d1) % 8
    return min(k, 8 - k)


def _ang(a, b, c) -> float:
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    l1, l2 = math.hypot(*v1), math.hypot(*v2)
    if l1 < 1e-12 or l2 < 1e-12:
        return 0.0
    cs = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)))
    return math.degrees(math.acos(cs))


def short_legs(pts) -> int:
    """Legs under `MICRO_MM` — pcbc's own `track_segment_length (min 0.2mm)` rule: a bound on the
    search, never a term in the objective."""
    return sum(1 for a, b in zip(pts, pts[1:]) if 0.0 < math.dist(a, b) < MICRO_MM)


def run_cost(pts, nc: NetCost, layer: str | None = None) -> float:
    """Length plus the bend term at the searched layer's pitch (`NetCost.width_on`)."""
    c = path_mm(pts)
    for i in range(1, len(pts) - 1):
        c += nc.bend_on(_ang(pts[i - 1], pts[i], pts[i + 1]), layer)
    return c


def _dedup(pts):
    out = []
    for p in pts:
        p = qp(p)
        if not out or out[-1] != p:
            out.append(p)
    return tuple(out)


def _straight(pts):
    """Drop a vertex that is not a corner."""
    if len(pts) < 3:
        return tuple(pts)
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        if _ang(pts[i - 1], pts[i], pts[i + 1]) > 0.5:
            out.append(pts[i])
    out.append(pts[-1])
    return _dedup(out)


# --- the clearance slack field ---------------------------------------------------------------------


class Slack:
    """`at(p)` = min over items of (distance from p to the item - what this net owes it): a track of
    width w may have its centreline at p iff `at(p) >= w/2`. A point query, 1-Lipschitz in p, which is
    what makes a lattice usable; never the arbiter (`blocked` is)."""

    def __init__(self, scene, net: str, layer: str, ignore: frozenset[int] = frozenset(), rule=None):
        self.scene = scene
        self.net = net
        self.layer = layer
        self.ignore = ignore
        self.rule = rule  # route_scene.JunctionRule: a foreign same-net item is priced at the net's clearance
        self.pad = scene._max_r + _MAX_NEED
        self.h2c = scene.table.hole_to_copper()
        self._need: dict[str, float] = {}
        self._parts: dict[int, list] = {}  # item id -> its copper wholly outside the rule's pads

    def need(self, other: str) -> float:
        v = self._need.get(other)
        if v is None:
            v = self.scene.table.between(self.net, other)[0]  # a keep-away is net to net (physics-subjects PR 7)
            self._need[other] = v
        return v

    def _foreign_parts(self, i: int, it) -> list:
        got = self._parts.get(i)
        if got is None:
            from .route_geom import _deep_parts

            if it.copper is None:
                got = []
            elif it.kind == "track" and len(it.copper.pts) == 2:
                got = _deep_parts(("track", it.copper.pts[0], it.copper.pts[-1], round(2 * it.copper.r, 6)), it.copper, self.rule.allowed, self.rule.clearance)
            elif it.kind == "via":
                got = _deep_parts(("via", it.at(), round(2 * it.copper.r, 6)), it.copper, self.rule.allowed, self.rule.clearance)
            else:
                got = [it.copper]
            self._parts[i] = got
        return got

    def nearest(self, x: float, y: float) -> tuple[float, int]:
        """`at`'s answer and the item that sets it (-1 for the board edge): what holds this point, for
        a failure that has to name it."""
        best, who = math.inf, -1
        p = ((x, y),)
        rule = self.rule
        inside = rule.in_allowed((x, y)) if rule is not None else True
        for i in self.scene.query(self.layer, (x, y, x, y), self.pad):
            if i in self.ignore:
                continue
            it = self.scene.items[i]
            if it.kind in ("lane", "edge") or self.layer not in it.layers:
                continue
            same = bool(self.net) and it.net == self.net
            if same and not inside and rule.foreign(it):
                for sh in self._foreign_parts(i, it):
                    d = math.sqrt(hull_dist2(p, sh.pts)) - sh.r - rule.clearance
                    if d < best:
                        best, who = d, i
                continue
            if it.copper is not None and not same:
                d = math.sqrt(hull_dist2(p, it.copper.pts)) - it.copper.r - self.need(it.net)
                if d < best:
                    best, who = d, i
            if it.hole is not None and not same:
                d = math.sqrt(hull_dist2(p, it.hole.pts)) - it.hole.r - self.h2c
                if d < best:
                    best, who = d, i
        o = self.scene.outline
        for d in (x - o[0], y - o[1], o[2] - x, o[3] - y):
            if d < best:
                best, who = d, -1
        return best, who

    def at(self, x: float, y: float) -> float:
        best = math.inf
        p = ((x, y),)
        rule = self.rule
        inside = rule.in_allowed((x, y)) if rule is not None else True
        for i in self.scene.query(self.layer, (x, y, x, y), self.pad):
            if i in self.ignore:
                continue
            it = self.scene.items[i]
            if it.kind in ("lane", "edge") or self.layer not in it.layers:
                continue
            same = bool(self.net) and it.net == self.net
            if same and not inside and rule.foreign(it):
                # §3.2: foreign same-net copper is an obstacle at the net's clearance, outside the pads
                # where contact is legal; the exact judge is `JunctionRule.clashes`. Only its copper that
                # lies wholly outside those pads (`route_geom._deep_parts`): a wide track ending in a
                # station pad reaches past the pad with its round end, and that end is not a junction
                # outside it (refutation round 2: a sense tap could not leave a pad a 2.276 mm rail ends in).
                for sh in self._foreign_parts(i, it):
                    d = math.sqrt(hull_dist2(p, sh.pts)) - sh.r - rule.clearance
                    if d < best:
                        best = d
                continue
            if it.copper is not None and not same:
                d = math.sqrt(hull_dist2(p, it.copper.pts)) - it.copper.r - self.need(it.net)
                if d < best:
                    best = d
            if it.hole is not None and not same:
                d = math.sqrt(hull_dist2(p, it.hole.pts)) - it.hole.r - self.h2c
                if d < best:
                    best = d
        o = self.scene.outline
        for d in (x - o[0], y - o[1], o[2] - x, o[3] - y):
            if d < best:
                best = d
        return best


@dataclass
class Lattice:
    step: float
    x0: float
    y0: float
    nx: int
    ny: int

    def pt(self, ix: int, iy: int):
        return qp((self.x0 + ix * self.step, self.y0 + iy * self.step))

    def cell(self, p) -> tuple[int, int]:
        return (int(round((p[0] - self.x0) / self.step)), int(round((p[1] - self.y0) / self.step)))

    def near(self, p, rings: int = 3):
        ix, iy = self.cell(p)
        out = []
        for dy in range(-rings, rings + 1):
            for dx in range(-rings, rings + 1):
                jx, jy = ix + dx, iy + dy
                if 0 <= jx < self.nx and 0 <= jy < self.ny:
                    out.append((jx, jy))
        out.sort(key=lambda c: (math.dist(p, self.pt(*c)), c))
        return out


def lattice_step(stack) -> float:
    """The router's lattice pitch: the fab's own finest track pitch, `track_min + clearance_min` (two
    `Stackup` numbers). A board constant, not a per-net knob, and not `Scene.grid` (the patterns'
    candidate step, which stays what it was)."""
    return round(stack.track_min + stack.clearance_min, 6)


# --- the router --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupPlan:
    """One placed via group (§4.3): the line from `p1` (where the arriving run on `layer_from` ends) to
    `pn` (where the leaving run on `layer_to` starts), its barrels, and the landing both layers lay over
    every barrel."""

    layer_from: str
    layer_to: str
    p1: tuple[float, float]
    pn: tuple[float, float]
    barrels: tuple[tuple[float, float], ...]
    landing_mm: float
    pitch_mm: float
    worst_a: float
    cell: tuple[int, int]
    gd: int
    cost: float


class Router:
    """One net's router over the live scene. `link(a, b)` returns a list of `(layer, pts)` runs joined
    by vias at their shared ends, or None."""

    def __init__(self, scene, nc: NetCost, layers, *, ignore=frozenset(), exempt=frozenset(), vias_left: int | None = None, budget: int | None = None):
        self.scene = scene
        self.nc = nc
        self.layers = tuple(layers)
        self.step = lattice_step(scene.stack)
        o = scene.outline
        self.lat = Lattice(self.step, o[0], o[1], max(2, int((o[2] - o[0]) / self.step) + 1), max(2, int((o[3] - o[1]) / self.step) + 1))
        self.ignore = frozenset(ignore)
        self.exempt = frozenset(exempt)
        self.slack = {L: Slack(scene, nc.net, L, self.ignore, nc.rule) for L in self.layers}
        self.free: dict[str, dict] = {L: {} for L in self.layers}
        self.via_ok: dict = {}
        self.banned: set = set()
        cap = 4 if nc.via_count_max is None else nc.via_count_max
        if vias_left is not None:
            cap = min(cap, vias_left)
        self.via_cap = cap if nc.via_allowed else 0
        self.budget = BUDGET if budget is None else budget
        self.calls = 0
        self.expanded = 0
        self.lanes = [(it.owner.split(" ")[0], aabb(it.copper), min(aabb(it.copper)[2] - aabb(it.copper)[0], aabb(it.copper)[3] - aabb(it.copper)[1])) for it in scene.items if it.kind == "lane" and it.copper is not None]
        self.smd = [it for it in scene.items if it.kind == "pad" and it.net == nc.net and it.hole is None and it.copper is not None]
        self.last_hits: dict[int, int] = {}
        # A layer change carrying a current places its via group (§4.3): a flow-through line, laid as
        # one macro move of the search. One barrel holds it otherwise, as before.
        grp = nc.group
        self.group = grp if (grp is not None and getattr(grp, "count", 1) > 1 and nc.via_allowed) else None
        self._gplans: dict = {}
        self.groups: list = []  # per boundary between consecutive runs of the last `link`: a GroupPlan or None
        self.junction = nc.rule  # route_scene.JunctionRule or None (§3.2)
        # Decision 1, the pad neck: {frozenset of a stub's two ends: its width}. A path's link may leave (or
        # reach) a pad narrower than its width at no more than the pad's own width across the exit, for at
        # most `constraints.PAD_NECK_MAX_MM` from the pad's edge, and then runs at full width (`_neck_ends`).
        self.necks: dict = {}
        self.allow_neck = False  # set by `link` for its second search only: a neck is a compromise, used only when no full-width route exists

    # -- legality --------------------------------------------------------------------------------

    def cell_free(self, L, ix, iy) -> bool:
        f = self.free[L]
        v = f.get((ix, iy))
        if v is None:
            p = self.lat.pt(ix, iy)
            v = self.slack[L].at(p[0], p[1]) >= self.nc.width_on(L) / 2.0
            f[(ix, iy)] = v
        return v

    def _lane_ok(self, a, b, L=None, w=None) -> bool:
        w = self.nc.width_on(L) if w is None else w
        for ref, box, span in self.lanes:
            if ref in self.exempt:
                continue
            if clip_len_in_box(a, b, box) > span + w + 2 * self.scene.grid:
                return False
        return True

    def legal(self, L, pts, ignore=frozenset(), width=None) -> bool:
        ignore = ignore | self.ignore
        self.calls += 1
        w = self.nc.width_on(L) if width is None else width
        ps = [seg_piece(self.nc.net, "route", L, a, b, self.necks.get(frozenset((qp(a), qp(b))), w)) for a, b in zip(pts, pts[1:]) if qp(a) != qp(b)]
        if not ps:
            return True
        for p in ps:
            if not self._lane_ok(p.a, p.b, L, p.w):
                return False
        hit = blocked(self.scene, ps, self.nc.net, ignore=ignore)
        if hit is not None:
            self.last_hits[hit.item.id] = self.last_hits.get(hit.item.id, 0) + 1
            return False
        if self.junction is not None:
            hit = self.junction.clashes(self.scene, ps, ignore=ignore)
            if hit is not None:
                self.last_hits[hit] = self.last_hits.get(hit, 0) + 1
                return False
        return True

    def via_legal(self, at) -> bool:
        at = qp(at)
        v = self.via_ok.get(at)
        if v is not None:
            return v
        v = self._via_legal(at)
        self.via_ok[at] = v
        return v

    def _via_legal(self, at) -> bool:
        nc = self.nc
        p = via_piece(nc.net, "route", at, nc.via_dia, nc.via_drill)
        hit = blocked(self.scene, [p], nc.net, ignore=self.ignore)
        if hit is not None:
            self.last_hits[hit.item.id] = self.last_hits.get(hit.item.id, 0) + 1
            return False
        ring = via_shape(at, nc.via_dia)
        floor = self.scene.stack.clearance_min
        for it in self.smd:
            if not clears(ring, it.copper, floor):
                return False  # a via in (or against) an SMD pad of its own net: fab refuses via-in-pad
        for ref, box, _span in self.lanes:
            if ref in self.exempt:
                continue
            x0, y0, x1, y1 = box
            r = nc.via_dia / 2.0
            if at[0] + r > x0 and at[0] - r < x1 and at[1] + r > y0 and at[1] - r < y1:
                return False
        if antipad_clash(self.scene, at, nc.via_dia, nc.net, ignore=self.ignore) is not None:
            return False
        if self.junction is not None and self.junction.clashes(self.scene, [p], ignore=self.ignore) is not None:
            return False
        return True

    # -- the via group (§4.3) ----------------------------------------------------------------------

    def group_plan(self, ix, iy, L, L2, gd):
        """The flow-through line of the link's group starting at cell (ix, iy) along direction `gd`:
        a `GroupPlan` whose first barrel is on the lattice and whose last barrel is the lattice cell the
        search continues from on `L2`, or None when it does not fit. The barrel pitch is at least the
        compiled one (the line is stretched to land on the lattice), and the count is re-asked of the
        lumped model at the pitch the line really has, so the worst barrel stays within its rating."""
        key = (ix, iy, L, L2, gd)
        if key in self._gplans:
            return self._gplans[key]
        plan = self._group_plan(ix, iy, L, L2, gd)
        self._gplans[key] = plan
        return plan

    def _group_plan(self, ix, iy, L, L2, gd):
        from .stackup import via_group

        g = self.group
        nc = self.nc
        dx, dy = DIRS[gd]
        unit = self.step * (SQ2 if dx and dy else 1.0)
        stack = self.scene.stack
        landing = max(g.landing_mm, nc.width_on(L), nc.width_on(L2))
        n = g.count
        for _ in range(4):
            k = max(1, math.ceil((n - 1) * g.pitch_mm / unit - 1e-9))
            span = k * unit
            pitch = span / (n - 1)
            # Round 3: the landings run on L and L2; their copper is the thinnest of the two (`Stackup.landing_oz`).
            got, worst = via_group(g.amps, g.drill_mm, stack.via_plating_mm, g.temp_rise_c, pitch, landing, board_mm=stack.board_mm, copper_oz=stack.landing_oz((L, L2)))
            if got <= n:
                break
            n = got
        jx, jy = ix + k * dx, iy + k * dy
        if not (0 <= jx < self.lat.nx and 0 <= jy < self.lat.ny):
            return None
        p1 = self.lat.pt(ix, iy)
        pn = self.lat.pt(jx, jy)
        barrels = tuple(qp((p1[0] + (pn[0] - p1[0]) * i / (n - 1), p1[1] + (pn[1] - p1[1]) * i / (n - 1))) for i in range(n))
        for b in barrels:
            if not self.via_legal(b):
                return None
        if not self.legal(L, (p1, pn), width=landing) or not self.legal(L2, (p1, pn), width=landing):
            return None
        cost = n * self.nc.via_mm_on(L) / max(1, g.count) * 1.0 + 2.0 * math.dist(p1, pn)
        return GroupPlan(L, L2, p1, pn, barrels, round(landing, 6), round(math.dist(p1, pn) / (n - 1), 6), worst, (jx, jy), gd, cost)


    # -- search ------------------------------------------------------------------------------------

    def _h(self, goal_cells, ix, iy, L) -> float:
        best = math.inf
        for (jx, jy) in goal_cells.get(L) or goal_cells.get("*", ()):
            dx, dy = abs(ix - jx), abs(iy - jy)
            d = (max(dx, dy) - min(dx, dy) + SQ2 * min(dx, dy)) * self.step
            if d < best:
                best = d
        return 0.0 if best is math.inf else best

    def astar(self, a, b, *, la=None, lb=None, pa=None, pb=None):
        lat, nc = self.lat, self.nc
        entries = self._ends(a, la, pa)
        exits = self._ends(b, lb, pb)
        if not entries or not exits:
            return None
        goal = exits
        goal_cells: dict = {}
        for (jx, jy, L) in sorted(goal):
            goal_cells.setdefault(L, []).append((jx, jy))
        allg = sorted({c for v in goal_cells.values() for c in v})
        goal_cells["*"] = allg
        pq: list = []
        seen: dict = {}
        seq = 0
        for key in sorted(entries):
            ix, iy, L = key
            pts, c, d0 = entries[key]
            st = (ix, iy, L, d0, 0)
            if c < seen.get(st, math.inf):
                seen[st] = c
                heapq.heappush(pq, (c + self._h(goal_cells, ix, iy, L), c, seq, st, None))
                seq += 1
        came: dict = {}
        n = 0
        while pq:
            f, c, _s, st, par = heapq.heappop(pq)
            if st in came:
                continue
            if seen.get(st, math.inf) < c - 1e-12:
                continue
            came[st] = par
            ix, iy, L, d0, nv = st
            if (ix, iy, L) in goal:
                self.expanded += n
                return self._rebuild(came, st, entries, goal)
            n += 1
            if n > self.budget:
                self.expanded += n
                return None
            for di, (dx, dy) in enumerate(DIRS):
                jx, jy = ix + dx, iy + dy
                if not (0 <= jx < lat.nx and 0 <= jy < lat.ny):
                    continue
                if (L, jx, jy) in self.banned or not self.cell_free(L, jx, jy):
                    continue
                stepc = self.step * SQ2 if (dx and dy) else self.step
                nc2 = c + stepc + nc.turn_on(_turn_k(d0, di), L)
                ns = (jx, jy, L, di, nv)
                if nc2 < seen.get(ns, math.inf) - 1e-12:
                    seen[ns] = nc2
                    heapq.heappush(pq, (nc2 + self._h(goal_cells, jx, jy, L), nc2, seq, ns, st))
                    seq += 1
            if nv < self.via_cap:
                for L2 in self.layers:
                    if L2 == L:
                        continue
                    if self.group is not None:
                        # The group is one move: the search continues on L2 from its last barrel.
                        for gd in range(8):
                            plan = self.group_plan(ix, iy, L, L2, gd)
                            if plan is None:
                                continue
                            jx, jy = plan.cell
                            if (L2, jx, jy) in self.banned or not self.cell_free(L2, jx, jy):
                                continue
                            nc2 = c + plan.cost
                            ns = (jx, jy, L2, gd, nv + 1)
                            if nc2 < seen.get(ns, math.inf) - 1e-12:
                                seen[ns] = nc2
                                heapq.heappush(pq, (nc2 + self._h(goal_cells, jx, jy, L2), nc2, seq, ns, st))
                                seq += 1
                        continue
                    if not self.cell_free(L2, ix, iy):
                        continue
                    if not self.via_legal(lat.pt(ix, iy)):
                        continue
                    nc2 = c + nc.via_mm_on(L)
                    ns = (ix, iy, L2, d0, nv + 1)
                    if nc2 < seen.get(ns, math.inf) - 1e-12:
                        seen[ns] = nc2
                        heapq.heappush(pq, (nc2 + self._h(goal_cells, ix, iy, L2), nc2, seq, ns, st))
                        seq += 1
        self.expanded += n
        return None

    def _ends(self, p, only=None, pad=None):
        """An off-lattice terminal -> the lattice cells it reaches by a legal stub, with its cost.

        Two kinds of stub: straight from the terminal to a lattice cell within three rings, and — when
        the terminal is a pad (`pad`, its scene item) — out through one of the pad's own exits
        (`route_scene.pad_exits`: straight out of the pad, past its neighbours' clearance) and on to a
        cell within two rings of the exit. A fine-pitch connector's pad has no free cell within three
        rings of its centre; it has an exit (docs/native-plan.md, critique #21)."""
        from .route_scene import pad_exits

        out = {}
        for L in (self.layers if only is None else tuple(x for x in self.layers if x in only)):
            got = 0
            for (ix, iy) in self.lat.near(p, rings=3):
                if got >= 6:
                    break
                if (L, ix, iy) in self.banned or not self.cell_free(L, ix, iy):
                    continue
                c = self.lat.pt(ix, iy)
                if qp(c) == qp(p):
                    pts, d0 = (qp(p),), -1
                else:
                    pts = octile_path(qp(p), c)
                    if not self.legal(L, pts):
                        continue
                    d0 = _dir8(pts[-2], pts[-1])
                out[(ix, iy, L)] = (pts, run_cost(pts, self.nc, L) if len(pts) > 1 else 0.0, d0)
                got += 1
            if pad is None or L not in pad.layers:
                continue
            for ex in pad_exits(self.scene, pad, self.nc.width_on(L), L):
                stub = (qp(p), ex.at) if qp(p) != ex.at else (ex.at,)
                for (ix, iy) in self.lat.near(ex.at, rings=2)[:6]:
                    if (ix, iy, L) in out or (L, ix, iy) in self.banned or not self.cell_free(L, ix, iy):
                        continue
                    c = self.lat.pt(ix, iy)
                    pts = _dedup(list(stub) + ([] if qp(c) == ex.at else list(octile_path(ex.at, c))))
                    if len(pts) < 2 or not self.legal(L, pts):
                        continue
                    out[(ix, iy, L)] = (pts, run_cost(pts, self.nc, L), _dir8(pts[-2], pts[-1]))
            self._neck_ends(p, pad, L, out)
        return out

    def _neck_ends(self, p, pad, L, out) -> None:
        """Decision 1: the stubs out of a pad narrower than this link's width on `L`, each a neck at the pad's
        own width across that exit for at most `PAD_NECK_MAX_MM` past the pad's edge, then full width to a
        lattice cell. Only on a path's link (`NetCost.link`), only on a side where the pad is narrower than the
        link, shortest neck first; a cell a full-width stub already reaches keeps that stub."""
        from .constraints import PAD_NECK_MAX_MM
        from .route_scene import _SIDE_ORDER, _SIDES

        if not self.nc.link or not self.allow_neck:
            return
        w = self.nc.width_on(L)
        box = pad.box()
        cx, cy = qp(p)
        for side in _SIDE_ORDER:
            dx, dy = _SIDES[side]
            across = (box[3] - box[1]) if dx else (box[2] - box[0])
            if across >= w - 1e-9:
                continue
            wn = math.floor(across * 1e4 + 1e-6) / 1e4
            half = (box[2] - box[0]) / 2.0 if dx else (box[3] - box[1]) / 2.0
            for k in range(1, int(round(PAD_NECK_MAX_MM / NECK_STEP_MM)) + 1):
                e = round(k * NECK_STEP_MM, 4)
                end = qp((cx + dx * (half + e), cy + dy * (half + e)))
                key = frozenset(((cx, cy), end))
                self.necks[key] = wn
                if not self.legal(L, ((cx, cy), end)):
                    del self.necks[key]
                    continue
                for (ix, iy) in self.lat.near(end, rings=2)[:6]:
                    if (ix, iy, L) in out or (L, ix, iy) in self.banned or not self.cell_free(L, ix, iy):
                        continue
                    c = self.lat.pt(ix, iy)
                    tail = [] if qp(c) == end else list(octile_path(end, c))
                    pts = _dedup([(cx, cy), end, *tail])
                    if len(pts) < 2 or (len(pts) > 2 and not self.legal(L, pts[1:])):
                        continue
                    out[(ix, iy, L)] = (pts, run_cost(pts, self.nc, L), _dir8(pts[-2], pts[-1]))

    def _rebuild(self, came, st, entries, goal):
        chain = []
        cur = st
        while cur is not None:
            chain.append(cur)
            cur = came[cur]
        chain.reverse()
        head = entries[(chain[0][0], chain[0][1], chain[0][2])][0]
        tail = goal[(chain[-1][0], chain[-1][1], chain[-1][2])][0]
        runs = []
        plans = []
        cur_layer = chain[0][2]
        pts = list(head)
        for a, b in zip(chain, chain[1:]):
            p = self.lat.pt(b[0], b[1])
            if b[2] != a[2]:
                runs.append((cur_layer, pts))
                cur_layer = b[2]
                if (a[0], a[1]) != (b[0], b[1]):
                    plans.append(self._gplans[(a[0], a[1], a[2], b[2], b[3])])
                    pts = [p]
                else:
                    plans.append(None)
                    pts = [self.lat.pt(a[0], a[1])]
            elif not pts or pts[-1] != p:
                pts.append(p)
        for p in reversed(tail):
            if not pts or pts[-1] != p:
                pts.append(p)
        runs.append((cur_layer, pts))
        if any(pl is not None for pl in plans):
            # A group's two runs are kept even when one is a single point: the group's landing is the copper.
            self._last_plans = plans
            return [(L, _dedup(ps)) for L, ps in runs]
        self._last_plans = []
        return [(L, _dedup(ps)) for L, ps in runs if len(_dedup(ps)) >= 2]

    # -- the pull ----------------------------------------------------------------------------------

    def pull(self, L, pts, *, rounds=60):
        """String-pull against `NetCost`: replace the inside of the run by the octile path between two of
        its vertices when that is cheaper and legal; never spend the min-leg rule for cost."""
        pts = _straight(_dedup(pts))
        best = run_cost(pts, self.nc, L)
        nshort = short_legs(pts)
        for _ in range(rounds):
            moved = False
            n = len(pts)
            for i in range(0, n - 2):
                for j in range(n - 1, i + 1, -1):
                    for diag in (True, False):
                        if qp(pts[i]) == qp(pts[j]):
                            continue
                        mid = octile_path(qp(pts[i]), qp(pts[j]), diagonal_first=diag)
                        cand = _straight(_dedup(list(pts[:i]) + list(mid) + list(pts[j + 1 :])))
                        if cand == pts or short_legs(cand) > nshort:
                            continue
                        cc = run_cost(cand, self.nc, L)
                        if cc < best - 1e-9 and self.legal(L, cand):
                            pts, best, nshort, moved = cand, cc, short_legs(cand), True
                            break
                    if moved:
                        break
                if moved:
                    break
            if not moved:
                break
        return self.tidy(L, pts)

    def tidy(self, L, pts):
        """Spend cost, if it must, to satisfy the min-leg rule: a bound outranks the objective."""
        for _ in range(12):
            n = short_legs(pts)
            if n == 0 or len(pts) < 3:
                return pts
            moved = False
            for i in range(1, len(pts) - 1):
                cand = _straight(_dedup(list(pts[:i]) + list(pts[i + 1 :])))
                if len(cand) < 2 or short_legs(cand) >= n:
                    continue
                if self.legal(L, cand):
                    pts, moved = cand, True
                    break
            if not moved:
                return pts
        return pts

    # -- one link ----------------------------------------------------------------------------------

    def link(self, a, b, *, la=None, lb=None, pa=None, pb=None):
        """Route a -> b: the two direct octile paths first, else the lattice, then the pull. `pa`/`pb`
        are the terminals' pad items when they are pads (their exits are stubs too)."""
        best = None
        self.groups = []
        self.neck_facts: list[str] = []
        both = tuple(L for L in self.layers if (la is None or L in la) and (lb is None or L in lb))
        if qp(a) != qp(b):
            for L in both:
                for diag in (True, False):
                    pts = octile_path(qp(a), qp(b), diagonal_first=diag)
                    if short_legs(pts) == 0 and self.legal(L, pts):
                        c = run_cost(pts, self.nc, L)
                        if best is None or c < best[0]:
                            best = (c, [(L, pts)])
        if best is not None:
            return best[1]
        got = self._search(a, b, la=la, lb=lb, pa=pa, pb=pb)
        if got is None and self.nc.link and (pa is not None or pb is not None) and self._narrow_pad(pa, pb):
            # Decision 1: only when no full-width route exists does the link leave (or reach) a pad narrower
            # than its width through a pad neck. A fresh search: the first one's banned cells were judged at full width.
            self.allow_neck = True
            self.banned = set()
            got = self._search(a, b, la=la, lb=lb, pa=pa, pb=pb)
            if got is None:
                self.neck_facts = [f for x, pad in ((a, pa), (b, pb)) if pad is not None for f in self._neck_short(x, pad)]
        return got

    NECK_PROBE_MM = 5.0
    """How far past a pad's edge `_neck_short` looks for the first room full-width copper has: a measurement for
    the error only; the neck itself stays at most `PAD_NECK_MAX_MM`."""

    def _neck_short(self, p, pad) -> list[str]:
        """Postfix B13 (refutation neck r2): when the neck search found no route, and on every exit of `pad` that is
        narrower than the link the room for full-width copper (plus clearance) first opens past `PAD_NECK_MAX_MM`, the
        neck limit is what stops the link: one fact per such pad, with the distance at which each exit first has room
        (QFN-16 pin 10 at 3 A: 0.25 mm across, room first 1.3 mm past its edge, against the 1 mm limit). Empty when
        some exit has room within the limit (the neck was not what stopped it)."""
        from .constraints import PAD_NECK_MAX_MM
        from .route_scene import _SIDE_ORDER, _SIDES

        out: list[str] = []
        cx, cy = qp(p)
        box = pad.box()
        for L in self.layers:
            if L not in pad.layers:
                continue
            w = self.nc.width_on(L)
            firsts: list[tuple[str, float, float | None]] = []
            for side in _SIDE_ORDER:
                dx, dy = _SIDES[side]
                across = (box[3] - box[1]) if dx else (box[2] - box[0])
                if across >= w - 1e-9:
                    firsts = []
                    break  # a full-width exit: not a neck question on this layer
                wn = math.floor(across * 1e4 + 1e-6) / 1e4
                half = (box[2] - box[0]) / 2.0 if dx else (box[3] - box[1]) / 2.0
                first = None
                for k in range(1, int(round(self.NECK_PROBE_MM / NECK_STEP_MM)) + 1):
                    e = round(k * NECK_STEP_MM, 4)
                    end = qp((cx + dx * (half + e), cy + dy * (half + e)))
                    key = frozenset(((cx, cy), end))
                    had = key in self.necks
                    old = self.necks.get(key)
                    self.necks[key] = wn
                    try:
                        ok = self.legal(L, ((cx, cy), end)) and any(
                            self.cell_free(L, ix, iy) and self.legal(L, _dedup([end, *([] if qp(self.lat.pt(ix, iy)) == end else list(octile_path(end, self.lat.pt(ix, iy))))]) or [end])
                            for (ix, iy) in self.lat.near(end, rings=2)[:6]
                        )
                    finally:
                        if had:
                            self.necks[key] = old
                        else:
                            del self.necks[key]
                    if ok:
                        first = e
                        break
                if first is not None and first <= PAD_NECK_MAX_MM + 1e-9:
                    firsts = []
                    break  # this exit has room within the limit
                firsts.append((side, wn, first))
            if firsts:
                each = "; ".join(
                    f"{side} {wn:g} mm across, room {'first ' + format(first, 'g') + ' mm past its edge' if first is not None else 'nowhere within ' + format(self.NECK_PROBE_MM, 'g') + ' mm'}"
                    for side, wn, first in firsts
                )
                out.append(
                    f"{pad.owner} is narrower than the link's {w:g} mm on {L} on every exit, and a pad neck may run at most "
                    f"{PAD_NECK_MAX_MM:g} mm past the pad's edge before full width (direction.md §5b); full width plus "
                    f"{self.nc.rule.clearance if self.nc.rule is not None else 0:g} mm clearance has room: {each}"
                )
        return out

    def _narrow_pad(self, pa, pb) -> bool:
        """Is either terminal a pad narrower than this link's width on some layer, on some axis?"""
        for pad in (pa, pb):
            if pad is None:
                continue
            box = pad.box()
            for L in self.layers:
                if L in pad.layers and min(box[2] - box[0], box[3] - box[1]) < self.nc.width_on(L) - 1e-9:
                    return True
        return False

    def _search(self, a, b, *, la=None, lb=None, pa=None, pb=None):
        for _attempt in range(6):
            runs = self.astar(a, b, la=la, lb=lb, pa=pa, pb=pb)
            if runs is None:
                return None
            out = []
            ok = True
            plans = list(getattr(self, "_last_plans", []) or [])
            for L, pts in runs:
                if len(pts) < 2:
                    out.append((L, tuple(pts)))
                    continue
                pulled = self.pull(L, pts)
                if not self.legal(L, pulled):
                    ok = False
                    for (x, y) in pulled:
                        ix, iy = self.lat.cell((x, y))
                        self.banned.add((L, ix, iy))
                    break
                out.append((L, tuple(pulled)))
            if ok:
                self.groups = plans
                return out
        return None
