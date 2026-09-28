"""`layout.gen.py`: the generator's layout objects written as the layout language, one KiCad primitive
per line; and `decompile`, the reader of a board KiCad saved.

The native generator (`place_native`, `route_native`; `docs/direction.md` §1) makes Python objects
directly and `gen_source` writes them: `Place`/`Seg`/`Arc`/`Via`/`Pour` and drawing lines with **every**
field explicit, sorted, one object per line, so any line can be copied verbatim into `layout.core.py`
to lock it (core and gen are held to one standard, `layout_check`). No board is decompiled to make
`layout.gen.py`. `decompile` is kept as an **importer and a checker**: it reads a board **KiCad itself
saved** (so every field KiCad writes is present and spelled KiCad's way) and returns one `model.Pose`
per footprint, one `model.Copper` per `(segment)`, `(arc)`, `(via)` and `(zone)`, and one
`model.Graphic` per board drawing (`gr_*`, `dimension`, `group`, `image`, `table`, `barcode`,
`target`, `point`, `generated`; `layout_prims.SCHEMAS`), field for field — which is how the build proves,
after the emit, that the emitted board is the Python it was written from.

The mapping is a table, not code paths: `FIELDS` below lists, per primitive, every Python keyword,
the model attribute it lands in, and the KiCad token it is. The writer walks it, the docs table is
checked against it, and the decompiler refuses a token it has no row for (`Unmapped`) rather than
dropping it — a field Python cannot carry is a defect to fix here, never a silent loss.

Build output only. `layout.gen.py` is rewritten every build, never edited, and never read back as
design input: the build loads it once, right after writing it, to emit the final board from the
loaded objects (so the board is Python -> KiCad, not router -> KiCad).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .model import Copper, Graphic, Pose
from .sexp import Q, parse_tree

# (python keyword, Copper/Pose attribute, KiCad token). A keyword of None is positional.
FIELDS: dict[str, list[tuple[str | None, str, str]]] = {
    "Place": [
        (None, "ref", 'footprint (property "Reference" ...)'),
        ("at", "at", "footprint (at x y)"),
        ("rot", "rot", "footprint (at x y rot) (omitted when 0)"),
        ("layer", "layer", "footprint (layer)"),
        ("locked", "locked", "footprint (locked yes)"),
    ],
    "Seg": [
        (None, "net", "segment (net)"),
        (None, "a", "segment (start x y)"),
        (None, "b", "segment (end x y)"),
        ("width", "width", "segment (width)"),
        ("layer", "layer", "segment (layer) / first of (layers cu mask)"),
        ("solder_mask", "solder_mask", 'segment (layers cu "F.Mask"|"B.Mask")'),
        ("solder_mask_margin", "solder_mask_margin", "segment (solder_mask_margin)"),
        ("locked", "locked", "segment (locked yes)"),
        ("uuid", "uuid", "segment (uuid)"),
        ("id", "id", "- (pcbc handle)"),
    ],
    "Arc": [
        (None, "net", "arc (net)"),
        (None, "a", "arc (start x y)"),
        (None, "mid", "arc (mid x y)"),
        (None, "b", "arc (end x y)"),
        ("width", "width", "arc (width)"),
        ("layer", "layer", "arc (layer) / first of (layers cu mask)"),
        ("solder_mask", "solder_mask", 'arc (layers cu "F.Mask"|"B.Mask")'),
        ("solder_mask_margin", "solder_mask_margin", "arc (solder_mask_margin)"),
        ("locked", "locked", "arc (locked yes)"),
        ("uuid", "uuid", "arc (uuid)"),
        ("id", "id", "- (pcbc handle)"),
    ],
    "Via": [
        (None, "net", "via (net)"),
        (None, "at", "via (at x y)"),
        ("size", "size", "via (size)"),
        ("drill", "drill", "via (drill)"),
        ("layers", "layers", "via (layers a b)"),
        ("kind", "via_type", "via atom: (via blind|buried|micro ...), none = through"),
        ("locked", "locked", "via (locked yes)"),
        ("free", "free", "via (free yes)"),
        ("remove_unused_layers", "remove_unused_layers", "via (remove_unused_layers yes)"),
        ("keep_end_layers", "keep_end_layers", "via (keep_end_layers yes)"),
        ("zone_layer_connections", "zone_layer_connections", "via (zone_layer_connections ...)"),
        ("tenting", "tenting", "via (tenting (front) (back))"),
        ("covering", "covering", "via (covering (front) (back))"),
        ("plugging", "plugging", "via (plugging (front) (back))"),
        ("capping", "capping", "via (capping yes|no)"),
        ("filling", "filling", "via (filling yes|no)"),
        ("padstack", "padstack", "via (padstack (mode) (layer name (size)) ...)"),
        ("backdrill", "backdrill", "via (backdrill (size) (layers a b))"),
        ("tertiary_drill", "tertiary_drill", "via (tertiary_drill (size) (layers a b))"),
        ("front_post_machining", "front_post_machining", "via (front_post_machining counterbore|countersink (size) (depth) (angle))"),
        ("back_post_machining", "back_post_machining", "via (back_post_machining counterbore|countersink (size) (depth) (angle))"),
        ("teardrops", "teardrops", "via (teardrops (best_length_ratio) ... (prefer_zone_connections))"),
        ("uuid", "uuid", "via (uuid)"),
        ("id", "id", "- (pcbc handle)"),
    ],
    "Pour": [
        (None, "net", "zone (net); None = no (net) token"),
        ("layer", "layer", "zone (layer)"),
        ("layers", "layers", "zone (layers ...)"),
        ("name", "name", "zone (name)"),
        ("priority", "priority", "zone (priority)"),
        ("locked", "locked", "zone (locked yes)"),
        ("hatch", "hatch", "zone (hatch style pitch)"),
        ("connect", "connect", "zone (connect_pads [yes|no|thru_hole_only] ...): thermal|solid|none|thru_hole_only"),
        ("clearance", "clearance", "zone (connect_pads (clearance))"),
        ("min_thickness", "min_thickness", "zone (min_thickness)"),
        ("filled_areas_thickness", "filled_areas_thickness", "zone (filled_areas_thickness yes|no)"),
        ("teardrop_type", "teardrop_type", "zone (attr (teardrop (type)))"),
        ("keepout", "keepout", "zone (keepout (tracks) (vias) (pads) (copperpour) (footprints))"),
        ("placement", "placement", "zone (placement (enabled) (sheetname|component_class|group))"),
        ("filled", "filled", "zone (fill yes ...)"),
        ("fill_mode", "fill_mode", "zone (fill (mode hatch))"),
        ("thermal_gap", "thermal_gap", "zone (fill (thermal_gap))"),
        ("thermal_bridge_width", "thermal_bridge_width", "zone (fill (thermal_bridge_width))"),
        ("smoothing", "smoothing", "zone (fill (smoothing))"),
        ("radius", "radius", "zone (fill (radius))"),
        ("island_removal_mode", "island_removal_mode", "zone (fill (island_removal_mode))"),
        ("island_area_min", "island_area_min", "zone (fill (island_area_min))"),
        ("hatch_thickness", "hatch_thickness", "zone (fill (hatch_thickness))"),
        ("hatch_gap", "hatch_gap", "zone (fill (hatch_gap))"),
        ("hatch_orientation", "hatch_orientation", "zone (fill (hatch_orientation))"),
        ("hatch_smoothing_level", "hatch_smoothing_level", "zone (fill (hatch_smoothing_level))"),
        ("hatch_smoothing_value", "hatch_smoothing_value", "zone (fill (hatch_smoothing_value))"),
        ("hatch_border_algorithm", "hatch_border_algorithm", "zone (fill (hatch_border_algorithm))"),
        ("hatch_min_hole_area", "hatch_min_hole_area", "zone (fill (hatch_min_hole_area))"),
        ("hatch_position", "hatch_position", 'zone (property (layer "X") (hatch_position (xy x y))), one per layer: {"X": (x, y)}'),
        ("points", "points", "zone first (polygon (pts (xy) | (arc (start) (mid) (end)) ...))"),
        ("holes", "holes", "zone every later (polygon (pts ...))"),
        ("uuid", "uuid", "zone (uuid)"),
        ("id", "id", "- (pcbc handle)"),
    ],
}
KIND_OF = {"seg": "Seg", "arc": "Arc", "via": "Via", "pour": "Pour"}


from .layout_prims import Unmapped  # noqa: E402  (one class: a token no Python field carries)


class DuplicateUuid(Unmapped):
    """A uuid two layout objects of one board carry. A uuid names one object: the fab gate compares
    the routed and the fab board by it, a role group names its members by it, and KiCad's DRC
    reports items by it. Until the fourth review a second via or segment carrying an existing uuid
    was invisible to `fab.fab_copper_diff` (a dict keyed by uuid kept one of the two) and shipped,
    KiCad re-netting the stray to GND on load. The decompiler refuses the board instead."""

    def __init__(self, uuid: str, kinds: list[str]):
        super().__init__(f"uuid {uuid} is carried by {len(kinds)} layout objects ({', '.join(kinds)}); a uuid names one object")
        self.uuid = uuid
        self.kinds = kinds


# ----------------------------------------------------------------------------------- reading


def _num(a) -> float:
    return float(a)


def _xy(node) -> tuple[float, float]:
    return (_num(node[1]), _num(node[2]))


def _bool(node, where: str) -> bool:
    if len(node) != 2 or node[1] not in ("yes", "no"):
        raise Unmapped(f"{where}: ({node[0]} ...) is not yes/no: {node!r}")
    return node[1] == "yes"


class _Kids:
    """A primitive's children by head, each taken at most once; whatever is left is `Unmapped`."""

    def __init__(self, node: list, where: str):
        self.where = where
        self.atoms = [x for x in node[1:] if not isinstance(x, list)]
        self.kids: dict[str, list] = {}
        self.many: dict[str, list[list]] = {}
        for x in node[1:]:
            if isinstance(x, list):
                self.many.setdefault(x[0], []).append(x)

    def take(self, head: str) -> list | None:
        got = self.many.get(head)
        if not got:
            return None
        if len(got) > 1:
            raise Unmapped(f"{self.where}: ({head}) appears {len(got)} times")
        del self.many[head]
        return got[0]

    def take_all(self, head: str) -> list[list]:
        return self.many.pop(head, [])

    def need(self, head: str) -> list:
        got = self.take(head)
        if got is None:
            raise Unmapped(f"{self.where}: no ({head})")
        return got

    def done(self) -> None:
        if self.many:
            raise Unmapped(f"{self.where}: KiCad wrote {sorted(self.many)} and no Python field carries it")


def _track(node: list, kind: str) -> Copper:
    k = _Kids(node, kind)
    if k.atoms:
        raise Unmapped(f"{kind}: bare atoms {k.atoms}")
    uid = str(k.need("uuid")[1])
    k.where = f"{kind} {uid}"
    layer_node, layers_node = k.take("layer"), k.take("layers")
    mask = False
    if layers_node is not None:
        if len(layers_node) != 3 or not str(layers_node[2]).endswith(".Mask"):
            raise Unmapped(f"{k.where}: (layers ...) is not a copper layer and its mask: {layers_node!r}")
        layer, mask = str(layers_node[1]), True
    else:
        layer = str(layer_node[1])
    locked = k.take("locked")
    margin = k.take("solder_mask_margin")
    net = k.take("net")
    c = Copper(
        "seg" if kind == "segment" else "arc",
        "",
        None if net is None else str(net[1]),
        layer=layer,
        a=_xy(k.need("start")),
        b=_xy(k.need("end")),
        mid=_xy(k.need("mid")) if kind == "arc" else None,
        width=_num(k.need("width")[1]),
        locked=_bool(locked, k.where) if locked is not None else False,
        solder_mask=mask,
        solder_mask_margin=None if margin is None else _num(margin[1]),
        uuid=uid,
        source="gen",
    )
    k.done()
    return c


def _sided(node: list | None, where: str) -> dict | None:
    if node is None:
        return None
    out = {}
    for x in node[1:]:
        if not isinstance(x, list) or x[0] not in ("front", "back") or len(x) != 2:
            raise Unmapped(f"{where}: ({node[0]} ...) holds {x!r}")
        out[x[0]] = str(x[1])
    return out


def _drill(node: list | None, where: str):
    if node is None:
        return None
    k = _Kids(node, f"{where} ({node[0]})")
    size = _num(k.need("size")[1])
    la = k.need("layers")
    k.done()
    return (size, (str(la[1]), str(la[2])))


def _post(node: list | None, where: str) -> dict | None:
    if node is None:
        return None
    k = _Kids(node, f"{where} ({node[0]})")
    from .layout_prims import WORDS

    if len(k.atoms) != 1 or k.atoms[0] not in WORDS["via.post_machining.mode"]:
        raise Unmapped(f"{k.where}: mode {k.atoms!r}")
    out: dict = {"mode": str(k.atoms[0])}
    for key in ("size", "depth", "angle"):
        n = k.take(key)
        if n is not None:
            out[key] = _num(n[1])
    k.done()
    return out


def _teardrop_value(a):
    if a in ("yes", "no"):
        return a == "yes"
    return _num(a)


def _via(node: list) -> Copper:
    k = _Kids(node, "via")
    kind = "through"
    if k.atoms:
        if len(k.atoms) != 1 or k.atoms[0] not in ("blind", "buried", "micro"):
            raise Unmapped(f"via: bare atoms {k.atoms}")
        kind = k.atoms[0]
    uid = str(k.need("uuid")[1])
    k.where = w = f"via {uid}"
    flags = {f: (_bool(n, w) if (n := k.take(f)) is not None else False) for f in ("remove_unused_layers", "keep_end_layers", "locked", "free")}
    zlc = k.take("zone_layer_connections")
    cap, fil = k.take("capping"), k.take("filling")
    ps = k.take("padstack")
    padstack = None
    if ps is not None:
        pk = _Kids(ps, f"{w} (padstack)")
        mode = str(pk.need("mode")[1])
        per = []
        for la in pk.take_all("layer"):
            lk = _Kids([la[0]] + la[2:], f"{w} (padstack (layer {la[1]}))")
            per.append((str(la[1]), _num(lk.need("size")[1])))
            lk.done()
        pk.done()
        padstack = (mode, tuple(per))
    td = k.take("teardrops")
    teardrops = None
    if td is not None:
        teardrops = {}
        for x in td[1:]:
            if not isinstance(x, list) or len(x) != 2:
                raise Unmapped(f"{w}: (teardrops ...) holds {x!r}")
            teardrops[str(x[0])] = _teardrop_value(x[1])
    net = k.take("net")
    la = k.need("layers")
    c = Copper(
        "via",
        "",
        None if net is None else str(net[1]),
        layer=None,
        at=_xy(k.need("at")),
        size=_num(k.need("size")[1]),
        drill=_num(k.need("drill")[1]),
        layers=(str(la[1]), str(la[2])),
        via_type=kind,
        zone_layer_connections=tuple(str(x) for x in zlc[1:]) if zlc is not None else (),
        tenting=_sided(k.take("tenting"), w),
        covering=_sided(k.take("covering"), w),
        plugging=_sided(k.take("plugging"), w),
        capping=None if cap is None else _bool(cap, w),
        filling=None if fil is None else _bool(fil, w),
        padstack=padstack,
        backdrill=_drill(k.take("backdrill"), w),
        tertiary_drill=_drill(k.take("tertiary_drill"), w),
        front_post_machining=_post(k.take("front_post_machining"), w),
        back_post_machining=_post(k.take("back_post_machining"), w),
        teardrops=teardrops,
        uuid=uid,
        source="gen",
        **flags,
    )
    k.done()
    return c


_FILL_NUM = {
    "thermal_gap": float,
    "thermal_bridge_width": float,
    "radius": float,
    "island_removal_mode": int,
    "island_area_min": float,
    "hatch_thickness": float,
    "hatch_gap": float,
    "hatch_orientation": float,
    "hatch_smoothing_level": int,
    "hatch_smoothing_value": float,
    "hatch_min_hole_area": float,
}
_FILL_WORD = ("smoothing", "hatch_border_algorithm")
_CONNECT_OF = {None: "thermal", "yes": "solid", "no": "none", "thru_hole_only": "thru_hole_only"}


def _pts(node: list, where: str) -> tuple:
    """A zone's `(polygon (pts ...))`: points and arc entries (`layout_prims.read_pts`)."""
    from .layout_prims import read_pts

    pk = _Kids(node, where)
    pts = pk.need("pts")
    pk.done()
    return read_pts(pts, where)


def _hatch_position(nodes: list[list], where: str) -> dict | None:
    """Every `(property (layer "X") (hatch_position (xy x y)))` of a zone as `{"X": (x, y)}`, in file
    order. KiCad 10 writes one per copper layer whose hatch offset was set; nothing else lives in a
    zone's `(property)`."""
    if not nodes:
        return None
    out: dict = {}
    for n in nodes:
        pk = _Kids(n, f"{where} (property)")
        if pk.atoms:
            raise Unmapped(f"{pk.where}: bare atoms {pk.atoms}")
        layer = str(pk.need("layer")[1])
        hp = pk.need("hatch_position")
        pk.done()
        hk = _Kids(hp, f"{pk.where} (hatch_position)")
        at = _xy(hk.need("xy"))
        hk.done()
        if layer in out:
            raise Unmapped(f"{pk.where}: layer {layer!r} appears twice")
        out[layer] = at
    return out


def is_teardrop_zone(node: list) -> bool:
    """A zone KiCad itself generated for a teardrop: `(attr (teardrop (type ...)))`, no `(uuid)`, no
    `(name)`. Not a field and not a layout object — like `filled_polygon`, it is derived from the
    fields the via, the track and the pad carry (`Via(teardrops=...)`), regenerated by KiCad's refill,
    and dropped here (third review P1: the decompiler raised on it at `(uuid)`)."""
    for x in node[1:]:
        if isinstance(x, list) and x and x[0] == "attr":
            return any(isinstance(y, list) and y and y[0] == "teardrop" for y in x[1:])
    return False


def _zone(node: list) -> tuple[Copper | None, int]:
    k = _Kids(node, "zone")
    if k.atoms:
        raise Unmapped(f"zone: bare atoms {k.atoms}")
    if is_teardrop_zone(node):
        return None, len(k.take_all("filled_polygon"))
    uid = str(k.need("uuid")[1])
    k.where = w = f"zone {uid}"
    net, locked, name, prio = k.take("net"), k.take("locked"), k.take("name"), k.take("priority")
    layer, layers = k.take("layer"), k.take("layers")
    hatch = k.need("hatch")
    cp = k.need("connect_pads")
    cpk = _Kids(cp, f"{w} (connect_pads)")
    if len(cpk.atoms) > 1 or (cpk.atoms and cpk.atoms[0] not in _CONNECT_OF):
        raise Unmapped(f"{w}: (connect_pads {cpk.atoms})")
    connect = _CONNECT_OF[cpk.atoms[0] if cpk.atoms else None]
    clearance = _num(cpk.need("clearance")[1])
    cpk.done()
    attr = k.take("attr")
    teardrop = None
    if attr is not None:
        ak = _Kids(attr, f"{w} (attr)")
        td = ak.need("teardrop")
        ak.done()
        tk = _Kids(td, f"{w} (attr (teardrop))")
        teardrop = str(tk.need("type")[1])
        tk.done()
    fat = k.take("filled_areas_thickness")
    keep = k.take("keepout")
    keepout = None
    if keep is not None:
        keepout = {}
        for x in keep[1:]:
            if not isinstance(x, list) or len(x) != 2:
                raise Unmapped(f"{w}: (keepout ...) holds {x!r}")
            keepout[str(x[0])] = str(x[1])
    pl = k.take("placement")
    placement = None
    if pl is not None:
        placement = {}
        for x in pl[1:]:
            if not isinstance(x, list) or len(x) != 2:
                raise Unmapped(f"{w}: (placement ...) holds {x!r}")
            placement[str(x[0])] = (x[1] == "yes") if x[0] == "enabled" else str(x[1])
    fill = k.need("fill")
    fk = _Kids(fill, f"{w} (fill)")
    if fk.atoms not in ([], ["yes"]):
        raise Unmapped(f"{w}: (fill {fk.atoms})")
    got: dict = {}
    mode = fk.take("mode")
    for key, cast in _FILL_NUM.items():
        n = fk.take(key)
        got[key] = None if n is None else cast(float(n[1])) if cast is int else cast(n[1])
    for key in _FILL_WORD:
        n = fk.take(key)
        got[key] = None if n is None else str(n[1])
    fk.done()
    hatch_position = _hatch_position(k.take_all("property"), w)
    polys = [_pts(p, f"{w} (polygon)") for p in k.take_all("polygon")]
    if not polys:
        raise Unmapped(f"{w}: no (polygon)")
    # The fill KiCad computed. Not a field: `kicad-cli pcb drc --refill-zones` regenerates it from the
    # fields above, and `docs/layout-properties.md` records the proof that it comes back identical.
    fills = len(k.take_all("filled_polygon"))
    c = Copper(
        "pour",
        "",
        None if net is None else str(net[1]),
        layer=None if layer is None else str(layer[1]),
        layers=None if layers is None else tuple(str(x) for x in layers[1:]),
        points=polys[0],
        holes=tuple(polys[1:]),
        connect=connect,
        clearance=clearance,
        min_thickness=_num(k.need("min_thickness")[1]),
        filled_areas_thickness=None if fat is None else _bool(fat, w),
        hatch=(str(hatch[1]), _num(hatch[2])),
        priority=None if prio is None else int(prio[1]),
        name=None if name is None else str(name[1]),
        locked=False if locked is None else _bool(locked, w),
        teardrop_type=teardrop,
        keepout=keepout,
        placement=placement,
        filled=fk.atoms == ["yes"],
        fill_mode=None if mode is None else str(mode[1]),
        hatch_position=hatch_position,
        uuid=uid,
        source="gen",
        **got,
    )
    k.done()
    return c, fills


def _pose(node: list) -> Pose:
    ref = None
    at = layer = None
    locked = False
    for x in node[1:]:
        if not isinstance(x, list):
            continue
        if x[0] == "property" and len(x) > 2 and x[1] == "Reference":
            ref = str(x[2])
        elif x[0] == "at":
            at = x
        elif x[0] == "layer":
            layer = str(x[1])
        elif x[0] == "locked":
            locked = _bool(x, f"footprint {ref}")
    if ref is None or at is None or layer is None:
        raise Unmapped(f"footprint {node[1]!r}: no Reference, (at) or (layer)")
    return Pose(ref, _xy(at), _num(at[3]) if len(at) > 3 else 0.0, layer, locked)


def _drawing(node: list) -> Graphic:
    """One board drawing, every field (`layout_prims.read_node`). A group's or generator's `members`
    are uuids here; `assign_ids` turns them into the ids `layout.gen.py` writes."""
    from .layout_prims import SCHEMAS, read_node

    head = str(node[0])
    uid = next((str(x[1]) for x in node[1:] if isinstance(x, list) and x and x[0] == "uuid"), "?")
    vals = read_node(SCHEMAS[head], node, where=f"{head} {uid}")
    return Graphic(head, "", vals, source="gen")


@dataclass
class Board:
    """A KiCad-saved board as layout objects."""

    poses: list[Pose] = field(default_factory=list)
    copper: list[Copper] = field(default_factory=list)
    graphics: list[Graphic] = field(default_factory=list)
    filled_polygons: int = 0  # KiCad's fill blocks seen on the zones, which are not fields
    teardrops: int = 0  # KiCad's own teardrop zones seen and dropped (`is_teardrop_zone`), which are not fields either


# The top-level nodes of a board the layout language does not own: the header, the layer table, the
# setup, the nets, board properties and embedded files. Footprints are read for their pose only.
# Anything else at top level is `Unmapped`: a node nobody reads would be a node emit silently drops.
BOARD_OWNED = frozenset({"version", "generator", "generator_version", "general", "paper", "title_block", "layers", "setup", "property", "net", "embedded_fonts", "embedded_files"})


def decompile(text: str, *, unique: bool = True) -> Board:
    """Every footprint pose, every copper primitive and every drawing of a board KiCad saved, as
    Python objects.

    Raises `Unmapped` on any token of any layout primitive that no field carries, and on any
    top-level node that is neither a layout primitive, a footprint nor one of `BOARD_OWNED`; and
    `DuplicateUuid` on a uuid two layout objects carry, unless `unique=False` — which only the route
    stage's normalise passes, for the router's own intermediate board, where a locked core piece and
    the pattern's re-emitted copy of it share a geometry-derived uuid until `split_core` drops the
    copy. Every board a gate or fab reads is decompiled with the refusal on."""
    from . import trace
    from .layout_prims import HEADS

    trace.note("decompile", "")
    root = parse_tree(text)
    if not isinstance(root, list) or root[0] != "kicad_pcb":
        raise ValueError("not a KiCad board")
    out = Board()
    for node in root[1:]:
        if not isinstance(node, list):
            raise Unmapped(f"a bare atom {node!r} at the top of the board")
        head = node[0]
        if head == "footprint":
            out.poses.append(_pose(node))
        elif head in ("segment", "arc"):
            out.copper.append(_track(node, head))
        elif head == "via":
            out.copper.append(_via(node))
        elif head == "zone":
            z, n = _zone(node)
            out.filled_polygons += n
            if z is None:
                out.teardrops += 1
            else:
                out.copper.append(z)
        elif head in HEADS:
            out.graphics.append(_drawing(node))
        elif head not in BOARD_OWNED:
            raise Unmapped(f"({head}) at the top of the board: neither a layout primitive, a footprint, nor a board setting the layout language leaves to KiCad")
    # A uuid names one object (`DuplicateUuid`): every layout object's own uuid on the board, a
    # table's cells included, must be unique. A dimension's `gr_text` is not counted: KiCad gives
    # that text the dimension's own uuid (docs, "What KiCad rewrites on save"), so it is the same
    # object's.
    from .layout_prims import SCHEMAS, own_uuids

    seen: dict[str, list[str]] = {}
    for c in out.copper:
        if c.uuid:
            seen.setdefault(c.uuid, []).append(c.kind)
    for g in out.graphics:
        for u in own_uuids(SCHEMAS[g.kind], g.f):
            seen.setdefault(u, []).append(g.kind)
    for uid, kinds in seen.items():
        if len(kinds) > 1 and unique:
            raise DuplicateUuid(uid, kinds)
    return out


# ----------------------------------------------------------------------------------- matching


def _r(v: float) -> float:
    return round(float(v), 6)


def signature(c: Copper) -> tuple:
    """What a piece of copper *is*, for finding a core object again on the router's board: kind,
    net, layer(s) and geometry, rounded to KiCad's 1 nm. Not the uuid (the router's `pin_copper_ids`
    re-keys it) and not `locked` (the router's copy of a core object is locked whatever core says)."""
    if c.kind in ("seg", "arc"):
        pts = (c.a, c.mid, c.b) if c.kind == "arc" else (c.a, c.b)
        pts = tuple((_r(p[0]), _r(p[1])) for p in pts if p is not None)
        if c.kind == "seg":
            pts = tuple(sorted(pts))
        return (c.kind, c.net, c.layer, pts, _r(c.width or 0))
    if c.kind == "via":
        return (c.kind, c.net, tuple(c.layers or ()), (_r(c.at[0]), _r(c.at[1])), _r(c.size or 0), _r(c.drill or 0))
    return (c.kind, c.net, c.layer, tuple(c.layers or ()), _r_pts(c.points))


def _r_pts(entries) -> tuple:
    from .layout_prims import is_arc

    return tuple(tuple((_r(x), _r(y)) for x, y in e) if is_arc(e) else (_r(e[0]), _r(e[1])) for e in entries)


def split_core(copper: list[Copper], core: list[Copper]) -> tuple[list[Copper], list[Copper], list[Copper]]:
    """(the board's copper that is not core, the core objects the board does not have, the board
    objects that are a core object's copy).

    **Every** board object whose signature is a core object's is that core object: the router's own
    copy of the lock, and any piece a pattern wrote again on top of it (a pattern that finds its own
    piece already locked on its input board re-emits it, and that second copy would otherwise ship
    as a duplicate segment). They are all dropped from the generated half; the core line is emitted
    once."""
    want = {signature(c) for c in core}
    rest: list[Copper] = []
    copies: list[Copper] = []
    for c in copper:
        (copies if signature(c) in want else rest).append(c)
    have = {signature(c) for c in copies}
    missing = [c for c in core if signature(c) not in have]
    return rest, missing, copies


# ----------------------------------------------------------------------------------- writing


def _py_str(v: str) -> str:
    """A string as a Python literal Python reads back to the same string: ASCII, KiCad's escapes for
    the control characters, `\\uXXXX` for the rest of the BMP and **`\\UXXXXXXXX` for a character
    beyond it**. `json.dumps` wrote an astral character (a net named `LED😀`) as a UTF-16 surrogate
    pair, `\\ud83d\\ude00`, which Python reads as two lone surrogates, so `layout.gen.py` did not
    load back equal to the board and emit refused its own output (fourth review, C3)."""
    out = ['"']
    for ch in v:
        o = ord(ch)
        if ch == '"' or ch == "\\":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\x{o:02x}")
        elif o < 0x7F:
            out.append(ch)
        elif o <= 0xFFFF:
            out.append(f"\\u{o:04x}")
        else:
            out.append(f"\\U{o:08x}")
    out.append('"')
    return "".join(out)


def _py(v) -> str:
    if v is None or isinstance(v, bool):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(0.0 if v == 0 else v)
    if isinstance(v, str):
        return _py_str(v)
    if isinstance(v, tuple):
        inner = ", ".join(_py(x) for x in v)
        return f"({inner},)" if len(v) == 1 else f"({inner})"
    if isinstance(v, list):
        return "[" + ", ".join(_py(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_py_str(str(k))}: {_py(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"no Python literal for {v!r}")


def _value_for(attr: str, obj) -> object:
    v = getattr(obj, attr)
    if attr in ("points",):
        return list(v)
    if attr == "holes":
        return [list(h) for h in v]
    return v


def line_of(obj: Copper | Pose | Graphic) -> str:
    """One object as the one line of Python that makes it, every field explicit."""
    if isinstance(obj, Graphic):
        return _drawing_line(obj)
    name = "Place" if isinstance(obj, Pose) else KIND_OF[obj.kind]
    args = []
    for kw, attr, _tok in FIELDS[name]:
        v = _py(_value_for(attr, obj))
        args.append(v if kw is None else f"{kw}={v}")
    return f"{name}(" + ", ".join(args) + ")"


def _drawing_line(g: Graphic) -> str:
    from .layout_prims import SCHEMAS

    schema = SCHEMAS[g.kind]
    args = [_py(g.f[kw]) for kw in schema.positional]
    args += [f"{f.kw}={_py(g.f[f.kw])}" for f in schema.fields() if f.kw not in schema.positional]
    args.append(f"id={_py(g.id)}")
    return f"{schema.name}(" + ", ".join(args) + ")"


class IdTaken(ValueError):
    """A generated object's id is already a core object's: `layout_job` names the core line."""

    def __init__(self, id: str, kind: str, uuid: str):
        super().__init__(f"id {id!r} is taken by a core line and is the id of {kind} {uuid}")
        self.id, self.kind, self.uuid = id, kind, uuid


def assign_ids(copper: list[Copper], taken: set[str], graphics: list[Graphic] = (), uuid_ids: dict[str, str] | None = None) -> None:
    """A stable, readable `id` per generated object: its kind and the head of its uuid, longer only
    where two generated objects would collide. Derived from the uuid, and the uuid of a router's
    object is derived from its geometry (`layout_job`), so the same copper gives the same id and
    locking one object renumbers nothing else. A core id (`taken`) that equals a generated object's
    id is `IdTaken`, never a quiet rename of the generated object (the second review's m8).

    A group's or generator's `members` come out of the decompiler as uuids; here they become ids.
    `uuid_ids` maps the uuids of objects that are not in these lists (core objects) to their ids."""
    used: set[str] = set()
    objs: list = sorted(copper, key=lambda c: (c.kind, c.uuid or "")) + sorted(graphics, key=lambda g: (g.kind, g.uuid or ""))
    for c in objs:
        kind = c.kind if isinstance(c, Copper) else c.kind.replace("gr_", "")
        for n in (8, 13, 18, 23, 36):
            cand = f"{kind}-{(c.uuid or '')[:n]}"
            if cand in taken:
                raise IdTaken(cand, c.kind, c.uuid or "")
            if cand not in used:
                break
        else:  # pragma: no cover - a uuid collision, refused upstream
            raise ValueError(f"no free id for {c.kind} {c.uuid}")
        c.id = cand
        used.add(cand)
    by_uuid = dict(uuid_ids or {})
    by_uuid.update({c.uuid: c.id for c in objs if c.uuid})
    for g in graphics:
        if "members" in g.f:
            missing = [m for m in g.f["members"] if m not in by_uuid]
            if missing:
                raise Unmapped(f"{g.kind} {g.uuid}: member {missing[0]} is not a layout object (a footprint or a pad cannot be a member here)")
            g.f["members"] = tuple(by_uuid[m] for m in g.f["members"])


HEADER = """\
# layout.gen.py - written by `pcbc build`'s generator for {board}: the placer and the router. BUILD OUTPUT.
# Never edit this file and never read it as design input: it is rewritten on every build.
# One line is one KiCad primitive, every field explicit (docs/layout-properties.md has the table).
# To lock an object, copy its line verbatim into layout.core.py next to board.py and rebuild: every
# copper and drawing line here passed KiCad's DRC at the board's hard minimums, which is what
# layout.core.py is held to. Three kinds of line cannot be locked: a Place (a part is placed in
# board.py), a Group named "pcbc:<reason>:<owner>" (a piece's role, the build's own, rewritten
# from the board every build) and the Edge.Cuts outline (Board(width, height) in board.py).
# {n_place} Place, {n_pour} Pour, {n_via} Via, {n_seg} Seg, {n_arc} Arc, {n_draw} drawing(s).
"""


def _sort_key(c: Copper) -> tuple:
    order = {"pour": 0, "via": 1, "seg": 2, "arc": 3}[c.kind]
    return (order, c.net or "", tuple("" if x is None else x for x in signature(c)[2:]), c.uuid or "")


def _drawing_key(g: Graphic) -> tuple:
    from .layout_prims import HEADS

    return (HEADS.index(g.kind), g.uuid or "", g.id)


def gen_source(poses: list[Pose], copper: list[Copper], *, board: str, graphics: list[Graphic] = ()) -> str:
    """The text of `layout.gen.py`: a pure function of the objects, so the same board gives the same
    bytes and a decompile of the emitted board gives this file back."""
    counts = Counter(c.kind for c in copper)
    head = HEADER.format(board=board, n_place=len(poses), n_pour=counts["pour"], n_via=counts["via"], n_seg=counts["seg"], n_arc=counts["arc"], n_draw=len(graphics))
    lines = [head, ""]
    lines += [line_of(p) for p in sorted(poses, key=lambda p: p.ref)]
    lines.append("")
    lines += [line_of(c) for c in sorted(copper, key=_sort_key)]
    if graphics:
        lines.append("")
        lines += [line_of(g) for g in sorted(graphics, key=_drawing_key)]
    return "\n".join(lines) + "\n"


def write_gen(path: Path, poses: list[Pose], copper: list[Copper], *, board: str, graphics: list[Graphic] = ()) -> str:
    text = gen_source(poses, copper, board=board, graphics=graphics)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text)
    return text


# ----------------------------------------------------------------------------------- the census map

_TRACK = {
    "start": "a (positional)",
    "end": "b (positional)",
    "width": "width",
    "locked": "locked",
    "layer": "layer",
    "layers": "layer + solder_mask=True",
    "solder_mask_margin": "solder_mask_margin",
    "net": "net (positional)",
    "uuid": "uuid",
}
_SIDED = {f"{b}{s}": f"{b.split('/')[0]}[{s[1:]!r}]" if s else b for b in ("tenting", "covering", "plugging") for s in ("", "/front", "/back")}
_TEARDROPS = ("best_length_ratio", "max_length", "best_width_ratio", "max_width", "curved_edges", "filter_ratio", "enabled", "allow_two_segments", "prefer_zone_connections")
CENSUS: dict[str, dict[str, str]] = {
    "segment": dict(_TRACK),
    "arc": {**_TRACK, "mid": "mid (positional)"},
    "via": {
        "(atom)": "kind (blind | buried | micro; none = through)",
        "at": "at (positional)",
        "size": "size",
        "drill": "drill",
        "layers": "layers",
        "locked": "locked",
        "free": "free",
        "remove_unused_layers": "remove_unused_layers",
        "keep_end_layers": "keep_end_layers",
        "zone_layer_connections": "zone_layer_connections",
        **_SIDED,
        "capping": "capping",
        "filling": "filling",
        "padstack": "padstack",
        "padstack/mode": "padstack[0]",
        "padstack/layer": "padstack[1][i][0]",
        "padstack/layer/size": "padstack[1][i][1]",
        **{f"{d}{s}": f"{d}{i}" for d in ("backdrill", "tertiary_drill") for s, i in (("", ""), ("/size", "[0]"), ("/layers", "[1]"))},
        **{f"{d}{s}": f"{d}{i}" for d in ("front_post_machining", "back_post_machining") for s, i in (("", "['mode']"), ("/size", "['size']"), ("/depth", "['depth']"), ("/angle", "['angle']"))},
        "teardrops": "teardrops",
        **{f"teardrops/{k}": f"teardrops[{k!r}]" for k in _TEARDROPS},
        "net": "net (positional)",
        "uuid": "uuid",
    },
    "zone": {
        "net": "net (positional; None = no token)",
        "locked": "locked",
        "layer": "layer",
        "layers": "layers",
        "uuid": "uuid",
        "name": "name",
        "hatch": "hatch",
        "priority": "priority",
        "attr": "teardrop_type",
        "attr/teardrop": "teardrop_type",
        "attr/teardrop/type": "teardrop_type",
        "connect_pads": "connect (the atom: none = thermal, yes = solid, no = none, thru_hole_only)",
        "connect_pads/clearance": "clearance",
        "min_thickness": "min_thickness",
        "filled_areas_thickness": "filled_areas_thickness",
        "keepout": "keepout",
        **{f"keepout/{k}": f"keepout[{k!r}]" for k in ("tracks", "vias", "pads", "copperpour", "footprints")},
        "placement": "placement",
        **{f"placement/{k}": f"placement[{k!r}]" for k in ("enabled", "sheetname", "component_class", "group")},
        "fill": "filled (the `yes` atom)",
        "fill/mode": "fill_mode",
        **{f"fill/{k}": k for k in ("thermal_gap", "thermal_bridge_width", "smoothing", "radius", "island_removal_mode", "island_area_min", "hatch_thickness", "hatch_gap", "hatch_orientation", "hatch_smoothing_level", "hatch_smoothing_value", "hatch_border_algorithm", "hatch_min_hole_area")},
        "property": "hatch_position (one (property) per layer)",
        "property/layer": "hatch_position key (the layer)",
        "property/hatch_position": "hatch_position[layer]",
        "property/hatch_position/xy": "hatch_position[layer] = (x, y)",
        "polygon": "points (first) / holes (every later one)",
        "polygon/pts": "points / holes[i]",
        "polygon/pts/xy": "points[i] / holes[i][j] (a point)",
        "polygon/pts/arc": "points[i] / holes[i][j] (an arc entry: ((sx, sy), (mx, my), (ex, ey)))",
        "polygon/pts/arc/start": "points[i][0] / holes[i][j][0]",
        "polygon/pts/arc/mid": "points[i][1] / holes[i][j][1]",
        "polygon/pts/arc/end": "points[i][2] / holes[i][j][2]",
        "filled_polygon": "not a field: KiCad's fill, regenerated by `kicad-cli pcb drc --refill-zones` from the fields above",
    },
    "footprint": {
        "at": "Place at + rot",
        "layer": "Place layer",
        "locked": "Place locked",
    },
}

def _drawings_census() -> dict[str, dict[str, str]]:
    from .layout_prims import census_map

    return census_map()


CENSUS.update(_drawings_census())
# A footprint's other children are the `.kicad_mod`'s and `board.py`'s (pads, drawings, properties,
# the 3D model): real KiCad fields, but not layout fields, and `Place` does not carry them.
FOOTPRINT_OWNED = frozenset(
    {"uuid", "property", "attr", "descr", "tags", "path", "sheetname", "sheetfile", "pad", "fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve", "fp_text", "fp_text_box", "model", "zone", "group", "embedded_fonts", "embedded_files", "duplicate_pad_numbers_are_jumpers", "jumper_pad_groups", "net_tie_pad_groups", "solder_mask_margin", "solder_paste_margin", "solder_paste_margin_ratio", "clearance", "zone_connect", "private_layers", "component_classes", "points", "dimension", "image", "barcode", "table", "units", "net_tie_pad_groups", "teardrops", "placed"}
)


def _drawing_heads() -> tuple[str, ...]:
    from .layout_prims import HEADS

    return HEADS


def census(text: str) -> dict[str, dict[str, int]]:
    """Every token path under every primitive of a board, counted: `{"zone": {"fill/thermal_gap": 23}}`.

    A footprint contributes its direct children only (the rest of it is the `.kicad_mod`'s); a
    `filled_polygon` contributes its own name only (it is fill, not a field)."""
    root = parse_tree(text)
    out: dict[str, Counter] = {}

    def walk(node: list, pre: str, bag: Counter) -> None:
        for x in node[1:]:
            if not isinstance(x, list):
                continue
            path = f"{pre}/{x[0]}" if pre else x[0]
            bag[path] += 1
            if x[0] in ("filled_polygon", "render_cache"):
                continue
            walk(x, path, bag)

    for node in root[1:]:
        if not isinstance(node, list):
            continue
        head = node[0]
        if head == "footprint":
            bag = out.setdefault(head, Counter())
            for x in node[1:]:
                if isinstance(x, list):
                    bag[x[0]] += 1
        elif head in ("segment", "arc", "via", "zone") or head in _drawing_heads():
            bag = out.setdefault(head, Counter())
            if any(not isinstance(x, list) for x in node[1:]):
                bag["(atom)"] += 1
            walk(node, "", bag)
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}


def unmapped(counts: dict[str, dict[str, int]]) -> list[str]:
    """Every census path no Python field carries: the bar is an empty list."""
    bad = []
    for prim, paths in counts.items():
        known = CENSUS.get(prim)
        for path in paths:
            if prim == "footprint" and path in FOOTPRINT_OWNED:
                continue
            if prim == "generated" and known is not None and "*" in known and path.split("/")[0] not in ("members",):
                continue  # a generator's own properties: every key lands in `props`
            if known is None or path not in known:
                bad.append(f"{prim}/{path}")
    return bad
