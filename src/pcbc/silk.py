"""The front silkscreen: where each reference designator prints, and the library drawings trimmed.

Every number comes from the stackup's fab limits (`stackup.silk_limits`, JLCPCB's published minimums:
1.0 mm text, 0.15 mm lines, 0.15 mm from a pad's mask opening and from other silk) and from KiCad's
stroke font as measured on the five example boards (2026-10-03, `pcbnew` 10.0.6 exact shapes, 92
references): ink per character is 0.70-0.95 x the text size and the ink is 1.15-1.30 x the size high,
so a reference's box is `INK_W x size x characters` by `INK_H x size`, the measured maxima.

A reference sits where its whole box keeps the fab's clearance from every pad on the board, from every
silk drawing (its own part's included) and from every other reference, stays off every other part's
outline (courtyard, pads and silk: the body that will sit there), and keeps `EDGE_MM` inside the board.
Spots are tried above, below, beside and turned 90 degrees beside the part, then at its corners, in rings
`RINGS` mm out from the part's outline; a part `BIG_MM` or more across may also carry its reference on
its own body (a grid over it, nearest the centre first), clear of its pads. Smallest parts first. When no spot is free the reference is not
printed (hidden), and the fact is a note (`hidden_line`), never an overprint.

The library's own silk is the part's file, copied to the board as it is (`seed.instantiate`); the copy
on the board is the build's. Each `F.SilkS` line is trimmed to the board (the fab's clearance inside the
edge) minus every pad's mask opening plus the fab's clearance; a circle, arc or polygon that crosses
either is left out, and a rectangle that needs trimming becomes its four lines. A pin-1 or polarity mark
(`is_mark`: a circle, arc, polygon or filled shape no wider than `MARK_MM`) is never trimmed away: where the
library draws it is kept when legal; otherwise it moves, whole, to the nearest spot within `MARK_MOVE_MM`
that is (away from the opening it touches first); only with no such spot, or no area to print, is it left
out, and that is a fact (`mark_line`, in the notes and in FAB_NOTES, read off the board by `unprinted_marks`).

The text angle KiCad reads from a footprint's `(property "Reference" (at x y angle))` is the text's
angle on the board, not relative to the footprint (checked in pcbnew: a part at 90 with a text angle
of 270 draws vertical text), so the angle written is the board angle the plan chose.
"""

from __future__ import annotations

import math
import re

from .sexp import board_footprint_spans, footprint_at, footprint_reference, matching_paren, stable_uuid

INK_W = 0.95  # KiCad stroke font: ink width per character per mm of text size (measured maximum)
INK_H = 1.30  # ink height per mm of text size (measured maximum, an underscore included)
EDGE_MM = 0.3  # a reference's box inside the board edge
RINGS = (0.2, 0.5, 0.9)  # mm out from the part's outline
BIG_MM = 6.0  # a part this wide or tall may carry its reference on its body
OUTLINE_GAP = 0.05  # a reference's box off another part's outline
MIN_PIECE = 0.1  # a trimmed silk line shorter than this is left out
MARK_MM = 2.0  # a round, polygon or filled silk drawing no wider than this is a pin-1 or polarity mark
MARK_MOVE_MM = 1.0  # how far a mark may move off its library spot to clear a pad opening, other silk or the edge
_DRAWINGS = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc")
_EPS = 0.005

Box = tuple[float, float, float, float]


def _rot_pt(x: float, y: float, deg: float) -> tuple[float, float]:
    a = math.radians(-deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


# ------------------------------------------------------------------ reading a footprint's drawings


def _num(chunk: str, tag: str) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in re.findall(rf"\({tag}\s+([-0-9.eE+]+)\s+([-0-9.eE+]+)\)", chunk)]


def _arc_points(s, m, e, n: int = 16) -> list[tuple[float, float]]:
    """Points along the arc through start, mid, end (KiCad's three-point arc)."""
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [s, m, e]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, m, e))

    def ccw(a, b):  # angle from a to b going counter-clockwise, in [0, 2pi)
        return (b - a) % (2 * math.pi)

    sweep = ccw(a1, a3)
    if ccw(a1, a2) > sweep:  # the mid point is on the other way round
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * k / n), uy + r * math.sin(a1 + sweep * k / n)) for k in range(n + 1)]


def drawings(block: str, layer: str) -> list[dict]:
    """Every `fp_line`/`fp_rect`/`fp_poly`/`fp_circle`/`fp_arc` of `block` on `layer`, in file order, in
    the footprint's own frame: `{"tag", "index" (order among this layer's drawings), "width", "fill",
    "pts" (outline points: a line's two ends, a rectangle's four corners, a polygon's points, an arc
    and a circle sampled), "closed", "span" (start, end of the chunk in `block`), "uuid"}`."""
    found: list[tuple[int, int, str]] = []
    for tag in _DRAWINGS:
        start = 0
        needle = f"({tag}"
        while True:
            j = block.find(needle, start)
            if j < 0:
                break
            end = matching_paren(block, j)
            start = end + 1
            nxt = block[j + len(needle) : j + len(needle) + 1]
            if nxt and not nxt.isspace():
                continue
            found.append((j, end + 1, tag))
    out: list[dict] = []
    for j, end, tag in sorted(found):
        chunk = block[j:end]
        if f'(layer "{layer}")' not in chunk:
            continue
        wm = re.search(r"\(width\s+([0-9.eE+-]+)\)", chunk)
        width = float(wm.group(1)) if wm else 0.0
        fill = re.search(r"\(fill\s+(yes|solid)\)", chunk) is not None
        closed = tag != "fp_line" and tag != "fp_arc"
        if tag == "fp_line":
            pts = _num(chunk, "start")[:1] + _num(chunk, "end")[:1]
        elif tag == "fp_rect":
            (x0, y0), (x1, y1) = _num(chunk, "start")[0], _num(chunk, "end")[0]
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        elif tag == "fp_poly":
            pts = _num(chunk, "xy")
        elif tag == "fp_circle":
            (cx, cy), (ex, ey) = _num(chunk, "center")[0], _num(chunk, "end")[0]
            r = math.hypot(ex - cx, ey - cy)
            pts = [(cx + r * math.cos(2 * math.pi * k / 32), cy + r * math.sin(2 * math.pi * k / 32)) for k in range(32)]
        else:
            pts = _arc_points(_num(chunk, "start")[0], _num(chunk, "mid")[0], _num(chunk, "end")[0])
        if len(pts) < 2:
            continue
        um = re.search(r'\(uuid\s+"([^"]*)"\)', chunk)
        out.append({"tag": tag, "index": len(out), "width": width, "fill": fill, "pts": tuple(pts), "closed": closed, "span": (j, end), "uuid": um.group(1) if um else ""})
    return out


def _bbox_on_layer(block: str, layer: str, tags: tuple[str, ...] = _DRAWINGS) -> Box | None:
    """The box of every drawing of `tags` on `layer` in the footprint's own frame: a polygon's points,
    a circle's radius and an arc's sweep included, each grown by half its line width."""
    xs: list[float] = []
    ys: list[float] = []
    for d in drawings(block, layer):
        if d["tag"] not in tags:
            continue
        hw = d["width"] / 2
        for x, y in d["pts"]:
            xs += [x - hw, x + hw]
            ys += [y - hw, y + hw]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ------------------------------------------------------------------ geometry


def _overlap(a: Box, b: Box, pad: float = 0.0) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _grow(b: Box, d: float) -> Box:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def _union(boxes) -> Box | None:
    bs = [b for b in boxes if b]
    if not bs:
        return None
    return min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)


def _clip(p0, p1, box: Box) -> tuple[float, float] | None:
    """The parameter interval of segment p0->p1 inside `box` (Liang-Barsky), or None."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    lo, hi = 0.0, 1.0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
        else:
            r = q / p
            if p < 0:
                lo = max(lo, r)
            else:
                hi = min(hi, r)
    return (lo, hi) if lo < hi else None


def _segments(shape: dict):
    pts = shape["pts"]
    pairs = list(zip(pts, pts[1:]))
    if shape["closed"]:
        pairs.append((pts[-1], pts[0]))
    return pairs


def _shape_hits(shape: dict, box: Box, clearance: float) -> bool:
    """Whether a world-frame silk shape comes within `clearance` of `box` (its line width included;
    a filled shape is its whole box)."""
    hw = shape["width"] / 2
    if shape["fill"]:
        return _overlap(box, shape["bbox"], clearance)
    if not _overlap(box, shape["bbox"], clearance + _EPS):
        return False
    grown = _grow(box, clearance + hw)
    return any(_clip(a, b, grown) is not None for a, b in _segments(shape))


def _world(shape: dict, at: tuple[float, float, float]) -> dict:
    fx, fy, rot = at
    pts = tuple((fx + p[0], fy + p[1]) for p in (_rot_pt(x, y, rot) for x, y in shape["pts"]))
    hw = shape["width"] / 2
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {**shape, "pts": pts, "bbox": (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw)}


def pad_box(sp, at: tuple[float, float, float], *, board_angle: bool = False) -> Box | None:
    """A front pad's mask opening as a world box: its copper (a custom pad's primitives' extent),
    turned by its angle, plus its own `solder_mask_margin`. `board_angle`: the pad's angle is a board
    file's (library plus footprint) rather than the library's. None for a pad on no front layer."""
    from .css import rotate_local_bounds

    if not any(t in sp.layers for t in ("F.Cu", "*.Cu", "F.Mask", "*.Mask", "F&B.Cu")):
        return None
    fx, fy, frot = at
    w, h = sp.w, sp.h
    if sp.custom is not None:
        w, h = max(w, sp.custom[0]), max(h, sp.custom[1])
    ang = sp.angle if board_angle else sp.angle + frot
    x0, y0, x1, y1 = rotate_local_bounds(-w / 2, -h / 2, w / 2, h / 2, ang % 360.0)
    cx, cy = _rot_pt(sp.x, sp.y, frot)
    m = sp.mask_margin
    return (fx + cx + x0 - m, fy + cy + y0 - m, fx + cx + x1 + m, fy + cy + y1 + m)


# ------------------------------------------------------------------ the parts


def silk_part(ref: str, at: tuple[float, float, float], crtyd: Box | None, shapes, pads: list[Box], orient: dict | None = None) -> dict:
    """One part as the silkscreen pass reads it: its reference, its `(at x y rot)`, its `F.CrtYd` box in
    its own frame, its `F.SilkS` drawings in its own frame (`drawings`), and its pads' mask openings as
    world boxes (`pad_box`). Its outline (`keep`) is courtyard, silk and pads together. `orient`: the part
    as the orientation rules read it (`orient.part_of`, with its `"layer"`), or None (no orientation pass)."""
    world = [_world(s, at) for s in shapes]
    cbox = None
    if crtyd is not None:
        fx, fy, rot = at
        cs = [_rot_pt(x, y, rot) for x, y in ((crtyd[0], crtyd[1]), (crtyd[2], crtyd[1]), (crtyd[0], crtyd[3]), (crtyd[2], crtyd[3]))]
        cbox = (fx + min(c[0] for c in cs), fy + min(c[1] for c in cs), fx + max(c[0] for c in cs), fy + max(c[1] for c in cs))
    keep = _union([cbox, _union(s["bbox"] for s in world), _union(pads)])
    if keep is None:
        keep = (at[0] - 0.8, at[1] - 0.8, at[0] + 0.8, at[1] + 0.8)
    span = max(keep[2] - keep[0], keep[3] - keep[1])
    return {"ref": ref, "at": at, "keep": keep, "span": span, "pads": list(pads), "silk": world, "orient": orient}


def silk_parts_of_text(text: str) -> list[dict]:
    """Every footprint of a board text as a `silk_part`, in file order (for a board file handed in)."""
    from .pads import pad_specs

    out = []
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        at = footprint_at(block) or (0.0, 0.0, 0.0)
        pads = [b for b in (pad_box(sp, at, board_angle=True) for sp in pad_specs(block)) if b is not None]
        out.append(silk_part(footprint_reference(block) or "?", at, _bbox_on_layer(block, "F.CrtYd"), drawings(block, "F.SilkS"), pads))
    return out


# ------------------------------------------------------------------ the plan


def hidden_line(ref: str, limits: dict) -> str:
    """The note for a reference the plan could not print (`docs/direction.md` §7b: a fact, no proposal)."""
    return (
        f"{ref}: silkscreen reference not printed: no spot for {limits['text_mm']:g} mm text "
        f"({limits['clearance_mm']:g} mm from every pad opening, silk line and other reference, off every other "
        f"part's outline, {EDGE_MM:g} mm inside the board edge) within {RINGS[-1]:g} mm of its outline"
    )


def _text_box(wx: float, wy: float, w: float, h: float, trot: float) -> Box:
    if trot % 180:
        w, h = h, w
    return wx - w / 2, wy - h / 2, wx + w / 2, wy + h / 2


def _spots(keep: Box, tw: float, th: float, big: bool) -> list[tuple[float, float, float]]:
    x0, y0, x1, y1 = keep
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    out: list[tuple[float, float, float]] = []
    for g in RINGS:
        out += [
            (cx, y0 - g - th / 2, 0.0),
            (cx, y1 + g + th / 2, 0.0),
            (x0 - g - tw / 2, cy, 0.0),
            (x1 + g + tw / 2, cy, 0.0),
            (x0 - g - th / 2, cy, 90.0),
            (x1 + g + th / 2, cy, 90.0),
            (cx, y0 - g - tw / 2, 90.0),
            (cx, y1 + g + tw / 2, 90.0),
            (x0 + tw / 2, y0 - g - th / 2, 0.0),
            (x1 - tw / 2, y0 - g - th / 2, 0.0),
            (x0 + tw / 2, y1 + g + th / 2, 0.0),
            (x1 - tw / 2, y1 + g + th / 2, 0.0),
        ]
    if big:
        # On the body: a 7 x 7 grid of centres that keep the box inside the outline, nearest the centre
        # first, upright before turned.
        inside = []
        for trot in (0.0, 90.0):
            w, h = (tw, th) if trot == 0 else (th, tw)
            if w > x1 - x0 or h > y1 - y0:
                continue
            for i in range(7):
                for j in range(7):
                    px = x0 + w / 2 + (x1 - x0 - w) * i / 6
                    py = y0 + h / 2 + (y1 - y0 - h) * j / 6
                    inside.append((round(math.hypot(px - cx, py - cy), 6), trot, px, py))
        out += [(px, py, trot) for _d, trot, px, py in sorted(inside)]
    return out


def _seg_boxes(shape: dict, grow: float) -> list[Box]:
    """Boxes covering a world-frame silk shape's ink, each grown by `grow` (a filled shape: its box)."""
    hw = shape["width"] / 2
    if shape["fill"]:
        return [_grow(shape["bbox"], grow)]
    return [(min(a[0], b[0]) - hw - grow, min(a[1], b[1]) - hw - grow, max(a[0], b[0]) + hw + grow, max(a[1], b[1]) + hw + grow) for a, b in _segments(shape)]


def _area(pts) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2


def _widen(shape: dict, line_mm: float) -> dict:
    """A world-frame silk drawing at the fab's minimum line width (a filled shape is its fill). A filled
    polygon with no area and an outline under the minimum prints nothing the fab can make (node U5's
    EasyEDA SOT-23-5 carries one, 0.35 x 0 mm at width 0): it is marked to be left out."""
    if shape["fill"]:
        if shape["tag"] == "fp_poly" and _area(list(shape["pts"])) < 1e-4 and shape["width"] < line_mm - 1e-9:
            return {**shape, "degenerate": True}
        return shape
    if shape["width"] >= line_mm - 1e-9:
        return shape
    d = (line_mm - shape["width"]) / 2
    return {**shape, "width": line_mm, "widened": True, "bbox": _grow(shape["bbox"], d)}


def _trim(part: dict, keepouts: list[Box], board: Box, clearance: float, silk: list[Box] = ()) -> dict[int, dict | None]:
    """`{drawing index: {"uuid": the drawing's, "width": its line width as printed, "pieces": [(start,
    end) in the footprint frame, ...] (empty: left out; None: kept whole, only widened)}}` for every
    `F.SilkS` drawing of `part` that crosses the board's inset edge, a pad's opening (`keepouts`) plus
    `clearance`, or another part's silk already kept (`silk`: its ink boxes grown by `clearance`), or
    that `_widen` widened; a drawing not listed is kept as it is."""
    fx, fy, rot = part["at"]
    out: dict[int, dict | None] = {}
    for s in part["silk"]:
        hw = s["width"] / 2
        edge = _grow(board, -(clearance + hw + _EPS))
        holes = [_grow(k, clearance + hw + _EPS) for k in keepouts if _overlap(k, s["bbox"], clearance + _EPS)]
        holes += [_grow(k, hw + _EPS) for k in silk if _overlap(k, s["bbox"], _EPS)]
        inside = s["bbox"][0] >= edge[0] - hw and s["bbox"][1] >= edge[1] - hw and s["bbox"][2] <= edge[2] + hw and s["bbox"][3] <= edge[3] + hw
        if s.get("degenerate"):
            out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": [], **({"mark": "no area"} if is_mark(s) else {})}
            continue
        if s["tag"] not in ("fp_line", "fp_rect") or s["fill"]:
            hits = any(_overlap(s["bbox"], h) for h in holes) if s["fill"] else any(_clip(a, b, h) is not None for a, b in _segments(s) for h in holes)
            if (not inside or hits) and is_mark(s):
                # A pin-1 or polarity mark moves whole rather than go (refute:silk: buck U1's pin-1 circle, the ESP32's
                # triangle and ds2 U1's dot were left out, and nothing said so).
                mv = _mark_move(s, keepouts, silk, board, clearance)
                if mv is not None:
                    out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": None, "move": _rot_pt(mv[0], mv[1], -rot)}
                else:
                    out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": [], "mark": "no spot"}
            elif not inside or hits:
                out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": []}
            elif s.get("widened"):
                out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": None}
            continue
        if inside and not holes:
            if s.get("widened"):
                out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": None}
            continue
        pieces: list[tuple[tuple[float, float], tuple[float, float]]] = []
        changed = False
        for a, b in _segments(s):
            got = _clip(a, b, edge)
            ivs = [got] if got else []
            for h in holes:
                cut = _clip(a, b, h)
                if not cut:
                    continue
                nv = []
                for lo, hi in ivs:
                    if cut[1] <= lo or cut[0] >= hi:
                        nv.append((lo, hi))
                        continue
                    if cut[0] > lo:
                        nv.append((lo, cut[0]))
                    if cut[1] < hi:
                        nv.append((cut[1], hi))
                ivs = nv
            length = math.dist(a, b)
            ivs = [(lo, hi) for lo, hi in ivs if (hi - lo) * length >= MIN_PIECE]
            if ivs != [(0.0, 1.0)]:
                changed = True
            for lo, hi in ivs:
                pa = (a[0] + (b[0] - a[0]) * lo, a[1] + (b[1] - a[1]) * lo)
                pb = (a[0] + (b[0] - a[0]) * hi, a[1] + (b[1] - a[1]) * hi)
                # back to the footprint frame
                la = _rot_pt(pa[0] - fx, pa[1] - fy, -rot)
                lb = _rot_pt(pb[0] - fx, pb[1] - fy, -rot)
                pieces.append((la, lb))
        if changed:
            out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": pieces}
        elif s.get("widened"):
            out[s["index"]] = {"uuid": s["uuid"], "width": s["width"], "pieces": None}
    return out


def is_mark(shape: dict) -> bool:
    """A pin-1 or polarity mark: a circle, arc or polygon, or any filled shape, no wider than `MARK_MM` (its points;
    a world shape's box when it has one)."""
    if shape["tag"] in ("fp_line", "fp_rect") and not shape["fill"]:
        return False
    xs = [q[0] for q in shape["pts"]]
    ys = [q[1] for q in shape["pts"]]
    return max(max(xs) - min(xs), max(ys) - min(ys)) <= MARK_MM + 1e-9


def _shifted(shape: dict, dx: float, dy: float) -> dict:
    x0, y0, x1, y1 = shape["bbox"]
    return {**shape, "pts": tuple((q[0] + dx, q[1] + dy) for q in shape["pts"]), "bbox": (x0 + dx, y0 + dy, x1 + dx, y1 + dy)}


def _mark_clear(shape: dict, edge: Box, holes: list[Box]) -> bool:
    hw = shape["width"] / 2
    b = shape["bbox"]
    if b[0] < edge[0] - hw - 1e-9 or b[1] < edge[1] - hw - 1e-9 or b[2] > edge[2] + hw + 1e-9 or b[3] > edge[3] + hw + 1e-9:
        return False
    if shape["fill"]:
        return not any(_overlap(b, h) for h in holes)
    return not any(_clip(a, c, h) is not None for a, c in _segments(shape) for h in holes)


def _mark_move(shape: dict, keepouts: list[Box], silk: list[Box], board: Box, clearance: float) -> tuple[float, float] | None:
    """The smallest world offset (dx, dy), up to `MARK_MOVE_MM`, that puts the mark `clearance` from every pad opening
    (`keepouts`), off every kept silk box (`silk`, already grown by the clearance) and inside the board's inset edge;
    rings of 0.05 mm, 16 directions, the one straight away from the nearest obstacle first. None with no such spot."""
    hw = shape["width"] / 2
    edge = _grow(board, -(clearance + hw + _EPS))
    reach = _grow(shape["bbox"], MARK_MOVE_MM + clearance + hw + 0.2)
    holes = [_grow(k, clearance + hw + _EPS) for k in keepouts if _overlap(k, reach)]
    holes += [_grow(k, hw + _EPS) for k in silk if _overlap(k, reach)]
    cx, cy = (shape["bbox"][0] + shape["bbox"][2]) / 2, (shape["bbox"][1] + shape["bbox"][3]) / 2
    if holes:
        near = min(holes, key=lambda h: math.hypot(min(max(cx, h[0]), h[2]) - cx, min(max(cy, h[1]), h[3]) - cy))
        hx, hy = (near[0] + near[2]) / 2, (near[1] + near[3]) / 2
        base = math.atan2(cy - hy, cx - hx) if (cx, cy) != (hx, hy) else 0.0
    else:
        # only the edge is in the way: straight in, towards the board's centre
        base = math.atan2((board[1] + board[3]) / 2 - cy, (board[0] + board[2]) / 2 - cx)
    turns = [0] + [k * sgn for k in range(1, 9) for sgn in (1, -1)][:15]
    n = int(round(MARK_MOVE_MM / 0.05))
    for i in range(1, n + 1):
        r = i * 0.05
        for k in turns:
            a = base + k * math.pi / 8
            dx, dy = round(r * math.cos(a), 4), round(r * math.sin(a), 4)
            if _mark_clear(_shifted(shape, dx, dy), edge, holes):
                return dx, dy
    return None


def _translate(chunk: str, dx: float, dy: float) -> str:
    """A drawing's text with every point (start, end, center, mid, xy) moved by (dx, dy), in its own frame."""

    def mv(m):
        return f"({m.group(1)} {float(m.group(2)) + dx:.4f} {float(m.group(3)) + dy:.4f})"

    return re.sub(r"\((start|end|center|mid|xy)\s+([-0-9.eE+]+)\s+([-0-9.eE+]+)\)", mv, chunk)


def _mark_words(shape: dict) -> str:
    xs = [q[0] for q in shape["pts"]]
    ys = [q[1] for q in shape["pts"]]
    kind = {"fp_circle": "circle", "fp_arc": "arc", "fp_poly": "polygon", "fp_rect": "rectangle", "fp_line": "line"}[shape["tag"]]
    return f"{'filled ' if shape['fill'] else ''}{kind} {max(xs) - min(xs):.2f} x {max(ys) - min(ys):.2f} mm"


def mark_line(ref: str, shape: dict, why: str, limits: dict) -> str:
    """The fact for a pin-1 or polarity mark the plan could not print (docs/direction.md §7b: no proposal)."""
    if why == "no area":
        return f"{ref}: silkscreen mark not printed: the library's {_mark_words(shape)} mark has no area and a line under {limits['line_mm']:g} mm"
    return (
        f"{ref}: silkscreen mark not printed: the library's {_mark_words(shape)} mark has no spot within {MARK_MOVE_MM:g} mm of where "
        f"the library draws it that is {limits['clearance_mm']:g} mm from every pad opening and other silk and inside the board edge"
    )


def unprinted_marks(text: str, lib_shapes: dict[str, tuple], limits: dict, reason=None) -> list[str]:
    """Read off a board text (fab, B.4's rule: what fab says is the board's): each library pin-1 or polarity mark
    (`is_mark`, from `lib_shapes`, `{ref: the part's library F.SilkS drawings}`) that no `F.SilkS` drawing of the
    footprint matches, moved or not, as its `mark_line`. `reason(ref, shape)`: the fact for a mark the silk pass
    left out because it names another pin than the part's mark (`orient.library_mark_reason`), or None."""
    out = []
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        have = drawings(block, "F.SilkS")

        def rel(sh):
            x0, y0 = sh["pts"][0]
            return [(round(x - x0, 3), round(y - y0, 3)) for x, y in sh["pts"]]

        for s in lib_shapes.get(ref, ()):
            if not is_mark(s):
                continue
            if any(h["tag"] == s["tag"] and h["fill"] == s["fill"] and rel(h) == rel(s) for h in have):
                continue
            degenerate = s["fill"] and s["tag"] == "fp_poly" and _area(list(s["pts"])) < 1e-4 and s["width"] < limits["line_mm"] - 1e-9
            other = reason(ref, s) if reason is not None and not degenerate else None
            out.append(other or mark_line(ref, s, "no area" if degenerate else "no spot", limits))
    return out


def silk_plan(parts: list[dict], board_mm: tuple[float, float], limits: dict, extra: list[tuple[str, dict]] = ()) -> tuple[dict, dict]:
    """(`{ref: {"ref": where its Reference goes (`_set_reference` kwargs), "trim": `_trim`'s}}`, the
    report) for `parts` (`silk_part`s). Reads no board text: the place stage hands it the parts from
    their `.kicad_mod`s and poses. `limits` is `stackup.silk_limits`. `extra`: the board's own `F.SilkS`
    drawings (`layout.core.py` lines), as (the line, a world shape): kept clear of, and counted as marks.

    The report: `hidden` (refs not printed, fiducials aside), `notes` (one `hidden_line` each),
    `moved` (where each printed reference went) and `trimmed` (library drawings trimmed or left out).
    With the parts' `orient` given, the orientation marks (`orient.plan`) are decided after the library
    silk and before the references: `orient` (one row per part that needs or gets a mark), `orient_marks`
    (the marks generated, world primitives), `orient_errors` (a required mark with no legal spot, a core
    drawing naming another pin: facts); a library mark naming another pin is left out, with its fact."""
    size = limits["text_mm"]
    thick = round(max(limits["line_mm"], size / 6.0), 3)
    clr = limits["clearance_mm"]
    # (x0, y0, x1, y1) of the outline the references stay inside, or a (w, h) from (0, 0).
    board = tuple(float(v) for v in board_mm) if len(board_mm) == 4 else (0.0, 0.0, float(board_mm[0]), float(board_mm[1]))
    inset = _grow(board, -EDGE_MM)
    all_pads = [b for p in parts for b in p["pads"]]
    report: dict = {"moved": [], "hidden": [], "notes": [], "trimmed": []}
    out: dict[str, dict] = {}
    # Library silk, biggest part first: each part's drawings trimmed to the board, off every pad's
    # opening, and off the silk of the bigger parts already kept (two parts' silk is one fab's print).
    all_silk: list[dict] = []  # every library drawing as it will print: trimmed, in the world frame
    prov: list[tuple[str, int]] = []  # (ref, library drawing index) of each `all_silk` entry
    parts = [{**p, "silk": [_widen(s, limits["line_mm"]) for s in p["silk"]]} for p in parts]
    for p in sorted(parts, key=lambda p: (-p["span"], p["ref"])):
        kept = [b for s in all_silk for b in _seg_boxes(s, clr)] + [b for _w, s in extra for b in _seg_boxes(s, clr)]
        trim = _trim(p, all_pads, board, clr, kept)
        out[p["ref"]] = {"trim": trim}
        if trim:
            report["trimmed"].append(
                {
                    "ref": p["ref"],
                    "trimmed": sum(1 for v in trim.values() if v["pieces"]),
                    "left_out": sum(1 for v in trim.values() if v["pieces"] == []),
                    "widened": sum(1 for s in p["silk"] if s.get("widened")),
                }
            )
        for s in p["silk"]:
            t = trim.get(s["index"])
            if t is not None and t.get("move"):
                all_silk.append(_shifted(s, *_rot_pt(t["move"][0], t["move"][1], p["at"][2])))
                prov.append((p["ref"], s["index"]))
            elif t is None or t["pieces"] is None:
                all_silk.append(s)
                prov.append((p["ref"], s["index"]))
            else:
                for a, b in trim[s["index"]]["pieces"]:
                    piece = {**s, "tag": "fp_line", "fill": False, "closed": False, "pts": (a, b)}
                    all_silk.append(_world(piece, p["at"]))
                    prov.append((p["ref"], s["index"]))
    if any(p.get("orient") for p in parts):
        _orient_pass(parts, all_silk, prov, out, report, extra, board, limits)
    all_silk += [s for _w, s in extra]
    report["trimmed"].sort(key=lambda r: r["ref"])
    report["marks_moved"] = []
    report["marks_dropped"] = []
    for p in parts:
        for s in p["silk"]:
            t = out[p["ref"]]["trim"].get(s["index"])
            if t is None:
                continue
            if t.get("move"):
                report["marks_moved"].append({"ref": p["ref"], "tag": s["tag"], "by": round(math.hypot(*t["move"]), 2)})
            elif t.get("mark"):
                report["marks_dropped"].append({"ref": p["ref"], "tag": s["tag"]})
                report["notes"].append(t.get("fact") or mark_line(p["ref"], s, t["mark"], limits))
    # Each reference's spots clear of everything but the other references, in the order tried.
    order = [p for p in sorted(parts, key=lambda p: (p["span"], p["ref"])) if not p["ref"].startswith("FID")]
    free: dict[str, list[tuple[float, float, float, Box]]] = {}
    for p in order:
        ref = p["ref"]
        tw, th = INK_W * size * max(len(ref), 1), INK_H * size
        big = p["span"] >= BIG_MM
        free[ref] = []
        for wx, wy, trot in _spots(p["keep"], tw, th, big):
            box = _text_box(wx, wy, tw, th, trot)
            if box[0] < inset[0] or box[1] < inset[1] or box[2] > inset[2] or box[3] > inset[3]:
                continue
            if any(_overlap(box, pb, clr) for pb in all_pads):
                continue
            if not big and _overlap(box, p["keep"]):
                continue
            if any(_overlap(box, q["keep"], OUTLINE_GAP) for q in parts if q is not p):
                continue
            if any(_shape_hits(s, box, clr) for s in all_silk):
                continue
            free[ref].append((wx, wy, trot, box))
    # Which spots of two references clash (closer than the clearance), between references whose spots
    # can meet at all.
    refs = [p["ref"] for p in order]
    reach = {r: _union(f[3] for f in free[r]) for r in refs}
    clash: dict[tuple[str, int], list[tuple[str, int]]] = {(r, i): [] for r in refs for i in range(len(free[r]))}
    for a_i, a in enumerate(refs):
        for b in refs[a_i + 1 :]:
            if reach[a] is None or reach[b] is None or not _overlap(reach[a], reach[b], clr):
                continue
            for i, fa in enumerate(free[a]):
                for j, fb in enumerate(free[b]):
                    if _overlap(fa[3], fb[3], clr):
                        clash[(a, i)].append((b, j))
                        clash[(b, j)].append((a, i))
    # The reference with the fewest spots left goes next (ties: the smaller part, then the name), at its
    # first spot (the order `_spots` tries them in) that leaves every reference still to place a spot;
    # when every spot takes some reference's last one, the spot that takes the fewest.
    live = {r: set(range(len(free[r]))) for r in refs}
    taken: dict[str, int] = {}
    span = {p["ref"]: p["span"] for p in order}
    while len(taken) + sum(1 for r in refs if r not in taken and not live[r]) < len(refs):
        todo = [r for r in refs if r not in taken and live[r]]
        ref = min(todo, key=lambda r: (len(live[r]), span[r], r))
        best = None
        for i in sorted(live[ref]):
            hit: dict[str, set[int]] = {}
            for r2, j in clash[(ref, i)]:
                if r2 not in taken and r2 != ref and j in live[r2]:
                    hit.setdefault(r2, set()).add(j)
            starved = sum(1 for r2, js in hit.items() if live[r2] <= js)
            if best is None or starved < best[0]:
                best = (starved, i, hit)
            if starved == 0:
                break
        _starved, i, hit = best
        taken[ref] = i
        for r2, js in hit.items():
            live[r2] -= js
    for p in sorted(parts, key=lambda p: (p["span"], p["ref"])):
        ref = p["ref"]
        fx, fy, frot = p["at"]
        if ref.startswith("FID"):
            out[ref]["ref"] = dict(lx=0, ly=0, prot=0, size=0.5, thick=0.08, hide=True)
            continue
        if ref not in taken:
            out[ref]["ref"] = dict(lx=0, ly=0, prot=0, size=size, thick=thick, hide=True)
            report["hidden"].append(ref)
            report["notes"].append(hidden_line(ref, limits))
            continue
        wx, wy, trot, _box = free[ref][taken[ref]]
        lx, ly = _rot_pt(wx - fx, wy - fy, -frot)
        out[ref]["ref"] = dict(lx=lx, ly=ly, prot=trot, size=size, thick=thick, hide=False)
        report["moved"].append({"ref": ref, "at": [round(wx, 2), round(wy, 2)], "angle": trot, "size": size})
    return out, report


def _orient_pass(parts, all_silk, prov, out, report, extra, board, limits) -> None:
    """`orient.plan` over the library silk as it will print (`all_silk`, `prov`) and the board's own drawings
    (`extra`): a library mark that names another pin leaves `all_silk` and is left out of its footprint
    (`trim`, with its fact); each generated mark joins `all_silk`, so the references keep clear of it."""
    from .orient import plan, prim_shape, row_line

    oparts = [p["orient"] for p in parts if p.get("orient")]
    pads = [(r, n, b) for op in oparts for r, n, b in ((op["ref"], q["num"], q["box"]) for q in op["pads"])]
    known = {op["ref"] for op in oparts}
    pads += [(p["ref"], "", b) for p in parts if p["ref"] not in known for b in p["pads"]]
    printed = [(f"{ref}'s library silk", ref, s) for s, (ref, _i) in zip(all_silk, prov)] + [(w, None, s) for w, s in extra]
    got = plan(oparts, printed, pads, board, limits)
    gone = set()
    for i, fact in got["drop"]:
        ref, index = prov[i]
        lib = next(s for p in parts if p["ref"] == ref for s in p["silk"] if s["index"] == index)
        out[ref]["trim"][index] = {"uuid": lib["uuid"], "width": lib["width"], "pieces": [], "mark": "another pin", "fact": fact}
        gone.add(i)
    keep = [k for k in range(len(all_silk)) if k not in gone]
    all_silk[:] = [all_silk[k] for k in keep]
    prov[:] = [prov[k] for k in keep]
    for m in got["marks"]:
        all_silk.append(prim_shape(m["prim"]))
        prov.append((m["ref"], -1))
    report["orient"] = got["rows"]
    report["orient_marks"] = got["marks"]
    report["orient_errors"] = got["errors"]
    report["orient_lines"] = [row_line(r) for r in got["rows"]]
    report["notes"] += got["notes"]


# ------------------------------------------------------------------ writing it


def _set_reference(block: str, *, lx: float, ly: float, prot: float, size: float, thick: float, hide: bool) -> str:
    j = block.find('(property "Reference"')
    if j < 0:
        return block
    open_at = block.find("(", j)
    end = matching_paren(block, open_at)
    prop = block[open_at : end + 1]
    prop = re.sub(r"\(at [0-9.eE+-]+ [0-9.eE+-]+(?: [0-9.eE+-]+)?\)", f"(at {lx:.4f} {ly:.4f} {prot:g})", prop, count=1)
    prop = re.sub(r"\(size [0-9.]+ [0-9.]+\)", f"(size {size:g} {size:g})", prop, count=1)
    if re.search(r"\(thickness [0-9.]+\)", prop):
        prop = re.sub(r"\(thickness [0-9.]+\)", f"(thickness {thick:g})", prop, count=1)
    else:
        prop = prop.replace(f"(size {size:g} {size:g})", f"(size {size:g} {size:g})\n\t\t\t\t\t(thickness {thick:g})", 1)
    if hide and "(hide yes)" not in prop:
        prop = prop.replace('(layer "F.SilkS")', '(layer "F.SilkS")\n\t\t\t(hide yes)', 1)
    if not hide:
        # The plan decides whether a reference prints; a library's own `(hide yes)` does not survive it.
        prop = re.sub(r"\n[ \t]*\(hide yes\)", "", prop)
    return block[:open_at] + prop + block[end + 1 :]


def _line_text(chunk: str, a, b, uid: str, width: float) -> str:
    """An `fp_line` from `a` to `b`, `width` wide, in the stroke type of the drawing `chunk`."""
    sm = re.search(r"\(stroke\b", chunk)
    stroke = chunk[sm.start() : matching_paren(chunk, sm.start()) + 1] if sm else "(stroke (width 0.12) (type solid))"
    stroke = re.sub(r"\(width\s+[0-9.eE+-]+\)", f"(width {width:g})", " ".join(stroke.split()), count=1)
    return f'(fp_line (start {a[0]:.4f} {a[1]:.4f}) (end {b[0]:.4f} {b[1]:.4f}) {stroke} (layer "F.SilkS") (uuid "{uid}"))'


def _piece_uuid(ref: str, index: int, base: str, k: int) -> str:
    """Piece `k` of the part's `index`-th silk drawing (whose own uuid is `base`, empty when the library
    file carries none: a fetched part's are stripped)."""
    return stable_uuid("silk", ref, index, base, k)


def _apply_trim(block: str, ref: str, trim: dict) -> str:
    """`block` with each trimmed library drawing replaced by its pieces (`fp_line`s) and each one left
    out removed with its line."""
    if not trim:
        return block
    out: list[str] = []
    last = 0
    for d in drawings(block, "F.SilkS"):
        if d["index"] not in trim:
            continue
        j, end = d["span"]
        nl = block.rfind("\n", last, j)
        t = trim[d["index"]]
        if t["pieces"] is None:
            chunk = block[j:end]
            if t.get("move"):
                chunk = _translate(chunk, *t["move"])
            out.append(block[last:j])
            out.append(re.sub(r"\(width\s+[0-9.eE+-]+\)", f"(width {t['width']:g})", chunk, count=1))
        elif not t["pieces"]:
            out.append(block[last : nl if nl >= 0 else j])
        else:
            chunk = block[j:end]
            lead = block[nl + 1 : j] if nl >= 0 else ""
            out.append(block[last:j])
            out.append(("\n" + lead).join(_line_text(chunk, a, b, _piece_uuid(ref, d["index"], t["uuid"], k), t["width"]) for k, (a, b) in enumerate(t["pieces"])))
        last = end
    out.append(block[last:])
    return "".join(out)


def silk_uuids(decisions: dict) -> dict[str, str]:
    """`{uuid: ref}` of every trimmed silk line piece the plan writes (`_apply_trim`)."""
    out: dict[str, str] = {}
    for ref, d in decisions.items():
        for index, t in (d.get("trim") or {}).items():
            for k in range(len(t["pieces"] or ())):
                out[_piece_uuid(ref, index, t["uuid"], k)] = ref
    return out


def silk_gone_uuids(decisions: dict) -> dict[str, set[str]]:
    """`{ref: the library drawings' own uuids the plan takes off that part}` (each drawing it trimmed or
    left out; another part made from the same `.kicad_mod` may still carry them)."""
    return {ref: {t["uuid"] for t in (d.get("trim") or {}).values() if t["uuid"] and t["pieces"] is not None} for ref, d in decisions.items()}


def apply_silk(text: str, decisions: dict) -> str:
    """Every footprint's Reference property and library silk as `silk_plan` decided."""
    pieces: list[str] = []
    last = 0
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        d = decisions.get(ref)
        pieces.append(text[last:start])
        if d is not None:
            block = _apply_trim(block, ref, d.get("trim") or {})
            if d.get("ref") is not None:
                block = _set_reference(block, **d["ref"])
        pieces.append(block)
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def hidden_references(text: str) -> list[str]:
    """The references a board text does not print (its `Reference` property on `F.SilkS` hidden),
    fiducials aside, in file order. The place stage's plan is the only writer of that flag
    (`_set_reference` drops a library's own), so each is a reference `silk_plan` found no spot for."""
    out = []
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        j = block.find('(property "Reference"')
        if j < 0 or ref.startswith("FID"):
            continue
        prop = block[j : matching_paren(block, j) + 1]
        if '(layer "F.SilkS")' in prop and re.search(r"\(hide yes\)", prop):
            out.append(ref)
    return out


def legalize_silk(text: str, board_mm: tuple[float, float], limits: dict | None = None) -> tuple[str, dict]:
    """A board text's silkscreen planned and written (`silk_plan` over `silk_parts_of_text`, then
    `apply_silk`). Does not move footprints. `limits` defaults to the 2-layer JLC stackup's."""
    if limits is None:
        from .stackup import get_stackup, silk_limits

        limits = silk_limits(get_stackup("jlcpcb_2l_1oz"))
    decisions, report = silk_plan(silk_parts_of_text(text), board_mm, limits)
    return apply_silk(text, decisions), report
