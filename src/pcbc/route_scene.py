"""The placed board as obstacles, and the five rules a candidate is judged by.

`build_scene` reads a placed or partly routed board file and turns everything on it — every pad
primitive, every drill, every track, every via, every keepout and rule area, every closed row's
fanout lane, and the board edge — into one flat, sorted tuple of `Item`s, each carrying the copper
shape, the hole shape and the mask shape `route_geom` decides with. `clashes` and `blocked` are the
only places a candidate is judged, they apply A.4's five rules in A.4's order, and every number they
use comes from `constraints.ClearanceTable` so the router cannot drift from the rules KiCad is
handed (`docs/r2-design.md` A.6).

Three things make byte-identical output arguable rather than hoped for. `Scene.items` is sorted by
`(KIND_ORDER[kind], net, owner, x_nm, y_nm)` and ids are assigned after the sort, so `item.id` is
the canonical tie-break and no answer depends on the order a file happened to list its footprints.
Copper added mid-run by `Scene.add` is appended above every existing id, in the order the patterns
emitted it. And nothing here iterates a `set` or a dict keyed on a float in a decision path.

The index is the one performance choice: a uniform bucket grid at `CELL_MM`, keyed
`(layer, floor(x), floor(y))`. node is 60 x 45 mm with about 1200 items and a typical query returns
a handful of ids against a linear scan of 152 pads alone.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .compile import CompiledJob
from .constraints import ClearanceTable, ConstraintSet, clearance_table
from .copper import _rotate
from .copper_bar import segments as _segments
from .layout import content_rect, footprints_by_ref, resolve_keepout, resolve_regions
from .model import Design
from .pads import PadGeom, pad_geoms
from .pcb_place import _EDGE_OUT, _pins_of, Foot, lane_rules, parse_foot
from .route_emit import Piece, seg_piece
from .sexp import matching_paren
from .route_geom import (
    Box,
    EPS_MM,
    MICRO_MM,
    Pt,
    Shape,
    aabb,
    box_grow,
    circle_shape,
    clears,
    gap,
    q,
    qp,
    rect_shape,
    track_shape,
    via_shape,
)
from .sexp import footprint_at
from .stackup import Stackup, get_stackup

__all__ = [
    "CELL_MM",
    "Clash",
    "Exit",
    "Item",
    "KIND_ORDER",
    "Scene",
    "ZoneRule",
    "antipad_clash",
    "audit",
    "blocked",
    "build_scene",
    "Run",
    "clashes",
    "clear_runs",
    "components",
    "copper_items",
    "free_intervals",
    "krt_grid",
    "lane_run_mm",
    "lane_strips",
    "net_open",
    "net_runs",
    "pad_exits",
    "plane_targets",
    "zone_rules",
]

KIND_ORDER = {"pad": 0, "hole": 1, "track": 2, "via": 3, "keepout": 4, "lane": 5, "edge": 6}
CELL_MM = 1.0
_TOL = 1e-9  # the merge tolerance of A.8, a thousandth of a nanometre


@dataclass(frozen=True)
class Item:
    """One thing on the board a candidate has to clear.

    `net` is `""` for an item with no net and it is **never invented**: node's `LED` link passes
    0.1367 mm from `U1.15`, an unbound pad, and a blocker message that gives it a net is the one
    kind of move the AI cannot act on — so the message prints `[no net]` instead (A.7).

    `mask` is `None` when the mask opening is the copper itself (no `solder_mask_margin`); `copper`
    is `None` for a bare drill and `hole` is `None` for everything that is not drilled.
    """

    id: int
    kind: str
    net: str
    layers: frozenset[str]
    copper: Shape | None
    hole: Shape | None
    mask: Shape | None
    owner: str
    reason: str = ""
    locked: bool = False
    prop: frozenset[str] = frozenset()
    """`pads.PadGeom.prop` carried through for a pad item, empty for everything else.

    Only one pattern reads it and only one token is in it on these boards
    (`pads.PadGeom.prop`): `pad_prop_heatsink`, the footprint's own declaration that a land is
    there to move heat. It rides on the `Item` rather than being looked up again from the board text
    because a pattern is handed a scene and nothing else, and re-parsing the footprint inside a
    candidate loop would be a second reader of what a pad is — which is the drift `pads.py` exists
    to prevent. Empty is the honest answer for a track, a via, a lane or the edge, none of which
    KiCad gives properties to."""

    def box(self) -> Box:
        boxes = [aabb(s) for s in (self.copper, self.hole, self.mask) if s is not None]
        if not boxes:
            return (0.0, 0.0, 0.0, 0.0)
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    def at(self) -> Pt:
        """The item's centre, on the grid. Quantised like every other coordinate here, so a stub
        that starts at a pad centre starts at a point the board file can write exactly — the raw
        midpoint of a box is a half-nanometre coordinate and that is how a stub comes out tilted."""
        b = self.box()
        return qp(((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0))

    def mask_shape(self) -> Shape | None:
        return self.mask if self.mask is not None else self.copper

    def label(self) -> str:
        """How a refusal names this item: the owner and the net, or `[no net]` when it has none."""
        return f"{self.owner} [{self.net or 'no net'}]"


@dataclass(frozen=True)
class Clash:
    """Why a candidate was refused: which item, how much air there is, how much it needed."""

    item: Item
    have: float
    need: float
    at: Pt
    rule: str  # a rule name from A.4: "copper" | "hole_to_copper" | "hole_to_hole" | "mask" | "edge"
    why: str = ""  # the clearance table's own word for where `need` came from

    def line(self) -> str:
        """One line, in the shape every pcbc move line already uses."""
        return f"{self.rule}: {self.item.label()} at {self.at[0]:.4g},{self.at[1]:.4g} has {self.have:g} mm, needs {self.need:g} mm ({self.why})"


_MAX_NEED = 3.0
"""How far past a candidate's own box the index is asked to look, in mm. Every clearance the five
boards compile is 0.0889 to 0.5 and the widest `keep_clear_mm` any example writes is 3 (buck's
`FB` and `AIN0` away from `SW`), so this covers `between` plus every hole rule with room; the index
answer is a superset either way and the exact test runs on what comes back."""

ADVISORY = ("mask",)
"""A.4 rule 4 is advisory in R2: a candidate that fails only the mask rule is accepted and emits a
`style:` note counted in the census. KiCad's own `solder_mask_bridge` check is what gates the build,
pcbc has not measured what 0.10 mm costs in tap sites, and R1's procedure for exactly this is to
count first and promote in the PR that shows the zeros."""


def krt_grid(job: CompiledJob) -> float:
    """KRT's routing grid for this board; `fanout.krt_grid` is this function and imports it."""
    return 0.05 if job.layers <= 2 else 0.1


def _plane_of(stack: Stackup, pours: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """`Scene.plane_of`: net -> **the outermost layer it is poured on**, in stackup order.

    One net could only ever be poured on one layer before S8 (`plane_targets`), so this used to be a
    dict comprehension and the question never came up. It comes up now: a net poured on two facing
    layers has two answers, and a comprehension would have silently taken whichever the author wrote
    **last**, making `tap`'s choice of plane a property of the order of a list rather than of the
    board.

    Outermost — the first in `Stackup.copper_layers()` — for two reasons, one of which is measured.
    It keeps node's existing answer if node ever declares `GND` on both inner layers: In1.Cu, the
    layer its 62 taps weld to today, so the experiment moves the pour and not the taps. And it is the
    layer nearest the pads a tap starts from, which is the shortest barrel that reaches a pour at all.
    Every via here is a through via, so the choice never decides whether a pad is connected — only
    which plane the report names and which pour `route_verify.pour_raster` predicts.
    """
    order = {lay: i for i, lay in enumerate(stack.copper_layers())}
    out: dict[str, str] = {}
    for net, layer in pours:
        if net not in out or order.get(layer, 99) < order.get(out[net], 99):
            out[net] = layer
    return out


def plane_targets(job: CompiledJob) -> tuple[tuple[str, str], ...]:
    """(net, layer) for every net that gets a plane or a pour — `krt_plan`'s own rule, factored out
    so the scene and the tap pattern read the same fact (C.2).

    **A declared `planes=` is the board's answer on every stackup; the implicit back pour is what a
    board that declares nothing gets, and only two layers have one.** That composition is the whole
    of S8's enabling change (`docs/stitch-plan.md` section 8 item 1) and it was chosen over the two
    alternatives for a reason that is measurable rather than tidy:

    - *Union* — the declaration plus the implicit `("GND", "B.Cu")` — makes `Board(planes=[("GND",
      "F.Cu")])` mean *two* pours on a board whose author asked for one, and there is then no way to
      write down "GND on the front and nothing on the back".
    - *Implicit wins* would make the declaration decoration, which is the lie `Board` used to refuse.

    Replacement keeps every board that declares nothing byte-identical — measured on all five, the
    tuple this returns is unchanged — and gives a 2-layer author the one thing S8 needs: **two pours
    of one net facing each other across the core**, which is the population R-E1 has never had
    (`docs/stitch-plan.md` section 2(l): before this change `plane_targets` was `(('GND','B.Cu'),)`
    on four boards and `(('GND','In1.Cu'),('3V3','In2.Cu'))` on node, so no board anywhere poured one
    net on two facing layers).
    """
    from fnmatch import fnmatch

    if job.planes:
        return tuple(job.planes)
    if job.layers > 2:
        return ()
    # `krt_plan` writes the back pour when GND is one of the power nets, which it reads off the
    # design; the same question asked of the compiled job is whether a `kind="power"` net's
    # patterns match GND. The two agree on every board here, and this form is pure in `job`.
    if any(cn.kind == "power" and any(fnmatch("GND", p) for p in cn.patterns) for cn in job.nets):
        return (("GND", "B.Cu"),)
    return ()


# --- the items ------------------------------------------------------------------------------------


def _quantise(dx: float, dy: float) -> str:
    """The nearest of right/left/down/up (KiCad's y grows downward). `route_checks._quantise`'s
    twin, kept here so the lower module does not import the higher one."""
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "down" if dy >= 0 else "up"


def lane_strips(f: Foot) -> dict[str, tuple[float, float, float, float]]:
    """A closed row's fanout lanes as world strips, by side. Moved here from `route_checks.py`,
    which imports it back, so the raster and the router ask about the same rectangle (A.7)."""
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


def _mask_of(copper: Shape | None, margin: float) -> Shape | None:
    """The mask opening: the copper outset by its `solder_mask_margin`. None means "the copper"."""
    if copper is None or not margin:
        return None
    return Shape(copper.pts, copper.r + margin)


def pad_items(geoms: Sequence[PadGeom], layers: tuple[str, ...]) -> list[Item]:
    """One item per pad primitive, plus the drill. A custom pad draws several primitives and each is
    its own obstacle, which is tighter than hulling them together and is why `PadGeom.copper` is a
    tuple. An NPTH has a hole and no copper at all, so it becomes a `hole` item."""
    out: list[Item] = []
    for g in geoms:
        if not g.copper:
            if g.hole is not None:
                out.append(Item(0, "hole", g.net, frozenset(layers), None, g.hole, None, g.id))
            continue
        for i, shape in enumerate(g.copper):
            out.append(
                Item(
                    0,
                    "pad",
                    g.net,
                    g.cu_layers,
                    shape,
                    g.hole if i == 0 else None,  # one item owns the drill; the rest are copper only
                    _mask_of(shape, g.mask_margin),
                    g.id,
                    prop=g.prop,
                )
            )
    return out


def copper_items(text: str, stack: Stackup) -> list[Item]:
    """Every track and via already on the board, as items. A via's hole is its drill, which is what
    rules 2 and 3 measure; its copper is the ring."""
    out: list[Item] = []
    for s in _segments(text):
        a, b = qp(s["start"]), qp(s["end"])
        if a == b:
            continue  # a zero-length segment is not copper anything can clash with
        out.append(
            Item(
                0,
                "track",
                s["net"],
                frozenset({s["layer"]}),
                track_shape(a, b, s["width"]),
                None,
                None,
                f"{s['net'] or 'no net'} track on {s['layer']}",
                locked=s["locked"],
            )
        )
    for v in _vias(text):
        at = qp(v["at"])
        out.append(
            Item(
                0,
                "via",
                v["net"],
                frozenset(stack.copper_layers()),
                via_shape(at, v["size"]),
                circle_shape(at[0], at[1], v["drill"]),
                None,
                f"{v['net'] or 'no net'} via at ({at[0]:g},{at[1]:g})",
                locked=v["locked"],
            )
        )
    return out


def _vias(text: str) -> list[dict]:
    """`copper_bar.vias` plus the drill, which the bar does not need and rules 2 and 3 do."""
    import re

    from .copper import net_table

    names = net_table(text)
    pat = re.compile(
        r'\(via\s*\(at ([-0-9.]+) ([-0-9.]+)\)\s*\(size ([-0-9.]+)\)\s*\(drill ([-0-9.]+)\)\s*\(layers "([^"]+)" "([^"]+)"\)'
        r'(?:\s*\(locked yes\))?\s*\(net (?:(\d+)|(?:\d+\s+)?"([^"]*)")\)'
    )
    out = []
    for m in pat.finditer(text):
        net = names.get(int(m.group(7))) if m.group(7) else m.group(8)
        out.append(
            {
                "net": net or "",
                "at": (float(m.group(1)), float(m.group(2))),
                "size": float(m.group(3)),
                "drill": float(m.group(4)),
                "layers": (m.group(5), m.group(6)),
                "locked": "(locked yes)" in m.group(0),
            }
        )
    return out


def _sort_key(it: Item) -> tuple:
    b = it.box()
    return (KIND_ORDER.get(it.kind, 99), it.net, it.owner, round(b[0] * 1e6), round(b[1] * 1e6))


# --- the zones' own numbers -----------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneRule:
    """One copper pour's fill arithmetic, read off the board file it is written in.

    A pour is not an obstacle (A.4), so nothing in the scene modelled it until S5's review. What it
    still decides is the copper it *keeps*: a foreign hole in a plane carves an antipad of the hole's
    copper plus `pad_clearance`, and where two antipads leave a neck narrower than `min_thickness`
    KiCad deletes the neck and the two become one slot (`docs/r2-measurements.md` S5r, finding 10:
    eleven taps on node's `U1` at the footprint's own 0.8 mm pitch cut an 8.65 mm slot through the
    3V3 plane, and the path across it went from 1.80 mm to 8.09 mm).

    Only the header is read, never the fill: on four layers the zone exists when the post stage runs
    and its polygons do not (`04_planes.kicad_pcb` has zero `filled_polygon`), which is the whole
    reason the numbers are worth having at placement time.
    """

    net: str
    layer: str
    pad_clearance: float
    min_thickness: float


_ZONE_HEAD = re.compile(r"\n\t\(zone\b")
_ZONE_NET_RE = re.compile(r'\(net_name "([^"]*)"\)|\(net "([^"]*)"\)')
_ZONE_LAYER_RE = re.compile(r'\(layers? "([^"]+)"')
_ZONE_CLEAR_RE = re.compile(r"\(connect_pads[^()]*(?:\s*\(clearance ([-0-9.]+)\))")
_ZONE_MINTHICK_RE = re.compile(r"\(min_thickness ([-0-9.]+)\)")


def zone_rules(text: str) -> tuple[ZoneRule, ...]:
    """Every copper pour's `(net, layer, connect_pads clearance, min_thickness)`, in file order.

    A keepout zone has no net and several layers and is skipped: it pours no copper, so it has no
    antipads to merge. A zone naming more than one layer is skipped for the same reason — every pour
    pcbc's plans write names exactly one.
    """
    out: list[ZoneRule] = []
    for m in _ZONE_HEAD.finditer(text):
        end = matching_paren(text, m.start() + 2)
        head = text[m.start() : end + 1].split("(polygon", 1)[0]
        if "(keepout" in head:
            continue
        nm = _ZONE_NET_RE.search(head)
        lm = _ZONE_LAYER_RE.search(head)
        net = (nm.group(1) or nm.group(2)) if nm else ""
        layers = lm.group(1) if lm else ""
        if not net or " " in layers or not layers:
            continue
        cm = _ZONE_CLEAR_RE.search(head)
        tm = _ZONE_MINTHICK_RE.search(head)
        out.append(
            ZoneRule(
                net=net,
                layer=layers,
                pad_clearance=float(cm.group(1)) if cm else 0.0,
                min_thickness=float(tm.group(1)) if tm else 0.0,
            )
        )
    return tuple(out)


ANTIPAD_QUERY_MM = 3.0
"""How far past the via the bucket index is asked to look for a neighbouring antipad, in mm. The
widest number this check can need is `2 * pad_clearance + min_thickness` — 0.46 mm on node — plus the
two rings; 3 mm covers it with the same room `_MAX_NEED` leaves the clearance rules."""


def antipad_clash(scene: "Scene", at: Pt, dia: float, net: str, *, ignore: frozenset[int] = frozenset()) -> Clash | None:
    """Would a through via here merge its antipad with a neighbour's, in a plane it does not join?

    A via on a foreign net punches a hole of `via + 2 * pad_clearance` in a pour. Two such holes
    `min_thickness` or less apart leave a neck KiCad deletes, and the two antipads become one slot —
    which is how eleven taps on node's `U1`, placed one per pad at the footprint's own 0.8 mm pitch
    (0.71 mm of antipad, a 0.09 mm neck against the zone's 0.1 mm `min_thickness`), cut 8.65 mm of
    the 3V3 plane in half and turned a 1.80 mm path across it into 8.09 mm (finding 10).

    Asked of **holes**, which is what the measurement found and what a tap can add: every drilled item
    already on this layer that is not the pour's own net. A track on a plane layer voids copper too,
    but no plan here routes one, and a rule is only worth what it has been measured against.

    The clearance an antipad is cut at is the **larger** of the zone's own `connect_pads` number and
    the clearance table's, because KiCad's fill honours both. Measured on node, where they differ:
    the zone is written `(connect_pads yes (clearance 0.18))` and the class clearance between GND and
    3V3 is 0.2, and a tap via's antipad in the 3V3 plane comes back with a radius of 0.375-0.38 mm
    against the via's own 0.175 — the class number, not the zone's. Taking the zone's alone put the
    requirement 0.02 mm light and left a neck KiCad then deleted anyway.
    """
    ring = via_shape(at, dia)
    box = (at[0], at[1], at[0], at[1])
    for z in scene.zone_rules:
        if z.net == net or z.layer not in scene.layers or z.min_thickness <= 0.0:
            continue
        mine = max(z.pad_clearance, scene.table.between(z.net, net)[0])
        for i in scene.query(z.layer, box, mine * 2 + z.min_thickness + dia + ANTIPAD_QUERY_MM):
            it = scene.items[i]
            if i in ignore or it.hole is None or it.net == z.net or z.layer not in it.layers:
                continue
            shape = it.copper if it.copper is not None else it.hole
            theirs = max(z.pad_clearance, scene.table.between(z.net, it.net)[0]) if it.copper is not None else scene.table.hole_to_copper()
            need = round(mine + theirs + z.min_thickness, 6)
            if not clears(ring, shape, need):
                return Clash(it, gap(ring, shape), need, it.at(), "plane_neck", f"the {z.net} plane on {z.layer}, whose min_thickness is {z.min_thickness:g} mm")
    return None


# --- the scene ------------------------------------------------------------------------------------


@dataclass
class Scene:
    """The board as obstacles, plus the numbers and the index every decision needs."""

    stack: Stackup
    cs: ConstraintSet
    table: ClearanceTable
    items: tuple[Item, ...]
    outline: Box  # the content rect inset by `stack.edge_clearance` exactly (A.4 rule 5)
    plane_of: dict[str, str]  # net -> the plane or pour layer it is welded to (C.2)
    layers: tuple[str, ...]
    grid: float  # krt_grid(job)
    feet: dict[str, Foot]
    zone_rules: tuple[ZoneRule, ...] = ()  # the filled zones' own fill numbers, read off the board
    _cells: dict = field(default_factory=dict, repr=False)
    _max_r: float = 0.0  # the widest offset radius on the board; the query pad is built from it

    # -- the index -----------------------------------------------------------------------------

    def _index(self, items: Iterable[Item]) -> None:
        for it in items:
            x0, y0, x1, y1 = it.box()
            for layer in sorted(it.layers):
                for cx in range(math.floor(x0 / CELL_MM), math.floor(x1 / CELL_MM) + 1):
                    for cy in range(math.floor(y0 / CELL_MM), math.floor(y1 / CELL_MM) + 1):
                        self._cells.setdefault((layer, cx, cy), []).append(it.id)

    def query(self, layer: str, box: Box, pad: float) -> tuple[int, ...]:
        """The ascending ids whose cells cover `box` grown by `pad` on `layer`.

        The caller passes `pad = max_need + max_item_r` so nothing near enough to matter is missed;
        a bucket answer is a superset and `clashes` does the exact test on what comes back.
        """
        x0, y0, x1, y1 = box_grow(box, pad)
        found: set[int] = set()
        for cx in range(math.floor(x0 / CELL_MM), math.floor(x1 / CELL_MM) + 1):
            for cy in range(math.floor(y0 / CELL_MM), math.floor(y1 / CELL_MM) + 1):
                found.update(self._cells.get((layer, cx, cy), ()))
        return tuple(sorted(found))

    def add(self, items: Iterable[Item]) -> tuple[Item, ...]:
        """Copper written mid-run: appended with ids above every existing id, in emission order, so
        an item's id never changes under a pattern that already asked about it."""
        added: list[Item] = []
        n = len(self.items)
        for i, it in enumerate(items):
            fresh = Item(n + i, it.kind, it.net, it.layers, it.copper, it.hole, it.mask, it.owner, it.reason, it.locked, it.prop)
            added.append(fresh)
        self.items = self.items + tuple(added)
        self._index(added)
        self._max_r = max([self._max_r] + [s.r for it in added for s in (it.copper, it.hole, it.mask) if s is not None])
        return tuple(added)

    def item_of(self, piece: Piece, *, reason: str = "") -> Item:
        """A `Piece` as an obstacle, so a pattern's own copper joins the scene it was judged against."""
        if piece.kind == "seg":
            return Item(
                0,
                "track",
                piece.net,
                frozenset({piece.layer}),
                track_shape(piece.a, piece.b, piece.w),
                None,
                None,
                piece.owner or f"{piece.net or 'no net'} {piece.reason} on {piece.layer}",
                reason=reason or piece.reason,
                locked=True,
            )
        return Item(
            0,
            "via",
            piece.net,
            frozenset(self.layers),
            via_shape(piece.a, piece.w),
            circle_shape(piece.a[0], piece.a[1], piece.drill or 0.0),
            None,
            piece.owner or f"{piece.net or 'no net'} {piece.reason} via at ({piece.a[0]:g},{piece.a[1]:g})",
            reason=reason or piece.reason,
            locked=True,
        )

    # -- the questions -------------------------------------------------------------------------

    def net_of(self, owner: str) -> str:
        for it in self.items:
            if it.owner == owner:
                return it.net
        return ""

    def pads_of(self, net: str) -> tuple[Item, ...]:
        return tuple(it for it in self.items if it.kind == "pad" and it.net == net)


def _layer_list(stack: Stackup) -> tuple[str, ...]:
    return tuple(stack.copper_layers())


def build_scene(design: Design, job: CompiledJob, cs: ConstraintSet, pcb_text: str) -> Scene:
    """Everything on the placed board, sorted, indexed, and with its numbers attached.

    The footprint pass is `fanout.py`'s own, kept identical on purpose: the same `parse_foot`, the
    same net binding from the design, the same `Foot.lane(...)` — so the lanes the scene holds are
    the lanes the fanout spent and `route_checks` guards, and there is no second reading of a
    footprint that could disagree with the first.
    """
    stack = get_stackup(job.stackup)
    layers = _layer_list(stack)
    table = clearance_table(cs)
    lane_stack, lane_clear = lane_rules(job)
    nets_of = _pins_of(design)
    items: list[Item] = []
    feet: dict[str, Foot] = {}
    for ref, block in sorted(footprints_by_ref(pcb_text).items()):
        at = footprint_at(block)
        if at is None:
            continue
        foot = parse_foot(ref, block)
        foot.at, foot.rot = (at[0], at[1]), at[2]
        for pad in foot.pads:
            pad.net = nets_of.get(ref, {}).get(pad.num, pad.net)
        foot.lane(lane_stack, lane_clear)
        feet[ref] = foot
        items.extend(pad_items(pad_geoms(block, at, ref=ref, nets=nets_of.get(ref), layers=layers), layers))
        for side, strip in sorted(lane_strips(foot).items()):
            # A lane is a policy obstacle, not copper: a piece may cross it and may not run along
            # it (A.7), which is the DS2 Addon's lesson — analog copper ran *along* a lane and
            # walled AVDD, DVDD and the UART pins in. `clashes` never refuses on a lane; the lane
            # rule is the caller's, and `lane_run_mm` is how it asks.
            items.append(Item(0, "lane", "", frozenset(layers), rect_shape((strip[0] + strip[2]) / 2.0, (strip[1] + strip[3]) / 2.0, strip[2] - strip[0], strip[3] - strip[1]), None, None, f"{ref} lane {side}"))
    items.extend(copper_items(pcb_text, stack))
    regions = resolve_regions(design.board, design.regions) if design.regions else {}
    for ko in sorted(job.keepouts, key=lambda k: k.name):
        x0, y0, x1, y1 = resolve_keepout(ko, design.board, regions)
        items.append(Item(0, "keepout", "", frozenset(layers), rect_shape((x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0), None, None, f"keepout {ko.name}"))
    for area in cs.rule_areas:
        x0, y0, x1, y1 = area.box
        items.append(Item(0, "keepout", "", frozenset(layers), rect_shape((x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0), None, None, f"rule area {area.name}"))
    content = content_rect(design.board)
    e = stack.edge_clearance
    outline = (q(content.x0 + e), q(content.y0 + e), q(content.x1 - e), q(content.y1 - e))
    items.append(Item(0, "edge", "", frozenset(layers), rect_shape((outline[0] + outline[2]) / 2.0, (outline[1] + outline[3]) / 2.0, outline[2] - outline[0], outline[3] - outline[1]), None, None, "board edge"))
    ordered = tuple(
        Item(i, it.kind, it.net, it.layers, it.copper, it.hole, it.mask, it.owner, it.reason, it.locked, it.prop)
        for i, it in enumerate(sorted(items, key=_sort_key))
    )
    scene = Scene(
        stack=stack,
        cs=cs,
        table=table,
        items=ordered,
        outline=outline,
        plane_of=_plane_of(stack, plane_targets(job)),
        layers=layers,
        grid=krt_grid(job),
        feet=feet,
        zone_rules=zone_rules(pcb_text),
    )
    scene._index(ordered)
    scene._max_r = max([0.0] + [s.r for it in ordered for s in (it.copper, it.hole, it.mask) if s is not None])
    return scene


# --- A.4: the five rules --------------------------------------------------------------------------


def _piece_shapes(scene: Scene, p: Piece) -> tuple[Shape | None, Shape | None, frozenset[str]]:
    it = scene.item_of(p)
    return (it.copper, it.hole, it.layers)


def _max_r(scene: Scene) -> float:
    """The widest offset radius anything on the board carries, which is what a query box has to grow
    by on top of the clearance. Kept on the `Scene` and refreshed by `add`, because recomputing it
    per candidate makes every pattern O(items) in the scene it is only asking a local question of."""
    return scene._max_r


def _pair_clashes(table: ClearanceTable, net: str, cu: Shape | None, hole: Shape | None, layers: frozenset[str], mask_margin: float, it: Item) -> list[Clash]:
    """A.4 rules 1 to 4 for one candidate against one item, in A.4's order.

    Two items on the same non-empty net skip rules 1 and 4 but **never** rules 2 or 3: a hole is a
    hole whatever it is connected to, and `rule_severities.hole_to_hole` is `error` in every example
    `.kicad_pro`.
    """
    out: list[Clash] = []
    same = bool(net) and it.net == net
    shared = bool(layers & it.layers)
    need, why = table.between(net, it.net)
    if not same and shared and cu is not None and it.copper is not None and not clears(cu, it.copper, need):
        out.append(Clash(it, gap(cu, it.copper), need, it.at(), "copper", why))
    # Rule 2 skips a same-net pair, because KiCad's `hole_clearance` does: a hole inside its own
    # net's copper is the connection, not a violation — the barrel is plated to it. A.4's table says
    # rule 2 never skips, and that reading is not a superset but a modelling error: measured on the
    # four routed examples it refuses 21 pieces on buck, 65 on c3_usb and 161 on node, every one of
    # them a track landing on its own via and every one of them passed by KiCad, and it would make
    # a plane tap — a via in the pad it is welding — impossible to emit. Rule 3 below does NOT skip:
    # two holes are a drilling constraint whatever they are connected to.
    if not same and shared and hole is not None and it.copper is not None and not clears(hole, it.copper, table.hole_to_copper()):
        out.append(Clash(it, gap(hole, it.copper), table.hole_to_copper(), it.at(), "hole_to_copper", "stackup.hole_clearance"))
    if not same and shared and cu is not None and it.hole is not None and not clears(cu, it.hole, table.hole_to_copper()):
        out.append(Clash(it, gap(cu, it.hole), table.hole_to_copper(), it.at(), "hole_to_copper", "stackup.hole_clearance"))
    if hole is not None and it.hole is not None and not clears(hole, it.hole, table.hole_to_hole()):
        out.append(Clash(it, gap(hole, it.hole), table.hole_to_hole(), it.at(), "hole_to_hole", "stackup.hole_to_hole"))
    if not same and shared and cu is not None and table.mask_bridge() > 0.0:
        mine = _mask_of(cu, mask_margin) or cu
        theirs = it.mask_shape()
        if theirs is not None and not clears(mine, theirs, table.mask_bridge()):
            out.append(Clash(it, gap(mine, theirs), table.mask_bridge(), it.at(), "mask", "stackup.mask_bridge_min"))
    return out


def _edge_clash(scene: Scene, shapes) -> Clash | None:
    """A.4 rule 5: the candidate's copper **and** its hole inside `scene.outline`, which is the
    content rect inset by `stack.edge_clearance` exactly — KiCad measures to the nominal Edge.Cuts
    line and ignores the outline's stroke (measured, A.4 rule 5)."""
    edge = next((it for it in scene.items if it.kind == "edge"), None)
    if edge is None:
        return None
    for shape in shapes:
        if shape is None:
            continue
        b = aabb(shape)
        over = min(b[0] - scene.outline[0], b[1] - scene.outline[1], scene.outline[2] - b[2], scene.outline[3] - b[3])
        if over < -_TOL:
            return Clash(edge, round(scene.table.edge() + over, 4), scene.table.edge(), ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0), "edge", "stackup.edge_clearance")
    return None


def _near(scene: Scene, layers: frozenset[str], box: Box, pad: float) -> list[int]:
    seen: set[int] = set()
    ids: list[int] = []
    for layer in sorted(layers):
        for i in scene.query(layer, box, pad):
            if i not in seen:
                seen.add(i)
                ids.append(i)
    return sorted(ids)


def clashes(scene: Scene, pieces: Sequence[Piece], net: str, *, ignore: frozenset[int] = frozenset()) -> tuple[Clash, ...]:
    """Every rule of A.4 that every piece fails, in item order, applied in A.4's order."""
    out: list[Clash] = []
    pad = _max_r(scene) + _MAX_NEED
    for p in pieces:
        cu, hole, layers = _piece_shapes(scene, p)
        box = aabb(cu) if cu is not None else aabb(hole)
        for i in _near(scene, layers, box, pad):
            if i in ignore:
                continue
            it = scene.items[i]
            if it.kind in ("lane", "edge"):
                # A lane is crossed, not cleared (A.7): `lane_run_mm` is its own question, and the
                # edge is containment rather than a pairwise distance.
                continue
            out.extend(_pair_clashes(scene.table, net, cu, hole, layers, 0.0, it))
        clash = _edge_clash(scene, (cu, hole))
        if clash is not None:
            out.append(clash)
    return tuple(out)


def audit(scene: Scene, *, kinds: tuple[str, ...] = ("track", "via")) -> tuple[tuple[Item, Clash], ...]:
    """Every item of `kinds` against everything else on the board, as one pass with the same rules
    and the same table — D.1 item 1, and the engine of D.2's agreement test.

    One pass rather than incremental checks on purpose: an ordering bug in the incremental path
    cannot hide from it. A pair is reported once, from the lower id, so the answer is a set of
    findings about the board and not two views of each finding.
    """
    out: list[tuple[Item, Clash]] = []
    pad = _max_r(scene) + _MAX_NEED
    for sub in scene.items:
        if sub.kind not in kinds:
            continue
        box = sub.box()
        for i in _near(scene, sub.layers, box, pad):
            it = scene.items[i]
            if it.id == sub.id or it.kind in ("lane", "edge"):
                continue
            if it.kind in kinds and it.id < sub.id:
                continue  # reported already, from the other side
            margin = sub.mask.r - sub.copper.r if (sub.mask is not None and sub.copper is not None) else 0.0
            for c in _pair_clashes(scene.table, sub.net, sub.copper, sub.hole, sub.layers, margin, it):
                out.append((sub, c))
        clash = _edge_clash(scene, (sub.copper, sub.hole))
        if clash is not None:
            out.append((sub, clash))
    return tuple(out)


def blocked(scene: Scene, pieces: Sequence[Piece], net: str, *, ignore: frozenset[int] = frozenset()) -> Clash | None:
    """The worst clash by `(need - have)` descending, then `item.id` ascending. None when it fits.

    The advisory rules of `ADVISORY` are reported by `clashes` and never block: a candidate that
    fails only the mask rule is accepted and emits a `style:` note (A.4 rule 4).
    """
    hard = [c for c in clashes(scene, pieces, net, ignore=ignore) if c.rule not in ADVISORY]
    if not hard:
        return None
    return sorted(hard, key=lambda c: (-(c.need - c.have), c.item.id, c.rule))[0]


def lane_run_mm(scene: Scene, a: Pt, b: Pt, *, owner: str = "") -> tuple[float, str]:
    """The longest run this segment makes **along** any fanout lane, and whose lane it is.

    A piece may cross a lane and may not run along it: forbidding crossing outright would cut
    c3_usb's board in half along `J1`'s 1.4515 mm lane, while running along one is what walled the
    DS2 Addon's AVDD, DVDD and UART pins in (`docs/copper-plan.md` line 191). The threshold is
    `lane_width + w + 2 * scene.grid`, and the lane's owner is exempt in its own lane.
    """
    from .route_geom import clip_len_in_box

    worst, who = 0.0, ""
    for it in scene.items:
        if it.kind != "lane" or (owner and it.owner.startswith(f"{owner} ")):
            continue
        box = aabb(it.copper)
        run = clip_len_in_box(a, b, box)
        if run > worst:
            worst, who = run, it.owner
    return (round(worst, 4), who)


# --- A.7: pad exits -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Exit:
    """Where a pattern's copper leaves a pad, and which way it is pointing when it does."""

    at: Pt
    dir: tuple[int, int]  # (+-1, 0) or (0, +-1)
    stub: tuple[Pt, ...]  # pad centre -> at, on the pad's layer, at the net's width
    side: str  # "right" | "left" | "down" | "up"
    width: float
    layer: str


_SIDES = {"right": (1, 0), "left": (-1, 0), "down": (0, 1), "up": (0, -1)}
_SIDE_ORDER = ("right", "left", "down", "up")


def pad_exits(scene: Scene, pad: Item, width: float, layer: str, *, check: bool = True) -> tuple[Exit, ...]:
    """Where copper may leave this pad, in a fixed deterministic order and no scoring.

    A pattern never starts a track at a pad centre and aims at another pad centre: measured on the
    placed boards, buck's straight `EN` centre line passes 0.0892 mm from `U1.4 [FB]` against the
    0.2 mm the Default/Analog pair needs, and node's `LED` line passes 0.1367 mm from an unbound
    pad (A.7). The distance out is the pad's own half extent, plus the widest clearance this net
    owes anything on that footprint, plus half the track — floored at `MICRO_MM`, which is what
    guarantees no pattern stub can fire `track_segment_length (min 0.2mm)`.

    The order is: the pad's fanout escape side when `Foot.escape` names one, because a closed row
    can only go straight out and that is the side `fanout.py` already spent the lane on; then
    outward, the axis direction maximising `(pad_centre - footprint_centre) . dir`, then the rest by
    the same dot product with ties broken right, left, down, up.
    """
    ref = pad.owner.split(".")[0]
    foot = scene.feet.get(ref)
    num = pad.owner.split(".", 1)[1] if "." in pad.owner else ""
    cx, cy = pad.at()
    box = pad.box()
    worst = 0.0
    if foot is not None:
        for other in foot.pads:
            if other.net and other.net != pad.net:
                worst = max(worst, scene.table.between(pad.net, other.net)[0])
    worst = max(worst, scene.table.between(pad.net, "")[0])
    order: list[str] = []
    if foot is not None and num in foot.escape:
        wx, wy = _rotate(*_EDGE_OUT[foot.escape[num]], foot.rot)
        order.append(min(_SIDE_ORDER, key=lambda s: (-(_SIDES[s][0] * wx + _SIDES[s][1] * wy), _SIDE_ORDER.index(s))))
    fx, fy = foot.center() if foot is not None else (cx, cy)
    order += sorted(
        (s for s in _SIDE_ORDER if s not in order),
        key=lambda s: (-((cx - fx) * _SIDES[s][0] + (cy - fy) * _SIDES[s][1]), _SIDE_ORDER.index(s)),
    )
    mine = frozenset(it.id for it in scene.items if it.owner == pad.owner)
    out: list[Exit] = []
    for side in order:
        dx, dy = _SIDES[side]
        half = (box[2] - box[0]) / 2.0 if dx else (box[3] - box[1]) / 2.0
        # `EPS_MM` is here because `clears` is one-sided: it wants `need + EPS_MM`, so a stub placed
        # at exactly `need` is copper this module's own judge refuses. The case is not a corner one —
        # it is every two-pad passive and every IC row, where the pad beside this one is the same size
        # and sits at the same offset, so a link leaving at this distance runs past it at exactly the
        # clearance. Measured: blinky's `LED` had all sixteen of its candidates refused at
        # `0.2 mm of the 0.2 mm` before this term, which left the one net on the simplest board in the
        # repo to KRT. `free_intervals` already carries the same term for the same reason.
        d = max(half + worst + width / 2.0 + EPS_MM, MICRO_MM)
        at = qp((cx + dx * d, cy + dy * d))
        ex = Exit(at=at, dir=(dx, dy), stub=(qp((cx, cy)), at), side=side, width=width, layer=layer)
        if check:
            # Every exit is judged as a piece, and a blocked one is dropped: a stub out of a pad is
            # copper like any other, and an exit that cannot be drawn is not an exit. The pad's own
            # items are ignored (the stub starts inside them by construction) and nothing else is —
            # a stub that would cross the footprint's neighbouring pad is exactly the refusal A.7
            # asks for. `check=False` gives the unfiltered order, which is what a refusal message
            # needs to say which exit got furthest.
            piece = seg_piece(pad.net, "exit", layer, ex.stub[0], ex.stub[1], width, owner=pad.owner)
            if blocked(scene, [piece], pad.net, ignore=mine) is not None:
                continue
        out.append(ex)
    return tuple(out)


# --- A.8: free intervals --------------------------------------------------------------------------

_AXES = ("x", "y", "u", "v")


def _project(box: Box, axis: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """(along, across) extents of a box on one of the four axes.

    A diagonal is the world transformed by `u = x + y`, `v = x - y`: no sqrt(2) on the coordinates,
    so 4 dp stays exact, and `half` and `need` carry the sqrt(2) instead.
    """
    x0, y0, x1, y1 = box
    if axis == "x":
        return ((x0, x1), (y0, y1))
    if axis == "y":
        return ((y0, y1), (x0, x1))
    us = (x0 + y0, x0 + y1, x1 + y0, x1 + y1)
    vs = (x0 - y0, x0 - y1, x1 - y0, x1 - y1)
    if axis == "u":
        return ((min(us), max(us)), (min(vs), max(vs)))
    return ((min(vs), max(vs)), (min(us), max(us)))


def free_intervals(
    axis: str,
    span: tuple[float, float],
    window: tuple[float, float],
    half: float,
    net: str,
    scene: Scene,
    layer: str,
) -> tuple[tuple[float, float], ...]:
    """Which perpendicular offsets a track of half-width `half` can take across `span` on `layer`.

    One closed-form slab sweep, O(obstacles in the band): every obstacle whose extent along `axis`
    overlaps `span` grown by `half + need` blocks the interval `[c0 - half - need, c1 + half + need]`
    across; merge, complement inside `window`, drop anything narrower than `scene.grid`, round to
    4 dp. Same-net obstacles contribute nothing. This is not a search — it returns the whole answer
    at once, which is what lets a refusal say a number the AI can act on ("the widest free lane
    across x = 2.45..19.56 is 0.43 mm at y = 13.10").
    """
    if axis not in _AXES:
        raise ValueError(f"axis is one of {_AXES}, got {axis!r}")
    scale = math.sqrt(2.0) if axis in ("u", "v") else 1.0
    lo, hi = min(span), max(span)
    w0, w1 = min(window), max(window)
    blocked_spans: list[tuple[float, float]] = []
    for it in scene.items:
        if it.kind in ("lane", "edge") or layer not in it.layers:
            continue
        if bool(net) and it.net == net:
            continue
        need = (scene.table.between(net, it.net)[0] if it.kind != "keepout" else 0.0) * scale
        # `clears` carries EPS_MM on the strict side, so an offset exactly on a blocked interval's
        # boundary is refused by the rule this is supposed to answer for. The blocked interval
        # carries the same epsilon and the free one is rounded INWARD, so every offset this returns
        # is one `clears` agrees with — measured by sweeping node at 0.01 mm, where the only two
        # disagreements were the two interval endpoints themselves.
        pad = half * scale + need + EPS_MM
        along, across = _project(it.box(), axis)
        if along[1] < lo - pad or along[0] > hi + pad:
            continue
        blocked_spans.append((across[0] - pad, across[1] + pad))
    blocked_spans.sort()
    merged: list[list[float]] = []
    for a, b in blocked_spans:
        if merged and a <= merged[-1][1] + _TOL:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    free: list[tuple[float, float]] = []
    cursor = w0
    for a, b in merged:
        if a > cursor + _TOL:
            free.append((cursor, min(a, w1)))
        cursor = max(cursor, b)
        if cursor >= w1:
            break
    if cursor < w1 - _TOL:
        free.append((cursor, w1))
    out: list[tuple[float, float]] = []
    for a, b in free:
        lo = math.ceil(a * 1e4 - _TOL) / 1e4  # inward: never wider than the truth
        hi = math.floor(b * 1e4 + _TOL) / 1e4
        if hi - lo >= scene.grid - _TOL and hi > lo:
            out.append((round(lo, 4), round(hi, 4)))
    return tuple(out)


def clear_runs(
    axis: str,
    span: tuple[float, float],
    offset: float,
    half: float,
    net: str,
    scene: Scene,
    layer: str,
    *,
    ignore: frozenset[int] = frozenset(),
) -> tuple[tuple[float, float], ...]:
    """The dual of `free_intervals`: which stretches **along** one fixed line are clear.

    `free_intervals` fixes the stretch and asks which perpendicular offsets are free — the question a
    trunk asks when it is choosing where to sit. A guard has no such choice: `Guard()` names a net,
    the net has a path, and the offset is arithmetic (`patterns.stitch.guard_offset`). What is left to
    ask is the other half of the same slab sweep — *given* this line, which parts of it are clear —
    and that is the question `docs/r2-design.md` B.6's partial rule is made of: a guard drops a
    blocked stretch instead of failing, so somebody has to say where the blocked stretches are.

    **The epsilon discipline is `free_intervals`' verbatim, and it is copied rather than shared.**
    `pad = half * scale + need + EPS_MM`, merge with `_TOL`, complement inside the span, drop anything
    narrower than `scene.grid`, round each surviving endpoint **inward** to 4 dp. Every value returned
    is therefore one `clears` agrees with, which is what makes this a proposer and `clears` still the
    judge (`test_guard.py::test_clear_runs_agrees_with_clears` sweeps it at 0.01 mm the way A.8
    calibrated `free_intervals`). Copied because the two functions differ in exactly one line — which
    projection is tested against the fixed value and which is returned — and a shared helper
    parameterised on that line would be longer than both and would put `free_intervals`, which four
    patterns rest on, inside a refactor this slice has no reason to make.

    `axis` is `"x"`, `"y"`, `"u"` or `"v"`; for a diagonal the world is the same transform, `u = x + y`
    and `v = x - y`, so `span` and the return are in that coordinate and a millimetre along the leg is
    **two** of its units — the caller converts, because only the caller knows the leg's own direction.
    Lanes and the edge are skipped exactly as `free_intervals` skips them: a lane is `lane_ok`'s
    question and the outline is rule 5's, both asked of the finished candidate by `blocked`.

    `ignore` is `clashes`' own parameter and it exists for one measured reason. `_project` takes an
    item's **axis-aligned bounding box**, which on a diagonal axis is a strict and sometimes very loose
    superset: a 45 degree track's box is a square, so its `v` extent spans the whole run and it reports
    as blocking every millimetre of a `u` sweep beside it. Measured 2026-09-21 on
    `tests/fixtures/guard/guard.py`, `SIG2`'s own 3.7 mm diagonal leg blocked **all** of its own
    flank that way, at an offset computed to clear it by 0.19 mm. A caller that has already solved the
    distance to a piece of copper exactly may say so here; `blocked` still judges the finished
    candidate against that very copper, so this loosens the **proposal** and never the decision.
    """
    if axis not in _AXES:
        raise ValueError(f"axis is one of {_AXES}, got {axis!r}")
    scale = math.sqrt(2.0) if axis in ("u", "v") else 1.0
    lo, hi = min(span), max(span)
    blocked_spans: list[tuple[float, float]] = []
    for it in scene.items:
        if it.kind in ("lane", "edge") or layer not in it.layers or it.id in ignore:
            continue
        if bool(net) and it.net == net:
            continue
        need = (scene.table.between(net, it.net)[0] if it.kind != "keepout" else 0.0) * scale
        pad = half * scale + need + EPS_MM
        along, across = _project(it.box(), axis)
        # The one line that differs from `free_intervals`: there the obstacle is tested against the
        # *stretch* and contributes an interval across; here it is tested against the *offset* and
        # contributes an interval along.
        if across[1] < offset - pad or across[0] > offset + pad:
            continue
        blocked_spans.append((along[0] - pad, along[1] + pad))
    blocked_spans.sort()
    merged: list[list[float]] = []
    for a, b in blocked_spans:
        if merged and a <= merged[-1][1] + _TOL:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    free: list[tuple[float, float]] = []
    cursor = lo
    for a, b in merged:
        if a > cursor + _TOL:
            free.append((cursor, min(a, hi)))
        cursor = max(cursor, b)
        if cursor >= hi:
            break
    if cursor < hi - _TOL:
        free.append((cursor, hi))
    out: list[tuple[float, float]] = []
    for a, b in free:
        c0 = math.ceil(a * 1e4 - _TOL) / 1e4  # inward: never longer than the truth
        c1 = math.floor(b * 1e4 + _TOL) / 1e4
        if c1 - c0 >= scene.grid - _TOL and c1 > c0:
            out.append((round(c0, 4), round(c1, 4)))
    return tuple(out)


@dataclass(frozen=True)
class Run:
    """One unbranched run of a net's own track copper on one layer, as an ordered polyline.

    A *run* is what a guard offsets and what a report measures: the copper between two places where
    the net stops being a line — a pad, a via, a junction where three tracks meet. It is the scene's
    answer to the question `route_verify.paths_of` answers about a pattern's `Piece`s, and the two
    are deliberately the same walk written twice over different inputs, because they are asked at
    different times: `paths_of` judges copper before it is written and this reads copper that is.
    """

    net: str
    layer: str
    pts: tuple[Pt, ...]  # the centreline, in order
    ids: tuple[int, ...]  # the scene items it is made of, in path order
    widths: tuple[float, ...]  # one per leg, in path order

    @property
    def mm(self) -> float:
        return sum(math.dist(self.pts[i], self.pts[i + 1]) for i in range(len(self.pts) - 1))


def net_runs(scene: Scene, net: str, layer: str | None = None) -> tuple[Run, ...]:
    """Every run of `net`'s track copper, sorted, with the items each is made of.

    Walked from every endpoint that is not a plain pass-through, exactly as `route_verify.paths_of`
    walks a pattern's pieces: **a vertex where three tracks meet is a junction and ends the chains
    that reach it**, which is what a spine rib, a chain tap or a T off a rail looks like. A run with
    no such endpoint at all is a closed loop, and it starts at its smallest vertex so the answer is a
    function of the copper rather than of the order the file happened to list it in.

    Sorted throughout — the legs by `(a, b)`, the runs by `(layer, pts)` — and canonicalised so a run
    reads from its smaller endpoint. Two boards with the same copper written in a different order give
    the same tuple, which is what a pattern reading this needs and what `free_intervals` gets for free
    by being closed-form.
    """
    want = (layer,) if layer is not None else scene.layers
    out: list[Run] = []
    for L in want:
        legs: list[tuple[Pt, Pt, float, int]] = []
        for it in scene.items:
            if it.kind != "track" or it.net != net or it.copper is None or L not in it.layers:
                continue
            pts = it.copper.pts
            if len(pts) != 2:
                continue  # a track is a capsule over two points; anything else is not one
            a, b = (pts[0], pts[1]) if pts[0] <= pts[1] else (pts[1], pts[0])
            legs.append((a, b, round(it.copper.r * 2.0, 4), it.id))
        if not legs:
            continue
        legs.sort(key=lambda g: (g[0], g[1], g[3]))
        adj: dict[Pt, list[int]] = {}
        for i, (a, b, _w, _id) in enumerate(legs):
            adj.setdefault(a, []).append(i)
            adj.setdefault(b, []).append(i)
        seen: set[int] = set()
        starts = [pt for pt in sorted(adj) if len(adj[pt]) != 2] or sorted(adj)[:1]
        for start in starts:
            for first in sorted(adj[start]):
                if first in seen:
                    continue
                chain = [start]
                used: list[int] = []
                here, leg = start, first
                while True:
                    seen.add(leg)
                    used.append(leg)
                    a, b, _w, _id = legs[leg]
                    nxt = b if a == here else a
                    chain.append(nxt)
                    here = nxt
                    if len(adj.get(here, ())) != 2:
                        break
                    onward = [j for j in adj[here] if j != leg]
                    if not onward or onward[0] in seen:
                        break
                    leg = onward[0]
                pts = tuple(chain)
                ids = tuple(legs[j][3] for j in used)
                widths = tuple(legs[j][2] for j in used)
                if pts[0] > pts[-1]:
                    pts, ids, widths = tuple(reversed(pts)), tuple(reversed(ids)), tuple(reversed(widths))
                out.append(Run(net=net, layer=L, pts=pts, ids=ids, widths=widths))
    return tuple(sorted(out, key=lambda r: (r.layer, r.pts)))


# --- A.9: who still needs KRT ---------------------------------------------------------------------


def _touch(a: Item, b: Item) -> bool:
    if not (a.layers & b.layers) or a.copper is None or b.copper is None:
        return False
    return gap(a.copper, b.copper) <= 0.0


def components(scene: Scene, net: str) -> tuple[frozenset[str], ...]:
    """The net's pads grouped by what is actually connected to what, computed and never declared.

    Union-find over the net's pads, tracks and vias — two items join when their copper touches on a
    shared layer — plus the plane when `scene.plane_of` names a layer for this net: a pad joins the
    plane when it has a via on the plane's layer, which is exactly what a tap is. So a pattern that
    connected three of a net's four pads is honest by construction, and this is the structure R3's
    maze core takes as its input.
    """
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
    plane = scene.plane_of.get(net)
    if plane is not None:
        roots = [find(i) for i, it in enumerate(mine) if it.kind == "via" and plane in it.layers]
        for r in roots[1:]:
            union(roots[0], r)
        if roots:
            for i, it in enumerate(mine):
                if it.kind == "via" and plane in it.layers:
                    union(roots[0], i)
    groups: dict[int, set[str]] = {}
    for i, it in enumerate(mine):
        if it.kind != "pad":
            continue
        groups.setdefault(find(i), set()).add(it.owner)
    return tuple(frozenset(v) for _k, v in sorted(groups.items(), key=lambda kv: sorted(kv[1])))


def net_open(scene: Scene, net: str) -> bool:
    """Does this net still need a router? True when its pads are in more than one component."""
    return len(components(scene, net)) > 1
