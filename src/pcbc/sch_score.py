"""What "readable" means for a schematic sheet, as numbers (docs/schematic-layout.md §2.6).

Every sheet is scored as a vector. The order is lexicographic — the sum of the hard numbers, then `lint_issues`,
then `labels`, then `label_partner_mm`, then the extent area — so the layout loop picks between passes with no
invented weights (`key`).

- **Generator invariants** (asserted, not scored): no two wires of different nets cross, and no wire runs through
  a symbol's graphics. `segment_ok` refuses both; a sheet that has one is a generator bug (`invariant_errors`), tested
  here with an exact segment-box test.
- **Hard** (must be 0): `body_overlaps`, `text_collisions`, `wire_through_text`, `parts_off_paper`, `region_frames`,
  `wire_over_pin` (a wire that crosses, runs along or ends on a pin's drawn line anywhere but its electrical end),
  `wire_overlaps` (two wires on one stretch, or a junction dot where fewer than three ways meet).
- **Soft** (reported, ratcheted by the picture benchmark): `lint_issues`, `labels`, `label_partner_mm`,
  `power_symbols`, `wrong_way_up`, `upside_down_power`, `text_crowding`, `ambiguous_names`, `min_gap_mm`,
  `tight_pairs`, `extent_mm`, `fill_pct`, `wire_mm`.

`text_collisions` is computed from text boxes: pcbc's model of them while the loop ranks passes, and **KiCad's own
SVG** of the emitted sheet for the number the build reports (`svg_texts`, `with_svg`): the model is wrong for some
rotated and mirrored symbols, and KiCad is the arbiter (docs/direction.md §3).
"""

from __future__ import annotations

import math
import re
from itertools import combinations

HARD = ("body_overlaps", "text_collisions", "wire_through_text", "parts_off_paper", "region_frames", "wire_over_pin", "wire_overlaps")
SOFT = (
    "lint_issues", "labels", "label_partner_mm", "power_symbols", "wrong_way_up", "upside_down_power", "text_crowding",
    "ambiguous_names", "min_gap_mm", "tight_pairs", "extent_mm", "fill_pct", "wire_mm",
)
PAPERS = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0)}
# KiCad's default drawing sheet: the title block is 108 x 32 mm in the bottom-right corner of the inner border, 12 mm
# from the paper's edge (measured on `kicad-cli sch export svg` of an A3 sheet: 299.989..407.989 x 253..285).
TITLE_BLOCK = (108.0, 32.0)
FRAME = 12.0
COLLIDE_MM2 = 0.6  # the lint's threshold: overlap below this is touching, not colliding
CROWD_MM = 0.25
TEXT_KINDS = frozenset({"label", "hat", "ref", "value"})
# A lint line that is a fact about the symbol file, which no layout can change: reported, never scored.
LIBRARY_FACT = "that is the .kicad_sym"


def _area(a, b) -> float:
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx * dy if dx > 0 and dy > 0 else 0.0


def _gap(a, b) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)


def seg_through_box(x0: float, y0: float, x1: float, y1: float, box) -> bool:
    """An axis-aligned (or any) segment's interior meets the box's interior: exact (Liang-Barsky), not sampled."""
    bx0, by0, bx1, by1 = box
    eps = 1e-6
    bx0, by0, bx1, by1 = bx0 + eps, by0 + eps, bx1 - eps, by1 - eps
    if bx0 >= bx1 or by0 >= by1:
        return False
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - bx0), (dx, bx1 - x0), (-dy, y0 - by0), (dy, by1 - y0)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return False
    return t1 - t0 > 1e-9


def _cross(s, t) -> bool:
    ax0, ay0, ax1, ay1, an = s
    bx0, by0, bx1, by1, bn = t
    if an == bn:
        return False
    ah, bh = abs(ay0 - ay1) < 1e-6, abs(by0 - by1) < 1e-6
    if ah == bh:
        return False
    h, v = (s, t) if ah else (t, s)
    hx0, hx1 = sorted((h[0], h[2]))
    vy0, vy1 = sorted((v[1], v[3]))
    return hx0 + 1e-6 < v[0] < hx1 - 1e-6 and vy0 + 1e-6 < h[1] < vy1 - 1e-6


def invariant_errors(sheet, parts) -> list[str]:
    """The two numbers the generator makes 0 by construction, as facts when they are not (a generator bug)."""
    from .sch_place import core_aabb

    out: list[str] = []
    segs = sheet.segs
    for s, t in combinations(segs, 2):
        if _cross(s, t):
            out.append(f"wire {s[4]} crosses wire {t[4]} at x {t[0] if abs(t[0] - t[2]) < 1e-6 else s[0]:g}")
    for x0, y0, x1, y1, net in segs:
        for p in parts:
            if seg_through_box(x0, y0, x1, y1, core_aabb(p)):
                out.append(f"wire {net} ({x0:g},{y0:g})-({x1:g},{y1:g}) runs through symbol {p.ref}")
    return out


# --------------------------------------------------------------------------- KiCad's SVG

_TEXT = re.compile(
    r'(?:<g transform="rotate\((?P<ang>[-0-9.]+) (?P<cx>[-0-9.]+) (?P<cy>[-0-9.]+)\)">\s*)?'
    r'<text x="(?P<x>[-0-9.]+)" y="(?P<y>[-0-9.]+)"\s*textLength="(?P<len>[0-9.]+)" font-size="(?P<fs>[0-9.]+)"'
    r'[^>]*?text-anchor="(?P<anchor>start|middle|end)"[^>]*>(?P<s>[^<]*)</text>'
)


def svg_texts(svg: str) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Every text KiCad drew on the sheet, as (string, box in sheet mm): the invisible `<text>` KiCad writes over each
    stroked text, its anchor, its length and its rotation. The box is the cap height above the baseline."""
    import html

    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for m in _TEXT.finditer(svg):
        x, y, length, fs = float(m["x"]), float(m["y"]), float(m["len"]), float(m["fs"])
        h = fs * 0.75  # KiCad's size is the cap height; the SVG font-size is size * 4/3
        x0 = {"start": x, "middle": x - length / 2.0, "end": x - length}[m["anchor"]]
        corners = [(x0, y - h), (x0 + length, y - h), (x0 + length, y + h * 0.05), (x0, y + h * 0.05)]
        if m["ang"] is not None:
            a = math.radians(float(m["ang"]))
            cx, cy = float(m["cx"]), float(m["cy"])
            c, s = math.cos(a), math.sin(a)
            corners = [(cx + (px - cx) * c - (py - cy) * s, cy + (px - cx) * s + (py - cy) * c) for px, py in corners]
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        out.append((html.unescape(m["s"]), (min(xs), min(ys), max(xs), max(ys))))
    return out


def _text_of(o, parts_by_ref) -> list[str]:
    """The strings an occupant draws: a Reference, a Value, a label's net, a power symbol's net, a pin number."""
    if o.kind == "ref":
        return [o.owner]
    if o.kind == "value":
        p = parts_by_ref.get(o.owner)
        return [getattr(p, "display", "")] if p is not None else []
    if o.kind == "hat" and (o.tag or "").startswith("flag:"):
        return ["PWR_FLAG"]  # a PWR_FLAG's value text is its name, not the net's
    if o.kind in ("label", "hat"):
        return [o.net]
    if o.kind == "pintext":
        return [o.owner.split(".", 1)[1]]
    return []


def with_svg(occupants, parts, svg: str, *, reach: float = 8.0):
    """`occupants` with each text box replaced by the box KiCad drew for the same string nearest to it (within
    `reach` mm); a power symbol keeps its graphic and takes KiCad's box for its name. Returns (occupants, matched,
    unmatched)."""
    from dataclasses import replace

    from .sch_emit import _graphic_box

    texts = svg_texts(svg)
    by_s: dict[str, list[tuple[float, float, float, float]]] = {}
    for s, box in texts:
        by_s.setdefault(s, []).append(box)
    by_ref = {p.ref: p for p in parts}
    used: set[tuple[str, int]] = set()
    out = []
    matched = unmatched = 0
    for o in occupants:
        strings = _text_of(o, by_ref)
        if not strings:
            out.append(o)
            continue
        cx, cy = (o.box[0] + o.box[2]) / 2.0, (o.box[1] + o.box[3]) / 2.0
        best = None
        for s in strings:
            for i, box in enumerate(by_s.get(s, ())):
                if (s, i) in used:
                    continue
                d = math.hypot((box[0] + box[2]) / 2.0 - cx, (box[1] + box[3]) / 2.0 - cy)
                if d <= reach and (best is None or d < best[0]):
                    best = (d, s, i, box)
        if best is None:
            unmatched += 1
            out.append(o)
            continue
        matched += 1
        used.add((best[1], best[2]))
        if o.kind == "hat":
            g = _graphic_box(o)
            o2 = replace(o, box=g)
            o2.text_box = best[3]
            o2.rot = getattr(o, "rot", None)
            out.append(o2)
        else:
            o2 = replace(o, box=best[3])
            o2.rot = getattr(o, "rot", None)
            out.append(o2)
    return out, matched, unmatched


def _boxes(o):
    tb = getattr(o, "text_box", None)
    return [o.box, tb] if tb is not None else [o.box]


def _overlap(a, b) -> float:
    return max((_area(x, y) for x in _boxes(a) for y in _boxes(b)), default=0.0)


def _paper_box(paper: str) -> tuple[float, float]:
    return PAPERS.get(paper, PAPERS["A2"])


def score(sheet, parts, report: dict, *, paper: str, region_boxes=(), occupants=None, svg_matched: int | None = None) -> dict:
    """The readability vector of one drawn sheet. `occupants` replaces the sheet's own (text boxes from KiCad's SVG);
    `region_boxes` are the drawn region outlines (`sch_emit._region_boxes`), each `{name, x0, y0, x1, y1, refs}`."""
    from .sch_place import body_aabb

    occ = [o for o in (occupants if occupants is not None else sheet.occupants) if o.kind != "reserve"]
    bodies = {p.ref: body_aabb(p) for p in parts}
    refs = sorted(bodies)
    body_overlaps = [(a, b) for a, b in combinations(refs, 2) if _area(bodies[a], bodies[b]) > 0.0]
    gaps = []
    for a, b in combinations(refs, 2):
        g = _gap(bodies[a], bodies[b])
        gaps.append(-1.0 if g == 0.0 and _area(bodies[a], bodies[b]) > 0 else g)
    tight = sum(1 for g in gaps if 0.0 <= g < 2.54 - 1e-3)
    collisions: list[tuple[object, object, float]] = []
    crowded = 0
    for a, b in combinations(occ, 2):
        if a.kind not in TEXT_KINDS and b.kind not in TEXT_KINDS:
            continue
        if a.owner == b.owner and {a.kind, b.kind} == {"ref", "value"}:
            continue
        ov = _overlap(a, b)
        if ov >= COLLIDE_MM2:
            collisions.append((a, b, ov))
        elif a.kind in TEXT_KINDS | {"pintext"} and b.kind in TEXT_KINDS | {"pintext"} and min(_gap(x, y) for x in _boxes(a) for y in _boxes(b)) < CROWD_MM:
            crowded += 1
    # One fact once: a two-pin symbol is drawn as two boxes (its core and its body with the pins), and a label over both
    # was one overlap counted twice (refute:lock 2026-10-09: buck's L1 posed onto U1's SW label).
    once: dict[tuple[str, str], tuple[object, object, float]] = {}
    for a, b, ov in collisions:
        k = tuple(sorted((_describe(a), _describe(b))))
        if k not in once or ov > once[k][2]:
            once[k] = (a, b, ov)
    collisions = list(once.values())
    from .sch_emit import _graphic_box, _seg_box

    through: list[tuple[str, object]] = []
    for x0, y0, x1, y1, net in sheet.segs:
        for o in occ:
            text_hit = o.kind in TEXT_KINDS | {"pin"} and o.net != net and max(_area(_seg_box(x0, y0, x1, y1), bx) for bx in _boxes(o)) > COLLIDE_MM2
            sym_hit = o.kind == "hat" and seg_through_box(x0, y0, x1, y1, _graphic_box(o) if getattr(o, "text_box", None) is None else o.box)
            if text_hit or sym_hit:
                through.append((net, o))
    from .sch_emit import over_pin_line, pin_lines

    over_pin = [(net, (x0, y0, x1, y1), ln[4]) for x0, y0, x1, y1, net in sheet.segs for ln in pin_lines(parts) if over_pin_line(x0, y0, x1, y1, ln)]
    from .sch_emit import wire_overlaps

    overlaps = wire_overlaps(sheet, parts)
    pw, ph = _paper_box(paper)
    tb = (pw - FRAME - TITLE_BLOCK[0], ph - FRAME - TITLE_BLOCK[1], pw - FRAME, ph - FRAME)
    # Inside the drawing sheet's border (12 mm in from the paper's edge) and clear of the title block.
    off = [r for r, b in bodies.items() if b[0] < FRAME or b[1] < FRAME or b[2] > pw - FRAME or b[3] > ph - FRAME or _area(b, tb) > 0]
    frames: list[str] = []
    rb = list(region_boxes)
    for a, b in combinations(rb, 2):
        if _area((a["x0"], a["y0"], a["x1"], a["y1"]), (b["x0"], b["y0"], b["x1"], b["y1"])) > 0:
            frames.append(f"region {a['name']} overlaps region {b['name']}")
    for box in rb:
        for r, body in bodies.items():
            if r in box.get("refs", ()):
                continue
            if _area((box["x0"], box["y0"], box["x1"], box["y1"]), body) > 0:
                frames.append(f"{r} is inside region {box['name']} and not one of its members")
    labels: dict[str, list[tuple[float, float]]] = {}
    for o in occ:
        if o.kind == "label":
            labels.setdefault(o.net, []).append(((o.box[0] + o.box[2]) / 2, (o.box[1] + o.box[3]) / 2))
    partner = max((math.hypot(p[0] - q[0], p[1] - q[1]) for v in labels.values() for p, q in combinations(v, 2)), default=0.0)
    from .sch_emit import cut_off

    ambiguous: dict[str, str] = {}  # one per part: its name reads as another part's
    for o in occ:
        if o.kind not in ("ref", "value") or o.owner not in bodies or o.owner in ambiguous:
            continue
        own = _gap(o.box, bodies[o.owner])
        near = min(((_gap(o.box, b), r) for r, b in bodies.items() if r != o.owner), default=(99.0, ""))
        if o.kind == "ref" and near[0] + 0.5 < own:
            ambiguous[o.owner] = f"{o.owner}'s Reference is {near[0]:.2f} mm from {near[1]} and {own:.2f} mm from {o.owner}"
            continue
        # A name a wire separates from its body reads as the next part's (refute:sheet 2026-10-09: DS2's R4).
        seg = cut_off(sheet.segs, o.box, bodies[o.owner])
        if seg is not None:
            x0, y0, x1, y1, net = seg
            field = "Reference" if o.kind == "ref" else "Value"
            ambiguous[o.owner] = f"wire {net} ({x0:g}, {y0:g})-({x1:g}, {y1:g}) runs between {o.owner} and its {field}"
    issues = [i for i in (report.get("issues") or []) if LIBRARY_FACT not in i]
    notes = report.get("notes") or []
    xs = [v for o in occ for b in _boxes(o) for v in (b[0], b[2])] + [v for s in sheet.segs for v in (s[0], s[2])]
    ys = [v for o in occ for b in _boxes(o) for v in (b[1], b[3])] + [v for s in sheet.segs for v in (s[1], s[3])]
    w, h = (max(xs) - min(xs), max(ys) - min(ys)) if xs else (0.0, 0.0)
    sym_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in bodies.values())
    vec = {
        "body_overlaps": len(body_overlaps),
        "text_collisions": len(collisions),
        "wire_through_text": len(through),
        "parts_off_paper": len(off),
        "region_frames": len(frames),
        "wire_over_pin": len(over_pin),
        "wire_overlaps": len(overlaps),
        "lint_issues": len(issues),
        "labels": sum(1 for o in occ if o.kind == "label"),
        "label_partner_mm": round(partner, 1),
        "power_symbols": sum(1 for o in occ if o.kind == "hat"),
        "wrong_way_up": sum(1 for i in issues if "wrong way up" in i),
        "upside_down_power": sum(1 for n in notes if " points " in n),
        "text_crowding": crowded,
        "ambiguous_names": len(ambiguous),
        "min_gap_mm": round(min(gaps), 3) if gaps else None,
        "tight_pairs": tight,
        "extent_mm": [round(w, 1), round(h, 1)],
        "fill_pct": round(100 * sym_area / (w * h), 1) if w * h else None,
        "wire_mm": round(sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in sheet.segs), 1),
        "paper": paper,
    }
    if svg_matched is not None:
        vec["text_source"] = "kicad-svg"
    detail = {
        "body_overlaps": [f"{a} overlaps {b} by {_area(bodies[a], bodies[b]):.2f} mm2" for a, b in body_overlaps],
        "text_collisions": [f"{_describe(a)} overlaps {_describe(b)} by {ov:.2f} mm2" for a, b, ov in collisions],
        "wire_through_text": [f"wire {net} runs through {_describe(o)}" for net, o in through],
        "parts_off_paper": [_off_fact(r, bodies[r], paper, pw, ph, tb) for r in off],
        "region_frames": frames,
        "wire_over_pin": [f"wire {net} ({_f(a[0])}, {_f(a[1])})-({_f(a[2])}, {_f(a[3])}) runs over {pin}'s pin line" for net, a, pin in over_pin],
        "wire_overlaps": overlaps,
    }
    vec["hard"] = sum(vec[k] for k in HARD)
    vec["_ambiguous"] = list(ambiguous.values())  # the facts behind ambiguous_names (a soft number: not in `detail`)
    vec["detail"] = {k: v for k, v in detail.items() if v}
    # The parts each hard finding involves (owners of what collides), for the layout loop's repair pushes.
    involved: list[tuple[str, ...]] = [tuple(pair) for pair in body_overlaps]
    involved += [(a.owner.split(".", 1)[0], b.owner.split(".", 1)[0]) for a, b, _ov in collisions]
    on_net: dict[str, list[str]] = {}
    for p in parts:
        for pn in p.pins:
            if pn.net:
                on_net.setdefault(pn.net, []).append(p.ref)
    involved += [(*on_net.get(net, []), o.owner.split(".", 1)[0]) for net, o in through]
    involved += [(*on_net.get(net, []), pin.split(".", 1)[0]) for net, _a, pin in over_pin]
    vec["_involved"] = involved
    return vec


def _off_fact(ref: str, b, paper: str, pw: float, ph: float, tb) -> str:
    """Where a body leaves the drawing area, in mm: past the border 12 mm in from the paper's edge, or into the title
    block."""
    out = max(FRAME - b[0], FRAME - b[1], b[2] - (pw - FRAME), b[3] - (ph - FRAME))
    if out > 0:
        return f"{ref}'s body is {out:.2f} mm outside the {paper} sheet's border ({pw - 2 * FRAME:g} x {ph - 2 * FRAME:g} mm inside it)"
    return f"{ref}'s body overlaps the {paper} sheet's title block by {_area(b, tb):.2f} mm2"


def _f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _describe(o) -> str:
    from .sch_emit import _describe as d

    return d(o)


def key(vec: dict) -> tuple:
    """Lexicographic order of sheets: hard sum, lint, labels, label partner distance, extent area (§2.6)."""
    w, h = vec.get("extent_mm") or (0.0, 0.0)
    return (vec["hard"], vec["lint_issues"], vec["labels"], vec["label_partner_mm"], round(w * h, 1))


def line(vec: dict) -> str:
    """The vector in one line, as `pcbc sch` prints it and `schematic.gen.py` heads with it."""
    w, h = vec.get("extent_mm") or (0.0, 0.0)
    hard = " ".join(str(vec[k]) for k in HARD)
    return (
        f"hard {hard} (body overlaps, text collisions, wires through text, parts off paper, region frames, wires over pins, wire overlaps) | "
        f"lint {vec['lint_issues']}, labels {vec['labels']}, partner {vec['label_partner_mm']} mm, "
        f"power symbols {vec['power_symbols']}, extent {w} x {h} mm, paper {vec.get('paper')}"
    )


def kicad_vector(sch_path, report: dict, svg: str | None = None) -> dict:
    """The vector of the kept pass with every text box from KiCad's SVG of the emitted sheet (`netcheck.sheet_svg`):
    the number the build reports. `report` is `emit_from_design`'s, carrying the kept pass (`_sheet`, `_parts`,
    `_boxes`)."""
    from .netcheck import sheet_svg

    if svg is None:
        svg = sheet_svg(sch_path)
    sheet, parts = report["_sheet"], report["_parts"]
    occ, matched, unmatched = with_svg(sheet.occupants, parts, svg)
    vec = score(sheet, parts, report, paper=report["readability"]["paper"], region_boxes=report.get("_boxes") or (), occupants=occ, svg_matched=matched)
    vec["svg_texts_matched"] = matched
    vec["svg_texts_unmatched"] = unmatched
    return vec
