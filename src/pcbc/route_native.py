"""The native route stage: patterns and the cost-field router over one live `Scene`, writing `Piece`s.

Nothing here reads or writes a board file or a board text. The scene is built from the Python model
(`place_native.Placement.feet`: each part's `.kicad_mod` posed by its `Pose`; `route_scene.build_scene`)
plus the core copper; every piece of copper a
pattern or the router writes joins the same scene before the next one runs, so every later judge sees
it (`docs/direction.md` §5: no router is left contesting space another step took; there is one router).

The order (`docs/native-plan.md` §3.4, as measured in step 5 — see `ORDER`):

1. `hop` (pre), `fanout`, `spine` (mid, `spine=`): least freedom first, on an empty board.
2. `Pour` objects for `route_scene.plane_targets` (fields only: a pour is not an obstacle).
3. `tap` (post): every SMD pad of a plane net welded to its plane.
4. the router: every net still open, in `net_order`, links in MST order over the net's components
   (a declared `Chain()` is linked station to station first).
5. `stitch` (final): copper whose absence disconnects nothing; nothing routes after it.
6. `route_relax.relax_pieces` (`relax=`): the string-pull over `RELAXABLE` pieces.

A net the router cannot finish leaves **no copper at all** (`docs/direction.md` §5: "the nets it
cannot route are absent from `layout.gen.py`"): every piece of that net — the links that did route, the
fanout escapes, the taps and hops a pattern wrote for it — is dropped before the pieces become objects
(`drop_unfinished`); only its `layout.core.py` lines stay, being the author's. Each failed link is
reported as a move naming what the failed search hit most (each blocker named by the
`board.py`/`layout.core.py` line that put it there), with a problem file beside it
(`routed/unrouted/<net>-<k>.json`).

Locked core copper is a routing terminal wherever it is free: each end of a core track or arc that
touches no other copper of its net, and each layer of a core via that nothing of its net joins while the
via is joined on fewer than two, is where a link starts or ends (`EndTerminal`), and the pads that lock
already reaches are not (the lock says which way the copper goes; `docs/direction.md` §7). Every such
end is accounted for at the end of the stage (`core_end_states`): joined, a spur with nothing left to
join, a search that found no path, or the link dropped with its unfinished net. Nothing here
catches an exception it did not raise: a bug is a bug (`layout_job` turns it into an internal-error
refusal at the build boundary).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path

from .route_cost import NetCost, Router, net_cost, path_mm
from .route_emit import Piece, census as _census, seg_piece, via_piece
from .route_geom import qp


# --- net classes ---------------------------------------------------------------------------------


def constrained_nets(job, design) -> frozenset[str]:
    """Nets a `NetReq` keeps to one layer or forbids vias on, and differential-pair nets: the nets
    whose copper is structural (the relaxer never pulls one half of a pair or a no-via net on its own).
    One function: the net order and `route_relax` both read it, so the two cannot drift."""
    out: set[str] = set()
    for cn in job.nets:
        if cn.autoroute == "diff_pair" or cn.vias is False or len(cn.layers) == 1:
            out.update(n for n in design.nets if any(fnmatch(n, p) for p in cn.patterns))
    return frozenset(out)


ORDER = "constrained"
"""How the router orders nets. `width` is `(-width_mm, net)`, the cost-field spike's order: measured
2026-09-25 on buck it is the order that routed 21/21 in the spike, where every order that routed the
constrained `SW` first scored 13/21 in the visibility spike's sweep (docs/native-plan.md, critique #4).
`Board(net_order=)` is refused at load (it was the old router's flag)."""


def net_order(scene, nets, cs, *, how: str = ORDER, constrained: frozenset[str] = frozenset()) -> list[str]:
    """`width`: (-width_mm, net). `constrained`: the constrained nets (one layer / no vias / pairs)
    first, each group by (-width_mm, net) — the old plan's stage order, kept for measurement."""

    def width(n: str) -> float:
        return net_cost(cs, n).width_mm

    if how == "constrained":
        return sorted(nets, key=lambda n: (n not in constrained, -width(n), n))
    return sorted(nets, key=lambda n: (-width(n), n))


def route_layers(scene, nc: NetCost) -> tuple[str, ...]:
    """The layers a net may run on: its constraint's, less every **other** net's plane layer on a board
    with inner planes (a signal never cuts a plane it does not belong to). On two layers the pour shares
    its layer with signals and nothing is taken away; the plane's split is judged after routing."""
    lays = [L for L in nc.layers if L in scene.layers]
    if len(scene.layers) > 2:
        others = {layer for net, layer in scene.plane_of.items() if net != nc.net}
        planes = {z.layer for z in scene.zone_rules}
        lays = [L for L in lays if L not in others and not (L in planes and scene.plane_of.get(nc.net) != L)]
    return tuple(lays)


# --- the result ------------------------------------------------------------------------------------


@dataclass
class Failed:
    net: str
    a: str  # "J1.A7"
    b: str
    at_a: tuple[float, float]
    at_b: tuple[float, float]
    layers: tuple[str, ...]
    blockers: list[tuple[int, int]]  # (item id, hits), most hit first
    why: str = ""
    refs: tuple[str, ...] = ()  # the footprints of the two ends
    ends: tuple[str, str] = ("pad", "pad")  # what each end is: "pad", "via" (a `ViaTerminal`) or "end" (an `EndTerminal`)
    pads: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())  # the pads each end's copper already reached


@dataclass
class RouteOut:
    pieces: list[Piece] = field(default_factory=list)
    pours: list = field(default_factory=list)
    failed: list[Failed] = field(default_factory=list)
    unrouted: list[str] = field(default_factory=list)
    links: dict = field(default_factory=dict)  # net -> [done, total]
    pattern_moves: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refusals: list[dict] = field(default_factory=list)
    refused: dict = field(default_factory=dict)
    pattern_links: dict = field(default_factory=dict)
    fanout: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    scene: object = None
    order: list[str] = field(default_factory=list)


# --- pours ------------------------------------------------------------------------------------------


def plane_pours(design, job, name: str) -> list:
    """One `Pour` per `route_scene.plane_targets` entry. Each field is a deliberate rule, recorded:

    - outline: the board rectangle inset by `stackup.edge_clearance` (the old router took the same
      number from the plan's `--board-edge-clearance`);
    - clearance: the **Default** class clearance, as the plane step always poured (a class clearance
      that is larger is enforced by KiCad's DRC against the pour anyway);
    - `connect="solid"`, `min_thickness=0.1`, thermal gap and spoke 0.2: the fields every shipped
      board's plane carried before the native generator (recorded in tests/fixtures/native/), kept so the plane gates,
      the island and area numbers keep the meaning they were measured with;
    - `filled=True`: KiCad fills it at emit (`kicad_drc(refill=True)` runs before every gate).
    """
    from .model import Copper
    from .place_native import geometry_uuids
    from .route_scene import plane_targets
    from .stackup import get_stackup

    stack = get_stackup(job.stackup)
    w, h = job.board_size_mm
    e = stack.edge_clearance
    default = next((c for c in job.classes if c.name == "Default"), None)
    clear = default.clearance_mm if default is not None else stack.clearance_min
    out = []
    for net, layer in plane_targets(job):
        pts = ((round(e, 6), round(e, 6)), (round(w - e, 6), round(e, 6)), (round(w - e, 6), round(h - e, 6)), (round(e, 6), round(h - e, 6)))
        out.append(
            Copper(
                "pour", "", net, layer=layer, points=pts, connect="solid", clearance=clear, min_thickness=0.1, filled=True,
                thermal_gap=0.2, thermal_bridge_width=0.2, island_removal_mode=0, source="gen",
            )
        )
    return geometry_uuids(out, name)


def zone_rules_of(pours) -> tuple:
    from .route_scene import ZoneRule

    out = []
    for c in pours:
        if c.kind != "pour" or c.keepout is not None or not c.net:
            continue
        lays = (c.layer,) if c.layer else tuple(c.layers or ())
        if len(lays) != 1:
            continue
        out.append(ZoneRule(net=c.net, layer=lays[0], pad_clearance=float(c.clearance or 0.0), min_thickness=float(c.min_thickness or 0.0)))
    return tuple(out)


# --- the stage -------------------------------------------------------------------------------------


def _pattern_stage(ctx_args, stage, scene, owned, out: RouteOut):
    from .patterns import pattern_copper

    design, job, cs, name = ctx_args
    plan = pattern_copper(design, job, cs, name, stage=stage, scene=scene, owned=owned)
    out.pattern_moves += list(plan.moves)
    out.notes += list(plan.notes)
    out.refusals += [r.to_dict() for r in plan.refusals()]
    for pattern, n in plan.counts().items():
        out.refused[pattern] = out.refused.get(pattern, 0) + n
    for r, v in plan.links.items():
        out.pattern_links.setdefault(r, {}).update(v)
    return plan


def route_stage(design, job, placement, *, name: str, core_cu=(), spine: bool = True, relax: bool = True, patterns: bool = True, pours: bool = True, problems: Path | None = None, order: str = ORDER) -> RouteOut:
    """See the module doc. `placement` is `place_native.place`'s; `core_cu` the stamped core copper
    (it enters the scene as locked items). The four switches exist for measurement (step 5)."""
    from .fanout import fanout_pieces
    from .patterns import hard_refusals
    from .route_scene import build_scene

    t0 = time.perf_counter()
    cs = job.constraints
    out = RouteOut()
    scene = build_scene(design, job, cs, placement.feet)
    out.scene = scene
    core_items = [_core_piece(c) for c in core_cu]
    core_pieces = [p for group in core_items for p in group]
    core_tracks: list = []
    if core_pieces:
        added = scene.add([scene.item_of(p) for p in core_pieces])
        core_tracks = [(p, it) for p, it in zip(core_pieces, added) if p.kind in ("seg", "via")]
    walls = _core_keepouts(core_cu, scene)
    if walls:
        scene.add(walls)
    scene.zone_rules = tuple(scene.zone_rules) + zone_rules_of([c for c in core_cu if c.kind == "pour"])
    # Every free end the locks have before anything is generated: each one is accounted for at the end
    # of the stage (`core_end_states`), so the build never has to guess why KiCad finds one open.
    first_ends = [t for n in sorted({p.net for p, _it in core_tracks if p.net}) for t in free_core_ends(scene, n, core_tracks)]
    ctx_args = (design, job, cs, name)
    owned: list[Piece] = []

    def take(pieces) -> None:
        owned.extend(pieces)

    if patterns:
        plan = _pattern_stage(ctx_args, "pre", scene, (), out)
        take(plan.pieces)
        fan, notes = fanout_pieces(design, job, name, scene, claimed=plan.claimed)
        if fan:
            scene.add(scene.item_of(p) for p in fan)
        take(fan)
        out.fanout = notes
        if spine:
            plan = _pattern_stage(ctx_args, "mid", scene, tuple(owned), out)
            take(plan.pieces)
    poured = plane_pours(design, job, name) if pours else []
    out.pours = poured
    scene.zone_rules = tuple(scene.zone_rules) + zone_rules_of(poured)
    if patterns:
        plan = _pattern_stage(ctx_args, "post", scene, tuple(owned), out)
        take(plan.pieces)
        fatal = hard_refusals(plan)
        if fatal:
            raise PatternRefused("pattern refused:\n" + "\n".join(r.move for r in fatal))
    routed = route_nets(design, job, scene, name=name, out=out, how=order, core_tracks=core_tracks)
    take(routed)
    if patterns:
        plan = _pattern_stage(ctx_args, "final", scene, tuple(owned), out)
        take(plan.pieces)
    out.stats["route_ms"] = int(round((time.perf_counter() - t0) * 1000))
    if relax and owned:
        from .route_relax import relax_pieces

        got = relax_pieces(design, job, cs, placement.feet, name, owned=owned, core=core_pieces, pours=poured)
        owned = list(got.owned)
        out.notes += list(got.notes)
        out.stats["relax"] = got.stats
    out.unrouted = sorted({f.net for f in out.failed})
    owned, out.stats["unfinished_dropped"] = drop_unfinished(owned, out.unrouted)
    out.pieces, out.stats["dangling_removed"] = prune_dangling(design, job, placement.feet, _dedup_pieces(owned), core_pieces, poured)
    held = frozenset(qp(t.item.pt) for t in first_ends)
    out.pieces, out.stats["overlaps_merged"] = merge_overlaps(out.pieces, core_pieces, [it for it in scene.items if it.kind == "pad"], held)
    # What is left laid over itself is pinned per board (`tests/test_native_refuted3.py`): 0.
    out.stats["overlap_mm"] = round(sum(o[-1] for o in overlaps(out.pieces, core_pieces)), 4)  # a lock's end held (`hold`) counts here
    for r in out.stats["dangling_removed"]:
        at = f"({r['a'][0]:g},{r['a'][1]:g})" if r["kind"] == "via" else f"({r['a'][0]:g},{r['a'][1]:g})->({r['b'][0]:g},{r['b'][1]:g})"
        out.notes.append(f"prune: removed the {r['net']} {r['reason']} {r['kind']} of {r['owner'] or 'no owner'} at {at}: {r['why']} (KiCad would call it dangling; removing it opens nothing)")
    final, core_items_now = final_scene(design, job, placement.feet, core_pieces, out.pieces, scene.zone_rules)
    out.stats["core_ends"] = core_end_states(final, core_pieces, core_items_now, first_ends, out.stats.get("core_ends") or {}, out.unrouted)
    # The router's own claim, per net, on the copper it hands on: how many links are still open,
    # counted per pad block as KiCad's ratsnest counts them. A net it reports routed must have none —
    # checked here, natively — and the build holds KiCad's count to this one on every build.
    claim = open_links(final)
    out.stats["open_links"] = claim
    padded = open_links(final, pads_only=True)
    lost = sorted(n for n, k in padded.items() if k and n not in out.unrouted)
    if lost:
        raise AssertionError("the router reported " + ", ".join(lost) + " routed and its own copper leaves " + ", ".join(f"{n} {padded[n]}" for n in lost) + " link(s) between its pads open")
    out.stats["ms"] = int(round((time.perf_counter() - t0) * 1000))
    out.stats["census"] = _census(out.pieces)
    if problems is not None:
        write_problems(problems, design, job, scene, out)
    return out


class PatternRefused(ValueError):
    pass


def final_scene(design, job, feet, core, pieces, zone_rules):
    """(the scene of what the stage hands on — the parts, the core copper, the pieces, the pours' rules —,
    the core pieces' items in it, in order)."""
    from .route_scene import build_scene

    sc = build_scene(design, job, job.constraints, feet)
    sc.zone_rules = tuple(zone_rules)
    added = sc.add(sc.item_of(p) for p in [*core, *pieces])
    return sc, list(added[: len(core)])


def open_links(sc, pads_only: bool = False) -> dict[str, int]:
    """{net: links still open} over a scene's copper, for every net with copper: its components less
    one, where one `(pad ...)` is one item (a pad number drawn as several pads is several, as KiCad's
    ratsnest counts them), copper touching on a shared layer joins, and a pour of the net joins all of
    the net's copper on its layer (KiCad fills it; a pour KiCad cuts is the plane gate's finding).
    `pads_only`: count only components that hold a pad (a lock no pad reaches is not a link the net
    misses; the build names it against its line)."""
    from .route_scene import _touch

    by_net: dict[str, list] = {}
    for it in sc.items:
        if it.net and it.kind in ("pad", "track", "via") and it.copper is not None:
            by_net.setdefault(it.net, []).append(it)
    poured: dict[str, set] = {}
    for z in sc.zone_rules:
        poured.setdefault(z.net, set()).add(z.layer)
    out: dict[str, int] = {}
    for net, mine in sorted(by_net.items()):
        find, union = _uf(len(mine) + len(poured.get(net, ())))
        pour_ix = {L: len(mine) + k for k, L in enumerate(sorted(poured.get(net, ())))}
        first_block: dict[tuple, int] = {}
        for i, a in enumerate(mine):
            if a.kind == "pad":
                key = (a.owner, a.block)
                if key in first_block:
                    union(first_block[key], i)
                else:
                    first_block[key] = i
            for L in a.layers:
                if L in pour_ix:
                    union(pour_ix[L], i)
            for j in range(i + 1, len(mine)):
                if _touch(a, mine[j]):
                    union(i, j)
        roots = {find(i) for i in range(len(mine)) if not pads_only or mine[i].kind == "pad"}
        out[net] = max(0, len(roots) - 1)
    return out


def core_end_states(sc, core, core_items, first, routed: dict, unrouted) -> list[dict]:
    """What became of every free end the locks had before the stage generated anything, asked of the
    copper the stage hands on (core + its pieces, the pours as welds). One record per end:

    - `joined`: copper of its net touches it now (a via: on two layers or more);
    - `dropped`: the router joined it, and its net is unfinished, so its generated copper — the link to
      this end with it — is not on the board (`drop_unfinished`, `docs/direction.md` §5); the net's
      moves finish it;
    - `unfinished`: its net is unfinished and this end is one of the links still missing (a move names it);
    - `nothing`: a spur — every other terminal of its net was already the lock's own copper;
    - `failed`: the router searched and found no path (`why` is its note);
    - `lost`: the stage joined it and a later step opened it — a pcbc bug, never a move.

    The build reads these to word KiCad's `track_dangling` / `via_dangling` on a core line: it says
    "pcbc bug" only for `joined` (KiCad disagrees with this record) and `lost`."""
    if not first:
        return []
    ids = {}
    for p, it in zip(core, core_items):
        ids[(p.owner, p.kind, qp(p.a))] = it.id
        if p.kind == "seg":
            ids[(p.owner, p.kind, qp(p.b))] = it.id
    gone = frozenset(unrouted)
    out = []
    for t in first:
        rec = routed.get(end_key(t), {})
        iid = ids.get((t.line, "via" if t.kind == "via" else "seg", qp(t.item.pt)))
        now = iid is not None and end_joined(sc, replace(t, item=_Point(iid, t.item.pt)))
        if now:
            state = "joined"
        elif t.net in gone:
            state = "dropped" if rec.get("route") == "linked" else "unfinished"
        elif rec.get("route") in ("nothing", "failed"):
            state = rec["route"]
        else:
            state = "lost"
        out.append({"line": t.line, "net": t.net, "kind": t.kind, "at": [t.item.pt[0], t.item.pt[1]], "layers": sorted(t.layers), "owner": t.owner, "state": state, "why": rec.get("why", "")})
    return out


def drop_unfinished(pieces, nets) -> tuple[list[Piece], dict]:
    """(the pieces of every net but `nets`, {net: {kind: count} dropped}). A net the router could not
    finish ships no generated copper at all (`docs/direction.md` §5), whoever wrote it — a routed link, a
    fanout escape, a tap, a hop. Dropping a whole net cannot open or strand another net's copper: copper
    of different nets never joins."""
    gone = frozenset(nets)
    keep = [p for p in pieces if p.net not in gone]
    dropped: dict = {}
    for p in pieces:
        if p.net in gone:
            d = dropped.setdefault(p.net, {})
            d[p.kind] = d.get(p.kind, 0) + 1
    return keep, {n: dict(sorted(v.items())) for n, v in sorted(dropped.items())}


def _core_piece(c) -> list[Piece]:
    """A core copper object as the pieces the scene judges against (a pour is not an obstacle)."""
    if c.kind == "seg" and c.a and c.b and c.width and c.a != c.b:
        return [seg_piece(c.net or "", "core", c.layer, c.a, c.b, c.width, owner=c.where or c.id)]
    if c.kind == "arc" and c.a and c.mid and c.b and c.width:
        return [seg_piece(c.net or "", "core", c.layer, c.a, c.mid, c.width, owner=c.where or c.id), seg_piece(c.net or "", "core", c.layer, c.mid, c.b, c.width, owner=c.where or c.id)]
    if c.kind == "via" and c.at and c.size and c.drill:
        return [via_piece(c.net or "", "core", c.at, c.size, c.drill, owner=c.where or c.id)]
    return []


def _core_keepouts(core_cu, scene) -> list:
    """A core rule area that forbids tracks is an obstacle to the router, like a `Keepout()`: a
    `keepout` item over its outline (its convex hull: the judge measures to a hull)."""
    from .layout_prims import flat_points
    from .route_geom import Shape, hull
    from .route_scene import Item

    out = []
    for c in core_cu:
        if c.kind != "pour" or c.keepout is None or c.keepout.get("tracks") != "not_allowed" or not c.points:
            continue
        pts = tuple(qp(p) for p in flat_points(c.points))
        lays = frozenset(c.layers or ((c.layer,) if c.layer else scene.layers)) & frozenset(scene.layers)
        out.append(Item(0, "keepout", "", lays, Shape(hull(pts), 0.0), None, None, f"{c.where or c.id} (rule area{' ' + repr(c.name) if c.name else ''})", reason="core"))
    return out


def prune_dangling(design, job, feet, pieces, core=(), pours=()):
    """(the pieces without dangling copper, what was removed). KiCad's two rules, asked natively of the
    pieces before they become objects: a via joined on fewer than two layers (`via_dangling`) and a
    track with an end that touches nothing (`track_dangling`). An escape whose net the router reached
    another way, a stub the relaxer left: removed, and asked again until nothing is. Removing dangling
    copper cannot open a net (it touches the rest on one side only). The build never ships a dangling
    piece of its own copper (docs/native-plan.md, critique #1); a core line's is the author's and is
    never touched here."""
    from .route_geom import circle_shape, gap
    from .route_scene import build_scene

    keep = list(pieces)
    removed: list[dict] = []
    planes = {}
    for c in pours:
        if c.kind == "pour" and c.keepout is None and c.net and c.layer:
            planes.setdefault(c.net, set()).add(c.layer)
    for _pass in range(12):
        sc = build_scene(design, job, job.constraints, feet)
        added = sc.add(sc.item_of(p) for p in [*core, *keep])
        mine = added[len(core):]
        by_net: dict[str, list] = {}
        for it in sc.items:
            if it.net and it.kind in ("pad", "track", "via"):
                by_net.setdefault(it.net, []).append(it)
        drop: dict[int, str] = {}
        for k, (p, it) in enumerate(zip(keep, mine)):
            if p.reason in ("guard",):
                continue  # a shield joins nothing by design; `_guard_gate` judges it
            others = [o for o in by_net.get(p.net, ()) if o.id != it.id and o.copper is not None]
            if p.kind == "via":
                layers = set()
                for L in sc.layers:
                    if L in planes.get(p.net, ()):
                        layers.add(L)
                        continue
                    if any(L in o.layers and gap(it.copper, o.copper) <= 0.0 for o in others):
                        layers.add(L)
                if len(layers) < 2:
                    drop[k] = f"joined on {', '.join(sorted(layers)) or 'no layer'} only"
            else:
                for end in (p.a, p.b):
                    dot = circle_shape(end[0], end[1], p.w)
                    if not any(p.layer in o.layers and gap(dot, o.copper) <= 0.0 for o in others):
                        if p.layer in planes.get(p.net, ()):
                            continue  # a track end inside its own net's pour is joined by the pour
                        drop[k] = f"its end at ({end[0]:g},{end[1]:g}) touches nothing" + (" once the pass before removed what it met" if _pass else "")
                        break
        if not drop:
            break
        for k in sorted(drop):
            p = keep[k]
            removed.append({"kind": p.kind, "net": p.net, "reason": p.reason, "owner": p.owner, "a": list(p.a), "b": list(p.b) if p.b else None, "why": drop[k], "pass": _pass})
        keep = [p for k, p in enumerate(keep) if k not in drop]
    return keep, removed


def _span(a, b, p, q) -> tuple[float, float] | None:
    """Where segment p-q lies along a-b, as (lo, hi) in mm from a, when it is collinear with it (within
    1 um); None when it is not."""
    L = math.dist(a, b)
    if L <= 0.0:
        return None
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    off = lambda pt: abs((pt[0] - a[0]) * uy - (pt[1] - a[1]) * ux)
    if off(p) > 1e-3 or off(q) > 1e-3:
        return None
    t = lambda pt: (pt[0] - a[0]) * ux + (pt[1] - a[1]) * uy
    return (min(t(p), t(q)), max(t(p), t(q)))


def overlaps(pieces, core=()) -> list[tuple]:
    """Every pair of same-net, same-layer segments that lie on one line and overlap by more than 0.01 mm
    (the third refutation round's `tools/overlap.py` rule): (net, layer, a, b, mm)."""
    segs = [p for p in [*core, *pieces] if p.kind == "seg" and p.b is not None and p.a != p.b]
    out = []
    for i, x in enumerate(segs):
        for y in segs[i + 1 :]:
            if y.net != x.net or y.layer != x.layer:
                continue
            sp = _span(x.a, x.b, y.a, y.b)
            if sp is None:
                continue
            lo, hi = max(0.0, sp[0]), min(math.dist(x.a, x.b), sp[1])
            if hi - lo > 0.01:
                out.append((x.net, x.layer, x.a, y.a, round(hi - lo, 4)))
    return out


def _joins_at(pt, w: float, net: str, layer: str, others, pads) -> bool:
    """Does copper of `net` other than `others`' excluded pieces touch a disc of width `w` at `pt` on
    `layer` — a segment through or ending at it, a via on it, a pad under it?"""
    from .route_geom import circle_shape, gap, track_shape, via_shape

    dot = circle_shape(pt[0], pt[1], w)
    for o in others:
        if o.net != net:
            continue
        if o.kind == "via":
            if gap(dot, via_shape(qp(o.a), o.w)) <= 0.0:
                return True
        elif o.layer == layer and o.a != o.b and gap(dot, track_shape(qp(o.a), qp(o.b), o.w)) <= 0.0:
            return True
    return any(it.net == net and layer in it.layers and it.copper is not None and gap(dot, it.copper) <= 0.0 for it in pads)


def merge_overlaps(pieces, core=(), pads=(), hold=frozenset()) -> tuple[list[Piece], list[dict]]:
    """(the pieces with same-net copper laid over itself resolved, what was done). No copper that joins
    anything is lost and nothing opens: what goes is copper of the same net on the same layer, already
    there (the third refutation round: c3_usb's `USB_DP` ran 3.695 mm over itself and `USB_DN` folded
    back on itself; ds2's `3V3` route ran 1.446 mm over a locked stub; node's two `GND` taps overlapped).

    - A generated segment lying end to end inside another of its net and layer at least as wide (a core
      segment included) is dropped. When the two share an end that nothing else touches, the route
      folded back there: the container's tail back to that end is dropped with it (the container, if
      generated, now starts where the inner segment turned), or it would dangle.
    - Two generated segments of one width on one line that overlap partly: the later one is trimmed to
      start where the earlier ends (both keep their owners; the earlier one's copper covers the cut).

    Asked again until nothing changes. `pads` are the scene's pad items (a pad at a shared end joins it).
    `hold` is the free ends of the locks (points): generated copper ending there is what joins a lock's
    end, and it is never cut or dropped here, even where it lies over the lock itself (a link from a
    lock's end back along the lock is a join KiCad counts, and the lock is the author's)."""
    keep = list(pieces)
    done: list[dict] = []
    fixed = [p for p in core if p.kind == "seg" and p.b is not None and p.a != p.b]
    for _pass in range(200):
        changed = False
        segs = [(k, p) for k, p in enumerate(keep) if p.kind == "seg" and p.b is not None and p.a != p.b]
        for k, x in segs:
            if qp(x.a) in hold or qp(x.b) in hold:
                continue
            for j, y in [*((None, f) for f in fixed), *((j, q) for j, q in segs if j != k)]:
                if y.net != x.net or y.layer != x.layer or y.w + 1e-9 < x.w:
                    continue
                sp = _span(y.a, y.b, x.a, x.b)
                if sp is None or sp[0] < -1e-3 or sp[1] > math.dist(y.a, y.b) + 1e-3:
                    continue
                rec = {"net": x.net, "layer": x.layer, "dropped": [list(x.a), list(x.b)], "inside": [list(y.a), list(y.b)], "mm": round(math.dist(x.a, x.b), 4)}
                shared = next((e for e in (y.a, y.b) if e in (x.a, x.b)), None)
                if j is not None and shared is not None:
                    turn = x.b if shared == x.a else x.a
                    rest = [o for m, o in enumerate(keep) if m not in (k, j)] + list(core)
                    if turn != shared and not _joins_at(shared, x.w, x.net, x.layer, rest, pads):
                        far = y.b if shared == y.a else y.a
                        keep[j] = replace(y, a=turn, b=far) if shared == y.a else replace(y, a=far, b=turn)
                        rec["folded"] = {"at": list(shared), "container_now": [list(keep[j].a), list(keep[j].b)]}
                done.append(rec)
                keep.pop(k)
                changed = True
                break
            if changed:
                break
        if changed:
            continue
        for i, (k, x) in enumerate(segs):
            for j, y in segs[i + 1 :]:
                if y.net != x.net or y.layer != x.layer or abs(y.w - x.w) > 1e-9:
                    continue
                sp = _span(x.a, x.b, y.a, y.b)
                L = math.dist(x.a, x.b)
                if sp is None or min(L, sp[1]) - max(0.0, sp[0]) <= 0.01:
                    continue
                # y overhangs x at one end only (containment was handled above): cut y back to x's end
                # on the side it overlaps. The cut end sits on x, so whatever joined it is joined by x.
                t = lambda pt: (pt[0] - x.a[0]) * (x.b[0] - x.a[0]) / L + (pt[1] - x.a[1]) * (x.b[1] - x.a[1]) / L
                inside_end = y.a if 0.0 <= t(y.a) <= L else y.b
                if qp(inside_end) in hold:
                    continue
                x_end = x.b if t(y.b if inside_end == y.a else y.a) > L else x.a
                keep[j] = replace(y, a=x_end) if inside_end == y.a else replace(y, b=x_end)
                done.append({"net": x.net, "layer": x.layer, "trimmed": [list(y.a), list(y.b)], "to": [list(keep[j].a), list(keep[j].b)], "over": [list(x.a), list(x.b)]})
                changed = True
                break
            if changed:
                break
        if not changed:
            break
    return keep, done


def _dedup_pieces(pieces) -> list[Piece]:
    """Same-net copper that lies exactly on top of other same-net copper is one piece (two identical
    objects would carry one geometry-derived uuid and emit would refuse them)."""
    from .route_emit import piece_key

    seen: set = set()
    out: list[Piece] = []
    for p in pieces:
        k = (p.net, piece_key(p))
        if p.kind == "seg":
            k = (p.net, ("seg", p.layer, *sorted([k[1][2], k[1][3]]), k[1][4]))
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


# --- the router over the board --------------------------------------------------------------------


def _uf(n):
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    return find, union


def _chain_stations(design, net: str) -> list[list[str]]:
    """Every declared `Chain()` on `net`, its members (`REF.PAD` or `REF.PIN`) in station order."""
    return [list(ch.pads) for ch in (getattr(design, "chains", ()) or ()) if ch.net == net]


def _pad_owner(design, member: str, owners: set[str]) -> str | None:
    """`J1.A7` or `U1.VBUS` (a pin name) -> the pad owner string the scene uses, when one exists."""
    if member in owners:
        return member
    ref, _, pin = member.partition(".")
    inst = next((i for i in design.instances if i.ref == ref), None)
    if inst is None:
        return None
    p = inst.part.pins.get(pin)
    if p is None:
        return None
    for pad in p.pads:
        o = f"{ref}.{pad}"
        if o in owners:
            return o
    return None


@dataclass(frozen=True)
class ViaTerminal:
    """A via already on the net (a fanout escape, a tap, an earlier link's layer change): copper a link
    may start or end on, so an escape via is continued rather than left dangling beside a new route."""

    owner: str
    net: str
    at: tuple[float, float]
    layers: frozenset
    item: object

    @property
    def ref(self) -> str:
        own = str(self.item.owner)
        return own.split(".")[0] if "." in own and " " not in own.split(".")[0] else ""


def _net_groups(scene, net: str):
    """`route_scene.components` over the net's pads **and vias**: ((pad owner, pad block) -> group, via
    or track item id -> group). Two items join when their copper touches on a shared layer; every via on the net's plane
    layer joins the plane."""
    from .route_scene import _touch

    mine = [it for it in scene.items if it.net == net and it.kind in ("pad", "track", "via")]
    parent = list(range(len(mine)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for i in range(len(mine)):
        for j in range(i + 1, len(mine)):
            if _touch(mine[i], mine[j]):
                union(i, j)
        if mine[i].kind == "pad":
            # One `(pad ...)` is one copper (a custom pad's primitives); two pads that share a number
            # are two, joined only by copper, as KiCad counts them (`route_scene.Item.block`).
            for j in range(i + 1, len(mine)):
                if mine[j].kind == "pad" and mine[j].owner == mine[i].owner and mine[j].block == mine[i].block:
                    union(i, j)
    plane = scene.plane_of.get(net)
    if plane is not None:
        on = [i for i, it in enumerate(mine) if it.kind == "via" and plane in it.layers]
        for i in on[1:]:
            union(on[0], i)
    pads = {(it.owner, it.block): find(i) for i, it in enumerate(mine) if it.kind == "pad"}
    vias = {it.id: find(i) for i, it in enumerate(mine) if it.kind in ("via", "track")}
    return pads, vias


@dataclass(frozen=True)
class _Point:
    """A bare point with the one method a terminal's `item` is asked for."""

    id: int
    pt: tuple[float, float]

    def at(self):
        return qp(self.pt)


@dataclass(frozen=True)
class EndTerminal:
    """A free end of locked core copper (`layout.core.py`), a routing terminal on every layer it has
    copper on that nothing of its net touches: a link must start or end there or the lock dangles (the
    refuters' E10/E2/E3: a verbatim gen line locked after `board.py` moved a part was routed around and
    then refused as joining nothing).

    Two kinds. A **track** end (a core `Seg`, or an `Arc`'s end) that touches no other copper of its net
    on its layer. A **via** (a core `Via`) joined on fewer than two layers — KiCad's `via_dangling` asks
    for two — is a terminal on each layer no copper of its net touches (a pour of the net on a layer
    joins it there): the third refutation round's major 3, a pad escape `Seg` + `Via` on buck's `5V`
    (no pour) was left joined on F.Cu only, because the via's other layer was never a terminal."""

    owner: str  # "layout.core.py:3's free end at (36.001,11.476)" / "layout.core.py:2's via at (28.132,19.5), free on B.Cu"
    net: str
    line: str  # "layout.core.py:3"
    layers: frozenset
    item: object  # a `_Point` whose id is the track's (or the via's) item id
    ref: str = ""
    kind: str = "track"  # "track" | "via"


def _pour_layers(scene, net: str) -> frozenset:
    """The layers a pour of `net` is on (the scene's zone rules: core pours and the plane pours)."""
    return frozenset(z.layer for z in scene.zone_rules if z.net == net)


def via_joined(scene, net: str, it) -> frozenset:
    """The layers of a via item that copper of its net touches: another pad, track or via's copper on
    that layer, or a pour of the net on it (a pour welds a via inside it)."""
    from .route_geom import gap

    poured = _pour_layers(scene, net)
    out = set()
    for L in it.layers:
        if L in poured:
            out.add(L)
            continue
        if any(o.id != it.id and o.net == net and o.kind in ("pad", "track", "via") and o.copper is not None and L in o.layers and gap(it.copper, o.copper) <= 0.0 for o in scene.items):
            out.add(L)
    return frozenset(out)


def free_core_ends(scene, net: str, core_tracks) -> list[EndTerminal]:
    """Every free end of the core copper of `net` (`EndTerminal`), in the order the lines were written
    (one terminal per point): each end of a core track that touches no other copper of the net on its
    layer, and each core via joined on fewer than two layers, on the layers nothing joins it on.
    `core_tracks` is the core pieces with their scene items, tracks and vias."""
    from .route_geom import circle_shape, gap

    others = [it for it in scene.items if it.net == net and it.kind in ("pad", "track", "via") and it.copper is not None]
    out: list[EndTerminal] = []
    seen: set = set()
    for p, it in core_tracks:
        if p.net != net:
            continue
        line = str(p.owner or "layout.core.py")
        if p.kind == "via":
            joined = via_joined(scene, net, it)
            if len(joined) >= 2:
                continue
            free = tuple(L for L in scene.layers if L in it.layers and L not in joined)
            key = ("via", qp(p.a))
            if not free or key in seen:
                continue
            seen.add(key)
            out.append(EndTerminal(f"{line}'s via at ({p.a[0]:g},{p.a[1]:g}), free on {', '.join(free)}", net, line, frozenset(free), _Point(it.id, tuple(p.a)), kind="via"))
            continue
        if p.kind != "seg":
            continue
        for end in (p.a, p.b):
            key = (p.layer, qp(end))
            if key in seen:
                continue
            dot = circle_shape(end[0], end[1], p.w)
            if any(o.id != it.id and p.layer in o.layers and gap(dot, o.copper) <= 0.0 for o in others):
                continue
            seen.add(key)
            out.append(EndTerminal(f"{line}'s free end at ({end[0]:g},{end[1]:g})", net, line, frozenset({p.layer}), _Point(it.id, tuple(end))))
    return out


def end_joined(scene, t: EndTerminal) -> bool:
    """Is this free end joined now: a track end touched by other copper of its net on its layer (or
    inside the net's pour there), a via joined on two layers or more."""
    from .route_geom import circle_shape, gap

    it = next((x for x in scene.items if x.id == t.item.id), None)
    if it is None:
        return False
    if t.kind == "via":
        return len(via_joined(scene, t.net, it)) >= 2
    (L,) = tuple(t.layers)
    if L in _pour_layers(scene, t.net):
        return True
    dot = circle_shape(t.item.pt[0], t.item.pt[1], _track_width(it))
    return any(o.id != it.id and o.net == t.net and o.kind in ("pad", "track", "via") and o.copper is not None and L in o.layers and gap(dot, o.copper) <= 0.0 for o in scene.items)


def end_key(t: EndTerminal) -> str:
    """One free end's name across the stage: its line, its kind and its point (a via's free layers
    may change as copper arrives; its point does not)."""
    return f"{t.line} {t.kind} ({t.item.pt[0]:g},{t.item.pt[1]:g})"


def _track_width(it) -> float:
    """A track item's width: its shape's radius, doubled."""
    return round(2.0 * it.copper.r, 6)


def pad_terminals(scene, net: str, pad_group: dict) -> list:
    """The net's pad terminals: `patterns.terminals` (one per pad owner), except that a pad number drawn
    as several pads whose copper is not already one — the ESP32-C3-MINI's nine `49` blocks — is one
    terminal per group of its blocks that touch (`route_scene.Item.block`). KiCad joins pads by copper,
    not by number, so each block is a terminal the router must reach. The first group keeps the owner's
    name (`U1.49`, which a `Chain()` station names); the others are `U1.49@(x,y)`."""
    from .patterns import Terminal, terminals

    out: list = []
    for t in terminals(scene, net):
        items = [it for it in scene.items if it.kind == "pad" and it.net == net and it.owner == t.owner]
        groups: dict[int, list] = {}
        for it in items:
            groups.setdefault(pad_group[(it.owner, it.block)], []).append(it)
        if len(groups) <= 1:
            out.append(t)
            continue
        ordered = sorted(groups.values(), key=lambda g: min(it.block for it in g))
        for k, g in enumerate(ordered):
            x0 = min(it.box()[0] for it in g)
            y0 = min(it.box()[1] for it in g)
            x1 = max(it.box()[2] for it in g)
            y1 = max(it.box()[3] for it in g)
            at = qp(((x0 + x1) / 2.0, (y0 + y1) / 2.0))
            widest = max(g, key=lambda p: ((p.box()[2] - p.box()[0]) * (p.box()[3] - p.box()[1]), -p.id))
            layers: frozenset = frozenset()
            for it in g:
                layers = layers | it.layers
            out.append(Terminal(t.owner if k == 0 else f"{t.owner}@({at[0]:g},{at[1]:g})", net, at, layers, widest))
    return out


def _is_pad(t) -> bool:
    return not isinstance(t, (ViaTerminal, EndTerminal))


def net_links(design, scene, net: str, core_tracks=()) -> tuple[list, list[tuple[int, int]], object]:
    """(terminals, the links still to route in order, the union-find over terminals by what already
    connects them). The terminals are the net's pads, the vias already on it (`ViaTerminal`) and the free
    ends of its locked core tracks (`EndTerminal`). A component that holds a free core end is reached
    **only** at its free ends: its pads and vias are already joined by the lock, and a link to them would
    leave the lock dangling. Links: a declared chain's consecutive stations first, then Kruskal over
    terminal pairs by (distance, owners), skipping pairs already joined."""
    from .patterns import terminals

    pad_group, via_group = _net_groups(scene, net)
    terms: list = pad_terminals(scene, net, pad_group)
    idx = {t.owner: i for i, t in enumerate(terms)}
    ends = free_core_ends(scene, net, core_tracks)
    end_vias = {t.item.id for t in ends if t.kind == "via"}
    for it in sorted((it for it in scene.items if it.kind == "via" and it.net == net), key=lambda it: it.id):
        if it.id in end_vias:
            continue  # a core via with a free layer is reached on that layer only (`EndTerminal`)
        terms.append(ViaTerminal(f"the {net} via at ({it.at()[0]:g},{it.at()[1]:g})", net, it.at(), frozenset(scene.layers), it))
    terms += ends
    find, union = _uf(len(terms))
    first: dict[int, int] = {}
    group_of: dict[int, int] = {}
    for k, t in enumerate(terms):
        g = pad_group.get((t.item.owner, t.item.block)) if _is_pad(t) else via_group.get(t.item.id)
        if g is None:
            continue
        group_of[k] = g
        if g in first:
            union(first[g], k)
        else:
            first[g] = k
    # A free end of a locked *track* says which way the copper goes: its component is reached there only.
    # A core via's free layer does not (it is often welded to a pour that joins half the net): it is an
    # ordinary terminal here, and `route_nets` joins it on its free layer afterwards if the tree did not.
    locked_groups = {group_of[k] for k, t in enumerate(terms) if isinstance(t, EndTerminal) and t.kind == "track" and k in group_of}
    usable = [not (group_of.get(k) in locked_groups and not isinstance(t, EndTerminal)) for k, t in enumerate(terms)]
    links: list[tuple[int, int]] = []
    for seq in _chain_stations(design, net):
        owners = [_pad_owner(design, m, set(idx)) for m in seq]
        for x, y in zip(owners, owners[1:]):
            if x is not None and y is not None and x != y:
                links.append((idx[x], idx[y]))
    pairs = []
    for i in range(len(terms)):
        if not usable[i]:
            continue
        for j in range(i + 1, len(terms)):
            if not usable[j] or find(i) == find(j):
                continue
            pairs.append((round(math.dist(terms[i].item.at(), terms[j].item.at()), 6), terms[i].owner, terms[j].owner, i, j))
    pairs.sort()
    links += [(i, j) for _d, _a, _b, i, j in pairs]
    return terms, links, (find, union)


def _link(scene, nc, layers, ta, tb, vias_left):
    """(runs or None, the router that searched). One link between two terminals."""
    la = tuple(L for L in layers if L in ta.layers)
    lb = tuple(L for L in layers if L in tb.layers)
    r = Router(scene, nc, layers, exempt=frozenset(x for x in (ta.ref, tb.ref) if x), vias_left=vias_left)
    pa_ = ta.item if _is_pad(ta) else None
    pb_ = tb.item if _is_pad(tb) else None
    runs = r.link(ta.item.at(), tb.item.at(), la=la or None, lb=lb or None, pa=pa_, pb=pb_) if la and lb else None
    return runs, r, bool(la and lb)


def _kind(t) -> str:
    return "via" if isinstance(t, ViaTerminal) else "end" if isinstance(t, EndTerminal) else "pad"


def route_nets(design, job, scene, *, name: str, out: RouteOut, order: list[str] | None = None, how: str = ORDER, core_tracks=()) -> list[Piece]:
    """Every open net, in `net_order`, link by link. Pieces join the scene as they are made."""
    from .route_scene import net_open

    cs = job.constraints
    # `NetReq(autoroute=False)` is not "leave it alone": every preset that sets it (switch_node,
    # analog) is a *sensitive* net that the old plan routed first on its own layers
    # (`compile._sensitive_groups`); its constraint (`vias=False`, one layer) is what the router
    # honours. So every net with two pads is routed.
    nets = sorted({it.net for it in scene.items if it.kind == "pad" and it.net})
    todo = [n for n in nets if len({(it.owner, it.block) for it in scene.pads_of(n)}) >= 2 and (net_open(scene, n) or free_core_ends(scene, n, core_tracks))]
    order = order if order is not None else net_order(scene, todo, cs, how=how, constrained=constrained_nets(job, design))
    out.order = list(order)
    made: list[Piece] = route_pairs(design, job, scene, out=out)
    for net in order:
        nc = net_cost(cs, net)
        layers = route_layers(scene, nc)
        terms, links, (find, union) = net_links(design, scene, net, core_tracks)
        before = [find(i) for i in range(len(terms))]
        done = 0
        vias_used = 0
        need = len(set(before)) - 1
        used: set[int] = set()
        tried: list[tuple[int, int, Failed]] = []

        def fail(i: int, j: int, r, reachable: bool) -> Failed:
            ta, tb = terms[i], terms[j]
            why = "" if reachable else f"no layer both {ta.owner} and {tb.owner} are on is open to {net} ({', '.join(layers) or 'none'})"
            pads = tuple(tuple(sorted(t.owner for k, t in enumerate(terms) if _is_pad(t) and find(k) == find(x))) for x in (i, j))
            return Failed(net, ta.owner, tb.owner, ta.item.at(), tb.item.at(), layers, sorted(r.last_hits.items(), key=lambda kv: (-kv[1], kv[0]))[:8], why, tuple(x for x in (ta.ref, tb.ref) if x), (_kind(ta), _kind(tb)), pads)

        def lay(i: int, j: int) -> Failed | None:
            nonlocal vias_used, done
            left = None if nc.via_count_max is None else max(0, nc.via_count_max - vias_used)
            runs, r, reachable = _link(scene, nc, layers, terms[i], terms[j], left)
            if runs is None:
                return fail(i, j, r, reachable)
            pieces = _runs_to_pieces(net, nc, runs)
            vias_used += sum(1 for p in pieces if p.kind == "via")
            if pieces:
                scene.add(scene.item_of(p) for p in pieces)
            made.extend(pieces)
            union(i, j)
            used.update((i, j))
            done += 1
            return None

        for (i, j) in links:
            if find(i) == find(j):
                continue
            f = lay(i, j)
            if f is not None:
                tried.append((i, j, f))
        # A free core end the tree did not use still has to be joined, or the lock dangles: it is linked
        # to the nearest terminal that was not its own copper before routing (a pad or via of a locked
        # component is reached through that component's free ends only, as above). The net may be
        # complete without it, so a failure here is a note; KiCad then reports the end as dangling and
        # the build names the core line (`build.py`).
        locked_before = {before[x] for x, v in enumerate(terms) if isinstance(v, EndTerminal)}
        ends = out.stats.setdefault("core_ends", {})
        for k, t in enumerate(terms):
            if not isinstance(t, EndTerminal) or t.kind != "track" or k in used:
                continue
            cand = sorted(
                (round(math.dist(t.item.at(), u.item.at()), 6), u.owner, m)
                for m, u in enumerate(terms)
                if before[m] != before[k] and (isinstance(u, EndTerminal) or before[m] not in locked_before)
            )
            if not cand:
                # Nothing is left for this end to join: every other terminal of the net is already
                # this lock's own copper (a spur). Said, so the build names the line and not itself.
                why = f"every {net} pad and via it could join is already joined by the lock's own copper"
                out.notes.append(f"{net}: {t.owner} has nothing to join: {why}; the lock is left with that end open")
                ends[end_key(t)] = {"route": "nothing", "why": why}
                continue
            f = lay(k, cand[0][2])
            if f is None:
                need += 1  # a link beyond the spanning tree, laid: counted on both sides
            else:
                note = f"{net}: {t.owner} could not be joined to {f.b} ({f.why or 'no path'}); the lock is left with that end open"
                out.notes.append(note)
                ends[end_key(t)] = {"route": "failed", "why": note}
        # A core via is joined when two of its layers are (KiCad's `via_dangling`). Each one still short
        # of two is linked on a layer still free to the nearest other terminal of the net — a loop through
        # its own component when that is all there is (a via welded to the pour on B.Cu and free on F.Cu
        # is joined to the nearest F.Cu copper of its net), twice for a via that joined nothing.
        for k, t in enumerate(terms):
            if not isinstance(t, EndTerminal) or t.kind != "via" or end_key(t) in ends:
                continue
            it = next(x for x in scene.items if x.id == t.item.id)
            for _join in range(2):
                joined = via_joined(scene, net, it)
                free = frozenset(L for L in scene.layers if L in it.layers and L not in joined)
                if len(joined) >= 2 or not free:
                    break
                t2 = replace(t, layers=free)
                cand = sorted((round(math.dist(t.item.at(), u.item.at()), 6), u.owner, m) for m, u in enumerate(terms) if m != k and u.item.id != t.item.id)
                if not cand:
                    break
                left = None if nc.via_count_max is None else max(0, nc.via_count_max - vias_used)
                runs, r, reachable = _link(scene, nc, layers, t2, terms[cand[0][2]], left)
                if runs is None:
                    note = f"{net}: {t.owner} could not be joined on {', '.join(sorted(free))} to {terms[cand[0][2]].owner} ({'no path' if reachable else 'no layer open to it'}); the lock is left joined on {', '.join(sorted(joined)) or 'no layer'}"
                    out.notes.append(note)
                    ends[end_key(t)] = {"route": "failed", "why": note}
                    break
                pieces = _runs_to_pieces(net, nc, runs)
                vias_used += sum(1 for p in pieces if p.kind == "via")
                if pieces:
                    scene.add(scene.item_of(p) for p in pieces)
                made.extend(pieces)
                done += 1
                need += 1
                union(k, cand[0][2])
        for k, t in enumerate(terms):
            if isinstance(t, EndTerminal) and end_key(t) not in ends:
                ends[end_key(t)] = {"route": "linked" if end_joined(scene, t) else "open"}
        # A failed attempt whose two ends a later link joined another way is not a failure. What is
        # left is one move per link still missing: the failed attempts, shortest first, that would
        # join two components still apart (a spanning forest over them, so moves == missing links).
        f2, u2 = _uf(len(terms))
        for i in range(len(terms)):
            f2_i = find(i)
            if f2_i != i:
                u2(i, f2_i)
        # A component with no pad is locked copper alone (a lock that joins no pad yet, a lone core via).
        # The net is finished when its pads are joined; a lock the router could not reach is the lock's
        # finding, noted above ("could not be joined") and named by the build against its line — it does
        # not make the net unfinished and drop every generated piece of it (the third refutation round:
        # one lone GND via by a fiducial dropped all of buck's GND copper).
        padded = {find(k) for k, t in enumerate(terms) if _is_pad(t)}
        islands = {find(k) for k in range(len(terms))} - padded
        for i, j, f in tried:
            if f2(i) == f2(j) or find(i) in islands or find(j) in islands:
                continue
            u2(i, j)
            out.failed.append(f)
        need -= len(islands)
        for k, t in enumerate(terms):
            if isinstance(t, EndTerminal) and find(k) in islands and ends.get(end_key(t), {}).get("route") not in ("failed", "nothing"):
                note = f"{net}: {t.owner} could not be joined to any {net} pad (its lock reaches none, and every link to it failed); the lock is left open"
                out.notes.append(note)
                ends[end_key(t)] = {"route": "failed", "why": note}
        out.links[net] = [done, need]
    return made


def pair_nets(job) -> list[tuple[str, str]]:
    """Every compiled differential pair, once, as (the smaller name, the larger)."""
    out = set()
    for c in (job.constraints.constraints if job.constraints is not None else ()):
        if c.pair is not None and c.pair.partner:
            out.add(tuple(sorted((c.net, c.pair.partner))))
    return sorted(out)


def route_pairs(design, job, scene, *, out: RouteOut) -> list[Piece]:
    """Every pair whose two nets declare chains of one length, routed as one object station pair by
    station pair (`route_pair`), before any single net. A link that does not fit as a pair is left to
    the single-net router and said so in `out.notes`; the coupling of what was emitted is gated on the
    emitted board (`route_verify.pair_coupling`)."""
    from .patterns import terminals
    from .route_pair import route_pair_link

    cs = job.constraints
    made: list[Piece] = []
    for p, n in pair_nets(job):
        cp, cn = _chain_stations(design, p), _chain_stations(design, n)
        if len(cp) != 1 or len(cn) != 1 or len(cp[0]) != len(cn[0]) or len(cp[0]) < 2:
            out.notes.append(f"pair {p}/{n}: no declared Chain() of one length on both nets, so it is routed as two nets and its coupling is measured on the board")
            continue
        tp = {t.owner: t for t in terminals(scene, p)}
        tn = {t.owner: t for t in terminals(scene, n)}
        sp = [_pad_owner(design, m, set(tp)) for m in cp[0]]
        sn = [_pad_owner(design, m, set(tn)) for m in cn[0]]
        if None in sp or None in sn:
            out.notes.append(f"pair {p}/{n}: a chain station is not a pad of the net, so it is routed as two nets")
            continue
        ncp, ncn = net_cost(cs, p), net_cost(cs, n)
        layers = tuple(L for L in route_layers(scene, ncp) if L in route_layers(scene, ncn))
        done = 0
        for k in range(len(sp) - 1):
            got = route_pair_link(scene, ncp, ncn, tp[sp[k]], tn[sn[k]], tp[sp[k + 1]], tn[sn[k + 1]], layers)
            if got is None:
                from .route_pair import WHY

                out.notes.append(f"pair {p}/{n}: {sp[k]}/{sn[k]} -> {sp[k + 1]}/{sn[k + 1]} does not fit as one pair ({'; '.join(WHY) or 'no layer the four pads share'}); the router links the two nets singly there")
                continue
            pp, nn = got
            scene.add(scene.item_of(x) for x in [*pp, *nn])
            made.extend(pp)
            made.extend(nn)
            done += 1
        out.stats.setdefault("pairs", {})[f"{p}/{n}"] = [done, len(sp) - 1]
    return made


def _runs_to_pieces(net: str, nc: NetCost, runs) -> list[Piece]:
    pieces: list[Piece] = []
    prev_end = None
    for k, (L, pp) in enumerate(runs):
        if k and prev_end is not None:
            pieces.append(via_piece(net, "route", prev_end, nc.via_dia, nc.via_drill, owner=net))
        for a, b in zip(pp, pp[1:]):
            if qp(a) == qp(b):
                continue
            pieces.append(seg_piece(net, "route", L, a, b, nc.width_mm, owner=net))
        prev_end = pp[-1]
    return pieces


# --- pieces -> layout objects ----------------------------------------------------------------------

ROLE_PREFIX = "pcbc:"


def to_objects(pieces, board: str) -> tuple[list, list]:
    """Every piece as the one KiCad primitive it is (`Seg`, `Via`), every field explicit, and one
    `pcbc:<reason>:<owner>` group per role: the role of a piece lives on the board, where the gates
    read it (`layout_job.roles_doc`). A piece a pattern wrote keeps the uuid its pattern derived; the
    router's take the uuid their geometry derives (`place_native.geometry_uuids`). Pattern copper is
    written locked (as it always was); the router's is not."""
    from .model import Copper, Graphic
    from .place_native import geometry_uuids
    from .sexp import stable_uuid

    copper = []
    for p in pieces:
        locked = p.reason != "route"
        if p.kind == "seg":
            copper.append(Copper("seg", "", p.net, layer=p.layer, a=p.a, b=p.b, width=p.w, locked=locked, uuid=p.uuid or None, source="gen"))
        else:
            copper.append(Copper("via", "", p.net, layer=None, at=p.a, size=p.w, drill=p.drill, layers=tuple(p.layer), locked=locked, uuid=p.uuid or None, source="gen"))
    copper = geometry_uuids(copper, board)
    by_role: dict[tuple[str, str], list[str]] = {}
    for p, c in zip(pieces, copper):
        members = by_role.setdefault((p.reason, p.owner or p.net), [])
        if c.uuid not in members:
            members.append(c.uuid)
    groups = []
    for (reason, owner), members in sorted(by_role.items()):
        gname = f"{ROLE_PREFIX}{reason}:{owner}"
        groups.append(Graphic("group", gname, {"name": gname, "uuid": stable_uuid(board, "role", reason, owner), "locked": False, "lib_id": None, "members": tuple(sorted(members))}, source="gen"))
    return copper, groups


# --- moves and problem files ------------------------------------------------------------------------


def _source_of(design, item) -> str:
    """The line that put a scene item where it is: a part's `Place()`, a core line, or the net whose
    copper was routed earlier."""
    kind = item.kind
    if kind in ("pad", "hole"):
        # "J1.A7", or "J1 pad at 32.9,38.7" for a pad with no number (the refuters: that one was read
        # as ref "J1 pad at 32" and blamed on a missing Place()).
        ref = item.owner.split(" ")[0].split(".")[0]
        spec = next((p for p in design.places if p.ref == ref), None)
        if spec is None:
            return f"{item.owner} pad [{item.net or 'no net'}] — {ref} has no Place() in board.py"
        line = getattr(spec, "line", 0)
        return f"{item.owner} pad [{item.net or 'no net'}] — {ref} is placed by board.py:{line} Place({ref!r})" if line else f"{item.owner} pad [{item.net or 'no net'}] — {ref}'s Place() in board.py"
    if kind in ("track", "via"):
        if item.reason == "core":
            return f"{item.owner} ({kind} on {item.net or 'no net'}, a core line)"
        req = _netreq_line(design, item.net)
        return f"{item.net} {kind} ({item.reason or 'copper'}, written before this net{', its NetReq ' + req if req else ', a net with no NetReq line'})"
    if kind == "keepout":
        return item.owner if item.reason == "core" else f"{item.owner} (a Keepout or rule area of board.py)"
    return item.label()


def _netreq_line(design, net: str) -> str:
    r = _netreq_of(design, net)
    return f"board.py:{r.line}" if r is not None else ""


def _netreq_of(design, net: str):
    """The `NetReq` line that names `net` (the first, as `constraints.py` reads them), or None."""
    for r in design.netreqs:
        if any(fnmatch(net, p) for p in r.nets) and getattr(r, "line", 0):
            return r
    return None


def _via_move(design, job, scene, net: str, nc: NetCost) -> str:
    """The `NetReq` edit that would let a no-via link change layers, or "" when there is none worth
    offering. It names the line the net is already on (the refuters: "allow vias with
    NetReq('REFN_F', vias=True)" was refused by `pcbc check` as naming REFN_F twice), says to split the
    net out when that line names others, and when the net's layers leave one copper layer a via alone
    cannot help, so the edit is layers= and vias= together."""
    if nc.via_allowed:
        return ""
    r = _netreq_of(design, net)
    if r is None:
        return f"or declare NetReq({net!r}, vias=True) in board.py"
    others = [n for n in r.nets if n != net]
    where = f"its NetReq at board.py:{r.line}"
    split = f" (that line also names {', '.join(others)}: split {net} out into a NetReq of its own first)" if others else ""
    lays = route_layers(scene, nc)
    board = tuple(L for L in scene.layers)
    if len(lays) >= 2:
        return f"or let {net} change layers: add vias=True to {where}{split}"
    if len(board) < 2:
        return ""
    two = ", ".join(repr(L) for L in board[:1] + board[-1:])
    return f"or let {net} change layers: {where} keeps it on {', '.join(lays) or 'no layer'}, so give it layers=({two}) and vias=True there{split}"


def move_lines(design, job, out: RouteOut, problems_dir: str | None = None) -> list[str]:
    """One move per failed link, each ending in a `board.py` or `layout.core.py` edit."""
    scene = out.scene
    lines = []
    count: dict[str, int] = {}
    for f in out.failed:
        count[f.net] = count.get(f.net, 0) + 1
        k = count[f.net]
        blockers = []
        seen = set()
        for iid, _n in f.blockers:
            it = scene.items[iid]
            s = _source_of(design, it)
            if s not in seen:
                seen.add(s)
                blockers.append(s)
            if len(blockers) == 4:
                break
        # A core rule area over one of the link's own pads is the one blocker with a sharper move than
        # "give it room": the pad cannot be reached at all (the second review's m9).
        from .route_geom import hull_dist2

        for it in scene.items:
            if it.kind == "keepout" and it.reason == "core" and it.copper is not None:
                for who, at in ((f.a, f.at_a), (f.b, f.at_b)):
                    if hull_dist2((tuple(at),), it.copper.pts) <= 1e-12:
                        blockers.insert(0, f"{it.owner} covers {who}: clear it off that pad or delete it")
        nc = net_cost(job.constraints, f.net)
        how = f"on {', '.join(f.layers)}" + (" without vias" if not nc.via_allowed else "")
        head = f"{f.net}: {_end_text(f, 0)} cannot reach {_end_text(f, 1)} {how} at {nc.width_mm:g} mm + {nc.clearance_mm:g} mm clearance."
        if f.why:
            head += f" {f.why}."
        way = ("  In the way: " + "; ".join(blockers) + ".") if blockers else "  The search found no free cell next to one of the pads."
        refs = sorted(set(f.refs) or {x.split(".")[0] for x in (*f.pads[0], *f.pads[1])} or {f.a.split('.')[0], f.b.split('.')[0]})
        fix = (
            f"  Move: give the link room — edit the Place() of {' or '.join(refs)} (or of what is in the way) in board.py, "
            f"or lock this link's copper in layout.core.py"
        )
        via = _via_move(design, job, scene, f.net, nc)
        if via:
            fix += ", " + via
        fix += "."
        tail = f"  Problem file: {problems_dir}/{f.net}-{k}.json" if problems_dir else ""
        lines.append("\n".join(x for x in (head, way, fix, tail) if x))
    for net in sorted({f.net for f in out.failed}):
        gone = (out.stats.get("unfinished_dropped") or {}).get(net)
        if gone:
            what = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in gone.items())
            lines.append(f"{net}: unfinished, so none of its generated copper ({what}) is in layout.gen.py or on the board; the moves above name the links still missing.")
    return lines


def _end_text(f: Failed, k: int) -> str:
    """One end of a failed link as the AI can find it: a pad with its centre; a via or a core line's
    free end by what it hangs off (a via's own text already carries its point)."""
    owner, at, kind, pads = (f.a, f.at_a, f.ends[0], f.pads[0]) if k == 0 else (f.b, f.at_b, f.ends[1], f.pads[1])
    if kind == "pad":
        return f"{owner} ({at[0]:g},{at[1]:g})"
    reach = f", on the copper from {', '.join(pads)}" if pads else ""
    return f"{owner}{reach}"



def write_problems(root: Path, design, job, scene, out: RouteOut) -> None:
    """A routing-problem file per failed link (`docs/direction.md` §8): JSON, never a board."""
    root = Path(root)
    if root.exists():
        for f in sorted(root.glob("*.json")):
            f.unlink()
    if not out.failed:
        if root.exists():
            root.rmdir()
        return
    root.mkdir(parents=True, exist_ok=True)
    count: dict[str, int] = {}
    for pos, f in enumerate(out.failed):
        count[f.net] = count.get(f.net, 0) + 1
        nc = net_cost(job.constraints, f.net)
        x0, y0 = min(f.at_a[0], f.at_b[0]) - 2.0, min(f.at_a[1], f.at_b[1]) - 2.0
        x1, y1 = max(f.at_a[0], f.at_b[0]) + 2.0, max(f.at_a[1], f.at_b[1]) + 2.0
        near = []
        for it in scene.items:
            if it.kind in ("edge",):
                continue
            b = it.box()
            if b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1:
                continue
            near.append({"kind": it.kind, "net": it.net, "owner": it.owner, "layers": sorted(it.layers), "box": [round(v, 4) for v in b]})
        doc = {
            "net": f.net,
            "a": f.a,
            "b": f.b,
            "at_a": list(f.at_a),
            "at_b": list(f.at_b),
            "layers": list(f.layers),
            "order": out.order.index(f.net) if f.net in out.order else None,
            "failed_index": pos,
            "cost": {"width_mm": nc.width_mm, "clearance_mm": nc.clearance_mm, "pitch_mm": nc.pitch_mm, "via_mm": (None if math.isinf(nc.via_mm) else round(nc.via_mm, 6)), "via_allowed": nc.via_allowed},
            "blockers": [{"item": scene.items[i].label(), "kind": scene.items[i].kind, "hits": n} for i, n in f.blockers],
            "scene_near": near,
            "replay": f"python -m pcbc.route_native --replay <this file> --board <board.py>",
        }
        (root / f"{f.net}-{count[f.net]}.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")


def replay(problem: Path, board: Path) -> dict:
    """Re-run the route that wrote a problem file and say whether its link still fails. It is the build's
    own route, not a second one: place natively, load and stamp `layout.core.py` beside `board.py` when
    there is one, and route in the build's adoption passes (`layout_job.route_in_passes`), so a failure a
    core line causes replays as a failure (the refuters' E8). The whole board is routed — a link's
    outcome depends on every net routed before it — and the one link is then looked up by its two ends."""
    from .layout_emit import stamp
    from .layout_job import core_path, load_core, route_in_passes
    from .language import load_board
    from .place_native import place

    doc = json.loads(Path(problem).read_text())
    board = Path(board).resolve()
    design = load_board(board)
    pl = place(design, name=board.stem, base=board.parent / "layout" / board.stem / "routed")
    if pl.error:
        raise ValueError(f"placement refused: {pl.error}")
    core_cu: list = []
    if core_path(board).exists():
        raw, _gr = load_core(design, board, pl, name=board.stem)
        core_cu = stamp(design, raw) if raw else []
        design.copper = [c for c in design.copper if c.source != "core"] + core_cu
    got, _produced, _groups, passes = route_in_passes(design, pl.job, pl, name=board.stem, core_cu=core_cu)
    hit = [f for f in got.failed if f.net == doc["net"] and f.a == doc["a"] and f.b == doc["b"]]
    return {"net": doc["net"], "a": doc["a"], "b": doc["b"], "still_fails": bool(hit), "core_lines": len(core_cu), "unrouted": got.unrouted, "locked": passes[-1]["locked"]}


if __name__ == "__main__":  # pragma: no cover - a command-line entry point
    import argparse

    ap = argparse.ArgumentParser(prog="python -m pcbc.route_native")
    ap.add_argument("--replay", required=True)
    ap.add_argument("--board", required=True)
    a = ap.parse_args()
    print(json.dumps(replay(Path(a.replay), Path(a.board)), indent=1))
