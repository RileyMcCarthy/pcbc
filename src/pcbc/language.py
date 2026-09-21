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
    # **The 2-layer refusal is gone, and what removed it is the router reading the declaration.**
    # It was added when `route.krt_plan` took `planes=` only above two layers and poured `GND` on
    # `B.Cu` itself below that, so a 2-layer `planes=` reached no step at all: `Board()` accepted it,
    # the router ignored it, the ampacity measurement exempted the net on the strength of it, and
    # `FAB_NOTES.md` reported a rail as poured that had no pour. "A declaration the router never
    # reads is a lie the board tells its author" was the right refusal for that router.
    #
    # S8 made the router read it (`route_scene.plane_targets`, `route.krt_plan`): a declared pour is
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
