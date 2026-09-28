"""The route stage, native: place and route in Python, write `layout.gen.py`, emit the board from it.

No KiCad board file is an intermediate (`docs/direction.md` §1): the first `.kicad_pcb` this stage
writes is the one `emit` writes. The order:

0. **clean** — every output of this stage from an earlier build (`layout.gen.py`, the emitted board,
   its sidecars, the unrouted problem files) is unlinked first, so a refused build never leaves an
   older board behind to be mistaken for this one.
1. **place** — `place_native.place`: the footprints from their `.kicad_mod`, posed from `board.py`'s
   `Place()`, in memory; the outline, slots, keepouts and rule areas as layout objects. The place-only
   board is rendered **in memory** and checked (`check.check_text`) before anything is routed.
2. **core** — `layout.core.py` is loaded (`language.load_layout`, source "core"): layout primitives
   only. Its copper is checked before any routing: KiCad's own reading of every line
   (`core_probe.round_trip`, M1), the board's hard minimums (`layout_check`), and the compiled
   clearances in the router's own scene (`route_scene.clashes`); a refusal names `layout.core.py:<line>`.
3. **route** — `route_native.route_stage`, **in passes** (`LOCK_PASSES`): the first with no core copper
   in the scene; every core line the generated copper then reproduces field for field is **adopted**
   (it is a piece the build draws anyway; it keeps its id and uuid). The lines not reproduced are the
   real locks: they enter the scene as locked items and the route runs again; after `LOCK_PASSES`
   passes every core line is locked. A net the router cannot finish is left open and reported as a
   move; nothing else is refused here.
4. **objects** — every piece becomes its `Seg`/`Via` (`route_native.to_objects`) with a
   `pcbc:<reason>:<owner>` group, beside the plane `Pour`s and the place-level objects.
5. **gen** — `routed/layout.gen.py` is written, loaded back (`load_layout`, source "gen") and must be
   field for field the objects it was written from.
6. **emit** — `merge(core, gen)` is rendered onto the in-memory footprint board
   (`layout_emit.emit_board`) to `routed/layout.kicad_pcb`; the emitted text is decompiled once and
   must give back exactly those objects. That file is the one board every later gate, and fab, reads.

Nothing here labels copper "verified": `build.py` runs `netcheck.check_copper` (KiCad) on the emitted
board and only that earns the word.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .gen import IdTaken, assign_ids, decompile, write_gen
from .language import load_layout
from .layout_check import check_layout
from .layout_emit import emit_board, own_uuids, stamp, uuid_of
from .layout_prims import flat_points
from .compile import compile_design
from .model import Copper, Design, Graphic
from .netcheck import KicadMissing
from .project import write_sidecars

CORE_NAME = "layout.core.py"
GEN_NAME = "layout.gen.py"
ROLE_PREFIX = "pcbc:"
UNROUTED_DIR = "unrouted"
# Route passes before every core line is locked outright: pass 1 locks nothing, pass 2 the lines pass 1
# did not reproduce, pass 3 (the cap) everything (`layout_job`).
LOCK_PASSES = 2


def core_path(board: Path) -> Path:
    """`layout.core.py` is a source file: it sits next to `board.py`."""
    return Path(board).parent / CORE_NAME


def gen_path(out: Path) -> Path:
    """`layout.gen.py` is build output: it sits next to the board emitted from it."""
    return Path(out).parent / GEN_NAME


def stage_outputs(out: Path) -> list[Path]:
    """Every file this stage writes, which an earlier build may have left: unlinked before it starts."""
    out = Path(out)
    return [gen_path(out), out, *(out.with_suffix(x) for x in (".kicad_pro", ".kicad_dru", ".kicad_prl"))]


def feet_pads(design: Design, feet) -> tuple[list, tuple[str, ...]]:
    """Every pad of the posed parts (`Placement.feet`) in world coordinates with its copper layers, and
    the copper layers: what the layout checks read before anything is emitted."""
    from .compile import compile_design
    from .foot_native import pad_geoms_of
    from .route_scene import _layer_list
    from .stackup import get_stackup

    layers = tuple(_layer_list(get_stackup(compile_design(design).stackup)))
    out = []
    for pf in sorted(feet, key=lambda f: f.ref):
        out.extend(pad_geoms_of(pf, layers))
    return out, layers


def board_pads(design: Design, text: str) -> tuple[list, tuple[str, ...]]:
    """Every pad on an emitted board in world coordinates with its copper layers, and the copper layers
    (a checker's read: `build.tap_stubs`)."""
    from .compile import compile_design
    from .layout import footprints_by_ref
    from .pads import pad_geoms
    from .route_scene import _layer_list
    from .sexp import footprint_at
    from .stackup import get_stackup

    layers = tuple(_layer_list(get_stackup(compile_design(design).stackup)))
    nets = {inst.ref: dict(inst.pins) for inst in design.instances}
    out = []
    for ref, block in sorted(footprints_by_ref(text).items()):
        at = footprint_at(block)
        if at is None:
            continue
        # No `side`: the same call `route_scene.build_scene` makes, so both read a pad identically.
        out.extend(pad_geoms(block, at, ref=ref, nets=nets.get(ref), layers=layers))
    return out, layers


def _copper_layers(design: Design) -> tuple[str, ...]:
    """The copper layers of the board's stackup, the only layers core copper may name."""
    from .compile import compile_design
    from .route_scene import _layer_list
    from .stackup import get_stackup

    return tuple(_layer_list(get_stackup(compile_design(design).stackup)))


# KiCad 10 accepts a drawing on any of the 45 user layers whether or not the board's layer table lists
# one (measured: `User.45` is kept, `User.46` fails the board's load, fifth review and `fix6/facts.py`).
USER_LAYERS = tuple(f"User.{n}" for n in range(1, 46))


def board_layer_names(design: Design, placed_text: str | None = None) -> tuple[str, ...]:
    """Every layer name KiCad reads on this board: the board's own `(layers ...)` table — the placed
    board's when there is one, else the one the seed writes (`seed._layers`) — and the user layers."""
    import re

    table = None
    if placed_text:
        m = re.search(r"\n\t\(layers\n(.*?)\n\t\)", placed_text, flags=re.S)
        table = m.group(1) if m else None
    if table is None:
        from .seed import _layers

        table = _layers(design.board.layers if design.board is not None else 2)
    names = tuple(re.findall(r'\(\d+\s+"([^"]+)"', table))
    return names + tuple(u for u in USER_LAYERS if u not in names)


def _foreign(design: Design, placed, name: str) -> dict[str, str]:
    """The uuids a core line may not take: from the Python model when `placed` is the place stage's
    `Placement` (the build: `place_native.foreign_uuids`, no board text parsed), from a board text when
    one is handed in (a file on disk), none without either."""
    if placed is None:
        return {}
    if isinstance(placed, str):
        return board_uuids(placed) if placed.strip() else {}
    from .place_native import foreign_uuids

    return foreign_uuids(design, placed, name=name)


def board_uuids(text: str) -> dict[str, str]:
    """`{uuid: what carries it}` for every uuid on the in-memory footprint board (`place_native`): a
    footprint and everything inside it by the footprint's reference, the outline and any other node
    by its head. A core line may not take one (fifth review: a core `Line` given the outline's uuid
    replaced the outline, and one given R1's uuid was blamed on `layout.gen.py`)."""
    from .sexp import Q, parse_tree

    out: dict[str, str] = {}

    def walk(n, owner: str) -> None:
        if not isinstance(n, list) or not n:
            return
        if n[0] == "uuid" and len(n) > 1:
            out.setdefault(str(n[1]), owner)
            return
        for x in n[1:]:
            walk(x, owner)

    for n in parse_tree(text)[1:] if text.strip() else ():
        if not isinstance(n, list) or not n:
            continue
        head = str(n[0])
        if head == "footprint":
            ref = next((str(x[2]) for x in n[1:] if isinstance(x, list) and len(x) > 2 and x[0] == "property" and x[1] == Q("Reference")), "?")
            owner = f"footprint {ref} (placed from board.py)"
        elif head == "gr_rect" and any(isinstance(x, list) and x[:1] == ["layer"] and len(x) > 1 and str(x[1]) == "Edge.Cuts" for x in n):
            owner = "the board outline (Board() in board.py)"
        else:
            owner = f"the board's ({head})"
        walk(n, owner)
    return out


def _strings(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, (list, tuple)):
        return [s for x in v for s in _strings(x)]
    return []


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        depth += 1 if ch == "(" else -1 if ch == ")" else 0
        if depth < 0:
            return False
    return depth == 0


def drawing_layers(g: Graphic) -> list[str]:
    """Every layer a drawing names: its own `layer`/`layers`, its table cells', its dimension text's."""
    f = g.f
    out = [f["layer"]] if f.get("layer") else []
    out += list(f.get("layers") or ())
    for cell in f.get("cells") or ():
        if cell.get("layer"):
            out.append(cell["layer"])
    if isinstance(f.get("gr_text"), dict) and f["gr_text"].get("layer"):
        out.append(f["gr_text"]["layer"])
    return out


def on_copper(g: Graphic, copper_layers) -> bool:
    """A drawing that is copper (on a copper layer): the router has to see it."""
    return any(la in copper_layers for la in drawing_layers(g))


def _expand_layers(c: Copper, cu_layers) -> None:
    """A pour's `*.Cu` / `F&B.Cu` spelled out against the board's copper layers, in KiCad's order:
    KiCad writes such a zone's layers one by one (measured, `l_pour_star`, `l_pour_fb`)."""
    from .layout_prims import lset_order

    wide: list[str] = []
    for la in c.layers or ():
        wide += list(cu_layers) if la == "*.Cu" else ["F.Cu", "B.Cu"] if la == "F&B.Cu" else [la]
    if len(set(wide)) != len(wide):
        raise ValueError(f"{c.where or CORE_NAME}: layers={c.layers!r} names a copper layer twice once '*.Cu'/'F&B.Cu' is spelled out")
    if len(wide) == 1:
        c.layer, c.layers = wide[0], None
    else:
        c.layer, c.layers = None, lset_order(wide)


def validate_core(design: Design, copper: list[Copper], graphics: list[Graphic], *, layers, copper_layers, foreign: dict[str, str], where_default: str = CORE_NAME, name: str = "board") -> None:
    """What a core line may not say given **this board** (the constructors check each line alone):
    a layer the board does not have, on any field that names one; a custom padstack without every
    copper layer below F.Cu; a pour's `*.Cu`/`F&B.Cu` spelled out as KiCad writes it; a tuning
    pattern on another layer than its members; a uuid the placed board already carries or another
    core object derives. Raises `ValueError` naming the
    line (fifth review, C3: each of these routed the whole board and then died in a `kicad-cli`
    traceback or a refusal naming nothing)."""
    from .layout_prims import SCHEMAS, all_uuids

    cu = tuple(copper_layers)
    here = lambda o: o.where or where_default  # noqa: E731

    def need(o, la: str, what: str, pool, kind: str = "a layer") -> None:
        if la not in pool and la in tuple(layers):
            # A layer the board has, but not the kind this field takes: the reason is pcbc's rule, not
            # KiCad's (sixth review: a KiCad-saved zone on F.SilkS was refused as "an undefined layer",
            # which KiCad had just saved).
            raise ValueError(f"{here(o)}: {what} names {la!r}, which is a layer of this board but not {kind}; in layout.core.py {what} is copper by design (docs/direction.md §2) — a zone or rule area on a non-copper layer is not a core primitive: leave the line out")
        if la not in pool:
            shown = ", ".join(pool) if len(pool) <= 12 else ", ".join(list(pool)[:30]) + ", User.1 .. User.45"
            raise ValueError(f"{here(o)}: {what} names {la!r}, which is not {kind} of this board ({shown}); KiCad refuses a board with an item on an undefined layer")

    by_id = {c.id: c for c in copper}
    for c in copper:
        if c.kind == "via":
            for la in c.zone_layer_connections:
                need(c, la, "zone_layer_connections", cu, "a copper layer")
            for tag in ("backdrill", "tertiary_drill"):
                d = getattr(c, tag)
                if d is not None:
                    for la in d[1]:
                        need(c, la, tag, cu, "a copper layer")
            if c.padstack is not None and c.padstack[0] == "custom":
                want = [la for la in cu if la != "F.Cu"]
                want = sorted(want, key=lambda n: (n != "B.Cu", n))  # membership only; order was set at load
                have = [la for la, _ in c.padstack[1]]
                for la in have:
                    need(c, la, "padstack", cu, "a copper layer")
                missing = sorted(set(want) - set(have))
                if missing:
                    raise ValueError(f"{here(c)}: a custom padstack gives a size for every copper layer below F.Cu, and KiCad's save fills in {', '.join(missing)} when it is left out; write them")
        if c.kind == "pour":
            if c.layers and any(la in ("*.Cu", "F&B.Cu") for la in c.layers):
                _expand_layers(c, cu)
            for la in (c.layers or ()) + ((c.layer,) if c.layer else ()):
                need(c, la, "a pour", cu, "a copper layer")
            for la in (c.hatch_position or {}):
                need(c, la, "hatch_position", tuple(layers))
    for g in graphics:
        for la in drawing_layers(g):
            need(g, la, f"{g.kind}", tuple(layers))
        if g.kind == "generated":
            lays = {by_id[m].layer for m in g.f.get("members", ()) if m in by_id and by_id[m].kind in ("seg", "arc")}
            if len(lays) == 1 and g.f.get("layer") not in lays:
                (la,) = lays
                raise ValueError(f"{here(g)}: a tuning pattern is on its members' layer, and KiCad's save moves this one from {g.f.get('layer')!r} to {la!r}: write layer={la!r}")
    # uuids: one object each, and never one the placed board already carries
    owner: dict[str, str] = {}
    for o in [*copper, *graphics]:
        from .layout_emit import uuid_of

        got = [uuid_of(o, name)] if isinstance(o, Copper) else list(dict.fromkeys([uuid_of(o, name), *all_uuids(SCHEMAS[o.kind], o.f)]))
        for u in got:
            who = f"{here(o)} ({o.kind} {o.id!r})"
            if u in foreign:
                raise ValueError(f"{who}: uuid {u} is {foreign[u]}'s; a uuid names one object and the build owns that one — drop uuid= from this line (one is derived from its id)")
            if u in owner and owner[u] != who:
                raise ValueError(f"{who}: uuid {u} is already {owner[u]}'s (derived from an id, or written); give one of them another uuid or id")
            owner[u] = who


def load_core(design: Design, board: Path, placed_text=None, *, name: str | None = None) -> tuple[list[Copper], list[Graphic]]:
    """Load `layout.core.py` into `design` and return its copper and drawings. Raises `ValueError`
    naming `layout.core.py:<line>` on anything the file may not say."""
    path = core_path(board)
    before_cu, before_gr = len(design.copper), len(design.graphics)
    load_layout(path, design, source="core")
    copper = [c for c in design.copper[before_cu:] if c.source == "core"]
    graphics = [g for g in design.graphics[before_gr:] if g.source == "core"]
    ids = {o.id for o in [*copper, *graphics]}
    # Core copper names a net of board.py, or no net at all (`net=None`: KiCad's own no-net — `(net "")`
    # on a track or via, no token on a zone — for a floating spreader, a coupon, a rule area;
    # `docs/direction.md` §2). A net board.py does not have is a conductor KiCad's netlist check never
    # asks about (it compares pad bindings), and `Seg(None, ...)` was a `(net "None")` on the shipped
    # board; both built "verified" until the second review. Refused at load, naming the line.
    nets = set(design.nets)
    cu_layers = _copper_layers(design)
    w, h = design.board.size_mm if design.board is not None else (0.0, 0.0)
    for c in copper:
        if c.kind == "pour" and c.layers and any(la in ("*.Cu", "F&B.Cu") for la in c.layers):
            _expand_layers(c, cu_layers)
        if c.net and c.net not in nets:
            raise ValueError(f"{c.where or CORE_NAME}: {c.kind} on {c.net!r}, which is not a net of board.py ({', '.join(sorted(nets)) or 'none'}); copper names a net board.py declares, or net=None for copper on no net")
        # Copper lives on a copper layer of the stackup. A `Pour(None, layer="Edge.Cuts")` built
        # "verified" and put a filled region into the outline Gerber (third review T1): the outline has
        # one home, Board(width, height) in board.py.
        on = [la for la in ((c.layers or ()) if c.kind in ("via", "pour") and c.layers else ()) if la] or ([c.layer] if c.layer else [])
        off = [la for la in on if la not in cu_layers]
        if off:
            hint = f" The outline is Board({w:g}, {h:g}) in board.py, and a drawing goes on a Line/Rect/... line, not a copper one." if "Edge.Cuts" in off else ""
            raise ValueError(f"{c.where or CORE_NAME}: {c.kind} on {off[0]!r}, which is not a copper layer of this stackup ({', '.join(cu_layers)}); copper is written on a copper layer only.{hint}")
        # A field the emitter needs and the build cannot fill: a via on no net has no class to take
        # its size and drill from (third review T3: this escaped as a traceback from `render_one`).
        if c.kind == "via" and not c.net and (c.size is None or c.drill is None):
            raise ValueError(f"{c.where or CORE_NAME}: a via on no net needs size= and drill= (there is no net class to take them from)")
        if c.kind in ("seg", "arc") and not c.net and c.width is None:
            raise ValueError(f"{c.where or CORE_NAME}: a {c.kind} on no net needs width= (there is no net class to take it from)")
    for g in graphics:
        if g.f.get("net") and g.f["net"] not in nets:
            raise ValueError(f"{g.where or CORE_NAME}: {g.kind} on net {g.f['net']!r}, which is not a net of board.py")
        if g.f.get("layer") == "Edge.Cuts" or "Edge.Cuts" in (g.f.get("layers") or ()):
            # Default chosen for this round, reversible: the outline has one home, Board(width, height)
            # in board.py, which the place stage reads; a core Edge.Cuts drawing would give it a second
            # one the placer never sees (a cutout under a part).
            raise ValueError(f"{g.where or CORE_NAME}: {g.kind} on Edge.Cuts; the outline is Board(width, height) in board.py and the place stage reads only that. Move the outline change there")
    for g in graphics:
        # A core group names core objects: a gen id is build output and changes when the board does.
        stray = [m for m in g.f.get("members", ()) if m not in ids]
        if stray:
            raise ValueError(f"{g.where or CORE_NAME}: {g.kind} {g.id!r} names {stray[0]!r}, which is not an object of {CORE_NAME}; a core group may only hold core objects")
        if g.kind == "group" and str(g.f.get("name", "")).startswith(ROLE_PREFIX):
            raise ValueError(f"{g.where or CORE_NAME}: group names starting {ROLE_PREFIX!r} are the build's own (a piece's role); name this group something else")
    validate_core(
        design,
        copper,
        graphics,
        layers=board_layer_names(design, placed_text if isinstance(placed_text, str) else None),
        copper_layers=cu_layers,
        foreign=_foreign(design, placed_text, name or Path(board).stem),
        name=name or Path(board).stem,
    )
    return copper, graphics


def _clearance_clashes(design: Design, feet, core: list[Copper]) -> list[str]:
    """A core segment, arc or via closer to a pad, hole, copper or the edge of another net than the
    compiled rules allow, judged in the scene pcbc's own patterns are judged in (`route_scene`). An
    overlap is a short; a near miss is refused too, with the air it has and the air it needs — a
    lock the router cannot move would otherwise route a whole board and then fail KiCad's DRC with
    no line named."""
    from .compile import compile_design
    from .route_emit import seg_piece, via_piece
    from .route_scene import build_scene, clashes

    pieces = []
    for c in core:
        if c.kind == "seg" and c.a and c.b and c.width and c.a != c.b:
            pieces.append((c, [seg_piece(c.net or "", "core", c.layer, c.a, c.b, c.width)]))
        elif c.kind == "arc" and c.a and c.mid and c.b and c.width:
            pieces.append((c, [seg_piece(c.net or "", "core", c.layer, c.a, c.mid, c.width), seg_piece(c.net or "", "core", c.layer, c.mid, c.b, c.width)]))
        elif c.kind == "via" and c.at and c.size and c.drill:
            pieces.append((c, [via_piece(c.net or "", "core", c.at, c.size, c.drill, layers=tuple(c.layers or ("F.Cu", "B.Cu")))]))
    if not pieces:
        return []
    job = compile_design(design)
    if job.constraints is None:
        return []
    scene = build_scene(design, job, job.constraints, feet)
    out = []
    for c, ps in pieces:
        for k in clashes(scene, ps, c.net or ""):
            if k.rule == "mask":
                continue  # advisory in R2 (A.4 rule 4); KiCad's solder_mask_bridge gates it
            kind = "a short" if k.have <= 1e-9 and k.rule == "copper" else f"{k.have:g} mm of air where {k.need:g} mm is needed ({k.why})"
            out.append(f"{c.where or c.id}: {c.kind} on {c.net} against {k.item.label()} at {k.at[0]:.4g},{k.at[1]:.4g}: {k.rule}, {kind}")
            break
    return out


def _unfillable(c: Copper) -> str | None:
    """Why a stamped core object still cannot be emitted, or None: the field `render_one` would refuse."""
    if c.kind in ("seg", "arc"):
        if c.a is None or c.b is None or (c.kind == "arc" and c.mid is None):
            return f"a {c.kind} needs its two ends{' and mid=' if c.kind == 'arc' else ''}"
        if c.width is None:
            return f"a {c.kind} on {c.net!r} needs width= (the net has no compiled class to take it from)"
    if c.kind == "via":
        if c.at is None:
            return "a via needs its position"
        if c.size is None or c.drill is None:
            return f"a via on {c.net!r} needs size= and drill= (the net has no compiled class to take them from)"
    if c.kind == "pour" and not c.points:
        return "a pour needs an outline (points=), and this board has no Board() to take one from"
    return None


def _short(v) -> str:
    s = repr(v)
    return s if len(s) <= 120 else s[:117] + "..."


def layout_uuids(board) -> frozenset[str]:
    """Every uuid a layout object of `board` (a `gen.Board`) carries, nested cells and texts included:
    the ids `pin_all_uuids` must keep, because each is a field of a Python object."""
    from .layout_prims import SCHEMAS, all_uuids

    got = {c.uuid for c in board.copper if c.uuid}
    for g in board.graphics:
        got |= set(all_uuids(SCHEMAS[g.kind], g.f))
    return frozenset(got)


def adopt_key(c: Copper) -> tuple:
    """A copper object's KiCad fields but its handles (`id`, `uuid`): what "a piece the build draws"
    is compared by for adoption (`layout_job`, fifth review). A segment's or arc's two ends compare
    as a set: the same copper written end to start is the same piece (a gen segment locked with its
    ends swapped must still be adopted, not become a new lock). `signature` already sorts them; this is
    the same rule for the full-field key."""
    key = [(f, getattr(c, f)) for f in Copper.__dataclass_fields__ if f not in ("source", "where", "comment", "id", "uuid")]
    if c.kind in ("seg", "arc") and c.a is not None and c.b is not None:
        ends = tuple(sorted((tuple(c.a), tuple(c.b))))
        key = [("ends", ends) if f == "a" else (f, v) for f, v in key if f != "b"]
    return tuple(key)


def comparable(o) -> tuple:
    """An object's KiCad fields, for "is this the same primitive": not pcbc's `source`/`where`/`comment`.
    A group's members compare as a set (KiCad writes them sorted by uuid, whatever order a line gave)."""
    if isinstance(o, Copper):
        return tuple((f, getattr(o, f)) for f in Copper.__dataclass_fields__ if f not in ("source", "where", "comment"))
    if isinstance(o, Graphic):
        f = dict(o.f)
        if "members" in f:
            f["members"] = tuple(sorted(f["members"]))
        if o.kind == "dimension" and f.get("gr_text") is not None:
            # KiCad recomputes a dimension's text from `pts`, `height` and `format` on every save, and
            # its position too unless `style["text_position_mode"]` is 2 (manual); and it gives the
            # text the dimension's own uuid. Derived, not authored: compared as KiCad's function of the
            # fields that are (docs/layout-properties.md, "What KiCad rewrites on save").
            txt = dict(f["gr_text"])
            txt.pop("text", None)
            txt.pop("uuid", None)
            style = f.get("style") or {}
            if style.get("text_position_mode") != 2:
                txt.pop("at", None)
            if style.get("keep_text_aligned"):
                txt.pop("angle", None)  # KiCad turns the text to the dimension's own angle
            f["gr_text"] = txt
        if o.kind == "generated" and isinstance(f.get("props"), dict):
            # A generator's `last_*` properties are KiCad's record of its last run, redone on load.
            f["props"] = {k: v for k, v in f["props"].items() if not k.startswith("last_")}
        return (o.kind, o.id, *((k, f[k]) for k in sorted(f)))
    return (o.ref, o.at, o.rot, o.layer, o.locked)


def diff_objects(want: list, got: list, *, what: str) -> list[str]:
    """Every object of `want` that `got` does not carry exactly, by id (by ref for a pose)."""
    key = lambda o: getattr(o, "id", None) or getattr(o, "ref", None)  # noqa: E731
    w = {key(o): o for o in want}
    g = {key(o): o for o in got}
    out = []
    for k in sorted(set(w) | set(g), key=str):
        if k not in g:
            out.append(f"{w[k].where or k}: {what} has no such object")
        elif k not in w:
            out.append(f"{g[k].where or k}: {what} has an object nothing wrote")
        elif comparable(w[k]) != comparable(g[k]):
            a, b = comparable(w[k]), comparable(g[k])
            # Each differing field with what was written and what came back (third review P5/R2:
            # KiCad keeps an angle or a ratio to 10 significant digits and a length to 1 nm, and a
            # refusal that names no value could not say what to write instead).
            fields = []
            for x, y in zip(a, b):
                if x == y:
                    continue
                name = x[0] if isinstance(x, tuple) else "kind/id"
                have, want = (x[1] if isinstance(x, tuple) and len(x) > 1 else x), (y[1] if isinstance(y, tuple) and len(y) > 1 else y)
                if isinstance(have, dict) and isinstance(want, dict):
                    # A dict-valued field (`teardrops`, a dimension's `format`): name the key that
                    # differs, so `filter_ratio` is blamed and not the nine-entry dict around it.
                    for sub in sorted(set(have) | set(want), key=str):
                        if have.get(sub) != want.get(sub):
                            fields.append(f"{name}.{sub} (written {_short(have.get(sub))}, {what} has {_short(want.get(sub))}: write that)")
                    continue
                fields.append(f"{name} (written {_short(have)}, {what} has {_short(want)}: write that)")
            out.append(f"{g[k].where or w[k].where or k}: {what} differs in {', '.join(fields)}")
    return out




def roles_doc(text: str, refusals=()):
    """The role of every piece pcbc wrote, read off the **emitted** board (a checker reading the
    `.kicad_pcb`, `docs/direction.md` §2): each piece in a `pcbc:<reason>:<owner>` group, with its
    geometry as the board has it. The keys are `route_emit.piece_key`'s shape (4 dp), so a gate's
    arithmetic is unchanged."""
    from .route_emit import Sidecar

    board = decompile(text)
    by_uuid = {c.uuid: c for c in board.copper}
    items = []
    for g in sorted((g for g in board.graphics if g.kind == "group" and str(g.f.get("name", "")).startswith(ROLE_PREFIX)), key=lambda g: g.f["name"]):
        _prefix, reason, owner = str(g.f["name"]).split(":", 2)
        for m in g.f["members"]:
            c = by_uuid.get(m)
            if c is None:
                raise ValueError(f"group {g.f['name']!r} names {m}, which is not copper on this board")
            if c.kind == "via":
                key = ["via", [round(c.at[0], 4), round(c.at[1], 4)], list(c.layers or ("F.Cu", "B.Cu"))]
                w, drill = c.size or 0.0, c.drill or 0.0
            elif c.kind in ("seg", "arc"):
                key = ["seg", c.layer, [round(c.a[0], 4), round(c.a[1], 4)], [round(c.b[0], 4), round(c.b[1], 4)], round(c.width or 0.0, 4)]
                w, drill = c.width or 0.0, 0.0
            else:
                continue
            items.append({"key": key, "uuid": m, "reason": reason, "net": c.net, "owner": owner, "w": round(w, 4), "drill": round(drill, 4)})
    return Sidecar(items=items, refusals=[dict(r) for r in refusals])


# ----------------------------------------------------------------------------------- the job


def _clean(out: Path) -> None:
    for stale in stage_outputs(out):
        if stale.exists():
            stale.unlink()
    un = Path(out).parent / UNROUTED_DIR
    if un.is_dir():
        for f in sorted(un.iterdir()):
            if f.is_file():
                f.unlink()
        un.rmdir()


def route_in_passes(design: Design, job, pl, *, name: str, core_cu: list[Copper]):
    """Step 3 of the module doc: `route_native.route_stage` in adoption passes (`LOCK_PASSES`). Returns
    (the last route, every object it produced — place-level copper, pours, pieces — stamped, the role
    groups, the pass record). One function, so `route_native.replay` routes a problem file with the same
    locks the build had (the refuters' E8: a replay that ignored `layout.core.py` said a failure caused
    by a core rule area no longer fails)."""
    from .gen import signature
    from .route_native import route_stage, to_objects

    locked: list[Copper] = []
    passes: list[dict] = []
    while True:
        rt = route_stage(design, job, pl, name=name, core_cu=locked)
        gen_cu, groups = to_objects(rt.pieces, name)
        produced = stamp(design, [*pl.copper, *rt.pours, *gen_cu])
        by_sig: dict = {}
        for b in produced:
            by_sig.setdefault(signature(b), []).append(b)
        locked_ids = {c.id for c in locked}

        def reproduced(c: Copper) -> bool:
            key = adopt_key(c)
            return any(adopt_key(b) == key for b in by_sig.get(signature(c), ()))

        missing = [c for c in core_cu if c.id not in locked_ids and not reproduced(c)]
        passes.append({"locked": sorted(locked_ids), "reproduced": sorted(c.id for c in core_cu if c.id not in locked_ids and reproduced(c)), "missing": sorted(c.id for c in missing)})
        if not missing or len(passes) > LOCK_PASSES:
            return rt, produced, groups, passes
        want = locked_ids | {c.id for c in missing}
        locked = list(core_cu) if len(passes) == LOCK_PASSES else [c for c in core_cu if c.id in want]


def layout_job(design: Design, *, out: Path, board: Path, name: str | None = None, placement=None) -> dict:
    """Place, route, write `layout.gen.py`, emit `out`. See the module doc. Never raises for a board
    fact: every refusal is `result["error"]`; an unrouted net is `result["unrouted"]` with its moves
    (the build runs every gate on what was emitted and then fails)."""
    from . import trace
    from .check import check_text
    from .gen import signature
    from .place_native import emitted_text, place
    from .route_native import PatternRefused, move_lines, write_problems

    out, board = Path(out), Path(board)
    name = name or board.stem
    work = out.parent
    work.mkdir(parents=True, exist_ok=True)
    _clean(out)
    result: dict = {"pcb": str(out), "core": str(core_path(board)) if core_path(board).exists() else None, "router": "native", "error": None}

    # 1. place, in memory; the place-only board checked in memory before any routing
    pl = placement if placement is not None else place(design, name=name, base=work)
    result["place"] = {"layout": pl.report, "notes": pl.notes, "poses": {p.ref: {"at": list(p.at), "rot": p.rot} for p in pl.poses}}
    if pl.error:
        return {**result, "error": pl.error}
    job = pl.job
    placed_text = emitted_text(design, pl, name=name)
    fails = check_text(job, placed_text)
    if fails:
        return {**result, "error": "check: " + "; ".join(fails)}

    # 2. core — judged against the Python model (the layer table `seed._layers` writes, the parts'
    # uuids from `place_native.foreign_uuids`); the emit base is built for the probe and emit only.
    try:
        core_raw, core_gr = load_core(design, board, pl, name=name)
    except ValueError as exc:
        return {**result, "error": f"a core line is refused: {exc}"}
    result["core_objects"] = len(core_raw) + len(core_gr)
    try:
        core_cu = stamp(design, core_raw) if core_raw else []
    except ValueError as exc:
        return {**result, "error": f"a core line is refused: {exc}"}
    for c in core_cu:
        lack = _unfillable(c)
        if lack:
            return {**result, "error": f"a core line is refused: {c.where or CORE_NAME}: {lack}"}
    filled = {}
    for raw, full in zip(core_raw, core_cu):
        diff = {f: getattr(full, f) for f in Copper.__dataclass_fields__ if getattr(raw, f) != getattr(full, f)}
        if diff:
            filled[raw.where or raw.id] = diff
    if filled:
        result["core_filled"] = filled
    if core_cu or core_gr:
        from .core_probe import round_trip

        try:
            probe_fails, probe_rec = round_trip(design, core_cu, core_gr, pl.text, name=name)
        except KicadMissing as exc:
            probe_fails, probe_rec = [], {"unchecked": str(exc)}
        result["core_probe"] = probe_rec
        if probe_fails:
            return {**result, "error": "a core line is refused: " + "; ".join(probe_fails[:8])}
    design.copper = [c for c in design.copper if c.source != "core"] + core_cu
    if core_cu or core_gr:
        pads, layers = feet_pads(design, pl.feet)
        fails, notes = check_layout(design, pads, layers=layers)
        fails += _clearance_clashes(design, pl.feet, core_cu)
        result["core_check"] = {"fails": fails, "notes": notes}
        if fails:
            return {**result, "error": "a core line is refused: " + "; ".join(fails)}

    # 3. route, in adoption passes
    core_pours = {(c.net, c.layer): c for c in core_cu if c.kind == "pour" and c.keepout is None and c.net and c.layer}
    try:
        rt, produced, groups, passes = route_in_passes(design, job, pl, name=name, core_cu=core_cu)
    except PatternRefused as exc:
        return {**result, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the build boundary: a bug is refused, never a traceback
        import traceback

        (work / UNROUTED_DIR).mkdir(parents=True, exist_ok=True)
        (work / UNROUTED_DIR / "internal-error.txt").write_text(traceback.format_exc())
        return {**result, "error": f"internal router error ({type(exc).__name__}: {exc}); this is a pcbc bug, not a board move. Problem file: {UNROUTED_DIR}/internal-error.txt"}
    result["lock_passes"] = passes
    result["adopted"] = sorted(passes[-1]["reproduced"])
    result["locked"] = sorted(passes[-1]["locked"])
    result.update(
        {
            "fanout": rt.fanout,
            "patterns": rt.stats.get("census", {}),
            "pattern_moves": rt.pattern_moves,
            "notes": rt.notes,
            "refusals": rt.refusals,
            "refused": dict(sorted(rt.refused.items())),
            "pattern_links": rt.pattern_links,
            "links": dict(sorted(rt.links.items())),
            "pattern_nets": __import__("pcbc.route_emit", fromlist=["census_by_net"]).census_by_net(rt.pieces),
            "order": rt.order,
            "route_stats": {k: v for k, v in rt.stats.items() if k != "census"},
        }
    )

    # 4. objects: a generated object that is a core object's copy is that core object (the core line wins)
    core_sig = {signature(c): uuid_of(c, name) for c in core_cu}
    # A core `Pour` on a plane target's (net, layer) is that plane: the generated one goes, whether the
    # core line was adopted (the same object) or holds a different outline or fields.
    gen_all = [c for c in produced if not (c.kind == "pour" and c.keepout is None and (c.net, c.layer) in core_pours)]
    rename: dict[str, str] = {}
    keep: list[Copper] = []
    for c in gen_all:
        u = core_sig.get(signature(c))
        if u is not None:
            if c.uuid:
                rename[c.uuid] = u
            continue
        keep.append(c)
    core_uids = {uuid_of(o, name): o for o in [*core_cu, *core_gr]}
    clash = sorted(c.uuid for c in keep if c.uuid in core_uids)
    if clash:
        o = core_uids[clash[0]]
        return {**result, "error": f"{o.where or o.id}: its uuid {clash[0]} is the uuid of different copper the build draws; a uuid names one object, so drop uuid= from that core line"}
    for g in groups:
        g.f["members"] = tuple(sorted({rename.get(m, m) for m in g.f["members"]}))
    gen_cu = keep
    gen_gr = [*pl.graphics, *groups]
    try:
        assign_ids(gen_cu, {o.id for o in [*core_cu, *core_gr]}, gen_gr, uuid_ids={u: o.id for u, o in core_uids.items()})
    except IdTaken as exc:
        owner = next((o for o in [*core_cu, *core_gr] if o.id == exc.id), None)
        return {**result, "error": f"{(owner.where if owner else None) or CORE_NAME}: id {exc.id!r} is the id {GEN_NAME} gives to a different object ({exc.kind} {exc.uuid}); give this line another id"}
    # A core group names its members by id and a role group by uuid: one item, one group.
    member_owner: dict[str, str] = {}
    core_uuid_of = {o.id: uuid_of(o, name) for o in [*core_cu, *core_gr]}
    gen_uuid_of = {o.id: o.uuid for o in gen_cu}
    for g in [*groups, *(g for g in core_gr if "members" in g.f)]:
        who = g.f.get("name", g.id)
        for m in g.f["members"]:
            u = core_uuid_of.get(m) or gen_uuid_of.get(m, m)
            if u in member_owner and member_owner[u] != who:
                what = "a piece pcbc's pattern wrote and grouped as " if str(member_owner[u]).startswith(ROLE_PREFIX) else "already in "
                return {**result, "error": f"{g.where or who}: {m!r} is {what}{member_owner[u]!r}; KiCad keeps an item in one group, so a core group cannot take it — drop it from the group or delete the group"}
            member_owner[u] = who

    # 5. gen
    gp = gen_path(out)
    write_gen(gp, pl.poses, gen_cu, board=name, graphics=gen_gr)
    result["gen"] = str(gp)
    before_cu, before_gr, before_pose = len(design.copper), len(design.graphics), len(design.poses)
    try:
        load_layout(gp, design, source="gen")
    except ValueError as exc:
        return {**result, "error": f"{GEN_NAME} does not load back: {exc}"}
    gen_loaded = [c for c in design.copper[before_cu:] if c.source == "gen"]
    gr_loaded = [g for g in design.graphics[before_gr:] if g.source == "gen"]
    poses = [p for p in design.poses[before_pose:] if p.source == "gen"]
    bad = diff_objects([*gen_cu, *gen_gr], [*gen_loaded, *gr_loaded], what=f"{GEN_NAME} as loaded")
    bad += diff_objects(pl.poses, poses, what=f"{GEN_NAME}'s Place")
    if bad:
        return {**result, "error": f"{GEN_NAME} is not the Python it was written from: " + "; ".join(bad[:6])}
    pads, layers = feet_pads(design, pl.feet)
    fails, notes = check_layout(design, pads, layers=layers)
    result["layout_check"] = {"fails": fails, "notes": notes}
    if fails:
        return {**result, "error": "a core line is refused: " + "; ".join(fails)}

    # 6. emit from the loaded Python, and prove the transcription both ways
    all_cu = [*core_cu, *gen_loaded]
    all_gr = [*core_gr, *gr_loaded]
    try:
        final = emit_board(pl.text, poses, all_cu, all_gr, design=design, board=name)
    except ValueError as exc:
        return {**result, "error": f"emit: {exc}"}
    # The emit: the board text exists from here on. Everything after it (the transcription check, the
    # write, every gate) reads this board; nothing before it read or wrote one (test_native.py, P1).
    trace.mark("emit")
    back = decompile(final)
    back_ids = {uuid_of(o, name): o.id for o in [*all_cu, *all_gr]}
    for o in [*back.copper, *back.graphics]:
        o.id = back_ids.get(o.uuid, o.uuid)
    for g in back.graphics:
        if "members" in g.f:
            g.f["members"] = tuple(back_ids.get(m, m) for m in g.f["members"])
    want = [replace(c, uuid=uuid_of(c, name)) for c in all_cu] + [Graphic(g.kind, g.id, own_uuids(g, name), g.source, g.comment, g.where) for g in all_gr]
    bad = diff_objects(want, [*back.copper, *back.graphics], what="the emitted board")
    if bad:
        return {**result, "error": "the emitted board is not the Python it was written from: " + "; ".join(bad[:6])}
    # Emit transcribes layout objects and may change nothing else: every footprint (its pads included),
    # the setup, the layers and the nets are the placed board's, uuids masked (the second review's m5;
    # `build.non_layout_diff`). `build.py` asks the same again after KiCad's refill-and-save.
    from .build import non_layout_diff

    moved = non_layout_diff(placed_text, final)
    if moved:
        return {**result, "error": "emit changed the board outside the layout objects (a footprint, a pad, a board setting): " + "; ".join(moved[:4])}
    write_sidecars(design, compile_design(design), out, name=name)
    out.write_text(final)
    result["emitted"] = {
        "pcb": str(out),
        "place": len(poses),
        "segments": sum(1 for c in all_cu if c.kind == "seg"),
        "arcs": sum(1 for c in all_cu if c.kind == "arc"),
        "vias": sum(1 for c in all_cu if c.kind == "via"),
        "zones": sum(1 for c in all_cu if c.kind == "pour"),
        "drawings": len(all_gr),
        "core": len(core_cu) + len(core_gr),
        "gen": len(gen_loaded) + len(gr_loaded),
    }
    result["layout_objects"] = {"copper": all_cu, "graphics": all_gr, "poses": poses}
    result["placed_text"] = placed_text  # popped by build.py for its after-save non-layout check
    result["copper_uuids"] = sorted({uuid_of(o, name) for o in [*all_cu, *all_gr]} | set(layout_uuids(back)))
    # 7. what the router could not finish: moves and problem files, beside the emitted board
    write_problems(work / UNROUTED_DIR, design, job, rt.scene, rt)
    if rt.failed:
        result["unrouted"] = rt.unrouted
        result["moves"] = move_lines(design, job, rt, problems_dir=f"layout/{name}/routed/{UNROUTED_DIR}")
    return result
