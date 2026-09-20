"""The exact-geometry self-check: pcbc's own verdict on pcbc's own copper (`docs/r2-design.md` D.1).

Runs unconditionally at the end of each pattern stage, and a non-empty result **raises** rather than
writes. It is not a nicety — it is the reason the gate can be expected to find nothing. KiCad is
still the arbiter (D.6); this is a stricter pre-filter that runs in milliseconds, without KiCad, on
every board, so a pattern that emits bad copper fails in the slice that wrote it rather than in a DRC
report three stages later.

It re-asks every question the patterns already asked, in one pass over the finished copper, because
an ordering bug in the incremental checks cannot hide from a pass that does not depend on the order.
"""

from __future__ import annotations

from typing import Sequence

from .constraints import ConstraintSet
from .route_emit import Piece
from .route_geom import MICRO_MM, Pt, aabb, clip_len_in_box, is_octilinear, legs_ok, q, seg_lengths, turn_ok
from .route_scene import Item, Scene, clashes

__all__ = ["lane_overrun", "paths_of", "verify_copper"]

BRANCH_REASONS = ("fanout", "tap")
"""A pattern via that is a **branch** uses the fab's standard via (`stack.via_diameter`); one that
carries the net's current across layers uses the class via (`Constraint.via`). B.0's rule, and the
argument is that a tap connects one pad to a plane, so it carries that pad's share — a decoupling
cap's ripple, not the rail's 1 A — while `ViaSpec.per_change` applies where the *trunk* changes
layer, which R2 never makes it do (a spine refuses instead)."""


def paths_of(pieces: Sequence[Piece]) -> tuple[tuple[Pt, ...], ...]:
    """The segments re-assembled into the runs of copper they are, so the turn rule can be asked.

    Grouped by (net, reason, layer) and walked from every endpoint that is not a plain pass-through;
    a vertex where three pieces meet is a junction and ends the chains that reach it, which is what a
    chain, a spine rib or a tap stub looks like. Sorted throughout, so the answer is a function of the
    copper and not of the order it happened to be emitted in.
    """
    out: list[tuple[Pt, ...]] = []
    groups: dict[tuple[str, str, str], list[Piece]] = {}
    for p in pieces:
        if p.kind != "seg":
            continue
        groups.setdefault((p.net, p.reason, str(p.layer)), []).append(p)
    for key in sorted(groups):
        adj: dict[Pt, list[Pt]] = {}
        for p in sorted(groups[key], key=lambda p: (p.a, p.b)):
            adj.setdefault(p.a, []).append(p.b)
            adj.setdefault(p.b, []).append(p.a)
        seen: set[tuple[Pt, Pt]] = set()
        starts = [pt for pt in sorted(adj) if len(adj[pt]) != 2] or sorted(adj)[:1]
        for start in starts:
            for first in sorted(adj[start]):
                if (start, first) in seen:
                    continue
                chain = [start, first]
                seen.add((start, first))
                seen.add((first, start))
                while len(adj[chain[-1]]) == 2:
                    nxt = [n for n in adj[chain[-1]] if n != chain[-2]]
                    if not nxt or (chain[-1], nxt[0]) in seen:
                        break
                    seen.add((chain[-1], nxt[0]))
                    seen.add((nxt[0], chain[-1]))
                    chain.append(nxt[0])
                out.append(tuple(chain))
    return tuple(out)


def _exact(v: float) -> bool:
    """Is this coordinate an exact multiple of a nanometre, and does it survive the round trip
    through the six decimals KiCad writes and re-reads? Emit -> parse -> emit is a fixed point."""
    return q(v) == v and float(f"{v:.6f}") == v


def verify_copper(scene: Scene, pieces: Sequence[Piece], cs: ConstraintSet, *, ids: Sequence[int] = ()) -> list[str]:
    """Every complaint D.1 can make about this copper, as lines. Empty means the copper is sound.

    `ids` is the scene item id of each piece, in the same order, so a piece is not checked against
    itself: the stage adds each pattern's copper to the scene before the next pattern runs, so by the
    time this is asked every piece is already an obstacle. Without it every piece clashes with its
    own twin at zero distance, and the check that is supposed to find real errors finds only itself.
    """
    out: list[str] = []
    served: dict[tuple[str, str], frozenset[str]] = {}
    for p in pieces:
        key = (p.net, p.reason)
        if key not in served:
            served[key] = served_refs(scene, pieces, p.net, p.reason)
    for n, p in enumerate(pieces):
        ignore = frozenset({ids[n]}) if n < len(ids) else frozenset()
        for c in clashes(scene, [p], p.net, ignore=ignore):
            if c.rule in ("mask",):
                continue  # A.4 rule 4 is advisory in R2: a note, never a refusal
            out.append(f"{p.reason} {p.net}: {_where(p)} breaks {c.rule} against {c.item.label()} ({c.have:g} mm of {c.need:g} mm, {c.why})")
    for path in paths_of(pieces):
        if not is_octilinear(path):
            out.append(f"not 0/45/90: {_pts(path)}")
        elif not turn_ok(path):
            out.append(f"a turn sharper than 135 degrees: {_pts(path)}")
        if not legs_ok(path):
            short = [f"{L:.4f}" for L in seg_lengths(path) if L < MICRO_MM]
            out.append(f"a leg under {MICRO_MM:g} mm ({', '.join(short)}): {_pts(path)}")
    for p in pieces:
        for v in (p.a[0], p.a[1]) + ((p.b[0], p.b[1]) if p.b else ()):
            if not _exact(v):
                out.append(f"{p.reason} {p.net}: {v!r} is not a whole number of nanometres")
        if p.kind == "via":
            out += _via_rules(scene, cs, p)
        else:
            run, who = lane_overrun(scene, p.a, p.b, p.w, served[(p.net, p.reason)] | {p.owner.split(".")[0]})
            if who:
                out.append(f"{p.reason} {p.net}: runs {run:g} mm along {who}, which is a fanout lane (A.7)")
    return out


def served_refs(scene: Scene, pieces: Sequence[Piece], net: str, reason: str) -> frozenset[str]:
    """The footprints this run of copper actually **serves**: the ones whose pads it lands on.

    A fanout lane exists so that footprint's own pads can escape, so copper that serves one of those
    pads is what the lane is for — which is the question `route_checks.check_corridors` already asks
    ("unless the net owns an escape in it"). Asking it per piece instead of per run reports the middle
    leg of a two-pad link for running along the lane of the very footprint it is running to.

    Per (net, reason), not per connected run: a net with two disjoint runs of one reason exempts both
    their footprints for both. That is looser than the question the pattern asks itself (`lane_ok`,
    which is handed one link's two terminals), so this check can only fail to catch something the
    pattern already refused — never invent one. Tightening it is a matter of threading the run
    through, and it waits for a pattern that writes disjoint runs on one net (`chain`, S6).
    """
    refs: set[str] = set()
    for p in pieces:
        if p.net != net or p.reason != reason:
            continue
        for pt in (p.a, p.b):
            if pt is None:
                continue
            layer = p.layer if isinstance(p.layer, str) else p.layer[0]
            for i in scene.query(layer, (pt[0], pt[1], pt[0], pt[1]), 0.0):
                it = scene.items[i]
                b = it.box()
                if it.kind == "pad" and it.net == net and b[0] <= pt[0] <= b[2] and b[1] <= pt[1] <= b[3]:
                    refs.add(it.owner.split(".")[0])
    return frozenset(refs)


def lane_budget(scene: Scene, lane: Item, w: float) -> float:
    """`lane_width + w + 2 * grid` (A.7). Long enough for any crossing, short enough that a run along
    the lane is one: forbidding a crossing outright would cut c3_usb's board in half along `J1`'s
    1.4515 mm lane, while running along one is what walled the DS2 Addon's AVDD, DVDD and UART pins
    in (`docs/copper-plan.md` line 191)."""
    b = lane.box()
    return min(b[2] - b[0], b[3] - b[1]) + w + 2 * scene.grid


def lane_overrun(scene: Scene, a: Pt, b: Pt, w: float, exempt: frozenset[str]) -> tuple[float, str]:
    """The worst run this segment makes **along** a fanout lane that is none of `exempt`'s, and whose
    lane it is; `(0.0, "")` when every lane it touches it merely crosses.

    `exempt` is a set of footprint refs, not one ref: a link runs between two pads and both their
    footprints own their own lanes, so asking the question one owner at a time reports a stub for
    running along the very lane its own pad sits in.
    """
    worst, who = 0.0, ""
    for it in sorted(scene.items, key=lambda i: i.id):
        if it.kind != "lane" or it.copper is None or it.owner.split(" ")[0] in exempt:
            continue
        run = clip_len_in_box(a, b, aabb(it.copper))
        if run > lane_budget(scene, it, w) and run > worst:
            worst, who = run, it.owner
    return (round(worst, 4), who)


def _via_rules(scene: Scene, cs: ConstraintSet, p: Piece) -> list[str]:
    """D.1 items 5, 6 and 8: the branch rule, the nets that may carry no via at all, and the layers."""
    out: list[str] = []
    if tuple(p.layer) != ("F.Cu", "B.Cu"):
        out.append(f"{p.reason} {p.net}: a via on {tuple(p.layer)}; no example turns blind and buried vias on (B.0)")
    c = cs.by_net(p.net)
    if c is not None and not c.via.allowed:
        out.append(f"{p.reason} {p.net}: a via on a net whose NetReq forbids vias")
    if c is not None and c.reference:
        out.append(f"{p.reason} {p.net}: a via on a controlled-impedance net (reference {c.reference}); R2 never changes layers on one")
    if p.reason in BRANCH_REASONS:
        want = (scene.stack.via_diameter, scene.stack.via_drill)
        why = "the fab's standard via, because a branch carries one pad's share"
    elif c is not None:
        want = (c.via.diameter_mm, c.via.drill_mm)
        why = f"the {c.class_name} class via, because this one carries the net across layers"
    else:
        want = (scene.stack.via_diameter, scene.stack.via_drill)
        why = "the fab's standard via"
    if (p.w, p.drill) != want:
        out.append(f"{p.reason} {p.net}: a {p.w:g}/{p.drill:g} via where B.0's branch rule says {want[0]:g}/{want[1]:g} ({why})")
    return out


def _where(p: Piece) -> str:
    if p.kind == "via":
        return f"via at ({p.a[0]:g},{p.a[1]:g})"
    return f"{p.layer} ({p.a[0]:g},{p.a[1]:g})-({p.b[0]:g},{p.b[1]:g})"


def _pts(path: Sequence[Pt]) -> str:
    return " -> ".join(f"({x:g},{y:g})" for x, y in path)
