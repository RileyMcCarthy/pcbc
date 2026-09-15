"""Resolve CSS boxes against the board / footprints into KiCad millimetres."""

from __future__ import annotations

from dataclasses import fields, replace

from .css import BoxStyle, Rect, origin_from_border, resolve_rect, rotate_local_bounds
from .geom import footprint_box_local
from .model import BoardSpec, KeepoutSpec, PlaceSpec, RegionSpec
from .sexp import board_footprint_spans, footprint_reference


def board_rect(board: BoardSpec) -> Rect:
    w, h = board.size_mm
    return Rect(0.0, 0.0, float(w), float(h))


def content_rect(board: BoardSpec) -> Rect:
    pt, pr, pb, pl = board.padding
    r = board_rect(board)
    return Rect(r.x0 + pl, r.y0 + pt, r.x1 - pr, r.y1 - pb)


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
            rect = resolve_rect(cb, st, who=f"Region({r.name!r})")
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
    rect = resolve_rect(cb, st, who=f"Keepout({ko.name!r})")
    return (rect.x0, rect.y0, rect.x1, rect.y1)


def resolve_place(
    place: PlaceSpec,
    board: BoardSpec,
    footprint_block: str | None,
    regions: dict[str, Rect] | None = None,
) -> PlaceSpec:
    """Fill ``at`` from CSS. No-op for static places with no insets."""
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
    else:
        local = footprint_box_local(footprint_block, kind)
    ox0, oy0, ox1, oy1 = rotate_local_bounds(*local, st.rotate)
    used_w, used_h = ox1 - ox0, oy1 - oy0
    cb = _cb_for(place, board, regions or {})
    border = resolve_rect(
        cb,
        st,
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
