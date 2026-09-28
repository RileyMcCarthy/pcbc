"""A differential pair is ONE object: one centreline, two offsets at the pair's pitch.

`docs/direction.md` §5: every spike that routed a pair half alone beat the length and destroyed the
coupling (99.5 % -> 59.7 %); embedded as one object the same path held 99.4 %. So a pair is routed as
its centreline, at the envelope width that holds both halves, and the two halves are its offsets at
`pitch = width + gap` — both numbers compiled (`PairSpec.width_mm`, `PairSpec.gap_mm`; the gap is
raised to the clearance the table demands between the two nets, since KiCad checks both). Offsetting an
octilinear polyline keeps every direction, so the halves are parallel everywhere and coupled by
construction; the only length difference between them is the bend term `pitch * tan(theta/2)` per
corner, which is exactly what the cost prices.

**What is routed as a pair.** The stations of the two nets' declared `Chain()`s, pairwise: link k of a
pair joins (DP station k, DN station k) to (DP station k+1, DN station k+1). A pair net's other pads
(a USB-C connector's flip side) and a pair with no declared chain are linked singly by the router
afterwards; the coupling of what was emitted is measured on the emitted board (`route_verify.
pair_coupling`) and gated there. Single layer, no vias: a via in a pair is two vias and a reference
change, and nothing prices that yet (`route_cost.REFUSED["reference / z_se / z_diff"]`).

`route_scene.blocked` judges each half against the board, and the two halves are judged against each
other before either is kept.
"""

from __future__ import annotations

import math
from dataclasses import replace

from .route_emit import seg_piece
from .route_geom import EPS_MM, octile_path, qp


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    return (dx / L, dy / L)


def offset(pts, d: float):
    """An octilinear polyline offset by signed distance d (left normal = (uy, -ux)): every leg moved by
    d along its normal and the corners re-intersected, so each leg keeps its direction."""
    n = len(pts)
    if n < 2:
        return tuple(pts)
    lines = []
    for a, b in zip(pts, pts[1:]):
        ux, uy = _unit(a, b)
        nx, ny = uy, -ux
        lines.append(((a[0] + d * nx, a[1] + d * ny), (ux, uy)))
    out = [qp(lines[0][0])]
    for i in range(len(lines) - 1):
        (p1, u1), (p2, u2) = lines[i], lines[i + 1]
        den = u1[0] * (-u2[1]) - u1[1] * (-u2[0])
        if abs(den) < 1e-12:
            continue
        rx, ry = p2[0] - p1[0], p2[1] - p1[1]
        t = (rx * (-u2[1]) - ry * (-u2[0])) / den
        out.append(qp((p1[0] + t * u1[0], p1[1] + t * u1[1])))
    last = lines[-1]
    e = pts[-1]
    out.append(qp((e[0] + d * last[1][1], e[1] - d * last[1][0])))
    ded = []
    for p in out:
        if not ded or ded[-1] != p:
            ded.append(p)
    return tuple(ded)


def _collinear(a, b, c) -> bool:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) < 1e-6


def _join(h, a, b):
    """The half run with its ends put on its two pads: slid along its first/last leg when the pad is
    on that leg's line (no extra corner), else an octile stub."""
    out = list(h)
    if qp(out[0]) != qp(a):
        if len(out) > 1 and _collinear(a, out[0], out[1]):
            out[0] = qp(a)
        else:
            out = list(octile_path(qp(a), qp(out[0])))[:-1] + out
    if qp(out[-1]) != qp(b):
        if len(out) > 1 and _collinear(b, out[-1], out[-2]):
            out[-1] = qp(b)
        else:
            out = out + list(octile_path(qp(out[-1]), qp(b)))[1:]
    ded = []
    for p in out:
        p = qp(p)
        if not ded or ded[-1] != p:
            ded.append(p)
    return tuple(ded)


def _against(scene, pieces, net, others) -> bool:
    """`pieces` of `net` clear the not-yet-added `others` (the partner half) by the table's rules."""
    from .route_scene import _pair_clashes

    items = [scene.item_of(p) for p in others]
    for p in pieces:
        it = scene.item_of(p)
        for o in items:
            if _pair_clashes(scene.table, net, it.copper, it.hole, it.layers, 0.0, o):
                return False
    return True


def pair_pitch(scene, ncp, ncn) -> float:
    """Centre-to-centre spacing of the two halves: the pair width plus the larger of the compiled pair
    gap and the clearance the table demands between the two nets, plus `EPS_MM` (`clears` demands the
    epsilon on top of every requirement)."""
    width = ncp.pair_width_mm or ncp.width_mm
    gap = max(ncp.pair_gap_mm or 0.0, scene.table.between(ncp.net, ncn.net)[0])
    # Four epsilons, not one: the halves' corners are re-intersected and put on the 1 nm grid, which
    # can take up to a nanometre off each side of the gap, and `clears` wants the full epsilon on top.
    return round(width + gap + 4 * EPS_MM, 6)


def _centre_router(scene, env, layers, ignore, reach_mm: float, pair=None):
    """A `Router` for the pair's centreline whose ends are **virtual**: the free lattice cells within
    `reach_mm` of the midpoint of the two pads, with no stub of their own. The midpoint of two pads is
    often not a place the envelope fits (an ESD array's GND pin sits between its DP and DN pins); the
    halves reach their pads by their own joins, and those are judged.

    With `pair` = (P, N, width, pitch, ends), `legal` judges the **two halves** a centreline would
    make (their offsets), not the envelope: a corner's outer offset reaches past a round envelope by
    up to (sqrt 2 - 1) * pitch / 2, so the envelope passes copper the halves then hit. A clash within
    `reach_mm` of either station is allowed here: that is where the halves are trimmed and joined."""
    from .route_cost import Router
    from .route_scene import clashes

    class _R(Router):
        def legal(self, L, pts, ignore=frozenset()):
            if pair is None:
                return super().legal(L, pts, ignore)
            if not super().legal(L, pts, ignore):
                return False
            p_net, n_net, width, pitch, ends = pair
            pts = tuple(pts)
            if len(pts) < 2:
                return True
            for net, d in ((p_net, pitch / 2.0), (n_net, -pitch / 2.0)):
                h = offset(pts, d)
                ps = [seg_piece(net, "route", L, a, b, width) for a, b in zip(h, h[1:]) if qp(a) != qp(b)]
                for c in clashes(self.scene, ps, net):
                    if c.rule == "mask":
                        continue
                    if min(math.dist(c.at, e) for e in ends) > reach_mm + 1.0:
                        return False
            return True

        def _ends(self, p, only=None, pad=None):
            out = {}
            rings = max(1, int(round(reach_mm / self.step)))
            for L in (self.layers if only is None else tuple(x for x in self.layers if x in only)):
                got = 0
                for (ix, iy) in self.lat.near(p, rings=rings):
                    if got >= 12:
                        break
                    if (L, ix, iy) in self.banned or not self.cell_free(L, ix, iy):
                        continue
                    c = self.lat.pt(ix, iy)
                    out[(ix, iy, L)] = ((c,), math.dist(p, c), -1)
                    got += 1
            return out

    return _R(scene, env, layers, ignore=ignore)


WHY: list[str] = []
"""Why the last pair links fell back to single routing, one line each (read by `route_native` for the
report; cleared per link)."""

PAIR_REACH_MM = 1.5
"""How far from the midpoint of a station's two pads the pair's centreline may start: past an ESD
array's middle pin (SOT-23-6 pins are 0.95 mm apart), not so far that the halves' own stubs become
the route."""


def _cut(pts, s: float, e: float):
    """The polyline between arc length `s` from its start and `e` from its end (both >= 0)."""
    total = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    lo, hi = s, total - e
    if hi - lo <= 1e-6:
        return ()
    out = []
    acc = 0.0
    for a, b in zip(pts, pts[1:]):
        L = math.dist(a, b)
        if L <= 0:
            continue
        t0, t1 = (lo - acc) / L, (hi - acc) / L
        if t1 < 0 or t0 > 1:
            acc += L
            continue
        u0, u1 = max(t0, 0.0), min(t1, 1.0)
        pa = qp((a[0] + (b[0] - a[0]) * u0, a[1] + (b[1] - a[1]) * u0))
        pb = qp((a[0] + (b[0] - a[0]) * u1, a[1] + (b[1] - a[1]) * u1))
        if not out:
            out.append(pa)
        if pb != out[-1]:
            out.append(pb)
        acc += L
    return tuple(out)


TRIM_STEP_MM = 0.1


def _trim(scene, pts, net, L, width, reach: float):
    """The half, shortened at each end by the least whole multiple of `TRIM_STEP_MM` (up to `reach`)
    that leaves it clear of the board: the offsets are exact in the middle and can run into a pad at a
    station, where the joins take over. Returns the pieces, or [] when the middle itself is blocked."""
    from .route_scene import blocked

    def pieces(path):
        return [seg_piece(net, "route", L, a, b, width, owner=net) for a, b in zip(path, path[1:]) if qp(a) != qp(b)]

    total = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    steps = int(reach / TRIM_STEP_MM) + 1
    half = total / 2.0

    def clear(s, e):
        path = _cut(pts, s, e)
        return path if len(path) >= 2 and blocked(scene, pieces(path), net) is None else None

    s = next((k * TRIM_STEP_MM for k in range(steps) if clear(k * TRIM_STEP_MM, half) is not None), None)
    e = next((k * TRIM_STEP_MM for k in range(steps) if clear(half, k * TRIM_STEP_MM) is not None), None)
    if s is None or e is None or s + e >= total:
        return []
    path = clear(s, e)
    return pieces(path) if path else []


def route_pair_link(scene, ncp, ncn, tp_a, tn_a, tp_b, tn_b, layers):
    """One pair link: (P pad a, N pad a) -> (P pad b, N pad b). The centreline is routed at the envelope
    width between virtual ends near the two stations; its two offsets are the halves, trimmed where
    they graze a pad; each half is joined to its two pads by the single-net router, on a **fork** of the
    scene that holds the partner half, so the four joins are judged against everything. Returns
    (P pieces, N pieces) or None; the real scene is not touched."""
    from .route_cost import Router, _dedup
    from .route_scene import blocked
    from .route_native import _runs_to_pieces

    width = ncp.pair_width_mm or ncp.width_mm
    pitch = pair_pitch(scene, ncp, ncn)
    common = tuple(L for L in layers if L in tp_a.layers and L in tn_a.layers and L in tp_b.layers and L in tn_b.layers)
    if not common:
        return None
    own = frozenset(it.id for it in scene.items if it.net in (ncp.net, ncn.net))
    env = replace(ncp, width_mm=width + pitch, clearance_mm=max(ncp.clearance_mm, ncn.clearance_mm), via_allowed=False, via_count_max=0)
    pa, na, pb, nb = (t.item.at() for t in (tp_a, tn_a, tp_b, tn_b))
    ca = qp(((pa[0] + na[0]) / 2.0, (pa[1] + na[1]) / 2.0))
    cb = qp(((pb[0] + nb[0]) / 2.0, (pb[1] + nb[1]) / 2.0))
    WHY.clear()
    if ca == cb:
        return None
    for L in common:
        r = _centre_router(scene, env, (L,), own, PAIR_REACH_MM, pair=(ncp.net, ncn.net, width, pitch, (ca, cb)))
        runs = r.astar(ca, cb, la=(L,), lb=(L,))
        if not runs or len(runs) != 1:
            WHY.append(f"{L}: no centreline at the pair envelope {env.width_mm:g} mm")
            continue
        centre = _dedup(r.pull(L, runs[0][1]))
        if len(centre) < 2:
            continue
        h1, h2 = offset(centre, +pitch / 2.0), offset(centre, -pitch / 2.0)
        if math.dist(h1[0], pa) > math.dist(h2[0], pa):
            h1, h2 = h2, h1
        if math.dist(h1[-1], pb) > math.dist(h2[-1], pb):
            WHY.append(f"{L}: the halves would cross to land (a twist needs a via swap)")
            continue  # the pair would have to cross itself to land: a twist needs a via swap
        fork = scene.fork()
        halves = []
        ok = True
        for net, h in ((ncp.net, h1), (ncn.net, h2)):
            mid = _trim(fork, h, net, L, width, PAIR_REACH_MM + 1.0)
            if not mid:
                WHY.append(f"{L}: the {net} half is blocked along its middle")
                ok = False
                break
            fork.add(fork.item_of(x) for x in mid)
            halves.append(mid)
        if not ok:
            continue
        joined: dict[str, list] = {ncp.net: list(halves[0]), ncn.net: list(halves[1])}
        for net, nc, mid, ta, tb in ((ncp.net, ncp, halves[0], tp_a, tp_b), (ncn.net, ncn, halves[1], tn_a, tn_b)):
            single = replace(nc, width_mm=width, via_allowed=False, via_count_max=0)
            for pad, end in ((ta, mid[0].a), (tb, mid[-1].b)):
                if qp(pad.item.at()) == qp(end):
                    continue
                rr = Router(fork, single, (L,), exempt=frozenset({pad.ref}))
                got = rr.link(pad.item.at(), end, la=(L,), lb=(L,), pa=pad.item)
                if got is None:
                    WHY.append(f"{L}: {pad.owner} cannot join its half at ({end[0]:g},{end[1]:g})")
                    ok = False
                    break
                stub = _runs_to_pieces(net, single, got)
                fork.add(fork.item_of(x) for x in stub)
                joined[net] += stub
            if not ok:
                break
        if not ok:
            continue
        return joined[ncp.net], joined[ncn.net]
    return None
