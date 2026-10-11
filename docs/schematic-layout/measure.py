"""Readability numbers for a pcbc schematic, today's tool (frozen copy).

usage: measure.py MODE BOARD.py NAME OUTDIR
MODE: current  - the board's own SchPlace/SchRegion lines (today's sheet)
      none     - every SchPlace/SchRegion removed, today's tool unchanged
      packer   - every SchPlace/SchRegion removed, sch_emit._layout (the in-tree no-input packer) places
"""
from __future__ import annotations

import json
import math
import re
import sys
from itertools import combinations
from pathlib import Path

import os

# The tool under measurement: PCBC_SRC, else this tree's src/. The numbers in
# docs/schematic-layout.md were taken on a frozen copy of the tree at 9225fd3 + the 2026-10-03 working tree.
SRC = Path(os.environ.get("PCBC_SRC") or Path(__file__).resolve().parents[2] / "src")
sys.path.insert(0, str(SRC))

import pcbc  # noqa: E402

assert str(SRC) in pcbc.__file__, pcbc.__file__
from pcbc import sch_emit as E  # noqa: E402
from pcbc.language import load_board  # noqa: E402
from pcbc.netcheck import check_erc, check_schematic  # noqa: E402
from pcbc.sch_place import body_aabb, core_aabb  # noqa: E402

mode, board, name, outdir = sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
outdir.mkdir(parents=True, exist_ok=True)
design = load_board(board)
if mode != "current":
    design.sch_places = []
    design.sch_regions = []
kinds = {n.name: n.kind for n in design.nets.values()}

calls: list = []
_orig_annotate = E._annotate
_orig_sheet = E._Sheet


class _Cap(_orig_sheet):
    def __init__(self, parts):
        super().__init__(parts)
        calls.append({"sheet": self, "parts": parts})


E._Sheet = _Cap
if mode == "packer":
    E.apply_sch_places = lambda d, parts, extra=None: E._layout(parts, kinds)

report: dict = {}
sch = outdir / f"{name}.kicad_sch"
try:
    text = E.emit_from_design(design, title=name, report=report)
except ValueError as exc:
    print(json.dumps({"board": name, "mode": mode, "refused": str(exc)[:300]}))
    sys.exit(0)
sch.write_text(text)

# The pass emit kept: its parts carry the poses the report lists.
want = {(p["ref"], p["x"], p["y"]) for p in report["parts"]}
pick = None
for c in calls:
    got = {(p.ref, round(p.x, 3), round(p.y, 3)) for p in c["parts"]}
    if got == want:
        pick = c
assert pick is not None
sheet, parts = pick["sheet"], pick["parts"]


def ov(a, b):
    return E._overlap_area(a, b)


def gap(a, b):
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    if dx == 0 and dy == 0:
        return -1.0 if ov(a, b) > 0 else 0.0
    return math.hypot(dx, dy)


bodies = {p.ref: body_aabb(p) for p in parts}
cores = {p.ref: core_aabb(p) for p in parts}
body_overlaps = [(a, b) for a, b in combinations(sorted(bodies), 2) if ov(bodies[a], bodies[b]) > 0.0]
gaps = [gap(bodies[a], bodies[b]) for a, b in combinations(sorted(bodies), 2)]
# 1e-3 so that an on-grid 2.54 mm gap held as 2.5399... is not counted as tight.
tight = sum(1 for g in gaps if 0.0 <= g < 2.54 - 1e-3)
segs = sheet.segs
through = sum(1 for (x0, y0, x1, y1, _n) in segs for r, c in cores.items() if E._seg_through_box(x0, y0, x1, y1, c))


def cross(s, t):
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


crossings = sum(1 for s, t in combinations(segs, 2) if cross(s, t))
text_kinds = {"label", "hat", "ref", "value"}
occ = [o for o in sheet.occupants if o.kind != "reserve"]
collide = 0
for a, b in combinations(occ, 2):
    if a.kind not in text_kinds and b.kind not in text_kinds:
        continue
    if a.owner == b.owner and {a.kind, b.kind} == {"ref", "value"}:
        continue
    if ov(a.box, b.box) >= 0.6:
        collide += 1
xs = [v for o in occ for v in (o.box[0], o.box[2])] + [v for s in segs for v in (s[0], s[2])]
ys = [v for o in occ for v in (o.box[1], o.box[3])] + [v for s in segs for v in (s[1], s[3])]
w, h = max(xs) - min(xs), max(ys) - min(ys)


def boxgap(a, b):
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)


# Defects a person sees that the hard numbers above miss (review-readability.md M1; extra_metrics.py there).
# A wire of another net through a label, Reference/Value, pin text or a power-symbol graphic.
wire_through_text = 0
for x0, y0, x1, y1, net in segs:
    for o in occ:
        if (o.kind in text_kinds | {"pin"} and o.net != net and ov(E._seg_box(x0, y0, x1, y1), o.box) > 0.6) or (
            o.kind == "hat" and E._seg_through_box(x0, y0, x1, y1, E._graphic_box(o))
        ):
            wire_through_text += 1
# Text within 0.25 mm of other text but under the 0.6 mm2 collision threshold ("VBUS" + "A5" reads "VBUSA5").
crowded = 0
for a, b in combinations(occ, 2):
    if a.kind not in text_kinds and b.kind not in text_kinds:
        continue
    if a.owner == b.owner and {a.kind, b.kind} == {"ref", "value"}:
        continue
    if a.kind in text_kinds | {"pintext"} and b.kind in text_kinds | {"pintext"} and ov(a.box, b.box) < 0.6 and boxgap(a.box, b.box) < 0.25:
        crowded += 1
# A Reference nearer (by 0.5 mm) to another part's body than to its own.
ambiguous = 0
for o in occ:
    if o.kind == "ref" and o.owner in bodies:
        near = min((boxgap(o.box, b) for r, b in bodies.items() if r != o.owner), default=99.0)
        if near + 0.5 < boxgap(o.box, bodies[o.owner]):
            ambiguous += 1
# Part bodies outside the paper the emitter chose.
paper = re.search(r'\(paper "(\w+)"\)', text).group(1)
pw, ph = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420)}[paper]
off_paper = sum(1 for b in bodies.values() if b[0] < 0 or b[1] < 0 or b[2] > pw or b[3] > ph)
# The farthest two labels of one net (how far a reader hunts for the partner).
lab: dict = {}
for o in occ:
    if o.kind == "label":
        lab.setdefault(o.net, []).append(((o.box[0] + o.box[2]) / 2, (o.box[1] + o.box[3]) / 2))
partner = max((math.hypot(p[0] - q[0], p[1] - q[1]) for v in lab.values() for p, q in combinations(v, 2)), default=0.0)
notes = report.get("notes") or []
upside_down_power = sum(1 for n in notes if "points" in n)
wrong_way_up = sum(1 for i in report.get("issues", []) if "wrong way up" in i)
sym_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in bodies.values())
labels = sum(1 for o in occ if o.kind == "label")
hats = sum(1 for o in occ if o.kind == "hat")
wire_mm = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in segs)
try:
    net = check_schematic(design, sch)
    erc = check_erc(sch)
    netlist = "verified" if not net else f"{len(net)} mismatch"
    ercs = "clean" if not erc["errors"] else f"{len(erc['errors'])} errors"
except Exception as exc:  # noqa: BLE001
    netlist = ercs = f"unchecked: {exc}"[:80]
print(
    json.dumps(
        {
            "board": name,
            "mode": mode,
            "parts": len(parts),
            "lint_issues": len(report.get("issues", [])),
            "body_overlaps": len(body_overlaps),
            "wire_through_symbol": through,
            "crossings": crossings,
            "text_collisions": collide,
            "tight_pairs_lt_2.54": tight,
            "min_gap_mm": round(min(gaps), 3) if gaps else None,
            "extent_mm": [round(w, 1), round(h, 1)],
            "fill_pct": round(100 * sym_area / (w * h), 1) if w * h else None,
            "labels": labels,
            "power_symbols": hats,
            "wires": len(segs),
            "wire_mm": round(wire_mm, 1),
            "wire_through_text": wire_through_text,
            "text_crowding_lt0.25": crowded,
            "ambiguous_names": ambiguous,
            "paper": paper,
            "parts_off_paper": off_paper,
            "label_partner_mm": round(partner, 1),
            "upside_down_power": upside_down_power,
            "wrong_way_up": wrong_way_up,
            "netlist": netlist,
            "erc": ercs,
        }
    )
)
