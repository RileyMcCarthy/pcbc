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
from .model import (
    BoardSpec,
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
)

_current: Design | None = None
_board_dir: Path | None = None
_board_path: Path | None = None  # the board file being loaded: constraint lines cite their line in it


def _doc() -> Design:
    global _current
    if _current is None:
        _current = Design()
    return _current


def reset() -> None:
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


def _register_net(net: Net) -> Net:
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
    spec = BoardSpec(
        size_mm=(w, h),
        layers=int(layers),
        stackup=stackup,
        pcb=pcb,
        planes=tuple((str(n), str(l)) for n, l in (planes or ())),
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
        "load": load,
        "__file__": str(path),
        "__name__": "__pcbc__",
    }
    exec(compile(path.read_text(), str(path), "exec"), ns, ns)  # noqa: S102
    design = _doc()
    design.source = str(path)
    return design


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
        raise ValueError(f"{who}: {what}={value!r} is not a number; write {what}=5 (millimetres, volts, amps or ohms, no unit)")
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


def check_board(path: str | Path, pcb: bool = True) -> list[str]:
    """Everything that can be wrong before a build: unbound pins, missing
    Place()/SchPlace(), and a SchPlace that names a pin or part that is not
    there or hangs a part off a pin it does not share a net with. The
    schematic loop passes ``pcb=False``: no Place() needed to draw a sheet."""
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
    fails = check_design(design, pcb=pcb)
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


_CONSTRUCTORS.update({"NetReq": NetReq, "Pair": Pair, "Bus": Bus, "Chain": Chain, "Isolation": Isolation, "Guard": Guard})
