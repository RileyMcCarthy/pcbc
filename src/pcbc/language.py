"""Board-file DSL. Ordinary Python that calls these constructors.

Electrical: Net, Power, Ground, Resistor, Capacitor, Led, load(part).
Spatial: Board, Place, Keepout, Region, NetReq (PCB CSS).
Schematic: SchRegion, SchPlace (CSS body, or pin=/to=/gap= attach).
"""

from __future__ import annotations

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
    Design,
    KeepoutSpec,
    Net,
    NetReqSpec,
    Part,
    Pin,
    PlaceSpec,
    RegionSpec,
    SchPlaceSpec,
)

_current: Design | None = None
_board_dir: Path | None = None


def _doc() -> Design:
    global _current
    if _current is None:
        _current = Design()
    return _current


def reset() -> None:
    global _current, _board_dir
    _current = Design()
    _board_dir = None


def _register_net(net: Net) -> Net:
    doc = _doc()
    existing = doc.nets.get(net.name)
    if existing is None:
        doc.nets[net.name] = net
        return net
    if existing.kind == "net" and net.kind != "net":
        existing.kind = net.kind
    return existing


def Power(name: str) -> Net:
    return _register_net(Net(str(name), kind="power"))


def Ground(name: str = "GND") -> Net:
    return _register_net(Net(str(name), kind="ground"))


def Net_(name: str) -> Net:  # noqa: N802 — exported as Net
    return _register_net(Net(str(name), kind="net"))


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
    **css,
) -> PlaceSpec:
    who = f"Place({ref!r})"
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
    )
    spec = apply_style_to_spec(spec, st)
    spec.rot = float(st.rotate)
    if at is not None:
        spec.position = "absolute"
        spec.from_box = "origin"
        spec.left = at[0]
        spec.top = at[1]
    elif st.is_absolute():
        spec.position = "absolute"
    if spec.locked and spec.at is None and not spec.has_css():
        raise ValueError(
            f"{who}: locked=True needs at=(x, y) or CSS top/right/bottom/left."
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
        no=tuple(no),
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
    minimum. Library symbols are used as-is.
    """
    who = f"SchPlace({ref!r})"
    if "gap" in css:
        raise ValueError(f"{who}: gap= is pin spacing (not CSS). Pass gap= as its own argument.")
    attach = bool(to or along)
    if side is not None and side not in ("left", "right", "top", "bottom"):
        raise ValueError(f"{who}: side must be left/right/top/bottom, got {side!r}")
    spec = SchPlaceSpec(
        ref=str(ref),
        pin=pin,
        to=to,
        along=along,
        gap=float(gap) if gap is not None else None,
        align=align,
        side=side,
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


def NetReq(*nets: str, kind: str, **kwargs) -> NetReqSpec:
    if not nets:
        raise ValueError("NetReq needs at least one net or glob")
    layers = kwargs.get("layers")
    spec = NetReqSpec(
        nets=tuple(str(n) for n in nets),
        kind=str(kind),
        z_diff_ohm=kwargs.get("z_diff_ohm"),
        z_se_ohm=kwargs.get("z_se_ohm"),
        volts=kwargs.get("volts"),
        amps=kwargs.get("amps"),
        temp_rise_c=float(kwargs.get("temp_rise_c", 10)),
        max_mm=kwargs.get("max_mm"),
        match_mm=kwargs.get("match_mm"),
        pair=bool(kwargs.get("pair", False)),
        vias=kwargs.get("vias"),
        layers=tuple(layers) if layers is not None else None,
        keep_clear_of=kwargs.get("keep_clear_of"),
        keep_clear_mm=kwargs.get("keep_clear_mm"),
        autoroute=kwargs.get("autoroute"),
        class_name=kwargs.get("class_name"),
    )
    _doc().netreqs.append(spec)
    return spec


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
    global _board_dir
    path = Path(path).resolve()
    reset()
    _board_dir = path.parent
    ns = {
        "Board": Board,
        "Place": Place,
        "Keepout": Keepout,
        "Region": Region,
        "NetReq": NetReq,
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
        "load": load,
        "__file__": str(path),
        "__name__": "__pcbc__",
    }
    exec(compile(path.read_text(), str(path), "exec"), ns, ns)  # noqa: S102
    design = _doc()
    design.source = str(path)
    return design


def check_board(path: str | Path) -> list[str]:
    design = load_board(path)
    return check_design(design)
