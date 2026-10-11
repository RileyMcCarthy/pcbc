"""The derived board outline (docs/board-outline.md Part B): what a board with no `Outline()` in layout.core.py is.

The order (B.2): anchors at their frame poses; I0, the anchors' courtyards and the keepouts; relations rooted at an
anchor, limited to I0 first; each edge group (an `edge=` part and its `to=` tree) shaped alone 300 mm out and docked
against the interior; the fixed lines (sides with edge parts, then sides the keepouts reach); fiducials last. Then the
routing box R (fixed lines, free sides at the placement plus an allowance A), and after routing the fit O (B.3): free
sides at the outermost courtyard, keepout, Region and fiducial (margin 0) or pad and copper (margin m).

Every number is a pure function of the placement and the routing, rounded outward to 0.01 mm (determinism, B.3).
The errors are the B.5 facts, naming the core line; none proposes an edit (docs/direction.md §7b).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .css import Rect, rotate_local_bounds

SIDES = ("left", "top", "right", "bottom")
IDX = {"left": 0, "top": 1, "right": 2, "bottom": 3}
OUT = {"left": -1.0, "top": -1.0, "right": 1.0, "bottom": 1.0}  # the outward sign along the side's axis
FAR = 300.0  # an edge group is shaped this far out from the interior, where nothing else is near (B.2 step 5)
BIG = 1.0e4
STEP = 0.01  # docking steps outward by this much until the group clears (B.2 step 5)


class OutlineError(ValueError):
    """A B.5 fact: the derived outline cannot be made as the core file is written."""


def _union(boxes):
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _outward(v: float, side: str) -> float:
    """`v` rounded outward to 0.01 mm on `side`."""
    if OUT[side] < 0:
        return math.floor(round(v * 100.0, 6)) / 100.0
    return math.ceil(round(v * 100.0, 6)) / 100.0


def _farther(side: str, *vals: float) -> float:
    return (min if OUT[side] < 0 else max)(vals)


def where(spec) -> str:
    """`J1 (edge="bottom", layout.core.py:1)`: a part and the line that placed it."""
    how = f'edge="{spec.edge}", ' if getattr(spec, "edge", None) else f'to="{spec.to}", ' if getattr(spec, "to", None) else ""
    line = f"layout.core.py:{spec.line}" if getattr(spec, "line", 0) else "its Place()"
    return f"{spec.ref} ({how}{line})"


def pad_boxes(foot, *, numbered: bool = True):
    """The world boxes of a part's pads (copper), with the pad's number."""
    out = []
    for p in foot.pads:
        if numbered and not p.num:
            continue
        x, y = foot.pad_world(p)
        w, h = (p.h, p.w) if foot.rot % 180 == 90 else (p.w, p.h)
        out.append((p.num, (x - w / 2, y - h / 2, x + w / 2, y + h / 2)))
    return out


def rule_areas(design) -> list[tuple[object, tuple]]:
    """The core rule areas (`Pour(None, keepout=...)` in layout.core.py) as (spec, box): `spec.name` names it in the
    keepout-face facts, `spec.line` is its line, `spec.label` the fit's label, `spec.kind` "rule area"."""
    from types import SimpleNamespace

    out = []
    for c in getattr(design, "copper", ()) or ():
        if c.source != "core" or c.kind != "pour" or c.keepout is None or not c.points:
            continue
        from .layout_prims import flat_points

        pts = flat_points(c.points)
        box = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
        where = c.where or "layout.core.py"
        line = int(where.rsplit(":", 1)[1]) if ":" in where and where.rsplit(":", 1)[1].isdigit() else 0
        name = c.name or "rule area"
        out.append((SimpleNamespace(name=name, line=line, kind="rule area", label=f"{where} rule area {name!r}"), box))
    return out


def margin_of(job, *, poured: bool) -> float:
    """m (B.3 step 4): `edge_clearance` with no pour; with any pour, edge_clearance + the largest clearance of any
    class + 2 x pour_min_mm, so the ring between the outermost copper's clearance halo and the plane's pull-back is at
    least two minimum widths of pour."""
    from .stackup import get_stackup

    stack = get_stackup(job.stackup)
    if not poured:
        return round(stack.edge_clearance, 6)
    return round(stack.edge_clearance + max([c.clearance_mm for c in job.classes] or [stack.clearance_min]) + 2.0 * stack.pour_min_mm, 6)


def allowance_of(job) -> tuple[float, float]:
    """(A, the widest pitch) (B.3 step 2): A = edge_clearance + 8 x the widest track width + clearance of a class."""
    from .stackup import get_stackup

    stack = get_stackup(job.stackup)
    pitch = max([c.track_width_mm + c.clearance_mm for c in job.classes] or [stack.track_min + stack.clearance_min])
    return round(stack.edge_clearance + 8.0 * pitch, 6), round(pitch, 6)


def has_pour(job, design=None) -> bool:
    from .route_scene import plane_targets

    if plane_targets(job):
        return True
    return any(c.kind == "pour" and c.keepout is None and c.net for c in (getattr(design, "copper", None) or ()) if c.source == "core")


@dataclass
class Derived:
    """What placement decided about the outline, and what the fit made of it (`job.derived`)."""

    fixed: dict = field(default_factory=dict)  # side -> line (frame coordinate)
    fixed_by: dict = field(default_factory=dict)  # side -> {"by", "what", "line"}
    P: tuple | None = None  # the placement box: courtyards, keepouts, Regions
    fids: list = field(default_factory=list)
    A: float = 0.0
    pitch: float = 0.0
    m: float = 0.0
    e: float = 0.3
    R: tuple | None = None
    fit: tuple | None = None  # the placement-only fit (`pcbc pcb`'s outline; the silkscreen's box)
    sides: dict = field(default_factory=dict)  # side -> what set it (the fit's)
    groups: dict = field(default_factory=dict)  # edge ref -> refs of its group

    def to_dict(self) -> dict:
        return {
            "fixed": dict(self.fixed),
            "fixed_by": {k: dict(v) for k, v in self.fixed_by.items()},
            "P": list(self.P) if self.P else None,
            "fids": [list(f) for f in self.fids],
            "A": self.A,
            "pitch": self.pitch,
            "m": self.m,
            "e": self.e,
            "R": list(self.R) if self.R else None,
            "fit": list(self.fit) if self.fit else None,
            "sides": {k: dict(v) for k, v in self.sides.items()},
        }

    def get(self, key, default=None):  # `job.derived.get("fit")`, as a dict reads
        return getattr(self, key, default)


# ------------------------------------------------------------------ B.5-1 / B.5-3 before placement


def unframed_places(places) -> list[str]:
    """B.5-1 for every Place of a derived board: an anchor needs frame coordinates, an edge part an along-edge
    `left`/`top` or none."""
    from .layout import unframed_lengths

    fails = []
    for p in places:
        if p.to or p.parent:
            continue
        line = f"layout.core.py:{p.line}: " if p.line else ""
        if p.edge:
            along = ("right",) if p.edge in ("top", "bottom") else ("bottom",)
            for key in along:
                v = getattr(p, key, None)
                if v is not None:
                    fails.append(
                        f"{line}Place({p.ref!r}) {key}={v:g} measures from the outline's {key} edge; this board has no Outline(), "
                        f"and its {key} edge is derived from the placement" if isinstance(v, (int, float)) else
                        f"{line}Place({p.ref!r}) {key}={v!r} measures from the outline's {key} edge; this board has no Outline(), "
                        f"and its {key} edge is derived from the placement"
                    )
            continue
        why = unframed_lengths(p)
        if why:
            fails.append(f"{line}Place({p.ref!r}) {why}")
    return fails


# ------------------------------------------------------------------ B.2: derived placement


def place_derived(ctx) -> Derived:
    """Run B.2 on `ctx` (`pcb_place._DerivedCtx`: the feet, the specs, `settle`, `attach`) and return what it fixed.
    Raises `OutlineError` with a B.5 fact."""
    from .pcb_place import GAP, _clashes, _edge_css, _face, _rot_for_face, _EDGE_OUT, parse_refpin

    job = ctx.job
    d = Derived()
    from .stackup import get_stackup

    d.e = get_stackup(job.stackup).edge_clearance
    specs = [s for s in job.places if s.ref in ctx.feet]
    edge_specs = [s for s in specs if s.edge and not s.to]
    anchors = [s for s in specs if not s.edge and not s.to]
    relations = [s for s in specs if s.to]
    to_of = {s.ref: parse_refpin(s.to)[0] for s in relations}

    def root(r):
        seen = set()
        while r in to_of and r not in seen:
            seen.add(r)
            r = to_of[r]
        return r

    edge_refs = {s.ref for s in edge_specs}
    keepouts = list(ctx.keepouts)
    # A Region counts like a courtyard (B.1): the interior I holds it, so an edge group docks outside it, and a keepout
    # fixes a side only when it reaches as far as the Regions too; no fixed side cuts one (a Region at x=0..8 was left 6.6 mm outside a left
    # edge docked against the courtyards alone).
    region_boxes = list(getattr(ctx, "region_boxes", ()))

    # 1. anchors, each at its frame pose, in file order
    for s in anchors:
        ctx.anchor(s)
    # 2. I0
    I0 = _union([ctx.feet[s.ref].world_box() for s in anchors if ctx.feet[s.ref].at is not None] + keepouts)
    if I0 is None and edge_specs:
        s = edge_specs[0]
        line = f"layout.core.py:{s.line}: " if s.line else ""
        raise OutlineError(
            f'{line}{s.ref} (edge="{s.edge}") has no edge to stand on: this board has no Outline(), and no part placed by at=, '
            f"left/top or a Region and no keepout, so nothing places the {s.edge} edge"
        )
    # 3. relations rooted at an anchor: limited to I0 first (its `_attach` limit is I0 grown by the edge clearance it
    # insets), then searched again with no limit
    rel_anchor = [s for s in relations if root(s.ref) not in edge_refs]
    if I0 is not None:
        lim0 = Rect(I0[0] - ctx.edge_clear, I0[1] - ctx.edge_clear, I0[2] + ctx.edge_clear, I0[3] + ctx.edge_clear)
    else:
        lim0 = Rect(-BIG, -BIG, BIG, BIG)
    ctx.relations(rel_anchor, [lim0, Rect(-BIG, -BIG, BIG, BIG)])
    # 4. I
    placed_now = [f for f in ctx.placed]
    I = _union([f.world_box() for f in placed_now] + keepouts + region_boxes)
    lines: dict[str, list[tuple[float, str]]] = {}
    groups: dict[str, list[str]] = {}
    edge_line: dict[str, tuple[str, float]] = {}
    floor_of: dict[str, dict] = {}

    def move(refs, side, dist):
        dx, dy = (OUT[side] * dist, 0.0) if side in ("left", "right") else (0.0, OUT[side] * dist)
        for r in refs:
            f = ctx.feet[r]
            f.at = (round(f.at[0] + dx, 4), round(f.at[1] + dy, 4))

    # 5. edge groups, in file order: shaped alone, then docked
    for s in edge_specs:
        f = ctx.feet[s.ref]
        side = s.edge
        sg = OUT[side]
        face = _face(f)
        rot = s.rot if s.rot_set else _rot_for_face(face, _EDGE_OUT[side])
        inset = float(getattr(_edge_css(s, f, ctx.edge_clear), side))
        if side in ("top", "bottom"):
            along = float(s.left) if isinstance(s.left, (int, float)) else (I0[0] + I0[2]) / 2.0
        else:
            along = float(s.top) if isinstance(s.top, (int, float)) else (I0[1] + I0[3]) / 2.0
        given = isinstance(s.left if side in ("top", "bottom") else s.top, (int, float))
        prov = I[IDX[side]] + sg * FAR
        cb = rotate_local_bounds(*f.box, rot)
        face_pos = prov - sg * inset
        if side in ("top", "bottom"):
            x = along - cb[0] if given else along - (cb[0] + cb[2]) / 2.0
            y = face_pos - (cb[3] if side == "bottom" else cb[1])
        else:
            y = along - cb[1] if given else along - (cb[1] + cb[3]) / 2.0
            x = face_pos - (cb[2] if side == "right" else cb[0])
        ctx.settle(s, (x, y), rot)
        lim = [-BIG, -BIG, BIG, BIG]
        lim[IDX[side]] = prov
        tree = [r for r in relations if root(r.ref) == s.ref]
        before = set(ctx.resolved)
        grown = Rect(lim[0] - ctx.edge_clear, lim[1] - ctx.edge_clear, lim[2] + ctx.edge_clear, lim[3] + ctx.edge_clear)
        ctx.relations(tree, [grown])
        grp = [s.ref] + [r for r in ctx.resolved if r not in before]
        groups[s.ref] = grp
        # the floor: the farther of I's side and every placed pad outside the group plus the edge clearance
        named = [(f"{g.ref}.{n_}", b) for g in ctx.placed if g.ref not in grp for n_, b in pad_boxes(g)]
        pb = _union([b for _w, b in named])
        floor = _farther(side, I[IDX[side]], pb[IDX[side]] + sg * d.e) if pb is not None else I[IDX[side]]
        if pb is not None and (pb[IDX[side]] + sg * d.e - I[IDX[side]]) * sg > 1e-9:
            # the pad floor, not the interior's courtyards, set where the group stands (node: U4's pads, not J_LOAD)
            who_pad = next(w for w, b in named if abs(b[IDX[side]] - pb[IDX[side]]) < 1e-9)
            floor_of[s.ref] = {"pad": who_pad, "pad_at": round(pb[IDX[side]], 4), "clearance": d.e}
        move(grp, side, (floor - prov) * sg)
        line = floor
        others = [g for g in ctx.placed if g.ref not in grp]
        far = Rect(-BIG, -BIG, BIG, BIG)

        def clear() -> bool:
            for r in grp:
                g = ctx.feet[r]
                if _clashes(g.world_box(), g.world_keep(), others, keepouts, far, GAP):
                    return False
            return True

        n = 0
        while not clear():
            move(grp, side, STEP)
            line += sg * STEP
            n += 1
            if n > 100000:
                raise OutlineError(f"{where(s)}: its group finds no clear line within {n * STEP:g} mm of the interior")
        if n:
            floor_of.pop(s.ref, None)  # docking moved it past the floor: a clash, not the pad, set the line
        edge_line[s.ref] = (side, round(line, 4))
        lines.setdefault(side, []).append((round(line, 4), s.ref))
    # 6. fixed lines: each side with edge groups at the outermost of their lines; every group moved out to it
    by_ref = {s.ref: s for s in specs}
    for side, ls in lines.items():
        best = _farther(side, *[v for v, _r in ls])
        who = next(r for v, r in ls if v == best)
        # On the 0.01 mm grid, outward: the report prints two decimals, and B.6 freezes the outline from them, so a
        # line between grid steps (node's right side at U4's pad floor, 56.6935) would freeze 0.0035 mm inside it.
        d.fixed[side] = _outward(best, side)
        d.fixed_by[side] = {"by": who, "what": "edge face", "line": by_ref[who].line, **floor_of.get(who, {})}
    for ref, (side, line) in edge_line.items():
        move(groups[ref], side, (d.fixed[side] - line) * OUT[side])
    # ... then each side with no edge part that the keepouts reach at least as far as every courtyard
    if keepouts:
        cy = _union([f.world_box() for f in ctx.placed] + region_boxes)
        names = [k.name for k in ctx.keepout_specs]
        for side in SIDES:
            if side in d.fixed or cy is None:
                continue
            i = IDX[side]
            kb = _union(keepouts)
            if (kb[i] - cy[i]) * OUT[side] >= -1e-9:
                k = next(j for j, b in enumerate(keepouts) if abs(b[i] - kb[i]) < 1e-9)
                d.fixed[side] = _outward(kb[i], side)
                kind = getattr(ctx.keepout_specs[k], "kind", "keepout") if k < len(ctx.keepout_specs) else "keepout"
                d.fixed_by[side] = {"by": names[k] if k < len(names) else f"keepout {k + 1}", "what": "keepout face", "line": getattr(ctx.keepout_specs[k], "line", 0) if k < len(ctx.keepout_specs) else 0, "kind": kind}
    # B.5-4: a part of one group past another side's fixed line
    for side, v in d.fixed.items():
        i = IDX[side]
        own = set()
        if d.fixed_by[side]["what"] == "edge face":
            own = {r for ref, (sd, _l) in edge_line.items() if sd == side for r in groups[ref]}
        for f in ctx.placed:
            if f.ref in own or f.at is None or f.ref.startswith("FID"):
                continue
            b = f.world_box()
            if (b[i] - v) * OUT[side] > 1e-6:
                setter = d.fixed_by[side]
                spec = by_ref.get(f.ref)
                axis = "x" if side in ("left", "right") else "y"
                sset = by_ref.get(setter["by"])
                why = where(sset) if sset is not None else _face_who(setter)
                raise OutlineError(
                    f"{where(spec) if spec is not None else f.ref} reaches {axis}={b[i]:.2f}, past the {side} edge at {axis}={v:.2f} that {why} sets"
                )
    d.groups = groups
    # 7. fiducials, last
    if not any(r.startswith("FID") for r in ctx.feet):
        d.fids = _fiducials(ctx, d)
    return d


def _face_who(setter: dict) -> str:
    """The keepout or core rule area whose face set a side, with its line."""
    if setter.get("kind") == "rule area":
        return f'the rule area Pour(None, keepout=...) "{setter["by"]}" (layout.core.py:{setter["line"]})'
    return f'Keepout("{setter["by"]}", layout.core.py:{setter["line"]})'


def _fiducials(ctx, d: Derived) -> list[tuple[str, float, float]]:
    """B.2 step 7: today's eight candidates `FID_INSET` inside P; a free one is taken. With fewer than three, a candidate
    with a free side moves outward across it until it is clear. Every centre is then `FID_INSET` from the final edge
    (the fit grows a free side to it)."""
    from .pcb_place import FID_HALF, FID_INSET, GAP, _overlap

    boxes = [f.world_box() for f in ctx.placed if f.at is not None] + list(ctx.keepouts) + list(ctx.region_boxes)
    P = _union(boxes)
    d.P = P
    fids: list[tuple[float, float]] = []

    def clear(x, y) -> bool:
        b = (x - FID_HALF, y - FID_HALF, x + FID_HALF, y + FID_HALF)
        if any(_overlap(b, c, GAP) for c in boxes):
            return False
        return not any(_overlap(b, (fx - FID_HALF, fy - FID_HALF, fx + FID_HALF, fy + FID_HALF), GAP) for fx, fy in fids)

    x0, y0, x1, y1 = P
    i = FID_INSET
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    cands = [
        ((x1 - i, y0 + i), ("top", "right")), ((x1 - i, y1 - i), ("bottom", "right")), ((x0 + i, y1 - i), ("bottom", "left")),
        ((x0 + i, y0 + i), ("top", "left")), ((mx, y0 + i), ("top",)), ((mx, y1 - i), ("bottom",)), ((x0 + i, my), ("left",)), ((x1 - i, my), ("right",)),
    ]
    for (x, y), _s in cands:
        if len(fids) < 3 and clear(x, y):
            fids.append((x, y))
    for (x, y), sides in cands:
        if len(fids) >= 3:
            break
        if (x, y) in fids:
            continue
        for sd in sides:
            if sd in d.fixed:
                continue
            k = IDX[sd]
            sg = OUT[sd]
            if sd in ("top", "bottom"):
                yy = P[k] + sg * (FID_HALF + GAP)
                while not clear(x, yy):
                    yy += sg * STEP
                fids.append((x, yy))
            else:
                xx = P[k] + sg * (FID_HALF + GAP)
                while not clear(xx, y):
                    xx += sg * STEP
                fids.append((xx, y))
            break
    return [(f"FID{n + 1}", round(x, 4), round(y, 4)) for n, (x, y) in enumerate(fids)]


# ------------------------------------------------------------------ B.3: the routing box and the fit


def routing_box(d: Derived) -> tuple:
    """R: the fixed lines, each free side at P's extent plus the allowance A — or at the placement-only fit when that
    reaches farther (a fiducial set outside a small placement), so R holds everything placed."""
    out = []
    for side in SIDES:
        i = IDX[side]
        if side in d.fixed:
            out.append(round(d.fixed[side], 4))
        else:
            v = d.P[i] + OUT[side] * d.A
            if d.fit is not None:
                v = _farther(side, v, d.fit[i])
            out.append(_outward(v, side))
    return tuple(out)


def fit(d: Derived, feet, *, copper=(), extra=(), m: float | None = None, refs_spec=None) -> tuple[tuple, dict]:
    """(O, what set each side): fixed sides stay; each free side moves to the outermost of the courtyards, keepouts,
    Regions and fiducial centres plus FID_INSET (margin 0), and the pads and `copper` (routed and core: segs, arcs,
    vias) plus m. `extra` is more boxes at margin 0 (core rule areas). Rounded outward to 0.01 mm."""
    from .pcb_place import FID_INSET

    m = d.m if m is None else m
    cands: dict[str, list[tuple[float, dict]]] = {s: [] for s in SIDES}

    def add(box, what: dict, margin: float):
        for side in SIDES:
            i = IDX[side]
            cands[side].append((box[i] + OUT[side] * margin, {**what, "margin": margin, "at": round(box[i], 4)}))

    for f in feet:
        if f.at is None:
            continue
        if f.ref.startswith("FID"):
            continue
        add(f.world_box(), {"by": f.ref, "what": "courtyard"}, 0.0)
        for num, b in pad_boxes(f):
            add(b, {"by": f"{f.ref}.{num}", "what": "pad"}, m)
    for name, b in extra:
        # `extra` is labelled by its caller ("Keepout KO", "Region PWR", a core rule area's line): a Region is no keepout
        add(b, {"by": name, "what": "region" if str(name).startswith("Region ") else "keepout"}, 0.0)
    for ref, x, y in d.fids:
        add((x - FID_INSET, y - FID_INSET, x + FID_INSET, y + FID_INSET), {"by": ref, "what": "fiducial"}, 0.0)
    for what, b in copper:
        add(b, {"by": what, "what": "copper"}, m)
    rect = []
    sides = {}
    for side in SIDES:
        if side in d.fixed:
            rect.append(round(d.fixed[side], 4))
            sides[side] = {**d.fixed_by[side], "at": round(d.fixed[side], 4), "edge": round(d.fixed[side], 4)}
            continue
        v, what = (min if OUT[side] < 0 else max)(cands[side], key=lambda c: c[0])
        rect.append(_outward(v, side))
        sides[side] = {**what, "edge": _outward(v, side)}  # `at`: the item's own coordinate; `edge`: the side's
    return tuple(rect), sides


def copper_boxes(items) -> list[tuple[str, tuple]]:
    """(label, world box) of copper `Copper` objects: a seg or arc grown by half its width, a via by half its size,
    a pour by its points."""
    out = []
    for c in items:
        if c.kind in ("seg", "arc"):
            pts = [p for p in (c.a, c.mid, c.b) if p is not None]
            if not pts:
                continue
            hw = (c.width or 0.0) / 2.0
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            if c.kind == "arc" and c.mid is not None:
                xs, ys = _arc_extent(c.a, c.mid, c.b)
            out.append((f"{c.net or 'no net'} {c.kind} ({pts[0][0]:g},{pts[0][1]:g})", (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw)))
        elif c.kind == "via" and c.at is not None:
            r = (c.size or 0.0) / 2.0
            out.append((f"{c.net or 'no net'} via ({c.at[0]:g},{c.at[1]:g})", (c.at[0] - r, c.at[1] - r, c.at[0] + r, c.at[1] + r)))
    return out


def _arc_extent(a, m, b):
    """The x and y extremes of the arc a -> m -> b (its end points, and each axis extreme it sweeps through)."""
    (ax, ay), (mx, my), (bx, by) = a, m, b
    dd = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(dd) < 1e-12:
        return [ax, mx, bx], [ay, my, by]
    ux = ((ax * ax + ay * ay) * (my - by) + (mx * mx + my * my) * (by - ay) + (bx * bx + by * by) * (ay - my)) / dd
    uy = ((ax * ax + ay * ay) * (bx - mx) + (mx * mx + my * my) * (ax - bx) + (bx * bx + by * by) * (mx - ax)) / dd
    r = math.hypot(ax - ux, ay - uy)
    xs, ys = [ax, bx, mx], [ay, by, my]

    def ang(px, py):
        return math.atan2(py - uy, px - ux)

    t0, tm, t1 = ang(ax, ay), ang(mx, my), ang(bx, by)

    def between(t, s, e):  # is t on the arc from s through tm to e
        def norm(v):
            return v % (2 * math.pi)

        ccw = norm(tm - s) <= norm(e - s)
        return norm(t - s) <= norm(e - s) if ccw else norm(s - t) <= norm(s - e)

    for t, (px, py) in ((0.0, (ux + r, uy)), (math.pi / 2, (ux, uy + r)), (math.pi, (ux - r, uy)), (-math.pi / 2, (ux, uy - r))):
        if between(t, t0, t1):
            xs.append(px)
            ys.append(py)
    return xs, ys


def line(d_or_dict, rect, sides: dict, *, placement_only: bool = False, source: str = "derived", decl_line: int = 0) -> str:
    """The report line (B.4): the size and corner in `Outline(width=, height=, at=)`'s terms, and what set each side."""
    x0, y0, x1, y1 = rect
    head = f"outline: {source} {x1 - x0:.2f} x {y1 - y0:.2f} mm at ({x0:.2f}, {y0:.2f})"
    if source == "declared":
        return head + (f" (Outline(), layout.core.py:{decl_line})" if decl_line else " (Outline())")
    parts = []
    for side in SIDES:
        s = sides.get(side) or {}
        what = s.get("what")
        if what == "edge face":
            axis = "x" if side in ("left", "right") else "y"
            floor = f", standing at {s['pad']} pad {axis}={s['pad_at']:.2f} + {s['clearance']:.2f} edge clearance" if s.get("pad") else ""
            parts.append(f"{side}: {s['by']} face (edge=, layout.core.py:{s.get('line', 0)}){floor}")
        elif what == "keepout face" and s.get("kind") == "rule area":
            parts.append(f'{side}: rule area "{s["by"]}" face (Pour(None, keepout=...), layout.core.py:{s.get("line", 0)})')
        elif what == "keepout face":
            parts.append(f'{side}: Keepout("{s["by"]}") face (layout.core.py:{s.get("line", 0)})')
        elif what in ("pad", "copper"):
            axis = "x" if side in ("left", "right") else "y"
            parts.append(f"{side}: {s['by']} {what} at {axis}={s['at']:.2f} + {s.get('margin', 0):.2f} margin")
        elif what in ("region", "keepout") and str(s.get("by", "")).split(" ", 1)[0] in ("Region", "Keepout"):
            axis = "x" if side in ("left", "right") else "y"
            parts.append(f"{side}: {s['by']} at {axis}={s['at']:.2f}")
        elif what:
            axis = "x" if side in ("left", "right") else "y"
            parts.append(f"{side}: {s['by']} {what} at {axis}={s['at']:.2f}")
    tail = "; placement-only fit; the free sides are fitted again after routing" if placement_only else ""
    return head + "; " + "; ".join(parts) + tail


def fixed_side_breach(job, core_cu) -> str | None:
    """B.5-5: the first core copper item that reaches past a fixed side, as the fact; None when none does. Core copper
    beyond a free side is design: the side grows to include it."""
    d = job.derived
    for c in core_cu:
        if c.kind == "pour" and c.keepout is not None and c.points:
            # A core rule area past a fixed side: the same fact (it would sit outside Edge.Cuts).
            from .layout_prims import flat_points

            pts = flat_points(c.points)
            b = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
            for side, v in d.fixed.items():
                i = IDX[side]
                if (b[i] - v) * OUT[side] > 1e-6:
                    axis = "x" if side in ("left", "right") else "y"
                    return f"{c.where or 'layout.core.py'}: rule area Pour(None, keepout=...) {c.name or ''!r} reaches {axis}={b[i]:.2f}, outside the {side} edge at {axis}={v:.2f} that {_setter_who(d.fixed_by[side], side)} sets"
            continue
        if c.kind not in ("seg", "arc", "via"):
            continue
        boxes = copper_boxes([c])
        if not boxes:
            continue
        _label, b = boxes[0]
        for side, v in d.fixed.items():
            i = IDX[side]
            if (b[i] - v) * OUT[side] > 1e-6:
                setter = d.fixed_by[side]
                axis = "x" if side in ("left", "right") else "y"
                who = _setter_who(setter, side)
                kind = {"seg": "Seg", "arc": "Arc", "via": "Via"}[c.kind]
                return f"{c.where or 'layout.core.py'}: {kind} on {c.net or 'no net'} reaches {axis}={b[i]:.2f}, outside the {side} edge at {axis}={v:.2f} that {who} sets"
    return None


def _setter_who(setter: dict, side: str) -> str:
    if setter["what"] == "edge face":
        return f'{setter["by"]} (edge="{side}", layout.core.py:{setter["line"]})'
    if setter.get("kind") == "rule area":
        return f'the rule area "{setter["by"]}" (layout.core.py:{setter["line"]})'
    return f'Keepout("{setter["by"]}") (layout.core.py:{setter["line"]})'


def routing_box_notes(d: Derived, R, copper) -> list[str]:
    """B.3 step 2: routed copper that came within one pitch of R on a free side, as a fact (not an error: the board
    is valid). One note per side."""
    notes = []
    for side in SIDES:
        if side in d.fixed:
            continue
        i = IDX[side]
        edge = R[i] - OUT[side] * d.e  # the route scene's outline is R inset by the edge clearance
        reach = [b[i] for _w, b in copper]
        if not reach:
            continue
        far = (min if OUT[side] < 0 else max)(reach)
        if (far - (edge - OUT[side] * d.pitch)) * OUT[side] >= -1e-9:
            axis = "x" if side in ("left", "right") else "y"
            notes.append(f"routed copper reached the routing box on the {side} side ({axis}={far:.2f}): the box is the placement plus {d.A:.2f} mm")
    return notes


def slot_refusal(design, job, O) -> str | None:
    """B.5-7: an `Isolation(slot=True)` slot is recomputed from O with a `SLOT_WEB_MM` web at each end of O's
    along-span; when that span is not longer than both webs, no slot is left between them. The fact, or None."""
    from pathlib import Path

    from .apply import SLOT_WEB_MM

    for spec in (job.constraints.isolation_specs if job.constraints is not None else ()):
        if not spec.req.slot:
            continue
        along = "y" if spec.axis == "x" else "x"
        span = (O[3] - O[1]) if along == "y" else (O[2] - O[0])
        if span <= 2 * SLOT_WEB_MM + 1e-9:
            bname = Path(design.source).name if getattr(design, "source", None) else "board.py"
            return (
                f"Isolation {spec.req.a}/{spec.req.b} ({bname}:{spec.req.line}): the slot needs {SLOT_WEB_MM:.2f} mm of board at each end, "
                f"and the derived outline is {span:.2f} mm along {along}"
            )
    return None
