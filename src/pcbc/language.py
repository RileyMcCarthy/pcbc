"""Board-file DSL. Ordinary Python that calls these constructors.

Electrical: Net, Power, Ground, Resistor, Capacitor, Led, load(part).
Spatial: Board, Place, Keepout, Region, NetReq (PCB CSS).
Schematic: SchRegion, SchPlace (CSS body, or pin=/to=/gap= attach).
"""

from __future__ import annotations

import ast
import difflib
import inspect
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .circuit import check_design, load_part
from .css import (
    AUTO,
    BoxStyle,
    apply_style_to_spec,
    box_style_from_kwargs,
    expand_shorthand,
    parse_length,
)
from .model import (  # noqa: E402 — extended below
    Copper,
    Graphic,
    BoardSpec,
    BridgeReq,
    BusReq,
    ChainReq,
    Design,
    GuardReq,
    IsolationReq,
    KeepoutSpec,
    Net,
    NetReqSpec,
    PairReq,
    Part,
    Pin,
    PlaceSpec,
    RegionSpec,
    SchPlaceSpec,
    ThermalReq,
)

_current: Design | None = None
_board_dir: Path | None = None
_board_path: Path | None = None  # the board file being loaded: constraint lines cite their line in it


_layout_source = "board"
_layout_path: str | None = None  # the layout file `load_layout` is executing, for `_where`


# The constructors a layout file may reach the design through. Any other caller of `_doc()` while a
# layout file executes is a `board.py` constructor (`Place`, `NetReq`, `Keepout`, a part, a net) that
# was imported past the layout namespace, and it is refused at the line that called it.
_LAYOUT_CALLERS = frozenset({"Seg", "Arc", "Via", "_copper", "_drawing", "_gid", "_gen_place"})


def _doc() -> Design:
    global _current
    if _layout_path is not None:
        caller = sys._getframe(1).f_code.co_name
        if caller not in _LAYOUT_CALLERS:
            name = Path(_layout_path).name
            raise ValueError(
                f"{_where() or name}: {caller}() is board.py's, not a layout primitive; {name} holds layout "
                f"primitives only. Move this line into board.py"
            )
    if _current is None:
        _current = Design()
    return _current


def reset() -> None:
    global _layout_source
    _layout_source = "board"
    global _current, _board_dir, _board_path
    _current = Design()
    _board_dir = None
    _board_path = None


def _line() -> int:
    """The board.py line of the constructor call two frames up, when that frame is the board being
    loaded; 0 otherwise (a call from Python). Deterministic: it is a property of the file."""
    frame = sys._getframe(2)
    if _board_path is not None and frame.f_code.co_filename == str(_board_path):
        return int(frame.f_lineno)
    return 0


def _net_name_refusal(name: str) -> str | None:
    """Why KiCad cannot hold `name` as a net name, or None. A backslash is an escape in KiCad's
    s-expressions and its netlist XML (`LED\\b` came back from `kicad-cli` as a backspace, which is not
    XML), and a control character (a newline, ...) breaks the schematic's label outright (sixth
    review: both escaped `build_job` as tracebacks out of the sch stage). A tab is kept: KiCad holds it."""
    import unicodedata

    if "\\" in name:
        return "carries a backslash, which KiCad reads as an escape"
    bad = next((ch for ch in name if ch != "\t" and unicodedata.category(ch) == "Cc"), None)
    if bad is not None:
        return f"carries the control character {bad!r}, which KiCad's schematic and netlist cannot hold"
    return None


def _board_line() -> int:
    """The line of `board.py` being executed now (the deepest frame in it), or 0."""
    frame = sys._getframe(1)
    while frame is not None:
        if _board_path is not None and frame.f_code.co_filename == str(_board_path):
            return int(frame.f_lineno)
        frame = frame.f_back
    return 0


def _register_net(net: Net) -> Net:
    why = _net_name_refusal(net.name)
    if why:
        line = _board_line()
        raise ValueError(f"{'board.py line ' + str(line) + ': ' if line else ''}the net name {net.name!r} {why}; rename the net")
    doc = _doc()
    existing = doc.nets.get(net.name)
    if existing is None:
        doc.nets[net.name] = net
        return net
    if existing.kind == "net" and net.kind != "net":
        existing.kind = net.kind
    if net.wire_mm is not None:
        existing.wire_mm = net.wire_mm
    return existing


def Power(name: str) -> Net:
    return _register_net(Net(str(name), kind="power"))


def Ground(name: str = "GND") -> Net:
    return _register_net(Net(str(name), kind="ground"))


def Net_(name: str, *, wire_mm: float | None = None) -> Net:  # noqa: N802 — exported as Net
    """A signal net. ``wire_mm`` caps the wire the schematic may draw for it
    (0 = labels only); the default is ``SchStyle(wire_mm=...)``, 25.4."""
    return _register_net(Net(str(name), kind="net", wire_mm=wire_mm))


def SchStyle(*, wire_mm: float | None = None) -> None:
    """Sheet-wide schematic defaults. ``wire_mm``: longest wire drawn between
    two symbols before the net is joined by labels instead."""
    if wire_mm is not None:
        _doc().sch_wire_mm = float(wire_mm)


def _padding4(padding) -> tuple[float, float, float, float]:
    t, r, b, l = expand_shorthand(padding if padding is not None else 0)
    return (
        float(parse_length(t) or 0),
        float(parse_length(r) or 0),
        float(parse_length(b) or 0),
        float(parse_length(l) or 0),
    )


def Board(
    size_mm: tuple[float, float] | None = None,
    *,
    width: float | None = None,
    height: float | None = None,
    layers: int = 4,
    stackup: str = "jlcpcb_4l_1oz",
    pcb: str | None = None,
    planes: list[tuple[str, str]] | None = None,
    padding: object = 0,
    net_order: str | None = None,
) -> BoardSpec:
    if size_mm is not None:
        w, h = float(size_mm[0]), float(size_mm[1])
    elif width is not None and height is not None:
        w, h = float(width), float(height)
    else:
        raise ValueError("Board needs size_mm=(w, h) or width= and height= (mm)")
    from .stackup import get_stackup

    stack = get_stackup(stackup)
    if int(layers) != stack.layers:
        # The seed writes `layers` copper layers while every number comes from the stackup: a
        # 4-layer board on a 2-layer stackup was seeded with In1/In2 and 2-layer impedances.
        raise ValueError(
            f"Board(layers={int(layers)}, stackup={stackup!r}): {stackup} is a {stack.layers}-layer stackup; "
            f"write layers={stack.layers}, or pick a {int(layers)}-layer stackup"
        )
    # **The 2-layer refusal is gone, and what removed it is the router reading the declaration.**
    # It was added when the old router took `planes=` only above two layers and poured `GND` on
    # `B.Cu` itself below that, so a 2-layer `planes=` reached no step at all: `Board()` accepted it,
    # the router ignored it, the ampacity measurement exempted the net on the strength of it, and
    # `FAB_NOTES.md` reported a rail as poured that had no pour. "A declaration the router never
    # reads is a lie the board tells its author" was the right refusal for that router.
    #
    # S8 made the router read it (`route_scene.plane_targets`): a declared pour is
    # poured on whatever stackup declared it, and the implicit back pour is what a board that
    # declares nothing gets. So the sentence no longer describes this code, and a refusal that has
    # stopped being true is worse than no refusal — it would now forbid the only way to write down
    # the thing R-E1 needs, **two pours of one net facing each other across the core on two layers**
    # (`docs/stitch-plan.md` section 8 item 1). The layer check below stays: a layer the stackup does
    # not have is still a lie, and it is one no router change can make true.
    declared = tuple((str(n), str(l)) for n, l in (planes or ()))
    copper = set(stack.copper_layers())
    for net, lay in declared:
        if lay not in copper:
            raise ValueError(
                f"Board(planes=[({net!r}, {lay!r})]): {stackup} has no copper layer {lay}; "
                f"it has {', '.join(sorted(copper))}"
            )
    if net_order is not None:
        # `net_order` was the old router's `--ordering` flag, and the old router is gone. The native
        # router orders nets by class width, then name (`route_native.ORDER`); a board that wants a
        # net routed earlier locks its copper in layout.core.py. Refused rather than ignored: a
        # declaration nothing reads is a lie the board tells its author (docs/native-plan.md §1.2).
        raise ValueError(
            f"Board(net_order={net_order!r}): net_order was the old router's flag; the native router "
            f"orders nets by class width, then name (docs/native-plan.md §3.2). Drop the argument; to "
            f"route a net first, lock its copper in layout.core.py"
        )
    spec = BoardSpec(
        size_mm=(w, h),
        layers=int(layers),
        stackup=stackup,
        pcb=pcb,
        planes=declared,
        padding=_padding4(padding),
    )
    _doc().board = spec
    return spec


def _style(who: str, **kwargs) -> BoxStyle:
    return box_style_from_kwargs(who=who, **kwargs)


def Place(
    ref: str,
    at: tuple[float, float] | None = None,
    rot: float | None = None,
    rotate: float | None = None,
    side: str = "F",
    locked: bool = True,
    reason: str = "",
    position: str | None = None,
    style: str | None = None,
    parent: str | None = None,
    box: str | None = None,
    transform: str | None = None,
    to: str | None = None,
    toward: str | None = None,
    gap: float | None = None,
    edge: str | None = None,
    overhang: float = 0.0,
    **css,
) -> PlaceSpec:
    """Where a part goes on the copper.

    Anchors take CSS (left/top/right/bottom, margin auto, a Region parent) or at=.
    Everything else says what it belongs to: to="U1.VIN" puts this part's pad on that
    net right outside U1, on the side the pin faces (toward= overrides, gap= is courtyard
    clearance in mm); edge="bottom" stands a connector on that board edge, face out,
    centred unless left/right (or top/bottom) say where along it.
    """
    who = f"Place({ref!r})"
    if to is not None and (at is not None or edge is not None):
        raise ValueError(f"{who}: to= places by relation; drop at= / edge=")
    if to is not None and any(css.get(k) is not None for k in ("left", "right", "top", "bottom")):
        raise ValueError(f"{who}: to= places by relation; drop left/top/right/bottom")
    if "padding" in css or "padding_top" in css:
        raise ValueError(
            f"{who}: padding belongs on Board or Region, not a footprint "
            "(footprints have intrinsic courtyard size). Use margin."
        )
    if css.get("width") is not None or css.get("height") is not None:
        raise ValueError(
            f"{who}: width/height belong on Region or Keepout. "
            "A footprint's size is its courtyard."
        )
    rot_v = rotate if rotate is not None else (rot if rot is not None else 0.0)
    st = _style(
        who,
        style=style,
        position=position,
        rotate=rot_v,
        transform=transform,
        box=box,
        parent=parent,
        **css,
    )
    if at is not None and st.has_insets():
        raise ValueError(
            f"{who}: use at=(x, y) OR left/top/right/bottom, not both. "
            "at= is the KiCad origin (CAD apertures). CSS names the edges."
        )
    spec = PlaceSpec(
        ref=str(ref),
        at=(float(at[0]), float(at[1])) if at is not None else None,
        rot=float(st.rotate),
        side=side.upper()[:1],
        locked=bool(locked),
        reason=reason,
        to=str(to) if to is not None else None,
        toward=str(toward) if toward is not None else None,
        gap=float(gap) if gap is not None else None,
        edge=str(edge) if edge is not None else None,
        overhang=float(overhang),
        rot_set=rotate is not None or rot is not None,
        source=_layout_source,
        line=_line(),
    )
    spec = apply_style_to_spec(spec, st)
    spec.rot = float(st.rotate)
    if at is not None:
        spec.position = "absolute"
        spec.from_box = "origin"
        spec.left = at[0]
        spec.top = at[1]
    elif st.is_absolute() or edge is not None:
        spec.position = "absolute"
    if spec.locked and spec.at is None and not spec.has_css() and not (spec.to or spec.edge):
        raise ValueError(
            f"{who}: locked=True needs at=(x, y), CSS top/right/bottom/left, to=\"U1.PIN\" or edge=."
        )
    _doc().places.append(spec)
    return spec


def Keepout(
    name: str,
    box: tuple[float, float, float, float] | None = None,
    no: list[str] | tuple[str, ...] = ("copper", "via"),
    position: str | None = None,
    style: str | None = None,
    parent: str | None = None,
    **css,
) -> KeepoutSpec:
    who = f"Keepout({name!r})"
    st = _style(who, style=style, position=position or "absolute", parent=parent, **css)
    if box is None and not st.has_insets():
        raise ValueError(
            f"{who}: needs box=(x0,y0,x1,y1) or CSS left/top/width/height."
        )
    spec = KeepoutSpec(
        name=str(name),
        box=(tuple(float(x) for x in box) if box is not None else None),  # type: ignore[arg-type]
        # `no="copper"` is one name, not six characters: a bare string used to be iterated into
        # ('c', 'o', 'p', 'p', 'e', 'r') and the keepout then forbade nothing pcbc looks for.
        no=(no,) if isinstance(no, str) else tuple(str(x) for x in no),
    )
    spec = apply_style_to_spec(spec, st)
    if spec.box is not None:
        spec.box = (float(spec.box[0]), float(spec.box[1]), float(spec.box[2]), float(spec.box[3]))
    _doc().keepouts.append(spec)
    return spec


def _finite(value, who: str) -> float:
    """A number, or a ValueError naming `who`: a bool, a string or a NaN is refused at load (fifth
    review: `solder_mask_margin="0.05"` and `angle="12"` used to be `float()`-ed silently)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{who} is a number, got {value!r}")
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{who} is a finite number, got {value!r}")
    return v


def _nm(v: float) -> float:
    """A length as KiCad keeps it: at 1 nm, rounded exactly as `sexp.fmt_num` writes it. A value
    finer than that (`width=0.2000004`) comes back from KiCad's save at 1 nm, so it is read at 1 nm
    at load rather than refused after routing (fifth review, C3)."""
    from .sexp import fmt_num

    return float(fmt_num(v))


def _dbl(v: float) -> float:
    """A number KiCad keeps as a double (an angle, a ratio, an area): to its ten significant digits,
    what its save writes (`12.345678901234567` comes back `12.3456789`, measured)."""
    return float(f"{float(v):.10g}")


def _pt(value, who: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{who}: expected (x, y) in millimetres, got {value!r}")
    return (_nm(_finite(value[0], f"{who}: x")), _nm(_finite(value[1], f"{who}: y")))


def _len(value, who: str, key: str) -> float | None:
    """An optional length field of a copper constructor: None, or a finite number at 1 nm."""
    return None if value is None else _nm(_finite(value, f"{_where() or who}: {who}: {key}"))


def _real(value, who: str, key: str) -> float | None:
    """An optional double field (an angle, a ratio, an area): None, or a finite number to ten digits."""
    return None if value is None else _dbl(_finite(value, f"{_where() or who}: {who}: {key}"))


def _int(value, who: str, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{_where() or who}: {who}: {key} is a whole number, got {value!r}")
    return value


def _bool(who: str, key: str, value, *, none_ok: bool = False) -> bool | None:
    """A yes/no field: True or False, nothing else. `bool("no")` is True, so the string "no" used to
    put a mask opening on a track (`solder_mask="no"`) and lock a line written `locked="no"` (fifth
    review BLOCKER); every yes/no field and every flag is refused at load unless it is a bool."""
    if value is None and none_ok:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"{_where() or who}: {who}: {key} is True or False, got {value!r}")


def _text(value, who: str) -> str:
    """A string field as KiCad can keep it: a str, with no NUL (KiCad's save cuts the string there,
    measured: 'a\\x00b' comes back 'a') and no lone surrogate (no file can hold one: the board write
    raised `UnicodeEncodeError` after routing)."""
    if not isinstance(value, str):
        raise ValueError(f"{who} is a string, got {value!r}")
    if "\x00" in value:
        raise ValueError(f"{who} holds a NUL character (\\x00), and KiCad's save cuts the string there; remove it")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        bad = value[exc.start]
        raise ValueError(f"{who} holds the lone surrogate U+{ord(bad):04X}, which no board file can store; remove it or write the character it was half of") from None
    return value


def _pt_entry(value, who: str):
    """One entry of a polygon's points: `(x, y)`, or an arc as `((sx, sy), (mx, my), (ex, ey))` —
    KiCad's `(arc (start) (mid) (end))` inside a `(pts ...)`."""
    from .layout_prims import is_arc

    if is_arc(value):
        return tuple(_pt(p, f"{who}: arc") for p in value)
    return _pt(value, who)


def _where() -> str:
    """`layout.core.py:12`: the line of the layout file being executed that made this call, or of the
    board file being loaded (so a layout object written in `board.py` is refused naming its line).

    Provenance only — a refusal names the line an author has to edit. Empty outside a load.
    """
    import sys

    target = _layout_path or (str(_board_path) if _board_path is not None else None)
    if target is None:
        return ""
    f = sys._getframe(1)
    while f is not None:
        if f.f_code.co_filename == target:
            return f"{Path(target).name}:{f.f_lineno}"
        f = f.f_back
    return ""


def _copper(item: Copper) -> Copper:
    item.where = _where()
    _text(item.id, f"{item.where or item.kind}: id")
    _doc().copper.append(item)
    return item


def _opt(v, cast=float):
    return None if v is None else cast(v)


def _uuid(who: str, uuid) -> str | None:
    """`uuid=` in the form KiCad keeps, or None (derived from `id` at emit); anything else is refused
    at load naming the line (`layout_prims.uuid_check`)."""
    from .layout_prims import uuid_check

    return None if uuid is None else uuid_check(uuid, _where() or who)


def _word(who: str, key: str, value, table: str, *, none_ok: bool = True):
    """`value` checked against KiCad's word list `layout_prims.WORDS[table]` at load: outside it is
    refused naming the line, never a `kicad-cli` traceback after routing (fourth review, C3)."""
    from .layout_prims import WORDS

    if value is None and none_ok:
        return None
    words = WORDS[table]
    if isinstance(value, bool) or value not in words:
        allowed = ", ".join(repr(w) for w in words)
        tail = " (or leave it out)" if none_ok else ""
        raise ValueError(f"{_where() or who}: {who}: {key} must be one of {allowed}{tail}, got {value!r}")
    return value


def _track_kw(who: str, layer: str, solder_mask: bool, solder_mask_margin, locked: bool, uuid) -> dict:
    # A margin is a number, and KiCad's save keeps it only on a track whose mask is opened: written on
    # a track with `solder_mask=False` it is dropped on save, and the build used to refuse the line only
    # after routing ("written 0.05, KiCad's save has None"; fourth review coreprobe5 `seg_width_string`).
    margin = _num(solder_mask_margin, "solder_mask_margin", _where() or who)
    margin = None if margin is None else _nm(margin)
    solder_mask = _bool(who, "solder_mask", solder_mask)
    if margin is not None and not solder_mask:
        raise ValueError(f"{_where() or who}: {who}: solder_mask_margin={_g_num(margin)} on a track with solder_mask=False is what KiCad's save drops; write solder_mask=True with it, or leave the margin out")
    layer = _text(layer, f"{_where() or who}: {who}: layer")
    if solder_mask and layer not in ("F.Cu", "B.Cu"):
        # KiCad's parser refuses the board outright ("Expecting no mask layer when track is on
        # internal layer"): an inner layer has no mask. Refused here, before routing (fifth review).
        raise ValueError(f"{_where() or who}: {who}: solder_mask=True on {layer!r}: only an outer layer (F.Cu, B.Cu) has a mask to open, and KiCad refuses the board; drop solder_mask or move the track")
    return dict(
        layer=layer,
        solder_mask=solder_mask,
        solder_mask_margin=margin,
        locked=_bool(who, "locked", locked),
        uuid=_uuid(who, uuid),
    )


def Seg(
    net: str,
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    layer: str = "F.Cu",
    width: float | None = None,
    locked: bool = False,
    solder_mask: bool = False,
    solder_mask_margin: float | None = None,
    uuid: str | None = None,
    id: str | None = None,
    comment: str = "",
) -> Copper:
    """One KiCad `(segment)` on `net`, field for field. Endpoints are points, not pin names.

    `net=None` (or `""`) is KiCad's no-net track, `(net "")`: copper on no net (a floating spreader,
    a coupon) — never the string "None". `a`/`b` are `(start)`/`(end)`; `solder_mask=True` is KiCad's `(layers "F.Cu" "F.Mask")` form of
    the layer (a track with its mask opened); `uuid=None` derives a stable one from `id`. A `width`
    left out is filled from the net's compiled constraint before emit; a width **below** it is refused
    for a core line, never widened.
    """
    who = f"Seg({net!r})"
    n = sum(1 for c in _doc().copper if c.kind == "seg" and c.net == net)
    return _copper(
        Copper(
            "seg",
            id or f"seg-{net}-{n}",
            "" if net is None else str(net),
            a=_pt(a, who),
            b=_pt(b, who),
            width=_len(width, who, "width"),
            source=_layout_source,
            comment=comment,
            **_track_kw(who, layer, solder_mask, solder_mask_margin, locked, uuid),
        )
    )


def Arc(
    net: str,
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
    *,
    layer: str = "F.Cu",
    width: float | None = None,
    locked: bool = False,
    solder_mask: bool = False,
    solder_mask_margin: float | None = None,
    uuid: str | None = None,
    id: str | None = None,
    comment: str = "",
) -> Copper:
    """One KiCad track `(arc)`: `(start)`, `(mid)`, `(end)`, and the same fields as `Seg`."""
    who = f"Arc({net!r})"
    n = sum(1 for c in _doc().copper if c.kind == "arc" and c.net == net)
    return _copper(
        Copper(
            "arc",
            id or f"arc-{net}-{n}",
            "" if net is None else str(net),
            a=_pt(start, who),
            mid=_pt(mid, who),
            b=_pt(end, who),
            width=_len(width, who, "width"),
            source=_layout_source,
            comment=comment,
            **_track_kw(who, layer, solder_mask, solder_mask_margin, locked, uuid),
        )
    )


_SIDES = ("front", "back")
_POST_KEYS = ("mode", "size", "depth", "angle")


def _post_machining(who: str, key: str, value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - set(_POST_KEYS):
        raise ValueError(f"{_where() or who}: {who}: {key} is {{'mode': 'counterbore'|'countersink', 'size': .., 'depth': .., 'angle': ..}}")
    _word(who, f"{key}['mode']", value.get("mode"), "via.post_machining.mode", none_ok=False)
    out = {}
    for k in _POST_KEYS:
        if k in value:
            v = value[k]
            out[k] = v if k == "mode" else _real(v, who, f"{key}[{k!r}]") if k == "angle" else _len(v, who, f"{key}[{k!r}]")
    return out


def _sided(who: str, key: str, value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - set(_SIDES):
        raise ValueError(f"{_where() or who}: {who}: {key} is {{'front': ..., 'back': ...}} with KiCad's words")
    for s in _SIDES:
        if s in value:
            _word(who, f"{key}[{s!r}]", value[s], f"via.{key}", none_ok=False)
    if not value or all(value.get(s, "none") == "none" for s in _SIDES):
        # KiCad's save drops a block whose every side is `none` (inherit the board's), so the line
        # would be refused after routing as "KiCad kept None": refused here instead.
        raise ValueError(f"{_where() or who}: {who}: {key} with every side 'none' is what KiCad writes nothing for; leave {key}= out")
    # A side left out is `none` (inherit the board's), and KiCad's save writes it so: `{"front": "yes"}`
    # comes back `{"front": "yes", "back": "none"}` for tenting, covering and plugging alike (measured,
    # `fix6/facts.py`). Filled here, so the object is what KiCad keeps.
    return {s: value.get(s, "none") for s in _SIDES}


def Via(
    net: str,
    at: tuple[float, float],
    *,
    layers: tuple[str, str] = ("F.Cu", "B.Cu"),
    size: float | None = None,
    drill: float | None = None,
    kind: str = "through",
    locked: bool = False,
    free: bool = False,
    remove_unused_layers: bool = False,
    keep_end_layers: bool = False,
    zone_layer_connections: Sequence[str] = (),
    tenting: dict | None = None,
    covering: dict | None = None,
    plugging: dict | None = None,
    capping: bool | None = None,
    filling: bool | None = None,
    padstack: tuple | None = None,
    backdrill: tuple | None = None,
    tertiary_drill: tuple | None = None,
    front_post_machining: dict | None = None,
    back_post_machining: dict | None = None,
    teardrops: dict | None = None,
    uuid: str | None = None,
    id: str | None = None,
    comment: str = "",
) -> Copper:
    """One KiCad `(via)`, field for field. `kind` is the atom after `via`: through (none), blind,
    buried, or micro.

    `tenting`/`covering`/`plugging` are KiCad's per-side blocks as `{"front": "yes", "back": "no"}`;
    `padstack` is `(mode, ((layer, size), ...))`; `backdrill`/`tertiary_drill` are KiCad's
    `(backdrill (size) (layers from to))` / `(tertiary_drill ...)` as `(size, (from, to))`;
    `front_post_machining`/`back_post_machining` are `{"mode": "counterbore"|"countersink", "size": ..,
    "depth": .., "angle": ..}` with the keys KiCad wrote; `teardrops` is the via's `(teardrops ...)`
    block with KiCad's own names.
    A field left at its default is not written, exactly as KiCad does not write it. `size`/`drill`
    left out are filled from the net's compiled via before emit.
    """
    who = f"Via({net!r})"
    from .layout_prims import stack_order

    _word(who, "kind", kind, "via.kind", none_ok=False)
    if not isinstance(layers, (tuple, list)) or len(tuple(layers)) != 2 or str(layers[0]) == str(layers[1]):
        raise ValueError(f"{_where() or who}: {who}: a via spans exactly two different layers, got {layers!r}")
    layers = tuple(_text(x, f"{_where() or who}: {who}: layers") for x in layers)
    remove_unused_layers = _bool(who, "remove_unused_layers", remove_unused_layers)
    zlc = _via_zlc(who, zone_layer_connections, remove_unused_layers)
    n = sum(1 for c in _doc().copper if c.kind == "via" and c.net == net)
    return _copper(
        Copper(
            "via",
            id or f"via-{net}-{n}",
            "" if net is None else str(net),
            at=_pt(at, who),
            # (top, bottom) in the copper stack's order, which is what KiCad's save writes (C3).
            layers=stack_order(layers[0], layers[1]),
            size=_len(size, who, "size"),
            drill=_len(drill, who, "drill"),
            via_type=kind,
            source=_layout_source,
            comment=comment,
            layer=None,
            locked=_bool(who, "locked", locked),
            free=_bool(who, "free", free),
            remove_unused_layers=remove_unused_layers,
            keep_end_layers=_bool(who, "keep_end_layers", keep_end_layers),
            zone_layer_connections=zlc,
            tenting=_sided(who, "tenting", tenting),
            covering=_sided(who, "covering", covering),
            plugging=_sided(who, "plugging", plugging),
            capping=_bool(who, "capping", capping, none_ok=True),
            filling=_bool(who, "filling", filling, none_ok=True),
            padstack=_padstack(who, padstack),
            backdrill=_via_drill(who, "backdrill", backdrill),
            tertiary_drill=_via_drill(who, "tertiary_drill", tertiary_drill),
            front_post_machining=_post_machining(who, "front_post_machining", front_post_machining),
            back_post_machining=_post_machining(who, "back_post_machining", back_post_machining),
            teardrops=_teardrops(who, teardrops),
            uuid=_uuid(who, uuid),
        )
    )


def _stack_key(name: str) -> tuple:
    """A copper layer's place in the stack, top to bottom: F.Cu, In1.Cu, In2.Cu, ..., B.Cu."""
    import re

    if name == "F.Cu":
        return (0, 0)
    if name == "B.Cu":
        return (2, 0)
    m = re.fullmatch(r"In(\d+)\.Cu", name)
    return (1, int(m.group(1))) if m else (3, 0)


def _via_zlc(who: str, value, remove_unused_layers: bool) -> tuple[str, ...]:
    """`zone_layer_connections`, in the copper stack's order, which is what KiCad's save writes
    (`("F.Cu", "B.Cu", "In2.Cu")` comes back `("F.Cu", "In2.Cu", "B.Cu")`, measured). KiCad keeps the
    list only on a via with `remove_unused_layers` (without it the save drops the list, measured), so
    one written without it is refused. The names are checked against the board in `load_core`."""
    if value is None or isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise ValueError(f"{_where() or who}: {who}: zone_layer_connections is a list of copper layer names, got {value!r}")
    names = [_text(x, f"{_where() or who}: {who}: zone_layer_connections") for x in value]
    if not names:
        return ()
    if len(set(names)) != len(names):
        raise ValueError(f"{_where() or who}: {who}: zone_layer_connections names a layer twice: {tuple(names)!r}")
    if not remove_unused_layers:
        raise ValueError(f"{_where() or who}: {who}: zone_layer_connections is kept by KiCad only on a via with remove_unused_layers=True (its save drops the list otherwise); write remove_unused_layers=True with it, or leave it out")
    return tuple(sorted(names, key=_stack_key))


def _padstack(who: str, value) -> tuple | None:
    """`padstack=(mode, ((layer, size), ...))` as KiCad keeps it. `front_inner_back` holds exactly the
    `"Inner"` and `"B.Cu"` sizes, in that order (KiCad writes `"Inner"`, never an `In<n>.Cu`, and fills
    the side left out with the other's size, measured); `custom` holds a size per copper layer below
    F.Cu in the stack's order — F.Cu's size is the via's own `size=`, and KiCad drops an `F.Cu` entry
    (measured). Completeness against the board's layers is checked in `load_core`."""
    if value is None:
        return None
    where = _where() or who
    if not isinstance(value, (tuple, list)) or len(value) != 2 or not isinstance(value[1], (tuple, list)):
        raise ValueError(f"{where}: {who}: padstack is (mode, ((layer, size), ...))")
    mode = _word(who, "padstack mode", value[0], "via.padstack.mode", none_ok=False)
    per = []
    for e in value[1]:
        if not isinstance(e, (tuple, list)) or len(e) != 2:
            raise ValueError(f"{where}: {who}: padstack entry {e!r} is (layer, size)")
        per.append((_text(e[0], f"{where}: {who}: padstack layer"), _len(e[1], who, f"padstack[{e[0]!r}]")))
    names = [la for la, _ in per]
    if len(set(names)) != len(names):
        raise ValueError(f"{where}: {who}: padstack names a layer twice: {tuple(names)!r}")
    if mode == "front_inner_back":
        if sorted(names) != ["B.Cu", "Inner"]:
            raise ValueError(f"{where}: {who}: a front_inner_back padstack holds the 'Inner' and 'B.Cu' sizes (F.Cu's is size=), and KiCad writes exactly those two; got {tuple(names)!r}")
        return (mode, tuple(sorted(per, key=lambda e: e[0] != "Inner")))
    if "F.Cu" in names:
        raise ValueError(f"{where}: {who}: a custom padstack has no F.Cu entry: F.Cu's size is the via's size=, and KiCad's save drops the entry; move it to size=")
    bad = [la for la in names if _stack_key(la)[0] == 3]
    if bad:
        raise ValueError(f"{where}: {who}: padstack layer {bad[0]!r} is not a copper layer (In<n>.Cu or B.Cu)")
    return (mode, tuple(sorted(per, key=lambda e: _stack_key(e[0]))))


def _via_drill(who: str, key: str, value) -> tuple | None:
    """`backdrill`/`tertiary_drill` as `(size, (from, to))`; the layer names are checked in `load_core`."""
    if value is None:
        return None
    where = _where() or who
    if not isinstance(value, (tuple, list)) or len(value) != 2 or not isinstance(value[1], (tuple, list)) or len(value[1]) != 2:
        raise ValueError(f"{where}: {who}: {key} is (size, (from layer, to layer)), got {value!r}")
    return (_len(value[0], who, f"{key} size"), (_text(value[1][0], f"{where}: {who}: {key} layer"), _text(value[1][1], f"{where}: {who}: {key} layer")))


def _teardrops(who: str, value) -> dict | None:
    """A via's `(teardrops ...)` with KiCad's names, every key KiCad writes filled from its defaults
    (`layout_emit.TEARDROP_DEFAULTS`, measured) and every value of the type KiCad keeps: an unknown key
    or a string for a yes/no used to be a `ValueError` traceback out of the build or a line refused
    after routing (fifth review)."""
    if value is None:
        return None
    from .layout_emit import TEARDROP_DEFAULTS

    where = _where() or who
    if not isinstance(value, dict):
        raise ValueError(f"{where}: {who}: teardrops is a dict of KiCad's (teardrops ...) fields, got {value!r}")
    unknown = sorted(set(map(str, value)) - set(TEARDROP_DEFAULTS))
    if unknown:
        raise ValueError(f"{where}: {who}: teardrops has no field {unknown}; KiCad's are {list(TEARDROP_DEFAULTS)}")
    out = {}
    for k, d in TEARDROP_DEFAULTS.items():
        v = value.get(k, d)
        if isinstance(d, bool):
            out[k] = _bool(who, f"teardrops[{k!r}]", v)
        elif k.endswith("_ratio"):
            out[k] = _real(v, who, f"teardrops[{k!r}]")
        else:
            out[k] = _len(v, who, f"teardrops[{k!r}]")
    return out


def Pour(
    net: str | None,
    *,
    layer: str | None = "B.Cu",
    layers: Sequence[str] | None = None,
    points: Sequence[tuple[float, float]] | None = None,
    holes: Sequence[Sequence[tuple[float, float]]] = (),
    connect: str = "thermal",
    clearance: float | None = None,
    min_thickness: float | None = None,
    filled_areas_thickness: bool | None = None,
    hatch: tuple[str, float] = ("edge", 0.5),
    priority: int | None = None,
    name: str | None = None,
    locked: bool = False,
    teardrop_type: str | None = None,
    keepout: dict | None = None,
    placement: dict | None = None,
    filled: bool | None = None,
    fill_mode: str | None = None,
    thermal_gap: float | None = None,
    thermal_bridge_width: float | None = None,
    smoothing: str | None = None,
    radius: float | None = None,
    island_removal_mode: int | None = None,
    island_area_min: float | None = None,
    hatch_thickness: float | None = None,
    hatch_gap: float | None = None,
    hatch_orientation: float | None = None,
    hatch_smoothing_level: int | None = None,
    hatch_smoothing_value: float | None = None,
    hatch_border_algorithm: str | None = None,
    hatch_min_hole_area: float | None = None,
    hatch_position: dict | None = None,
    uuid: str | None = None,
    id: str | None = None,
    comment: str = "",
) -> Copper:
    """One KiCad `(zone)`, field for field: a pour, or with `keepout=` a rule area.

    `net=None` is a zone with no `(net)` (a rule area). `layer` is `(layer X)`; `layers=` is the
    `(layers ...)` form KiCad writes for a multi-layer rule area, and then `layer` must be None.
    `connect` is `connect_pads`' mode (thermal = no atom, solid = `yes`, none = `no`,
    thru_hole_only) and `clearance` its `(clearance)`. The `(fill ...)` block is `filled` (KiCad's
    `yes` atom) plus `fill_mode`, `thermal_gap`, `thermal_bridge_width`, `smoothing`, `radius`,
    `island_removal_mode`, `island_area_min` and the `hatch_*` settings. `points` is the first
    `(polygon)` and `holes` every later one, in file order; an entry of either is a point `(x, y)`
    or KiCad's `(arc (start) (mid) (end))` as `((sx, sy), (mx, my), (ex, ey))`. `hatch_position` is
    the per-layer `(property (layer "X") (hatch_position (xy x y)))`, as `{"X": (x, y)}`. The
    `(filled_polygon ...)` KiCad writes is not a field: it is the fill, which
    `kicad-cli --refill-zones` regenerates from these.

    Empty `points` means the board rectangle, inset at emit time (a hand-written core line only).
    """
    who = f"Pour({net!r})"
    from .layout_prims import lset_order

    where = _where() or who
    _word(who, "connect", connect, "pour.connect", none_ok=False)
    if net == "":
        net = None  # a zone on no net: KiCad writes no `(net)` for "" either, and reads back None (measured)
    if net is not None:
        _text(net, f"{where}: {who}: net")
    if layers is not None:
        if isinstance(layers, str) or not isinstance(layers, (tuple, list)):
            raise ValueError(f"{where}: {who}: layers= is a list of layer names, got {layers!r}")
        layers = tuple(_text(x, f"{where}: {who}: layers") for x in layers)
        if len(set(layers)) != len(layers) or not layers:
            raise ValueError(f"{where}: {who}: layers= names each layer once, got {tuple(layers)!r}")
        if len(layers) == 1 and "*" not in layers[0] and "&" not in layers[0]:
            # One layer is `(layer X)`: KiCad's save writes a one-layer zone that way (measured), so the
            # line is read as it will come back.
            layer, layers = layers[0], None
        else:
            layer = None
    if layer is not None:
        layer = _text(layer, f"{where}: {who}: layer")
    pts = tuple(_pt_entry(p, who) for p in (points or ()))
    if pts:
        # A zone is an area: fewer than three distinct vertices, or no area at all, shipped a
        # degenerate zone "verified" (sixth review, `pour_2pts`). `Poly` refuses the same.
        from .layout_prims import flat_points

        flat = list(dict.fromkeys(flat_points(pts)))
        area = abs(sum(flat[i][0] * flat[(i + 1) % len(flat)][1] - flat[(i + 1) % len(flat)][0] * flat[i][1] for i in range(len(flat)))) / 2.0 if len(flat) >= 3 else 0.0
        if len(flat) < 3 or area <= 0.0:
            raise ValueError(f"{where}: {who}: a pour outline needs at least three points enclosing an area, got {len(flat)} distinct point(s){' on one line' if len(flat) >= 3 else ''}")
    if hatch_position is not None and not isinstance(hatch_position, dict):
        raise ValueError(f"{_where() or who}: {who}: hatch_position is a dict of layer -> (x, y)")
    if filled_areas_thickness is not None:
        # KiCad 10.0.6 never writes it back: True and False both come back None (measured).
        raise ValueError(f"{where}: {who}: filled_areas_thickness is a KiCad 5 field KiCad 10 drops on save (True and False both come back unset); leave it out")
    if teardrop_type is not None:
        # A teardrop zone is KiCad's own derived object, regenerated by its refill from the vias' and
        # pads' teardrop settings; a zone written with one is dropped whole by KiCad's load (measured,
        # `padvia`: "the router dropped it"). The teardrop is a `Via(teardrops=...)` field.
        raise ValueError(f"{where}: {who}: teardrop_type={teardrop_type!r} marks KiCad's own teardrop zone, which its refill regenerates and its load drops when handed one; set teardrops= on the via instead, or leave it out")
    priority = _int(priority, who, "priority")
    if priority == 0:
        priority = None  # KiCad writes no `(priority)` for 0, its default (measured)
    if name is not None:
        name = _text(name, f"{where}: {who}: name") or None  # KiCad writes no `(name)` for "" (measured)
    # Every word-valued field against KiCad's own list, at load (C3). `WORDS` is the one source.
    if not (isinstance(hatch, (tuple, list)) and len(hatch) == 2):
        raise ValueError(f"{_where() or who}: {who}: hatch is (style, pitch)")
    _word(who, "hatch style", str(hatch[0]), "pour.hatch", none_ok=False)
    _word(who, "fill_mode", fill_mode, "pour.fill_mode")
    _word(who, "smoothing", smoothing, "pour.smoothing")
    _word(who, "hatch_border_algorithm", hatch_border_algorithm, "pour.hatch_border_algorithm")
    _word(who, "island_removal_mode", island_removal_mode, "pour.island_removal_mode")
    _word(who, "hatch_smoothing_level", hatch_smoothing_level, "pour.hatch_smoothing_level")
    _word(who, "teardrop_type", teardrop_type, "pour.teardrop_type")
    if keepout is not None:
        from .layout_emit import KEEPOUT_KEYS

        unknown = set(map(str, keepout)) - set(KEEPOUT_KEYS)
        if unknown:
            raise ValueError(f"{_where() or who}: {who}: keepout has no key {sorted(unknown)}; KiCad's are {list(KEEPOUT_KEYS)}")
        for k, v in keepout.items():
            _word(who, f"keepout[{k!r}]", str(v), "pour.keepout", none_ok=False)
    if placement is not None:
        from .layout_emit import PLACEMENT_KEYS

        if not isinstance(placement, dict):
            raise ValueError(f"{where}: {who}: placement is a dict of KiCad's (placement ...) fields")
        unknown = set(map(str, placement)) - set(PLACEMENT_KEYS)
        if unknown:
            raise ValueError(f"{_where() or who}: {who}: placement has no key {sorted(unknown)}; KiCad's are {list(PLACEMENT_KEYS)}")
        for k, v in placement.items():
            if k == "enabled":
                _bool(who, "placement['enabled']", v)
            else:
                # KiCad keeps these as strings; an integer sheet name dropped the whole zone (fifth review).
                _text(v, f"{where}: {who}: placement[{k!r}]")
    locked = _bool(who, "locked", locked)
    filled = _bool(who, "filled", filled, none_ok=True)
    return _copper(
        Copper(
            "pour",
            id or f"pour-{net}-{layer or '+'.join(layers or ())}",
            None if net is None else str(net),
            layer=None if layer is None else str(layer),
            # In the order KiCad's save writes `(layers ...)`: by layer id (C3).
            layers=None if layers is None else lset_order(layers),
            points=pts,
            holes=tuple(tuple(_pt_entry(q, who) for q in h) for h in holes),
            connect=connect,
            clearance=_len(clearance, who, "clearance"),
            min_thickness=_len(min_thickness, who, "min_thickness"),
            filled_areas_thickness=None,
            hatch=(str(hatch[0]), _len(hatch[1], who, "hatch pitch")),
            priority=priority,
            name=name,
            locked=locked,
            teardrop_type=None,
            keepout=None if keepout is None else {str(k): str(v) for k, v in keepout.items()},
            placement=None if placement is None else dict(placement),
            filled=filled,
            fill_mode=fill_mode,
            thermal_gap=_len(thermal_gap, who, "thermal_gap"),
            thermal_bridge_width=_len(thermal_bridge_width, who, "thermal_bridge_width"),
            smoothing=smoothing,
            radius=_len(radius, who, "radius"),
            island_removal_mode=_int(island_removal_mode, who, "island_removal_mode"),
            island_area_min=_real(island_area_min, who, "island_area_min"),
            hatch_thickness=_len(hatch_thickness, who, "hatch_thickness"),
            hatch_gap=_len(hatch_gap, who, "hatch_gap"),
            hatch_orientation=_real(hatch_orientation, who, "hatch_orientation"),
            hatch_smoothing_level=_int(hatch_smoothing_level, who, "hatch_smoothing_level"),
            hatch_smoothing_value=_real(hatch_smoothing_value, who, "hatch_smoothing_value"),
            hatch_border_algorithm=hatch_border_algorithm,
            hatch_min_hole_area=_real(hatch_min_hole_area, who, "hatch_min_hole_area"),
            hatch_position=None if hatch_position is None else {_text(k, f"{where}: {who}: hatch_position layer"): _pt(v, f"{who}: hatch_position[{k!r}]") for k, v in hatch_position.items()},
            uuid=_uuid(who, uuid),
            source=_layout_source,
            comment=comment,
        )
    )


def _check_words(a, value, who: str) -> None:
    """`value` against the Atom's KiCad word list (`layout_prims.WORDS` is the one source), for a
    word, a whole number or a list of words alike."""
    if not a.words:
        return
    if a.kind == "atoms":
        bad = [x for x in value if x not in a.words]
        if bad:
            raise ValueError(f"{who} holds {bad[0]!r}; KiCad's words are {', '.join(repr(w) for w in a.words)}")
    elif isinstance(value, bool) or value not in a.words:
        raise ValueError(f"{who} must be one of {', '.join(repr(w) for w in a.words)}, got {value!r}")


# KiCad's own order for a text's justification: horizontal, vertical, mirror (measured,
# `fix6/facts.py`: `("top", "left")` comes back `("left", "top")`, `("mirror", "left")` comes back
# `("left", "mirror")`), and one word per axis — `("left", "right")` comes back `("right",)`.
_JUSTIFY_AXIS = {"left": 0, "right": 0, "top": 1, "bottom": 1, "mirror": 2}
# The heads whose text effects carry `hide`, which KiCad 10 never writes on board text: `hide=True`
# comes back False on a text, a text box, a table cell and a dimension's text (measured).
_TEXT_HEADS = ("gr_text", "gr_text_box", "table_cell")
# Text height and width below this come back at it from KiCad's save (`(0.0001, 1)` -> `(0.001, 1)`).
_TEXT_MIN = 0.001
# A drawing on copper with its mask opened: KiCad's `(layers cu mask)` form, each copper side with its
# own mask, in that order (measured: `("B.Mask", "B.Cu")` comes back `("B.Cu", "B.Mask")`,
# `("F.Cu", "B.Mask")` comes back `("F.Cu", "F.Mask")`, three layers come back two, and an inner copper
# layer comes back as `(layer)` alone).
_CU_MASK = {frozenset(("F.Cu", "F.Mask")): ("F.Cu", "F.Mask"), frozenset(("B.Cu", "B.Mask")): ("B.Cu", "B.Mask")}


def _norm(schema, vals: dict, who: str) -> dict:
    """Every value in the one Python type the decompiler produces for its kind (a tuple, never a list;
    a float, never an int where KiCad writes a number), so a hand-written object, a `layout.gen.py`
    line and a decompiled KiCad primitive compare equal field for field. Every value KiCad's save
    would rewrite is either read here as KiCad keeps it (a length at 1 nm, a double to ten digits,
    `justify` in KiCad's order, a drawing's `layers` as (copper, its mask)) or refused naming the
    field (fifth review, C3)."""
    from .layout_prims import REQ, Atom, DictNode, Many, Props, Sub, _fields, complete

    out = dict(vals)
    for f in _fields(schema.slots):
        kw = f.kw
        v = out.get(kw)
        if v is None:
            continue
        if isinstance(f, Atom):
            out[kw] = _norm_atom(f.kind, v, f"{who}: {kw}")
            if kw == "uuid":
                from .layout_prims import uuid_check

                out[kw] = uuid_check(out[kw], _where() or who)
            else:
                _check_words(f, out[kw], f"{who}: {kw}")
            if kw == "justify":
                out[kw] = _justify(out[kw], f"{who}: {kw}")
            if f.kind == "reals" and kw.endswith("color") and len(out[kw]) != 4:
                raise ValueError(f"{who}: {kw} is (r, g, b, a), four numbers — KiCad's parser refuses the board without the alpha — got {v!r}")
        elif isinstance(f, DictNode):
            if not isinstance(v, dict):
                raise ValueError(f"{who}: {kw} is a dict of KiCad's ({f.tok} ...) fields")
            atoms = {a.kw: a for a in _fields(f.slots) if isinstance(a, Atom)}
            unknown = set(v) - set(atoms)
            if unknown:
                raise ValueError(f"{who}: {kw} has no key {sorted(unknown)}; KiCad's are {sorted(atoms)}")
            full = {}
            for k, a in atoms.items():
                if k in v:
                    full[k] = None if v[k] is None else _norm_atom(a.kind, v[k], f"{who}: {kw}[{k!r}]")
                    if full[k] is not None:
                        # The words check the Atom branch makes, here too: until the fifth review a
                        # dimension's `arrow_direction` and a table border's `type` bypassed it and
                        # died in a `kicad-cli` traceback after routing.
                        _check_words(a, full[k], f"{who}: {kw}[{k!r}]")
                        if a.kind == "reals" and k.endswith("color") and len(full[k]) != 4:
                            raise ValueError(f"{who}: {kw}[{k!r}] is (r, g, b, a), four numbers — KiCad's parser refuses the board without the alpha — got {v[k]!r}")
                elif a.omit is REQ:
                    raise ValueError(f"{who}: {kw} needs {k!r} (KiCad always writes it)")
                else:
                    full[k] = a.omit  # the value at which KiCad writes nothing: what its save reads back
            out[kw] = full
        elif isinstance(f, Sub):
            out[kw] = _norm(f.schema, complete(f.schema, dict(v)), f"{who}: {kw}")
        elif isinstance(f, Many):
            out[kw] = tuple(_norm(f.schema, complete(f.schema, dict(d)), f"{who}: {kw}[{i}]") for i, d in enumerate(v))
        elif isinstance(f, Props):
            bag = {}
            for k, x in dict(v).items():
                if not str(k).isidentifier():
                    raise ValueError(f"{who}: property {k!r} is not a name")
                if isinstance(x, bool):
                    bag[str(k)] = x
                elif isinstance(x, str):
                    bag[str(k)] = _text(x, f"{who}: property {k!r}")
                elif isinstance(x, (int, float)):
                    bag[str(k)] = _dbl(_finite(x, f"{who}: property {k!r}"))
                elif isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in x):
                    bag[str(k)] = _pt(x, f"{who}: property {k!r}")
                elif isinstance(x, (list, tuple)) and x and all(isinstance(p, (list, tuple)) for p in x):
                    bag[str(k)] = tuple(_pt(p, who) for p in x)
                else:
                    raise ValueError(f"{who}: property {k!r} is not a number, string, yes/no, (x, y) or a list of points")
            out[kw] = bag
    if schema.head in _TEXT_HEADS and out.get("hide"):
        raise ValueError(f"{who}: hide=True is not kept on board text: KiCad 10 writes no (hide) on a text, a text box, a table cell or a dimension's text, and its save reads it back False; delete the text, or move it to a layer that is not plotted")
    if schema.head in _TEXT_HEADS and out.get("size") is not None and min(out["size"]) < _TEXT_MIN:
        raise ValueError(f"{who}: size {out['size']!r}: a text's height and width are at least {_TEXT_MIN} mm (KiCad's save raises anything smaller to that)")
    if schema.head in _TEXT_HEADS and out.get("thickness") is not None and out["thickness"] <= 0:
        raise ValueError(f"{who}: thickness={out['thickness']:g}: KiCad writes no stroke thickness for 0 or less (its default is used); give a positive thickness or leave it out")
    if "layers" in out and out.get("layers") is not None:
        lay = tuple(out["layers"])
        if len(lay) == 1:
            out["layer"], out["layers"] = lay[0], None
        else:
            pair = _CU_MASK.get(frozenset(lay)) if len(lay) == 2 else None
            if pair is None:
                raise ValueError(f"{who}: layers={lay!r}: a drawing's layers= is KiCad's form for copper with its mask opened, ('F.Cu', 'F.Mask') or ('B.Cu', 'B.Mask'), and KiCad keeps no other (a mismatched side, an inner layer or a third layer is rewritten); one layer is layer=")
            out["layer"], out["layers"] = None, pair
    return out


def _justify(words: tuple, who: str) -> tuple:
    """`justify` in KiCad's order (horizontal, vertical, mirror), one word per axis."""
    seen: dict[int, str] = {}
    for w in words:
        axis = _JUSTIFY_AXIS[w]
        if axis in seen:
            if seen[axis] == w:
                raise ValueError(f"{who} names {w!r} twice; KiCad keeps it once")
            raise ValueError(f"{who} names both {seen[axis]!r} and {w!r}; they are one axis and KiCad keeps only the last ({w!r}): write one")
        seen[axis] = w
    return tuple(seen[a] for a in sorted(seen))


def _seq(v, who: str) -> list:
    if isinstance(v, (str, bytes, dict)) or not isinstance(v, (list, tuple)):
        raise ValueError(f"{who} is a list, got {v!r}")
    return list(v)


def _norm_atom(kind: str, v, who: str):
    if kind == "xy":
        return _pt(v, who)
    if kind == "pts":
        return tuple(_pt_entry(p, who) for p in _seq(v, who))
    if kind == "len":
        return _nm(_finite(v, who))
    if kind == "real":
        return _dbl(_finite(v, who))
    if kind == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{who} is a whole number, got {v!r}")
        return v
    if kind in ("str", "sym"):
        return _text(v, who)
    if kind in ("yn", "flag"):
        if not isinstance(v, bool):
            raise ValueError(f"{who} is True or False, got {v!r}")
        return v
    if kind == "fill":
        # KiCad's save writes `yes`/`no` for a plain fill and keeps `hatch`/`reverse_hatch`/
        # `cross_hatch`; `solid` and `none` are read back as `yes` and `no` (measured, C3), so they
        # are normalised here to what KiCad keeps rather than refused after routing.
        from .layout_prims import WORDS

        if isinstance(v, bool):
            return v
        if v in WORDS["fill.plain"]:
            return v in ("yes", "solid")
        if v in WORDS["fill"]:
            return v
        raise ValueError(f"{who}: fill must be True, False, or one of {', '.join(repr(w) for w in (*WORDS['fill.plain'], *WORDS['fill']))}, got {v!r}")
    if kind == "lens":
        return tuple(_nm(_finite(x, who)) for x in _seq(v, who))
    if kind == "reals":
        return tuple(_dbl(_finite(x, who)) for x in _seq(v, who))
    if kind == "ints":
        out = _seq(v, who)
        if any(isinstance(x, bool) or not isinstance(x, int) for x in out):
            raise ValueError(f"{who} is a list of whole numbers, got {v!r}")
        return tuple(out)
    if kind in ("atoms", "strs", "refs"):
        return tuple(_text(x, who) for x in _seq(v, who))
    if kind == "blob":
        return "".join(_text(v, who).split())
    raise AssertionError(kind)


def _drawing(head: str, given: dict, *, id: str | None, comment: str) -> Graphic:
    """One board drawing: `given` completed from the schema's defaults, normalised, and recorded."""
    from .layout_prims import SCHEMAS, complete

    schema = SCHEMAS[head]
    who = schema.name
    if given.get("layers") is not None and "layers" in {f.kw for f in schema.fields()}:
        if given.get("layer") is not None:
            raise ValueError(f"{_where() or who}: {who}: give layer= or layers=, not both (layers= is the copper-with-mask form and replaces layer=)")
        given = {**given, "layer": None}
    try:
        vals = complete(schema, {k: v for k, v in given.items()})
    except ValueError as exc:
        raise ValueError(f"{_where() or who}: {exc}") from None
    g = Graphic(head, id or _gid(head), _norm(schema, vals, who), source=_layout_source, comment=comment)
    g.where = _where()
    _text(g.id, f"{g.where or who}: id")
    _check_drawing(g, who)
    _doc().graphics.append(g)
    return g


def _check_drawing(g: Graphic, who: str) -> None:
    """What one drawing may not say, beyond its fields' own types and words: the values KiCad's save
    would rewrite (fifth review, C3), each refused naming the value KiCad keeps instead."""
    f = g.f
    where = _where() or who
    if g.kind == "gr_rect" and f.get("radius") is not None:
        (x0, y0), (x1, y1) = f["start"], f["end"]
        half = min(abs(x1 - x0), abs(y1 - y0)) / 2.0
        if f["radius"] == 0:
            f["radius"] = None  # KiCad writes no (radius) for 0, its default (measured)
        elif f["radius"] < 0 or f["radius"] > half + 1e-9:
            raise ValueError(f"{where}: {who}: radius={f['radius']:g} on a {abs(x1 - x0):g} x {abs(y1 - y0):g} mm box: KiCad's save clamps a corner radius to half the short side ({half:g}); write that or less")
    if g.kind == "gr_poly":
        from .layout_prims import is_arc

        pts = list(f["pts"])
        for a, b in zip(pts, pts[1:]):
            if not is_arc(a) and not is_arc(b) and a == b:
                raise ValueError(f"{where}: {who}: the point {a!r} is written twice in a row, and KiCad's save drops the repeat; write it once")
    if g.kind == "image" and f.get("scale") is not None and f["scale"] < 0:
        raise ValueError(f"{where}: {who}: scale={f['scale']:g}: KiCad keeps no negative scale (its save drops it); give a scale of 0 or more")
    if g.kind == "image":
        import base64
        import binascii

        try:
            raw = base64.b64decode(f["data"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"{where}: {who}: data is not base64 (KiCad cannot read the image and refuses the board)") from None
        if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
            raise ValueError(f"{where}: {who}: data is not a PNG or JPEG file (KiCad reads 'Unknown image data format' and refuses the board)")
    if g.kind == "barcode":
        from .layout_prims import WORDS

        w, h = f["size"]
        if min(w, h) < 0.01:
            raise ValueError(f"{where}: {who}: size {f['size']!r}: a barcode is at least 0.01 mm each way (KiCad's save raises anything smaller to that)")
        level = f.get("ecc_level")
        if f["kind"] in ("qr", "microqr"):
            table = "barcode.ecc_level.microqr" if f["kind"] == "microqr" else "barcode.ecc_level"
            if level is None or level not in WORDS[table]:
                raise ValueError(f"{where}: {who}: a {f['kind']} code's ecc_level is one of {', '.join(repr(x) for x in WORDS[table])}, got {level!r}" + (" (KiCad's save turns a micro QR's H into Q)" if f["kind"] == "microqr" and level == "H" else ""))
        elif level is not None:
            raise ValueError(f"{where}: {who}: a {f['kind']} code has no error-correction level, and KiCad's save drops ecc_level={level!r}; leave it out")
    if g.kind == "group":
        mem = list(f["members"])
        dup = sorted({m for m in mem if mem.count(m) > 1})
        if dup:
            raise ValueError(f"{where}: {who}: member {dup[0]!r} is named twice, and KiCad keeps it once; name it once")
        if f.get("lib_id") is not None:
            _lib_id(f["lib_id"], f"{where}: {who}: lib_id")
    if g.kind == "generated" and f.get("type") == "tuning_pattern":
        from .layout_prims import WORDS

        # A tuning pattern's properties are KiCad's own set, each of one type: KiCad drops a key it has
        # no property for and rewrites a value of another type (measured, `fix6/facts3.py`:
        # `bogus_prop` gone, `max_amplitude "2"` and `rounded 1` rewritten).
        known = {**TUNING_PATTERN_DEFAULTS, **TUNING_PATTERN_OPTIONAL}
        for key, v in (f.get("props") or {}).items():
            if key not in known and _layout_source == "gen":
                continue  # KiCad wrote it: a property this list does not know is still KiCad's own
            if key not in known:
                raise ValueError(f"{where}: {who}: a tuning pattern has no property {key!r}, and KiCad's save drops it; its properties are {', '.join(sorted(known))}")
            d = known[key]
            kind_ok = isinstance(v, bool) if isinstance(d, bool) else isinstance(v, str) if isinstance(d, str) else isinstance(v, tuple) if isinstance(d, tuple) else isinstance(v, float) and not isinstance(v, bool)
            if not kind_ok:
                what = "True or False" if isinstance(d, bool) else "a string" if isinstance(d, str) else "a point (x, y)" if isinstance(d, tuple) else "a number"
                raise ValueError(f"{where}: {who}: props[{key!r}] is {what} (KiCad rewrites anything else), got {v!r}")
        for key in ("initial_side", "tuning_mode"):
            v = (f.get("props") or {}).get(key)
            if v is not None and v not in WORDS[f"generated.tuning_pattern.{key}"]:
                raise ValueError(f"{where}: {who}: props[{key!r}] must be one of {', '.join(repr(x) for x in WORDS[f'generated.tuning_pattern.{key}'])}, got {v!r} (KiCad rewrites anything else to its default)")
    if g.kind == "dimension":
        _check_dimension(g, who, where)
    if g.kind == "table":
        cols, widths, heights, cells = f["column_count"], f.get("column_widths") or (), f.get("row_heights") or (), f.get("cells") or ()
        if cols < 1 or len(widths) != cols or len(heights) != len(cells) // cols:
            # KiCad rebuilds the grid from column_count and the cells: a width per column and a height
            # per full row of cells (measured: 3 columns with one width and one cell comes back with
            # widths (10, 0, 0) and no row heights; a KiCad-saved 3-column table of 5 spanned cells
            # carries 3 widths and 1 row height, `refute-primitives-r3/hw/hw3_drawings`).
            raise ValueError(f"{where}: {who}: column_count={cols} needs {cols} column_widths (got {len(widths)}) and a row height per full row of cells ({len(cells)} cells / {cols} = {len(cells) // cols}, got {len(heights)}); KiCad rebuilds the grid from these")
        for i, cell in enumerate(cells):
            if cell.get("layer") != f.get("layer"):
                raise ValueError(f"{where}: {who}: cell {i} is on {cell.get('layer')!r} and the table on {f.get('layer')!r}; KiCad keeps a table's cells on its layer, so write layer={f.get('layer')!r} on the cell")


def _lib_id(value: str, who: str) -> str:
    """A group's `lib_id`, KiCad's `library:item` link: one colon with a name on both sides, and none
    of the characters KiCad's parser refuses in it (a quote, a backslash, a control character) — a
    link without its colon comes back unset from KiCad's save, and one with a refused character
    fails the board's load (measured, `fix6/facts.py` and the fifth review's `S_withlibid`)."""
    bad = next((ch for ch in value if ch in '"\\' or ord(ch) < 0x20 or ord(ch) == 0x7F), None)
    if bad is not None:
        raise ValueError(f"{who} {value!r} holds {bad!r}, which KiCad refuses in a library link (the board fails to load)")
    parts = value.split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"{who} {value!r} is not a KiCad library link 'library:item' (one colon, a name on each side); KiCad's save drops anything else")
    return value


# What a dimension carries by kind, measured (`fix6/facts.py`): KiCad writes `height` on aligned and
# orthogonal dimensions only, `orientation` on orthogonal only, `leader_length` on radial only, and
# drops each written on another kind.
_DIM_ONLY = {"height": ("aligned", "orthogonal"), "orientation": ("orthogonal",), "leader_length": ("radial",)}
# The same for a dimension's style, measured: `arrow_direction` is written on aligned and orthogonal
# dimensions only (dropped from a radial, leader or center one), and `extension_height` and
# `text_frame` on another kind than theirs make KiCad refuse the board ("Expecting '('").
_DIM_STYLE_ONLY = {"arrow_direction": ("aligned", "orthogonal"), "extension_height": ("aligned", "orthogonal"), "text_frame": ("leader",)}


def _an(word: str) -> str:
    return ("an " if word[:1] in "aeiou" else "a ") + word


def _check_dimension(g: Graphic, who: str, where: str) -> None:
    f = g.f
    kind = f["kind"]
    for key, kinds in _DIM_ONLY.items():
        if f.get(key) is not None and kind not in kinds:
            raise ValueError(f"{where}: {who}: {key} is a field of {' and '.join(kinds)} dimensions only; KiCad's save drops it from {_an(kind)} one — leave it out")
    style = f.get("style") or {}
    for key, kinds in _DIM_STYLE_ONLY.items():
        if style.get(key) is not None and kind not in kinds:
            raise ValueError(f"{where}: {who}: style[{key!r}] is a field of {' and '.join(kinds)} dimensions only; KiCad drops it from {_an(kind)} one or refuses the board — leave it out")
    if style.get("keep_text_aligned") is False:
        # KiCad writes (keep_text_aligned) only when it is yes; no is its default and its save writes
        # nothing (measured on every kind). Read as KiCad keeps it.
        f["style"] = {**style, "keep_text_aligned": None}
        style = f["style"]
    if kind in ("aligned", "orthogonal"):
        missing = [k for k in ("arrow_direction", "extension_height") if style.get(k) is None]
        if missing:
            raise ValueError(f"{where}: {who}: a {kind} dimension's style carries {' and '.join(missing)}, which KiCad's save fills in when it is left out (arrow_direction 'outward', extension_height from the text); write it")
    txt = f.get("gr_text")
    if txt is not None:
        if txt.get("layer") != f.get("layer"):
            raise ValueError(f"{where}: {who}: the dimension is on {f.get('layer')!r} and its text on {txt.get('layer')!r}; KiCad keeps a dimension on its text's layer, so they are one layer: write gr_text['layer'] = {f.get('layer')!r}")
        if bool(txt.get("locked")) != bool(f.get("locked")):
            raise ValueError(f"{where}: {who}: locked={f.get('locked')} and its text's locked={txt.get('locked')}; KiCad's save locks both when either is locked, so they are one flag: write the same on both")
        if txt.get("uuid") is not None and txt["uuid"] != f.get("uuid"):
            raise ValueError(f"{where}: {who}: its text's uuid {txt['uuid']!r} is not the dimension's own ({f.get('uuid')!r}); KiCad gives a dimension one uuid and keeps the text's, so they are one: write the same uuid on both, or leave the text's out")


def _gid(kind: str) -> str:
    n = sum(1 for g in _doc().graphics if g.kind == kind)
    return f"{kind}-{n}"


def _given(**kw) -> dict:
    """The keywords a call actually passed: `_UNSET` means "not given, take the schema's default"."""
    return {k: v for k, v in kw.items() if v is not _UNSET}


class _Unset:
    def __repr__(self) -> str:
        return "<default>"


_UNSET = _Unset()


def Line(start, end, *, layer=_UNSET, width=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_line`, field for field (`layout_prims.SCHEMAS["gr_line"]`): `start`, `end`, the
    stroke's `width`/`stroke_type`/`stroke_color`, `locked`, `layer` (or `layers=(cu, mask)` on copper
    with its mask opened), `solder_mask_margin`, `net`, `uuid`. On any board layer, `Edge.Cuts` included."""
    return _drawing("gr_line", {"start": start, "end": end, **_given(layer=layer, width=width), **fields}, id=id, comment=comment)


def Rect(start, end, *, layer=_UNSET, width=_UNSET, fill=_UNSET, radius=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_rect`: `start`, `end`, `radius` (the corner radius), the stroke, `fill`, `locked`,
    `layer`/`layers`, `solder_mask_margin`, `net`, `uuid`. KiCad stores the corners as (min x, min y) and
    (max x, max y) whichever two opposite corners it is handed; so does this, so the object is what
    KiCad's save reads back."""
    a, b = _pt(start, "Rect"), _pt(end, "Rect")
    start, end = (min(a[0], b[0]), min(a[1], b[1])), (max(a[0], b[0]), max(a[1], b[1]))
    return _drawing("gr_rect", {"start": start, "end": end, **_given(layer=layer, width=width, fill=fill, radius=radius), **fields}, id=id, comment=comment)


def Circle(center, radius: float | None = None, *, end=_UNSET, layer=_UNSET, width=_UNSET, fill=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_circle`: `center` and `end`, a point on the circle, as KiCad stores it. `radius`
    is input only: it writes `end=(cx + radius, cy)`, and `layout.gen.py` writes `end` itself."""
    who = "Circle"
    c = _pt(center, who)
    if end is _UNSET:
        if radius is None:
            raise ValueError(f"{who}: give radius= or end=")
        end = (c[0] + float(radius), c[1])
    elif radius is not None:
        raise ValueError(f"{who}: give radius= or end=, not both")
    return _drawing("gr_circle", {"center": c, "end": end, **_given(layer=layer, width=width, fill=fill), **fields}, id=id, comment=comment)


def DrawArc(start, mid, end, *, layer=_UNSET, width=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_arc` (a drawn arc, not a track): `start`, `mid`, `end`, the stroke, `locked`, `layer`,
    `net`, `uuid`. KiCad stores a drawn arc turning one way only (start -> mid -> end clockwise on the
    screen, y down) and swaps `start` and `end` of one handed to it the other way (measured with
    `kicad-cli pcb upgrade`); this does the same, so the object is what KiCad's save reads back. The
    arc is the same arc either way. A track `Arc` is never swapped: a track has a direction."""
    s, m, e = _pt(start, "DrawArc"), _pt(mid, "DrawArc"), _pt(end, "DrawArc")
    if (m[0] - s[0]) * (e[1] - m[1]) - (m[1] - s[1]) * (e[0] - m[0]) < 0:
        s, e = e, s
    return _drawing("gr_arc", {"start": s, "mid": m, "end": e, **_given(layer=layer, width=width), **fields}, id=id, comment=comment)


def Poly(pts, *, layer=_UNSET, width=_UNSET, fill=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_poly`: `pts`, the stroke, `fill`, `locked`, `layer`/`layers`, `solder_mask_margin`, `net`, `uuid`."""
    if len(tuple(pts)) < 3:
        raise ValueError("Poly: needs at least three points")
    return _drawing("gr_poly", {"pts": pts, **_given(layer=layer, width=width, fill=fill), **fields}, id=id, comment=comment)


def Curve(pts, *, layer=_UNSET, width=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_curve`, a cubic Bezier: `pts` is start, control 1, control 2, end."""
    if len(tuple(pts)) != 4:
        raise ValueError("Curve: a cubic Bezier is four points: start, control, control, end")
    return _drawing("gr_curve", {"pts": pts, **_given(layer=layer, width=width), **fields}, id=id, comment=comment)


def _size2(v, order: str = "(height, width)") -> tuple[float, float]:
    """A text size: one number for both, or (height, width). Anything else is refused with that
    sentence (sixth review: `size=True` became (1, 1), `size=(1, 2, 3)` dropped a value silently, and
    `size="1"` or `size=(1,)` raised a bare IndexError)."""
    def num(x) -> float:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise ValueError(f"size is {order} in millimetres, or one number for both; got {v!r}")
        return float(x)

    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (num(v), num(v))
    if not isinstance(v, (tuple, list)) or len(v) != 2:
        raise ValueError(f"size is {order} in millimetres, or one number for both; got {v!r}")
    return (num(v[0]), num(v[1]))


def Text(text: str, at, *, layer=_UNSET, size=_UNSET, angle=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_text`: the string, `locked`, `at` and `angle`, `layer` and `knockout`, `uuid`, and
    the effects KiCad writes: `face`, `size` (height, width; a number means both), `line_spacing`,
    `thickness`, `bold`, `italic`, `justify` (KiCad's own words), `hide`. KiCad's `render_cache` (an
    outline font's polygons) is not a field: KiCad regenerates it on save."""
    return _drawing(
        "gr_text",
        {"text": _text(text, "Text: text"), "at": at, **_given(layer=layer, angle=angle), **({} if size is _UNSET else {"size": _size2(size)}), **fields},
        id=id,
        comment=comment,
    )


def TextBox(text: str, start=None, end=None, *, layer=_UNSET, size=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `gr_text_box`: the string, `locked`, `start`/`end` (or `pts`, the four corners KiCad
    writes for a turned box), `margins`, `angle`, `layer`, `uuid`, the text effects (as `Text`),
    `border`, the stroke, `knockout`."""
    given = {"text": _text(text, "TextBox: text"), "start": start, "end": end, **_given(layer=layer), **({} if size is _UNSET else {"size": _size2(size)}), **fields}
    if "margins" not in given:
        # KiCad writes every text box's margins, and one left out gets its own default: three quarters
        # of the text height plus half the border stroke (measured, `kicad-cli pcb upgrade`: 0.825 for
        # 1 mm text and a 0.15 stroke, 1.225 for 1.5 mm and 0.2).
        from .layout_prims import SCHEMAS

        h = float((given.get("size") or (1.0, 1.0))[0])
        w = float(given.get("width", next(f.default for f in SCHEMAS["gr_text_box"].fields() if f.kw == "width")))
        m = round(h * 0.75 + w / 2.0, 6)
        given["margins"] = (m, m, m, m)
    return _drawing("gr_text_box", given, id=id, comment=comment)


def Dimension(
    a=None,
    b=None,
    *,
    kind: str = "aligned",
    layer: str = "Dwgs.User",
    height=_UNSET,
    orientation=_UNSET,
    text: str = "",
    id: str | None = None,
    comment: str = "",
    **fields,
) -> Graphic:
    """One KiCad `dimension`: `kind` (its `(type)`), `locked`, `layer`, `uuid`, `pts` (the two points;
    `a`, `b` are the same), `height` (aligned, orthogonal), `orientation` (orthogonal: 0 horizontal,
    1 vertical), `leader_length` (radial), `format` and `style` (dicts keyed by KiCad's own tokens), and
    `gr_text`, the dimension's own text as a dict of `Text`'s fields.

    Left out, `format`, `style` and `gr_text` get the values KiCad's editor draws a new dimension with;
    `text=` is the override KiCad shows in place of the measurement. KiCad recomputes the text's
    string, and its position unless `style["text_position_mode"]` is 2, on every save."""
    who = "Dimension"
    _word(who, "kind", kind, "dimension.kind", none_ok=False)
    pts = fields.pop("pts", None)
    if pts is None and b is None and isinstance(a, (tuple, list)) and len(a) == 2 and all(isinstance(p, (tuple, list)) for p in a):
        # The schema's positional, `pts`, as `layout.gen.py` writes it: `Dimension(((x0, y0), (x1, y1)), ...)`
        # (third review P3: a gen Dimension line did not load back).
        pts = a
    if pts is None:
        if a is None or b is None:
            raise ValueError(f"{who}: give the two points")
        pts = (_pt(a, who), _pt(b, who))
    pts = tuple(_pt(p, who) for p in pts)
    given: dict = {"kind": kind, "layer": layer, "pts": pts}
    if kind in ("aligned", "orthogonal"):
        given["height"] = 2.0 if height is _UNSET else height
    elif height is not _UNSET:
        given["height"] = height
    if kind == "orthogonal":
        o = 0 if orientation is _UNSET else orientation
        if o not in (0, 1):
            raise ValueError(f"{who}: orthogonal orientation is 0 (horizontal) or 1 (vertical)")
        given["orientation"] = o
    elif orientation is not _UNSET:
        given["orientation"] = orientation
    if kind == "radial" and "leader_length" not in fields:
        given["leader_length"] = 3.81  # KiCad's default leader for a new radial dimension
    if kind != "center":
        fmt = {"prefix": "", "suffix": "", "units": 2, "units_format": 1, "precision": 4}
        if text or kind == "leader":
            fmt["override_value"] = str(text) or "Leader"  # a leader shows text, never a measurement
        given["format"] = fmt
        (x0, y0), (x1, y1) = pts
        span = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        given["gr_text"] = {"text": str(text) or f"{span:g} mm", "at": ((x0 + x1) / 2, (y0 + y1) / 2), "angle": 0.0, "layer": layer}
    # What KiCad 10.0.6 writes for a new dimension of each kind (measured with `kicad-cli pcb upgrade`
    # and the pcbnew probe): an arrow direction on the linear kinds, a text frame on a leader.
    style = {"thickness": 0.15, "arrow_length": 1.27, "text_position_mode": 0, "extension_offset": 0.5}
    if kind in ("aligned", "orthogonal"):
        style.update(arrow_direction="outward", extension_height=0.5, keep_text_aligned=True)
    if kind in ("radial", "center"):
        style.update(keep_text_aligned=True)
    if kind == "leader":
        style.update(text_frame=0)
    given["style"] = style
    given.update(fields)
    txt = given.get("gr_text")
    if isinstance(txt, dict):
        # A dimension and its text are one object to KiCad: one layer, one locked flag, one uuid. A
        # text dict that leaves them out takes the dimension's (fifth review: a hand-written
        # `gr_text={"text": ..., "at": ...}` took the text schema's F.SilkS and KiCad moved the whole
        # dimension there).
        fill = {"layer": given.get("layer"), "locked": given.get("locked", False)}
        if given.get("uuid") is not None:
            fill["uuid"] = given["uuid"]
        given["gr_text"] = {**{k: v for k, v in fill.items() if k not in txt}, **txt}
    return _drawing("dimension", given, id=id, comment=comment)


def Group(name: str, members: Sequence[str], *, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `group`: `name`, `uuid`, `locked`, `lib_id`, and `members`, the ids of other layout
    objects (KiCad writes their uuids, sorted)."""
    if not members:
        raise ValueError(f"Group({name!r}): needs at least one member id")
    # A name and member ids are strings, never coerced (sixth review: `Group(None, ...)` shipped a
    # group named "None").
    name = _text(name, f"Group({name!r}): name")
    if isinstance(members, str) or not isinstance(members, (tuple, list)):
        raise ValueError(f"Group({name!r}): members is a list of ids, got {members!r}")
    members = tuple(_text(m, f"Group({name!r}): member") for m in members)
    return _drawing("group", {"name": name, "members": members, **fields}, id=id or name, comment=comment)


def Image(at, data: str, *, layer=_UNSET, scale=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `image` (a reference image): `at`, `layer`, `scale`, `locked`, `data` (the PNG or JPEG,
    base64), `uuid`."""
    raw = "".join(str(data).split())
    if not raw:
        raise ValueError("Image: data is the base64-encoded file")
    sc = {} if scale is _UNSET or scale in (None, 1, 1.0) else {"scale": scale}
    return _drawing("image", {"at": at, "data": raw, **_given(layer=layer), **sc, **fields}, id=id, comment=comment)


def Table(at=None, rows=None, *, layer: str = "F.SilkS", col_mm: float = 10.0, row_mm: float = 2.54, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `table`: `column_count`, `uuid`, `locked`, `layer`, `border` and `separators` (dicts of
    KiCad's tokens), `column_widths`, `row_heights`, and `cells`, one dict per `table_cell` (its text,
    `start`/`end`, `margins`, `span`, `layer`, `uuid` and text effects).

    `at` and `rows` (a list of rows of strings) are the short form: a `col_mm` x `row_mm` grid from `at`."""
    who = "Table"
    given: dict = {"layer": layer}
    if rows is None and isinstance(at, int) and not isinstance(at, bool):
        # The schema's positional, `column_count`, as `layout.gen.py` writes it: `Table(2, cells=[...], ...)`
        # (third review P3: a gen Table line did not load back).
        given["column_count"] = at
        at = None
    if rows is not None:
        if not rows or not rows[0]:
            raise ValueError(f"{who}: needs at least one cell")
        cols = len(rows[0])
        if any(len(r) != cols for r in rows):
            raise ValueError(f"{who}: every row needs {cols} cells")
        x0, y0 = _pt(at if at is not None else (0.0, 0.0), who)
        cells = []
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                sx, sy = x0 + c * col_mm, y0 + r * row_mm
                # KiCad writes a cell's margins and span whatever it is handed: its default margin is
                # three quarters of the text height less one nanometre (measured: 0.749999 for 1 mm text,
                # 1.124999 for 1.5 mm), and a cell spans one row and one column.
                m = (round(1.0 * 750000) - 1) / 1e6
                cells.append({"text": str(value), "start": (sx, sy), "end": (sx + col_mm, sy + row_mm), "margins": (m, m, m, m), "span": (1, 1), "layer": layer})
        given.update(
            column_count=cols,
            border={"external": True, "header": True, "width": 0.15, "type": "solid"},
            separators={"rows": True, "cols": True, "width": 0.1, "type": "solid"},
            column_widths=(col_mm,) * cols,
            row_heights=(row_mm,) * len(rows),
            cells=cells,
        )
    given.update(fields)
    return _drawing("table", given, id=id, comment=comment)


# Other spellings of KiCad's barcode words (`layout_prims.WORDS["barcode.kind"]` is the list of words).
_BARCODE_ALIASES = {"qrcode": "qr", "qr_code": "qr", "micro_qr": "microqr", "code_39": "code39", "code_128": "code128", "data_matrix": "datamatrix"}


def Barcode(text: str, at, *, kind: str = "qr", size=_UNSET, text_mm=_UNSET, ecc=_UNSET, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `barcode`: `locked`, `at` and `angle`, `layer`, `size` (width, height; a number means
    both), `text`, `text_height` (`text_mm` is the same), `kind` (its `(type)`: qr, microqr, code39,
    code128, datamatrix), `ecc_level` (L, M, Q or H; qr and microqr; `ecc` is the same), `hide`,
    `knockout`, `margins`, `uuid`."""
    token = _word("Barcode", "kind", _BARCODE_ALIASES.get(kind, kind) if isinstance(kind, str) else kind, "barcode.kind", none_ok=False)
    given: dict = {"text": _text(text, f"{_where() or 'Barcode'}: Barcode: text"), "at": at, "kind": token}
    if size is not _UNSET:
        given["size"] = _size2(size, "(width, height)")
    if text_mm is not _UNSET:
        given["text_height"] = text_mm
    level = fields.pop("ecc_level", _UNSET)
    if level is _UNSET:
        level = ecc if ecc is not _UNSET else ("L" if token in ("qr", "microqr") else None)
    if level is not None:
        level = _word("Barcode", "ecc", level, "barcode.ecc_level", none_ok=False)
    given["ecc_level"] = level
    given.update(fields)
    return _drawing("barcode", given, id=id, comment=comment)


def Target(at, *, shape: str = "plus", id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `target` (an alignment mark): `shape` (plus or x), `at`, `size`, `width`, `layer`, `uuid`."""
    _word("Target", "shape", shape, "target.shape", none_ok=False)
    return _drawing("target", {"shape": shape, "at": at, **fields}, id=id, comment=comment)


def Point(at, *, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `point`: `at`, `size`, `layer`, `uuid`."""
    return _drawing("point", {"at": at, **fields}, id=id, comment=comment)


def Generated(type: str, name: str, members: Sequence[str], *, props: dict | None = None, id: str | None = None, comment: str = "", **fields) -> Graphic:
    """One KiCad `generated` object (a tuning pattern): `uuid`, `type`, `name`, `layer`, `props` (the
    generator's own `(key value)`s, KiCad's names: numbers, strings, yes/no, `(x, y)`, point lists)
    and `members`, the ids of the layout objects it made."""
    who = f"Generated({type!r})"
    if not str(type).isidentifier():
        raise ValueError(f"{who}: type must be a KiCad generator name")
    given_props = dict(props or {})
    if type == "tuning_pattern":
        # KiCad writes every property of a tuning pattern and names it itself; one left out takes the
        # value KiCad 10.0.6 gives a new one (measured with `kicad-cli pcb upgrade`). The `last_*`
        # properties are KiCad's record of its last tuning run, which it redoes on load.
        given_props = {**TUNING_PATTERN_DEFAULTS, **given_props}
        name = "Tuning Pattern"
    given_props = dict(sorted(given_props.items()))  # KiCad writes a generator's properties sorted
    return _drawing("generated", {"type": str(type), "name": str(name), "members": tuple(str(m) for m in members), "props": given_props, **fields}, id=id or str(name), comment=comment)


# The tuning pattern properties KiCad writes only on some patterns (measured on
# `tests/fixtures/kicad10/kicad10_primitives3.kicad_pcb`: `base_line`, a point list; the fifth review's
# `fp/generated_locked`: `locked`), with a value of the type each takes. A core line may use these and
# `TUNING_PATTERN_DEFAULTS`' keys; a `layout.gen.py` line carries whatever KiCad wrote.
TUNING_PATTERN_OPTIONAL = {"base_line": (), "base_line_coupled": (), "last_tuning": "", "locked": False}

TUNING_PATTERN_DEFAULTS = {
    "corner_radius_percent": 80.0,
    "end": (10.0, 0.0),
    "initial_side": "default",
    "is_time_domain": False,
    "last_diff_pair_gap": 0.0,
    "last_netname": "",
    "last_status": "tuned",
    "last_track_width": 0.0,
    "last_tuning_length": 0.0,
    "max_amplitude": 1.0,
    "min_amplitude": 0.2,
    "min_spacing": 0.6,
    "origin": (0.0, 0.0),
    "override_custom_rules": False,
    "rounded": False,
    "single_sided": False,
    "target_delay": 0.0,
    "target_delay_max": 0.1,
    "target_delay_min": -0.1,
    "target_length": 0.0,
    "target_length_max": 0.1,
    "target_length_min": -0.1,
    "target_skew": 0.0,
    "target_skew_max": 0.1,
    "target_skew_min": -0.1,
    "tuning_mode": "single",
}


def Region(
    name: str,
    position: str | None = None,
    style: str | None = None,
    parent: str | None = None,
    padding: object | None = None,
    **css,
) -> RegionSpec:
    who = f"Region({name!r})"
    if padding is not None:
        css = {**css, "padding": padding}
    st = _style(who, style=style, position=position or "absolute", parent=parent, **css)
    if not st.has_insets():
        raise ValueError(
            f"{who}: needs CSS left/top/right/bottom/width/height "
            "(a containing block has to have a box)."
        )
    spec = RegionSpec(name=str(name))
    spec = apply_style_to_spec(spec, st)
    _doc().regions.append(spec)
    return spec


def SchRegion(
    name: str,
    position: str | None = None,
    style: str | None = None,
    parent: str | None = None,
    padding: object | None = None,
    title: str | None = None,
    **css,
) -> RegionSpec:
    who = f"SchRegion({name!r})"
    if padding is not None:
        css = {**css, "padding": padding}
    st = _style(who, style=style, position=position or "absolute", parent=parent, **css)
    if not st.has_insets():
        raise ValueError(
            f"{who}: needs CSS left/top/right/bottom/width/height"
        )
    spec = RegionSpec(name=str(name))
    spec = apply_style_to_spec(spec, st)
    spec.title = title or str(name)
    _doc().sch_regions.append(spec)
    return spec


def SchPlace(
    ref: str,
    *,
    pin: str | None = None,
    to: str | None = None,
    along: str | None = None,
    gap: float | None = None,
    align: str | None = None,
    side: str | None = None,
    rotate: float | None = None,
    mirror: str | None = None,
    parent: str | None = None,
    position: str | None = None,
    style: str | None = None,
    reason: str = "",
    **css,
) -> SchPlaceSpec:
    """Schematic pose. CSS parks a body; to=/along= hangs the part off another pin.

    ``to="U1.EN"`` puts the pin of this part that shares U1.EN's net in line with
    it; ``pin=`` only when a different pin should go there. ``along="C1.1"``
    stacks beside that part instead. ``side`` is ``left``/``right``/``top``/
    ``bottom`` (CSS sense: top is the smaller sheet Y). ``gap`` (mm, pin to pin)
    is chosen by the tool unless given: room for the net's label, else the grid
    minimum. ``align="U2.EN"`` (with ``along=``) picks the gap that lands the
    hanging pin on that pin's row or column, so one straight wire joins them. The tool also turns or mirrors the part so the attached pin faces
    its target (2-pin parts may rotate; bigger symbols only mirror); ``rotate=``
    / ``mirror="x"|"y"`` override that. Library symbols are used as-is.
    """
    who = f"SchPlace({ref!r})"
    if "gap" in css:
        raise ValueError(f"{who}: gap= is pin spacing (not CSS). Pass gap= as its own argument.")
    attach = bool(to or along)
    if side is not None and side not in ("left", "right", "top", "bottom"):
        raise ValueError(f"{who}: side must be left/right/top/bottom, got {side!r}")
    if mirror is not None and mirror not in ("x", "y"):
        raise ValueError(f"{who}: mirror must be 'x' (top/bottom) or 'y' (left/right), got {mirror!r}")
    spec = SchPlaceSpec(
        ref=str(ref),
        pin=pin,
        to=to,
        along=along,
        gap=float(gap) if gap is not None else None,
        align=align,
        side=side,
        mirror=mirror,
        reason=reason,
        parent=parent,
    )
    if rotate is not None:
        spec.rot = float(rotate)
        spec.rotate_set = True
    if css or position or style or (parent and not attach):
        st = _style(
            who,
            style=style,
            position=position or ("absolute" if css else None),
            rotate=rotate,
            parent=parent,
            **css,
        )
        spec = apply_style_to_spec(spec, st)
        spec.rot = float(st.rotate)
        if st.is_absolute():
            spec.position = "absolute"
        if parent:
            spec.parent = parent
    if not spec.has_css() and not spec.has_attach():
        raise ValueError(f"{who}: needs CSS left/top/... or pin= and to=")
    _doc().sch_places.append(spec)
    return spec


def NetReq(
    *nets: str,
    kind: str = "generic",
    z_diff_ohm: float | None = None,
    z_se_ohm: float | None = None,
    volts: float | None = None,
    amps: float | None = None,
    temp_rise_c: float = 10.0,
    max_mm: float | None = None,
    length_mm: float | None = None,
    match_mm: float | None = None,
    uncoupled_mm: float | None = None,
    pair: bool = False,
    vias: bool | None = None,
    vias_max: int | None = None,
    layers: Sequence[str] | None = None,
    reference: str | None = None,
    keep_clear_of: str | Sequence[str] | None = None,
    keep_clear_mm: float | None = None,
    clock: str | None = None,
    pf_max: float | None = None,
    loop_mm2: float | None = None,
    rise_ps: float | None = None,
    autoroute: bool | str | None = None,
    class_name: str | None = None,
) -> NetReqSpec:
    """What a net needs (docs/r1-design.md section D): a preset `kind` plus the numbers the board
    overrides. No `**kwargs`: an unknown keyword is a TypeError at load and `pcbc check` names the
    line and the nearest keyword; a keyword the kind does not use is a `check` refusal."""
    if not nets:
        raise ValueError("NetReq needs at least one net or glob")
    who = f'NetReq("{nets[0]}")'
    z_diff_ohm = _num(z_diff_ohm, "z_diff_ohm", who, positive=True, hi=400.0)
    z_se_ohm = _num(z_se_ohm, "z_se_ohm", who, positive=True, hi=400.0)
    volts = _num(volts, "volts", who, positive=True, allow_zero=True, hi=1000.0)
    amps = _num(amps, "amps", who, positive=True, hi=30.0)
    temp_rise_c = _num(temp_rise_c, "temp_rise_c", who, positive=True) or 10.0
    max_mm = _num(max_mm, "max_mm", who, positive=True)
    length_mm = _num(length_mm, "length_mm", who, positive=True)
    match_mm = _num(match_mm, "match_mm", who, positive=True, allow_zero=True)
    uncoupled_mm = _num(uncoupled_mm, "uncoupled_mm", who, positive=True, allow_zero=True)
    keep_clear_mm = _num(keep_clear_mm, "keep_clear_mm", who, positive=True)
    pf_max = _num(pf_max, "pf_max", who, positive=True)
    loop_mm2 = _num(loop_mm2, "loop_mm2", who, positive=True)
    # **No default, no preset value, and no per-kind table** — `docs/stitch-plan.md` section 8
    # item 3 refuses `DEFAULT_RISE_PS` and it is right: an edge rate is a fact about the parts on
    # this board, the way `Thermal(watts=)` is, and four "pcbc default" guesses entering the
    # constraint compiler would be four invented numbers that a pitch then scales linearly with.
    # A net that does not say carries no edge rate, and the plane stitch that wants one refuses
    # by name rather than guessing (`patterns.stitch.stitch_pitch`).
    rise_ps = _num(rise_ps, "rise_ps", who, positive=True, hi=1e6)
    if vias_max is not None and (isinstance(vias_max, bool) or not isinstance(vias_max, int) or vias_max < 0):
        raise ValueError(f"{who}: vias_max={vias_max!r} must be a whole number of vias, 0 or more")
    if layers is not None and not tuple(layers):
        raise ValueError(f'{who}: layers=[] names no layer; drop it, or name one: layers=["F.Cu"]')
    if isinstance(keep_clear_of, str):
        keep = (keep_clear_of,)
    else:
        keep = tuple(str(k) for k in keep_clear_of) if keep_clear_of is not None else ()
    spec = NetReqSpec(
        nets=tuple(str(n) for n in nets),
        kind=str(kind),
        z_diff_ohm=z_diff_ohm,
        z_se_ohm=z_se_ohm,
        volts=volts,
        amps=amps,
        temp_rise_c=float(temp_rise_c),
        max_mm=max_mm,
        match_mm=match_mm,
        pair=bool(pair),
        vias=vias,
        layers=tuple(layers) if layers is not None else None,
        keep_clear_of=keep,
        keep_clear_mm=keep_clear_mm,
        autoroute=autoroute,
        class_name=class_name,
        length_mm=length_mm,
        uncoupled_mm=uncoupled_mm,
        vias_max=vias_max,
        reference=reference,
        clock=clock,
        pf_max=pf_max,
        loop_mm2=loop_mm2,
        rise_ps=rise_ps,
        line=_line(),
    )
    _doc().netreqs.append(spec)
    return spec


def Pair(
    p: str,
    n: str,
    *,
    z_diff_ohm: float = 90.0,
    match_mm: float = 0.5,
    uncoupled_mm: float = 2.0,
    gap_mm: float | None = None,
    layers: Sequence[str] | None = None,
    reference: str | None = None,
) -> PairReq:
    """A differential pair: the explicit form of `NetReq(kind="usb_hs")`, or its override
    (only z_diff_ohm, match_mm, uncoupled_mm, gap_mm, layers, reference)."""
    who = f'Pair("{p}", "{n}")'
    z_diff_ohm = _num(z_diff_ohm, "z_diff_ohm", who, positive=True, hi=400.0)
    match_mm = _num(match_mm, "match_mm", who, positive=True, allow_zero=True)
    uncoupled_mm = _num(uncoupled_mm, "uncoupled_mm", who, positive=True, allow_zero=True)
    gap_mm = _num(gap_mm, "gap_mm", who, positive=True)
    spec = PairReq(
        p=str(p),
        n=str(n),
        z_diff_ohm=float(z_diff_ohm),
        match_mm=float(match_mm),
        uncoupled_mm=float(uncoupled_mm),
        gap_mm=float(gap_mm) if gap_mm is not None else None,
        layers=tuple(layers) if layers is not None else None,
        reference=reference,
        line=_line(),
    )
    _doc().pairs.append(spec)
    return spec


def Bus(*nets: str, match_mm: float, clock: str | None = None, length_mm: float | None = None) -> BusReq:
    """Nets routed as a ribbon and length-matched within `match_mm` (to `clock` when named)."""
    if len(nets) < 2:
        raise ValueError(f'Bus("{nets[0]}") needs at least two nets' if nets else "Bus needs at least two nets")
    who = f'Bus("{nets[0]}")'
    match_mm = _num(match_mm, "match_mm", who, positive=True, allow_zero=True)
    length_mm = _num(length_mm, "length_mm", who, positive=True)
    spec = BusReq(
        nets=tuple(str(n) for n in nets),
        match_mm=float(match_mm),
        clock=str(clock) if clock is not None else None,
        length_mm=float(length_mm) if length_mm is not None else None,
        line=_line(),
    )
    _doc().buses.append(spec)
    return spec


def Chain(net: str, *pads: str) -> ChainReq:
    """The feed order of a net's pads, `"REF.PIN"` as `Place(to=)` takes them: cap before pin."""
    if len(pads) < 2:
        raise ValueError(f'Chain({net!r}) needs at least two "REF.PIN" pads')
    for pad in pads:
        if "." not in str(pad):
            raise ValueError(f"Chain({net!r}): {pad!r}: write REF.PIN, e.g. U1.VIN")
    spec = ChainReq(net=str(net), pads=tuple(str(p) for p in pads), line=_line())
    _doc().chains.append(spec)
    return spec


def Isolation(
    a: str,
    b: str,
    *,
    volts: float,
    slot: bool = False,
    across: Sequence[str] = (),
    reinforced: bool = False,
) -> IsolationReq:
    """An isolation barrier between two `Region`s; `across` names the parts that span it."""
    who = f'Isolation("{a}", "{b}")'
    volts = _num(volts, "volts", who, positive=True, hi=1000.0)
    if str(a) == str(b):
        raise ValueError(f"{who}: both sides name the same Region; an isolation separates two")
    spec = IsolationReq(
        a=str(a),
        b=str(b),
        volts=float(volts),
        slot=bool(slot),
        across=(across,) if isinstance(across, str) else tuple(str(r) for r in across),
        reinforced=bool(reinforced),
        line=_line(),
    )
    _doc().isolations.append(spec)
    return spec


BRIDGE_KINDS = ("short", "cap", "bead", "off_board")
"""What may tie two grounds, in the order a board is most likely to reach for one.

No `"net_tie"`, and its absence is the decision of `docs/stitch-plan.md` §8 item 4 rather than an
omission — see `Bridge`'s own docstring for the two judges that would each need an exemption.
"""


def Bridge(a: str, b: str, *, at: str | Sequence[str], kind: str = "short", why: str = "") -> BridgeReq:
    """The one point at which two declared grounds are tied, and what ties them.

    **pcbc writes no copper for this, and that is the whole design.** `at` names a part the board
    already places — a 0R, a ferrite, a cap. The netlist keeps both nets, so `netcheck.expected_nets`
    and `netcheck.copper_nets` agree, KiCad sees no short, and the copper gate is untouched: measured,
    `copper_nets` (`netcheck.py:125-146`) matches `(pad "N" ... (net "NAME"))` **inside footprint
    blocks only** and never reads a `(segment)` or a `(via)`, so a component tie changes no pad
    binding at all. `netcheck.py` has a zero diff in the slice that added this statement.

    **Why there is no `kind="net_tie"` and no copper tie of any shape.** A track joining two declared
    nets would need an exemption in **two** judges, not one:

    - `fab.copper_drc_errors` (`fab.py:489-505`) surfaces KiCad's own `shorting_items` and exempts
      only same-footprint pad-to-pad (`_same_footprint_pad_pad`, `fab.py:473`). An exemption keyed on
      a net pair would be board-wide, so it would also excuse the **accidental** short between those
      two nets — which is the one thing the check exists to catch.
    - `route_scene._pair_clashes` decides same-net by `it.net == net`, so GND copper touching a VSS
      pad is a `Clash("copper", 0.0, 0.200)` that `verify_copper` raises on before the fab gate ever
      runs.

    So a copper tie is **refused, not exempted**, and it is refused by not existing. And on the one
    board in reach that has two grounds there is nowhere to put a moat either: measured on the DS2
    Addon 2026-09-21, GND's and VSS's pad populations overlap on **all four** separating axes
    (x 24.480 mm, y 15.444 mm, u 20.610 mm, v 16.797 mm), so no straight line divides the domains
    and `Split(gap_mm=)` has no geometry to act on (`docs/stitch-plan.md` §8 item 5). A 0R **is** a
    DC tie made of a part; what a component bridge cannot be is zero-area and BOM-free, and no board
    here needs that.

    `kind` is one of `BRIDGE_KINDS`. `kind="off_board"` says the tie is made elsewhere — in a
    harness, a chassis stud, a mating board — and `at` then names the two pads that leave the board,
    `"REF.PAD"` each, with a mandatory `why`. A `kind="cap"` tie earns a note rather than a refusal:
    a capacitor is an AC bridge and leaves the two grounds with no DC reference between them.
    """
    who = f'Bridge("{a}", "{b}")'
    a, b = str(a), str(b)
    if a == b:
        raise ValueError(f"{who}: both sides name the same net; a bridge ties two grounds, not one to itself")
    kind = str(kind)
    if kind not in BRIDGE_KINDS:
        raise ValueError(f'{who}: kind={kind!r} is not one of {", ".join(BRIDGE_KINDS)}')
    pads = (at,) if isinstance(at, str) else tuple(str(p) for p in at)
    pads = tuple(str(p) for p in pads)
    if not pads:
        raise ValueError(f'{who}: at= names the part that ties them, e.g. at="R11"')
    if kind == "off_board":
        if len(pads) != 2 or any("." not in p for p in pads):
            raise ValueError(
                f'{who}: kind="off_board" names the two pads that leave the board, '
                f'e.g. at=("J1.2", "J2.2") — write REF.PAD for each'
            )
        if not str(why).strip():
            raise ValueError(f'{who}: kind="off_board" needs why= — where the tie is made is not on this board, so it has to be written down')
    elif len(pads) != 1:
        raise ValueError(f'{who}: at= names one part, e.g. at="R11"; only kind="off_board" takes two pads')
    spec = BridgeReq(a=a, b=b, at=pads, kind=kind, why=str(why), line=_line())
    _doc().bridges.append(spec)
    return spec


def Thermal(pad: str, *, watts: float, rise_c: float = 10.0, across_planes: bool = False, fill: bool = False) -> ThermalReq:
    """A via array under one exposed pad, so the heat it carries has somewhere to go (R-T1).

    `pad` is `"REF.PADNUM"` or `"REF.PINNAME"` — `Thermal("U1.49")` and `Thermal("U1.GND")` name the
    same land — resolved by `constraints._refpin_net`, which is the resolver `Chain()` already uses,
    so the two statements cannot disagree about what `"U1.49"` means.

    **`watts` is a board fact, not a library fact, and that is why this is a statement rather than
    `thermal=` on a `Part`.** The same LDO dissipates 40 mW on one board and 900 mW on the next; what
    a part knows is its package, and what the board knows is the load. There is a mechanical reason
    too: `Part.__call__(**pin_nets)` binds every keyword as a **pin name**, so a `thermal=` kwarg on a
    part would silently become a pin called "thermal" and bind a net called `0.35`.

    `rise_c` is the copper rise this array is budgeted for, defaulting to the 10.0 that
    `CurrentSpec.temp_rise_c` already defaults to so the thermal model and the current model share one
    number rather than inventing a second. **It is not a junction rise and this is not `theta_JA`:**
    pcbc knows the barrel (`stackup.via_theta_c_per_w`) and knows nothing about the package, the
    spreading in the plane it lands on, or the air above the board. The count is
    `ceil(theta_barrel / (rise_c / watts))` and the sentence the report prints says exactly that much
    and no more.

    `across_planes=True` consents to carving antipads in a plane this pad's net does **not** join. On
    four layers every via is a through via, so an array of twelve GND barrels under a pad punches
    twelve holes in the 3V3 plane below it whether anybody meant to or not — finding 10's lesson (one
    row of taps slotted that plane and turned a 1.80 mm path across it into 8.09 mm) at nine times the
    scale. The default is to refuse and name the arithmetic; the pitch that keeps the webs alive is
    derived rather than chosen (`patterns.stitch.thermal_pitch`), so consenting is a decision about
    the plane and not about a tolerance.

    `fill=True` places every site that fits instead of the `n` the budget asks for. The budget is a
    floor — more copper is more conductance — so this cannot make the array worse, only larger; it is
    the switch for a pad whose real dissipation is not known and whose land is free anyway.

    **A thermal array is via-in-pad by definition**, so the fab package has to say so: see
    `Stackup.via_fill` for the difference between tenting a barrel and plugging it, and
    `fab._write_notes` for the paragraph the order has to carry. pcbc refuses the array outright on a
    **passive's** pad, where no amount of filling saves the joint, and `fab.via_in_pad_blockers` is
    unchanged by this statement — it must stay the judge it was.
    """
    who = f'Thermal("{pad}")'
    pad = str(pad)
    if "." not in pad:
        raise ValueError(f"{who}: write REF.PAD or REF.PIN, e.g. U1.49")
    watts = _num(watts, "watts", who, positive=True)
    rise_c = _num(rise_c, "rise_c", who, positive=True)
    spec = ThermalReq(
        pad=pad,
        watts=float(watts),
        rise_c=float(rise_c),
        across_planes=bool(across_planes),
        fill=bool(fill),
        line=_line(),
    )
    _doc().thermals.append(spec)
    return spec


def Guard(net: str, *, stitch_mm: float = 2.5, ground: str = "GND") -> GuardReq:
    """A stitched ground guard around a net (recorded in R1; the router pattern is R2)."""
    stitch_mm = _num(stitch_mm, "stitch_mm", f'Guard("{net}")', positive=True)
    spec = GuardReq(net=str(net), stitch_mm=float(stitch_mm), ground=str(ground), line=_line())
    _doc().guards.append(spec)
    return spec


_CONSTRUCTORS: dict[str, object] = {}


def _explain_type_error(exc: TypeError, path: Path) -> str | None:
    """`NetReq("VBUS") line 144: unexpected keyword 'amp'; did you mean amps=?` for a bad keyword on
    one of the board constructors; None for any other TypeError (re-raised by the caller)."""
    m = re.match(r"^(\w+)\(\) got an unexpected keyword argument '(\w+)'", str(exc))
    if not m:
        return None
    name, kw = m.group(1), m.group(2)
    fn = _CONSTRUCTORS.get(name)
    if fn is None:
        return None
    lineno = 0
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == str(path):
            lineno = tb.tb_lineno
        tb = tb.tb_next
    first = ""
    try:
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == name and node.lineno == lineno:
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    first = f'"{node.args[0].value}"'
                break
    except (SyntaxError, OSError):
        pass
    params = [p for p in inspect.signature(fn).parameters if p not in ("nets", "pads")]  # type: ignore[arg-type]
    close = difflib.get_close_matches(kw, params, n=1)
    hint = f"did you mean {close[0]}=?" if close else "it takes " + ", ".join(f"{p}=" for p in params)
    return f"{name}({first}) line {lineno}: unexpected keyword '{kw}'; {hint}"


def _generic(
    *,
    ref: str,
    prefix: str,
    value: str,
    package: str,
    mpn: str,
    lcsc: str | None,
    manufacturer: str,
    pin_nets: dict[str, object],
) -> Part:
    part = Part(
        name=mpn or f"{prefix}_{ref}",
        prefix=prefix,
        mpn=mpn,
        manufacturer=manufacturer,
        lcsc=lcsc,
        footprint="",
        package=package,
        value=value,
        kind="generic",
        pins={
            "1": Pin(name="1", pads=("1",)),
            "2": Pin(name="2", pads=("2",)),
        },
    )
    part(ref, **pin_nets)
    return part


def Resistor(
    ref: str,
    value: str,
    *,
    package: str,
    mpn: str,
    lcsc: str | None = None,
    manufacturer: str = "",
    p1: object = None,
    p2: object = None,
    P1: object = None,
    P2: object = None,
) -> Part:
    a = p1 if p1 is not None else P1
    b = p2 if p2 is not None else P2
    if a is None or b is None:
        raise ValueError(f"Resistor({ref!r}) needs p1 and p2")
    return _generic(
        ref=ref,
        prefix="R",
        value=value,
        package=package,
        mpn=mpn,
        lcsc=lcsc,
        manufacturer=manufacturer,
        pin_nets={"1": a, "2": b},
    )


def Capacitor(
    ref: str,
    value: str,
    *,
    package: str,
    mpn: str,
    lcsc: str | None = None,
    manufacturer: str = "",
    p1: object = None,
    p2: object = None,
    P1: object = None,
    P2: object = None,
) -> Part:
    a = p1 if p1 is not None else P1
    b = p2 if p2 is not None else P2
    if a is None or b is None:
        raise ValueError(f"Capacitor({ref!r}) needs p1 and p2")
    return _generic(
        ref=ref,
        prefix="C",
        value=value,
        package=package,
        mpn=mpn,
        lcsc=lcsc,
        manufacturer=manufacturer,
        pin_nets={"1": a, "2": b},
    )


def Led(
    ref: str,
    color: str = "",
    *,
    package: str,
    mpn: str,
    lcsc: str | None = None,
    manufacturer: str = "",
    a: object = None,
    k: object = None,
    A: object = None,
    K: object = None,
) -> Part:
    anode = a if a is not None else A
    cathode = k if k is not None else K
    if anode is None or cathode is None:
        raise ValueError(f"Led({ref!r}) needs a= and k=")
    # KiCad Device:LED / 0603 LED: pin 1 = K (cathode), pin 2 = A (anode).
    part = Part(
        name=mpn or f"D_{ref}",
        prefix="D",
        mpn=mpn,
        manufacturer=manufacturer,
        lcsc=lcsc,
        footprint="",
        package=package,
        value=color or "LED",
        kind="led",
        pins={
            "K": Pin(name="K", pads=("1",)),
            "A": Pin(name="A", pads=("2",)),
        },
    )
    part(ref, A=anode, K=cathode)
    return part


def Component(**kwargs) -> Part:
    """Library constructor used in part.py. Not an instance."""
    from .circuit import _library_component

    return _library_component(**kwargs)


def load(path: str | Path) -> Part:
    p = Path(path)
    if not p.is_absolute():
        if _board_dir is not None:
            p = (_board_dir / p).resolve()
        else:
            p = p.resolve()
    return load_part(p)


def load_board(path: str | Path) -> Design:
    """Execute a board.py and return the collected design."""
    global _board_dir, _board_path
    path = Path(path).resolve()
    reset()
    _board_dir = path.parent
    _board_path = path
    ns = {
        "Board": Board,
        "Place": Place,
        "Keepout": Keepout,
        "Region": Region,
        "NetReq": NetReq,
        "Pair": Pair,
        "Bus": Bus,
        "Chain": Chain,
        "Isolation": Isolation,
        "Guard": Guard,
        "Bridge": Bridge,
        "Thermal": Thermal,
        "AUTO": AUTO,
        "Net": Net_,
        "Power": Power,
        "Ground": Ground,
        "Resistor": Resistor,
        "Capacitor": Capacitor,
        "Led": Led,
        "Component": Component,
        "SchPlace": SchPlace,
        "SchRegion": SchRegion,
        "SchStyle": SchStyle,
        "Seg": Seg,
        "Arc": Arc,
        "Via": Via,
        "Pour": Pour,
        "Line": Line,
        "Rect": Rect,
        "Circle": Circle,
        "DrawArc": DrawArc,
        "Poly": Poly,
        "Curve": Curve,
        "Text": Text,
        "TextBox": TextBox,
        "Dimension": Dimension,
        "Group": Group,
        "Image": Image,
        "Table": Table,
        "Barcode": Barcode,
        "Target": Target,
        "Point": Point,
        "Generated": Generated,
        "load": load,
        "__file__": str(path),
        "__name__": "__pcbc__",
    }
    from .trace import tracing

    before = set(sys.modules)
    with tracing("load", path.parent) as seen:
        try:
            exec(compile(path.read_text(), str(path), "exec"), ns, ns)  # noqa: S102
        finally:
            local = _local_modules(path.parent, set(sys.modules) - before)
    design = _doc()
    design.source = str(path)
    design.modules = sorted(local)
    # M2: every file loading the board opened — board.py, its helper modules wherever they live, a data
    # file a helper reads, the part files, the symbol and footprint files — by content, as the load was
    # seen to read them (`trace`). Every stage's stamp carries it.
    design.reads = seen.record()["reads"]
    _refuse_layout_in_board(design, path)
    return design


def _local_modules(root: Path, names: set[str]) -> list[str]:
    """The files of the modules `board.py` imported that live under its own directory, relative to
    it — and those modules taken back out of `sys.modules`, so the next `load_board` in this process
    reads the file again rather than Python's cache of it.

    A `board.py` that says `from helper import LEFT` has a second source file, and until the third
    review (T2) nothing hashed it: edit `helper.py`, run a plain build, and the board placed from the
    old value shipped `ok=True`. The choice made here is to **hash** such imports rather than refuse
    them: a board's helpers are its author's to organise, and a recorded input costs nothing. A module
    imported from outside the board's directory (a library) is not recorded."""
    from .trace import is_tool_file

    root = root.resolve()
    out: list[str] = []
    for name in sorted(names):
        mod = sys.modules.get(name)
        file = getattr(mod, "__file__", None)
        if not file:
            continue
        p = Path(file).resolve()
        try:
            rel = p.relative_to(root)
        except ValueError:
            # A module from elsewhere that is not the tool (Python, an installed package, pcbc): taken
            # out of the cache too, so the next load reads — and the tracer sees — its file again (M2).
            if not is_tool_file(str(p)):
                sys.modules.pop(name, None)
            continue
        out.append(rel.as_posix())
        sys.modules.pop(name, None)
    return out


def _refuse_layout_in_board(design: Design, path: Path) -> None:
    """`board.py` holds the circuit and the per-net requirements; copper and drawings are layout, and
    their one source file is `layout.core.py`. A `Seg`/`Via`/`Pour`/`Line`/... written in `board.py`
    (by name, or through `from pcbc import Seg`) used to be accepted and then dropped by the build
    without a word, which made a third place to write copper that nothing read. Refused, naming the
    line."""
    objs = [*design.copper, *design.graphics]
    if not objs:
        return
    what = "; ".join(f"{o.where or path.name}: {type(o).__name__ if not hasattr(o, 'kind') else o.kind}" for o in objs[:4])
    raise ValueError(
        f"{what}: copper and drawings are layout, not circuit, and belong in layout.core.py next to "
        f"{path.name} (the router writes the rest into layout.gen.py). Move the line there."
    )


def _core_place(ref: str, *args, **kwargs):
    """`Place()` in `layout.core.py` is refused, not quietly applied.

    **Default chosen for step 1, reversible:** a part's placement has one home, `board.py`, where the
    place stage resolves it (CSS, relations, edges). Grok's first cut let a core `Place` silently
    override `board.py`'s, which made the same part's pose depend on which of two files an author
    read. A pose the generator wrote is still copyable — it lives in `layout.gen.py` as a `Place` —
    but locking it means writing it in `board.py` as `Place(ref, at=(x, y), rot=r, side=...)`.
    """
    raise ValueError(
        f"{_where() or 'layout.core.py'}: Place({ref!r}) in layout.core.py is refused: a part is placed in "
        f"board.py only. Move this line into board.py as Place({ref!r}, at=(x, y), rot=r, side=\"F\")"
    )


def _gen_place(ref: str, *, at: tuple[float, float], rot: float = 0.0, layer: str = "F.Cu", locked: bool = False):
    """`Place()` as `layout.gen.py` writes it: one footprint's `(at x y rot)`, `(layer)` and `(locked)`."""
    from .model import Pose

    who = f"Place({ref!r})"
    if layer not in ("F.Cu", "B.Cu"):
        raise ValueError(f"{who}: a footprint's layer is F.Cu or B.Cu, got {layer!r}")
    pose = Pose(str(ref), _pt(at, who), float(rot), str(layer), bool(locked), source=_layout_source, where=_where())
    _doc().poses.append(pose)
    return pose


# The fields of a design that are `board.py`'s: executing a layout file may add layout objects and
# nothing else. Snapshotted before and compared after, so a `Place`, `NetReq`, `Keepout`, `Region`, a
# net or a part made through *any* path (`from pcbc import Place`, `import pcbc.language as L`) is
# refused, not merged.
_BOARD_FIELDS = ("board", "places", "keepouts", "regions", "netreqs", "pairs", "buses", "chains", "isolations", "guards", "bridges", "thermals", "nets", "instances", "sch_places", "sch_regions", "sch_wire_mm")
_LAYOUT_NAMES = ("Seg", "Arc", "Via", "Pour", "Line", "Rect", "Circle", "DrawArc", "Poly", "Curve", "Text", "TextBox", "Dimension", "Group", "Image", "Table", "Barcode", "Target", "Point", "Generated")


_BOARD_NAMES = ("Board", "Keepout", "Region", "NetReq", "Pair", "Bus", "Chain", "Isolation", "Guard", "Bridge", "Thermal", "Net", "Power", "Ground", "Resistor", "Capacitor", "Led", "Component", "SchPlace", "SchRegion", "SchStyle", "load")


def _board_only(name: str, file: str):
    def refuse(*_a, **_k):
        hint = " A rule area in a layout file is Pour(None, keepout={...})." if name in ("Keepout", "Region") else ""
        raise ValueError(f"{_where() or file}: {name}() is board.py's, not a layout primitive; {file} holds layout primitives only.{hint} Move this line into board.py")

    return refuse


def _board_state(design: Design) -> dict[str, str]:
    return {f: repr(getattr(design, f)) for f in _BOARD_FIELDS}


def _layout_line(exc: BaseException, path: Path) -> str:
    """`layout.core.py:N` from the deepest traceback frame in the layout file, or the syntax error's line."""
    import traceback

    if isinstance(exc, SyntaxError) and exc.lineno:
        return f"{path.name}:{exc.lineno}"
    line = 0
    for fr in traceback.extract_tb(exc.__traceback__):
        if fr.filename == str(path):
            line = fr.lineno or line
    return f"{path.name}:{line}" if line else path.name


def load_layout(path: str | Path, design: Design, *, source: str) -> Design:
    """Execute a layout file into an already-loaded design. Does not reset it.

    `source` is `core` or `gen`; objects created here are tagged with it. The namespace is the layout
    primitives and nothing else: `Seg`, `Arc`, `Via`, `Pour`, the drawings, and `Place`, which means
    something different in each file and is never `board.py`'s — in `layout.gen.py` it is a
    footprint's pose (`_gen_place`, a `model.Pose`), and in `layout.core.py` it is refused naming
    `board.py` (`_core_place`). `Keepout` and `Region` are `board.py`'s and are not in it; a rule area
    written here is `Pour(None, keepout=...)`.

    **Default chosen for step 1, reversible:** whatever path a `board.py` object is made by
    (`from pcbc import Place`, `NetReq`, `Keepout`, ...), a layout file that changes any `board.py`
    field of the design is refused, naming the line. Grok's first cut merged a core `Place` over
    `board.py`'s, which gave one part two homes. Two objects of one file with the same `id` are
    refused naming both lines. Any error while executing the file is re-raised as a `ValueError`
    naming `file:line`.
    """
    global _layout_source, _current, _layout_path
    path = Path(path).resolve()
    if not path.exists():
        return design
    prev_src, prev_doc, prev_path = _layout_source, _current, _layout_path
    _layout_source = source
    _current = design
    _layout_path = str(path)
    ns = {name: _board_only(name, path.name) for name in _BOARD_NAMES}
    ns.update({name: globals()[name] for name in _LAYOUT_NAMES})
    ns.update({"Place": _gen_place if source == "gen" else _core_place, "__file__": str(path), "__name__": "__pcbc_layout__"})
    before_state = _board_state(design)
    before_pose = len(design.poses)
    before_cu = len(design.copper)
    before_gr = len(design.graphics)
    before_mods = set(sys.modules)
    before_path = list(sys.path)
    try:
        exec(compile(path.read_text(), str(path), "exec"), ns, ns)  # noqa: S102
    except ValueError as exc:
        msg = str(exc)
        raise ValueError(msg if msg.startswith(path.name) else f"{_layout_line(exc, path)}: {msg}") from None
    except Exception as exc:  # noqa: BLE001 - a typo in a layout file is the author's to fix, named by its line
        raise ValueError(f"{_layout_line(exc, path)}: {type(exc).__name__}: {exc}") from None
    finally:
        _layout_source = prev_src
        _current = prev_doc
        _layout_path = prev_path
        # A module the layout file imported (a helper beside it, anything that is not the tool) leaves
        # the cache, and a `sys.path` entry it added goes: the next load reads the helper's file again,
        # so the route stage's tracer sees it and stamps it (M2; sixth review, a core helper edit
        # shipped stale as "skipped, ok").
        _local_modules(path.parent, set(sys.modules) - before_mods)
        sys.path[:] = before_path
    after_state = _board_state(design)
    if after_state != before_state:
        changed = [f for f in _BOARD_FIELDS if after_state[f] != before_state[f]]
        raise ValueError(
            f"{path.name}: this file changed board.py's part of the design ({', '.join(changed)}): a Place, NetReq, "
            f"Keepout, Region, net or part made here, however it was imported. {path.name} holds layout primitives "
            f"only; that line belongs in board.py"
        )
    added_c = design.copper[before_cu:]
    added_g = design.graphics[before_gr:]
    added_pose = design.poses[before_pose:]
    if source == "core" and added_pose:
        # `Place()` in core is refused by name (`_core_place`); a pose made by any other path
        # (`pcbc.language._gen_place`, a `Pose` appended by hand) is the same second home for a
        # part's placement, and is refused the same way (the second review's m12).
        pose = added_pose[0]
        design.poses = design.poses[:before_pose]
        raise ValueError(f"{pose.where or path.name}: a pose for {pose.ref!r} was made in {path.name} (however it was made): a part is placed in board.py only. Move it there as Place({pose.ref!r}, at=(x, y), rot=r, side=\"F\")")
    seen: dict[str, str] = {}
    for o in [*added_c, *added_g]:
        if o.id in seen:
            raise ValueError(f"{o.where or path.name}: id {o.id!r} is already {seen[o.id]}'s; two objects of one file cannot share an id")
        seen[o.id] = o.where or path.name
    if source == "core":
        _core_groups(added_c, added_g, path.name)
        _core_uuids(added_c, added_g, path.name)
    if source == "gen":
        # A gen object never replaces a core one: the build removes core objects from gen before
        # writing it, and a gen id that equals a core id is a count mismatch the build reports.
        owned_c = {c.id for c in design.copper[:before_cu]}
        owned_g = {g.id for g in design.graphics[:before_gr]}
        design.copper = design.copper[:before_cu] + [c for c in added_c if c.id not in owned_c]
        design.graphics = design.graphics[:before_gr] + [g for g in added_g if g.id not in owned_g]
        seen_refs = {p.ref for p in design.poses[:before_pose]}
        design.poses = design.poses[:before_pose] + [p for p in added_pose if p.ref not in seen_refs]
    return design


def _locked_of(o) -> bool:
    if isinstance(o, Copper):
        return bool(o.locked)
    return bool(o.f.get("locked"))


def _core_groups(copper: list, graphics: list, file: str) -> None:
    """KiCad's save locks every member of a locked group, a drawing, a table (and its cells), a
    dimension (and its text), a barcode or a nested group as much as copper, and every member of a
    nested group too (measured, fifth review `f_group_locked_*`). A member that does not itself say
    `locked=True` is refused naming its line and the group that locks it; until the fifth review only
    direct copper members were checked. KiCad keeps a `target` and a `point` without a locked flag at
    all, so those (and any drawing whose schema has no `locked`) are exempt."""
    from .layout_prims import SCHEMAS

    by_id = {o.id: o for o in [*copper, *graphics]}

    def walk(g, root, seen):
        for m in g.f.get("members", ()):
            o = by_id.get(m)
            if o is None or m in seen:
                continue
            seen.add(m)
            if not (isinstance(o, Graphic) and "locked" not in {f.kw for f in SCHEMAS[o.kind].fields()}) and not _locked_of(o):
                raise ValueError(f"{o.where or file}: {m!r} is a member of the locked group {root.f.get('name')!r} ({root.where or file}){' through ' + repr(g.f.get('name')) if g is not root else ''}, and KiCad's save locks it: write locked=True on it, or unlock the group")
            if isinstance(o, Graphic) and o.kind == "table":
                for i, cell in enumerate(o.f.get("cells") or ()):
                    if not cell.get("locked"):
                        raise ValueError(f"{o.where or file}: {m!r}'s cell {i} is in the locked group {root.f.get('name')!r} ({root.where or file}), and KiCad's save locks it: write locked=True on the cell")
            if isinstance(o, Graphic) and o.kind == "group":
                walk(o, root, seen)

    for g in graphics:
        if g.kind == "group" and g.f.get("locked"):
            walk(g, g, set())


def _core_uuids(copper: list, graphics: list, file: str) -> None:
    """Every uuid a core line writes — an object's own, a table cell's, a dimension text's — names one
    object: a repeat is refused at load naming both lines (fifth review: two core lines with one uuid
    routed the board and then raised `DuplicateUuid` out of normalise). A dimension's text carries the
    dimension's own uuid (`_check_dimension`), so it is counted once."""
    from .layout_prims import SCHEMAS, all_uuids

    owner: dict[str, str] = {}
    for o in [*copper, *graphics]:
        if isinstance(o, Copper):
            got = [o.uuid] if o.uuid else []
        else:
            got = list(dict.fromkeys(all_uuids(SCHEMAS[o.kind], o.f)))
        for u in got:
            here = f"{o.where or file} ({o.kind} {o.id!r})"
            if u in owner:
                raise ValueError(f"{here}: uuid {u} is already {owner[u]}'s; a uuid names one object, and KiCad would re-key one of them: give this one another uuid, or leave uuid= out")
            owner[u] = here


def _at_board_line(exc: Exception, board: Path) -> str:
    """`msg` with `line N` from the deepest traceback frame inside the board file."""
    import traceback

    line = 0
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            same = Path(frame.filename).resolve() == board
        except OSError:
            same = False
        if same:
            line = frame.lineno or 0
    return f"line {line}: {exc}" if line else str(exc)


def _num(value, what: str, who: str, *, positive: bool = False, allow_zero: bool = False, hi: float | None = None):
    """A number the board wrote, or a ValueError naming what to change. `language` raises; the
    loader turns it into a line-cited refusal, so `pcbc check` never prints a traceback."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # The example is the value itself when it reads as a number (`"0.05"` -> write 0.05), never a
        # made-up one: the old fixed "5" suggested a 5 mm solder-mask margin (sixth review).
        import re as _re

        m = _re.match(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", value) if isinstance(value, str) else None
        shown = _g_num(float(m.group(1))) if m else "<number>"
        raise ValueError(f"{who}: {what}={value!r} is not a number; write {what}={shown} (millimetres, volts, amps or ohms, no unit)")
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(f"{who}: {what}={value!r} is not a finite number")
    if positive and (v < 0 or (v == 0 and not allow_zero)):
        raise ValueError(f"{who}: {what}={_g_num(v)} is not physical; {what} must be {'at least 0' if allow_zero else 'greater than 0'}")
    if hi is not None and v > hi:
        raise ValueError(f"{who}: {what}={_g_num(v)} is past what pcbc models ({_g_num(hi)}); say what the board really needs")
    return v


def _g_num(v: float) -> str:
    return f"{v:g}"


def check_board(path: str | Path, pcb: bool = True, notes: list[str] | None = None) -> list[str]:
    """Everything that can be wrong before a build: unbound pins, missing
    Place()/SchPlace(), and a SchPlace that names a pin or part that is not
    there or hangs a part off a pin it does not share a net with. The
    schematic loop passes ``pcb=False``: no Place() needed to draw a sheet.
    ``notes`` is `check_design`'s out-parameter: findings that are moves and not failures."""
    try:
        design = load_board(path)
    except TypeError as exc:
        msg = _explain_type_error(exc, Path(path).resolve())
        if msg is None:
            raise
        return [msg]
    except ValueError as exc:
        # Everything language.py refuses at load (a Bus of one net, a Chain pad without a dot, a
        # number that is not a number): the board's line, not a 15-line traceback.
        return [_at_board_line(exc, Path(path).resolve())]
    fails = check_design(design, pcb=pcb, notes=notes)
    if pcb:
        from .pcb_place import validate

        fails += validate(design)
    if fails:
        return fails
    from .sch_emit import _parts_from_design
    from .sch_place import apply_sch_places

    kinds = {n.name: n.kind for n in design.nets.values()}
    try:
        apply_sch_places(design, _parts_from_design(design, kinds))
    except ValueError as exc:
        fails.append(f"schematic: {exc}")
    return fails


_CONSTRUCTORS.update({"NetReq": NetReq, "Pair": Pair, "Bus": Bus, "Chain": Chain, "Isolation": Isolation, "Guard": Guard, "Bridge": Bridge, "Thermal": Thermal})
