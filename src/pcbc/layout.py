"""Resolve CSS boxes against the board / footprints into KiCad millimetres."""

from __future__ import annotations

from dataclasses import fields, replace

from .css import BoxStyle, Rect, origin_from_border, resolve_rect, rotate_local_bounds
from .geom import footprint_box_local
from .model import BoardSpec, KeepoutSpec, PlaceSpec, RegionSpec
from .sexp import board_footprint_spans, footprint_reference


# The derived frame's far edges (docs/board-outline.md B.1): with no `Outline()` a `left`/`top` number is a frame
# coordinate and nothing measures from a right or bottom edge (B.5-1), so the containing block only has to hold it.
FRAME_FAR = 1.0e4


def board_rect(board: BoardSpec) -> Rect:
    w, h = board.size_mm
    ox, oy = getattr(board, "origin", (0.0, 0.0)) or (0.0, 0.0)
    return Rect(float(ox), float(oy), float(ox) + float(w), float(oy) + float(h))


def declared_outline(design) -> tuple[tuple[float, float, float, float] | None, str, int]:
    """(the declared outline as (x0, y0, x1, y1) in the frame, "declared", its layout.core.py line) from
    `Outline()`, or (None, "derived", 0) with none (docs/board-outline.md B). A design built in Python with a
    `BoardSpec(size_mm=...)` keeps that size as its declared outline."""
    o = getattr(design, "outline", None)
    if o is not None:
        x, y = o.at
        # Rounded to KiCad's 1 nm: `at` + `width` in floats gives 34.120000000000005, which the board text cannot hold.
        return (round(float(x), 6), round(float(y), 6), round(float(x) + float(o.width), 6), round(float(y) + float(o.height), 6)), "declared", int(o.line or 0)
    b = design.board
    if b is not None and b.size_mm is not None:
        ox, oy = b.origin or (0.0, 0.0)
        return (round(float(ox), 6), round(float(oy), 6), round(float(ox) + float(b.size_mm[0]), 6), round(float(oy) + float(b.size_mm[1]), 6)), "declared", 0
    return None, "derived", 0


def frame_board(board: BoardSpec, rect) -> BoardSpec:
    """`board` with its extent set to `rect` (x0, y0, x1, y1): the box CSS resolves against. With no rect (derived
    mode, before placement) the frame from (0, 0) to `FRAME_FAR`."""
    from dataclasses import replace as _replace

    x0, y0, x1, y1 = rect if rect is not None else (0.0, 0.0, FRAME_FAR, FRAME_FAR)
    return _replace(board, size_mm=(x1 - x0, y1 - y0), origin=(x0, y0))


def _g(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else repr(v)


def unframed_lengths(spec, *, need_size: bool = False) -> str | None:
    """Why `spec` (a Place, Region or Keepout) cannot be resolved with no `Outline()` (docs/board-outline.md B.5-1),
    as the fact after its name, or None. In derived mode `left`/`top` (or `at=`) are frame coordinates; `right`,
    `bottom`, `margin: auto` and `%` measure from an outline edge the placement itself derives, and an axis with none
    of them would be centred in a width or height there is none of."""
    from .css import AUTO, Percent

    tail = "this board has no Outline(), and its {edge} edge is derived from the placement"
    for key, edge in (("right", "right"), ("bottom", "bottom")):
        v = getattr(spec, key, None)
        if v is not None:
            return f"{key}={_g(v)} measures from the outline's {edge} edge; " + tail.format(edge=edge)
    for key in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        if getattr(spec, key, 0) == AUTO or str(getattr(spec, key, 0)) == "auto":
            edge = "right" if key in ("margin_left", "margin_right") else "bottom"
            return f"{key.replace('_', '-')}: auto centres it in the outline; " + tail.format(edge=edge)
    for key in ("left", "top", "width", "height"):
        v = getattr(spec, key, None)
        if isinstance(v, Percent) or (isinstance(v, str) and v.strip().endswith("%")):
            edge = "right" if key in ("left", "width") else "bottom"
            return f"{key}= is a % of the outline; " + tail.format(edge=edge)
    if getattr(spec, "at", None) is None:
        if getattr(spec, "left", None) is None:
            return "has no left= or at=: with no Outline() there is no width to centre it in"
        if getattr(spec, "top", None) is None:
            return "has no top= or at=: with no Outline() there is no height to centre it in"
    if need_size:
        for key in ("width", "height"):
            if getattr(spec, key, None) is None:
                return f"has no {key}=: with no Outline() there is no {key} to stretch it to"
    return None


def _frame(st: BoxStyle, board: BoardSpec, spec) -> BoxStyle:
    """`left`/`top` numbers are frame coordinates (docs/board-outline.md B.1): against a board whose corner is not the
    frame's origin (a derived outline, or `Outline(at=)`), they are taken back to the board's corner. A Region's own box
    (a `parent=`) is not the frame, and keeps CSS's meaning; so does the schematic sheet, whose corner is (0, 0)."""
    if getattr(spec, "parent", None):
        return st
    ox, oy = getattr(board, "origin", (0.0, 0.0)) or (0.0, 0.0)
    if isinstance(st.left, (int, float)) and not isinstance(st.left, bool) and ox:
        st.left = st.left - ox
    if isinstance(st.top, (int, float)) and not isinstance(st.top, bool) and oy:
        st.top = st.top - oy
    return st
    ox, oy = getattr(board, "origin", (0.0, 0.0)) or (0.0, 0.0)
    # An `edge=` part's own inset (`left=` of an edge="left" part, as `pcb_place._edge_css` writes it) and its centring
    # (`left=0, right=0, margin: auto`) measure from the outline's edge, not from the frame: only a frame coordinate moves
    # with the corner (Outline(at=(5, 7)) put an edge="left" part 5 mm off the board).
    edge = getattr(spec, "edge", None)

    def board_relative(axis: str) -> bool:
        if not edge:
            return False
        if edge == axis:
            return True
        margin = st.margin_left if axis == "left" else st.margin_top
        return str(margin) == "auto"

    if isinstance(st.left, (int, float)) and not isinstance(st.left, bool) and ox and not board_relative("left"):
        st.left = st.left - ox
    if isinstance(st.top, (int, float)) and not isinstance(st.top, bool) and oy and not board_relative("top"):
        st.top = st.top - oy
    return st


def content_rect(board: BoardSpec) -> Rect:
    """The box placement and routing fit the parts into: the outline. There is no inset (docs/direction.md §2,
    2026-10-09): a board's edge clearance is its stackup's rule, and a part kept off an edge is a Keepout or a Place in
    layout.core.py."""
    return board_rect(board)


def style_from(spec) -> BoxStyle:
    st = BoxStyle()
    for f in fields(BoxStyle):
        if hasattr(spec, f.name):
            setattr(st, f.name, getattr(spec, f.name))
    if hasattr(spec, "rot"):
        st.rotate = float(spec.rot)
    return st


def _cb_for(spec, board: BoardSpec, regions: dict[str, Rect]) -> Rect:
    parent = getattr(spec, "parent", None)
    if parent:
        if parent not in regions:
            who = getattr(spec, "ref", None) or getattr(spec, "name", "box")
            raise ValueError(f"{who}: unknown parent Region {parent!r}")
        return regions[parent]
    # Keepouts sit on Edge.Cuts. Parts and regions sit in the padded content box.
    if isinstance(spec, KeepoutSpec):
        return board_rect(board)
    return content_rect(board)


def resolve_regions(board: BoardSpec, regions: list[RegionSpec]) -> dict[str, Rect]:
    done: dict[str, Rect] = {}
    pending = list(regions)
    guard = 0
    while pending:
        guard += 1
        if guard > 64:
            names = [r.name for r in pending]
            raise ValueError(f"Region parent cycle or missing parent: {names}")
        nxt = []
        for r in pending:
            if r.parent and r.parent not in done:
                nxt.append(r)
                continue
            st = style_from(r)
            cb = _cb_for(r, board, done)
            rect = resolve_rect(cb, _frame(st, board, r), who=f"Region({r.name!r})")
            # Region padding is inside; children use the content box.
            done[r.name] = rect.content_box(st)
        if len(nxt) == len(pending) and nxt:
            names = [r.name for r in nxt]
            raise ValueError(f"Region parent cycle or missing parent: {names}")
        pending = nxt
    return done


def resolve_keepout(
    ko: KeepoutSpec,
    board: BoardSpec,
    regions: dict[str, Rect] | None = None,
) -> tuple[float, float, float, float]:
    if ko.box is not None and not any(
        getattr(ko, k) is not None for k in ("top", "right", "bottom", "left", "width", "height")
    ):
        return ko.box
    st = style_from(ko)
    cb = _cb_for(ko, board, regions or {})
    rect = resolve_rect(cb, _frame(st, board, ko), who=f"Keepout({ko.name!r})")
    return (rect.x0, rect.y0, rect.x1, rect.y1)


def resolve_place(
    place: PlaceSpec,
    board: BoardSpec,
    footprint_block,
    regions: dict[str, Rect] | None = None,
) -> PlaceSpec:
    """Fill ``at`` from CSS. No-op for static places with no insets. `footprint_block` is the part's
    `foot_native.LibFoot` (placement) or a footprint block of a board text (`check`, on the emitted
    board), or None."""
    if place.at is not None and not place.has_css():
        return place
    st = style_from(place)
    if place.at is not None and st.left is None and st.top is None:
        return place
    if not st.is_absolute() and place.at is None:
        return place
    kind = st.from_box or "courtyard"
    if kind == "origin" or footprint_block is None:
        local = (0.0, 0.0, 0.0, 0.0)
    elif isinstance(footprint_block, str):
        local = footprint_box_local(footprint_block, kind)
    else:
        local = footprint_block.box(kind)  # a `foot_native.LibFoot`: the part's own `.kicad_mod`
    ox0, oy0, ox1, oy1 = rotate_local_bounds(*local, st.rotate)
    used_w, used_h = ox1 - ox0, oy1 - oy0
    cb = _cb_for(place, board, regions or {})
    border = resolve_rect(
        cb,
        _frame(st, board, place),
        intrinsic_w=used_w,
        intrinsic_h=used_h,
        who=f"Place({place.ref!r})",
    )
    at = origin_from_border(border, local, st.rotate)
    return replace(place, at=(at[0], at[1]), rot=float(st.rotate), position="absolute")


def footprints_by_ref(pcb_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for start, end in board_footprint_spans(pcb_text):
        block = pcb_text[start:end]
        ref = footprint_reference(block)
        if ref:
            out[ref] = block
    return out
