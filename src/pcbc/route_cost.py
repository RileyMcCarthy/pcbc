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


def run_cost(pts, nc: NetCost) -> float:
    c = path_mm(pts)
    for i in range(1, len(pts) - 1):
        c += nc.bend_mm(_ang(pts[i - 1], pts[i], pts[i + 1]))
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

    def __init__(self, scene, net: str, layer: str, ignore: frozenset[int] = frozenset()):
        self.scene = scene
        self.net = net
        self.layer = layer
        self.ignore = ignore
        self.pad = scene._max_r + _MAX_NEED
        self.h2c = scene.table.hole_to_copper()
        self._need: dict[str, float] = {}

    def need(self, other: str) -> float:
        v = self._need.get(other)
        if v is None:
            v = self.scene.table.between(self.net, other)[0]
            self._need[other] = v
        return v

    def at(self, x: float, y: float) -> float:
        best = math.inf
        p = ((x, y),)
        for i in self.scene.query(self.layer, (x, y, x, y), self.pad):
            if i in self.ignore:
                continue
            it = self.scene.items[i]
            if it.kind in ("lane", "edge") or self.layer not in it.layers:
                continue
            same = bool(self.net) and it.net == self.net
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
        self.slack = {L: Slack(scene, nc.net, L, self.ignore) for L in self.layers}
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

    # -- legality --------------------------------------------------------------------------------

    def cell_free(self, L, ix, iy) -> bool:
        f = self.free[L]
        v = f.get((ix, iy))
        if v is None:
            p = self.lat.pt(ix, iy)
            v = self.slack[L].at(p[0], p[1]) >= self.nc.width_mm / 2.0
            f[(ix, iy)] = v
        return v

    def _lane_ok(self, a, b) -> bool:
        w = self.nc.width_mm
        for ref, box, span in self.lanes:
            if ref in self.exempt:
                continue
            if clip_len_in_box(a, b, box) > span + w + 2 * self.scene.grid:
                return False
        return True

    def legal(self, L, pts, ignore=frozenset()) -> bool:
        ignore = ignore | self.ignore
        self.calls += 1
        ps = [seg_piece(self.nc.net, "route", L, a, b, self.nc.width_mm) for a, b in zip(pts, pts[1:]) if qp(a) != qp(b)]
        if not ps:
            return True
        for p in ps:
            if not self._lane_ok(p.a, p.b):
                return False
        hit = blocked(self.scene, ps, self.nc.net, ignore=ignore)
        if hit is not None:
            self.last_hits[hit.item.id] = self.last_hits.get(hit.item.id, 0) + 1
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
        return True

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
                nc2 = c + stepc + nc.turn_mm(_turn_k(d0, di))
                ns = (jx, jy, L, di, nv)
                if nc2 < seen.get(ns, math.inf) - 1e-12:
                    seen[ns] = nc2
                    heapq.heappush(pq, (nc2 + self._h(goal_cells, jx, jy, L), nc2, seq, ns, st))
                    seq += 1
            if nv < self.via_cap:
                for L2 in self.layers:
                    if L2 == L or not self.cell_free(L2, ix, iy):
                        continue
                    if not self.via_legal(lat.pt(ix, iy)):
                        continue
                    nc2 = c + nc.via_mm
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
                out[(ix, iy, L)] = (pts, run_cost(pts, self.nc) if len(pts) > 1 else 0.0, d0)
                got += 1
            if pad is None or L not in pad.layers:
                continue
            for ex in pad_exits(self.scene, pad, self.nc.width_mm, L):
                stub = (qp(p), ex.at) if qp(p) != ex.at else (ex.at,)
                for (ix, iy) in self.lat.near(ex.at, rings=2)[:6]:
                    if (ix, iy, L) in out or (L, ix, iy) in self.banned or not self.cell_free(L, ix, iy):
                        continue
                    c = self.lat.pt(ix, iy)
                    pts = _dedup(list(stub) + ([] if qp(c) == ex.at else list(octile_path(ex.at, c))))
                    if len(pts) < 2 or not self.legal(L, pts):
                        continue
                    out[(ix, iy, L)] = (pts, run_cost(pts, self.nc), _dir8(pts[-2], pts[-1]))
        return out

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
        cur_layer = chain[0][2]
        pts = list(head)
        for a, b in zip(chain, chain[1:]):
            p = self.lat.pt(b[0], b[1])
            if b[2] != a[2]:
                runs.append((cur_layer, pts))
                cur_layer = b[2]
                pts = [self.lat.pt(a[0], a[1])]
            elif not pts or pts[-1] != p:
                pts.append(p)
        for p in reversed(tail):
            if not pts or pts[-1] != p:
                pts.append(p)
        runs.append((cur_layer, pts))
        return [(L, _dedup(ps)) for L, ps in runs if len(_dedup(ps)) >= 2]

    # -- the pull ----------------------------------------------------------------------------------

    def pull(self, L, pts, *, rounds=60):
        """String-pull against `NetCost`: replace the inside of the run by the octile path between two of
        its vertices when that is cheaper and legal; never spend the min-leg rule for cost."""
        pts = _straight(_dedup(pts))
        best = run_cost(pts, self.nc)
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
                        cc = run_cost(cand, self.nc)
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
        both = tuple(L for L in self.layers if (la is None or L in la) and (lb is None or L in lb))
        if qp(a) != qp(b):
            for L in both:
                for diag in (True, False):
                    pts = octile_path(qp(a), qp(b), diagonal_first=diag)
                    if short_legs(pts) == 0 and self.legal(L, pts):
                        c = run_cost(pts, self.nc)
                        if best is None or c < best[0]:
                            best = (c, [(L, pts)])
        if best is not None:
            return best[1]
        for _attempt in range(6):
            runs = self.astar(a, b, la=la, lb=lb, pa=pa, pb=pb)
            if runs is None:
                return None
            out = []
            ok = True
            for L, pts in runs:
                pulled = self.pull(L, pts)
                if not self.legal(L, pulled):
                    ok = False
                    for (x, y) in pulled:
                        ix, iy = self.lat.cell((x, y))
                        self.banned.add((L, ix, iy))
                    break
                out.append((L, tuple(pulled)))
            if ok:
                return out
        return None
