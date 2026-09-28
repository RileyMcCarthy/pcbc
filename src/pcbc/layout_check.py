"""Cheap property checks on the merged layout objects, before emit. **Not** a verdict on the board.

Nothing here is KiCad and nothing here says "verified": that word belongs to `netcheck.check_copper`,
which runs KiCad's own DRC and netlist on the emitted board. What this module adds is earlier and
narrower — it reads `model.Copper` objects, never a `.kicad_pcb` — and it answers three questions:

- **Is a core line legal as written?** Core and generated copper are held to **one standard**, the
  board's hard minimums: the numbers pcbc compiles into the `.kicad_pro` design rules that KiCad's
  DRC fails a board on (`stackup.board_rules`: minimum track width, via diameter, through-hole
  diameter, clearance). A `layout.core.py` object below one is a *failure* naming its file and
  line: refused, never widened. A generated object below one is a *note* (KiCad's DRC fails the
  build on it anyway). **Default chosen for step 1, reversible:** the net's class numbers (its
  compiled track width, its netclass via) are *targets* — pcbc compiles the width as a soft KiCad
  rule (`dru.py`, a warning) and the via as the netclass default — so an object under them is a note
  for core and generated copper alike. That is what makes every line of `layout.gen.py` lockable
  verbatim: the router's neck-down tracks and plane-tap vias are under their class targets and above
  the board's minimums, and KiCad accepted them.
- **Does a core line touch another net?** A core segment, arc or via whose copper overlaps a pad or
  copper of another net is a short; one closer to it than the compiled clearance is refused too
  (`layout_job` asks the router's own scene, which knows every pad and the clearance each pair of
  nets compiles to). Both name the line.
- **Is each net one piece of copper?** Reported as a *note*, never a failure (KiCad's unconnected
  check is the gate). The connectivity is geometric and per layer: a segment joins what its copper
  touches on its own layer, a via joins what its barrel touches on the layers it spans, a pour joins
  what lies inside its outline and outside its holes, **on its own layer**. Grok's first cut unioned
  every pad of a net the moment one via or one pour of that net existed anywhere on the board.
"""

from __future__ import annotations

from collections import defaultdict

from .layout_prims import flat_points
from .model import Copper, Design
from .pads import PadGeom
from .route_geom import aabb, gap, track_shape, via_shape

_CELL = 1.0  # mm, the spatial hash's cell


def _cu_layers(c: Copper, all_layers: tuple[str, ...]) -> frozenset[str]:
    if c.kind == "via":
        a, b = c.layers or ("F.Cu", "B.Cu")
        if a in all_layers and b in all_layers:
            i, j = sorted((all_layers.index(a), all_layers.index(b)))
            return frozenset(all_layers[i : j + 1])
        return frozenset((a, b))
    if c.kind == "pour" and c.layers is not None:
        return frozenset(x for x in c.layers if x.endswith(".Cu"))
    return frozenset((c.layer,)) if c.layer else frozenset()


def _shapes(c: Copper) -> tuple:
    if c.kind == "seg" and c.a and c.b and c.width:
        return (track_shape(c.a, c.b, c.width),)
    if c.kind == "arc" and c.a and c.mid and c.b and c.width:
        return (track_shape(c.a, c.mid, c.width), track_shape(c.mid, c.b, c.width))
    if c.kind == "via" and c.at and c.size:
        return (via_shape(c.at, c.size),)
    return ()


def _inside(p: tuple[float, float], poly) -> bool:
    """Even-odd point in polygon, the polygon exactly as written (concave outlines included)."""
    x, y = p
    n = len(poly)
    hit = False
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


class _Pour:
    """A pour's outline minus its holes: what `layout_check` treats as the copper a pour can join.
    Not the fill (KiCad's clearance cut-outs are not here), so a note it gives is a hint, not a fact."""

    def __init__(self, c: Copper):
        from .layout_prims import flat_points

        # An arc entry of the outline stands in as its start, mid and end: a property check, not the fill.
        self.poly = flat_points(c.points)
        self.holes = tuple(flat_points(h) for h in c.holes)
        xs = [p[0] for p in self.poly]
        ys = [p[1] for p in self.poly]
        self.box = (min(xs), min(ys), max(xs), max(ys))

    def covers(self, p: tuple[float, float]) -> bool:
        return _inside(p, self.poly) and not any(_inside(p, h) for h in self.holes)


def _points(shapes) -> list[tuple[float, float]]:
    if isinstance(shapes, _Pour):
        return list(shapes.poly)
    return [p for s in shapes for p in s.pts]


def _box(shapes) -> tuple[float, float, float, float]:
    if isinstance(shapes, _Pour):
        return shapes.box
    xs = [aabb(s) for s in shapes]
    return (min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs), max(b[3] for b in xs))


def _cells(box) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = box
    return [(i, j) for i in range(int(x0 // _CELL), int(x1 // _CELL) + 1) for j in range(int(y0 // _CELL), int(y1 // _CELL) + 1)]


def _touch(sa, sb) -> bool:
    if isinstance(sa, _Pour) and isinstance(sb, _Pour):
        return any(sa.covers(p) for p in sb.poly) or any(sb.covers(p) for p in sa.poly)
    if isinstance(sa, _Pour):
        return any(sa.covers(p) for p in _points(sb))
    if isinstance(sb, _Pour):
        return any(sb.covers(p) for p in _points(sa))
    return any(gap(a, b) <= 1e-6 for a in sa for b in sb)


def copper_graph(copper: list[Copper], pads: list[PadGeom], layers: tuple[str, ...], *, pours: bool = True, on_short=None):
    """The board's copper as a union-find: `(nodes, find)`, a node per pad and per copper object
    (`(net, layers, shapes, owner)`), two nodes joined when their copper touches on a shared layer and
    they are one net. `pours=False` leaves the pours out, which asks whether two things are joined by
    tracks and vias alone (a tap's stub, `build._plane_gate`). `on_short(copper, text)` is called for
    copper of one net touching a pad or copper of another."""
    nodes: list[tuple[str, frozenset[str], object, object]] = []
    for p in pads:
        if p.net and p.copper:
            nodes.append((p.net, frozenset(p.cu_layers), tuple(p.copper), p))
    for c in copper:
        if not c.net:
            continue
        if c.kind == "pour":
            if pours and c.keepout is None and len(flat_points(c.points)) >= 3:
                nodes.append((c.net, _cu_layers(c, layers), _Pour(c), c))
            continue
        sh = _shapes(c)
        if sh:
            nodes.append((c.net, _cu_layers(c, layers), sh, c))
    parent = list(range(len(nodes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    boxes = []
    for i, (_net, _ls, sh, _o) in enumerate(nodes):
        box = _box(sh)
        boxes.append(box)
        for cell in _cells(box):
            grid[cell].append(i)
    checked: set[tuple[int, int]] = set()
    for _cell, members in grid.items():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                i, j = members[x], members[y]
                if (i, j) in checked:
                    continue
                checked.add((i, j))
                ni, nj = nodes[i], nodes[j]
                if not (ni[1] & nj[1]):
                    continue
                bi, bj = boxes[i], boxes[j]
                if bi[2] < bj[0] or bj[2] < bi[0] or bi[3] < bj[1] or bj[3] < bi[1]:
                    continue
                if ni[0] != nj[0] and (isinstance(ni[2], _Pour) or isinstance(nj[2], _Pour)):
                    continue  # a pour's fill keeps its clearance around other nets: an outline across a pad is normal
                if not _touch(ni[2], nj[2]):
                    continue
                if ni[0] == nj[0]:
                    if not _pour_joins_pad(ni, nj):
                        continue  # the pour's connect mode keeps its fill off this pad (m7)
                    parent[find(i)] = find(j)
                    continue
                if on_short is None:
                    continue
                oi, oj = ni[3], nj[3]
                for mine, other in ((oi, nj), (oj, ni)):
                    if isinstance(mine, Copper):
                        what = f"pad {other[3].ref}.{other[3].num}" if isinstance(other[3], PadGeom) else f"{other[3].kind} {other[3].id}"
                        on_short(mine, f"{mine.kind} on {mine.net} overlaps {what} of {other[0]}: a short")
    return nodes, find


def _pour_joins_pad(ni, nj) -> bool:
    """Whether a pour of the pad's own net connects to the pad at all: `connect="none"` keeps the fill
    clear of every pad, `thru_hole_only` of every SMD pad. A pour joins other copper regardless."""
    pour, pad = (ni, nj) if isinstance(ni[2], _Pour) else (nj, ni) if isinstance(nj[2], _Pour) else (None, None)
    if pour is None or not isinstance(pad[3], PadGeom):
        return True
    mode = pour[3].connect
    if mode == "none":
        return False
    if mode == "thru_hole_only" and pad[3].kind == "smd":
        return False
    return True


def floors(design: Design) -> dict[str, float] | None:
    """The board's hard minimums, the ones KiCad's DRC fails a board on (`stackup.board_rules`)."""
    from .compile import compile_design
    from .stackup import board_rules, get_stackup

    job = compile_design(design)
    if job.constraints is None:
        return None
    return board_rules(get_stackup(job.stackup))


def check_layout(design: Design, pads: list[PadGeom], *, layers: tuple[str, ...] = ("F.Cu", "B.Cu")) -> tuple[list[str], list[str]]:
    """(failures, notes) for `design.copper` against the placed board's `pads`.

    Failures are core lines only (see the module docstring). Pours of no net (rule areas) are
    ignored for connectivity: they are keepouts, not conductors."""
    from .compile import compile_design

    copper = list(design.copper)
    job = compile_design(design) if copper else None
    cs = job.constraints if job is not None else None
    hard = floors(design) if copper else None
    fails: list[str] = []
    notes: list[str] = []

    def say(c: Copper, text: str, *, fatal: bool) -> None:
        where = c.where or c.id
        (fails if (fatal and c.source == "core") else notes).append(f"{where}: {text}")

    # ---- one standard for core and gen: the hard minimums refuse a core line; the class targets note
    for c in copper:
        if hard is not None:
            if c.kind in ("seg", "arc") and c.width is not None and c.width + 1e-6 < hard["min_track_width"]:
                say(c, f"width {c.width:g} mm is under the board's {hard['min_track_width']:g} mm minimum track", fatal=True)
            if c.kind == "via" and c.drill is not None and c.drill + 1e-6 < hard["min_through_hole_diameter"]:
                say(c, f"drill {c.drill:g} mm is under the board's {hard['min_through_hole_diameter']:g} mm minimum hole", fatal=True)
            if c.kind == "via" and c.size is not None and c.size + 1e-6 < hard["min_via_diameter"]:
                say(c, f"size {c.size:g} mm is under the board's {hard['min_via_diameter']:g} mm minimum via", fatal=True)
            if c.kind == "pour" and c.net is not None and c.keepout is None and c.clearance is not None and c.clearance + 1e-6 < hard["min_clearance"]:
                say(c, f"clearance {c.clearance:g} mm is under the board's {hard['min_clearance']:g} mm minimum", fatal=True)
        con = cs.by_net(c.net) if cs is not None and c.net else None
        if con is None:
            continue
        if c.kind in ("seg", "arc") and c.width is not None and c.width + 1e-6 < con.width_mm.value:
            say(c, f"width {c.width:g} mm is under the {con.width_mm.value:g} mm {c.net} targets (a soft rule: KiCad warns)", fatal=False)
        if c.kind == "via" and c.size is not None and c.drill is not None and (c.size + 1e-6 < con.via.diameter_mm or c.drill + 1e-6 < con.via.drill_mm):
            say(c, f"via {c.size:g}/{c.drill:g} mm is under the {con.via.diameter_mm:g}/{con.via.drill_mm:g} mm netclass via {c.net} targets", fatal=False)

    # ---- the graph: pads and copper, joined only where copper touches on a shared layer
    nodes, find = copper_graph(copper, pads, layers, on_short=lambda mine, text: say(mine, text, fatal=True))
    by_net: dict[str, set[int]] = defaultdict(set)
    for i, (net, _ls, _sh, owner) in enumerate(nodes):
        if isinstance(owner, PadGeom):
            by_net[net].add(find(i))
    for net in sorted(by_net):
        pieces = [i for i, n in enumerate(nodes) if n[0] == net and isinstance(n[3], PadGeom)]
        groups = by_net[net]
        if len(pieces) > 1 and len(groups) > 1:
            notes.append(f"{net}: its pads are in {len(groups)} pieces of copper by this check (property check, not KiCad's)")
    return sorted(set(fails)), notes
