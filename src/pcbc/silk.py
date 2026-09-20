"""Legalize F.SilkS reference text: size from courtyard, slots off the body."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path

from .compile import CompiledJob
from .sexp import board_footprint_spans, footprint_at, footprint_reference, matching_paren


def _rot_pt(x: float, y: float, deg: float) -> tuple[float, float]:
    a = math.radians(-deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def _bbox_on_layer(block: str, layer: str, tags: tuple[str, ...]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for tag in tags:
        start = 0
        needle = f"({tag}"
        while True:
            j = block.find(needle, start)
            if j < 0:
                break
            end = matching_paren(block, j)
            chunk = block[j : end + 1]
            start = end + 1
            if f'(layer "{layer}")' not in chunk:
                continue
            for x, y in re.findall(r"\((?:start|end|center) ([0-9.+-]+) ([0-9.+-]+)\)", chunk):
                xs.append(float(x))
                ys.append(float(y))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _world_aabb(
    local: tuple[float, float, float, float],
    at: tuple[float, float, float],
) -> tuple[float, float, float, float]:
    fx, fy, rot = at
    x0, y0, x1, y1 = local
    pts = [_rot_pt(x, y, rot) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs = [fx + p[0] for p in pts]
    ys = [fy + p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad: float = 0.15,
) -> bool:
    return not (
        a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1]
    )


def ref_font_mm(keepout_span: float) -> float:
    if keepout_span < 2.0:
        return 0.5
    if keepout_span < 5.0:
        return 0.6
    return 0.8


def _text_aabb(wx: float, wy: float, w: float, h: float) -> tuple[float, float, float, float]:
    return wx - w / 2, wy - h / 2, wx + w / 2, wy + h / 2


def _candidates(
    keep: tuple[float, float, float, float],
    tw: float,
    th: float,
    gap: float,
) -> list[tuple[float, float, float]]:
    """(x, y, text rotation): above, below, beside, the corners, then turned 90 beside the
    part for the narrow slot between two neighbours (the schematic's rotated-text spots)."""
    x0, y0, x1, y1 = keep
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    g = gap
    return [
        (cx, y0 - g - th / 2, 0.0),
        (cx, y1 + g + th / 2, 0.0),
        (x0 - g - tw / 2, cy, 0.0),
        (x1 + g + tw / 2, cy, 0.0),
        (x0 - g - tw / 2, y0 - g - th / 2, 0.0),
        (x1 + g + tw / 2, y0 - g - th / 2, 0.0),
        (x0 - g - tw / 2, y1 + g + th / 2, 0.0),
        (x1 + g + tw / 2, y1 + g + th / 2, 0.0),
        (cx - tw, y0 - g - th / 2, 0.0),
        (cx + tw, y0 - g - th / 2, 0.0),
        (cx - tw, y1 + g + th / 2, 0.0),
        (cx + tw, y1 + g + th / 2, 0.0),
        (x0 - g - th / 2, cy, 90.0),
        (x1 + g + th / 2, cy, 90.0),
        (cx, y0 - g - tw / 2, 90.0),
        (cx, y1 + g + tw / 2, 90.0),
    ]


_PAD_RE = re.compile(r'\(pad\s+"[^"]*"[^()]*\(at\s+([0-9.+-]+)\s+([0-9.+-]+)(?:\s+([0-9.+-]+))?\)\s*\(size\s+([0-9.+-]+)\s+([0-9.+-]+)\)')


def _pad_boxes(block: str, at: tuple[float, float, float]) -> list[tuple[float, float, float, float]]:
    """World AABBs of a footprint's pads (a pad's file angle already includes the footprint's)."""
    from .css import rotate_local_bounds

    fx, fy, frot = at
    out = []
    for m in _PAD_RE.finditer(block):
        px, py, prot = float(m.group(1)), float(m.group(2)), float(m.group(3) or 0)
        w, h = float(m.group(4)) / 2, float(m.group(5)) / 2
        x0, y0, x1, y1 = rotate_local_bounds(-w, -h, w, h, prot)
        cx, cy = _rot_pt(px, py, frot)
        out.append((fx + cx + x0, fy + cy + y0, fx + cx + x1, fy + cy + y1))
    return out


def _inside_candidates(keep, tw: float, th: float) -> list[tuple[float, float, float]]:
    """On a big part (a module, a connector body) the reference can sit on the body itself,
    away from its pads: the centre, then a little off-centre, both ways."""
    x0, y0, x1, y1 = keep
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    out = []
    for trot in (0.0, 90.0):
        w, h = (tw, th) if trot == 0 else (th, tw)
        for dx, dy in ((0, 0), (0, -h), (0, h), (-w, 0), (w, 0), (-w, -h), (w, -h), (-w, h), (w, h)):
            out.append((cx + dx, cy + dy, trot))
    return out


def _set_reference(
    block: str,
    *,
    lx: float,
    ly: float,
    prot: float,
    size: float,
    thick: float,
    hide: bool,
) -> str:
    j = block.find('(property "Reference"')
    if j < 0:
        return block
    open_at = block.find("(", j)
    end = matching_paren(block, open_at)
    prop = block[open_at : end + 1]
    prop = re.sub(
        r"\(at [0-9.+-]+ [0-9.+-]+(?: [0-9.+-]+)?\)",
        f"(at {lx:.4f} {ly:.4f} {prot:g})",
        prop,
        count=1,
    )
    prop = re.sub(r"\(size [0-9.]+ [0-9.]+\)", f"(size {size:g} {size:g})", prop, count=1)
    if re.search(r"\(thickness [0-9.]+\)", prop):
        prop = re.sub(r"\(thickness [0-9.]+\)", f"(thickness {thick:g})", prop, count=1)
    else:
        prop = prop.replace(
            f"(size {size:g} {size:g})",
            f"(size {size:g} {size:g})\n\t\t\t\t\t(thickness {thick:g})",
            1,
        )
    if hide and "(hide yes)" not in prop:
        prop = prop.replace('(layer "F.SilkS")', '(layer "F.SilkS")\n\t\t\t(hide yes)', 1)
    return block[:open_at] + prop + block[end + 1 :]


def legalize_silk(text: str, board_mm: tuple[float, float], hide_if_no_room: frozenset[str] = frozenset()) -> tuple[str, dict]:
    """Rewrite footprint Reference properties. Does not move footprints.

    A ref in `hide_if_no_room` (a decoupling cap: its distance to its pin matters more than its
    label) is hidden when nothing fits, and noted, instead of printed over a neighbour."""
    bw, bh = board_mm
    edge = (0.4, 0.4, bw - 0.4, bh - 0.4)
    parts: list[dict] = []
    for start, end in board_footprint_spans(text):
        block = text[start:end]
        ref = footprint_reference(block) or "?"
        at = footprint_at(block) or (0.0, 0.0, 0.0)
        crtyd = _bbox_on_layer(block, "F.CrtYd", ("fp_line", "fp_rect", "fp_poly", "fp_circle"))
        silk = _bbox_on_layer(block, "F.SilkS", ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"))
        local = crtyd or silk or (-0.8, -0.8, 0.8, 0.8)
        if silk:
            local = (
                min(local[0], silk[0]),
                min(local[1], silk[1]),
                max(local[2], silk[2]),
                max(local[3], silk[3]),
            )
        keep = _world_aabb(local, at)
        span = max(keep[2] - keep[0], keep[3] - keep[1])
        parts.append(
            {
                "start": start,
                "end": end,
                "ref": ref,
                "at": at,
                "keep": keep,
                "span": span,
                "block": block,
            }
        )

    parts.sort(key=lambda p: p["span"])
    occupied: list[tuple[float, float, float, float]] = []
    bodies = [p["keep"] for p in parts]
    report = {"moved": [], "hidden": [], "sized": [], "issues": [], "notes": []}
    names = {id(p["keep"]): p["ref"] for p in parts}

    new_blocks: dict[int, str] = {}
    for p in parts:
        ref = p["ref"]
        fx, fy, frot = p["at"]
        if ref.startswith("FID"):
            new_blocks[p["start"]] = _set_reference(
                p["block"], lx=0, ly=0, prot=0, size=0.5, thick=0.08, hide=True
            )
            report["hidden"].append(ref)
            continue
        size = ref_font_mm(p["span"])
        thick = round(min(0.12, size * 0.2), 3)
        tw, th = 0.72 * size * max(len(ref), 1), size
        placed = False
        big = p["span"] >= 8.0
        own_pads = _pad_boxes(p["block"], p["at"]) if big else []
        for attempt in (size, 0.5, 0.4):
            size = attempt
            thick = round(min(0.12, size * 0.2), 3)
            tw, th = 0.72 * size * max(len(ref), 1), size
            gap = 0.2 + size * 0.15
            spots = _candidates(p["keep"], tw, th, gap) + (_inside_candidates(p["keep"], tw, th) if big else [])
            for wx, wy, trot in spots:
                box = _text_aabb(wx, wy, tw, th) if trot == 0 else _text_aabb(wx, wy, th, tw)
                if box[0] < edge[0] or box[1] < edge[1] or box[2] > edge[2] or box[3] > edge[3]:
                    continue
                if any(_overlap(box, o) for o in occupied):
                    continue
                inside = _overlap(box, p["keep"], pad=0.0)
                if inside and any(_overlap(box, pb, pad=0.2) for pb in own_pads):
                    continue  # on the body is fine, on a pad is not
                if any(_overlap(box, b, pad=0.05) for b in bodies if b is not p["keep"]):
                    continue
                dx, dy = wx - fx, wy - fy
                lx, ly = _rot_pt(dx, dy, -frot)
                prot = (trot - frot) % 360
                new_blocks[p["start"]] = _set_reference(
                    p["block"], lx=lx, ly=ly, prot=prot, size=size, thick=thick, hide=False
                )
                occupied.append(box)
                report["moved"].append({"ref": ref, "at": [round(wx, 2), round(wy, 2)], "size": size})
                report["sized"].append({"ref": ref, "size": size})
                placed = True
                break
            if placed:
                break
        if not placed and ref in hide_if_no_room:
            new_blocks[p["start"]] = _set_reference(p["block"], lx=0, ly=0, prot=0, size=0.4, thick=0.08, hide=True)
            report["hidden"].append(ref)
            report["notes"].append(f"{ref}'s silkscreen reference is hidden: no room around it, and a decoupling cap stays at its pin")
            continue
        if not placed:
            new_blocks[p["start"]] = _set_reference(
                p["block"], lx=0, ly=-(p["span"] / 2 + 0.6), prot=(-frot) % 360,
                size=0.4, thick=0.08, hide=False,
            )
            report["moved"].append({"ref": ref, "at": "fallback", "size": 0.4})
            tw, th = 0.72 * 0.4 * max(len(ref), 1), 0.4
            over = _text_aabb(fx, fy - (p["span"] / 2 + 0.6), tw, th)
            hit = sorted({names[id(b)] for b in bodies if b is not p["keep"] and _overlap(over, b, pad=0.05)})
            what = ", ".join(hit) if hit else "another reference or the board edge"
            report["issues"].append(
                f"{ref}: no clear spot for its silkscreen reference around the part (0.8 to 0.4 mm text, both ways); "
                f"it prints above it over {what}: give {ref} room or move {hit[0] if hit else 'its neighbour'}"
            )

    # Rebuild from the original text using original spans (not mutated).
    pieces: list[str] = []
    last = 0
    for start, end in board_footprint_spans(text):
        pieces.append(text[last:start])
        pieces.append(new_blocks.get(start, text[start:end]))
        last = end
    pieces.append(text[last:])
    return "".join(pieces), report


def silk_job(
    job: CompiledJob,
    pcb: Path,
    *,
    out: Path | None = None,
    backup: bool = True,
    hide_if_no_room: frozenset[str] = frozenset(),
) -> dict:
    pcb = Path(pcb)
    text = pcb.read_text()
    new, report = legalize_silk(text, job.board_size_mm, hide_if_no_room)
    dest = Path(out) if out else pcb
    if dest == pcb and backup:
        bak = pcb.with_suffix(pcb.suffix + ".bak-silk")
        shutil.copy2(pcb, bak)
        report["backup"] = str(bak)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest != pcb:
        from .project import copy_with_siblings

        copy_with_siblings(pcb, dest)
    dest.write_text(new)
    return {"pcb": str(dest), "silk": report}
