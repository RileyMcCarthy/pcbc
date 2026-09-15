"""Footprint-local boxes from a KiCad footprint s-expression."""

from __future__ import annotations

import re

from .sexp import matching_paren

_PAD_SIZE = re.compile(r"\(size\s+([0-9.+-]+)\s+([0-9.+-]+)\)")
_XY = re.compile(r"\((?:start|end|center|xy|mid)\s+([0-9.+-]+)\s+([0-9.+-]+)\)")


def footprint_box_local(block: str, kind: str = "courtyard") -> tuple[float, float, float, float]:
    """Local-frame AABB used as the CSS border box.

    kind: courtyard | pads | fab | origin
    Courtyard falls back to pads, then a 1 mm box around the origin.
    """
    if kind == "origin":
        return (0.0, 0.0, 0.0, 0.0)
    if kind == "courtyard":
        box = _graphics_bbox(block, "CrtYd")
        if box is not None:
            return box
        box = _pads_bbox(block)
        return box if box is not None else (-0.5, -0.5, 0.5, 0.5)
    if kind == "fab":
        box = _graphics_bbox(block, ".Fab")
        if box is not None:
            return box
        box = _pads_bbox(block)
        return box if box is not None else (-0.5, -0.5, 0.5, 0.5)
    if kind == "pads":
        box = _pads_bbox(block)
        return box if box is not None else (0.0, 0.0, 0.0, 0.0)
    raise ValueError(f"unknown box kind {kind!r}")


def _iter_tagged(block: str, tag: str):
    token = f"({tag}"
    start = 0
    while True:
        j = block.find(token, start)
        if j < 0:
            return
        # don't match fp_line when looking for fp_li etc — token is exact prefix
        nxt = j + len(token)
        if nxt < len(block) and (block[nxt].isalnum() or block[nxt] == "_"):
            start = nxt
            continue
        end = matching_paren(block, j)
        yield block[j : end + 1]
        start = end + 1


def _graphics_bbox(block: str, layer_substr: str) -> tuple[float, float, float, float] | None:
    pts: list[tuple[float, float]] = []
    for tag in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
        for g in _iter_tagged(block, tag):
            if layer_substr not in g:
                continue
            if tag == "fp_circle":
                pts.extend(_circle_pts(g))
            else:
                pts.extend((float(m.group(1)), float(m.group(2))) for m in _XY.finditer(g))
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _circle_pts(g: str) -> list[tuple[float, float]]:
    center = re.search(r"\(center\s+([0-9.+-]+)\s+([0-9.+-]+)\)", g)
    end = re.search(r"\(end\s+([0-9.+-]+)\s+([0-9.+-]+)\)", g)
    if not center or not end:
        return [(float(m.group(1)), float(m.group(2))) for m in _XY.finditer(g)]
    cx, cy = float(center.group(1)), float(center.group(2))
    ex, ey = float(end.group(1)), float(end.group(2))
    r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
    return [(cx - r, cy - r), (cx + r, cy + r)]


def _pads_bbox(block: str) -> tuple[float, float, float, float] | None:
    from .css import rotate_local_bounds

    pts_x: list[float] = []
    pts_y: list[float] = []
    for pad in _iter_tagged(block, "pad"):
        at = re.search(r"\(at\s+([0-9.+-]+)\s+([0-9.+-]+)(?:\s+([0-9.+-]+))?\)", pad)
        size = _PAD_SIZE.search(pad)
        if not at or not size:
            continue
        ax, ay = float(at.group(1)), float(at.group(2))
        rot = float(at.group(3) or 0)
        sx, sy = float(size.group(1)), float(size.group(2))
        hx, hy = sx / 2.0, sy / 2.0
        x0, y0, x1, y1 = rotate_local_bounds(-hx, -hy, hx, hy, rot)
        pts_x.extend((ax + x0, ax + x1))
        pts_y.extend((ay + y0, ay + y1))
    if not pts_x:
        return None
    return (min(pts_x), min(pts_y), max(pts_x), max(pts_y))


def box_size_mm(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (abs(box[2] - box[0]), abs(box[3] - box[1]))


def footprint_pad_count(block: str) -> int:
    return sum(1 for _ in _iter_tagged(block, "pad"))
